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
