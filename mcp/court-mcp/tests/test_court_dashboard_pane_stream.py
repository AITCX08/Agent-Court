"""Tests for GET /api/agent/{team_id}/pane/stream — SSE endpoint (PR-19b-3)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from court_dashboard_server import create_app


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    def _make(*, capture_sequence=None):
        # Replace capture_pane with a sequenced mock
        if capture_sequence is not None:
            it = iter(capture_sequence)
            def fake_capture(team_id, lines=1000):
                try:
                    return next(it)
                except StopIteration:
                    return capture_sequence[-1]  # repeat last
            monkeypatch.setattr("court_dashboard_server.capture_pane", fake_capture)
        return create_app(
            token="t",
            state_dir=tmp_path / "gw",
            frontend_dist=None,
            fs_watcher_enabled=False,
        )
    return _make


def _q(path):
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}t=t"


def _parse_sse_events(raw: str):
    """Extract data: payloads from raw SSE bytes (string)."""
    events = []
    for chunk in raw.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(line[len("data: "):])
    return events


@pytest.mark.asyncio
async def test_pane_stream_invalid_team_id_rejected(app_factory):
    app = app_factory(capture_sequence=["x"])
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_q("/api/agent/external-session/pane/stream"))
        assert resp.status == 400


@pytest.mark.asyncio
async def test_pane_stream_initial_snapshot_pushed(app_factory):
    """Connecting → server immediately writes one data: <snapshot>."""
    app = app_factory(capture_sequence=["initial pane content\n"])
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            _q("/api/agent/agent-team-abc/pane/stream"),
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
        # Read the first 4 KB or until we see the first event
        raw = await asyncio.wait_for(resp.content.readany(), timeout=3.0)
        events = _parse_sse_events(raw.decode("utf-8"))
        assert any("initial pane content" in e for e in events)


@pytest.mark.asyncio
async def test_pane_stream_only_pushes_on_change(app_factory, monkeypatch):
    """If capture-pane returns same content N times, we get 1 event + keepalives only."""
    # Reduce poll + keepalive intervals to make this test fast
    monkeypatch.setattr("court_dashboard_server.PANE_STREAM_TICK_SEC", 0.05)
    monkeypatch.setattr("court_dashboard_server.PANE_STREAM_KEEPALIVE_SEC", 0.4)
    # Same content 5x
    app = app_factory(capture_sequence=["same\n", "same\n", "same\n", "same\n", "same\n"])

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_q("/api/agent/agent-team-x/pane/stream"))
        # Read for ~0.2s — enough for initial event + ~3 ticks
        raw = bytearray()
        deadline = asyncio.get_event_loop().time() + 0.3
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(resp.content.readany(), timeout=0.1)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            raw.extend(chunk)

        events = _parse_sse_events(raw.decode("utf-8"))
        # Exactly one data event (initial); no duplicates because content didn't change
        assert len(events) == 1
        assert "same" in events[0]


@pytest.mark.asyncio
async def test_pane_stream_pushes_on_change(app_factory, monkeypatch):
    """Content changes between ticks → multiple data events."""
    monkeypatch.setattr("court_dashboard_server.PANE_STREAM_TICK_SEC", 0.05)
    monkeypatch.setattr("court_dashboard_server.PANE_STREAM_KEEPALIVE_SEC", 5.0)
    app = app_factory(capture_sequence=["one\n", "one\n", "two\n", "two\n", "three\n"])

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(_q("/api/agent/agent-team-x/pane/stream"))
        raw = bytearray()
        # Need >= 5 * 0.05s = 0.25s for the sequence to exhaust; keep reading
        # generously so we don't bail before the 3rd distinct value arrives.
        deadline = asyncio.get_event_loop().time() + 1.5
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(resp.content.readany(), timeout=0.3)
            except asyncio.TimeoutError:
                # After exhaustion, capture returns "three" forever so no more
                # data events fire — a timeout here means we've seen everything.
                break
            if not chunk:
                break
            raw.extend(chunk)
            # Early-exit if we already see all three distinct values
            text = raw.decode("utf-8", errors="ignore")
            if "one" in text and "two" in text and "three" in text:
                break

        events = _parse_sse_events(raw.decode("utf-8"))
        # 3 distinct contents = 3 data events
        joined = " ".join(events)
        assert "one" in joined
        assert "two" in joined
        assert "three" in joined
        assert len(events) == 3
