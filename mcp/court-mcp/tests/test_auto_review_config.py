"""Tests for auto_review.config — env-driven configuration loader."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 项目沿用顶层 sys.path import 风格 (cf. test_git_board.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_review.config import AutoReviewConfig, AutoReviewConfigError, load_config


def _clear_a2a_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("A2A_GITEA_"):
            monkeypatch.delenv(k, raising=False)


def test_load_config_minimal_required_fields(monkeypatch):
    """只设置 username + watch_repos 应当成功, 其余字段取默认."""
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "agent-court-bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/agent-court,K2Lab/moras-brain")

    cfg = load_config()

    assert cfg.bot_username == "agent-court-bot"
    assert cfg.watch_repos == ["K2Lab/agent-court", "K2Lab/moras-brain"]
    assert cfg.pr_auto_post is True
    assert cfg.issue_auto_post is True
    assert cfg.webhook_triggers_enabled is False
    assert cfg.worker_count == 2
    assert cfg.light_deep_threshold == 10
    assert cfg.gitea_base_url == "https://git.k2lab.ai"
    assert cfg.poll_discovery_interval_sec == 60
    assert cfg.poll_active_interval_sec == 30


def test_load_config_missing_username_raises(monkeypatch):
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/agent-court")

    with pytest.raises(AutoReviewConfigError, match="A2A_GITEA_USERNAME"):
        load_config()


def test_load_config_missing_watch_repos_raises(monkeypatch):
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "agent-court-bot")

    with pytest.raises(AutoReviewConfigError, match="A2A_GITEA_WATCH_REPOS"):
        load_config()


def test_load_config_empty_watch_repos_raises(monkeypatch):
    """空字符串或全空白也应该当作未设置."""
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "agent-court-bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "   ,, ,  ")

    with pytest.raises(AutoReviewConfigError, match="A2A_GITEA_WATCH_REPOS"):
        load_config()


def test_load_config_watch_repos_strips_whitespace(monkeypatch):
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv(
        "A2A_GITEA_WATCH_REPOS", "  K2Lab/a , K2Lab/b ,, K2Lab/c  "
    )

    cfg = load_config()

    assert cfg.watch_repos == ["K2Lab/a", "K2Lab/b", "K2Lab/c"]


def test_load_config_watch_repos_rejects_malformed(monkeypatch):
    """没有斜杠的条目不是合法 owner/repo, 报错而非静默丢弃."""
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/a,not-a-repo")

    with pytest.raises(AutoReviewConfigError, match="owner/repo"):
        load_config()


def test_load_config_bool_envs(monkeypatch):
    """auto-post 默认 true; 显式 false / 0 / no 视为关闭."""
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/a")
    monkeypatch.setenv("A2A_GITEA_PR_AUTO_POST", "false")
    monkeypatch.setenv("A2A_GITEA_ISSUE_AUTO_POST", "0")
    monkeypatch.setenv("A2A_GITEA_WEBHOOK_TRIGGERS", "true")

    cfg = load_config()

    assert cfg.pr_auto_post is False
    assert cfg.issue_auto_post is False
    assert cfg.webhook_triggers_enabled is True


def test_load_config_int_overrides(monkeypatch):
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/a")
    monkeypatch.setenv("A2A_GITEA_WORKER_COUNT", "4")
    monkeypatch.setenv("A2A_GITEA_LIGHT_DEEP_THRESHOLD", "25")
    monkeypatch.setenv("A2A_GITEA_POLL_DISCOVERY_SEC", "120")
    monkeypatch.setenv("A2A_GITEA_POLL_ACTIVE_SEC", "15")

    cfg = load_config()

    assert cfg.worker_count == 4
    assert cfg.light_deep_threshold == 25
    assert cfg.poll_discovery_interval_sec == 120
    assert cfg.poll_active_interval_sec == 15


def test_load_config_int_invalid_raises(monkeypatch):
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/a")
    monkeypatch.setenv("A2A_GITEA_WORKER_COUNT", "not-a-number")

    with pytest.raises(AutoReviewConfigError, match="A2A_GITEA_WORKER_COUNT"):
        load_config()


def test_dataclass_is_frozen(monkeypatch):
    """AutoReviewConfig 应当是 frozen, 防止运行时被改."""
    _clear_a2a_env(monkeypatch)
    monkeypatch.setenv("A2A_GITEA_USERNAME", "bot")
    monkeypatch.setenv("A2A_GITEA_WATCH_REPOS", "K2Lab/a")

    cfg = load_config()

    with pytest.raises(Exception):  # FrozenInstanceError 或 AttributeError
        cfg.bot_username = "other"  # type: ignore[misc]
