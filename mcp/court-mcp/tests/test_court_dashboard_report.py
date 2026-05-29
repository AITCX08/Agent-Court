"""Tests for the report endpoint on court_dashboard_server (PR-20b).

GET /api/agent/{team_id}/report — reads ~/.agent-court/reports/<id>.md or falls
back to claude sonnet on the agent's tmux pane.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from court_dashboard_server import create_app  # noqa: E402

TOKEN = "t"


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))

    def _make():
        state_dir = tmp_path / "gw"
        state_dir.mkdir(parents=True, exist_ok=True)
        return create_app(
            token=TOKEN,
            state_dir=state_dir,
            frontend_dist=None,
            fs_watcher_enabled=False,
        )

    return _make


def _qt(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}t={TOKEN}"


def _write_report(tmp_path: Path, team_id: str, body: str) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{team_id}.md").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_report_endpoint_returns_file_content(app_factory, tmp_path):
    """File present → source=file, sections populated."""
    import agent_report
    agent_report.invalidate_report_cache()
    _write_report(
        tmp_path, "agent-team-r1",
        "---\nteam_id: agent-team-r1\nissue: K2Lab/x#1\n"
        "status: done\nphase: verification\n"
        "updated_at: 2026-05-25T08:00:00Z\n---\n\n"
        "# 问题描述\nP\n\n# 调查情况\nI\n\n# 解决方案\nS\n",
    )
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_qt("/api/agent/agent-team-r1/report"))
        assert resp.status == 200
        body = await resp.json()
        assert body["source"] == "file"
        assert body["status"] == "done"
        assert body["phase"] == "verification"
        assert body["problem"].strip() == "P"
        assert body["investigation"].strip() == "I"
        assert body["solution"].strip() == "S"
        assert body["error"] is None


@pytest.mark.asyncio
async def test_report_endpoint_missing_file_falls_back_to_pane(
    app_factory, tmp_path, monkeypatch,
):
    """No file but tmux pane has content → claude sonnet summarizer fires."""
    import agent_report
    agent_report.invalidate_report_cache()
    monkeypatch.setattr(
        agent_report, "capture_pane", lambda team, **kw: "agent: looking into K2Lab/x#9",
    )

    def fake_run(argv, **kwargs):
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = "# 问题描述\nfb-P\n\n# 调查情况\nfb-I\n\n# 解决方案\nfb-S\n"
        cp.stderr = ""
        return cp

    monkeypatch.setattr(agent_report.subprocess, "run", fake_run)

    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_qt("/api/agent/agent-team-r2/report"))
        assert resp.status == 200
        body = await resp.json()
        assert body["source"] == "fallback"
        assert "fb-P" in body["problem"]
        assert "fb-I" in body["investigation"]
        assert "fb-S" in body["solution"]


@pytest.mark.asyncio
async def test_report_endpoint_missing_file_and_empty_pane(
    app_factory, monkeypatch,
):
    """No file + pane unavailable → source=missing, sections empty."""
    import agent_report
    agent_report.invalidate_report_cache()
    monkeypatch.setattr(
        agent_report, "capture_pane",
        lambda team, **kw: (_ for _ in ()).throw(agent_report.TmuxPaneError("nope")),
    )

    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_qt("/api/agent/agent-team-r3/report"))
        assert resp.status == 200
        body = await resp.json()
        assert body["source"] == "missing"
        assert body["problem"] == ""


@pytest.mark.asyncio
async def test_report_endpoint_force_refresh_bypasses_cache(
    app_factory, tmp_path, monkeypatch,
):
    """force=1 在 cache 内仍重读."""
    import agent_report
    agent_report.invalidate_report_cache()

    # First, no file → cached missing
    monkeypatch.setattr(
        agent_report, "capture_pane",
        lambda team, **kw: (_ for _ in ()).throw(agent_report.TmuxPaneError("nope")),
    )
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_qt("/api/agent/agent-team-r4/report"))
        body = await resp.json()
        assert body["source"] == "missing"

        # Now write file
        _write_report(
            tmp_path, "agent-team-r4",
            "---\nteam_id: agent-team-r4\nstatus: done\nphase: done\n"
            "updated_at: 2026-05-25T00:00:00Z\n---\n\n"
            "# 问题描述\nP4\n\n# 调查情况\n\n\n# 解决方案\nS4\n",
        )

        # Without force, cached
        resp2 = await client.get(_qt("/api/agent/agent-team-r4/report"))
        body2 = await resp2.json()
        assert body2["source"] == "missing"

        # With force, re-reads
        resp3 = await client.get(_qt("/api/agent/agent-team-r4/report?force=1"))
        body3 = await resp3.json()
        assert body3["source"] == "file"
        assert body3["problem"] == "P4"


@pytest.mark.asyncio
async def test_report_endpoint_strips_tmux_prefix(app_factory, tmp_path, monkeypatch):
    """``tmux:agent-team-x`` 路径参数也应能命中文件."""
    import agent_report
    agent_report.invalidate_report_cache()
    _write_report(
        tmp_path, "agent-team-r5",
        "---\nteam_id: agent-team-r5\nstatus: done\nphase: done\n"
        "updated_at: 2026-05-25T00:00:00Z\n---\n\n"
        "# 问题描述\nP5\n\n# 调查情况\n\n\n# 解决方案\nS5\n",
    )

    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_qt("/api/agent/tmux:agent-team-r5/report"))
        body = await resp.json()
        assert resp.status == 200
        assert body["source"] == "file"
        assert body["problem"] == "P5"


@pytest.mark.asyncio
async def test_report_endpoint_requires_token(app_factory):
    app = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/agent/agent-team-r1/report")  # no ?t=
        assert resp.status == 401
