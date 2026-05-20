"""SY-4 (#17): RetryQueue tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retry_queue import (  # noqa: E402
    DeadLetter,
    RetryItem,
    RetryQueue,
)


class FakeClock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_queue(tmp_path: Path, **overrides):
    clock = overrides.pop("clock", FakeClock())
    return RetryQueue(
        state_dir=tmp_path,
        max_attempts=overrides.pop("max_attempts", 3),
        base_backoff_seconds=overrides.pop("base_backoff_seconds", 60),
        now=clock,
        **overrides,
    ), clock


def test_first_failure_schedules_at_base_delay(tmp_path):
    q, clock = make_queue(tmp_path)
    item = q.push("foo/bar#1", "boom")
    assert isinstance(item, RetryItem)
    assert item.attempt == 1
    assert item.next_at == clock.t + 60   # base * 2**0
    assert item.last_error == "boom"


def test_second_failure_doubles_delay(tmp_path):
    q, clock = make_queue(tmp_path)
    q.push("foo/bar#1", "boom")
    clock.t += 100  # 跳过第一次 backoff
    item = q.push("foo/bar#1", "boom2")
    assert isinstance(item, RetryItem)
    assert item.attempt == 2
    assert item.next_at == clock.t + 120  # base * 2**1


def test_third_failure_quadruples_delay(tmp_path):
    q, clock = make_queue(tmp_path)
    q.push("foo/bar#1", "boom")
    clock.t += 100
    q.push("foo/bar#1", "boom2")
    clock.t += 200
    item = q.push("foo/bar#1", "boom3")
    assert isinstance(item, RetryItem)
    assert item.attempt == 3
    assert item.next_at == clock.t + 240  # base * 2**2


def test_fourth_failure_returns_dead_letter_and_removes_from_queue(tmp_path):
    q, clock = make_queue(tmp_path, max_attempts=3)
    q.push("foo/bar#1", "e1")
    q.push("foo/bar#1", "e2")
    q.push("foo/bar#1", "e3")
    result = q.push("foo/bar#1", "e4")
    assert isinstance(result, DeadLetter)
    assert result.issue_key == "foo/bar#1"
    assert result.attempt == 3   # 之前已失败 3 次
    assert result.last_error == "e4"
    # dead letter 已从队列移除
    assert len(q) == 0


def test_pop_due_returns_only_due_items_and_removes_them(tmp_path):
    q, clock = make_queue(tmp_path)
    q.push("repo#1", "e")           # next_at = 1060
    clock.t += 10
    q.push("repo#2", "e")           # next_at = 1070
    # now=1060, only #1 due
    due = q.pop_due(now=1060)
    assert due == ["repo#1"]
    assert len(q) == 1
    # #2 still queued
    assert q.snapshot()[0].issue_key == "repo#2"


def test_pop_due_empty_when_nothing_due(tmp_path):
    q, clock = make_queue(tmp_path)
    q.push("repo#1", "e")  # next_at = 1060
    assert q.pop_due(now=1059) == []
    assert len(q) == 1


def test_remove_clears_entry(tmp_path):
    q, _ = make_queue(tmp_path)
    q.push("repo#1", "e")
    assert q.remove("repo#1") is True
    assert q.remove("repo#1") is False  # 已不在
    assert len(q) == 0


def test_snapshot_sorted_by_next_at(tmp_path):
    q, clock = make_queue(tmp_path)
    q.push("repo#1", "e")            # next_at 1060
    clock.t += 5
    q.push("repo#2", "e")            # next_at 1065
    q.push("repo#3", "e")            # next_at 1065 (same)
    items = q.snapshot()
    assert [it.issue_key for it in items] == ["repo#1", "repo#2", "repo#3"]


def test_invalid_max_attempts_raises():
    with pytest.raises(ValueError, match="max_attempts"):
        RetryQueue(state_dir=Path("/tmp"), max_attempts=0)


def test_invalid_backoff_raises():
    with pytest.raises(ValueError, match="base_backoff_seconds"):
        RetryQueue(state_dir=Path("/tmp"), base_backoff_seconds=0)


def test_persistence_survives_new_instance(tmp_path):
    q1, clock = make_queue(tmp_path)
    q1.push("repo#1", "boom")
    q2 = RetryQueue(state_dir=tmp_path, now=clock)
    assert [it.issue_key for it in q2.snapshot()] == ["repo#1"]


def test_corrupt_json_treated_as_empty(tmp_path):
    (tmp_path / "retry-queue.json").write_text("not json")
    q, _ = make_queue(tmp_path)
    assert q.snapshot() == []
    # 重新 push 后文件被覆盖成合法 json
    q.push("repo#1", "e")
    data = json.loads((tmp_path / "retry-queue.json").read_text())
    assert "repo#1" in data


def test_atomic_write_no_partial_file_visible(tmp_path):
    """tempfile + os.replace 应保证 reader 看不到半写文件 (这里只校 reader 行为)."""
    q, _ = make_queue(tmp_path)
    q.push("repo#1", "e")
    q.push("repo#2", "e")
    # 直接 read 应得到完整 JSON
    text = (tmp_path / "retry-queue.json").read_text()
    data = json.loads(text)
    assert set(data.keys()) == {"repo#1", "repo#2"}
