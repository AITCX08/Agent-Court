"""Entry-point CLI for the auto-review subsystem.

Wires PR-18a..e into a single ``python -m auto_review.serve`` process:

- PollingWorker (discovery 60s + active 30s) writes to StateStore
- DispatcherLoop pulls DISCOVERED tasks every N seconds and runs them
  through ReviewDispatcher (light or deep)
- aiohttp webhook listener accepts Gitea push events
- Optional ngrok tunnel exposes the webhook to the public internet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from auto_review.bot_account import BotAccount, BotAccountMismatch, identify_bot
from auto_review.config import AutoReviewConfig, AutoReviewConfigError, load_config
from auto_review.dispatcher import ReviewDispatcher
from auto_review.executor import DeepExecutor, LightExecutor
from auto_review.ngrok import NgrokTunnel
from auto_review.state import StateStore
from auto_review.webhook import create_app as create_webhook_app
from auto_review.worker import PollingWorker

_log = logging.getLogger("auto_review.serve")


class DispatcherLoop:
    """Background thread that periodically drains DISCOVERED tasks.

    The PollingWorker writes new tasks; this loop reads them and runs them
    through ReviewDispatcher.process_pending().
    """

    def __init__(
        self,
        dispatcher: ReviewDispatcher,
        *,
        interval_sec: float = 10.0,
        batch_limit: int = 5,
    ):
        self._dispatcher = dispatcher
        self._interval = interval_sec
        self._limit = batch_limit
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="auto-review-dispatcher", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._dispatcher.process_pending(limit=self._limit)
            except Exception:
                _log.exception("dispatcher loop crashed (continuing)")
            self._stop_event.wait(self._interval)


@dataclass(frozen=True, slots=True)
class Serve:
    """Wired components ready to run_forever()."""

    cfg: AutoReviewConfig
    bot: BotAccount
    store: StateStore
    client: Any
    worker: PollingWorker
    dispatcher: ReviewDispatcher
    dispatcher_loop: DispatcherLoop
    webhook_app: web.Application
    ngrok: Optional[NgrokTunnel] = None

    def run_forever(self) -> None:
        _log.info("starting auto-review services (port=%d)", self.cfg.webhook_port)
        self.worker.start()
        self.dispatcher_loop.start()
        if self.ngrok is not None:
            self.ngrok.start()
            try:
                url = self.ngrok.public_url(timeout=15)
                _log.info("ngrok public URL: %s", url)
                print(f"ngrok public URL: {url}", flush=True)
            except Exception as exc:
                _log.warning("ngrok public_url unavailable: %s", exc)

        try:
            # web.run_app blocks until SIGINT/SIGTERM
            web.run_app(
                self.webhook_app,
                host="0.0.0.0",
                port=self.cfg.webhook_port,
                print=lambda *a, **k: None,  # quiet startup banner
            )
        finally:
            _log.info("shutting down auto-review services")
            self.dispatcher_loop.stop(timeout=5)
            self.worker.stop(timeout=5)
            if self.ngrok is not None:
                self.ngrok.stop(timeout=5)
            self.store.close()


def build_components(
    *,
    cfg: AutoReviewConfig,
    webhook_secret: str,
    court_root: Path,
    light_prefer: str = "codex",
    credential_provider=None,
    client=None,
    enable_ngrok: bool = False,
    ngrok_extra_args: tuple[str, ...] = (),
) -> Serve:
    """Construct all PR-18 components and return a ready-to-run Serve.

    ``client`` is injectable for tests. In production, omit it — the function
    will construct a real GiteaClient from ``credential_provider``.
    """
    # 1. Gitea client
    if client is None:
        from gitea_client import GiteaClient
        if credential_provider is None:
            from gitea_credentials import KeychainCredentialProvider
            host = (
                cfg.gitea_base_url
                .replace("https://", "")
                .replace("http://", "")
                .rstrip("/")
            )
            credential_provider = KeychainCredentialProvider(host=host)
        client = GiteaClient(
            base_url=cfg.gitea_base_url, provider=credential_provider
        )

    # 2. Bot account verification (fail-fast)
    bot = identify_bot(cfg, client=client)

    # 3. SQLite state store
    db_path = court_root / "auto_review" / "state.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(db_path))

    # 4. Light + Deep executors
    light = LightExecutor(prefer=light_prefer)
    from agent_spawn import AgentSpawner
    from team_links import TeamLinks
    spawner = AgentSpawner(
        team_links=TeamLinks(court_root=court_root),
        cwd_for_session=None,
    )
    deep = DeepExecutor(spawner=spawner)

    # 5. Dispatcher + loop
    dispatcher = ReviewDispatcher(
        cfg=cfg, store=store, client=client, light=light, deep=deep
    )
    dispatcher_loop = DispatcherLoop(dispatcher, interval_sec=10.0, batch_limit=5)

    # 6. Polling worker
    worker = PollingWorker(cfg=cfg, bot=bot, client=client, store=store)

    # 7. Webhook app
    webhook_app = create_webhook_app(
        cfg=cfg, bot=bot, store=store, secret=webhook_secret
    )

    # 8. Ngrok (optional)
    ngrok = (
        NgrokTunnel(port=cfg.webhook_port, extra_args=list(ngrok_extra_args))
        if enable_ngrok else None
    )

    return Serve(
        cfg=cfg, bot=bot, store=store, client=client,
        worker=worker, dispatcher=dispatcher, dispatcher_loop=dispatcher_loop,
        webhook_app=webhook_app, ngrok=ngrok,
    )


def _print_dry_run(serve: Serve) -> None:
    print("--- auto_review dry-run ---")
    print(f"bot_username       = {serve.bot.login}")
    print(f"bot_user_id        = {serve.bot.user_id}")
    print(f"watch_repos        = {serve.cfg.watch_repos}")
    print(f"webhook_port       = {serve.cfg.webhook_port}")
    print(f"webhook_triggers   = {serve.cfg.webhook_triggers_enabled}")
    print(f"pr_auto_post       = {serve.cfg.pr_auto_post}")
    print(f"issue_auto_post    = {serve.cfg.issue_auto_post}")
    print(f"worker_count       = {serve.cfg.worker_count}")
    print(f"light_deep_thresh  = {serve.cfg.light_deep_threshold}")
    print(f"poll_discovery_sec = {serve.cfg.poll_discovery_interval_sec}")
    print(f"poll_active_sec    = {serve.cfg.poll_active_interval_sec}")
    print(f"ngrok              = {'enabled' if serve.ngrok else 'disabled'}")
    print("--- (would run_forever() without --dry-run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m auto_review.serve",
        description="Auto-review entry point: polling worker + webhook + dispatcher + (optional) ngrok",
    )
    parser.add_argument("--ngrok", action="store_true",
                        help="start an ngrok tunnel exposing the webhook publicly")
    parser.add_argument("--ngrok-arg", action="append", default=[],
                        help="pass-through arg for ngrok (repeat for multiple)")
    parser.add_argument("--dry-run", action="store_true",
                        help="wire components, print configuration, exit 0")
    parser.add_argument("--court-root", type=Path,
                        default=Path.home() / ".agent-court",
                        help="root dir for SQLite + tmux state (default ~/.agent-court)")
    parser.add_argument("--prefer", choices=["codex", "claude"], default="codex",
                        help="light review CLI preference")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config()
    except AutoReviewConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    webhook_secret = os.environ.get("A2A_GITEA_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        print("A2A_GITEA_WEBHOOK_SECRET not set; webhook listener will reject all events",
              file=sys.stderr)

    try:
        serve = build_components(
            cfg=cfg,
            webhook_secret=webhook_secret,
            court_root=args.court_root,
            light_prefer=args.prefer,
            enable_ngrok=args.ngrok,
            ngrok_extra_args=tuple(args.ngrok_arg),
        )
    except BotAccountMismatch as exc:
        print(f"bot account mismatch: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"build_components failed: {exc}", file=sys.stderr)
        return 4

    if args.dry_run:
        _print_dry_run(serve)
        return 0

    serve.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
