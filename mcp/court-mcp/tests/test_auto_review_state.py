"""Tests for auto_review.state — SQLite-backed task state machine."""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.state import (
    TaskState,
    TaskKind,
    AutoReviewTask,
    StateStore,
    DedupeSkipped,
)


@pytest.fixture
def store():
    """In-memory SQLite store, fresh schema per test."""
    return StateStore(":memory:")


def test_initial_schema_created(store):
    """构造时应当自动建表 + 索引."""
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='auto_review_tasks'"
    ).fetchall()
    assert len(rows) == 1


def test_enqueue_new_pr_returns_task(store):
    task = store.enqueue(
        kind=TaskKind.PR,
        repo="K2Lab/agent-court",
        number=42,
        head_sha="abc123",
    )
    assert isinstance(task, AutoReviewTask)
    assert task.id is not None
    assert task.dedupe_key == "K2Lab/agent-court#42@abc123"
    assert task.state == TaskState.DISCOVERED
    assert task.kind == TaskKind.PR
    assert task.repo == "K2Lab/agent-court"
    assert task.number == 42
    assert task.head_sha == "abc123"


def test_enqueue_new_issue_dedupe_key_excludes_sha(store):
    task = store.enqueue(
        kind=TaskKind.ISSUE,
        repo="K2Lab/agent-court",
        number=7,
        head_sha=None,
    )
    assert task.dedupe_key == "K2Lab/agent-court#7"
    assert task.head_sha is None
    assert task.kind == TaskKind.ISSUE


def test_enqueue_same_dedupe_key_raises(store):
    """同 dedupe_key 第二次 enqueue 抛 DedupeSkipped, 不创建新行."""
    store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")

    with pytest.raises(DedupeSkipped, match="K2Lab/a#1@sha1"):
        store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")

    assert store.count() == 1  # 没多一行


def test_enqueue_pr_with_new_sha_creates_new_task(store):
    """PR 推了新 commit (head_sha 变), 是新任务."""
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    t2 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha2")
    assert t1.id != t2.id
    assert t1.dedupe_key != t2.dedupe_key
    assert store.count() == 2


def test_pr_requires_head_sha(store):
    """PR 必须带 head_sha, 否则报错 (不能用 None dedupe_key)."""
    with pytest.raises(ValueError, match="head_sha"):
        store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha=None)


def test_issue_must_not_have_head_sha(store):
    """Issue 不允许传 head_sha (语义上 issue 没有 head)."""
    with pytest.raises(ValueError, match="head_sha"):
        store.enqueue(
            kind=TaskKind.ISSUE, repo="K2Lab/a", number=1, head_sha="something"
        )


def test_get_by_dedupe_key(store):
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    found = store.get_by_dedupe_key("K2Lab/a#1@sha1")
    assert found is not None
    assert found.id == t1.id


def test_get_by_dedupe_key_missing_returns_none(store):
    assert store.get_by_dedupe_key("nope#0@x") is None


def test_update_state_transition(store):
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    store.update_state(t1.id, TaskState.QUEUED)
    refreshed = store.get_by_dedupe_key(t1.dedupe_key)
    assert refreshed.state == TaskState.QUEUED
    assert refreshed.last_event_at >= t1.last_event_at


def test_update_state_with_error_message(store):
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    store.update_state(t1.id, TaskState.FAILED, error_message="timeout calling gitea")
    refreshed = store.get_by_dedupe_key(t1.dedupe_key)
    assert refreshed.state == TaskState.FAILED
    assert refreshed.error_message == "timeout calling gitea"


def test_list_by_state(store):
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    t2 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=2, head_sha="sha2")
    store.enqueue(kind=TaskKind.ISSUE, repo="K2Lab/a", number=3, head_sha=None)
    store.update_state(t2.id, TaskState.QUEUED)

    discovered = store.list_by_state(TaskState.DISCOVERED)
    assert len(discovered) == 2  # t1 + the issue
    queued = store.list_by_state(TaskState.QUEUED)
    assert len(queued) == 1
    assert queued[0].id == t2.id


def test_known_pr_numbers_for_active_polling(store):
    """active 30s polling 需要列出"已知但 head_sha 可能变了"的 PR."""
    store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=2, head_sha="sha2")
    store.enqueue(kind=TaskKind.ISSUE, repo="K2Lab/a", number=3, head_sha=None)

    prs = store.known_pr_keys()
    assert sorted(prs) == [("K2Lab/a", 1), ("K2Lab/a", 2)]


def test_persistent_storage_round_trip(tmp_path):
    """非 :memory: 模式, 关掉再重开应当看到数据."""
    db_path = tmp_path / "state.sqlite3"
    s1 = StateStore(str(db_path))
    s1.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="sha1")
    s1.close()

    s2 = StateStore(str(db_path))
    found = s2.get_by_dedupe_key("K2Lab/a#1@sha1")
    assert found is not None
    s2.close()


def test_task_state_enum_values():
    """状态枚举值固定字符串, 跟 KAXY-3022/Agent-manager 对齐."""
    assert TaskState.DISCOVERED.value == "discovered"
    assert TaskState.QUEUED.value == "queued"
    assert TaskState.RUNNING.value == "running"
    assert TaskState.REVIEW_DONE.value == "review_done"
    assert TaskState.POSTED.value == "posted"
    assert TaskState.FAILED.value == "failed"
    assert TaskState.DEDUPE_SKIPPED.value == "dedupe_skipped"


def test_task_kind_enum_values():
    assert TaskKind.PR.value == "pr"
    assert TaskKind.ISSUE.value == "issue"


def test_find_active_task_returns_none_when_idle(store):
    """没有任何 task 时返回 None."""
    assert store.find_active_task("K2Lab/a", 1) is None


def test_find_active_task_finds_in_flight(store):
    """同 PR 不同 head_sha, 第一个 RUNNING — find_active_task 返活的."""
    t1 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="s1")
    store.update_state(t1.id, TaskState.RUNNING)
    t2 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="s2")
    found = store.find_active_task("K2Lab/a", 1)
    assert found is not None
    # SQL only includes QUEUED/RUNNING/REVIEW_DONE — t1 (RUNNING) matches,
    # t2 (DISCOVERED) does not
    assert found.id == t1.id
    assert found.state == TaskState.RUNNING


def test_find_active_task_ignores_terminal_states(store):
    """POSTED / FAILED / DEDUPE_SKIPPED 不算 active."""
    t = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="s")
    store.update_state(t.id, TaskState.POSTED)
    assert store.find_active_task("K2Lab/a", 1) is None

    t2 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=2, head_sha="s")
    store.update_state(t2.id, TaskState.FAILED, error_message="x")
    assert store.find_active_task("K2Lab/a", 2) is None

    t3 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=3, head_sha="s")
    store.update_state(t3.id, TaskState.DEDUPE_SKIPPED)
    assert store.find_active_task("K2Lab/a", 3) is None
