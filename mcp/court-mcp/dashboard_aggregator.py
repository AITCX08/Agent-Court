"""PR-15 court-dashboard 状态聚合层.

把 tmux dashboard session / pending-approval / seen-issues / watcher / receiver
聚合成单个 JSON 供前端读取. 1s cache 避免 SSE 每条连接都触发 N 次 I/O;
fs watcher (T-15-02) 文件变更时调 ``emit_change()`` 让 cache 失效并广播给订阅者.

数据源约定 (跟 PR-12 / PR-13 / PR-14 现状对齐, 跟 WBS plan 数据结构有微调):
- ``pending-approval/<slug>.json`` 是真实路径 (plan 写 pending-court 是 typo)
- watcher / receiver 没有共同 pid 文件约定, 改用 ``pgrep -f`` 探测进程
- dashboard 模式实际是 **单 session + N window**: ``SESSION_NAME =
  'agent-court-dashboard'`` (来自 PR-13 ``dashboard_tmux.py``); ``courts``
  字段从该 session 的 window 列表派生, 跳过 ``watcher`` 这个保留 window.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from dashboard_tmux import SESSION_NAME as DASHBOARD_SESSION
from dashboard_tmux import WATCHER_WINDOW
from seen_state import default_state_dir, load_seen, state_lock

CACHE_TTL_SECONDS = 1.0
RECEIVER_DEFAULT_PORT = int(os.environ.get("WEBHOOK_PORT", "8765"))
DEBOUNCE_DEFAULT_MS = 200


class DashboardAggregator:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or default_state_dir()
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    async def aggregate_status(self) -> dict[str, Any]:
        async with self._cache_lock:
            now = time.time()
            if self._cache is not None and now - self._cache_ts < CACHE_TTL_SECONDS:
                return self._cache
            snapshot = await self._collect()
            self._cache = snapshot
            self._cache_ts = now
            return snapshot

    def invalidate_cache(self) -> None:
        self._cache = None
        self._cache_ts = 0.0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def emit_change(self, payload: dict[str, Any] | None = None) -> None:
        """fs watcher 文件变更时调; 失效 cache + 唤醒所有 SSE 订阅者.

        payload 不会原样推给前端 (SSE handler 收到任何唤醒消息都会重新调
        ``aggregate_status`` 拉最新 snapshot, 避免在 invalidate / 重新 collect
        之间出现 race). payload=None 时使用 ``{"_kick": True}`` 占位.
        """
        self.invalidate_cache()
        wake = payload if payload is not None else {"_kick": True}
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(wake)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    async def _collect(self) -> dict[str, Any]:
        tmux_state, pending, seen_data, watcher_info, receiver_info, retry_items = await asyncio.gather(
            _collect_tmux_state(),
            asyncio.to_thread(_collect_pending, self._state_dir),
            asyncio.to_thread(_load_seen_safe, self._state_dir),
            _collect_process_info("gitea_watcher"),
            _collect_process_info("gitea_webhook_receiver", port=RECEIVER_DEFAULT_PORT),
            asyncio.to_thread(_collect_retry_queue, self._state_dir),
        )
        courts = _derive_courts(tmux_state["windows"], pending, seen_data)
        return {
            "courts": courts,
            "tmux_sessions": tmux_state["sessions"],
            "pending": pending,
            "seen_issues_count": len(seen_data),
            "watcher": watcher_info,
            "receiver": receiver_info,
            "retry_queue": retry_items,
            "ts": int(time.time()),
        }


# ---------------------------------------------------------------------------
# tmux 状态采集 (T-15-03): dashboard session + window 列表
# ---------------------------------------------------------------------------

async def _collect_tmux_state() -> dict[str, Any]:
    """返回 ``{"sessions": [...], "windows": [...]}`` 两份元数据.

    sessions: 只关心 ``agent-court-dashboard`` (PR-13 dashboard 模式的固定 session).
    windows : 该 session 内全部 window (含 watcher 保留 window).

    tmux 不可用 / 无 session / 解析失败一律返空列表.
    """
    session_info = await _query_dashboard_session()
    if session_info is None:
        return {"sessions": [], "windows": []}
    windows = await _query_dashboard_windows()
    return {"sessions": [session_info], "windows": windows}


async def _query_dashboard_session() -> dict[str, Any] | None:
    fmt = "#{session_name}|#{session_windows}|#{session_attached}"
    rc, stdout = await _run_subprocess(
        "tmux", "list-sessions", "-F", fmt,
    )
    if rc != 0:
        return None
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, windows, attached = parts[0], parts[1], parts[2]
        if name != DASHBOARD_SESSION:
            continue
        try:
            return {
                "name": name,
                "windows": int(windows),
                "attached": attached.strip() == "1",
            }
        except ValueError:
            return None
    return None


async def _query_dashboard_windows() -> list[dict[str, Any]]:
    fmt = "#{window_index}|#{window_name}|#{window_active}|#{window_panes}"
    rc, stdout = await _run_subprocess(
        "tmux", "list-windows", "-t", DASHBOARD_SESSION, "-F", fmt,
    )
    if rc != 0:
        return []
    windows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            panes = int(parts[3])
        except ValueError:
            continue
        windows.append({
            "index": idx,
            "name": parts[1],
            "active": parts[2].strip() == "1",
            "panes": panes,
        })
    return windows


async def _run_subprocess(*args: str) -> tuple[int, str]:
    """通用 subprocess 包装: 兼容 ``tmux``/``pgrep`` 等; FileNotFound 返 (127, '')."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return 127, ""
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# pending-approval 采集
# ---------------------------------------------------------------------------

