"""BOOT-1 (#20): tracker factory tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gitea_client import GiteaClient  # noqa: E402
from github_client import GithubClient  # noqa: E402
from tracker_factory import build_tracker_client  # noqa: E402
from workflow_loader import TrackerConfig  # noqa: E402


def test_default_no_config_returns_gitea():
    client = build_tracker_client(None)
    assert isinstance(client, GiteaClient)


def test_gitea_provider_returns_gitea():
    client = build_tracker_client(TrackerConfig(provider="gitea"))
    assert isinstance(client, GiteaClient)


def test_github_provider_returns_github():
    client = build_tracker_client(TrackerConfig(provider="github"))
    assert isinstance(client, GithubClient)


def test_custom_base_url_overrides_default():
    client = build_tracker_client(TrackerConfig(provider="github", base_url="https://ghe.example.com/api/v3"))
    assert isinstance(client, GithubClient)
    assert client.base_url == "https://ghe.example.com/api/v3"


def test_custom_gitea_base_url():
    client = build_tracker_client(TrackerConfig(provider="gitea", base_url="https://gitea.example.com/api/v1"))
    assert isinstance(client, GiteaClient)
    assert client.base_url == "https://gitea.example.com/api/v1"


def test_unsupported_provider_raises():
    with pytest.raises(ValueError, match="unsupported tracker provider"):
        build_tracker_client(TrackerConfig(provider="jira"))


def test_tracker_config_with_missing_provider_field_defaults_to_gitea():
    """模拟一个不带 provider 属性的对象 (defensive)."""
    class FakeConfig:
        pass

    client = build_tracker_client(FakeConfig())
    assert isinstance(client, GiteaClient)
