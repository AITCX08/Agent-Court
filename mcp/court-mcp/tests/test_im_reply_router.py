"""ImReplyRouter (PR-13 重写后) 测试.

PR-13 review C6 之后, router 只负责 INTAKE 阶段:
- approve: 调 spawn-issue-window + update seen DISPATCHED_DASHBOARD
- reject: comment + close + update seen REJECTED_DASHBOARD

PLAN 阶段不再由 router 注入 (dual_channel_approval._wait_for_result 内部 drain),
旧的 test_plan_edit_injects_tmux_command 已删除.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from im_reply_router import ImReplyRouter


REPO = "K2Lab/test"
NUM = 7
SLUG = "k2lab-test"


class _StubGitea:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.transitions: list[tuple[str, int, str]] = []

    def comment_on_issue(self, repo: str, num: int, body: str) -> dict:
        self.comments.append((repo, num, body))
        return {"id": 1}

    def transition_issue(self, repo: str, num: int, state: str) -> dict:
        self.transitions.append((repo, num, state))
        return {"state": state}


def _setup_fixtures(tmp_path: Path) -> tuple[Path, Path, Path]:
    pending = tmp_path / "gitea-watcher" / "pending-approval"
    ctx = tmp_path / "gitea-watcher" / "pending-intake-context"
    pending.mkdir(parents=True)
    ctx.mkdir(parents=True)
    (ctx / f"{SLUG}-{NUM}.json").write_text(json.dumps({
        "issue": {
            "number": NUM,
            "title": "fixture",
            "html_url": f"http://localhost/{REPO}/issues/{NUM}",
            "body": "fixture body",
            "labels": [],
            "repository": {"full_name": REPO},
        },
        "decision": {
            "decision": "GO",
            "court_project_name": f"issue-{SLUG}-{NUM}",
            "branch_prefix": f"auto/issue-{NUM}/",
            "agent_team_plan": {},
        },
        "comments": [],
    }))
    return pending, ctx, tmp_path


def _make_stub_spawn(tmp_path: Path) -> Path:
    stub = tmp_path / "spawn-issue-window-stub.sh"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    return stub


def test_intake_approve_dispatches_window_and_updates_seen(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps({
        "repo": REPO, "number": NUM, "stage": "INTAKE",
        "verdict": "approve", "winner": "terminal", "reason": "", "edit_instruction": "",
    }))
    gitea = _StubGitea()
    router = ImReplyRouter(court_root, gitea_client=gitea, spawn_window_bin=stub)
    n = router.scan_once()
    assert n == 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    entry = seen[f"{REPO}#{NUM}"]
    assert entry["last_action"] == "DISPATCHED_DASHBOARD"
    assert entry["approval_winner"] == "terminal"
    assert entry["tmux_window"]
    # .result 被 archive
    archived = list((pending / ".processed").glob("*"))
    assert len(archived) == 1
    # reject path 未触发
    assert gitea.comments == []
    assert gitea.transitions == []


def test_intake_reject_comments_and_closes(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps({
        "repo": REPO, "number": NUM, "stage": "INTAKE",
        "verdict": "reject", "winner": "feishu", "reason": "重复 issue", "edit_instruction": "",
    }))
    gitea = _StubGitea()
    router = ImReplyRouter(court_root, gitea_client=gitea, spawn_window_bin=stub)
    n = router.scan_once()
    assert n == 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    entry = seen[f"{REPO}#{NUM}"]
    assert entry["last_action"] == "REJECTED_DASHBOARD"
    assert entry["approval_winner"] == "feishu"
    assert gitea.comments == [(REPO, NUM, "重复 issue")]
    assert gitea.transitions == [(REPO, NUM, "closed")]


def test_router_ignores_plan_results(tmp_path, monkeypatch):
    """PR-13 C6: router 不再处理 PLAN result (plan 由 _wait_for_result 内部 drain)."""
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-plan.result").write_text(json.dumps({
        "repo": REPO, "number": NUM, "stage": "PLAN",
        "verdict": "approve", "winner": "terminal",
    }))
    gitea = _StubGitea()
    router = ImReplyRouter(court_root, gitea_client=gitea, spawn_window_bin=stub)
    n = router.scan_once()
    assert n == 0  # router 只 glob *-intake.result, plan.result 不被 scan
    seen_path = court_root / "gitea-watcher" / "seen-issues.json"
    assert not seen_path.exists()


def test_invalid_json_archived_with_reason(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    bad = pending / f"{SLUG}-{NUM}-intake.result"
    bad.write_text("not json {{{")
    gitea = _StubGitea()
    router = ImReplyRouter(court_root, gitea_client=gitea, spawn_window_bin=stub)
    n = router.scan_once()
    assert n == 1
    archived = list((pending / ".processed").glob("*invalid-json*"))
    assert len(archived) == 1


def test_missing_context_archived(tmp_path, monkeypatch):
    pending, ctx, court_root = _setup_fixtures(tmp_path)
    # 把 ctx 清掉 (模拟 watcher 没写 context 但 result 来了)
    for f in ctx.iterdir():
        f.unlink()
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps({
        "repo": REPO, "number": NUM, "stage": "INTAKE",
        "verdict": "approve", "winner": "terminal",
    }))
    gitea = _StubGitea()
    router = ImReplyRouter(court_root, gitea_client=gitea, spawn_window_bin=stub)
    n = router.scan_once()
    assert n == 1
    archived = list((pending / ".processed").glob("*missing-context*"))
    assert len(archived) == 1


# ---------------------------------------------------------------------------
# SY-4 #17: bounded concurrency + retry queue
# ---------------------------------------------------------------------------

from retry_queue import RetryQueue  # noqa: E402
from workflow_loader import WorkflowConfig  # noqa: E402


def _result_approve(repo: str, num: int) -> dict:
    return {"repo": repo, "number": num, "stage": "INTAKE",
            "verdict": "approve", "winner": "terminal", "reason": "", "edit_instruction": ""}


def test_at_capacity_defers_and_pushes_retry_queue(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps(_result_approve(REPO, NUM)))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=60)
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(max_concurrent_runs=2),
        retry_queue=rq,
        active_court_counter=lambda: 2,  # 已满
    )
    n = router.scan_once()
    assert n == 1
    # seen 标记 DEFERRED_CAPACITY
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "DEFERRED_CAPACITY"
    # retry queue 有这条
    items = rq.snapshot()
    assert len(items) == 1
    assert items[0].issue_key == f"{REPO}#{NUM}"
    assert "concurrency cap" in items[0].last_error


def test_under_capacity_dispatches_and_clears_retry(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps(_result_approve(REPO, NUM)))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=60)
    # 预置一条 (模拟之前 deferred 过的)
    rq.push(f"{REPO}#{NUM}", "previous defer")
    assert len(rq) == 1
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(max_concurrent_runs=5),
        retry_queue=rq,
        active_court_counter=lambda: 1,  # 未满
    )
    n = router.scan_once()
    assert n == 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "DISPATCHED_DASHBOARD"
    # dispatch 成功 → retry queue 清空
    assert len(rq) == 0


def test_spawn_failure_pushes_retry_queue(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    bad_stub = tmp_path / "always-fail.sh"
    bad_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    bad_stub.chmod(0o755)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps(_result_approve(REPO, NUM)))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=60)
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=bad_stub,
        workflow_config=WorkflowConfig(max_concurrent_runs=5),
        retry_queue=rq,
        active_court_counter=lambda: 0,
    )
    n = router.scan_once()
    assert n == 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "SPAWN_FAILED"
    items = rq.snapshot()
    assert len(items) == 1
    assert "spawn-issue-window failed" in items[0].last_error


def test_no_workflow_config_no_capacity_check(tmp_path, monkeypatch):
    """workflow_config=None 时不限流, 保持向后兼容 (老行为)."""
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (pending / f"{SLUG}-{NUM}-intake.result").write_text(json.dumps(_result_approve(REPO, NUM)))
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=None,  # 显式禁用 auto-load
        retry_queue=None,
        active_court_counter=lambda: 9999,  # 假装已经满了 9999 个 court
    )
    n = router.scan_once()
    assert n == 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    # 没限流 → 正常 dispatch
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "DISPATCHED_DASHBOARD"


# ---------------------------------------------------------------------------
# SY-4 review R-1: retry queue 自己 tick (没有外部 daemon 兜底)
# ---------------------------------------------------------------------------

def test_retry_queue_pop_due_dispatches_when_under_capacity(tmp_path, monkeypatch):
    """到点 retry 在 scan_once 末尾被自动消费, dispatch 成功后清掉 queue."""
    pending, ctx_dir, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    # 没有新 result 文件; 但 retry queue 里有一条到点的
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=1)
    rq.push(f"{REPO}#{NUM}", "previous spawn failed")
    # 预先把 seen-issues 写好让 _dispatch_approved 能写 winner
    (court_root / "gitea-watcher").mkdir(parents=True, exist_ok=True)
    (court_root / "gitea-watcher" / "seen-issues.json").write_text(json.dumps({
        f"{REPO}#{NUM}": {"approval_winner": "feishu", "stage": "INTAKE"},
    }))
    # 等过 backoff (1s base * 2^0 = 1s)
    import time
    time.sleep(1.1)
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(max_concurrent_runs=5),
        retry_queue=rq,
        active_court_counter=lambda: 0,
    )
    n = router.scan_once()
    # n 含 retry dispatch 数
    assert n >= 1
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "DISPATCHED_DASHBOARD"
    assert seen[f"{REPO}#{NUM}"]["approval_winner"] == "feishu"  # 保留历史 winner
    # retry queue 已清
    assert len(rq) == 0


def test_retry_pop_due_skip_when_context_missing(tmp_path, monkeypatch):
    """context dir 没了 → retry skip, 不再 re-push (避免无限循环)."""
    pending, ctx_dir, court_root = _setup_fixtures(tmp_path)
    # 删 context 模拟丢失
    for f in ctx_dir.iterdir():
        f.unlink()
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=0.001)
    rq.push("ghost/repo#99", "old")
    import time
    time.sleep(0.01)
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(),
        retry_queue=rq,
        active_court_counter=lambda: 0,
    )
    router.scan_once()
    # context 丢 → skip, queue 已被 pop_due 清掉 (没再 push)
    assert len(rq) == 0


# ---------------------------------------------------------------------------
# SY-4 review Mi-2: run_timeout_seconds 守护
# ---------------------------------------------------------------------------

def test_timeout_kills_old_dispatched_window_and_pushes_retry(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    # 模拟一个 30 min 前 dispatched 的 court (跑超了)
    (court_root / "gitea-watcher").mkdir(parents=True, exist_ok=True)
    (court_root / "gitea-watcher" / "seen-issues.json").write_text(json.dumps({
        f"{REPO}#{NUM}": {
            "last_action": "DISPATCHED_DASHBOARD",
            "dispatched_at": "2020-01-01T00:00:00Z",  # 远古
            "tmux_window": "k2lab-test-7",
        },
    }))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=60)
    kill_calls: list[str] = []
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(run_timeout_seconds=1800),
        retry_queue=rq,
        active_court_counter=lambda: 1,
    )
    # mock 出 tmux kill 不真打
    def fake_kill(window_name):
        kill_calls.append(window_name)
    router._kill_tmux_window = fake_kill
    n = router.scan_once()
    assert kill_calls == ["k2lab-test-7"]
    seen = json.loads((court_root / "gitea-watcher" / "seen-issues.json").read_text())
    assert seen[f"{REPO}#{NUM}"]["last_action"] == "TIMEOUT_KILLED"
    assert seen[f"{REPO}#{NUM}"]["timeout_killed_at"]
    # 进 retry queue
    items = rq.snapshot()
    assert len(items) == 1
    assert "timeout after 1800s" in items[0].last_error


def test_timeout_skips_recent_dispatched_window(tmp_path, monkeypatch):
    """刚 dispatch 不久 (< timeout) 不该被 kill."""
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (court_root / "gitea-watcher").mkdir(parents=True, exist_ok=True)
    (court_root / "gitea-watcher" / "seen-issues.json").write_text(json.dumps({
        f"{REPO}#{NUM}": {
            "last_action": "DISPATCHED_DASHBOARD",
            "dispatched_at": now,  # 刚刚
            "tmux_window": "k2lab-test-7",
        },
    }))
    rq = RetryQueue(state_dir=court_root / "gitea-watcher", max_attempts=3, base_backoff_seconds=60)
    kill_calls: list[str] = []
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=WorkflowConfig(run_timeout_seconds=1800),
        retry_queue=rq,
        active_court_counter=lambda: 1,
    )
    router._kill_tmux_window = lambda w: kill_calls.append(w)
    router.scan_once()
    assert kill_calls == []  # 没杀
    assert len(rq) == 0


def test_timeout_disabled_when_workflow_config_missing(tmp_path, monkeypatch):
    pending, _, court_root = _setup_fixtures(tmp_path)
    stub = _make_stub_spawn(tmp_path)
    monkeypatch.setenv("COURT_ROOT", str(court_root))
    (court_root / "gitea-watcher").mkdir(parents=True, exist_ok=True)
    (court_root / "gitea-watcher" / "seen-issues.json").write_text(json.dumps({
        f"{REPO}#{NUM}": {
            "last_action": "DISPATCHED_DASHBOARD",
            "dispatched_at": "2020-01-01T00:00:00Z",  # 远古
            "tmux_window": "k2lab-test-7",
        },
    }))
    kill_calls: list[str] = []
    router = ImReplyRouter(
        court_root, gitea_client=_StubGitea(), spawn_window_bin=stub,
        workflow_config=None,  # 显式禁用
        retry_queue=None,
        active_court_counter=lambda: 1,
    )
    router._kill_tmux_window = lambda w: kill_calls.append(w)
    router.scan_once()
    assert kill_calls == []  # 没配置 → 不 enforce


def test_iso_older_than_helper():
    from im_reply_router import _iso_older_than
    assert _iso_older_than("2020-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 60) is True
    assert _iso_older_than("2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z", 60) is False
    assert _iso_older_than("", "2026-01-01T00:00:00Z", 60) is False
    assert _iso_older_than("garbage", "2026-01-01T00:00:00Z", 60) is False
