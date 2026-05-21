"""PR-17a Agents 团队聚合层.

把"在跑着的 agent 团队"两类来源聚合成 dashboard 前端用的统一列表:

1. **ghostty 终端 tab** — 用户手动开的 claude / codex CLI. 通过
   ``ps -eo tty,pid,etime,comm,lstart`` 扫整机进程, 留 ``tty != "??"`` 且
   ``comm ~ ^(claude|Claude|codex)`` 的.
2. **tmux session** — dashboard 模式 / spawn endpoint 起的, session 名前缀
   ``agent-team-``. 通过 ``tmux list-sessions``.

每个 team 带可编辑的业务 label, 持久化在 ``~/.agent-court/team-labels.json``
(chmod 600). 主键 = ``{kind}:{id}``; TTY 重分配时用 ``{cli}::{started_at[:16]}``
fallback 兜底匹配, 找不到就空 label.

数据结构跟 plan §4.3 ``GET /api/agent-teams`` 响应一致.

PR-17a 范围: 只读 + label 编辑. spawn / DELETE / SSE stream 留 PR-17b/c.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any


TMUX_TEAM_PREFIX = "agent-team-"
LABEL_FILE = "team-labels.json"
LABEL_FILE_MODE = 0o600
# CLI 进程 comm 匹配
CLI_NAME_RE = re.compile(r"^(Claude|claude|codex|Codex)$")
# MCP 子进程关键字 (出现在 command 行里就当 MCP)
MCP_KEYWORDS = ("mcp", "context7", "souls", "modelcontext")


@dataclass(frozen=True)
class AgentTeam:
    id: str                      # ghostty:ttys025 / tmux:agent-team-xxxx
    kind: str                    # ghostty | tmux
    label: str                   # 业务标签 (用户可改); 找不到时 ""
    cli: str                     # Claude / claude / codex
    pid: int | None              # ghostty: 进程 pid; tmux: 首 pane pid
    started_at: str              # ISO 8601 UTC-naive
    cwd: str = ""                # 暂留 (PR-17b 实装, 需要 lsof 拿)
    tty: str = ""                # ghostty kind 用
    session: str = ""            # tmux kind 用
    windows: int = 0
    panes: list[dict[str, Any]] = field(default_factory=list)
    mcp_subprocs: list[dict[str, Any]] = field(default_factory=list)
    linked: dict[str, Any] | None = None  # {repo, number, kind, url}
    can_stream: bool = False
    can_stop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentTeamAggregator:
    """每次 snapshot 重扫全机 ps + tmux. 不缓存 (PR-17b 加 1s TTL)."""

    def __init__(self, court_root: Path | None = None,
                 team_links: "TeamLinks | None" = None) -> None:
        self.court_root = court_root or (Path.home() / ".agent-court")
        if team_links is None:
            from team_links import TeamLinks
            team_links = TeamLinks(court_root=self.court_root)
        self.team_links = team_links

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        labels = self._load_labels()
        ghostty_teams = self._collect_ghostty(labels)
        tmux_teams = self._collect_tmux(labels)
        # PR-17b: 给 tmux team 附 linked (从 team_links 反查).
        # team_links 用原始 session 名作 key (agent_spawn.py 落的格式), 跟
        # AgentTeam.id 的 "tmux:" 前缀不一致, 所以这里用 team.session lookup.
        # ghostty team 暂无对应 spawn 链路, 不查 team_links.
        for i, team in enumerate(tmux_teams):
            linked = self.team_links.lookup_by_team(team.session)
            if linked:
                tmux_teams[i] = replace(team, linked=linked)
        # ghostty 先排 (按 started_at desc), 再 tmux
        ghostty_teams.sort(key=lambda t: t.started_at, reverse=True)
        tmux_teams.sort(key=lambda t: t.started_at, reverse=True)
        all_teams = ghostty_teams + tmux_teams
        return {
            "updated_at": _utc_now_iso(),
            "teams": [t.to_dict() for t in all_teams],
        }

    # ------------------------------------------------------------------
    # ghostty
    # ------------------------------------------------------------------

    def _collect_ghostty(self, labels: dict[str, Any]) -> list[AgentTeam]:
        rows = _ps_eo_tty_pid_etime_comm_lstart()
        teams: list[AgentTeam] = []
        for row in rows:
            tty, pid, _etime, comm, lstart = row
            if tty == "??":
                continue
            if not CLI_NAME_RE.match(comm):
                continue
            started_iso = _lstart_to_iso(lstart)
            team_id = f"ghostty:{tty}"
            label = _resolve_label(labels, team_id, comm, started_iso)
            mcp = _ps_subprocs_for_tty(tty, exclude_pid=pid)
            teams.append(AgentTeam(
                id=team_id,
                kind="ghostty",
                label=label,
                cli=comm,
                pid=pid,
                started_at=started_iso,
                tty=tty,
                mcp_subprocs=mcp,
                can_stream=False,   # PR-17c 仍 False (ghostty 无 stream)
                can_stop=False,     # PR-17b 仍 False
            ))
        return teams

    # ------------------------------------------------------------------
    # tmux
    # ------------------------------------------------------------------

    def _collect_tmux(self, labels: dict[str, Any]) -> list[AgentTeam]:
        sessions = _tmux_list_sessions()
        teams: list[AgentTeam] = []
        for session_name, windows_count, session_created in sessions:
            if not session_name.startswith(TMUX_TEAM_PREFIX):
                continue
            panes = _tmux_list_panes(session_name)
            first_pane = panes[0] if panes else None
            first_pid = first_pane.get("pid") if first_pane else None
            cli = first_pane.get("command", "") if first_pane else ""
            started_iso = _epoch_to_iso(session_created) if session_created else ""
            team_id = f"tmux:{session_name}"
            label = _resolve_label(labels, team_id, cli, started_iso)
            teams.append(AgentTeam(
                id=team_id,
                kind="tmux",
                label=label,
                cli=cli,
                pid=first_pid,
                started_at=started_iso,
                session=session_name,
                windows=windows_count,
                panes=panes,
                mcp_subprocs=[],     # tmux pane 下 MCP 暂不扫 (PR-17b)
                can_stream=True,     # PR-17c 接 SSE 后真起来
                can_stop=True,       # PR-17b 接 DELETE 后真起来
            ))
        return teams

    # ------------------------------------------------------------------
    # label 持久化 (PR-17a 公共 API)
    # ------------------------------------------------------------------

    def set_label(self, team_id: str, label: str, *, cli: str = "",
                  started_at: str = "") -> dict[str, Any]:
        """写主键 + 兼容写 fallback key (cli::started_at[:16]).

        label 为空字符串 = 删除. 文件用 chmod 600.
        """
        labels = self._load_labels()
        labels_main = labels.setdefault("labels", {})
        labels_fb = labels.setdefault("fallback", {})
        if not label:
            labels_main.pop(team_id, None)
        else:
            labels_main[team_id] = label
        fb_key = _fallback_key(cli, started_at)
        if fb_key:
            if label:
                labels_fb[fb_key] = label
            else:
                labels_fb.pop(fb_key, None)
        self._write_labels(labels)
        return {"labels": dict(labels_main), "fallback": dict(labels_fb)}

    def _load_labels(self) -> dict[str, Any]:
        path = self.court_root / LABEL_FILE
        if not path.is_file():
            return {"labels": {}, "fallback": {}}
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"labels": {}, "fallback": {}}
        if not isinstance(data, dict):
            return {"labels": {}, "fallback": {}}
        data.setdefault("labels", {})
        data.setdefault("fallback", {})
        return data

    def _write_labels(self, data: dict[str, Any]) -> None:
        self.court_root.mkdir(parents=True, exist_ok=True)
        path = self.court_root / LABEL_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        try:
            os.chmod(tmp, LABEL_FILE_MODE)
        except OSError:
            pass
        tmp.replace(path)


# ---------------------------------------------------------------------------
# subprocess wrappers (mocked in tests)
# ---------------------------------------------------------------------------

def _ps_eo_tty_pid_etime_comm_lstart() -> list[tuple[str, int, str, str, str]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "tty,pid,etime,comm,lstart"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    rows: list[tuple[str, int, str, str, str]] = []
    for i, line in enumerate(proc.stdout.splitlines()):
        if i == 0:
            continue  # header
        parts = line.split()
        if len(parts) < 5:
            continue
        tty = parts[0]
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        etime = parts[2]
        comm = parts[3]
        # lstart 是 "Thu May 21 10:19:37 2026" 5 个 token
        lstart = " ".join(parts[4:])
        rows.append((tty, pid, etime, comm, lstart))
    return rows


def _ps_subprocs_for_tty(tty: str, *, exclude_pid: int | None = None) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-t", tty, "-o", "pid,command"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(proc.stdout.splitlines()):
        if i == 0:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue
        command = parts[1]
        lower = command.lower()
        if not any(kw in lower for kw in MCP_KEYWORDS):
            continue
        # 标准化: 截短到 100 字符避免 UI 撑爆
        out.append({"pid": pid, "command": command[:100], "name": _mcp_short_name(command)})
    return out


def _tmux_list_sessions() -> list[tuple[str, int, int]]:
    """返 [(session_name, windows_count, session_created_epoch), ...]."""
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}|#{session_windows}|#{session_created}"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, int, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            out.append((parts[0], int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return out


def _tmux_list_panes(session_name: str) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-t", session_name,
             "-F", "#{pane_index}|#{pane_pid}|#{pane_current_command}|#{pane_start_time}"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    panes: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
            pid = int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        started = ""
        if len(parts) >= 4 and parts[3].strip():
            try:
                started = _epoch_to_iso(int(parts[3]))
            except ValueError:
                pass
        panes.append({"index": idx, "pid": pid, "command": command, "started_at": started})
    return panes


# ---------------------------------------------------------------------------
# helpers (纯函数, 易测)
# ---------------------------------------------------------------------------

_LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def _lstart_to_iso(lstart: str) -> str:
    """``Thu May 21 10:19:37 2026`` → ``2026-05-21T10:19:37``. 失败返空."""
    try:
        dt = datetime.strptime(lstart.strip(), _LSTART_FMT)
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _epoch_to_iso(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError, OSError):
        return ""


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _fallback_key(cli: str, started_at: str) -> str:
    """``{cli}::{started_at[:16]}`` — 精度到分钟, TTY 重分配后还能恢复 label."""
    if not cli or not started_at:
        return ""
    return f"{cli}::{started_at[:16]}"


def _resolve_label(labels_root: dict[str, Any], team_id: str, cli: str, started_at: str) -> str:
    main_labels = labels_root.get("labels", {})
    if isinstance(main_labels, dict):
        v = main_labels.get(team_id)
        if isinstance(v, str) and v:
            return v
    fallback = labels_root.get("fallback", {})
    if isinstance(fallback, dict):
        key = _fallback_key(cli, started_at)
        if key:
            v = fallback.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _mcp_short_name(command: str) -> str:
    """从 npm exec / pipx 命令里抽 MCP 名: ``context7`` / ``souls-mcp`` 等."""
    lower = command.lower()
    for kw in MCP_KEYWORDS:
        if kw in lower:
            idx = lower.index(kw)
            # 取关键字所在的 token 前后到边界
            tail = command[idx:].split()[0]
            return tail.rstrip("@/")
    return command.split()[0] if command.split() else ""
