"""PR-17a: AgentTeamAggregator 单元测试.

monkeypatch 替 ps / tmux subprocess wrapper, 不打真 OS. 验证:
- ghostty 过滤规则 (tty 必须不是 ??, comm 必须是 Claude/claude/codex)
- tmux session 必须以 agent-team- 前缀
- label 持久化 + fallback 匹配 (TTY 重分配恢复)
- set_label 空 label = 删除
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_teams as at  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _agg(tmp_path: Path) -> at.AgentTeamAggregator:
    return at.AgentTeamAggregator(court_root=tmp_path)


def _patch_ps(monkeypatch, ps_rows: list[tuple[str, int, str, str, str]],
              subprocs: dict[str, list[dict]] | None = None) -> None:
    monkeypatch.setattr(at, "_ps_eo_tty_pid_etime_comm_lstart", lambda: list(ps_rows))
    sub_map = subprocs or {}
    monkeypatch.setattr(at, "_ps_subprocs_for_tty",
                        lambda tty, exclude_pid=None: list(sub_map.get(tty, [])))


def _patch_tmux(monkeypatch, sessions: list[tuple[str, int, int]],
                panes: dict[str, list[dict]] | None = None) -> None:
    monkeypatch.setattr(at, "_tmux_list_sessions", lambda: list(sessions))
    panes_map = panes or {}
    monkeypatch.setattr(at, "_tmux_list_panes",
                        lambda session_name: list(panes_map.get(session_name, [])))


# ---------------------------------------------------------------------------
# ghostty 过滤
# ---------------------------------------------------------------------------

def test_skips_ttymark_processes(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [
        ("??", 100, "10:00", "claude", "Mon May 11 15:56:26 2026"),  # 应过滤
        ("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026"),
    ])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    assert [t["id"] for t in snap["teams"]] == ["ghostty:ttys025"]


def test_skips_non_cli_commands(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [
        ("ttys001", 10, "00:30", "bash", "Thu May 21 10:00:00 2026"),  # 非 cli, 过滤
        ("ttys002", 20, "00:30", "codex", "Thu May 21 10:01:00 2026"),
    ])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    assert [t["id"] for t in snap["teams"]] == ["ghostty:ttys002"]


def test_ghostty_includes_started_at_iso(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [
        ("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026"),
    ])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    team = snap["teams"][0]
    assert team["started_at"] == "2026-05-21T10:19:37"
    assert team["pid"] == 200
    assert team["cli"] == "Claude"
    assert team["tty"] == "ttys025"


def test_ghostty_includes_mcp_subprocs(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [
        ("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026"),
    ], subprocs={
        "ttys025": [
            {"pid": 301, "command": "npm exec @upstash/context7-mcp@latest", "name": "context7-mcp"},
            {"pid": 302, "command": "uvx souls-mcp", "name": "souls-mcp"},
        ],
    })
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    team = snap["teams"][0]
    assert len(team["mcp_subprocs"]) == 2
    assert team["mcp_subprocs"][0]["pid"] == 301


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------

def test_only_tmux_sessions_with_team_prefix_included(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [])
    _patch_tmux(monkeypatch, [
        ("agent-team-abc123", 1, 1779356980),  # 应包含
        ("agent-court-dashboard", 4, 1779356900),  # PR-15 dashboard, 应过滤
        ("agent-team-def456", 1, 1779357000),  # 应包含
    ])
    snap = _agg(tmp_path).snapshot()
    ids = {t["id"] for t in snap["teams"]}
    assert ids == {"tmux:agent-team-abc123", "tmux:agent-team-def456"}


def test_tmux_team_can_stream_and_can_stop_true(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [])
    _patch_tmux(monkeypatch, [("agent-team-x", 1, 1779356980)],
                panes={"agent-team-x": [{"index": 0, "pid": 1234, "command": "claude", "started_at": ""}]})
    snap = _agg(tmp_path).snapshot()
    team = snap["teams"][0]
    assert team["can_stream"] is True
    assert team["can_stop"] is True
    assert team["windows"] == 1
    assert team["session"] == "agent-team-x"


def test_ghostty_team_cannot_stream_or_stop(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    team = snap["teams"][0]
    assert team["can_stream"] is False
    assert team["can_stop"] is False


# ---------------------------------------------------------------------------
# label 持久化
# ---------------------------------------------------------------------------

def test_set_label_writes_main_and_fallback(tmp_path):
    agg = _agg(tmp_path)
    agg.set_label("ghostty:ttys025", "moras-finder",
                  cli="Claude", started_at="2026-05-21T10:19:37")
    raw = json.loads((tmp_path / "team-labels.json").read_text())
    assert raw["labels"]["ghostty:ttys025"] == "moras-finder"
    assert raw["fallback"]["Claude::2026-05-21T10:19"] == "moras-finder"


def test_set_label_empty_string_deletes(tmp_path):
    agg = _agg(tmp_path)
    agg.set_label("ghostty:ttys025", "moras-finder",
                  cli="Claude", started_at="2026-05-21T10:19:37")
    agg.set_label("ghostty:ttys025", "", cli="Claude", started_at="2026-05-21T10:19:37")
    raw = json.loads((tmp_path / "team-labels.json").read_text())
    assert "ghostty:ttys025" not in raw["labels"]
    assert "Claude::2026-05-21T10:19" not in raw["fallback"]


def test_snapshot_resolves_label_from_main(monkeypatch, tmp_path):
    agg = _agg(tmp_path)
    agg.set_label("ghostty:ttys025", "moras-finder",
                  cli="Claude", started_at="2026-05-21T10:19:37")
    _patch_ps(monkeypatch, [("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    snap = agg.snapshot()
    assert snap["teams"][0]["label"] == "moras-finder"


def test_snapshot_resolves_label_from_fallback_after_tty_change(monkeypatch, tmp_path):
    """TTY 从 ttys025 重分配到 ttys027, fallback key 仍命中."""
    agg = _agg(tmp_path)
    agg.set_label("ghostty:ttys025", "moras-finder",
                  cli="Claude", started_at="2026-05-21T10:19:37")
    # 同 cli + 同 started_at 但 tty 变了
    _patch_ps(monkeypatch, [("ttys027", 300, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    snap = agg.snapshot()
    assert snap["teams"][0]["id"] == "ghostty:ttys027"
    assert snap["teams"][0]["label"] == "moras-finder"  # fallback 命中


def test_snapshot_with_no_label_returns_empty_string(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    assert snap["teams"][0]["label"] == ""


def test_label_file_chmod_600(tmp_path):
    agg = _agg(tmp_path)
    agg.set_label("ghostty:ttys025", "moras-finder",
                  cli="Claude", started_at="2026-05-21T10:19:37")
    path = tmp_path / "team-labels.json"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupt_label_file_treated_as_empty(monkeypatch, tmp_path):
    (tmp_path / "team-labels.json").write_text("not json")
    _patch_ps(monkeypatch, [("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    # 不抛, 退化到空 label
    assert snap["teams"][0]["label"] == ""


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------

def test_ghostty_sorted_by_started_at_desc(monkeypatch, tmp_path):
    _patch_ps(monkeypatch, [
        ("ttys001", 100, "08:00", "Claude", "Thu May 21 09:00:00 2026"),  # 旧
        ("ttys002", 200, "01:00", "Claude", "Thu May 21 12:00:00 2026"),  # 新
    ])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    ids = [t["id"] for t in snap["teams"]]
    assert ids == ["ghostty:ttys002", "ghostty:ttys001"]


def test_lstart_parse_failure_returns_empty(monkeypatch, tmp_path):
    """坏 lstart 应不让整条记录消失, started_at 留空."""
    _patch_ps(monkeypatch, [("ttys001", 100, "00:30", "Claude", "garbage")])
    _patch_tmux(monkeypatch, [])
    snap = _agg(tmp_path).snapshot()
    assert snap["teams"][0]["started_at"] == ""


def test_snapshot_joins_team_links(monkeypatch, tmp_path):
    """tmux team 若在 team_links 里有记录, snapshot 给附 linked 字段."""
    import team_links as tl
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-xyz789", "K2Lab/foo", 441, "pr",
                   "https://git.k2lab.ai/K2Lab/foo/pulls/441")
    _patch_ps(monkeypatch, [])
    _patch_tmux(monkeypatch, [("agent-team-xyz789", 1, 1779356980)],
                panes={"agent-team-xyz789": [{"index": 0, "pid": 1234, "command": "claude", "started_at": ""}]})
    agg = at.AgentTeamAggregator(court_root=tmp_path, team_links=links)
    snap = agg.snapshot()
    assert snap["teams"][0]["linked"]["repo"] == "K2Lab/foo"
    assert snap["teams"][0]["linked"]["number"] == 441


def test_snapshot_unlinked_team_linked_is_none(monkeypatch, tmp_path):
    import team_links as tl
    links = tl.TeamLinks(court_root=tmp_path)
    _patch_ps(monkeypatch, [("ttys025", 200, "00:30", "Claude", "Thu May 21 10:19:37 2026")])
    _patch_tmux(monkeypatch, [])
    agg = at.AgentTeamAggregator(court_root=tmp_path, team_links=links)
    snap = agg.snapshot()
    assert snap["teams"][0]["linked"] is None
