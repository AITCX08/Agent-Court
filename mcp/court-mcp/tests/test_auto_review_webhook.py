"""Tests for auto_review.webhook — Gitea webhook listener.

Uses aiohttp.test_utils to start a TestServer; signature signing via
hmac.HMAC-SHA256 (matches Gitea X-Gitea-Signature scheme).

Note: project has pytest-asyncio (mode=manual) but not pytest-aiohttp, so we
follow the existing test_gitea_webhook_receiver.py pattern — @pytest.mark.asyncio
+ explicit ``async with TestClient(TestServer(app))`` instead of the
``aiohttp_client`` fixture.
"""
from __future__ import annotations

import hmac
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.bot_account import BotAccount
from auto_review.config import AutoReviewConfig
from auto_review.state import StateStore, TaskKind
from auto_review.webhook import create_app


SECRET = "test-webhook-secret-pr18c"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, sha256).hexdigest()


def _cfg(webhook_triggers: bool = True, watch=("K2Lab/agent-court",)) -> AutoReviewConfig:
    return AutoReviewConfig(
        bot_username="bot",
        watch_repos=list(watch),
        webhook_triggers_enabled=webhook_triggers,
    )


def _bot() -> BotAccount:
    return BotAccount(login="bot", user_id=99)


def _store() -> StateStore:
    return StateStore(":memory:")


def _pr_payload(
    action: str = "opened",
    number: int = 42,
    head_sha: str = "sha-abc",
    reviewers: tuple[str, ...] = ("bot",),
    repo: str = "K2Lab/agent-court",
) -> dict:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "state": "open",
            "head": {"sha": head_sha},
            "requested_reviewers": [{"login": r} for r in reviewers],
            "assignees": [],
        },
        "repository": {"full_name": repo},
    }


def _issue_payload(
    action: str = "assigned",
    number: int = 7,
    assignee: str | None = "bot",
    repo: str = "K2Lab/agent-court",
) -> dict:
    return {
        "action": action,
        "issue": {
            "number": number,
            "state": "open",
            "assignees": [{"login": assignee}] if assignee else [],
        },
        "repository": {"full_name": repo},
    }


def _make_app(*, cfg=None, bot=None, store=None, secret: str = SECRET):
    return create_app(
        cfg=cfg or _cfg(),
        bot=bot or _bot(),
        store=store or _store(),
        secret=secret,
    )


async def _post(client, path, payload, *, event, secret: str = SECRET, sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {"X-Gitea-Event": event, "Content-Type": "application/json"}
    if sign:
        headers["X-Gitea-Signature"] = _sign(body, secret)
    return await client.post(path, data=body, headers=headers)


@pytest.mark.asyncio
async def test_health_endpoint_returns_status():
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["queue_depth"] == 0
        assert data["webhook_triggers_enabled"] is True


@pytest.mark.asyncio
async def test_pr_opened_enqueued_when_bot_is_reviewer():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client, "/gitea/webhook", _pr_payload(action="opened"), event="pull_request"
        )
        assert resp.status == 200
        assert "enqueued" in await resp.text()
    assert store.count() == 1
    task = store.get_by_dedupe_key("K2Lab/agent-court#42@sha-abc")
    assert task is not None
    assert task.kind == TaskKind.PR


@pytest.mark.asyncio
async def test_pr_dropped_when_triggers_disabled():
    store = _store()
    app = _make_app(cfg=_cfg(webhook_triggers=False), store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client, "/gitea/webhook", _pr_payload(), event="pull_request"
        )
        assert resp.status == 200
        assert "status=disabled" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_invalid_signature_dropped():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        body = json.dumps(_pr_payload()).encode()
        resp = await client.post(
            "/gitea/webhook",
            data=body,
            headers={
                "X-Gitea-Event": "pull_request",
                "X-Gitea-Signature": "0" * 64,
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 200
        assert "invalid-signature" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_pr_dropped_when_bot_not_reviewer():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client,
            "/gitea/webhook",
            _pr_payload(reviewers=("alice",)),
            event="pull_request",
        )
        assert resp.status == 200
        assert "not-targeted-at-bot" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_pr_dropped_when_repo_not_watched():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client,
            "/gitea/webhook",
            _pr_payload(repo="K2Lab/other-repo"),
            event="pull_request",
        )
        assert resp.status == 200
        assert "not-watched" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_issue_opened_enqueued_when_bot_is_assignee():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client, "/gitea/webhook", _issue_payload(action="opened"), event="issues"
        )
        assert resp.status == 200
        assert "enqueued" in await resp.text()
    assert store.count() == 1
    task = store.get_by_dedupe_key("K2Lab/agent-court#7")
    assert task is not None
    assert task.kind == TaskKind.ISSUE


@pytest.mark.asyncio
async def test_issue_dropped_when_bot_not_assignee():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client,
            "/gitea/webhook",
            _issue_payload(action="assigned", assignee="alice"),
            event="issue_assign",
        )
        assert resp.status == 200
        assert "not-targeted-at-bot" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_review_requested_event_enqueued():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client,
            "/gitea/webhook",
            _pr_payload(action="review_requested"),
            event="pull_request_review_request",
        )
        assert resp.status == 200
        assert "enqueued" in await resp.text()
    assert store.count() == 1


@pytest.mark.asyncio
async def test_pull_request_sync_event_enqueued():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client,
            "/gitea/webhook",
            _pr_payload(action="synchronized"),
            event="pull_request_sync",
        )
        assert resp.status == 200
        assert "enqueued" in await resp.text()
    assert store.count() == 1


@pytest.mark.asyncio
async def test_bridge_test_event_ok_no_enqueue():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client, "/gitea/webhook", {"test": True}, event="bridge_test"
        )
        assert resp.status == 200
        assert "bridge-test" in await resp.text()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_unknown_event_dropped():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(
            client, "/gitea/webhook", {"foo": "bar"}, event="push"
        )
        assert resp.status == 200
        text = await resp.text()
        assert "event=push" in text
        assert "dropped" in text
    assert store.count() == 0


@pytest.mark.asyncio
async def test_pr_new_head_sha_creates_new_task():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        await _post(
            client,
            "/gitea/webhook",
            _pr_payload(head_sha="sha-old"),
            event="pull_request",
        )
        await _post(
            client,
            "/gitea/webhook",
            _pr_payload(head_sha="sha-new", action="synchronized"),
            event="pull_request",
        )
    assert store.count() == 2


@pytest.mark.asyncio
async def test_pr_same_head_sha_dedupe():
    store = _store()
    app = _make_app(store=store)
    async with TestClient(TestServer(app)) as client:
        await _post(
            client,
            "/gitea/webhook",
            _pr_payload(head_sha="sha-same"),
            event="pull_request",
        )
        resp = await _post(
            client,
            "/gitea/webhook",
            _pr_payload(head_sha="sha-same", action="synchronized"),
            event="pull_request",
        )
        assert resp.status == 200
        assert "dedupe-skipped" in await resp.text()
    assert store.count() == 1
