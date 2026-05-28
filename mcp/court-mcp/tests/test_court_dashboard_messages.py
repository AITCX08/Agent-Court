"""Integration tests for /api/messages and /api/messages/stream (PR-21b)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from court_dashboard_server import create_app  # noqa: E402


def _write_fixture(tmp_path, project="k2work", history=None):
    if history is None:
        history = [
            {"role": "user", "content": "Hi", "timestamp": "2026-05-10T10:00:00+08:00"},
            {"role": "assistant", "content": "Hello!", "timestamp": "2026-05-10T10:00:05+08:00"},
        ]
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    fp = sessions_dir / f"{project}_abc.json"
    fp.write_text(json.dumps({
        "sessions": {"s1": {"id": "s1", "history": history}},
        "active_session": {"weixin:dm:u@h": "s1"},
        "version": 1,
    }), encoding="utf-8")
    return fp


@pytest_asyncio.fixture
async def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_CONNECT_HOME", str(tmp_path))
    app = create_app(token="testtoken", state_dir=tmp_path / "state", fs_watcher_enabled=False)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_messages_history_returns_recent(tmp_path, app_client):
    _write_fixture(tmp_path)
    resp = await app_client.get("/api/messages",
                                 headers={"Authorization": "Bearer testtoken"})
    assert resp.status == 200
    data = await resp.json()
    assert "messages" in data
    assert len(data["messages"]) == 2
    assert data["messages"][0]["content"] == "Hello!"
    assert data["messages"][0]["platform"] == "weixin"


@pytest.mark.asyncio
async def test_messages_history_respects_limit(tmp_path, app_client):
    _write_fixture(tmp_path, history=[
        {"role": "user", "content": f"M{i}", "timestamp": f"2026-05-10T10:00:{i:02d}+08:00"}
        for i in range(5)
    ])
    resp = await app_client.get("/api/messages?limit=2",
                                 headers={"Authorization": "Bearer testtoken"})
    assert resp.status == 200
    data = await resp.json()
    assert len(data["messages"]) == 2


@pytest.mark.asyncio
async def test_messages_history_before_cursor(tmp_path, app_client):
    _write_fixture(tmp_path, history=[
        {"role": "user", "content": f"M{i}", "timestamp": f"2026-05-10T10:00:{i:02d}+08:00"}
        for i in range(5)
    ])
    resp = await app_client.get(
        "/api/messages?before=2026-05-10T10:00:03%2B08:00",
        headers={"Authorization": "Bearer testtoken"})
    assert resp.status == 200
    data = await resp.json()
    contents = [m["content"] for m in data["messages"]]
    assert "M3" not in contents
    assert "M2" in contents


@pytest.mark.asyncio
async def test_messages_history_requires_auth(tmp_path, app_client):
    _write_fixture(tmp_path)
    resp = await app_client.get("/api/messages")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_messages_stream_pushes_new_message(tmp_path, app_client):
    """初始空 → 写入文件 → SSE 收到新消息事件。"""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    fp = sessions_dir / "k2work_abc.json"
    fp.write_text(json.dumps({
        "sessions": {"s1": {"id": "s1", "history": []}},
        "active_session": {"weixin:dm:u@h": "s1"},
        "version": 1,
    }), encoding="utf-8")

    async with app_client.get(
        "/api/messages/stream",
        headers={"Authorization": "Bearer testtoken"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        await asyncio.sleep(0.5)

        fp.write_text(json.dumps({
            "sessions": {"s1": {"id": "s1", "history": [
                {"role": "user", "content": "NEW", "timestamp": "2026-05-10T10:00:00+08:00"},
            ]}},
            "active_session": {"weixin:dm:u@h": "s1"},
            "version": 1,
        }), encoding="utf-8")

        got_new = False
        try:
            async with asyncio.timeout(5.0):
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("data:"):
                        payload = json.loads(line[len("data:"):].strip())
                        if payload.get("content") == "NEW":
                            got_new = True
                            break
        except (TimeoutError, asyncio.TimeoutError):
            pass

        assert got_new, "SSE 未推送新消息"
