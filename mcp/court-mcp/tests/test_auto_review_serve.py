"""Tests for auto_review.serve — entry-point wiring + DispatcherLoop."""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.config import AutoReviewConfig
from auto_review.serve import DispatcherLoop, build_components, Serve


def _cfg(**overrides):
    base = dict(
        bot_username="bot",
        watch_repos=["K2Lab/agent-court"],
        webhook_port=48731,
        worker_count=2,
        poll_discovery_interval_sec=60,
        poll_active_interval_sec=30,
    )
    base.update(overrides)
    return AutoReviewConfig(**base)


# ---------- DispatcherLoop ----------

def test_dispatcher_loop_calls_process_pending_at_interval():
    dispatcher = MagicMock()
    dispatcher.process_pending.return_value = []
    loop = DispatcherLoop(dispatcher, interval_sec=0.05, batch_limit=3)
    loop.start()
    time.sleep(0.2)  # 至少 3 个 tick
    loop.stop(timeout=1.0)
    assert dispatcher.process_pending.call_count >= 2
    args, kwargs = dispatcher.process_pending.call_args
    assert kwargs.get("limit") == 3 or (args and args[0] == 3)


def test_dispatcher_loop_stop_is_clean():
    dispatcher = MagicMock()
    dispatcher.process_pending.return_value = []
    loop = DispatcherLoop(dispatcher, interval_sec=10, batch_limit=5)
    loop.start()
    assert loop._thread.is_alive()
    loop.stop(timeout=2.0)
    assert not loop._thread.is_alive()


def test_dispatcher_loop_start_idempotent():
    dispatcher = MagicMock()
    loop = DispatcherLoop(dispatcher, interval_sec=10, batch_limit=5)
    loop.start()
    t1 = loop._thread
    loop.start()
    t2 = loop._thread
    try:
        assert t1 is t2
    finally:
        loop.stop(timeout=2.0)


def test_dispatcher_loop_swallows_exceptions():
    """process_pending 抛错不能崩 dispatcher thread."""
    dispatcher = MagicMock()
    dispatcher.process_pending.side_effect = [RuntimeError("boom"), []]
    loop = DispatcherLoop(dispatcher, interval_sec=0.05, batch_limit=1)
    loop.start()
    time.sleep(0.2)
    loop.stop(timeout=1.0)
    assert dispatcher.process_pending.call_count >= 2  # 第二次仍被调


# ---------- build_components ----------

def test_build_components_returns_serve_instance(tmp_path):
    """完整 wiring 不抛错, 返回 Serve 实例; credential / spawner / ngrok 全 mock."""
    cfg = _cfg()
    fake_provider = MagicMock()
    fake_provider.get_username.return_value = "test-bot"
    fake_provider.get_token.return_value = "fake-token"

    # GiteaClient.whoami 在 identify_bot 里会被调; mock 它返回 login=bot
    import auto_review.serve as serve_mod
    # 注: 我们让 build_components 接受 credential_provider; identify_bot 调
    # client.whoami(), 我们 mock 整个 client.
    fake_client = MagicMock()
    fake_client.whoami.return_value = {"login": "bot", "id": 1}

    serve = build_components(
        cfg=cfg,
        webhook_secret="test-secret",
        court_root=tmp_path,
        light_prefer="codex",
        credential_provider=fake_provider,
        client=fake_client,  # 注入避免真 GiteaClient 构造
    )
    assert isinstance(serve, Serve)
    assert serve.cfg is cfg
    assert serve.store is not None
    assert serve.worker is not None
    assert serve.dispatcher is not None
    assert serve.webhook_app is not None


def test_build_components_creates_sqlite_under_court_root(tmp_path):
    cfg = _cfg()
    fake_client = MagicMock()
    fake_client.whoami.return_value = {"login": "bot", "id": 1}
    serve = build_components(
        cfg=cfg, webhook_secret="s", court_root=tmp_path,
        light_prefer="codex", credential_provider=MagicMock(), client=fake_client,
    )
    expected_db = tmp_path / "auto_review" / "state.sqlite3"
    assert expected_db.exists()


def test_build_components_bot_mismatch_raises(tmp_path):
    """whoami 返回的 login 跟 cfg.bot_username 不一致, 应该原样抛 BotAccountMismatch."""
    from auto_review.bot_account import BotAccountMismatch
    cfg = _cfg(bot_username="bot")
    fake_client = MagicMock()
    fake_client.whoami.return_value = {"login": "different-account", "id": 9}
    with pytest.raises(BotAccountMismatch):
        build_components(
            cfg=cfg, webhook_secret="s", court_root=tmp_path,
            light_prefer="codex", credential_provider=MagicMock(), client=fake_client,
        )
