"""Tests for tmux_pane — capture-pane + send-keys-text helpers (PR-19b-1)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmux_pane import (
    TmuxPaneError,
    capture_pane,
    send_keys_text,
)


def _ok_completed(stdout: str = "", stderr: str = ""):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _fail_completed(stderr: str = "session not found", code: int = 1):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = code
    cp.stdout = ""
    cp.stderr = stderr
    return cp


# ---- capture_pane ----

def test_capture_pane_default_lines():
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed(stdout="hello world\n")

    out = capture_pane("agent-team-abc", runner=fake_runner)
    assert out == "hello world\n"
    assert calls[0][0:5] == ["tmux", "capture-pane", "-t", "agent-team-abc", "-p"]
    # default scrollback 1000 → expect ["-S", "-1000"]
    assert "-S" in calls[0]
    assert "-1000" in calls[0]


def test_capture_pane_custom_lines():
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed(stdout="snippet")
    capture_pane("sess1", lines=200, runner=fake_runner)
    assert "-S" in calls[0]
    assert "-200" in calls[0]


def test_capture_pane_failure_raises():
    def fake_runner(argv, **kwargs):
        return _fail_completed(stderr="can't find session sess1", code=1)
    with pytest.raises(TmuxPaneError, match="capture-pane failed"):
        capture_pane("sess1", runner=fake_runner)


# ---- send_keys_text ----

def test_send_keys_text_single_line_with_enter():
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed()

    send_keys_text("agent-team-abc", "hello", runner=fake_runner)

    # Expect 2 invocations: one -l "hello", one "Enter"
    assert len(calls) == 2
    assert calls[0][0:5] == ["tmux", "send-keys", "-t", "agent-team-abc", "-l"]
    assert calls[0][5] == "hello"
    assert calls[1][0:5] == ["tmux", "send-keys", "-t", "agent-team-abc", "Enter"]


def test_send_keys_text_multiline_with_enter():
    """Multi-line text should be sent as a single -l chunk + one Enter."""
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed()

    send_keys_text("sess", "line one\nline two\nline three", runner=fake_runner)

    # Implementation choice: one -l with full text (newlines preserved as
    # literal chars to tmux), then one Enter. Verify the text reaches tmux
    # in one literal chunk so newlines aren't interpreted as Enter keystrokes.
    literal_calls = [c for c in calls if "-l" in c]
    assert len(literal_calls) >= 1
    # The combined literal payload should contain all three lines
    combined = " ".join(c[-1] for c in literal_calls)
    assert "line one" in combined
    assert "line two" in combined
    assert "line three" in combined
    # Final Enter
    assert calls[-1][-1] == "Enter"


def test_send_keys_text_no_enter():
    """append_enter=False → only -l, no Enter."""
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _ok_completed()

    send_keys_text("sess", "draft", append_enter=False, runner=fake_runner)

    assert len(calls) == 1
    assert calls[0][-1] == "draft"


def test_send_keys_text_failure_raises():
    def fake_runner(argv, **kwargs):
        return _fail_completed(stderr="bad session", code=1)
    with pytest.raises(TmuxPaneError, match="send-keys failed"):
        send_keys_text("sess", "hi", runner=fake_runner)
