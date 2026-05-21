"""agent-court — ``court-reconcile`` CLI (SY-3 #18 MVP v1 旁挂).

跑一次 Orchestrator.snapshot() + reconcile, 把不一致清单打到 stdout.

Exit code:
    0 — clean (没 inconsistency)
    1 — 只有 warn 级别的 inconsistency
    2 — 有 error 级别的 inconsistency (例如 dispatched_window_gone)

Usage:
    court-reconcile                # 人类可读
    court-reconcile --json         # 整个 snapshot.to_dict() 一坨 json
    court-reconcile --quiet        # 只打 inconsistencies, clean 时啥也不打

``bin/court-reconcile`` 是个 bash 薄包装 (走 venv python), 复用
``bin/court-approve`` 同款套路.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from orchestrator import (  # noqa: E402
    Orchestrator,
    SEVERITY_ERROR,
    SEVERITY_WARN,
)


def _resolve_court_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser()
    env = os.environ.get("COURT_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agent-court"


def _format_human(snap, *, quiet: bool) -> str:
    lines: list[str] = []
    inc = snap.inconsistencies
    metrics = snap.metrics
    if not quiet:
        lines.append(
            f"runs={metrics.get('total', 0)}  "
            f"active={metrics.get('active', 0)}  "
            f"pending={metrics.get('pending_approval_count', 0)}  "
            f"retry={metrics.get('in_retry_queue', 0)}  "
            f"orphan_windows={metrics.get('orphan_tmux_windows', 0)}"
        )
        lines.append(
            f"inconsistencies: total={len(inc)}  "
            f"error={metrics.get('inconsistencies_error', 0)}  "
            f"warn={metrics.get('inconsistencies_warn', 0)}"
        )
        lines.append("")
    if not inc:
        if not quiet:
            lines.append("clean — no inconsistencies")
        return "\n".join(lines)
    for i in inc:
        tag = "ERROR" if i.severity == SEVERITY_ERROR else "WARN"
        key = i.issue_key or "-"
        lines.append(f"[{tag}] {i.kind}  {key}")
        lines.append(f"    detail: {i.detail}")
        lines.append(f"    fix:    {i.suggested_fix}")
    return "\n".join(lines)


def _exit_code(snap) -> int:
    metrics = snap.metrics
    if metrics.get("inconsistencies_error", 0) > 0:
        return 2
    if metrics.get("inconsistencies_warn", 0) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="court-reconcile")
    parser.add_argument("--court-root", default=None, help="覆盖 COURT_ROOT (默认 ~/.agent-court)")
    parser.add_argument("--json", action="store_true", help="输出整个 snapshot.to_dict() 的 json")
    parser.add_argument("--quiet", action="store_true", help="clean 时不打 metrics, 只在有 inconsistency 时打")
    args = parser.parse_args(argv)

    orch = Orchestrator(court_root=_resolve_court_root(args.court_root))
    snap = orch.snapshot()
    if args.json:
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
    else:
        text = _format_human(snap, quiet=args.quiet)
        if text:
            print(text)
    return _exit_code(snap)


if __name__ == "__main__":
    raise SystemExit(main())
