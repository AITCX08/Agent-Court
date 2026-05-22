"""Tests for GiteaClient.get_pr — added in PR-18d for changed_files lookup."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gitea_client import GiteaClient, GiteaNotFoundError


class _StubProvider:
    def get_token(self) -> str:
        return "dummy"


def _make_client():
    """Construct a GiteaClient with dummy creds; tests only exercise method routing."""
    return GiteaClient(base_url="https://git.k2lab.ai", provider=_StubProvider())


def test_get_pr_calls_correct_endpoint():
    client = _make_client()
    client._request_json = MagicMock(return_value={"number": 42, "changed_files": 7})

    result = client.get_pr("K2Lab/agent-court", 42)

    client._request_json.assert_called_once_with(
        "GET", "/repos/K2Lab/agent-court/pulls/42"
    )
    assert result == {"number": 42, "changed_files": 7}


def test_get_pr_passes_through_full_payload():
    client = _make_client()
    payload = {
        "number": 1,
        "title": "test",
        "head": {"sha": "abc123"},
        "changed_files": 25,
        "requested_reviewers": [{"login": "bot"}],
        "html_url": "https://git.k2lab.ai/K2Lab/agent-court/pulls/1",
    }
    client._request_json = MagicMock(return_value=payload)

    result = client.get_pr("K2Lab/agent-court", 1)
    assert result is payload  # full passthrough, no copy/filter


def test_get_pr_propagates_not_found_error():
    client = _make_client()
    client._request_json = MagicMock(
        side_effect=GiteaNotFoundError("PR 999 not found")
    )

    with pytest.raises(GiteaNotFoundError, match="999"):
        client.get_pr("K2Lab/agent-court", 999)
