"""Tests for auto_review.bot_account — Gitea whoami caching + startup mismatch check."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.bot_account import BotAccount, BotAccountMismatch, identify_bot
from auto_review.config import AutoReviewConfig


class _FakeClient:
    """Minimal stand-in for gitea_client.GiteaClient — only whoami() is needed."""

    def __init__(self, whoami_payload):
        self._payload = whoami_payload
        self.whoami_calls = 0

    def whoami(self):
        self.whoami_calls += 1
        return self._payload


def _make_cfg(bot_username: str = "agent-court-bot") -> AutoReviewConfig:
    return AutoReviewConfig(
        bot_username=bot_username,
        watch_repos=["K2Lab/agent-court"],
    )


def test_identify_bot_success_returns_bot_account():
    client = _FakeClient({"login": "agent-court-bot", "id": 42, "email": "bot@example.com"})
    cfg = _make_cfg("agent-court-bot")

    bot = identify_bot(cfg, client=client)

    assert isinstance(bot, BotAccount)
    assert bot.login == "agent-court-bot"
    assert bot.user_id == 42
    assert bot.email == "bot@example.com"
    assert client.whoami_calls == 1


def test_identify_bot_mismatch_raises():
    """whoami 返回的 login 跟配置里的 bot_username 对不上 → 抛错."""
    client = _FakeClient({"login": "some-other-account", "id": 99})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="agent-court-bot.*some-other-account"):
        identify_bot(cfg, client=client)


def test_identify_bot_missing_login_field_raises():
    """whoami payload 缺 login 字段 — 视为 Gitea API 异常."""
    client = _FakeClient({"id": 42})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="login"):
        identify_bot(cfg, client=client)


def test_identify_bot_blank_login_field_raises():
    """whoami 返回 login 字段是空白字符串 — 也当作 API 异常."""
    client = _FakeClient({"login": "   ", "id": 42})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="login"):
        identify_bot(cfg, client=client)


def test_bot_account_is_frozen():
    bot = BotAccount(login="bot", user_id=1)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        bot.login = "other"  # type: ignore[misc]


def test_bot_account_email_optional():
    """email 字段可选, 默认 None."""
    bot = BotAccount(login="bot", user_id=1)
    assert bot.email is None


def test_identify_bot_missing_id_field_raises():
    """whoami payload 缺 id 字段 — 防止 user_id 静默退化为 0."""
    client = _FakeClient({"login": "agent-court-bot"})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="'id'"):
        identify_bot(cfg, client=client)


def test_identify_bot_non_int_id_field_raises():
    """whoami 'id' 不是整数, 抛 BotAccountMismatch (不是 ValueError)."""
    client = _FakeClient({"login": "agent-court-bot", "id": "not-a-number"})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="not an integer"):
        identify_bot(cfg, client=client)


def test_identify_bot_non_string_login_raises():
    """whoami 'login' 类型错 (list / int) → BotAccountMismatch, 不是 AttributeError."""
    client = _FakeClient({"login": ["array"], "id": 42})
    cfg = _make_cfg("agent-court-bot")

    with pytest.raises(BotAccountMismatch, match="not a string"):
        identify_bot(cfg, client=client)


def test_identify_bot_empty_email_normalized_to_none():
    """Gitea 对隐藏邮箱用户可能返回 email='', 归一化为 None 避免下游分歧."""
    client = _FakeClient({"login": "agent-court-bot", "id": 42, "email": ""})
    cfg = _make_cfg("agent-court-bot")

    bot = identify_bot(cfg, client=client)

    assert bot.email is None
