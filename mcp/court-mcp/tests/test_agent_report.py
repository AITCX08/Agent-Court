"""Tests for agent_report (PR-20a)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from agent_report import (  # noqa: E402
    ReportResult,
    REPORT_CACHE_TTL_SEC,
    get_report,
    invalidate_report_cache,
)


def _mock_claude_ok(stdout: str) -> MagicMock:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = ""
    return cp


# ---------------------------------------------------------------------------
# dataclass shape
# ---------------------------------------------------------------------------


def test_report_result_dataclass_shape():
    r = ReportResult(
        team_id="agent-team-x",
        problem="P",
        investigation="I",
        solution="S",
        status="investigating",
        phase="requirements",
        updated_at="2026-05-25T00:00:00Z",
        source="file",
        captured_at=0.0,
    )
    assert r.source in ("file", "fallback", "missing")
    assert r.problem == "P"
    assert r.error is None


# ---------------------------------------------------------------------------
# file-source path
# ---------------------------------------------------------------------------


def test_get_report_reads_file_when_exists(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "agent-team-x.md").write_text(
        "---\n"
        "team_id: agent-team-x\n"
        "issue: K2Lab/foo#1\n"
        "status: investigating\n"
        "phase: requirements\n"
        "updated_at: 2026-05-25T08:00:00Z\n"
        "---\n\n"
        "# 问题描述\n\nproblem body line 1\nline 2\n\n"
        "# 调查情况\n\ninvest body\n\n"
        "# 解决方案\n\nsolution body\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()

    r = get_report("agent-team-x")
    assert r.source == "file"
    assert r.status == "investigating"
    assert r.phase == "requirements"
    assert "problem body line 1" in r.problem
    assert "line 2" in r.problem
    assert "invest body" in r.investigation
    assert "solution body" in r.solution
    assert r.updated_at == "2026-05-25T08:00:00Z"
    assert r.error is None


def test_get_report_missing_file_returns_missing_when_no_gatherer(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()

    r = get_report("agent-team-nonexistent")
    assert r.source == "missing"
    assert r.problem == "" and r.investigation == "" and r.solution == ""


def test_get_report_file_with_extra_sections_ignored(tmp_path, monkeypatch):
    """Extra non-standard ``# 备注`` sections shouldn't break parsing."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "agent-team-y.md").write_text(
        "---\nteam_id: agent-team-y\nstatus: done\nphase: done\n"
        "updated_at: 2026-05-25T09:00:00Z\n---\n\n"
        "# 问题描述\nP-only\n\n"
        "# 备注\nignored\n\n"
        "# 解决方案\nS-only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()

    r = get_report("agent-team-y")
    assert r.problem == "P-only"
    assert r.investigation == ""
    assert r.solution == "S-only"


# ---------------------------------------------------------------------------
# sonnet fallback path
# ---------------------------------------------------------------------------


def test_fallback_calls_claude_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()
    runner = MagicMock(return_value=_mock_claude_ok(
        "# 问题描述\nfallback prob\n\n# 调查情况\nfallback invest\n\n# 解决方案\nfallback sol\n"
    ))
    r = get_report(
        "agent-team-x",
        runner=runner,
        gather_context=lambda team_id: "pane content snapshot",
    )
    assert r.source == "fallback"
    assert "fallback prob" in r.problem
    assert "fallback invest" in r.investigation
    assert "fallback sol" in r.solution
    argv = runner.call_args.args[0]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--model" in argv
    assert "claude-sonnet-4-6" in argv
    assert "--bare" not in argv  # PR-20e: 不带 --bare 才能走 keychain OAuth


def test_fallback_returns_missing_when_context_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()
    runner = MagicMock()
    r = get_report(
        "agent-team-y",
        runner=runner,
        gather_context=lambda team_id: "",
    )
    assert r.source == "missing"
    runner.assert_not_called()


def test_fallback_claude_timeout_records_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()
    runner = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60))
    r = get_report(
        "agent-team-z",
        runner=runner,
        gather_context=lambda team_id: "ctx",
    )
    assert r.source == "fallback"
    assert r.error and "timeout" in r.error


def test_fallback_claude_not_found_records_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()
    runner = MagicMock(side_effect=FileNotFoundError("claude"))
    r = get_report(
        "agent-team-nf",
        runner=runner,
        gather_context=lambda team_id: "ctx",
    )
    assert r.source == "fallback"
    assert r.error and "PATH" in r.error


def test_fallback_claude_nonzero_exit_records_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 2
    cp.stdout = ""
    cp.stderr = "boom"
    runner = MagicMock(return_value=cp)
    r = get_report(
        "agent-team-nz",
        runner=runner,
        gather_context=lambda team_id: "ctx",
    )
    assert r.source == "fallback"
    assert r.error and "exit 2" in r.error


# ---------------------------------------------------------------------------
# cache behavior
# ---------------------------------------------------------------------------


def test_cache_returns_same_result_within_ttl(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "agent-team-c.md").write_text(
        "---\nteam_id: agent-team-c\nstatus: done\nphase: done\n"
        "updated_at: 2026-05-25T00:00:00Z\n---\n\n"
        "# 问题描述\nP\n\n# 调查情况\nI\n\n# 解决方案\nS\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()

    r1 = get_report("agent-team-c")
    # Delete the file — without cache this would now miss
    (reports_dir / "agent-team-c.md").unlink()
    r2 = get_report("agent-team-c")
    assert r1 is r2  # cached, returns same instance


def test_force_refresh_bypasses_cache(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    invalidate_report_cache()

    # 1st call: missing
    r1 = get_report("agent-team-f")
    assert r1.source == "missing"

    # Write file now
    (reports_dir / "agent-team-f.md").write_text(
        "---\nteam_id: agent-team-f\nstatus: done\nphase: done\nupdated_at: 2026-05-25T00:00:00Z\n---\n\n"
        "# 问题描述\nP\n\n# 调查情况\n\n\n# 解决方案\nS\n",
        encoding="utf-8",
    )

    # Without force, still cached missing
    r2 = get_report("agent-team-f")
    assert r2.source == "missing"

    # With force, re-reads
    r3 = get_report("agent-team-f", force_refresh=True)
    assert r3.source == "file"
    assert r3.problem == "P"


# ---------------------------------------------------------------------------
# default_gather_context
# ---------------------------------------------------------------------------


def test_default_gather_context_tmux_reads_pane(monkeypatch):
    import agent_report
    monkeypatch.setattr(agent_report, "capture_pane", lambda team, **kw: "pane content here")
    ctx = agent_report.default_gather_context("agent-team-foo")
    assert "pane content" in ctx


def test_default_gather_context_non_tmux_returns_empty():
    import agent_report
    # ghostty-style team id (not "agent-team-" prefix)
    ctx = agent_report.default_gather_context("ghostty:1234")
    assert ctx == ""


def test_default_gather_context_tmux_error_returns_empty(monkeypatch):
    import agent_report

    def boom(*a, **kw):
        raise agent_report.TmuxPaneError("no such session")

    monkeypatch.setattr(agent_report, "capture_pane", boom)
    ctx = agent_report.default_gather_context("agent-team-missing")
    assert ctx == ""
