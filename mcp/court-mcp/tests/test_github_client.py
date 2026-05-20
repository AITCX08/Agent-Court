"""BOOT-1 (#20): GithubClient tests.

不打真 api.github.com (会受 rate limit / 网络影响), 用 mock urlopen.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_client as gh  # noqa: E402
from gitea_client import (  # noqa: E402
    GiteaAuthError,
    GiteaNotFoundError,
    GiteaPermissionError,
    GiteaRateLimitError,
    GiteaServerError,
    GiteaTransportError,
    GiteaValidationError,
)


class StubProvider:
    def __init__(self, token: str = "stub-token"):
        self._token = token

    def get_token(self) -> str:
        return self._token


def _http_response(body: object, link_header: str | None = None, status: int = 200):
    """Build a fake urlopen response context manager that yields body + headers."""

    class _Resp:
        def __init__(self):
            self._raw = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body

        def read(self):
            return self._raw

        @property
        def headers(self):
            h: dict[str, str] = {}
            if link_header is not None:
                h["Link"] = link_header
            return h

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp()


def _http_error(status: int, body: dict | str):
    raw = json.dumps(body).encode("utf-8") if isinstance(body, dict) else str(body).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://example",
        code=status,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(raw),
    )


# ---------------------------------------------------------------------------
# Basic calls
# ---------------------------------------------------------------------------

def test_whoami_sends_bearer_auth():
    client = gh.GithubClient(provider=StubProvider("tok-abc"))
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        return _http_response({"login": "alice"})

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = client.whoami()
    assert result == {"login": "alice"}
    assert captured["url"] == "https://api.github.com/user"
    # GitHub 优先 Bearer (兼容 token<PAT>)
    assert captured["headers"].get("Authorization") == "Bearer tok-abc"
    assert captured["headers"].get("Accept") == "application/vnd.github+json"


def test_get_issue_normalizes_repo_full_name():
    """GitHub 返的 issue payload 没有顶层 ``repository``; client 应补一份让 caller 跟 Gitea 一致."""
    client = gh.GithubClient(provider=StubProvider())

    def fake_urlopen(_request, timeout=None):
        return _http_response({
            "number": 7,
            "title": "demo",
            "body": "...",
            # 注意没有 repository 字段
        })

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        issue = client.get_issue("AITCX08/agent-court", 7)
    assert issue["repository"] == {"full_name": "AITCX08/agent-court"}


def test_comment_on_issue_posts_json_body():
    client = gh.GithubClient(provider=StubProvider())
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["content_type"] = request.headers.get("Content-type")
        return _http_response({"id": 99, "body": "hello"})

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = client.comment_on_issue("foo/bar", 3, "hello")
    assert result == {"id": 99, "body": "hello"}
    assert captured["url"].endswith("/repos/foo/bar/issues/3/comments")
    assert captured["method"] == "POST"
    assert json.loads(captured["body"]) == {"body": "hello"}
    assert captured["content_type"] == "application/json"


def test_transition_issue_to_closed():
    client = gh.GithubClient(provider=StubProvider())
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["method"] = request.get_method()
        captured["body"] = request.data
        return _http_response({"state": "closed"})

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = client.transition_issue("foo/bar", 9, "closed")
    assert result == {"state": "closed"}
    assert captured["method"] == "PATCH"
    assert json.loads(captured["body"]) == {"state": "closed"}


def test_transition_issue_rejects_invalid_state():
    client = gh.GithubClient(provider=StubProvider())
    with pytest.raises(ValueError, match="state must be"):
        client.transition_issue("foo/bar", 1, "merged")


# ---------------------------------------------------------------------------
# Pagination: Link header rel=next
# ---------------------------------------------------------------------------

def test_paginate_follows_link_next():
    client = gh.GithubClient(provider=StubProvider(), per_page=2)
    pages = [
        ([{"number": 1}, {"number": 2}], '<https://api.github.com/issues?page=2>; rel="next", <...>; rel="last"'),
        ([{"number": 3}], None),  # last page, no Link rel=next
    ]
    call_count = {"n": 0}

    def fake_urlopen(_request, timeout=None):
        idx = call_count["n"]
        call_count["n"] += 1
        body, link = pages[idx]
        return _http_response(body, link_header=link)

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        collected = list(client._paginate("/issues"))
    assert collected == [[{"number": 1}, {"number": 2}], [{"number": 3}]]
    assert call_count["n"] == 2


def test_paginate_stops_on_empty_page():
    client = gh.GithubClient(provider=StubProvider())

    def fake_urlopen(_request, timeout=None):
        return _http_response([], link_header='<https://api.github.com/issues?page=2>; rel="next"')

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        collected = list(client._paginate("/issues"))
    assert collected == [[]]  # 第一页空就停, 不追 Link


# ---------------------------------------------------------------------------
# list_assigned_issues: PR 过滤 + repository 反解
# ---------------------------------------------------------------------------

def test_list_assigned_filters_pull_requests_and_normalizes_repo():
    client = gh.GithubClient(provider=StubProvider())
    fake_body = [
        {"number": 1, "title": "real issue", "pull_request": None,
         "repository_url": "https://api.github.com/repos/AITCX08/agent-court"},
        {"number": 2, "title": "actually a PR", "pull_request": {"url": "..."},
         "repository_url": "https://api.github.com/repos/AITCX08/agent-court"},
        {"number": 3, "title": "another issue", "pull_request": None,
         "repository_url": "https://api.github.com/repos/foo/bar"},
    ]

    def fake_urlopen(_request, timeout=None):
        return _http_response(fake_body)

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        issues = client.list_assigned_issues()
    assert [it["number"] for it in issues] == [1, 3]
    assert issues[0]["repository"] == {"full_name": "AITCX08/agent-court"}
    assert issues[1]["repository"] == {"full_name": "foo/bar"}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,body,exc_cls",
    [
        (401, {"message": "Bad credentials"}, GiteaAuthError),
        (403, {"message": "no permission"}, GiteaPermissionError),
        (404, {"message": "Not Found"}, GiteaNotFoundError),
        (422, {"message": "Validation Failed"}, GiteaValidationError),
        (429, {"message": "Too Many Requests"}, GiteaRateLimitError),
        (500, {"message": "boom"}, GiteaServerError),
    ],
)
def test_error_status_maps_to_correct_exception(status, body, exc_cls):
    client = gh.GithubClient(provider=StubProvider())

    def fake_urlopen(_request, timeout=None):
        raise _http_error(status, body)

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(exc_cls):
            client.whoami()


def test_403_with_rate_limit_message_maps_to_rate_limit_error():
    """GitHub 把 secondary rate limit 也归 403; message 含 'rate limit' 走 RateLimit."""
    client = gh.GithubClient(provider=StubProvider())

    def fake_urlopen(_request, timeout=None):
        raise _http_error(403, {"message": "API rate limit exceeded"})

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(GiteaRateLimitError):
            client.whoami()


def test_network_timeout_maps_to_transport_error():
    client = gh.GithubClient(provider=StubProvider())

    def fake_urlopen(_request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch.object(gh.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(GiteaTransportError):
            client.whoami()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_next_link_parses_github_format():
    h = '<https://api.github.com/issues?page=2>; rel="next", <https://api.github.com/issues?page=5>; rel="last"'
    assert gh._next_link(h) == "https://api.github.com/issues?page=2"


def test_next_link_returns_none_when_no_next():
    h = '<https://api.github.com/issues?page=5>; rel="last", <https://api.github.com/issues?page=1>; rel="first"'
    assert gh._next_link(h) is None


def test_derive_repo_from_url():
    assert gh._derive_repo_from_url("https://api.github.com/repos/foo/bar") == {"full_name": "foo/bar"}
    assert gh._derive_repo_from_url("https://api.github.com/repos/AITCX08/agent-court/") == {"full_name": "AITCX08/agent-court"}
    assert gh._derive_repo_from_url(None) is None
    assert gh._derive_repo_from_url("") is None
