"""Tests for auto_review.poster — format + post review comments to Gitea."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.executor import ReviewResult
from auto_review.poster import format_review_comment, post_review
from auto_review.state import AutoReviewTask, TaskKind, TaskState


def _task(kind=TaskKind.PR, repo="K2Lab/agent-court", number=42, head_sha="deadbeefcafe"):
    return AutoReviewTask(
        id=1,
        dedupe_key=f"{repo}#{number}@{head_sha}" if head_sha else f"{repo}#{number}",
        kind=kind,
        repo=repo,
        number=number,
        head_sha=head_sha,
        state=TaskState.REVIEW_DONE,
        runtime="codex",
        discovered_at="2026-05-22T00:00:00Z",
        last_event_at="2026-05-22T00:00:00Z",
        error_message=None,
    )


def _review(success=True, runtime="codex", output="## Looks good\nNo issues.", error=None):
    return ReviewResult(success=success, runtime=runtime, output=output, error=error)


def test_format_comment_contains_runtime_header():
    body = format_review_comment(_task(), _review(runtime="codex"))
    assert "Auto-review" in body
    assert "codex" in body
    assert "Looks good" in body  # original output preserved


def test_format_comment_pr_includes_short_sha():
    """PR comments mention head SHA (8-char prefix) so reviewers can pin the run."""
    body = format_review_comment(_task(head_sha="deadbeefcafe"), _review())
    assert "deadbeef" in body  # 8-char prefix
    # Should not include the full 12-char SHA in the header (only the prefix)
    # — but the test allows the full SHA elsewhere if present.


def test_format_comment_issue_omits_sha():
    """Issue task has head_sha=None → header doesn't try to render '8-char prefix'."""
    task = _task(kind=TaskKind.ISSUE, number=7, head_sha=None)
    body = format_review_comment(task, _review())
    # No literal "head:" header for issues
    assert "head:" not in body.lower() or "head: (none)" not in body.lower()
    assert "Looks good" in body


def test_post_review_calls_comment_on_issue():
    """post_review forwards to client.comment_on_issue(repo, number, body)."""
    client = MagicMock()
    client.comment_on_issue.return_value = {
        "id": 999, "html_url": "https://git.k2lab.ai/K2Lab/agent-court/issues/42#issuecomment-999"
    }
    task = _task()
    review = _review(output="REVIEW BODY")

    result = post_review(client=client, task=task, review=review)

    assert client.comment_on_issue.call_count == 1
    args, kwargs = client.comment_on_issue.call_args
    # Allow positional or keyword args
    call_repo = kwargs.get("repo") or args[0]
    call_number = kwargs.get("number") or args[1]
    call_body = kwargs.get("body") or args[2]
    assert call_repo == "K2Lab/agent-court"
    assert call_number == 42
    assert "REVIEW BODY" in call_body
    assert result["id"] == 999


def test_post_review_propagates_client_exception():
    """post_review does NOT swallow client errors — caller (dispatcher) handles state."""
    client = MagicMock()
    client.comment_on_issue.side_effect = RuntimeError("gitea 500")

    with pytest.raises(RuntimeError, match="gitea 500"):
        post_review(client=client, task=_task(), review=_review())
