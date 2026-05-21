"""SY-3 (#18) MVP v1 旁挂: /api/orchestrator/snapshot endpoint 测试.

只测新加的 endpoint, 不动 PR-15 既有 /api/status / SSE / approve 路径.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import orchestrator as orch  # noqa: E402
from court_dashboard_server import create_app  # noqa: E402


TOKEN = "test-token-orch-1"


class _InjectedOrchestrator(orch.Orchestrator):
    """跟 test_orchestrator.py 同款: 注入 tmux_windows 避开真 tmux."""

    def __init__(self, *args, tmux_windows: list[str] | None = None, **kw):
        super().__init__(*args, **kw)
        self._injected = tmux_windows or []

    def _collect_tmux_windows(self) -> list[str]:
        return list(self._injected)


def _seed_seen(state_dir: Path, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen-issues.json").write_text(json.dumps(data))


@pytest.mark.asyncio
async def test_orchestrator_snapshot_endpoint_returns_clean(tmp_path):
    state_dir = tmp_path / "gitea-watcher"
    _seed_seen(state_dir, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1"},
    })
    app = create_app(token=TOKEN, state_dir=state_dir, fs_watcher_enabled=False)
    app["orchestrator"] = _InjectedOrchestrator(
        court_root=tmp_path,
        tmux_windows=[orch.WATCHER_WINDOW, "foo-bar-1"],
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/orchestrator/snapshot?t={TOKEN}")
        assert resp.status == 200
        data = await resp.json()
        assert set(data.keys()) == {"runs", "inconsistencies", "metrics", "orphan_tmux_windows"}
        assert data["inconsistencies"] == []
        assert data["metrics"]["total"] == 1
        assert data["metrics"]["dispatched"] == 1


@pytest.mark.asyncio
async def test_orchestrator_snapshot_endpoint_reports_inconsistencies(tmp_path):
    state_dir = tmp_path / "gitea-watcher"
    _seed_seen(state_dir, {
        "foo/bar#1": {"last_action": "DISPATCHED_DASHBOARD", "tmux_window": "foo-bar-1"},
    })
    app = create_app(token=TOKEN, state_dir=state_dir, fs_watcher_enabled=False)
    app["orchestrator"] = _InjectedOrchestrator(
        court_root=tmp_path,
        tmux_windows=[orch.WATCHER_WINDOW],  # foo-bar-1 缺
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/orchestrator/snapshot?t={TOKEN}")
        assert resp.status == 200
        data = await resp.json()
        kinds = {i["kind"] for i in data["inconsistencies"]}
        assert "dispatched_window_gone" in kinds
        assert data["metrics"]["inconsistencies_error"] == 1


@pytest.mark.asyncio
async def test_orchestrator_snapshot_endpoint_requires_token(tmp_path):
    state_dir = tmp_path / "gitea-watcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(token=TOKEN, state_dir=state_dir, fs_watcher_enabled=False)
    app["orchestrator"] = _InjectedOrchestrator(court_root=tmp_path, tmux_windows=[])
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/orchestrator/snapshot")
        assert resp.status == 401
