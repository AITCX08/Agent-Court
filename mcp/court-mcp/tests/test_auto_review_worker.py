"""Tests for auto_review.worker — discovery + active-PR polling threads."""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.config import AutoReviewConfig
from auto_review.bot_account import BotAccount
from auto_review.state import StateStore, TaskKind, TaskState
from auto_review.worker import PollingWorker, DiscoverySummary


class _FakeGiteaClient:
    """Captures search_issues calls and returns canned responses."""

    def __init__(self, responses=None):
        self._responses = responses or {}
        self.calls: list[dict[str, str]] = []

    def search_issues(self, params: dict[str, str]) -> list[dict[str, Any]]:
        self.calls.append(dict(params))
        key = (params.get("type"), params.get("state"))
        return list(self._responses.get(key, []))

    def get_pr_head_sha(self, repo: str, number: int) -> str:
        """PR-18b doesn't need this on the client, but PR-18d will."""
        raise NotImplementedError


def _cfg(watch=("K2Lab/agent-court",), worker_count=2, poll_discovery=60, poll_active=30):
    return AutoReviewConfig(
        bot_username="bot",
        watch_repos=list(watch),
        worker_count=worker_count,
        poll_discovery_interval_sec=poll_discovery,
        poll_active_interval_sec=poll_active,
    )


def _bot():
    return BotAccount(login="bot", user_id=99, email=None)


def _pr_payload(repo, number, head_sha, reviewers=("bot",)):
    return {
        "number": number,
        "title": "test PR",
        "state": "open",
        "head": {"sha": head_sha},
        "requested_reviewers": [{"login": r} for r in reviewers],
        "assignees": [],
        "repository": {"full_name": repo},
        "pull_request": {"html_url": f"https://git.k2lab.ai/{repo}/pulls/{number}"},
    }


def _issue_payload(repo, number, assignees=("bot",)):
    return {
        "number": number,
        "title": "test issue",
        "state": "open",
        "assignees": [{"login": a} for a in assignees],
        "repository": {"full_name": repo},
    }


def test_discovery_enqueues_assigned_pr():
    cfg = _cfg()
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/agent-court", 42, "sha-aaa")],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    summary = w.run_discovery_once()

    assert isinstance(summary, DiscoverySummary)
    assert summary.new_tasks == 1
    assert summary.dedupe_skipped == 0
    task = store.get_by_dedupe_key("K2Lab/agent-court#42@sha-aaa")
    assert task is not None
    assert task.state == TaskState.DISCOVERED
    assert task.kind == TaskKind.PR
    assert task.head_sha == "sha-aaa"


def test_discovery_skips_pr_without_bot_as_reviewer():
    cfg = _cfg()
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/agent-court", 42, "sha-aaa", reviewers=("alice",))],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    summary = w.run_discovery_once()

    assert summary.new_tasks == 0
    assert store.count() == 0


def test_discovery_skips_pr_outside_watch_repos():
    cfg = _cfg(watch=("K2Lab/agent-court",))
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/other-repo", 1, "sha-x")],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    summary = w.run_discovery_once()
    assert summary.new_tasks == 0
    assert store.count() == 0


def test_discovery_enqueues_assigned_issue():
    cfg = _cfg()
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [],
            ("issues", "open"): [_issue_payload("K2Lab/agent-court", 7)],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    summary = w.run_discovery_once()
    assert summary.new_tasks == 1
    task = store.get_by_dedupe_key("K2Lab/agent-court#7")
    assert task.kind == TaskKind.ISSUE
    assert task.head_sha is None


def test_discovery_dedupes_repeat_run():
    cfg = _cfg()
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/agent-court", 42, "sha-aaa")],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)
    w.run_discovery_once()

    summary2 = w.run_discovery_once()
    assert summary2.new_tasks == 0
    assert summary2.dedupe_skipped == 1
    assert store.count() == 1


def test_active_polling_detects_head_sha_change():
    """已知 PR head_sha 变了 → 创建新 task (老 task 不动)."""
    cfg = _cfg()
    store = StateStore(":memory:")
    # 第一轮 discovery
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/agent-court", 42, "sha-old")],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)
    w.run_discovery_once()
    assert store.count() == 1

    # 模拟 head_sha 变化, active poll 应当再入队
    client._responses[("pulls", "open")] = [
        _pr_payload("K2Lab/agent-court", 42, "sha-new")
    ]
    summary = w.run_active_once()

    assert summary.new_tasks == 1  # sha-new 是新任务
    assert summary.dedupe_skipped == 0
    assert store.count() == 2
    assert store.get_by_dedupe_key("K2Lab/agent-court#42@sha-old") is not None
    assert store.get_by_dedupe_key("K2Lab/agent-court#42@sha-new") is not None


def test_active_polling_no_op_when_sha_unchanged():
    cfg = _cfg()
    store = StateStore(":memory:")
    client = _FakeGiteaClient(
        responses={
            ("pulls", "open"): [_pr_payload("K2Lab/agent-court", 42, "sha-same")],
            ("issues", "open"): [],
        }
    )
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)
    w.run_discovery_once()
    summary = w.run_active_once()
    assert summary.new_tasks == 0
    assert summary.dedupe_skipped == 1


def test_start_stop_lifecycle():
    """start() 起两个 thread, stop() 干净退出."""
    cfg = _cfg(poll_discovery=1, poll_active=1)  # 加速测试
    store = StateStore(":memory:")
    client = _FakeGiteaClient()
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    w.start()
    try:
        # 等一个周期, 让线程至少进一次循环
        time.sleep(1.5)
    finally:
        w.stop(timeout=3)

    # 两个线程都必须退出
    assert not w._discovery_thread.is_alive()
    assert not w._active_thread.is_alive()


def test_start_idempotent():
    """重复 start() 不报错, 也不重复起线程."""
    cfg = _cfg(poll_discovery=60, poll_active=30)
    store = StateStore(":memory:")
    client = _FakeGiteaClient()
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    w.start()
    t1 = w._discovery_thread
    w.start()
    t2 = w._discovery_thread
    try:
        assert t1 is t2
    finally:
        w.stop(timeout=3)


def test_discovery_search_params_target_watch_repos():
    """search_issues 应当被调 2 次 (pulls + issues), params 含 type/state/reviewer."""
    cfg = _cfg(watch=("K2Lab/agent-court", "K2Lab/moras-brain"))
    store = StateStore(":memory:")
    client = _FakeGiteaClient()
    w = PollingWorker(cfg=cfg, bot=_bot(), client=client, store=store)

    w.run_discovery_once()

    assert len(client.calls) == 2  # 一次 pulls, 一次 issues
    pull_call = next(c for c in client.calls if c.get("type") == "pulls")
    issue_call = next(c for c in client.calls if c.get("type") == "issues")
    assert pull_call["state"] == "open"
    assert issue_call["state"] == "open"
    # 用 reviewer (PR) 和 assignee (issue) 过滤; 具体参数名跟 Gitea API 对齐
    assert "reviewer" in pull_call or "review_requested" in pull_call
    assert "assignee" in issue_call or "assigned" in issue_call


def test_failed_discovery_does_not_crash_worker():
    """search_issues 抛错 → run_discovery_once 返回 summary.errors > 0, 不向上抛."""
    cfg = _cfg()
    store = StateStore(":memory:")

    class BrokenClient:
        def search_issues(self, params):
            raise RuntimeError("gitea 500")

    w = PollingWorker(cfg=cfg, bot=_bot(), client=BrokenClient(), store=store)
    summary = w.run_discovery_once()
    assert summary.errors >= 1
    assert summary.new_tasks == 0
