"""Helpers for reading + writing tmux pane content (PR-19b-1).

Wraps ``tmux capture-pane`` (read) and ``tmux send-keys -l`` (write literal
text including newlines, then a separate Enter keypress). Used by the
freeform agent dashboard endpoints to sync a pane bidirectionally with the
frontend modal.

``runner`` is injectable for tests; defaults to ``subprocess.run``.
"""
from __future__ import annotations

import subprocess
from typing import Callable


class TmuxPaneError(RuntimeError):
    """Raised when a tmux capture-pane or send-keys call returns non-zero."""


def capture_pane(
    session: str,
    *,
    lines: int = 1000,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Return the visible + scrollback content of a tmux session's pane.

    ``lines`` controls scrollback depth (passed as ``-S -<lines>``). The
    default 1000 should comfortably cover a multi-step chat session.
    """
    argv = [
        "tmux", "capture-pane",
        "-t", session,
        "-p",  # print to stdout
        "-S", f"-{int(lines)}",
    ]
    cp = runner(argv, capture_output=True, text=True)
    if cp.returncode != 0:
        raise TmuxPaneError(
            f"capture-pane failed for {session!r}: {(cp.stderr or '').strip()}"
        )
    return cp.stdout or ""


def send_keys_text(
    session: str,
    text: str,
    *,
    append_enter: bool = True,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Send a literal text payload (incl. newlines) into a tmux session.

    Uses ``send-keys -l`` (literal) so newlines in the payload are sent as
    actual newline characters into the pane stdin (not interpreted as Enter
    keypresses by tmux). When ``append_enter=True`` (default), a single
    ``Enter`` is sent after the text to submit the line.
    """
    argv = [
        "tmux", "send-keys",
        "-t", session,
        "-l",
        text,
    ]
    cp = runner(argv, capture_output=True, text=True)
    if cp.returncode != 0:
        raise TmuxPaneError(
            f"send-keys failed for {session!r}: {(cp.stderr or '').strip()}"
        )

    if append_enter:
        cp2 = runner(
            ["tmux", "send-keys", "-t", session, "Enter"],
            capture_output=True, text=True,
        )
        if cp2.returncode != 0:
            raise TmuxPaneError(
                f"send-keys Enter failed for {session!r}: {(cp2.stderr or '').strip()}"
            )
