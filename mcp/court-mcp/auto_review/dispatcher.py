"""Orchestrate the full auto-review pipeline per task.

Routes by ``changed_files`` (PR only — issues always go light), drives the
state machine forward (DISCOVERED → QUEUED → RUNNING → REVIEW_DONE → POSTED
or FAILED), and honors the auto-post toggle (`cfg.pr_auto_post` /
`cfg.issue_auto_post`). Deep-review tasks stop at RUNNING — the agent team
runs async and posts its own comment.
"""
from __future__ import annotations

import logging
from typing import Any

from auto_review.config import AutoReviewConfig
from auto_review.executor import Reviewer, ReviewResult
from auto_review.poster import post_review
from auto_review.state import AutoReviewTask, StateStore, TaskKind, TaskState

_log = logging.getLogger("auto_review.dispatcher")


class ReviewDispatcher:
    """Pulls DISCOVERED tasks, routes them, executes, posts, updates state."""

    def __init__(
        self,
        *,
        cfg: AutoReviewConfig,
        store: StateStore,
        client,            # GiteaClient-like with get_pr / get_issue / comment_on_issue
        light: Reviewer,
        deep: Reviewer,
    ):
        self._cfg = cfg
        self._store = store
        self._client = client
        self._light = light
        self._deep = deep

    def process_one(self, task: AutoReviewTask) -> str:
        """Drive one task end-to-end; return final TaskState value string."""
        # PR-18g: parallel-job guard — 同 (repo, number) 已有 active task 时跳过
        # 防止同 PR 不同 head_sha 并发执行 (port KAXY-3022/Agent-manager d348400)
        active = self._store.find_active_task(task.repo, task.number)
        if active is not None and active.id != task.id:
            self._store.update_state(
                task.id, TaskState.DEDUPE_SKIPPED,
                error_message=f"active task id={active.id} state={active.state.value}",
            )
            return TaskState.DEDUPE_SKIPPED.value

        try:
            self._store.update_state(task.id, TaskState.QUEUED)
            context = self._fetch_context(task)
            self._store.update_state(task.id, TaskState.RUNNING)
        except Exception as exc:
            _log.exception("fetch_context failed")
            self._store.update_state(
                task.id, TaskState.FAILED,
                error_message=f"fetch_context: {type(exc).__name__}: {exc}",
            )
            return TaskState.FAILED.value

        # Route
        go_deep = (
            task.kind == TaskKind.PR
            and context.get("changed_files", 0) > self._cfg.light_deep_threshold
        )

        if go_deep:
            return self._run_deep(task, context)
        return self._run_light(task, context)

    def process_pending(self, limit: int = 5) -> list[str]:
        """Pop up to `limit` DISCOVERED tasks (FIFO) and process them."""
        pending = self._store.list_by_state(TaskState.DISCOVERED)[:limit]
        return [self.process_one(t) for t in pending]

    # ----- internals -----

    def _fetch_context(self, task: AutoReviewTask) -> dict[str, Any]:
        if task.kind == TaskKind.PR:
            payload = self._client.get_pr(task.repo, task.number)
            return {
                "html_url": payload.get("html_url")
                    or f"https://git.k2lab.ai/{task.repo}/pulls/{task.number}",
                "changed_files": payload.get("changed_files", 0),
                "title": payload.get("title", ""),
            }
        payload = self._client.get_issue(task.repo, task.number)
        return {
            "html_url": payload.get("html_url")
                or f"https://git.k2lab.ai/{task.repo}/issues/{task.number}",
            "changed_files": 0,
            "title": payload.get("title", ""),
        }

    def _run_deep(self, task: AutoReviewTask, context: dict[str, Any]) -> str:
        result = self._deep.review(task, context)
        if not result.success:
            self._store.update_state(
                task.id, TaskState.FAILED,
                error_message=result.error, runtime=result.runtime,
            )
            return TaskState.FAILED.value
        # Spawn succeeded — leave at RUNNING; team will post async itself.
        self._store.update_state(
            task.id, TaskState.RUNNING, runtime=result.runtime,
        )
        return TaskState.RUNNING.value

    def _run_light(self, task: AutoReviewTask, context: dict[str, Any]) -> str:
        result = self._light.review(task, context)
        if not result.success:
            self._store.update_state(
                task.id, TaskState.FAILED,
                error_message=result.error, runtime=result.runtime,
            )
            return TaskState.FAILED.value

        self._store.update_state(
            task.id, TaskState.REVIEW_DONE, runtime=result.runtime,
        )

        auto_post = (
            self._cfg.pr_auto_post if task.kind == TaskKind.PR
            else self._cfg.issue_auto_post
        )
        if not auto_post:
            return TaskState.REVIEW_DONE.value

        try:
            post_result = post_review(client=self._client, task=task, review=result)
        except Exception as exc:
            _log.exception("post_review failed")
            self._store.update_state(
                task.id, TaskState.FAILED,
                error_message=f"post failed: {type(exc).__name__}: {exc}",
                runtime=result.runtime,
            )
            return TaskState.FAILED.value

        # PR-18g: post_review may return {"skipped": True, ...} when an
        # identical comment already exists on the PR/issue. Treat as POSTED
        # (the comment IS on the PR — just by an earlier run) but record why
        # in error_message for ops visibility.
        dup_note = None
        if isinstance(post_result, dict) and post_result.get("skipped"):
            dup_note = f"duplicate-skipped: existing_comment_id={post_result.get('existing_comment_id')}"

        self._store.update_state(
            task.id, TaskState.POSTED, runtime=result.runtime,
            error_message=dup_note,
        )
        return TaskState.POSTED.value
