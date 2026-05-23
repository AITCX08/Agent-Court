"""Helpers for reading + writing tmux pane content (PR-19b-1 / PR-19e).

- ``capture_pane``: tmux capture-pane → str
- ``send_keys_text``: tmux send-keys -l <text> [+ Enter]
- ``paste_buffer_to_pane`` (PR-19e): 用 tmux set-buffer + paste-buffer 模拟
  剪贴板粘贴, 适合给 claude/codex TUI 投递 multi-line prompt 让它当一次 paste
  事件接收并 submit (而不是 newline)

``runner`` is injectable for tests; defaults to ``subprocess.run``.
"""
from __future__ import annotations

import subprocess
import uuid
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


def paste_buffer_to_pane(
    session: str,
    text: str,
    *,
    append_enter: bool = True,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Paste a (multi-line) text into a tmux pane via set-buffer + paste-buffer.

    PR-19e: claude / codex TUI 把 ``send-keys -l <multi-line>`` 当多行输入
    (newline = newline, 不 submit), 必须等 user 按 Enter (但 Enter 也是 newline).
    用 paste-buffer (= bracketed paste 模式) 让 TUI 把整段当一次 paste event
    收, 而不是逐字 keypress, 然后单独的 Enter 才能 submit.

    实现: ``set-buffer -b <id> <text>`` → ``paste-buffer -b <id> -d -t <session>``
    (-d = paste 后立即删 buffer, 干净), 接着可选 send-keys Enter.
    """
    buf_name = f"freeform-{uuid.uuid4().hex[:8]}"
    set_cp = runner(
        ["tmux", "set-buffer", "-b", buf_name, text],
        capture_output=True, text=True,
    )
    if set_cp.returncode != 0:
        raise TmuxPaneError(
            f"set-buffer failed for {session!r}: {(set_cp.stderr or '').strip()}"
        )

    paste_cp = runner(
        ["tmux", "paste-buffer", "-b", buf_name, "-d", "-t", session],
        capture_output=True, text=True,
    )
    if paste_cp.returncode != 0:
        raise TmuxPaneError(
            f"paste-buffer failed for {session!r}: {(paste_cp.stderr or '').strip()}"
        )

    if append_enter:
        ent_cp = runner(
            ["tmux", "send-keys", "-t", session, "Enter"],
            capture_output=True, text=True,
        )
        if ent_cp.returncode != 0:
            raise TmuxPaneError(
                f"send-keys Enter failed for {session!r}: {(ent_cp.stderr or '').strip()}"
            )