def _collect_pending(state_dir: Path) -> list[dict[str, Any]]:
    pending_dir = state_dir / "pending-approval"
    if not pending_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for json_path in sorted(pending_dir.glob("*.json")):
        if not json_path.is_file():
            continue
        slug = json_path.stem
        if (pending_dir / f"{slug}.result").exists():
            # 已审批; 不算 pending
            continue
        try:
            meta = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        items.append({
            "slug_id": meta.get("slug_id") or slug,
            "repo": meta.get("repo"),
            "number": meta.get("number"),
            "stage": meta.get("stage"),
            "created_at": meta.get("created_at"),
            "channels": meta.get("channels") or [],
        })
    return items


def _load_seen_safe(state_dir: Path) -> dict[str, Any]:
    try:
        with state_lock(state_dir):
            return load_seen(state_dir)
    except OSError:
        return {}


def _collect_retry_queue(state_dir: Path) -> list[dict[str, Any]]:
    """SY-4 #17: 把 retry queue 暴露给 UI. 不存在 / 模块未导入返 []."""
    try:
        from retry_queue import RetryQueue
    except ImportError:
        return []
    try:
        q = RetryQueue(state_dir=state_dir)
    except (ValueError, OSError):
        return []
    items = q.snapshot()
    return [
        {
            "issue_key": it.issue_key,
            "attempt": it.attempt,
            "next_at": it.next_at,
            "last_error": it.last_error,
            "last_failed_at": it.last_failed_at,
        }
        for it in items
    ]


# ---------------------------------------------------------------------------
# courts 派生 (从 dashboard windows + pending + seen-issues)
# ---------------------------------------------------------------------------

_RESERVED_WINDOWS = {WATCHER_WINDOW}


def _derive_courts(
    windows: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    seen: dict[str, Any],
) -> list[dict[str, Any]]:
    """从 dashboard session 的 window 列表派生 court 业务视角.

    Window 名格式来自 ``dashboard_tmux.issue_window_name``:
    ``<repo with '/'→'-'>-<num>``. ``-`` 不可逆 (repo 也可能含 ``-``),
    所以用 seen-issues + pending 反查找到原始 ``repo``.
    """
    seen_index: dict[tuple[str, int], dict[str, Any]] = {}
    for key, entry in seen.items():
        if not isinstance(entry, dict):
            continue
        if "#" not in key:
            continue
        repo, _, num_part = key.partition("#")
        try:
            num = int(num_part)
        except ValueError:
            continue
        seen_index[(repo, num)] = entry

    pending_index: dict[tuple[str, int], dict[str, Any]] = {}
    for item in pending:
        repo = item.get("repo")
        num = item.get("number")
        if isinstance(repo, str) and isinstance(num, int):
            pending_index[(repo, num)] = item

    courts: list[dict[str, Any]] = []
    for win in windows:
        name = win.get("name") or ""
        if name in _RESERVED_WINDOWS:
            continue
        repo, issue = _resolve_window_repo_num(name, seen_index, pending_index)
        pending_match = pending_index.get((repo, issue)) if (repo and issue is not None) else None
        seen_match = seen_index.get((repo, issue)) if (repo and issue is not None) else None
        courts.append({
            "id": name,
            "window": name,
            "window_index": win.get("index"),
            "repo": repo,
            "issue": issue,
            "active": win.get("active"),
            "panes": win.get("panes"),
            "stage": (pending_match or {}).get("stage") or (seen_match or {}).get("stage"),
            "status": _court_status(pending_match, seen_match),
        })
    return courts


