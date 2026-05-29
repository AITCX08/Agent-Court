"""PR-20d: CLI for issue-resolver to write/update ~/.agent-court/reports/<team_id>.md.

Usage:
    python -m issue_report_writer checkpoint \\
      --team-id agent-team-foo --issue K2Lab/x#1 \\
      --phase requirements --status investigating \\
      --problem "..."

存在的字段保留; 只覆盖本次传入的段. 调用约定见 .claude/skills/issue-resolver/SKILL.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VALID_PHASES = ("requirements", "plan", "execution", "verification", "done")
VALID_STATUSES = ("investigating", "planning", "executing", "verifying", "done", "blocked")
SECTIONS = ("问题描述", "调查情况", "解决方案")


def _reports_dir() -> Path:
    root = os.environ.get("COURT_ROOT")
    if root:
        return Path(root) / "reports"
    return Path.home() / ".agent-court" / "reports"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_existing(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (frontmatter_dict, sections_dict). Missing → empty defaults."""
    sections: dict[str, str] = {s: "" for s in SECTIONS}
    if not path.is_file():
        return {}, sections
    text = path.read_text(encoding="utf-8")
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            raw = text[4:end]
            body = text[end + 5:]
            for line in raw.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip()

    current: Optional[str] = None
    buf: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
                current = None
                buf = []
            name = stripped[2:].strip()
            if name in SECTIONS:
                current = name
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return fm, sections


def _write(path: Path, fm: dict[str, str], sections: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    body_parts = []
    for s in SECTIONS:
        body = sections.get(s, "").strip()
        body_parts.append(f"# {s}\n\n{body}\n")
    out = "---\n" + fm_lines + "\n---\n\n" + "\n".join(body_parts)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(path)


def _resolve_value(direct: Optional[str], file_arg: Optional[str]) -> Optional[str]:
    """``--problem "txt"`` 优先; 否则 ``--problem-file path``; ``-`` 读 stdin."""
    if direct is not None:
        return direct
    if file_arg is None:
        return None
    if file_arg == "-":
        return sys.stdin.read()
    return Path(file_arg).read_text(encoding="utf-8")


def cmd_checkpoint(args: argparse.Namespace) -> int:
    if args.phase not in VALID_PHASES:
        print(f"invalid --phase {args.phase!r}; allowed: {VALID_PHASES}", file=sys.stderr)
        return 2
    if args.status not in VALID_STATUSES:
        print(f"invalid --status {args.status!r}; allowed: {VALID_STATUSES}", file=sys.stderr)
        return 2

    path = _reports_dir() / f"{args.team_id}.md"
    fm, sections = _read_existing(path)

    fm["team_id"] = args.team_id
    fm["issue"] = args.issue
    fm["phase"] = args.phase
    fm["status"] = args.status
    fm["updated_at"] = _now_iso()

    p = _resolve_value(args.problem, args.problem_file)
    if p is not None:
        sections["问题描述"] = p.strip()
    i = _resolve_value(args.investigation, args.investigation_file)
    if i is not None:
        sections["调查情况"] = i.strip()
    s = _resolve_value(args.solution, args.solution_file)
    if s is not None:
        sections["解决方案"] = s.strip()

    _write(path, fm, sections)
    print(str(path))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="issue_report_writer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("checkpoint", help="Upsert a checkpoint into report file")
    cp.add_argument("--team-id", required=True)
    cp.add_argument("--issue", required=True, help="e.g. K2Lab/foo#1")
    cp.add_argument("--phase", required=True)
    cp.add_argument("--status", required=True)
    cp.add_argument("--problem", default=None)
    cp.add_argument("--problem-file", default=None, help="path or '-' for stdin")
    cp.add_argument("--investigation", default=None)
    cp.add_argument("--investigation-file", default=None)
    cp.add_argument("--solution", default=None)
    cp.add_argument("--solution-file", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "checkpoint":
        return cmd_checkpoint(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
