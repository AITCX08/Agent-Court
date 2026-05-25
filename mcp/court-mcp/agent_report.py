"""PR-20a: Agent report module.

读 ``~/.agent-court/reports/<team_id>.md`` (issue-resolver 在 4 个 checkpoint
主动写). 文件不存在时, 用 Sonnet 4.6 (``claude -p --bare``) 从 tmux pane /
claude jsonl 实时提炼三段汇报 (problem / investigation / solution).

布局参考 ``agent_summary.py``: 30s 内存 cache, threading-safe, 可注入
``runner`` / ``gather_context`` 方便测试.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tmux_pane import TmuxPaneError, capture_pane

_log = logging.getLogger("agent_report")

REPORT_CACHE_TTL_SEC = 30.0
REPORT_FALLBACK_TIMEOUT_SEC = 60
REPORT_FALLBACK_MODEL = "claude-sonnet-4-6"
CONTEXT_PANE_LINES = 200
FALLBACK_CONTEXT_BUDGET_CHARS = 8000


@dataclass(frozen=True, slots=True)
class ReportResult:
    team_id: str
    problem: str
    investigation: str
    solution: str
    status: str            # investigating | planning | executing | verifying | done | blocked | unknown
    phase: str             # requirements | plan | execution | verification | done | unknown
    updated_at: str        # ISO8601 or ""
    source: str            # "file" | "fallback" | "missing"
    captured_at: float
    error: Optional[str] = None


@dataclass(slots=True)
class _CacheEntry:
    result: ReportResult
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    return time.time()


def invalidate_report_cache(team_id: Optional[str] = None) -> None:
    with _cache_lock:
        if team_id is None:
            _cache.clear()
        else:
            _cache.pop(team_id, None)


def _resolve_reports_dir() -> Path:
    root = os.environ.get("COURT_ROOT")
    if root:
        return Path(root) / "reports"
    return Path.home() / ".agent-court" / "reports"


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_SECTIONS = ("问题描述", "调查情况", "解决方案")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    fm: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, body


def _split_sections(body: str) -> dict[str, str]:
    """按 ``# 问题描述`` / ``# 调查情况`` / ``# 解决方案`` 切分.

    遇到其他 ``# ...`` 标题 (不在白名单内) 会**结束**当前 section, 但**不开**
    新 section, 中间内容直接丢弃, 直到再次命中白名单标题为止.
    """
    out = {s: "" for s in _SECTIONS}
    current: Optional[str] = None
    buf: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        is_h1 = stripped.startswith("# ")
        if is_h1:
            # 任何 # 标题都先关掉当前 section
            if current is not None:
                out[current] = "\n".join(buf).strip()
                current = None
                buf = []
            # 命中白名单才开新 section
            name = stripped[2:].strip()
            if name in _SECTIONS:
                current = name
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def _read_report_file(path: Path) -> Optional[ReportResult]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("read report %s failed: %s", path, exc)
        return None
    fm, body = _parse_frontmatter(text)
    sections = _split_sections(body)
    return ReportResult(
        team_id=fm.get("team_id", path.stem),
        problem=sections["问题描述"],
        investigation=sections["调查情况"],
        solution=sections["解决方案"],
        status=fm.get("status", "unknown"),
        phase=fm.get("phase", "unknown"),
        updated_at=fm.get("updated_at", ""),
        source="file",
        captured_at=_now(),
    )


FALLBACK_PROMPT = (
    "下面是一个正在处理 Gitea issue 的 agent 的工作上下文 "
    "(tmux pane 内容 / claude 对话 jsonl). "
    "请生成一份给上级看的汇报, 严格按以下 3 段 markdown 格式输出, "
    "段标题必须一模一样. 不要其他多余内容:\n\n"
    "# 问题描述\n<3-5 行说明这个 issue 是什么>\n\n"
    "# 调查情况\n<3-5 行说明 agent 已经发现/确认了什么>\n\n"
    "# 解决方案\n<3-5 行说明 agent 计划怎么解 + 已经做了什么>\n\n"
    "--- 上下文 ---\n"
)


def _build_fallback_argv() -> tuple[str, ...]:
    return (
        "claude", "-p", "--bare",
        "--model", REPORT_FALLBACK_MODEL,
        "--allow-dangerously-skip-permissions",
    )


def _run_claude_fallback(
    team_id: str,
    context_text: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> ReportResult:
    prompt = FALLBACK_PROMPT + context_text[-FALLBACK_CONTEXT_BUDGET_CHARS:]
    try:
        cp = runner(
            list(_build_fallback_argv()),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=REPORT_FALLBACK_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return ReportResult(
            team_id=team_id, problem="", investigation="", solution="",
            status="unknown", phase="unknown", updated_at="",
            source="fallback", captured_at=_now(),
            error=f"claude fallback timeout after {REPORT_FALLBACK_TIMEOUT_SEC}s",
        )
    except FileNotFoundError as exc:
        return ReportResult(
            team_id=team_id, problem="", investigation="", solution="",
            status="unknown", phase="unknown", updated_at="",
            source="fallback", captured_at=_now(),
            error=f"claude not on PATH: {exc}",
        )
    if cp.returncode != 0:
        return ReportResult(
            team_id=team_id, problem="", investigation="", solution="",
            status="unknown", phase="unknown", updated_at="",
            source="fallback", captured_at=_now(),
            error=f"claude exit {cp.returncode}: {(cp.stderr or '').strip()[:200]}",
        )
    sections = _split_sections(cp.stdout or "")
    return ReportResult(
        team_id=team_id,
        problem=sections["问题描述"],
        investigation=sections["调查情况"],
        solution=sections["解决方案"],
        status="unknown", phase="unknown", updated_at="",
        source="fallback", captured_at=_now(),
    )


def default_gather_context(team_id: str) -> str:
    """tmux team 默认: capture pane 最近 200 行. ghostty team 留给 caller 自己实现."""
    if not team_id.startswith("agent-team-"):
        return ""
    try:
        return capture_pane(team_id, lines=CONTEXT_PANE_LINES)
    except TmuxPaneError:
        return ""


def get_report(
    team_id: str,
    *,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    gather_context: Optional[Callable[[str], str]] = None,
    force_refresh: bool = False,
) -> ReportResult:
    """读 report 文件; 不存在则用 sonnet fallback 从 pane/jsonl 实时提炼.

    ``gather_context=None`` 时**不走 fallback**, 直接返 source=missing.
    ``runner=None`` 时在调用点 late-resolve 到 ``subprocess.run``, 方便测试 monkeypatch.
    """
    if runner is None:
        runner = subprocess.run  # late-bind so monkeypatch(agent_report.subprocess, "run", ...) 能生效
    if not force_refresh:
        with _cache_lock:
            entry = _cache.get(team_id)
            if entry and entry.expires_at > _now():
                return entry.result

    path = _resolve_reports_dir() / f"{team_id}.md"
    if path.is_file():
        result = _read_report_file(path)
        if result is not None:
            _store_cache(team_id, result)
            return result

    if gather_context is None:
        result = ReportResult(
            team_id=team_id, problem="", investigation="", solution="",
            status="unknown", phase="unknown", updated_at="",
            source="missing", captured_at=_now(),
        )
        _store_cache(team_id, result)
        return result

    context = gather_context(team_id) or ""
    if not context.strip():
        result = ReportResult(
            team_id=team_id, problem="", investigation="", solution="",
            status="unknown", phase="unknown", updated_at="",
            source="missing", captured_at=_now(),
        )
        _store_cache(team_id, result)
        return result

    result = _run_claude_fallback(team_id, context, runner)
    _store_cache(team_id, result)
    return result


def _store_cache(team_id: str, result: ReportResult) -> None:
    with _cache_lock:
        _cache[team_id] = _CacheEntry(result=result, expires_at=_now() + REPORT_CACHE_TTL_SEC)
