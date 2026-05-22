"""ngrok subprocess wrapper for exposing the auto-review webhook publicly.

Wraps ``ngrok http <port>`` as a child process and exposes the public HTTPS
URL via ngrok's local API at ``http://127.0.0.1:4040/api/tunnels``. All side
effects (Popen, urlopen) are injectable for testing.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.request
from typing import Callable, Iterable, Optional

_log = logging.getLogger("auto_review.ngrok")


class NgrokTimeoutError(RuntimeError):
    """Raised when public_url() does not see an https tunnel within the deadline."""


class NgrokTunnel:
    """Manages an ``ngrok http <port>`` child process and queries its public URL."""

    def __init__(
        self,
        port: int,
        *,
        extra_args: Iterable[str] = (),
        api_port: int = 4040,
        popen: Optional[Callable] = None,
        urlopen: Optional[Callable] = None,
    ):
        self._port = port
        self._extra_args = list(extra_args)
        self._api_port = api_port
        self._popen = popen or subprocess.Popen
        self._urlopen = urlopen or urllib.request.urlopen
        self._process = None

    def start(self) -> None:
        """Spawn the ngrok process. No-op if already running."""
        if self.is_running():
            return
        argv = ["ngrok", "http", str(self._port), *self._extra_args]
        self._process = self._popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the ngrok process and reap. No-op if never started or exited."""
        if self._process is None:
            return
        if self._process.poll() is not None:
            self._process = None
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
        except Exception:
            try:
                self._process.kill()
                self._process.wait(timeout=timeout)
            except Exception:
                _log.exception("ngrok kill failed")
        finally:
            self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def public_url(self, timeout: float = 10.0, poll_interval: float = 0.5) -> str:
        """Poll ngrok's local API until an https tunnel is reported, or timeout."""
        deadline = time.monotonic() + timeout
        api_url = f"http://127.0.0.1:{self._api_port}/api/tunnels"
        last_seen = None

        while time.monotonic() < deadline:
            try:
                with self._urlopen(api_url, timeout=1.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_seen = exc
                time.sleep(poll_interval)
                continue

            tunnels = data.get("tunnels") if isinstance(data, dict) else None
            if isinstance(tunnels, list):
                for t in tunnels:
                    if not isinstance(t, dict):
                        continue
                    url = t.get("public_url")
                    proto = t.get("proto")
                    if isinstance(url, str) and (
                        proto == "https" or url.startswith("https://")
                    ):
                        return url
            time.sleep(poll_interval)

        raise NgrokTimeoutError(
            f"no https tunnel reported within {timeout}s (last_seen={last_seen!r})"
        )

    def __enter__(self) -> "NgrokTunnel":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
