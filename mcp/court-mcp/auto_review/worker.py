"""Background polling worker for the auto-review pipeline.

Two threading.Thread instances:
- discovery (60s): poll Gitea /repos/issues/search for open PRs / issues where
  the bot is requested-reviewer / assignee; filter to watch_repos; enqueue.
- active (30s): re-poll the same query but only enqueue when the head_sha for
  a known (repo, number) PR has changed.

This module is execution-free — it only writes to StateStore. Routing to light/
deep review (PR-18d) and webhook ingestion (PR-18c) live elsewhere.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from auto_review.bot_account import BotAccount
from auto_review.config import AutoReviewConfig
from auto_review.state import DedupeSkipped, StateStore, TaskKind

_log = logging.getLogger("auto_review.worker")


class _SearchClient(Protocol):
    def search_issues(self, params: dict[str, str]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    new_tasks: int = 0
    dedupe_skipped: int = 0
    errors: int = 0


def _extract_repo_full_name(item: dict[str, Any]) -> str | None:
    """Gitea search-issues items expose repo as 'repository.full_name' or via 'url'."""
    repo = item.get("repository")
    if isinstance(repo, dict):
        full = repo.get("full_name")
        if isinstance(full, str):
            return full
    return None


def _is_pr_item(item: dict[str, Any]) -> bool:
    return bool(item.get("pull_request")) or "pulls" in str(item.get("url", ""))


def _extract_head_sha(item: dict[str, Any]) -> str | None:
    head = item.get("head")
    if isinstance(head, dict):
        sha = head.get("sha")
        if isinstance(sha, str) and sha:
            return sha
    return None


def _reviewer_logins(item: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("requested_reviewers", "requested_reviewer", "reviewers"):
        v = item.get(key)
        if isinstance(v, list):
            for r in v:
                if isinstance(r, dict) and isinstance(r.get("login"), str):
                    out.add(r["login"])
    return out


def _assignee_logins(item: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    a = item.get("assignee")
    if isinstance(a, dict) and isinstance(a.get("login"), str):
        out.add(a["login"])
    al = item.get("assignees")
    if isinstance(al, list):
        for r in al:
            if isinstance(r, dict) and isinstance(r.get("login"), str):
                out.add(r["login"])
    return out


class PollingWorker:
    def __init__(
        self,
        *,
        cfg: AutoReviewConfig,
        bot: BotAccount,
        client: _SearchClient,
        store: StateStore,
    ):
        self._cfg = cfg
        self._bot = bot
        self._client = client
        self._store = store
        self._stop_event = threading.Event()
        self._discovery_thread: threading.Thread | None = None
        self._active_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._discovery_thread is not None and self._discovery_thread.is_alive():
                return  # idempotent
            self._stop_event.clear()
            self._discovery_thread = threading.Thread(
                target=self._discovery_loop, name="auto-review-discovery", daemon=True
            )
            self._active_thread = threading.Thread(
                target=self._active_loop, name="auto-review-active", daemon=True
            )
            self._discovery_thread.start()
            self._active_thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        for t in (self._discovery_thread, self._active_thread):
            if t is not None:
                t.join(timeout=timeout)

    def run_discovery_once(self) -> DiscoverySummary:
        return self._poll(active_only=False)

    def run_active_once(self) -> DiscoverySummary:
        return self._poll(active_only=True)

    def _poll(self, *, active_only: bool) -> DiscoverySummary:
        new_tasks = 0
        dedupe_skipped = 0
        errors = 0
        watch = set(self._cfg.watch_repos)

        for kind, params in self._search_params():
            if active_only and kind == TaskKind.ISSUE:
                continue  # active poll 只关心 PR head_sha 变化
            try:
                items = self._client.search_issues(params)
            except Exception as exc:
                _log.warning("search_issues failed: %s", exc)
                errors += 1
                continue

            for item in items:
                try:
                    repo = _extract_repo_full_name(item)
                    if repo is None or repo not in watch:
                        continue
                    number = item.get("number")
                    if not isinstance(number, int):
                        continue
                    if kind == TaskKind.PR and not self._bot_is_pr_reviewer(item):
                        continue
                    if kind == TaskKind.ISSUE and not self._bot_is_issue_assignee(item):
                        continue
                    head_sha = _extract_head_sha(item) if kind == TaskKind.PR else None
                    if kind == TaskKind.PR and head_sha is None:
                        continue  # PR 没拿到 sha, 等下轮
                    try:
                        self._store.enqueue(
                            kind=kind, repo=repo, number=number, head_sha=head_sha
                        )
                        new_tasks += 1
                    except DedupeSkipped:
                        dedupe_skipped += 1
                except Exception as exc:
                    _log.warning("enqueue failed: %s", exc)
                    errors += 1

        return DiscoverySummary(
            new_tasks=new_tasks, dedupe_skipped=dedupe_skipped, errors=errors
        )

    def _search_params(self) -> list[tuple[TaskKind, dict[str, str]]]:
        return [
            (
                TaskKind.PR,
                {
                    "type": "pulls",
                    "state": "open",
                    "reviewer": self._bot.login,
                },
            ),
            (
                TaskKind.ISSUE,
                {
                    "type": "issues",
                    "state": "open",
                    "assignee": self._bot.login,
                },
            ),
        ]

    def _bot_is_pr_reviewer(self, item: dict[str, Any]) -> bool:
        return self._bot.login in _reviewer_logins(item) or self._bot.login in _assignee_logins(item)

    def _bot_is_issue_assignee(self, item: dict[str, Any]) -> bool:
        return self._bot.login in _assignee_logins(item)

    def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_discovery_once()
            except Exception:
                _log.exception("discovery loop crashed (continuing)")
            self._stop_event.wait(self._cfg.poll_discovery_interval_sec)

    def _active_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_active_once()
            except Exception:
                _log.exception("active loop crashed (continuing)")
            self._stop_event.wait(self._cfg.poll_active_interval_sec)
