"""Tests for auto_review.executor — LightExecutor + DeepExecutor."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.executor import (
    LightExecutor,
    DeepExecutor,
    ReviewResult,
)
from auto_review.state import AutoReviewTask, TaskKind, TaskState


def _task(kind=TaskKind.PR, repo="K2Lab/agent-court", number=42, head_sha="abc123"):
    return AutoReviewTask(
        id=1,
        dedupe_key=f"{repo}#{number}@{head_sha}" if head_sha else f"{repo}#{number}",
        kind=kind,
        repo=repo,
        number=number,
        head_sha=head_sha,
        state=TaskState.RUNNING,
        runtime=None,
        discovered_at="2026-05-22T00:00:00Z",
        last_event_at="2026-05-22T00:00:00Z",
        error_message=None,
    )


def _ctx(html_url="https://git.k2lab.ai/K2Lab/agent-court/pulls/42", changed_files=5):
    return {"html_url": html_url, "changed_files": changed_files, "title": "test"}


def _ok_completed(stdout="REVIEW MARKDOWN OUTPUT", stderr=""):
    """Build a fake subprocess.CompletedProcess with exit 0."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _fail_completed(stdout="", stderr="codex: error xyz", code=1):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = code
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------- LightExecutor ----------

def test_light_default_prefer_codex_argv():
    runner_calls = []

    def fake_runner(argv, **kwargs):
        runner_calls.append((list(argv), dict(kwargs)))
        return _ok_completed()

    ex = LightExecutor(runner=fake_runner)
    ex.review(_task(), _ctx())

    assert runner_calls, "runner not invoked"
    argv, kwargs = runner_calls[0]
    assert argv[0:2] == ["codex", "exec"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 600  # default


def test_light_prefer_claude():
    runner_calls = []
    def fake_runner(argv, **kwargs):
        runner_calls.append(list(argv))
        return _ok_completed()
    ex = LightExecutor(prefer="claude", runner=fake_runner)
    ex.review(_task(), _ctx())
    assert runner_calls[0][0] == "claude"


def test_light_prompt_contains_task_metadata():
    captured = {}
    def fake_runner(argv, **kwargs):
        captured["input"] = kwargs.get("input", "")
        return _ok_completed()
    ex = LightExecutor(runner=fake_runner)
    ex.review(
        _task(repo="K2Lab/agent-court", number=42, head_sha="deadbeef"),
        _ctx(html_url="https://git.k2lab.ai/K2Lab/agent-court/pulls/42"),
    )
    prompt = captured["input"]
    assert "K2Lab/agent-court" in prompt
    assert "42" in prompt
    assert "deadbeef" in prompt
    assert "https://git.k2lab.ai/K2Lab/agent-court/pulls/42" in prompt


def test_light_success_returns_review_result():
    def fake_runner(argv, **kwargs):
        return _ok_completed(stdout="GOOD REVIEW\n")
    ex = LightExecutor(runner=fake_runner)
    result = ex.review(_task(), _ctx())
    assert isinstance(result, ReviewResult)
    assert result.success is True
    assert result.runtime == "codex"
    assert "GOOD REVIEW" in result.output
    assert result.error is None


def test_light_non_zero_exit_returns_failure():
    def fake_runner(argv, **kwargs):
        return _fail_completed(stderr="codex: exploded\n", code=2)
    ex = LightExecutor(runner=fake_runner)
    result = ex.review(_task(), _ctx())
    assert result.success is False
    assert result.runtime == "codex"
    assert "exploded" in (result.error or "")


def test_light_timeout_expired_returns_failure():
    def fake_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 600))
    ex = LightExecutor(runner=fake_runner)
    result = ex.review(_task(), _ctx())
    assert result.success is False
    assert "timeout" in (result.error or "").lower()


def test_light_unexpected_exception_returns_failure():
    """OSError / FileNotFoundError (e.g., codex not on PATH) → ReviewResult failure."""
    def fake_runner(argv, **kwargs):
        raise FileNotFoundError("codex")
    ex = LightExecutor(runner=fake_runner)
    result = ex.review(_task(), _ctx())
    assert result.success is False
    assert "codex" in (result.error or "")


# ---------- DeepExecutor ----------

def test_deep_calls_spawner_with_correct_args():
    spawner = MagicMock()
    spawner.spawn.return_value = {
        "team_id": "agent-team-abc123",
        "session": "agent-team-abc123",
        "already_spawned": False,
    }
    ex = DeepExecutor(spawner=spawner)
    result = ex.review(_task(kind=TaskKind.PR), _ctx())

    spawner.spawn.assert_called_once_with(
        repo="K2Lab/agent-court",
        number=42,
        kind="pr",
        url="https://git.k2lab.ai/K2Lab/agent-court/pulls/42",
    )
    assert result.success is True
    assert result.runtime == "team"
    assert result.output == "agent-team-abc123"


def test_deep_handles_already_spawned():
    spawner = MagicMock()
    spawner.spawn.return_value = {
        "team_id": "agent-team-existing",
        "already_spawned": True,
    }
    ex = DeepExecutor(spawner=spawner)
    result = ex.review(_task(), _ctx())
    assert result.success is True
    assert result.output == "agent-team-existing"


def test_deep_spawn_failure_returns_review_failure():
    """If AgentSpawner raises SpawnError, return ReviewResult(success=False)."""
    from agent_spawn import SpawnError
    spawner = MagicMock()
    spawner.spawn.side_effect = SpawnError("tmux new-session failed")
    ex = DeepExecutor(spawner=spawner)
    result = ex.review(_task(), _ctx())
    assert result.success is False
    assert result.runtime == "team"
    assert "tmux new-session failed" in (result.error or "")
