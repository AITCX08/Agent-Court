"""Tests for agent_summary (PR-19c-2): one-line AI summary of agent pane."""
from __future__ import annotations

import json
import subprocess
import sys
import time
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


def test_ghostty_no_pid_does_not_call_capture():
    """ghostty:* 没传 pid → sentinel='ghostty-no-pid', 不调 capture/runner."""
    cap = MagicMock()
    run = MagicMock()
    r = get_summary("ghostty:ttys025", capture=cap, runner=run)
    assert r.sentinel == "ghostty-no-pid"
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
    # PR-20e: 默认 CLI 切到 claude sonnet 4.6 (不带 --bare, 走 OAuth)
    argv = runner_calls[0][0]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--model" in argv
    assert "claude-sonnet-4-6" in argv
    assert "--bare" not in argv  # --bare 会要 ANTHROPIC_API_KEY env
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
        raise FileNotFoundError("claude")
    r = get_summary("agent-team-nocli", capture=cap, runner=fake_run)
    assert r.sentinel == "error"
    assert "claude" in (r.error or "")


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


# ---- PR-19d: ghostty branch ----


def test_ghostty_lsof_cwd_missing_returns_sentinel():
    """lsof 返非零 → ghostty-no-cwd."""
    cap = MagicMock()
    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 1; cp.stdout = ""; cp.stderr = "lsof: no info"
            return cp
        return _ok(stdout="should-not-reach")
    r = get_summary("ghostty:ttys025", pid=12345, capture=cap, runner=fake_run)
    assert r.sentinel == "ghostty-no-cwd"


def test_ghostty_jsonl_missing_returns_sentinel(tmp_path):
    """lsof 拿到 cwd 但 ~/.claude/projects 下没匹配目录 → ghostty-no-session."""
    cap = MagicMock()
    fake_cwd = "/Users/wjx/nonexistent"
    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 0
            cp.stdout = f"p12345\nfcwd\nn{fake_cwd}\n"
            cp.stderr = ""
            return cp
        return _ok(stdout="x")
    # 用 tmp_path 当 claude_root, 里面啥都没有
    r = get_summary("ghostty:ttys025", pid=12345, capture=cap, runner=fake_run, claude_root=tmp_path)
    assert r.sentinel == "ghostty-no-session"


def test_ghostty_jsonl_empty_content_returns_sentinel(tmp_path):
    """有 jsonl 但里面全是空行 → ghostty-no-content."""
    cap = MagicMock()
    proj = tmp_path / "-Users-wjx-foo"
    proj.mkdir()
    (proj / "session1.jsonl").write_text("\n\n")
    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 0
            cp.stdout = "p12345\nfcwd\nn/Users/wjx/foo\n"
            cp.stderr = ""
            return cp
        return _ok(stdout="x")
    r = get_summary("ghostty:ttys025", pid=12345, capture=cap, runner=fake_run, claude_root=tmp_path)
    assert r.sentinel == "ghostty-no-content"


def test_ghostty_happy_path(tmp_path):
    """完整流程: lsof → projects 目录 → jsonl → codex → 一句话."""
    cap = MagicMock()
    proj = tmp_path / "-Users-wjx-foo"
    proj.mkdir()
    # 写两条 jsonl 假数据
    jsonl_lines = [
        json.dumps({"message": {"role": "user", "content": "帮我改下 PR-37 的测试"}}),
        json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "好, 我看一下 PR 内容"}]}}),
    ]
    (proj / "session1.jsonl").write_text("\n".join(jsonl_lines) + "\n")
    runner_calls = []
    def fake_run(argv, **kwargs):
        runner_calls.append((list(argv), kwargs.get("input", "")))
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 0
            cp.stdout = "p12345\nfcwd\nn/Users/wjx/foo\n"
            cp.stderr = ""
            return cp
        # codex
        return _ok(stdout="正在改 PR-37 的测试\n")

    r = get_summary("ghostty:ttys025", pid=12345, capture=cap, runner=fake_run, claude_root=tmp_path)
    assert r.sentinel is None
    assert r.summary == "正在改 PR-37 的测试"
    # 第二次调 codex 时, input 应当含 jsonl 内容
    codex_calls = [c for c in runner_calls if c[0][0] != "lsof"]
    assert len(codex_calls) == 1
    assert "PR-37" in codex_calls[0][1] or "帮我改" in codex_calls[0][1]


def test_ghostty_picks_latest_jsonl_by_mtime(tmp_path):
    cap = MagicMock()
    proj = tmp_path / "-Users-wjx-foo"
    proj.mkdir()
    old = proj / "old.jsonl"
    new = proj / "new.jsonl"
    old.write_text(json.dumps({"message": {"role": "user", "content": "OLD"}}) + "\n")
    new.write_text(json.dumps({"message": {"role": "user", "content": "NEW"}}) + "\n")
    # 让 new 比 old 新 (即使我们刚写完两个文件)
    import os as _os
    _os.utime(old, (time.time() - 100, time.time() - 100))

    captured_input = {"text": ""}
    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 0
            cp.stdout = "p1\nfcwd\nn/Users/wjx/foo\n"
            cp.stderr = ""
            return cp
        captured_input["text"] = kwargs.get("input", "")
        return _ok(stdout="OK")

    get_summary("ghostty:ttys001", pid=1, capture=cap, runner=fake_run, claude_root=tmp_path)
    assert "NEW" in captured_input["text"]
    assert "OLD" not in captured_input["text"]


def test_ghostty_ignores_stale_jsonl(tmp_path):
    """超过 60 分钟没改的 jsonl 不应被选 (用户可能换 session 了)."""
    cap = MagicMock()
    proj = tmp_path / "-Users-wjx-foo"
    proj.mkdir()
    stale = proj / "stale.jsonl"
    stale.write_text(json.dumps({"message": {"role": "user", "content": "OLD"}}) + "\n")
    import os as _os
    _os.utime(stale, (time.time() - 3700, time.time() - 3700))  # 61+ 分钟前
    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            cp = MagicMock(spec=subprocess.CompletedProcess)
            cp.returncode = 0
            cp.stdout = "p1\nfcwd\nn/Users/wjx/foo\n"
            cp.stderr = ""
            return cp
        return _ok(stdout="x")
    r = get_summary("ghostty:ttys001", pid=1, capture=cap, runner=fake_run, claude_root=tmp_path)
    assert r.sentinel == "ghostty-no-session"  # stale 被排除


def test_helper_cwd_to_projects_dir():
    from agent_summary import _cwd_to_projects_dir
    assert _cwd_to_projects_dir("/Users/wjx/Desktop") == "-Users-wjx-Desktop"
    assert _cwd_to_projects_dir("/") == "-"
    assert _cwd_to_projects_dir("relative-path") == ""
