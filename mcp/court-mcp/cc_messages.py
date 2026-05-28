"""PR-21: Read cc-connect session JSON files and expose unified Message stream.

cc-connect (1.3.2) 把 wechat/feishu 收发的消息持久化到
``~/.cc-connect/sessions/<project>_<hash>.json``. 本模块:

1. 读这些文件(快照式), 转成统一 Message dataclass
2. 提供 list_messages(limit, before) 给 REST history endpoint
3. 提供 watchdog-based subscribe() 给 SSE stream endpoint

零侵入 cc-connect: 我们只读它的持久化文件, 不调它的 API, 不动它的源码.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger("cc_messages")

# session_key 格式: "<platform>:<scope>:<user_id>@<host>"
# e.g. "weixin:dm:o9cq80x07FGiDZK2xxQ2KcEDzY_Q@im.wechat"
#      "feishu:dm:ou_xxx@feishu"
KNOWN_PLATFORMS = {"weixin", "feishu", "telegram", "slack", "dingtalk",
                   "discord", "line", "wecom-ws", "qq", "qqbot"}


@dataclass(frozen=True, slots=True)
class Message:
    platform: str          # weixin / feishu / unknown
    session_key: str       # cc-connect session_key (含 platform/scope/user_id)
    session_id: str        # cc-connect 内部 session id (s1/s2/...)
    project: str           # 项目名 (从 sessions 文件名解出)
    role: str              # user (inbound) / assistant (outbound)
    content: str           # 消息全文
    timestamp: str         # ISO8601 with tz, 直接从 cc-connect 来
    msg_id: str            # 复合 ID: "<project>:<session_id>:<history_index>"


def _resolve_sessions_dir() -> Path:
    return Path(os.environ.get("CC_CONNECT_HOME",
                               str(Path.home() / ".cc-connect"))) / "sessions"


def _platform_from_session_key(session_key: str) -> str:
    """从 'weixin:dm:user@host' 提取 'weixin'. 兜底 'unknown'."""
    if not session_key:
        return "unknown"
    head = session_key.split(":", 1)[0]
    return head if head in KNOWN_PLATFORMS else "unknown"


def parse_session_file(path: Path, *, project: str) -> list[Message]:
    """解一个 sessions/<project>_<hash>.json, 返 Message 列表(按 history 原始顺序)。

    异常 / 损坏文件 → 返空列表, 不抛。
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("parse_session_file %s failed: %s", path, exc)
        return []

    # active_session: {session_key: session_id} — 反向查 session_id → session_key
    active = data.get("active_session") or {}
    sid_to_key: dict[str, str] = {}
    for skey, sid in active.items():
        sid_to_key[sid] = skey

    out: list[Message] = []
    for sid, sess in (data.get("sessions") or {}).items():
        if not isinstance(sess, dict):
            continue
        skey = sid_to_key.get(sid, "")
        platform = _platform_from_session_key(skey)
        for idx, h in enumerate(sess.get("history") or []):
            if not isinstance(h, dict):
                continue
            out.append(Message(
                platform=platform,
                session_key=skey,
                session_id=sid,
                project=project,
                role=str(h.get("role") or "unknown"),
                content=str(h.get("content") or ""),
                timestamp=str(h.get("timestamp") or ""),
                msg_id=f"{project}:{sid}:{idx}",
            ))
    return out


def _project_from_filename(stem: str) -> Optional[str]:
    """sessions 文件名格式 '<project>_<hash>.json' → 取 '_' 之前."""
    if "_" not in stem:
        return None
    return stem.split("_", 1)[0]


def list_messages(
    *,
    limit: int = 50,
    before: Optional[str] = None,
) -> list[Message]:
    """聚合 sessions 目录所有 *.json, 按 timestamp 降序返最多 limit 条。

    Args:
        limit: 最多返回多少条
        before: ISO8601 时间字符串, 只返时间戳**严格小于** before 的(用于翻页)
    """
    sessions_dir = _resolve_sessions_dir()
    if not sessions_dir.is_dir():
        return []

    all_msgs: list[Message] = []
    for fp in sessions_dir.glob("*.json"):
        project = _project_from_filename(fp.stem)
        if not project:
            continue
        all_msgs.extend(parse_session_file(fp, project=project))

    all_msgs.sort(key=lambda m: m.timestamp or "", reverse=True)

    if before:
        all_msgs = [m for m in all_msgs if (m.timestamp or "") < before]

    return all_msgs[:limit]


