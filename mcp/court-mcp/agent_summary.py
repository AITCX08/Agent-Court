"""PR-19c-2: AI summary for agent team panes.

后端拿一段 tmux pane 内容, 调 codex/claude CLI 让它压成一句话 (e.g.
"正在 review PR-37, 跑测试"). 30s 内存 cache 避免每次 dashboard 刷都打 API.

ghostty 类型 agent 没有 tmux session, 没法 capture-pane, 后端返 None +
sentinel 'ghostty-no-capture' 让前端展示固定提示.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from tmux_pane import capture_pane, TmuxPaneError

_log = logging.getLogger("agent_summary")

SUMMARY_CACHE_TTL_SEC = 30.0
SUMMARY_PANE_LINES = 80  # 摘要不需要全 scrollback, 只看最近几屏
SUMMARY_TIMEOUT_SEC = 30  # codex/claude 摘要应当几秒内出, 超 30s 就放弃
SUMMARY_PROMPT = (
    "下面是一个 agent 终端最近的输出. 用一句话 (≤30 字, 中文) 总结它在干啥. "
    "只输出这一句话, 不要多余前缀/解释/标点装饰.\n\n"
)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    team_id: str
    summary: str  # AI 输出的一句话, 或 sentinel
    sentinel: Optional[str] = None  # 'ghostty-no-capture' / 'error' / None
    error: Optional[str] = None
    captured_at: float = 0.0  # unix ts


@dataclass(slots=True)
class _CacheEntry:
    result: SummaryResult
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _is_tmux_team(team_id: str) -> bool:
    return team_id.startswith("agent-team-")


def get_summary(
    team_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    capture: Callable[..., str] = capture_pane,
    now: Callable[[], float] = _now,
    cli_argv: tuple[str, ...] = ("codex", "exec"),
    force_refresh: bool = False,
) -> SummaryResult:
    """Get a one-line AI summary of the agent's recent pane output.

    Returns from cache if a fresh entry exists; otherwise:
    - tmux team: capture_pane → codex/claude CLI → cache
    - ghostty team: return sentinel 'ghostty-no-capture' immediately (no cache)

    All side effects (subprocess, capture) are injectable for tests.
    """
    if not _is_tmux_team(team_id):
        return SummaryResult(
            team_id=team_id,
            summary="",
            sentinel="ghostty-no-capture",
            captured_at=now(),
        )

    if not force_refresh:
        with _cache_lock:
            entry = _cache.get(team_id)
            if entry and entry.expires_at > now():
                return entry.result

    try:
        pane = capture(team_id, lines=SUMMARY_PANE_LINES)
    except TmuxPaneError as exc:
        result = SummaryResult(
            team_id=team_id,
            summary="",
            sentinel="error",
            error=f"capture-pane: {exc}",
            captured_at=now(),
        )
        # 缓存 error 也是为了防止短时间反复重试连一个挂的 session
        with _cache_lock:
            _cache[team_id] = _CacheEntry(result=result, expires_at=now() + SUMMARY_CACHE_TTL_SEC)
        return result

    prompt = SUMMARY_PROMPT + pane.strip()[-4000:]  # 防止 prompt 过大

    try:
        cp = runner(
            list(cli_argv),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=SUMMARY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        result = SummaryResult(
            team_id=team_id, summary="",
            sentinel="error", error=f"summary CLI timeout after {SUMMARY_TIMEOUT_SEC}s",
            captured_at=now(),
        )
    except FileNotFoundError as exc:
        result = SummaryResult(
            team_id=team_id, summary="",
            sentinel="error", error=f"{cli_argv[0]} not found on PATH: {exc}",
            captured_at=now(),
        )
    except Exception as exc:
        _log.exception("summary CLI unexpected error")
        result = SummaryResult(
            team_id=team_id, summary="",
            sentinel="error", error=f"{type(exc).__name__}: {exc}",
            captured_at=now(),
        )
    else:
        if cp.returncode != 0:
            result = SummaryResult(
                team_id=team_id, summary="",
                sentinel="error",
                error=f"exit {cp.returncode}: {(cp.stderr or '').strip()[:200]}",
                captured_at=now(),
            )
        else:
            summary_text = (cp.stdout or "").strip().splitlines()[0] if cp.stdout else ""
            # 兜底: CLI 没输出 → 视作 error
            if not summary_text:
                result = SummaryResult(
                    team_id=team_id, summary="",
                    sentinel="error", error="CLI returned empty stdout",
                    captured_at=now(),
                )
            else:
                result = SummaryResult(
                    team_id=team_id,
                    summary=summary_text[:120],  # hard cap 防 CLI 不听话输出超长
                    captured_at=now(),
                )

    with _cache_lock:
        _cache[team_id] = _CacheEntry(result=result, expires_at=now() + SUMMARY_CACHE_TTL_SEC)
    return result


def invalidate_cache(team_id: Optional[str] = None) -> None:
    """Clear a single team_id or all entries (used by tests / 'force refresh')."""
    with _cache_lock:
        if team_id is None:
            _cache.clear()
        else:
            _cache.pop(team_id, None)
