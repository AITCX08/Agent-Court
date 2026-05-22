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


def post_review(*, client, task: AutoReviewTask, review: ReviewResult) -> dict[str, Any]:
    """Post the formatted review comment via GiteaClient.

    Returns the Gitea comment payload (caller can use 'id' / 'html_url').
    Raises any exception from the client; the dispatcher catches and marks FAILED.
    """
    body = format_review_comment(task, review)
    return client.comment_on_issue(task.repo, task.number, body)
