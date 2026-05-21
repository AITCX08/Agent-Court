"""SY-3 (#18): Orchestrator MVP v1 tests.

测试只走文件 (不 mock tmux 真调用 → 注入 tmux_windows fixture); reconcile
所有 4 类不一致都有 case.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator as orch  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: 在 tmp_path 下造一个迷你 state_dir
# ---------------------------------------------------------------------------

def _state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "gitea-watcher"
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def _write_seen(tmp_path: Path, data: dict):
    sd = _state_dir(tmp_path)
    (sd / "seen-issues.json").write_text(json.dumps(data))


def _write_pending_approval(tmp_path: Path, slug: str, meta: dict, *, has_result: bool = False):
    sd = _state_dir(tmp_path) / "pending-approval"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{slug}.json").write_text(json.dumps(meta))
    if has_result:
        (sd / f"{slug}.result").write_text("{}")


def _write_retry_queue(tmp_path: Path, data: dict):
    sd = _state_dir(tmp_path)
    (sd / "retry-queue.json").write_text(json.dumps(data))


class _Orchestrator(orch.Orchestrator):
    """注入 tmux_windows, 避免依赖真 tmux."""

    def __init__(self, court_root: Path, *, tmux_windows: list[str] | None = None, **kw):
        super().__init__(court_root=court_root, **kw)
        self._injected_windows = tmux_windows or []

    def _collect_tmux_windows(self) -> list[str]:
        return list(self._injected_windows)


# ---------------------------------------------------------------------------
# snapshot: 基础 join 行为
# ---------------------------------------------------------------------------

def test_snapshot_with_only_seen_returns_runs_with_states_derived(tmp_path):
    _write_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "stage": "INTAKE",
                      "tmux_window": "foo-bar-1", "dispatched_at": "2026-05-20T00:00:00Z"},
        "x/y#9": {"last_action": "DONE_DASHBOARD", "stage": "DONE"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW, "foo-bar-1"])
    snap = o.snapshot()
    runs = {r.issue_key: r for r in snap.runs}
    assert len(runs) == 2
    r1 = runs["foo/bar#1"]
    assert r1.state == orch.RunState.DISPATCHED
    assert r1.tmux_window == "foo-bar-1"
    assert r1.tmux_window_alive is True
    r2 = runs["x/y#9"]
    assert r2.state == orch.RunState.DONE
    assert r2.tmux_window_alive is False


def test_snapshot_picks_pending_approval_state_when_no_seen_entry(tmp_path):
    """pending-approval/*.json 在但 seen 没记录 → 应该出现在 runs 列表 (state=QUEUED)."""
    _write_pending_approval(tmp_path, "demo-1-intake", {
        "repo": "demo/x", "number": 1, "stage": "INTAKE", "slug_id": "demo-1-intake",
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    snap = o.snapshot()
    assert any(r.issue_key == "demo/x#1" and r.has_pending_approval for r in snap.runs)


def test_snapshot_pending_already_resulted_not_counted_as_pending(tmp_path):
    """同 slug 有 .result → 已审批, 不算 pending."""
    _write_seen(tmp_path, {
        "demo/x#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "demo-x-1"},
    })
    _write_pending_approval(tmp_path, "demo-1-intake", {
        "repo": "demo/x", "number": 1, "stage": "INTAKE",
    }, has_result=True)
    o = _Orchestrator(tmp_path, tmux_windows=["demo-x-1"])
    snap = o.snapshot()
    r = next(r for r in snap.runs if r.issue_key == "demo/x#1")
    assert r.has_pending_approval is False
    assert r.state == orch.RunState.DISPATCHED


def test_snapshot_includes_retry_queue_attempt(tmp_path):
    _write_seen(tmp_path, {
        "demo/x#1": {"last_action": "SPAWN_FAILED", "stage": "INTAKE"},
    })
    _write_retry_queue(tmp_path, {
        "demo/x#1": {"attempt": 2, "next_at": 0, "last_error": "boom", "last_failed_at": 0},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    r = o.get_run("demo/x#1")
    assert r is not None
    assert r.state == orch.RunState.FAILED
    assert r.in_retry_queue is True
    assert r.retry_attempt == 2


# ---------------------------------------------------------------------------
# reconcile: 4 类不一致
# ---------------------------------------------------------------------------

def test_reconcile_detects_dispatched_window_gone(tmp_path):
    """I-1: seen=DISPATCHED 但 tmux 没对应 window → 错误."""
    _write_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW])  # foo-bar-1 缺
    inc = o.reconcile()
    assert len(inc) == 1
    assert inc[0].kind == "dispatched_window_gone"
    assert inc[0].severity == orch.SEVERITY_ERROR
    assert inc[0].issue_key == "foo/bar#1"


def test_reconcile_detects_executing_window_gone(tmp_path):
    """I-2: seen=EXECUTING 但 tmux 没 window → 错误."""
    _write_seen(tmp_path, {
        "foo/bar#1": {"last_action": "EXECUTING", "tmux_window": "foo-bar-1"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    inc = o.reconcile()
    kinds = {i.kind for i in inc}
    assert "executing_window_gone" in kinds


def test_reconcile_detects_retry_stale_after_done(tmp_path):
    """I-3: seen 已 DONE 但 retry queue 还有条 → 警告 (应该 remove)."""
    _write_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DONE_DASHBOARD"},
    })
    _write_retry_queue(tmp_path, {
        "foo/bar#1": {"attempt": 1, "next_at": 0, "last_error": "old", "last_failed_at": 0},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    inc = o.reconcile()
    assert any(i.kind == "retry_stale_after_done" for i in inc)


def test_reconcile_detects_pending_after_done(tmp_path):
    """I-4: seen 已 DONE 但 pending-approval/*.json 还在 → 警告."""
    _write_seen(tmp_path, {
        "demo/x#1": {"last_action": "DONE_DASHBOARD"},
    })
    _write_pending_approval(tmp_path, "demo-1-intake", {
        "repo": "demo/x", "number": 1, "stage": "INTAKE",
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    inc = o.reconcile()
    assert any(i.kind == "pending_after_done" for i in inc)


def test_reconcile_detects_tmux_window_orphan(tmp_path):
    """I-5: tmux window 在但 seen 没对应 → 警告."""
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW, "manual-test-1"])
    inc = o.reconcile()
    assert len(inc) == 1
    assert inc[0].kind == "tmux_window_orphan"
    assert "manual-test-1" in inc[0].detail


def test_reconcile_clean_state_returns_empty(tmp_path):
    """完全一致的 state → 没 inconsistencies."""
    _write_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1",
                      "dispatched_at": "2026-05-20T00:00:00Z"},
        "x/y#2": {"last_action": "DONE_DASHBOARD"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW, "foo-bar-1"])
    inc = o.reconcile()
    assert inc == []


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_metrics_counts_states_correctly(tmp_path):
    _write_seen(tmp_path, {
        "a/b#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "a-b-1"},
        "a/b#2": {"last_action": "EXECUTING", "tmux_window": "a-b-2"},
        "a/b#3": {"last_action": "DONE_DASHBOARD"},
        "a/b#4": {"last_action": "SPAWN_FAILED"},
        "a/b#5": {"last_action": "REJECTED_DASHBOARD"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW, "a-b-1", "a-b-2"])
    m = o.get_metrics()
    assert m["total"] == 5
    assert m["dispatched"] == 1
    assert m["executing"] == 1
    assert m["done"] == 1
    assert m["failed"] == 1
    assert m["rejected"] == 1
    assert m["active"] == 2  # dispatched + executing
    # 完全一致 → 0 inconsistency
    assert m["inconsistencies"] == 0


def test_metrics_counts_orphan_and_inconsistencies(tmp_path):
    _write_seen(tmp_path, {
        "a/b#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "a-b-1"},  # window 缺
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW, "manual-orphan"])
    m = o.get_metrics()
    assert m["orphan_tmux_windows"] == 1
    assert m["inconsistencies_error"] == 1  # dispatched_window_gone
    assert m["inconsistencies_warn"] == 1   # orphan_tmux_window


# ---------------------------------------------------------------------------
# 容错路径
# ---------------------------------------------------------------------------

def test_corrupt_seen_json_treated_as_empty(tmp_path):
    sd = _state_dir(tmp_path)
    (sd / "seen-issues.json").write_text("not json")
    o = _Orchestrator(tmp_path, tmux_windows=[])
    snap = o.snapshot()
    assert snap.runs == []


def test_corrupt_retry_queue_json_ignored(tmp_path):
    _write_seen(tmp_path, {"a/b#1": {"last_action": "SPAWN_FAILED"}})
    sd = _state_dir(tmp_path)
    (sd / "retry-queue.json").write_text("garbage")
    o = _Orchestrator(tmp_path, tmux_windows=[])
    r = o.get_run("a/b#1")
    assert r is not None
    assert r.in_retry_queue is False


def test_missing_state_dir_returns_empty_snapshot(tmp_path):
    o = _Orchestrator(tmp_path / "nope", tmux_windows=[])
    snap = o.snapshot()
    assert snap.runs == []
    assert snap.inconsistencies == []
    assert snap.metrics["total"] == 0


def test_invalid_issue_key_in_seen_skipped(tmp_path):
    """seen 里有非 'repo#num' 格式的 key → silently skip."""
    _write_seen(tmp_path, {
        "garbage-no-hash": {"last_action": "DONE_DASHBOARD"},
        "valid/repo#1": {"last_action": "DONE_DASHBOARD"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[])
    snap = o.snapshot()
    keys = {r.issue_key for r in snap.runs}
    assert keys == {"valid/repo#1"}


# ---------------------------------------------------------------------------
# 输出格式 (to_dict)
# ---------------------------------------------------------------------------

def test_snapshot_to_dict_is_json_serializable(tmp_path):
    _write_seen(tmp_path, {
        "a/b#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "a-b-1"},
    })
    o = _Orchestrator(tmp_path, tmux_windows=[orch.WATCHER_WINDOW])  # 故意没 a-b-1
    snap = o.snapshot()
    blob = json.dumps(snap.to_dict(), ensure_ascii=False)  # 不抛即成功
    data = json.loads(blob)
    assert isinstance(data["runs"], list)
    assert isinstance(data["inconsistencies"], list)
    assert isinstance(data["metrics"], dict)
    assert "dispatched_window_gone" in {i["kind"] for i in data["inconsistencies"]}
