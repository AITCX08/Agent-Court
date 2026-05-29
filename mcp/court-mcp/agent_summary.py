"""PR-19c-2 / PR-19d / PR-20e: AI summary for agent team panes.

两条来源:

- **tmux** (dashboard-spawned 或用户手动 tmux new-session): tmux capture-pane
  拿最近 stdout, 喂给 claude (sonnet 4.6) 摘要.

- **ghostty** (用户在 ghostty terminal 里手动跑 claude): 后端没法直接 capture
  ghostty buffer, 但 claude 把 session 写在 ``~/.claude/projects/<cwd>/<uuid>.jsonl``.
  通过 PID → cwd (ps) → projects 目录 → 找 mtime 最新的 jsonl → 读最后 N 条
  message, 喂给 claude sonnet 摘要. 多 claude 跑同 cwd 时不能精准对应, 用 mtime
  最新当近似.

降级链 (ghostty 任意步骤失败):
1. lsof / ps 拿不到 cwd → SummaryResult.sentinel='ghostty-no-cwd'
2. cwd 转义目录里没 jsonl → SummaryResult.sentinel='ghostty-no-session'
3. jsonl 解析全失败 / 内容空 → SummaryResult.sentinel='ghostty-no-content'
4. claude CLI 失败 → SummaryResult.sentinel='error' + error 字段

30s 内存 cache 避免每次 dashboard 刷都打 API.

PR-20e: 默认 cli_argv 从 ("codex", "exec") → ("claude", "-p", "--model",
"claude-sonnet-4-6", "--allow-dangerously-skip-permissions"). 把所有"整理概括"
场景统一到 sonnet 4.6.

注意: 不加 ``--bare`` flag. ``--bare`` 会跳过 keychain/OAuth 强制要
ANTHROPIC_API_KEY env, 但用户本地是通过 claude.ai OAuth 登录, 没设 env.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tmux_pane import capture_pane, TmuxPaneError

_log = logging.getLogger("agent_summary")

SUMMARY_CACHE_TTL_SEC = 30.0
SUMMARY_PANE_LINES = 80
SUMMARY_TIMEOUT_SEC = 30
SUMMARY_PROMPT = (
    "下面是一个 agent 终端最近的输出. 用一句话 (≤30 字, 中文) 总结它在干啥. "
    "只输出这一句话, 不要多余前缀/解释/标点装饰.\n\n"
)
# PR-19d: ghostty branch — 读 claude jsonl 的最后 N 条 message 拼成摘要输入
GHOSTTY_TAIL_MESSAGES = 12
GHOSTTY_CONTENT_BUDGET_CHARS = 8000  # 防止 prompt 过大
GHOSTTY_SESSION_FRESH_MIN = 60  # 只看 60 分钟内有过修改的 jsonl


@dataclass(frozen=True, slots=True)
class SummaryResult:
    team_id: str
    summary: str
    sentinel: Optional[str] = None
    error: Optional[str] = None
    captured_at: float = 0.0


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


# ---------- PR-19d: ghostty branch helpers ----------

def _cwd_to_projects_dir(cwd: str) -> str:
    """``/Users/wjx/Desktop`` → ``-Users-wjx-Desktop`` (claude 转义规则)."""
    if not cwd.startswith("/"):
        return ""
    return cwd.replace("/", "-")


def _find_latest_jsonl_for_cwd(cwd: str, *, claude_root: Path | None = None) -> Optional[Path]:
    """在 ``~/.claude/projects/<escaped-cwd>/`` 下找 mtime 最新的 .jsonl 文件."""
    root = claude_root or (Path.home() / ".claude" / "projects")
    escaped = _cwd_to_projects_dir(cwd)
    if not escaped:
        return None
    proj_dir = root / escaped
    if not proj_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    cutoff = time.time() - GHOSTTY_SESSION_FRESH_MIN * 60
    for p in proj_dir.iterdir():
        if p.suffix != ".jsonl":
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _read_jsonl_tail_messages(path: Path, *, count: int = GHOSTTY_TAIL_MESSAGES) -> list[dict]:
    """读 jsonl 最后 ``count`` 条 message records (任意类型, 包括 user/assistant/tool_use)."""
    try:
        with path.open("rb") as f:
            # 简单 tail: 读末尾 64KB 应当覆盖最近几条 (单条 message 一般 ≤ 5KB)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_from = max(0, size - 64 * 1024)
            f.seek(read_from)
            blob = f.read()
    except OSError as exc:
        _log.debug("read jsonl %s failed: %s", path, exc)
        return []
    text = blob.decode("utf-8", errors="replace")
    # 末尾不完整行扔掉
    lines = text.splitlines()
    if read_from > 0 and lines:
        lines = lines[1:]  # 第一行可能截半
    out: list[dict] = []
    for line in lines[-count * 3:]:  # 多读一些, 后续过滤
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                out.append(d)
        except json.JSONDecodeError:
            continue
    return out[-count:]


def _format_jsonl_messages_for_prompt(messages: list[dict]) -> str:
    """把 jsonl messages 拼成 codex 可读的 pane-like text."""
    parts: list[str] = []
    for m in messages:
        msg = m.get("message") if isinstance(m.get("message"), dict) else m
        role = msg.get("role") or m.get("type") or "?"
        content = msg.get("content", "")
        if isinstance(content, list):
            text_chunks: list[str] = []
            for c in content:
                if isinstance(c, dict):
                    t = c.get("type")
                    if t == "text":
                        text_chunks.append(str(c.get("text", "")))
                    elif t == "tool_use":
                        name = c.get("name", "?")
                        text_chunks.append(f"[tool: {name}]")
                    elif t == "tool_result":
                        text_chunks.append("[tool result omitted]")
                else:
                    text_chunks.append(str(c))
            content_str = " ".join(text_chunks)
        else:
            content_str = str(content)
        if content_str.strip():
            parts.append(f"[{role}] {content_str.strip()}")
    joined = "\n".join(parts)
    if len(joined) > GHOSTTY_CONTENT_BUDGET_CHARS:
        joined = joined[-GHOSTTY_CONTENT_BUDGET_CHARS:]
    return joined


def _ps_cwd_for_pid(pid: int, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Optional[str]:
    """用 ``lsof -p $PID -F n`` 拿进程 cwd. macOS 通用方法."""
    try:
        cp = runner(
            ["lsof", "-p", str(pid), "-F", "n"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    # lsof -F n 输出格式:  fXX (fd) / aXX / lXX ... / nXXX (filename)
    # cwd 项: 一行 "f cwd" 紧跟一行 "n<path>"
    lines = (cp.stdout or "").splitlines()
    for i, line in enumerate(lines):
        if line == "fcwd" and i + 1 < len(lines) and lines[i + 1].startswith("n"):
            return lines[i + 1][1:]
    return None


def _gather_ghostty_pane_text(
    pid: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    claude_root: Path | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """对 ghostty pid 返 (pane_text, sentinel).

    成功: (text, None)
    失败链: cwd 缺 → ('', 'ghostty-no-cwd'); jsonl 缺 → ('', 'ghostty-no-session');
    内容空 → ('', 'ghostty-no-content').
    """
    cwd = _ps_cwd_for_pid(pid, runner=runner)
    if not cwd:
        return ("", "ghostty-no-cwd")
    jsonl = _find_latest_jsonl_for_cwd(cwd, claude_root=claude_root)
    if jsonl is None:
        return ("", "ghostty-no-session")
    messages = _read_jsonl_tail_messages(jsonl)
    text = _format_jsonl_messages_for_prompt(messages)
    if not text.strip():
        return ("", "ghostty-no-content")
    return (text, None)


# ---------- core entry ----------

def get_summary(
    team_id: str,
    *,
    pid: Optional[int] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    capture: Callable[..., str] = capture_pane,
    now: Callable[[], float] = _now,
    cli_argv: tuple[str, ...] = (
        "claude", "-p",
        "--model", "claude-sonnet-4-6",
        "--allow-dangerously-skip-permissions",
    ),
    force_refresh: bool = False,
    claude_root: Path | None = None,
) -> SummaryResult:
    """Return one-line AI summary; 30s cache; tmux + ghostty 双路径."""
    if not force_refresh:
        with _cache_lock:
            entry = _cache.get(team_id)
            if entry and entry.expires_at > now():
                return entry.result

    # --- 拿 pane text + 早期 sentinel ---
    if _is_tmux_team(team_id):
        try:
            pane = capture(team_id, lines=SUMMARY_PANE_LINES)
        except TmuxPaneError as exc:
            result = SummaryResult(
                team_id=team_id, summary="", sentinel="error",
                error=f"capture-pane: {exc}", captured_at=now(),
            )
            _store_cache(team_id, result, now)
            return result
        pane_text = pane.strip()[-4000:]
    else:
        # ghostty 类型: 必须给 pid 才能查 cwd
        if pid is None or pid <= 0:
            result = SummaryResult(
                team_id=team_id, summary="", sentinel="ghostty-no-pid",
                error="ghostty team_id but no pid provided", captured_at=now(),
            )
            _store_cache(team_id, result, now)
            return result
        pane_text, sentinel = _gather_ghostty_pane_text(
            pid, runner=runner, claude_root=claude_root,
        )
        if sentinel:
            result = SummaryResult(
                team_id=team_id, summary="", sentinel=sentinel, captured_at=now(),
            )
            _store_cache(team_id, result, now)
            return result

    # --- 喂给 codex/claude CLI ---
    prompt = SUMMARY_PROMPT + pane_text

    try:
        cp = runner(
            list(cli_argv), input=prompt, capture_output=True,
            text=True, timeout=SUMMARY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        result = SummaryResult(
            team_id=team_id, summary="", sentinel="error",
            error=f"summary CLI timeout after {SUMMARY_TIMEOUT_SEC}s",
            captured_at=now(),
        )
    except FileNotFoundError as exc:
        result = SummaryResult(
            team_id=team_id, summary="", sentinel="error",
            error=f"{cli_argv[0]} not found on PATH: {exc}",
            captured_at=now(),
        )
    except Exception as exc:
        _log.exception("summary CLI unexpected error")
        result = SummaryResult(
            team_id=team_id, summary="", sentinel="error",
            error=f"{type(exc).__name__}: {exc}",
            captured_at=now(),
        )
    else:
        if cp.returncode != 0:
            result = SummaryResult(
                team_id=team_id, summary="", sentinel="error",
                error=f"exit {cp.returncode}: {(cp.stderr or '').strip()[:200]}",
                captured_at=now(),
            )
        else:
            stdout = (cp.stdout or "").strip()
            summary_text = stdout.splitlines()[0] if stdout else ""
            if not summary_text:
                result = SummaryResult(
                    team_id=team_id, summary="", sentinel="error",
                    error="CLI returned empty stdout", captured_at=now(),
                )
            else:
                result = SummaryResult(
                    team_id=team_id, summary=summary_text[:120], captured_at=now(),
                )

    _store_cache(team_id, result, now)
    return result


def _store_cache(team_id: str, result: SummaryResult, now: Callable[[], float]) -> None:
    with _cache_lock:
        _cache[team_id] = _CacheEntry(result=result, expires_at=now() + SUMMARY_CACHE_TTL_SEC)


def invalidate_cache(team_id: Optional[str] = None) -> None:
    with _cache_lock:
        if team_id is None:
            _cache.clear()
        else:
            _cache.pop(team_id, None)
