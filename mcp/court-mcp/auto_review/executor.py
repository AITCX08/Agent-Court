"""Light/deep review executors for the auto-review pipeline.

LightExecutor runs ``codex exec`` or ``claude`` as a child process and treats
stdout as a Markdown review. DeepExecutor passes through to the existing
``agent_spawn.AgentSpawner`` to start a tmux-based agent team.

Both return a unified ``ReviewResult`` so the dispatcher can route uniformly.
"""
from __future__ import annotations

import logging
import subprocess
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


class LightExecutor:
    """Runs codex exec / claude CLI; stdout is the review markdown."""

    def __init__(
        self,
        *,
        codex_cmd: tuple[str, ...] = ("codex", "exec"),
        claude_cmd: tuple[str, ...] = ("claude",),
        timeout_sec: int = 600,
        prefer: str = "codex",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self._codex_cmd = list(codex_cmd)
        self._claude_cmd = list(claude_cmd)
        self._timeout = timeout_sec
        self._prefer = prefer
        self._runner = runner

    def review(self, task: AutoReviewTask, context: dict[str, Any]) -> ReviewResult:
        argv = list(self._codex_cmd if self._prefer == "codex" else self._claude_cmd)
        prompt = _build_prompt(task, context)
        try:
            cp = self._runner(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return ReviewResult(
                success=False, runtime=self._prefer, output="",
                error=f"timeout after {self._timeout}s",
            )
        except FileNotFoundError as exc:
            return ReviewResult(
                success=False, runtime=self._prefer, output="",
                error=f"{argv[0]} not found on PATH: {exc}",
            )
        except Exception as exc:
            _log.exception("LightExecutor unexpected error")
            return ReviewResult(
                success=False, runtime=self._prefer, output="",
                error=f"{type(exc).__name__}: {exc}",
            )

        if cp.returncode != 0:
            return ReviewResult(
                success=False, runtime=self._prefer, output=cp.stdout or "",
                error=f"exit {cp.returncode}: {(cp.stderr or '').strip()}",
            )
        return ReviewResult(
            success=True, runtime=self._prefer, output=cp.stdout or "",
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