def _resolve_window_repo_num(
    window_name: str,
    seen_index: dict[tuple[str, int], dict[str, Any]],
    pending_index: dict[tuple[str, int], dict[str, Any]],
) -> tuple[str | None, int | None]:
    """``<safe_repo>-<num>`` 反解: ``safe_repo`` 是 ``/`` 替换为 ``-`` 的版本.

    用 seen/pending 已知的 (repo, num) 集合做反查; 找不到时 ``repo`` 返 None.
    """
    if "-" not in window_name:
        return None, None
    head, _, tail = window_name.rpartition("-")
    try:
        num = int(tail)
    except ValueError:
        return None, None
    for repo, n in list(seen_index.keys()) + list(pending_index.keys()):
        if n == num and repo.replace("/", "-") == head:
            return repo, num
    return None, num


def _court_status(
    pending: dict[str, Any] | None, seen: dict[str, Any] | None
) -> str:
    if pending:
        return "awaiting_approval"
    if seen and seen.get("last_action") in {"DISPATCHED_DASHBOARD", "EXECUTING"}:
        return "running"
    if seen and seen.get("last_action") == "AWAITING_PLAN":
        return "awaiting_plan"
    return "running"


# ---------------------------------------------------------------------------
# 进程探测 (watcher / receiver)
# ---------------------------------------------------------------------------

async def _collect_process_info(
    module_name: str, *, port: int | None = None
) -> dict[str, Any]:
    pid = await _pgrep_first(module_name)
    info: dict[str, Any] = {"alive": pid is not None, "pid": pid}
    if port is not None:
        info["port"] = port
    return info


async def _pgrep_first(pattern: str) -> int | None:
    rc, stdout = await _run_subprocess("pgrep", "-f", pattern)
    if rc != 0:
        return None
    self_pid = str(os.getpid())
    for line in stdout.splitlines():
        pid = line.strip()
        if not pid or pid == self_pid:
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 文件 watcher (T-15-02): watchdog + asyncio debounce
# ---------------------------------------------------------------------------


class FsWatcher:
    """监听 state_dir 子树 (gitea-watcher/, pending-approval/, pending-webhook/) 变更.

    watchdog Observer 跑在自己线程, 事件回调切回 event loop 后做 debounce,
    最后调 ``on_change()``. ``on_change`` 可以是同步或 async 函数; async 会被
    ``loop.create_task`` 调度.
    """

    def __init__(
        self,
        watch_dir: Path,
        on_change: Callable[[], Any],
        debounce_ms: int = DEBOUNCE_DEFAULT_MS,
    ) -> None:
        self._watch_dir = watch_dir
        self._on_change = on_change
        self._debounce_s = max(0.0, debounce_ms / 1000.0)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Any = None
        self._handle: asyncio.TimerHandle | None = None

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self._loop = asyncio.get_running_loop()
        self._watch_dir.mkdir(parents=True, exist_ok=True)

        handler = FileSystemEventHandler()
        handler.on_any_event = self._on_fs_event  # type: ignore[method-assign]
        self._observer = Observer()
        self._observer.schedule(handler, str(self._watch_dir), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _on_fs_event(self, _event: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._reschedule)

    def _reschedule(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        loop = self._loop
        assert loop is not None
        if self._debounce_s <= 0:
            self._fire()
            return
        self._handle = loop.call_later(self._debounce_s, self._fire)

    def _fire(self) -> None:
        self._handle = None
        try:
            result = self._on_change()
        except Exception as exc:
            print(f"[dashboard-aggregator] on_change raised: {exc!r}", flush=True)
            return
        if asyncio.iscoroutine(result):
            loop = self._loop
            if loop is not None:
                loop.create_task(result)


# ---------------------------------------------------------------------------
# 模块内自测入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _smoke() -> None:
        agg = DashboardAggregator()
        snap = await agg.aggregate_status()
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))

    asyncio.run(_smoke())
