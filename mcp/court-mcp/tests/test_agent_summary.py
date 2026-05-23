"""Tests for agent_summary (PR-19c-2): one-line AI summary of agent pane."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_summary import (
    SummaryResult,
    get_summary,
    invalidate_cache,
    SUMMARY_CACHE_TTL_SEC,
)


def _ok(stdout: str, stderr: str = "") -> MagicMock:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _fail(stderr: str, code: int = 1) -> MagicMock:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = code
    cp.stdout = ""
    cp.stderr = stderr
    return cp


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def test_ghostty_returns_sentinel_immediately():
    """ghostty:* team_id 立即返 sentinel, 不调 capture/runner."""
    cap = MagicMock()
    run = MagicMock()
    r = get_summary("ghostty:ttys025", capture=cap, runner=run)
    assert r.sentinel == "ghostty-no-capture"
    assert r.summary == ""
    cap.assert_not_called()
    run.assert_not_called()


def test_tmux_happy_path_calls_capture_and_runner():
    cap = MagicMock(return_value="pane content here")
    runner_calls = []
    def fake_run(argv, **kwargs):
        runner_calls.append((list(argv), kwargs.get("input", "")))
        return _ok(stdout="正在 review PR-37, 跑测试\n")

    r = get_summary("agent-team-abc", capture=cap, runner=fake_run)

    assert r.sentinel is None
    assert r.error is None
    assert r.summary == "正在 review PR-37, 跑测试"
    assert r.team_id == "agent-team-abc"
    cap.assert_called_once_with("agent-team-abc", lines=80)
    assert runner_calls[0][0][:2] == ["codex", "exec"]
    assert "pane content here" in runner_calls[0][1]


def test_summary_cached_for_30s():
    cap = MagicMock(return_value="x")
    runner = MagicMock(return_value=_ok(stdout="正在干活"))
    fake_time = [1000.0]
    def fake_now():
        return fake_time[0]

    r1 = get_summary("agent-team-c1", capture=cap, runner=runner, now=fake_now)
    fake_time[0] += 10  # within TTL
    r2 = get_summary("agent-team-c1", capture=cap, runner=runner, now=fake_now)
    assert r1.summary == r2.summary
    assert cap.call_count == 1  # 第二次走 cache, 没再 capture
    assert runner.call_count == 1


def test_cache_expires_after_ttl():
    cap = MagicMock(return_value="x")
    runner = MagicMock(side_effect=[_ok(stdout="第一次"), _ok(stdout="第二次")])
    fake_time = [1000.0]
    fake_now = lambda: fake_time[0]

    r1 = get_summary("agent-team-c2", capture=cap, runner=runner, now=fake_now)
    fake_time[0] += SUMMARY_CACHE_TTL_SEC + 1  # 过期
    r2 = get_summary("agent-team-c2", capture=cap, runner=runner, now=fake_now)
    assert r1.summary == "第一次"
    assert r2.summary == "第二次"
    assert cap.call_count == 2


def test_force_refresh_bypasses_cache():
    cap = MagicMock(return_value="x")
    runner = MagicMock(side_effect=[_ok(stdout="A"), _ok(stdout="B")])

    get_summary("agent-team-c3", capture=cap, runner=runner)
    r2 = get_summary("agent-team-c3", capture=cap, runner=runner, force_refresh=True)
    assert r2.summary == "B"
    assert cap.call_count == 2


def test_capture_pane_failure_caches_error():
    from tmux_pane import TmuxPaneError
    cap = MagicMock(side_effect=TmuxPaneError("session not found"))
    runner = MagicMock()
    r = get_summary("agent-team-dead", capture=cap, runner=runner)
    assert r.sentinel == "error"
    assert "session not found" in (r.error or "")
    runner.assert_not_called()
    # 缓存 error 防止反复打 dead session
    r2 = get_summary("agent-team-dead", capture=cap, runner=runner)
    assert cap.call_count == 1  # 第二次走 cache


def test_cli_timeout_returns_error():
    cap = MagicMock(return_value="x")
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))
    r = get_summary("agent-team-slow", capture=cap, runner=fake_run)
    assert r.sentinel == "error"
    assert "timeout" in (r.error or "").lower()


def test_cli_not_found_returns_error():
    cap = MagicMock(return_value="x")
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("codex")
    r = get_summary("agent-team-nocli", capture=cap, runner=fake_run)
    assert r.sentinel == "error"
    assert "codex" in (r.error or "")


def test_cli_non_zero_exit_returns_error():
    cap = MagicMock(return_value="x")
    runner = MagicMock(return_value=_fail(stderr="model out of budget"))
    r = get_summary("agent-team-fail", capture=cap, runner=runner)
    assert r.sentinel == "error"
    assert "model out of budget" in (r.error or "")


def test_empty_stdout_treated_as_error():
    cap = MagicMock(return_value="x")
    runner = MagicMock(return_value=_ok(stdout=""))
    r = get_summary("agent-team-empty", capture=cap, runner=runner)
    assert r.sentinel == "error"
    assert "empty" in (r.error or "").lower()


def test_summary_caps_at_120_chars():
    """CLI 不听话输出 500 字也只保留 120."""
    long_out = "a" * 500
    cap = MagicMock(return_value="x")
    runner = MagicMock(return_value=_ok(stdout=long_out))
    r = get_summary("agent-team-long", capture=cap, runner=runner)
    assert len(r.summary) == 120


def test_invalidate_cache_specific_team():
    cap = MagicMock(return_value="x")
    runner = MagicMock(side_effect=[_ok(stdout="A"), _ok(stdout="B")])

    get_summary("agent-team-i1", capture=cap, runner=runner)
    invalidate_cache("agent-team-i1")
    r = get_summary("agent-team-i1", capture=cap, runner=runner)
    assert r.summary == "B"
    assert cap.call_count == 2
