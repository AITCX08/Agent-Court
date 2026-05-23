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


# ---- PR-19b-1: spawn_freeform ----


def _ok_completed(stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr=stderr)


def _fail_completed(stderr: str = "fail") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def test_spawn_freeform_creates_tmux_session_without_team_links(monkeypatch):
    """spawn_freeform 不写 team_links (它不绑 PR/issue)."""
    from agent_spawn import AgentSpawner
    from team_links import TeamLinks
    from pathlib import Path
    import tempfile

    calls = []
    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed()

    monkeypatch.setattr("agent_spawn.subprocess.run", fake_run)
    monkeypatch.setattr("agent_spawn._generate_team_uuid", lambda: "abc12345")

    with tempfile.TemporaryDirectory() as td:
        tl_local = TeamLinks(court_root=Path(td))
        spawner = AgentSpawner(team_links=tl_local)

        result = spawner.spawn_freeform(
            label="试做一个 X 功能",
            initial_prompt="我想要 X 这个东西, 大概是 ... (用户大白话)",
        )

        # 返回值
        assert result["team_id"] == "agent-team-abc12345"
        assert result["already_spawned"] is False
        assert result.get("linked") is None  # 不绑 PR/issue
        assert result["label"] == "试做一个 X 功能"

        # team_links 不应有这个 team 的记录 (lookup_by_team 返 None)
        assert tl_local.lookup_by_team("agent-team-abc12345") is None


def test_spawn_freeform_calls_tmux_new_session_then_claude(monkeypatch):
    """PR-19e: tmux new-session → send-keys claude → wait → set-buffer + paste-buffer + Enter."""
    from agent_spawn import AgentSpawner
    from team_links import TeamLinks
    from pathlib import Path
    import tempfile

    calls = []
    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed()

    monkeypatch.setattr("agent_spawn.subprocess.run", fake_run)
    monkeypatch.setattr("agent_spawn._generate_team_uuid", lambda: "x1")
    # PR-19e: skip the 2.5s claude-warmup sleep in tests
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda _: None)

    with tempfile.TemporaryDirectory() as td:
        spawner = AgentSpawner(team_links=TeamLinks(court_root=Path(td)))
        spawner.spawn_freeform(label="L", initial_prompt="P")

    # tmux new-session 是第 1 个 call
    assert calls[0][0:4] == ["tmux", "new-session", "-d", "-s"]
    # send-keys claude 第 2 个
    assert calls[1][0:3] == ["tmux", "send-keys", "-t"]
    assert "claude" in calls[1]
    # PR-19e: bootstrap 走 set-buffer + paste-buffer 而不是 send-keys -l
    set_buf_calls = [c for c in calls if c[0:2] == ["tmux", "set-buffer"]]
    paste_buf_calls = [c for c in calls if c[0:2] == ["tmux", "paste-buffer"]]
    assert len(set_buf_calls) == 1, f"expected 1 set-buffer call, got {len(set_buf_calls)}"
    assert len(paste_buf_calls) == 1, f"expected 1 paste-buffer call, got {len(paste_buf_calls)}"
    # set-buffer 最后一个 arg = 实际投递的 text
    payload = set_buf_calls[0][-1]
    assert "Freeform Agent Bootstrap" in payload
    assert "P" in payload  # initial_prompt 被替换进去
    # Enter 提交 paste
    enter_calls = [c for c in calls if c[0:2] == ["tmux", "send-keys"] and "Enter" in c]
    assert len(enter_calls) >= 2  # 一个启 claude 一个提交 bootstrap


def test_spawn_freeform_label_in_result():
    """Label 透传到返回 dict 给 caller (前端可显示)."""
    from agent_spawn import AgentSpawner
    from team_links import TeamLinks
    from pathlib import Path
    from unittest.mock import patch
    import tempfile

    with tempfile.TemporaryDirectory() as td, \
         patch("agent_spawn.subprocess.run", return_value=_ok_completed()), \
         patch("agent_spawn._generate_team_uuid", return_value="lbl1"):
        spawner = AgentSpawner(team_links=TeamLinks(court_root=Path(td)))
        result = spawner.spawn_freeform(label="my label", initial_prompt="x")
        assert result["label"] == "my label"


def test_spawn_freeform_tmux_failure_raises_spawn_error(monkeypatch):
    """tmux new-session 失败 → SpawnError."""
    from agent_spawn import AgentSpawner, SpawnError
    from team_links import TeamLinks
    from pathlib import Path
    import tempfile

    call_count = {"n": 0}
    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:  # new-session 第一个调用
            return _fail_completed(stderr="duplicate session name")
        return _ok_completed()
    monkeypatch.setattr("agent_spawn.subprocess.run", fake_run)
    monkeypatch.setattr("agent_spawn._generate_team_uuid", lambda: "fail1")

    with tempfile.TemporaryDirectory() as td:
        spawner = AgentSpawner(team_links=TeamLinks(court_root=Path(td)))
        with pytest.raises(SpawnError, match="duplicate session"):
            spawner.spawn_freeform(label="L", initial_prompt="P")


def test_spawn_freeform_empty_initial_prompt_rejected(monkeypatch):
    """空 prompt 抛 ValueError (前端应当先校验, 后端做最后防线)."""
    from agent_spawn import AgentSpawner
    from team_links import TeamLinks
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        spawner = AgentSpawner(team_links=TeamLinks(court_root=Path(td)))
        with pytest.raises(ValueError, match="initial_prompt"):
            spawner.spawn_freeform(label="L", initial_prompt="")
        with pytest.raises(ValueError, match="initial_prompt"):
            spawner.spawn_freeform(label="L", initial_prompt="   ")
