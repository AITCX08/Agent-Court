"""PR-17b: AgentSpawner — tmux session spawn + send-keys 任务投递 测试."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import agent_spawn as ag  # noqa: E402
import team_links as tl  # noqa: E402


@pytest.fixture
def links(tmp_path) -> tl.TeamLinks:
    return tl.TeamLinks(court_root=tmp_path)


def test_spawn_creates_tmux_session_and_sends_task(monkeypatch, links):
    run_calls: list[list[str]] = []

    def fake_run(args, *, check=False, **_):
        run_calls.append(list(args))
        rc = 0
        return MagicMock(returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    monkeypatch.setattr(ag, "_generate_team_uuid", lambda: "abc12345")

    spawner = ag.AgentSpawner(team_links=links)
    result = spawner.spawn(repo="K2Lab/foo", number=441, kind="pr",
                          url="https://git.k2lab.ai/K2Lab/foo/pulls/441")

    assert result["team_id"] == "agent-team-abc12345"
    assert result["session"] == "agent-team-abc12345"
    assert result["already_spawned"] is False
    # 验证调了 tmux new-session
    new_session_calls = [c for c in run_calls if "new-session" in c]
    assert len(new_session_calls) == 1
    assert "agent-team-abc12345" in new_session_calls[0]
    # 验证 send-keys 至少 2 次 (启 claude + 投递任务)
    send_keys_calls = [c for c in run_calls if "send-keys" in c]
    assert len(send_keys_calls) >= 2
    # link 落盘
    assert links.lookup_by_team("agent-team-abc12345") is not None


def test_spawn_dedup_returns_existing_team(monkeypatch, links):
    links.set_link("agent-team-existing", "K2Lab/foo", 441, "pr",
                   "https://git.k2lab.ai/K2Lab/foo/pulls/441")
    run_calls: list[list[str]] = []
    monkeypatch.setattr(ag.subprocess, "run",
                        lambda args, **kw: (run_calls.append(list(args)) or MagicMock(returncode=0)))
    spawner = ag.AgentSpawner(team_links=links)
    result = spawner.spawn(repo="K2Lab/foo", number=441, kind="pr", url="x")
    assert result["team_id"] == "agent-team-existing"
    assert result["already_spawned"] is True
    # 不应该调 tmux new-session
    assert not any("new-session" in c for c in run_calls)


def test_spawn_pr_kind_task_text_mentions_pr_review(monkeypatch, links):
    captured: list[str] = []

    def fake_run(args, **_):
        captured.extend(args)
        return MagicMock(returncode=0)

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    monkeypatch.setattr(ag, "_generate_team_uuid", lambda: "abc12345")
    spawner = ag.AgentSpawner(team_links=links)
    spawner.spawn(repo="K2Lab/foo", number=441, kind="pr", url="url")
    joined = " ".join(captured)
    assert "PR" in joined or "pr" in joined
    assert "441" in joined
    assert "K2Lab/foo" in joined


def test_spawn_issue_kind_task_text_mentions_issue(monkeypatch, links):
    captured: list[str] = []

    def fake_run(args, **_):
        captured.extend(args)
        return MagicMock(returncode=0)

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    monkeypatch.setattr(ag, "_generate_team_uuid", lambda: "abc12345")
    spawner = ag.AgentSpawner(team_links=links)
    spawner.spawn(repo="K2Lab/foo", number=99, kind="issue", url="url")
    joined = " ".join(captured)
    assert "issue" in joined.lower() or "Issue" in joined
    assert "99" in joined


def test_spawn_invalid_kind_raises(links):
    spawner = ag.AgentSpawner(team_links=links)
    with pytest.raises(ValueError):
        spawner.spawn(repo="K2Lab/foo", number=1, kind="nonsense", url="x")


def test_spawn_tmux_new_session_failure_no_link_written(monkeypatch, links):
    monkeypatch.setattr(ag, "_generate_team_uuid", lambda: "abc12345")

    def fake_run(args, **_):
        if "new-session" in args:
            return MagicMock(returncode=1, stderr="port busy")
        return MagicMock(returncode=0)

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    spawner = ag.AgentSpawner(team_links=links)
    with pytest.raises(ag.SpawnError):
        spawner.spawn(repo="K2Lab/foo", number=441, kind="pr", url="x")
    assert links.lookup_by_team("agent-team-abc12345") is None


def test_kill_rejects_invalid_prefix(links):
    spawner = ag.AgentSpawner(team_links=links)
    with pytest.raises(ValueError):
        spawner.kill("ghostty:ttys025")
    with pytest.raises(ValueError):
        spawner.kill("agent-team-xyz")  # 缺 tmux: 前缀


def test_kill_calls_tmux_and_removes_link(monkeypatch, links):
    links.set_link("agent-team-tokill", "K2Lab/foo", 1, "pr", "url")
    run_calls: list[list[str]] = []

    def fake_run(args, **_):
        run_calls.append(list(args))
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    spawner = ag.AgentSpawner(team_links=links)
    result = spawner.kill("tmux:agent-team-tokill")
    assert result["ok"] is True
    assert result["session"] == "agent-team-tokill"
    kill_calls = [c for c in run_calls if "kill-session" in c]
    assert len(kill_calls) == 1
    assert "agent-team-tokill" in kill_calls[0]
    # link 应被清掉
    assert links.lookup_by_team("agent-team-tokill") is None


def test_kill_session_not_found_is_idempotent(monkeypatch, links):
    """tmux 已 dead 的 session, kill 应不报错, 仍清 link."""
    links.set_link("agent-team-gone", "K2Lab/foo", 1, "pr", "url")

    def fake_run(args, **_):
        return MagicMock(returncode=1, stderr="can't find session: agent-team-gone")

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    spawner = ag.AgentSpawner(team_links=links)
    result = spawner.kill("tmux:agent-team-gone")
    assert result["ok"] is True
    assert links.lookup_by_team("agent-team-gone") is None


def test_kill_other_tmux_error_raises_spawnerror(monkeypatch, links):
    links.set_link("agent-team-evil", "K2Lab/foo", 1, "pr", "url")

    def fake_run(args, **_):
        return MagicMock(returncode=1, stderr="server not running on socket")

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    spawner = ag.AgentSpawner(team_links=links)
    with pytest.raises(ag.SpawnError):
        spawner.kill("tmux:agent-team-evil")
    # 失败时 link 不清 (resource leak 总比删错 ID 好)
    assert links.lookup_by_team("agent-team-evil") is not None
