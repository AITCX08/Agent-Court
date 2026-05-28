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
from dataclasses import dataclass
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
