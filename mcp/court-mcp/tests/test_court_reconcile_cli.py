"""SY-3 (#18) MVP v1 旁挂: court-reconcile CLI 测试.

只测 main() 的 exit code + 输出格式. tmux 通过 monkey-patch 避开真调用.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import court_reconcile_cli as cli  # noqa: E402
import orchestrator as orch  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_tmux(monkeypatch):
    """默认让 tmux 返空 list, 个别 case 自己覆盖."""
    monkeypatch.setattr(orch.Orchestrator, "_collect_tmux_windows", lambda self: [])


def _seed_seen(court_root: Path, data: dict) -> None:
    sd = court_root / "gitea-watcher"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "seen-issues.json").write_text(json.dumps(data))


def test_clean_state_exits_zero(tmp_path, capsys):
    _seed_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DONE_DASHBOARD"},
    })
    rc = cli.main(["--court-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out
    assert "runs=1" in out


def test_warn_inconsistency_exits_one(tmp_path, capsys, monkeypatch):
    """retry queue 残留 DONE 条目 → warn-only → exit 1."""
    _seed_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DONE_DASHBOARD"},
    })
    (tmp_path / "gitea-watcher" / "retry-queue.json").write_text(json.dumps({
        "foo/bar#1": {"attempt": 1, "next_at": 0, "last_error": "old", "last_failed_at": 0},
    }))
    rc = cli.main(["--court-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "retry_stale_after_done" in out
    assert "[WARN]" in out


def test_error_inconsistency_exits_two(tmp_path, capsys, monkeypatch):
    """seen=DISPATCHED 但 tmux window 没起 → error → exit 2."""
    _seed_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1"},
    })
    # tmux 没 foo-bar-1
    rc = cli.main(["--court-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "dispatched_window_gone" in out
    assert "[ERROR]" in out


def test_quiet_clean_outputs_nothing(tmp_path, capsys):
    rc = cli.main(["--court-root", str(tmp_path), "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_json_output_is_parseable(tmp_path, capsys):
    _seed_seen(tmp_path, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1"},
    })
    rc = cli.main(["--court-root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 2  # dispatched_window_gone
    data = json.loads(out)
    assert "runs" in data and "inconsistencies" in data and "metrics" in data
    assert any(i["kind"] == "dispatched_window_gone" for i in data["inconsistencies"])
