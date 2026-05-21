"""BOOT-1 (#20) GitHub HTTP client.

平行 ``gitea_client.GiteaClient``: 同 7 个公开方法签名, 但 base URL +
auth header + 分页规则按 GitHub 走. 复用 ``KeychainCredentialProvider``
(传 ``host="github.com"``).

跟 Gitea 关键差异:

| 维度 | Gitea | GitHub |
|---|---|---|
| base URL | ``https://git.k2lab.ai/api/v1`` | ``https://api.github.com`` |
| Auth header | ``Authorization: token <PAT>`` | ``Authorization: Bearer <PAT>`` (or ``token <PAT>``) |
| 分配给我的 issue 端点 | ``/repos/issues/search?assigned=true`` | ``/issues?filter=assigned&state=open`` (cross-repo, 自动用 ``/user`` 身份) |
| 分页 | ``?page=N&limit=N``, ``len(rows) < per_page`` 终止 | ``?page=N&per_page=N``, ``Link`` header ``rel="next"`` 终止 |
| Rate limit header | (一般无) | ``X-RateLimit-Remaining`` |
| PR 混入 issue 列表 | 用 ``type=issues`` 过滤 | 用 ``pull_request != null`` 字段过滤 |

错误体系**复用** ``gitea_client.*Error`` (语义通用, 不绑 Gitea 特性). 这样
caller 写 ``except GiteaClientError`` 能同时 catch 两边.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Optional

from gitea_client import (
    GiteaAuthError,
    GiteaClientError,
    GiteaNotFoundError,
    GiteaPermissionError,
    GiteaRateLimitError,
    GiteaServerError,
    GiteaTransportError,
    GiteaValidationError,
)
from gitea_credentials import KeychainCredentialProvider


# GitHub 的 Link header 样式: ``<https://api.github.com/...?page=2>; rel="next", <...>; rel="last"``
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GithubClient:
    def __init__(
        self,
        base_url: str = "https://api.github.com",
        provider: KeychainCredentialProvider | None = None,
        timeout: float = 10.0,
        per_page: int = 50,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # KeychainCredentialProvider 已支持自定义 host; GitHub PAT 存
        # service=github.com (用户用 ``security add-generic-password`` 加)
        self.provider = provider or KeychainCredentialProvider(host="github.com")
        self.timeout = timeout
        self.per_page = per_page

    # ------------------------------------------------------------------
    # 公开方法 (跟 GiteaClient 同签名)
    # ------------------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        return self._request_json("GET", "/user")

    def list_assigned_issues(
        self, state: str = "open", since: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """GitHub ``/issues?filter=assigned``: 跨所有 repo 列出指派给当前 token 用户的.

        GitHub 把 PR 也算作 issue 返回, 用 ``pull_request`` 字段过滤掉.
        """
        params: dict[str, str] = {
            "filter": "assigned",
            "state": state,
        }
        if since:
            params["since"] = since

        issues: list[dict[str, Any]] = []
        for rows in self._paginate("/issues", params=params):
            for item in rows:
                if item.get("pull_request") is not None:
                    continue
                # GitHub 的 issue 没有顶层 ``repository`` (Gitea 有);
                # 用 ``repository_url`` 反解 owner/name 补一份, 让 caller 跟 Gitea 接口一致
                if "repository" not in item:
                    item["repository"] = _derive_repo_from_url(item.get("repository_url"))
                issues.append(item)
        return issues

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        owner, name = self._split_repo(repo)
        item = self._request_json("GET", f"/repos/{owner}/{name}/issues/{number}")
        if "repository" not in item:
            item["repository"] = {"full_name": f"{owner}/{name}"}
        return item

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        owner, name = self._split_repo(repo)
        comments: list[dict[str, Any]] = []
        for rows in self._paginate(f"/repos/{owner}/{name}/issues/{number}/comments"):
            comments.extend(rows)
        return comments

    def comment_on_issue(self, repo: str, number: int, body: str) -> dict[str, Any]:
        owner, name = self._split_repo(repo)
        return self._request_json(
            "POST",
            f"/repos/{owner}/{name}/issues/{number}/comments",
            json_body={"body": body},
        )

    def transition_issue(self, repo: str, number: int, state: str) -> dict[str, Any]:
        if state not in {"open", "closed"}:
            raise ValueError("state must be 'open' or 'closed'")
        owner, name = self._split_repo(repo)
        return self._request_json(
            "PATCH",
            f"/repos/{owner}/{name}/issues/{number}",
            json_body={"state": state},
        )

    # ------------------------------------------------------------------
    # 内部: 分页 + 请求
    # ------------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """GitHub 分页: 看 ``Link`` header ``rel="next"``.

        没 Link header 或 rel=next 缺失 → 已是最后一页, 结束循环.
        """
        page_params = dict(params or {})
        page_params["per_page"] = str(self.per_page)
        page_params["page"] = "1"
        next_url = f"{self.base_url}{path}?{urllib.parse.urlencode(page_params)}"
        while next_url:
            rows, link_header = self._request_with_link(next_url)
            if not isinstance(rows, list):
                raise GiteaValidationError(f"unexpected pagination payload for {path!r}")
            yield rows
            if not rows:
                break
            next_url = _next_link(link_header) if link_header else None

    def _request_with_link(self, url: str) -> tuple[Any, str | None]:
        token = self.provider.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else []
                link = response.headers.get("Link")
                return payload, link
        except urllib.error.HTTPError as exc:
            payload = self._decode_error_body(exc)
            self._raise_http_error(exc.code, payload)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GiteaTransportError(str(exc)) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = self.provider.get_token()
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        payload_bytes = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if json_body is not None:
            payload_bytes = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=payload_bytes,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = self._decode_error_body(exc)
            self._raise_http_error(exc.code, payload)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GiteaTransportError(str(exc)) from exc

    @staticmethod
    def _decode_error_body(exc: urllib.error.HTTPError) -> Any:
        raw = exc.read().decode("utf-8", errors="replace")
        if not raw:
            return {"detail": f"http {exc.code}"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    @staticmethod
    def _raise_http_error(status: int, payload: Any) -> None:
        detail = payload if isinstance(payload, dict) else {"detail": str(payload)}
        if status == 401:
            raise GiteaAuthError(str(detail))
        if status == 403:
            # GitHub 把 rate limit 也归 403 (header 含 X-RateLimit-Remaining=0).
            # 简化处理: 看 message 含 "rate limit" 即视为 RateLimit.
            msg = str(detail).lower()
            if "rate limit" in msg or "abuse" in msg:
                raise GiteaRateLimitError(str(detail))
            raise GiteaPermissionError(str(detail))
        if status == 404:
            raise GiteaNotFoundError(str(detail))
        if status == 422:
            raise GiteaValidationError(str(detail))
        if status == 429:
            raise GiteaRateLimitError(str(detail))
        if status >= 500:
            raise GiteaServerError(str(detail))
        raise GiteaClientError(str(detail))

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        if "/" not in repo:
            raise ValueError(f"repo must be owner/name, got {repo!r}")
        owner, name = repo.split("/", 1)
        return owner, name


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _next_link(link_header: str) -> str | None:
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def _derive_repo_from_url(repository_url: Any) -> dict[str, str] | None:
    """``https://api.github.com/repos/<owner>/<name>`` → ``{full_name: owner/name}``."""
    if not isinstance(repository_url, str):
        return None
    parts = repository_url.rstrip("/").split("/")
    if len(parts) < 2:
        return None
    name = parts[-1]
    owner = parts[-2]
    return {"full_name": f"{owner}/{name}"}