@dataclass(frozen=True, slots=True)
class Exchange:
    pair_id: str
    platform: str
    session_key: str
    session_id: str
    project: str
    user: Optional[Message]
    assistant: Optional[Message]
    think_seconds: Optional[float]
    timestamp: str


def _think_seconds(user: Optional[Message], assistant: Optional[Message]) -> Optional[float]:
    if user is None or assistant is None:
        return None
    try:
        u = datetime.fromisoformat(user.timestamp)
        a = datetime.fromisoformat(assistant.timestamp)
    except (ValueError, TypeError):
        return None
    return (a - u).total_seconds()


def _make_exchange(user: Optional[Message], assistant: Optional[Message]) -> Exchange:
    rep = user or assistant
    assert rep is not None
    return Exchange(
        pair_id=(user.msg_id if user else assistant.msg_id),
        platform=rep.platform,
        session_key=rep.session_key,
        session_id=rep.session_id,
        project=rep.project,
        user=user,
        assistant=assistant,
        think_seconds=_think_seconds(user, assistant),
        timestamp=(user.timestamp if user else assistant.timestamp),
    )


def _history_idx(msg_id: str) -> int:
    # msg_id format: "<project>:<session_id>:<history_index>"; order by index,
    # not timestamp string (timestamps may be malformed; history order is truth).
    try:
        return int(msg_id.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return 0


def pair_messages(msgs: list[Message]) -> list[Exchange]:
    """把相邻 user->assistant 配成 Exchange. 按 (project, session_id) 分组后组内按 history 顺序配对.

    孤立 user / 孤立 assistant / 连续同 role 各自单独成 Exchange.
    """
    by_session: dict[tuple, list[Message]] = defaultdict(list)
    for m in msgs:
        by_session[(m.project, m.session_id)].append(m)

    out: list[Exchange] = []
    for group in by_session.values():
        group.sort(key=lambda m: _history_idx(m.msg_id))
        i = 0
        n = len(group)
        while i < n:
            cur = group[i]
            if cur.role == "user" and i + 1 < n and group[i + 1].role == "assistant":
                out.append(_make_exchange(cur, group[i + 1]))
                i += 2
            elif cur.role == "user":
                out.append(_make_exchange(cur, None))
                i += 1
            else:
                out.append(_make_exchange(None, cur))
                i += 1
    return out


try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore


def subscribe(
    *,
    callback: Callable[[Message], None],
) -> Callable[[], None]:
    """启动 watchdog 监听 sessions 目录, 每次文件改写都 diff 出新 Message 推给 callback。

    Returns:
        stop() — 调用以停止 observer
    """
    if Observer is None:
        raise RuntimeError("watchdog not installed")

    sessions_dir = _resolve_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    seen: dict[Path, set[str]] = {}
    lock = threading.Lock()

    for fp in sessions_dir.glob("*.json"):
        project = _project_from_filename(fp.stem)
        if not project:
            continue
        with lock:
            seen[fp] = {m.msg_id for m in parse_session_file(fp, project=project)}

    def _handle(fp: Path) -> None:
        project = _project_from_filename(fp.stem)
        if not project:
            return
        current = parse_session_file(fp, project=project)
        with lock:
            seen_ids = seen.setdefault(fp, set())
            new = [m for m in current if m.msg_id not in seen_ids]
            for m in new:
                seen_ids.add(m.msg_id)
        for m in new:
            try:
                callback(m)
            except Exception as exc:  # noqa: BLE001
                _log.warning("subscribe callback failed: %s", exc)

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):  # type: ignore
            if event.is_directory:
                return
            p = Path(event.src_path)
            if p.suffix != ".json":
                return
            _handle(p)

        def on_created(self, event):  # type: ignore
            if event.is_directory:
                return
            p = Path(event.src_path)
            if p.suffix != ".json":
                return
            _handle(p)

    obs = Observer()
    obs.schedule(_Handler(), str(sessions_dir), recursive=False)
    obs.start()

    def stop():
        obs.stop()
        obs.join(timeout=2.0)

    return stop
