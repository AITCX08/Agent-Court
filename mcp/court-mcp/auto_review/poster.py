"""Post auto-review results back to Gitea as a Markdown comment.

Gitea treats PR comments and issue comments via the same endpoint
(``/repos/{owner}/{name}/issues/{number}/comments``), so we use
``GiteaClient.comment_on_issue`` for both PR and Issue tasks.

Failures (network / API errors) propagate to the caller (the dispatcher
decides how to mark the task — usually transition to FAILED).
"""
from __future__ import annotations

from typing import Any

from auto_review.executor import ReviewResult
from auto_review.state import AutoReviewTask


def format_review_comment(task: AutoReviewTask, review: ReviewResult) -> str:
    """Wrap a ReviewResult.output in a Markdown comment with a runtime header.

    Header carries enough metadata for a human reviewer to know which run produced
    this comment: runtime (codex/claude/team) and (for PRs) the 8-char head SHA.
    """
    header_parts = [f"**Auto-review** · runtime: `{review.runtime}`"]
    if task.head_sha:
        header_parts.append(f"head: `{task.head_sha[:8]}`")
    header = " · ".join(header_parts)
    return f"{header}\n\n---\n\n{review.output}"


def _normalize_for_dedupe(text: str) -> str:
    """Whitespace-collapse for body comparison.

    Mirrors KAXY-3022/Agent-manager normalize_comment_for_dedupe — turns
    arbitrary whitespace (newlines, tabs, multiple spaces) into single spaces
    so cosmetic formatting differences don't bypass dedupe.
    """
    return " ".join(str(text or "").split())


def post_review(*, client, task: AutoReviewTask, review: ReviewResult) -> dict[str, Any]:
    """Post the formatted review comment via GiteaClient, with dup-comment guard.

    Before posting, fetches existing comments on the PR/issue. If any has an
    identical normalized body, returns ``{"skipped": True, "reason": "duplicate",
    "existing_comment_id": <id>}`` without posting (ports KAXY-3022 1be8848).

    If ``list_issue_comments`` itself raises (Gitea down, network blip), the
    dup check is skipped — we still try to post so the failure mode is
    "post twice" not "lose review entirely".

    Otherwise returns the Gitea comment payload from comment_on_issue.
    Raises any exception from comment_on_issue (caller handles state machine).
    """
    body = format_review_comment(task, review)
    target = _normalize_for_dedupe(body)

    try:
        existing = client.list_issue_comments(task.repo, task.number)
    except Exception:
        existing = []

    for comment in existing or []:
        if not isinstance(comment, dict):
            continue
        existing_body = comment.get("body") or ""
        if _normalize_for_dedupe(existing_body) == target:
            return {
                "skipped": True,
                "reason": "duplicate",
                "existing_comment_id": comment.get("id"),
            }

    return client.comment_on_issue(task.repo, task.number, body)
