"""Tests for the 3 freeform agent endpoints on court_dashboard_server (PR-19b-1).

Uses aiohttp.test_utils with monkeypatched AgentSpawner + tmux_pane to avoid
real tmux side effects.
"""
from __future__ import annotations

import json
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
    """Build the dashboard aiohttp app with a fixed token and fs paths.

    Returns a factory: app_factory(spawner_mock=..., capture_mock=..., send_keys_mock=...).
    - spawner_mock: replaces app["agent_spawner"] post create_app
    - capture_mock: monkeypatches court_dashboard_server.capture_pane
    - send_keys_mock: monkeypatches court_dashboard_server.send_keys_text
    """
    def _make(*, spawner_mock=None, capture_mock=None, send_keys_mock=None):
        state_dir = tmp_path / "gw"
        state_dir.mkdir(parents=True, exist_ok=True)
        if capture_mock is not None:
            monkeypatch.setattr("court_dashboard_server.capture_pane", capture_mock)
        if send_keys_mock is not None:
            monkeypatch.setattr("court_dashboard_server.send_keys_text", send_keys_mock)
        app = create_app(
            token=TOKEN,
            state_dir=state_dir,
            frontend_dist=None,
            fs_watcher_enabled=False,
        )
        if spawner_mock is not None:
            app["agent_spawner"] = spawner_mock
        return app
    return _make


def _qt(path: str) -> str:
    """Append ?t=TOKEN."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}t={TOKEN}"


# ---- POST /api/agent/freeform-spawn ----

@pytest.mark.asyncio
async def test_freeform_spawn_happy_path(app_factory):
    spawner = MagicMock()
    spawner.spawn_freeform.return_value = {
        "team_id": "agent-team-xyz789",
        "session": "agent-team-xyz789",
        "already_spawned": False,
        "linked": None,
        "label": "试做 X",
    }
    app = app_factory(spawner_mock=spawner)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/freeform-spawn"),
            data=json.dumps({"label": "试做 X", "initial_prompt": "我想做 X"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["team_id"] == "agent-team-xyz789"
        assert body["label"] == "试做 X"
    spawner.spawn_freeform.assert_called_once_with(
        label="试做 X", initial_prompt="我想做 X"
    )


@pytest.mark.asyncio
async def test_freeform_spawn_empty_prompt_rejected(app_factory):
    app = app_factory(spawner_mock=MagicMock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/freeform-spawn"),
            data=json.dumps({"label": "L", "initial_prompt": "   "}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "initial_prompt" in body["error"]


@pytest.mark.asyncio
async def test_freeform_spawn_missing_label_rejected(app_factory):
    app = app_factory(spawner_mock=MagicMock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/freeform-spawn"),
            data=json.dumps({"initial_prompt": "x"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "label" in body["error"]


@pytest.mark.asyncio
async def test_freeform_spawn_backend_failure(app_factory):
    spawner = MagicMock()
    from agent_spawn import SpawnError
    spawner.spawn_freeform.side_effect = SpawnError("tmux new-session failed")
    app = app_factory(spawner_mock=spawner)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/freeform-spawn"),
            data=json.dumps({"label": "L", "initial_prompt": "p"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 500
        body = await resp.json()
        assert "tmux new-session" in body["error"]


# ---- GET /api/agent/{team_id}/pane ----

@pytest.mark.asyncio
async def test_pane_capture_default(app_factory):
    capture = MagicMock(return_value="hello world\nline 2\n")
    app = app_factory(capture_mock=capture)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            _qt("/api/agent/agent-team-abc123/pane")
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["team_id"] == "agent-team-abc123"
        assert "hello world" in body["content"]
        assert "captured_at" in body
    capture.assert_called_once_with("agent-team-abc123", lines=1000)


@pytest.mark.asyncio
async def test_pane_capture_custom_lines(app_factory):
    capture = MagicMock(return_value="short")
    app = app_factory(capture_mock=capture)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            _qt("/api/agent/agent-team-z1/pane?lines=200")
        )
        assert resp.status == 200
    capture.assert_called_once_with("agent-team-z1", lines=200)


@pytest.mark.asyncio
async def test_pane_invalid_team_id_prefix_rejected(app_factory):
    """team_id 必须以 agent-team- 开头 (防误读其他 tmux session)."""
    app = app_factory(capture_mock=MagicMock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            _qt("/api/agent/some-other-session/pane")
        )
        assert resp.status == 400
        body = await resp.json()
        assert "team_id" in body["error"]


@pytest.mark.asyncio
async def test_pane_backend_failure(app_factory):
    from tmux_pane import TmuxPaneError
    capture = MagicMock(side_effect=TmuxPaneError("session not found"))
    app = app_factory(capture_mock=capture)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            _qt("/api/agent/agent-team-bad/pane")
        )
        assert resp.status == 500
        body = await resp.json()
        assert "session not found" in body["error"]


# ---- POST /api/agent/{team_id}/input ----

@pytest.mark.asyncio
async def test_input_sends_text(app_factory):
    send = MagicMock()
    app = app_factory(send_keys_mock=send)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/agent-team-x1/input"),
            data=json.dumps({"text": "hello\nmulti line"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
    send.assert_called_once_with(
        "agent-team-x1", "hello\nmulti line", append_enter=True
    )


@pytest.mark.asyncio
async def test_input_append_enter_false(app_factory):
    send = MagicMock()
    app = app_factory(send_keys_mock=send)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/agent-team-q/input"),
            data=json.dumps({"text": "draft", "append_enter": False}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
    send.assert_called_once_with(
        "agent-team-q", "draft", append_enter=False
    )


@pytest.mark.asyncio
async def test_input_empty_text_rejected(app_factory):
    send = MagicMock()
    app = app_factory(send_keys_mock=send)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/agent-team-x/input"),
            data=json.dumps({"text": "   "}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "text" in body["error"]
    send.assert_not_called()


@pytest.mark.asyncio
async def test_input_invalid_team_id_rejected(app_factory):
    send = MagicMock()
    app = app_factory(send_keys_mock=send)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            _qt("/api/agent/external-session/input"),
            data=json.dumps({"text": "hi"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    send.assert_not_called()
