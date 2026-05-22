"""Read-only status query for the auto-review frontend badge UI.

PR-18b ``StateStore`` may have multiple tasks per (repo, number) when a PR
gets new commits (each head_sha is its own row). For the dashboard badge,
the UI cares about "what's the current state of this PR's auto-review?" —
so we collapse rows by (repo, number), keeping the most-recent ``last_event_at``.
"""
from __future__ import annotations

from typing import Any

from auto_review.state import StateStore


def build_status_map(store: StateStore) -> dict[str, dict[str, Any]]:
    """Return ``{"owner/repo#number": {state, kind, runtime, head_sha, last_event_at, error_message}}``."""
    rows = store._conn.execute(
        """SELECT repo, number, kind, state, runtime, head_sha,
                  last_event_at, error_message
           FROM auto_review_tasks
           ORDER BY last_event_at DESC, id DESC"""
    ).fetchall()

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r['repo']}#{r['number']}"
        if key in out:
            continue  # already saw a more recent row for this PR/issue
        out[key] = {
            "state": r["state"],
            "kind": r["kind"],
            "runtime": r["runtime"],
            "head_sha": r["head_sha"],
            "last_event_at": r["last_event_at"],
            "error_message": r["error_message"],
        }
    return out
