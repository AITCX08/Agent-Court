"""SQLite-backed task state machine for the auto-review pipeline.

Single ``auto_review_tasks`` table keyed by a unique ``dedupe_key``:
- PR: ``owner/repo#number@head_sha`` — pushing a new commit creates a new task
- Issue: ``owner/repo#number`` — issues have no head_sha

State transitions live in PR-18d (router). PR-18b only does discovery + enqueue.
"""
from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class TaskState(str, enum.Enum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW_DONE = "review_done"
    POSTED = "posted"
    FAILED = "failed"
    DEDUPE_SKIPPED = "dedupe_skipped"


class TaskKind(str, enum.Enum):
    PR = "pr"
    ISSUE = "issue"


class DedupeSkipped(RuntimeError):
    """Raised by enqueue() when dedupe_key already exists in the store."""


@dataclass(frozen=True, slots=True)
class AutoReviewTask:
    id: int
    dedupe_key: str
    kind: TaskKind
    repo: str
    number: int
    head_sha: Optional[str]
    state: TaskState
    runtime: Optional[str]
    discovered_at: str
    last_event_at: str
    error_message: Optional[str]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auto_review_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    head_sha        TEXT,
    state           TEXT NOT NULL,
    runtime         TEXT,
    discovered_at   TEXT NOT NULL,
    last_event_at   TEXT NOT NULL,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_review_state ON auto_review_tasks(state);
CREATE INDEX IF NOT EXISTS idx_auto_review_repo_number ON auto_review_tasks(repo, number);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_dedupe_key(kind: TaskKind, repo: str, number: int, head_sha: Optional[str]) -> str:
    if kind == TaskKind.PR:
        if not head_sha:
            raise ValueError("PR tasks require non-empty head_sha")
        return f"{repo}#{number}@{head_sha}"
    if head_sha is not None:
        raise ValueError("Issue tasks must not carry head_sha")
    return f"{repo}#{number}"


class StateStore:
    """Thin DAO over a single SQLite file (or ``:memory:`` for tests)."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM auto_review_tasks").fetchone()[0]

    def enqueue(
        self,
        *,
        kind: TaskKind,
        repo: str,
        number: int,
        head_sha: Optional[str],
    ) -> AutoReviewTask:
        dedupe_key = _make_dedupe_key(kind, repo, number, head_sha)
        now = _utc_now_iso()
        try:
            cursor = self._conn.execute(
                """INSERT INTO auto_review_tasks
                   (dedupe_key, kind, repo, number, head_sha, state, runtime, discovered_at, last_event_at, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)""",
                (
                    dedupe_key,
                    kind.value,
                    repo,
                    number,
                    head_sha,
                    TaskState.DISCOVERED.value,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DedupeSkipped(f"dedupe_key already exists: {dedupe_key}") from exc

        return self._row_to_task(self._fetch_by_id(cursor.lastrowid))

    def get_by_dedupe_key(self, dedupe_key: str) -> Optional[AutoReviewTask]:
        row = self._conn.execute(
            "SELECT * FROM auto_review_tasks WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def update_state(
        self,
        task_id: int,
        new_state: TaskState,
        *,
        error_message: Optional[str] = None,
        runtime: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """UPDATE auto_review_tasks
               SET state = ?, error_message = ?, runtime = COALESCE(?, runtime), last_event_at = ?
               WHERE id = ?""",
            (new_state.value, error_message, runtime, _utc_now_iso(), task_id),
        )
        self._conn.commit()

    def list_by_state(self, state: TaskState) -> list[AutoReviewTask]:
        rows = self._conn.execute(
            "SELECT * FROM auto_review_tasks WHERE state = ? ORDER BY id ASC",
            (state.value,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def known_pr_keys(self) -> list[tuple[str, int]]:
        """For active polling: list distinct (repo, number) of PR tasks we've seen."""
        rows = self._conn.execute(
            "SELECT DISTINCT repo, number FROM auto_review_tasks WHERE kind = ? ORDER BY repo, number",
            (TaskKind.PR.value,),
        ).fetchall()
        return [(r["repo"], r["number"]) for r in rows]

    def _fetch_by_id(self, task_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM auto_review_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None, f"task id {task_id} disappeared after insert"
        return row

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> AutoReviewTask:
        return AutoReviewTask(
            id=row["id"],
            dedupe_key=row["dedupe_key"],
            kind=TaskKind(row["kind"]),
            repo=row["repo"],
            number=row["number"],
            head_sha=row["head_sha"],
            state=TaskState(row["state"]),
            runtime=row["runtime"],
            discovered_at=row["discovered_at"],
            last_event_at=row["last_event_at"],
            error_message=row["error_message"],
        )
