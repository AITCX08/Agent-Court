"""Tests for issue_report_writer (PR-20d)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "issue_report_writer", *args],
        cwd=HERE,
        env={**dict(env), "PYTHONPATH": str(HERE)},
        capture_output=True,
        text=True,
    )


def test_requirements_phase_writes_problem_section(tmp_path):
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    cp = _run_cli([
        "checkpoint",
        "--team-id", "agent-team-w1",
        "--issue", "K2Lab/foo#1",
        "--phase", "requirements",
        "--status", "investigating",
        "--problem", "issue says X is broken, repro steps Y",
    ], env=env)
    assert cp.returncode == 0, cp.stderr
    out = (tmp_path / "reports" / "agent-team-w1.md").read_text(encoding="utf-8")
    assert "team_id: agent-team-w1" in out
    assert "phase: requirements" in out
    assert "status: investigating" in out
    assert "# 问题描述" in out
    assert "X is broken" in out


def test_four_checkpoints_accumulate(tmp_path):
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    # (a) requirements
    cp1 = _run_cli([
        "checkpoint", "--team-id", "agent-team-w2", "--issue", "K2Lab/x#9",
        "--phase", "requirements", "--status", "investigating",
        "--problem", "P1",
    ], env=env)
    assert cp1.returncode == 0, cp1.stderr
    # (b) plan
    cp2 = _run_cli([
        "checkpoint", "--team-id", "agent-team-w2", "--issue", "K2Lab/x#9",
        "--phase", "plan", "--status", "planning",
        "--solution", "S1",
    ], env=env)
    assert cp2.returncode == 0, cp2.stderr
    # (c) execution
    cp3 = _run_cli([
        "checkpoint", "--team-id", "agent-team-w2", "--issue", "K2Lab/x#9",
        "--phase", "execution", "--status", "executing",
        "--investigation", "I1",
        "--solution", "S2-updated",
    ], env=env)
    assert cp3.returncode == 0, cp3.stderr
    # (d) verification
    cp4 = _run_cli([
        "checkpoint", "--team-id", "agent-team-w2", "--issue", "K2Lab/x#9",
        "--phase", "verification", "--status", "done",
    ], env=env)
    assert cp4.returncode == 0, cp4.stderr

    text = (tmp_path / "reports" / "agent-team-w2.md").read_text(encoding="utf-8")
    # All 3 sections survived
    assert "P1" in text  # from (a)
    assert "I1" in text  # from (c)
    assert "S2-updated" in text  # from (c), overwrote (b)
    assert "S1" not in text  # overwritten
    # Final frontmatter reflects last checkpoint
    assert "phase: verification" in text
    assert "status: done" in text


def test_invalid_phase_returns_nonzero(tmp_path):
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    cp = _run_cli([
        "checkpoint", "--team-id", "x", "--issue", "K2Lab/x#1",
        "--phase", "bogus", "--status", "investigating",
    ], env=env)
    assert cp.returncode != 0
    assert "invalid --phase" in cp.stderr


def test_invalid_status_returns_nonzero(tmp_path):
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    cp = _run_cli([
        "checkpoint", "--team-id", "x", "--issue", "K2Lab/x#1",
        "--phase", "requirements", "--status", "happy",
    ], env=env)
    assert cp.returncode != 0
    assert "invalid --status" in cp.stderr


def test_problem_file_path_works(tmp_path):
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    src = tmp_path / "src.txt"
    src.write_text("file-fed problem", encoding="utf-8")
    cp = _run_cli([
        "checkpoint", "--team-id", "agent-team-w3", "--issue", "K2Lab/x#5",
        "--phase", "requirements", "--status", "investigating",
        "--problem-file", str(src),
    ], env=env)
    assert cp.returncode == 0, cp.stderr
    text = (tmp_path / "reports" / "agent-team-w3.md").read_text(encoding="utf-8")
    assert "file-fed problem" in text


def test_round_trip_with_agent_report(tmp_path, monkeypatch):
    """writer 写出来的文件, agent_report 能正确解析."""
    env = {"COURT_ROOT": str(tmp_path), "PATH": ""}
    cp = _run_cli([
        "checkpoint", "--team-id", "agent-team-w4", "--issue", "K2Lab/x#7",
        "--phase", "execution", "--status", "executing",
        "--problem", "P-rt",
        "--investigation", "I-rt",
        "--solution", "S-rt",
    ], env=env)
    assert cp.returncode == 0, cp.stderr

    # Now read it via agent_report.get_report
    sys.path.insert(0, str(HERE))
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    import agent_report
    agent_report.invalidate_report_cache()
    r = agent_report.get_report("agent-team-w4")
    assert r.source == "file"
    assert r.problem == "P-rt"
    assert r.investigation == "I-rt"
    assert r.solution == "S-rt"
    assert r.status == "executing"
    assert r.phase == "execution"
