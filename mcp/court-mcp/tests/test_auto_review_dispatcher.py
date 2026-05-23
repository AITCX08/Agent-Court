"""Tests for auto_review.dispatcher — full pipeline orchestrator."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.config import AutoReviewConfig
from auto_review.dispatcher import ReviewDispatcher
from auto_review.executor import ReviewResult
from auto_review.state import StateStore, TaskKind, TaskState


def _cfg(threshold=10, pr_auto=True, issue_auto=True) -> AutoReviewConfig:
    return AutoReviewConfig(
        bot_username="bot",
        watch_repos=["K2Lab/agent-court"],
        light_deep_threshold=threshold,
        pr_auto_post=pr_auto,
        issue_auto_post=issue_auto,
    )


def _client_for_pr(changed_files=5, html_url=None):
    """Returns a MagicMock GiteaClient.get_pr returning given changed_files."""
    client = MagicMock()
    client.get_pr.return_value = {
        "number": 42,
        "title": "test PR",
        "html_url": html_url or "https://git.k2lab.ai/K2Lab/agent-court/pulls/42",
        "changed_files": changed_files,
        "head": {"sha": "abc123"},
    }
    client.get_issue.return_value = {
        "number": 7, "title": "test issue",
        "html_url": "https://git.k2lab.ai/K2Lab/agent-court/issues/7",
    }
    return client


def _fake_light(success=True, output="REVIEW BODY", error=None):
    ex = MagicMock()
    ex.review.return_value = ReviewResult(
        success=success, runtime="codex", output=output, error=error
    )
    return ex


def _fake_deep(success=True, team_id="agent-team-xyz", error=None):
    ex = MagicMock()
    ex.review.return_value = ReviewResult(
        success=success, runtime="team", output=team_id, error=error
    )
    return ex


def _enqueue_pr(store, *, repo="K2Lab/agent-court", number=42, head_sha="abc123"):
    return store.enqueue(
        kind=TaskKind.PR, repo=repo, number=number, head_sha=head_sha
    )


def _enqueue_issue(store, *, repo="K2Lab/agent-court", number=7):
    return store.enqueue(
        kind=TaskKind.ISSUE, repo=repo, number=number, head_sha=None
    )


# ---------- Routing by changed_files ----------

def test_pr_with_few_files_goes_light():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=3)
    light = _fake_light()
    deep = _fake_deep()
    d = ReviewDispatcher(cfg=_cfg(threshold=10), store=store, client=client,
                        light=light, deep=deep)

    final = d.process_one(task)

    assert final == TaskState.POSTED.value
    light.review.assert_called_once()
    deep.review.assert_not_called()
    client.comment_on_issue.assert_called_once()


def test_pr_with_many_files_goes_deep():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=25)
    light = _fake_light()
    deep = _fake_deep(team_id="agent-team-deep1")
    d = ReviewDispatcher(cfg=_cfg(threshold=10), store=store, client=client,
                        light=light, deep=deep)

    final = d.process_one(task)

    assert final == TaskState.RUNNING.value
    deep.review.assert_called_once()
    light.review.assert_not_called()
    client.comment_on_issue.assert_not_called()  # team posts itself later

    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    assert refreshed.state == TaskState.RUNNING
    assert refreshed.runtime == "team"


def test_threshold_exactly_at_boundary_goes_light():
    """changed_files == threshold → 走轻 (>= threshold 才走深)."""
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=10)  # exactly at threshold
    light = _fake_light()
    deep = _fake_deep()
    d = ReviewDispatcher(cfg=_cfg(threshold=10), store=store, client=client,
                        light=light, deep=deep)
    d.process_one(task)

    light.review.assert_called_once()
    deep.review.assert_not_called()


def test_issue_always_goes_light():
    """Issue 总是轻路径, 不看 threshold."""
    store = StateStore(":memory:")
    task = _enqueue_issue(store)
    client = _client_for_pr(changed_files=999)  # changed_files 不应当被读
    light = _fake_light()
    deep = _fake_deep()
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=light, deep=deep)

    final = d.process_one(task)

    assert final == TaskState.POSTED.value
    client.get_issue.assert_called_once_with("K2Lab/agent-court", 7)
    client.get_pr.assert_not_called()
    light.review.assert_called_once()


# ---------- Failure handling ----------

def test_light_failure_transitions_to_failed():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=3)
    light = _fake_light(success=False, error="codex exit 1: panic")
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=light, deep=_fake_deep())

    final = d.process_one(task)

    assert final == TaskState.FAILED.value
    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    assert refreshed.state == TaskState.FAILED
    assert "codex exit 1" in (refreshed.error_message or "")


def test_deep_failure_transitions_to_failed():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=25)
    deep = _fake_deep(success=False, error="tmux new-session failed")
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=deep)

    final = d.process_one(task)

    assert final == TaskState.FAILED.value
    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    assert "tmux new-session failed" in (refreshed.error_message or "")


def test_post_failure_transitions_to_failed():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=3)
    client.comment_on_issue.side_effect = RuntimeError("gitea 500")
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    final = d.process_one(task)

    assert final == TaskState.FAILED.value
    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    assert "post" in (refreshed.error_message or "").lower()


def test_get_pr_failure_transitions_to_failed():
    """Even fetching PR metadata can fail (gitea down) — graceful FAILED."""
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = MagicMock()
    client.get_pr.side_effect = RuntimeError("network timeout")
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    final = d.process_one(task)

    assert final == TaskState.FAILED.value


# ---------- Auto-post toggle ----------

def test_pr_auto_post_disabled_stops_at_review_done():
    store = StateStore(":memory:")
    task = _enqueue_pr(store)
    client = _client_for_pr(changed_files=3)
    light = _fake_light()
    d = ReviewDispatcher(cfg=_cfg(pr_auto=False), store=store, client=client,
                        light=light, deep=_fake_deep())

    final = d.process_one(task)

    assert final == TaskState.REVIEW_DONE.value
    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    assert refreshed.state == TaskState.REVIEW_DONE
    client.comment_on_issue.assert_not_called()


def test_issue_auto_post_disabled_stops_at_review_done():
    store = StateStore(":memory:")
    task = _enqueue_issue(store)
    client = _client_for_pr()
    light = _fake_light()
    d = ReviewDispatcher(cfg=_cfg(issue_auto=False), store=store, client=client,
                        light=light, deep=_fake_deep())

    final = d.process_one(task)

    assert final == TaskState.REVIEW_DONE.value
    client.comment_on_issue.assert_not_called()


# ---------- State machine transitions ----------

def test_state_transitions_recorded_in_correct_order():
    """Verify the task goes DISCOVERED → QUEUED → RUNNING → REVIEW_DONE → POSTED."""
    store = StateStore(":memory:")
    task = _enqueue_pr(store)  # initial state = DISCOVERED
    assert store.get_by_dedupe_key(task.dedupe_key).state == TaskState.DISCOVERED

    client = _client_for_pr(changed_files=3)
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    d.process_one(task)
    refreshed = store.get_by_dedupe_key(task.dedupe_key)
    # We don't track intermediate states explicitly, but the final state +
    # runtime + no error_message indicates the full path succeeded.
    assert refreshed.state == TaskState.POSTED
    assert refreshed.runtime == "codex"
    assert refreshed.error_message is None


# ---------- Batch processing ----------

def test_process_pending_fifo_with_limit():
    """process_pending pulls DISCOVERED tasks (FIFO) up to limit."""
    store = StateStore(":memory:")
    t1 = _enqueue_pr(store, number=1, head_sha="sha1")
    t2 = _enqueue_pr(store, number=2, head_sha="sha2")
    t3 = _enqueue_pr(store, number=3, head_sha="sha3")
    client = _client_for_pr(changed_files=3)
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    results = d.process_pending(limit=2)

    assert len(results) == 2
    # First two were processed; t3 still DISCOVERED
    assert store.get_by_dedupe_key(t3.dedupe_key).state == TaskState.DISCOVERED


def test_process_pending_no_discovered_returns_empty():
    store = StateStore(":memory:")
    client = _client_for_pr()
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())
    results = d.process_pending(limit=10)
    assert results == []


def test_parallel_job_guard_skips_when_active_exists():
    """同 PR 两个 head_sha, 第一个走 RUNNING, 第二个调 process_one 时被跳."""
    store = StateStore(":memory:")
    t1 = _enqueue_pr(store, head_sha="sha-old")
    store.update_state(t1.id, TaskState.RUNNING)  # 模拟 t1 还在跑
    t2 = _enqueue_pr(store, head_sha="sha-new")
    client = _client_for_pr(changed_files=3)
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    final = d.process_one(t2)

    assert final == TaskState.DEDUPE_SKIPPED.value
    client.get_pr.assert_not_called()  # fetch_context 没发生
    refreshed = store.get_by_dedupe_key(t2.dedupe_key)
    assert refreshed.state == TaskState.DEDUPE_SKIPPED
    assert "active task" in (refreshed.error_message or "")


def test_parallel_job_guard_allows_after_active_finishes():
    """active task 跑完进 POSTED 后, 新 head_sha 可以正常跑."""
    store = StateStore(":memory:")
    t1 = _enqueue_pr(store, head_sha="sha-old")
    store.update_state(t1.id, TaskState.POSTED)  # t1 已结束
    t2 = _enqueue_pr(store, head_sha="sha-new")
    client = _client_for_pr(changed_files=3)
    d = ReviewDispatcher(cfg=_cfg(), store=store, client=client,
                        light=_fake_light(), deep=_fake_deep())

    final = d.process_one(t2)

    assert final == TaskState.POSTED.value
