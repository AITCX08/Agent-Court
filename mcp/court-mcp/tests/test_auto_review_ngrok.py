"""Tests for auto_review.ngrok — mocked subprocess + urlopen.

We do NOT spawn real ngrok; all subprocess.Popen and urllib.request.urlopen
calls are injected via kwargs.
"""
from __future__ import annotations

import io
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.ngrok import NgrokTunnel, NgrokTimeoutError


def _fake_popen_factory(*, poll_side: list[Any] | None = None):
    """Returns a callable that mimics subprocess.Popen, returning a MagicMock proc."""
    state = {"argv": None, "kwargs": None}

    def fake_popen(argv, **kwargs):
        state["argv"] = list(argv)
        state["kwargs"] = kwargs
        proc = MagicMock()
        proc.poll.side_effect = poll_side if poll_side else [None] * 10
        proc.wait.return_value = 0
        return proc

    fake_popen.state = state
    return fake_popen


def _fake_urlopen_factory(responses: list[dict | Exception]):
    """Returns a callable that mimics urllib.request.urlopen, returning canned JSON."""
    calls = {"count": 0}

    def fake_urlopen(url, timeout=None):
        i = calls["count"]
        calls["count"] += 1
        item = responses[min(i, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        body = json.dumps(item).encode("utf-8")
        fp = io.BytesIO(body)
        fp.__enter__ = lambda self: self  # type: ignore[attr-defined]
        fp.__exit__ = lambda self, *a: False  # type: ignore[attr-defined]
        return fp

    fake_urlopen.calls = calls
    return fake_urlopen


def test_start_invokes_popen_with_correct_argv():
    fp = _fake_popen_factory()
    tunnel = NgrokTunnel(port=48731, popen=fp)
    tunnel.start()
    assert fp.state["argv"] == ["ngrok", "http", "48731"]
    assert tunnel.is_running()


def test_start_with_extra_args():
    fp = _fake_popen_factory()
    tunnel = NgrokTunnel(
        port=48731,
        extra_args=["--domain=foo.ngrok-free.app"],
        popen=fp,
    )
    tunnel.start()
    assert fp.state["argv"] == [
        "ngrok",
        "http",
        "48731",
        "--domain=foo.ngrok-free.app",
    ]


def test_start_is_idempotent():
    fp = _fake_popen_factory()
    tunnel = NgrokTunnel(port=48731, popen=fp)
    tunnel.start()
    proc1 = tunnel._process
    tunnel.start()
    proc2 = tunnel._process
    assert proc1 is proc2


def test_public_url_returns_https_tunnel():
    fp = _fake_popen_factory()
    fu = _fake_urlopen_factory(
        [
            {
                "tunnels": [
                    {"public_url": "https://abc.ngrok-free.app", "proto": "https"},
                    {"public_url": "http://abc.ngrok-free.app", "proto": "http"},
                ]
            }
        ]
    )
    tunnel = NgrokTunnel(port=48731, popen=fp, urlopen=fu)
    tunnel.start()
    url = tunnel.public_url(timeout=2.0, poll_interval=0.01)
    assert url == "https://abc.ngrok-free.app"


def test_public_url_skips_non_https_then_finds_https():
    """First response has no https; second response has it."""
    fp = _fake_popen_factory()
    fu = _fake_urlopen_factory(
        [
            {"tunnels": [{"public_url": "http://a", "proto": "http"}]},
            {"tunnels": [{"public_url": "https://b", "proto": "https"}]},
        ]
    )
    tunnel = NgrokTunnel(port=48731, popen=fp, urlopen=fu)
    tunnel.start()
    url = tunnel.public_url(timeout=2.0, poll_interval=0.01)
    assert url == "https://b"
    assert fu.calls["count"] >= 2


def test_public_url_timeout_raises():
    fp = _fake_popen_factory()
    fu = _fake_urlopen_factory(
        [{"tunnels": [{"public_url": "http://only", "proto": "http"}]}]
    )
    tunnel = NgrokTunnel(port=48731, popen=fp, urlopen=fu)
    tunnel.start()
    with pytest.raises(NgrokTimeoutError):
        tunnel.public_url(timeout=0.1, poll_interval=0.01)


def test_stop_terminates_then_waits():
    fp = _fake_popen_factory()
    tunnel = NgrokTunnel(port=48731, popen=fp)
    tunnel.start()
    proc = tunnel._process
    tunnel.stop(timeout=2.0)
    proc.terminate.assert_called_once()
    proc.wait.assert_called()
    assert not tunnel.is_running() or proc.poll() is not None
