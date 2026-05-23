"""Light/deep review executors for the auto-review pipeline.

LightExecutor runs ``codex exec`` or ``claude`` as a child process and treats
stdout as a Markdown review. DeepExecutor passes through to the existing
``agent_spawn.AgentSpawner`` to start a tmux-based agent team.

Both return a unified ``ReviewResult`` so the dispatcher can route uniformly.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from auto_review.state import AutoReviewTask, TaskKind

_log = logging.getLogger("auto_review.executor")


@dataclass(frozen=True, slots=True)
class ReviewResult:
    success: bool
    runtime: str
    output: str
    error: str | None = None


class Reviewer(Protocol):
    def review(self, task: AutoReviewTask, context: dict[str, Any]) -> ReviewResult: ...


_PROMPT_TEMPLATE = """Please review this {kind}:

- Repository: {repo}
- Number: #{number}
- URL: {url}
- Head SHA: {head_sha}
- Changed files: {changed_files}
- Title: {title}

Produce a concise Markdown review covering: correctness, scope, tests, risks.
Be specific (file:line where applicable). Output the review only — no preamble.
"""


def _build_prompt(task: AutoReviewTask, context: dict[str, Any]) -> str:
    return _PROMPT_TEMPLATE.format(
        kind="PR" if task.kind == TaskKind.PR else "issue",
        repo=task.repo,
        number=task.number,
        url=context.get("html_url", ""),
        head_sha=task.head_sha or "(none)",
        changed_files=context.get("changed_files", 0),
        title=context.get("title", ""),
    )


TRANSIENT_ERROR_PATTERNS = (
    "command timed out",
    "connect: operation timed out",
    "connection timed out",
    "read: connection reset by peer",
    "connection reset by peer",
    "no such host",
    "network is unreachable",
    "temporary failure",
    "tls handshake timeout",
    "i/o timeout",
)
_DEFAULT_RETRY_LIMIT = 3
_DEFAULT_RETRY_BACKOFF_SEC = 2.0


def _is_transient_error(text: str = "", exc: BaseException | None = None) -> bool:
    """Match KAXY-3022/Agent-manager 21fa822 transient detection.

    Checks both a free-form text blob (typically subprocess stderr) and an
    exception message; returns True if any TRANSIENT_ERROR_PATTERNS substring
    appears in either, case-insensitive.
    """
    haystack = (text or "").lower()
    if exc is not None:
        haystack += " " + str(exc).lower()
    return any(p in haystack for p in TRANSIENT_ERROR_PATTERNS)


class LightExecutor:
    """Runs codex exec / claude CLI; stdout is the review markdown.

    PR-18g: transient-error retry. If stderr or exception message matches a
    known network glitch pattern (timeout, connection reset, DNS, TLS handshake),
    retry up to ``retry_limit`` times with linear backoff
    (``retry_backoff_sec * attempt`` seconds between attempts). FileNotFoundError
    (CLI not on PATH) is never retried — that's a config issue, not transient.
    """

    def __init__(
        self,
        *,
        codex_cmd: tuple[str, ...] = ("codex", "exec"),
        claude_cmd: tuple[str, ...] = ("claude",),
        timeout_sec: int = 600,
        prefer: str = "codex",
        retry_limit: int = _DEFAULT_RETRY_LIMIT,
        retry_backoff_sec: float = _DEFAULT_RETRY_BACKOFF_SEC,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._codex_cmd = list(codex_cmd)
        self._claude_cmd = list(claude_cmd)
        self._timeout = timeout_sec
        self._prefer = prefer
        self._retry_limit = max(1, int(retry_limit))
        self._retry_backoff = float(retry_backoff_sec)
        self._runner = runner
        self._sleep = sleep

    def review(self, task: AutoReviewTask, context: dict[str, Any]) -> ReviewResult:
        argv = list(self._codex_cmd if self._prefer == "codex" else self._claude_cmd)
        prompt = _build_prompt(task, context)
        last_error: str = "retry budget exhausted"

        for attempt in range(1, self._retry_limit + 1):
            try:
                cp = self._runner(
                    argv, input=prompt, capture_output=True,
                    text=True, timeout=self._timeout,
                )
            except subprocess.TimeoutExpired:
                # subprocess timeout is inherently transient (network glitch /
                # upstream LLM slow) — always retry within the budget.
                last_error = f"timeout after {self._timeout}s"
                if attempt < self._retry_limit:
                    _log.warning(
                        "LightExecutor transient timeout, attempt %d/%d — retrying",
                        attempt, self._retry_limit,
                    )
                    self._sleep(self._retry_backoff * attempt)
                    continue
                return ReviewResult(
                    success=False, runtime=self._prefer, output="", error=last_error,
                )
            except FileNotFoundError as exc:
                # CLI binary missing — not transient, fail immediately
                return ReviewResult(
                    success=False, runtime=self._prefer, output="",
                    error=f"{argv[0]} not found on PATH: {exc}",
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retry_limit and _is_transient_error("", exc):
                    _log.warning(
                        "LightExecutor transient %s, attempt %d/%d — retrying",
                        type(exc).__name__, attempt, self._retry_limit,
                    )
                    self._sleep(self._retry_backoff * attempt)
                    continue
                _log.exception("LightExecutor unexpected error")
                return ReviewResult(
                    success=False, runtime=self._prefer, output="", error=last_error,
                )

            if cp.returncode != 0:
                stderr_text = (cp.stderr or "").strip()
                last_error = f"exit {cp.returncode}: {stderr_text}"
                if attempt < self._retry_limit and _is_transient_error(stderr_text):
                    _log.warning(
                        "LightExecutor transient stderr (exit %d), attempt %d/%d — retrying",
                        cp.returncode, attempt, self._retry_limit,
                    )
                    self._sleep(self._retry_backoff * attempt)
                    continue
                return ReviewResult(
                    success=False, runtime=self._prefer,
                    output=cp.stdout or "", error=last_error,
                )

            return ReviewResult(
                success=True, runtime=self._prefer, output=cp.stdout or "",
            )

        return ReviewResult(
            success=False, runtime=self._prefer, output="", error=last_error,
        )


class DeepExecutor:
    """Spawns an agent team via existing AgentSpawner; non-blocking."""

    def __init__(self, *, spawner):
        self._spawner = spawner

    def review(self, task: AutoReviewTask, context: dict[str, Any]) -> ReviewResult:
        try:
            res = self._spawner.spawn(
                repo=task.repo,
                number=task.number,
                kind="pr" if task.kind == TaskKind.PR else "issue",
                url=context.get("html_url", ""),
            )
        except Exception as exc:
            return ReviewResult(
                success=False, runtime="team", output="",
                error=f"{type(exc).__name__}: {exc}",
            )
        team_id = res.get("team_id", "") if isinstance(res, dict) else ""
        return ReviewResult(success=True, runtime="team", output=team_id)
