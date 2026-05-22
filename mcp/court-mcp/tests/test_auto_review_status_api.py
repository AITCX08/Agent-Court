"""Tests for auto_review.status_api — build status map for frontend badges."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.state import StateStore, TaskKind, TaskState
from auto_review.status_api import build_status_map


def test_build_status_map_empty_store():
    store = StateStore(":memory:")
    assert build_status_map(store) == {}


def test_build_status_map_returns_per_repo_number():
    store = StateStore(":memory:")
    store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="s1")
    store.enqueue(kind=TaskKind.ISSUE, repo="K2Lab/a", number=2, head_sha=None)

    m = build_status_map(store)

    assert set(m.keys()) == {"K2Lab/a#1", "K2Lab/a#2"}
    assert m["K2Lab/a#1"]["state"] == "discovered"
    assert m["K2Lab/a#1"]["kind"] == "pr"
    assert m["K2Lab/a#1"]["head_sha"] == "s1"
    assert m["K2Lab/a#1"]["runtime"] is None
    assert "last_event_at" in m["K2Lab/a#1"]
    assert m["K2Lab/a#2"]["kind"] == "issue"
    assert m["K2Lab/a#2"]["head_sha"] is None


def test_build_status_map_dedupes_keeps_most_recent_event():
    """同 PR 多 head_sha task → 返 last_event_at 最新那个."""
    store = StateStore(":memory:")
    store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="old")
    time.sleep(1.1)  # 确保 last_event_at 秒级差异 (state.py 用 ISO seconds 精度)
    t2 = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="new")
    store.update_state(t2.id, TaskState.POSTED, runtime="codex")

    m = build_status_map(store)
    assert m["K2Lab/a#1"]["head_sha"] == "new"
    assert m["K2Lab/a#1"]["state"] == "posted"
    assert m["K2Lab/a#1"]["runtime"] == "codex"


def test_build_status_map_includes_error_message_for_failed():
    store = StateStore(":memory:")
    t = store.enqueue(kind=TaskKind.PR, repo="K2Lab/a", number=1, head_sha="s")
    store.update_state(t.id, TaskState.FAILED, error_message="codex timed out", runtime="codex")

    m = build_status_map(store)
    assert m["K2Lab/a#1"]["state"] == "failed"
    assert m["K2Lab/a#1"]["error_message"] == "codex timed out"
