"""Gitea webhook listener for the auto-review pipeline.

Fresh module (does not reuse ``gitea_webhook_receiver`` so the two webhooks can
run on different ports without entanglement). Port 48731 is the
KAXY-3022/Agent-manager default.

Behavior contract — every code path returns HTTP 200 with a human-readable body
describing the disposition (Agent-manager style: never 4xx, makes Gitea retry
unproductive). Side effect is the ``store.enqueue`` call, gated by:

1. HMAC-SHA256 signature match (``X-Gitea-Signature`` over raw body, key = secret)
2. ``X-Gitea-Event`` header maps to a known event family
3. ``payload.action`` maps to an in-scope action (opened / assigned / ...)
4. Repo is in ``cfg.watch_repos``
5. Bot is requested_reviewer / assignee on the item
6. ``cfg.webhook_triggers_enabled`` is True (matches Agent-manager opt-in default)
"""
from __future__ import annotations

import hmac
import json
import logging
from hashlib import sha256
from typing import Any

from aiohttp import web

from auto_review.bot_account import BotAccount
from auto_review.config import AutoReviewConfig
from auto_review.state import DedupeSkipped, StateStore, TaskKind

_log = logging.getLogger("auto_review.webhook")


_PR_EVENTS = {"pull_request", "pull_request_review_request", "pull_request_sync"}
_PR_ACTIONS = {
    "opened",
    "reopened",
    "synchronized",
    "synchronize",
    "sync",
    "review_requested",
}
_ISSUE_EVENTS = {"issues", "issue_assign"}
_ISSUE_ACTIONS = {"opened", "assigned", "reopened"}


def _verify_signature(secret: str, raw_body: bytes, header_sig: str) -> bool:
    if not header_sig:
        return False
    try:
        computed = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(computed, header_sig)


def _extract_repo(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        full = repo.get("full_name")
        if isinstance(full, str) and full:
            return full
    return None


def _reviewer_logins_from_pr(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    for source in (pr, payload):
        if not isinstance(source, dict):
            continue
        for key in ("requested_reviewers", "reviewers"):
            v = source.get(key)
            if isinstance(v, list):
                for r in v:
                    if isinstance(r, dict) and isinstance(r.get("login"), str):
                        out.add(r["login"])
        for key in ("requested_reviewer", "reviewer", "assignee"):
            v = source.get(key)
            if isinstance(v, dict) and isinstance(v.get("login"), str):
                out.add(v["login"])
        al = source.get("assignees")
        if isinstance(al, list):
            for r in al:
                if isinstance(r, dict) and isinstance(r.get("login"), str):
                    out.add(r["login"])
    return out


def _assignee_logins_from_issue(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    for source in (issue, payload):
        if not isinstance(source, dict):
            continue
        v = source.get("assignee")
        if isinstance(v, dict) and isinstance(v.get("login"), str):
            out.add(v["login"])
        al = source.get("assignees")
        if isinstance(al, list):
            for r in al:
                if isinstance(r, dict) and isinstance(r.get("login"), str):
                    out.add(r["login"])
    return out


def _pr_head_sha(payload: dict[str, Any]) -> str | None:
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        head = pr.get("head")
        if isinstance(head, dict):
            sha = head.get("sha")
            if isinstance(sha, str) and sha:
                return sha
    return None


def _pr_number(payload: dict[str, Any]) -> int | None:
    for source in (payload.get("pull_request"), payload):
        if isinstance(source, dict):
            n = source.get("number")
            if isinstance(n, int):
                return n
    return None


def _issue_number(payload: dict[str, Any]) -> int | None:
    issue = payload.get("issue")
    if isinstance(issue, dict):
        n = issue.get("number")
        if isinstance(n, int):
            return n
    return None


def create_app(
    *,
    cfg: AutoReviewConfig,
    bot: BotAccount,
    store: StateStore,
    secret: str,
) -> web.Application:
    """Build the aiohttp Application; caller runs it via aiohttp.web.run_app."""
    watch = set(cfg.watch_repos)

    async def healthz(_req: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "queue_depth": store.count(),
                "webhook_triggers_enabled": cfg.webhook_triggers_enabled,
            }
        )

    async def webhook(request: web.Request) -> web.Response:
        raw = await request.read()
        sig = request.headers.get("X-Gitea-Signature", "")
        if not _verify_signature(secret, raw, sig):
            return web.Response(status=200, text="invalid-signature dropped")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.Response(status=200, text="bad-json-acked")

        event = request.headers.get("X-Gitea-Event", "")

        if event == "bridge_test":
            return web.Response(status=200, text="bridge-test ok")

        if event in _PR_EVENTS:
            return _handle_pr(payload, event, cfg, bot, store, watch)
        if event in _ISSUE_EVENTS:
            return _handle_issue(payload, event, cfg, bot, store, watch)

        return web.Response(status=200, text=f"event={event} dropped")

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/health", healthz)
    app.router.add_post("/gitea/webhook", webhook)
    return app


def _handle_pr(
    payload: dict[str, Any],
    event: str,
    cfg: AutoReviewConfig,
    bot: BotAccount,
    store: StateStore,
    watch: set[str],
) -> web.Response:
    action = payload.get("action")
    if action not in _PR_ACTIONS:
        return web.Response(status=200, text=f"action={action} dropped")
    repo = _extract_repo(payload)
    if repo is None or repo not in watch:
        return web.Response(status=200, text=f"repo={repo} not-watched dropped")
    number = _pr_number(payload)
    head_sha = _pr_head_sha(payload)
    if number is None or head_sha is None:
        return web.Response(status=200, text="missing-number-or-head-sha dropped")
    if bot.login not in _reviewer_logins_from_pr(payload):
        return web.Response(status=200, text="not-targeted-at-bot dropped")
    if not cfg.webhook_triggers_enabled:
        return web.Response(status=200, text="status=disabled")
    return _enqueue(store, kind=TaskKind.PR, repo=repo, number=number, head_sha=head_sha)


def _handle_issue(
    payload: dict[str, Any],
    event: str,
    cfg: AutoReviewConfig,
    bot: BotAccount,
    store: StateStore,
    watch: set[str],
) -> web.Response:
    action = payload.get("action")
    if action not in _ISSUE_ACTIONS:
        return web.Response(status=200, text=f"action={action} dropped")
    repo = _extract_repo(payload)
    if repo is None or repo not in watch:
        return web.Response(status=200, text=f"repo={repo} not-watched dropped")
    number = _issue_number(payload)
    if number is None:
        return web.Response(status=200, text="missing-number dropped")
    if bot.login not in _assignee_logins_from_issue(payload):
        return web.Response(status=200, text="not-targeted-at-bot dropped")
    if not cfg.webhook_triggers_enabled:
        return web.Response(status=200, text="status=disabled")
    return _enqueue(store, kind=TaskKind.ISSUE, repo=repo, number=number, head_sha=None)


def _enqueue(
    store: StateStore,
    *,
    kind: TaskKind,
    repo: str,
    number: int,
    head_sha: str | None,
) -> web.Response:
    try:
        store.enqueue(kind=kind, repo=repo, number=number, head_sha=head_sha)
        return web.Response(status=200, text="enqueued")
    except DedupeSkipped:
        return web.Response(status=200, text="dedupe-skipped")
    except Exception as exc:
        _log.exception("enqueue failed")
        return web.Response(status=200, text=f"enqueue-error-acked: {exc}")
