"""PR-16b Git 看板聚合层.

把 Gitea ``/repos/issues/search`` 不同 scope 的结果聚合成 dashboard 前端用的
4 列 Kanban + 底部 Issues 行的统一结构. 30s 内存 cache 避免每次切 scope 都
打一次 Gitea API.

数据结构跟 plan §4.2 ``GET /api/git-board`` 响应一致:

.. code-block:: json

    {
      "scope": "related",
      "updated_at": "2026-05-21T18:02:33Z",
      "stale": false,
      "columns": {
        "wip": [PrCard, ...],
        "under_review": [PrCard, ...],
        "reviewing": [PrCard, ...],
        "reviewed": [PrCard, ...]
      },
      "issues_row": [IssueCard, ...]
    }

PR-16b MVP **不调 reviews endpoint** (每个 PR 一次 N+1 太贵), 4 列分类用 search
响应里 ``draft`` / ``requested_reviewers`` / ``state`` / ``merged_at`` 字段做粗判.
PR-16c 可加 review fetch 改成精分类.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gitea_client import GiteaClient, GiteaClientError


CACHE_TTL_SECONDS = 30.0
REVIEWED_WINDOW_DAYS = 7

# 6 scope → search params 映射. 全部用 boolean filter (不依赖 username/whoami).
# Gitea API 接受多个 boolean 同时 = OR (拉所有命中至少一个的). plan §4.2 写的
# "union" 语义跟 Gitea 这个行为天然对齐.
_SCOPE_PARAMS: dict[str, dict[str, str]] = {
    # 跟 plan 一致: assigned ∪ created ∪ review_requested ∪ mentioned
    "related": {"assigned": "true", "created": "true",
                "review_requested": "true", "mentioned": "true"},
    "created": {"created": "true"},
    "assigned": {"assigned": "true"},
    "review": {"review_requested": "true", "reviewed": "true"},
    "participating": {"mentioned": "true"},
    "all": {"assigned": "true", "created": "true",
            "review_requested": "true", "mentioned": "true", "reviewed": "true"},
}


def list_scopes() -> list[str]:
    return list(_SCOPE_PARAMS.keys())


@dataclass(frozen=True)
class BoardCard:
    """前端 PrCard / IssueCard 共用 schema. 字段跟 plan §4.2 对齐."""
    kind: str            # "pr" | "issue"
    repo: str
    number: int
    title: str
    state: str           # "open" | "closed"
    tags: list[str] = field(default_factory=list)   # chips
    color_bar: str = "gray"                          # purple / orange / blue / gray
    url: str = ""
    updated_at: str = ""
    linked_team: str | None = None  # team_id or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "tags": list(self.tags),
            "color_bar": self.color_bar,
            "url": self.url,
            "updated_at": self.updated_at,
            "linked_team": self.linked_team,
        }


class GitBoardAggregator:
    """每个 scope 一份 cache. 同一个 scope 30s 内重复请求走 cache."""

    def __init__(self, client: GiteaClient | None = None, *,
                 ttl: float = CACHE_TTL_SECONDS,
                 team_links: "TeamLinks | None" = None) -> None:
        self._client = client or GiteaClient()
        self._ttl = ttl
        if team_links is None:
            from team_links import TeamLinks
            team_links = TeamLinks()
        self._team_links = team_links
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_error: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, scope: str | None = None) -> None:
        if scope is None:
            self._cache.clear()
            self._last_error.clear()
        else:
            self._cache.pop(scope, None)
            self._last_error.pop(scope, None)

    async def get_board(self, scope: str) -> dict[str, Any]:
        if scope not in _SCOPE_PARAMS:
            raise ValueError(f"unknown scope: {scope!r}; valid: {list_scopes()}")
        async with self._lock:
            now = time.time()
            cached = self._cache.get(scope)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
            try:
                board = await asyncio.to_thread(self._collect, scope)
            except GiteaClientError as exc:
                # 失败时, 如果还有老 cache 就返 stale=true; 否则透传错误
                self._last_error[scope] = repr(exc)
                if cached is not None:
                    fallback = dict(cached[1])
                    fallback["stale"] = True
                    fallback["error"] = str(exc)
                    return fallback
                raise
            self._cache[scope] = (now, board)
            self._last_error.pop(scope, None)
            return board

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------

    def _collect(self, scope: str) -> dict[str, Any]:
        params_base = dict(_SCOPE_PARAMS[scope])
        # 跟 plan §4.2 一致: PR 列查 open + closed-recent; issue 行只查 open
        pulls_open = self._client.search_issues({**params_base, "type": "pulls", "state": "open"})
        pulls_closed = self._client.search_issues({**params_base, "type": "pulls", "state": "closed"})
        issues_open = self._client.search_issues({**params_base, "type": "issues", "state": "open"})

        def _attach_link(card_dict: dict[str, Any], kind: str) -> None:
            team_id = self._team_links.lookup_by_target(kind, card_dict["repo"], card_dict["number"])
            if team_id:
                card_dict["linked_team"] = team_id

        columns: dict[str, list[dict[str, Any]]] = {
            "wip": [],
            "under_review": [],
            "reviewing": [],
            "reviewed": [],
        }
        seen_pr_keys: set[tuple[str, int]] = set()
        for pr in pulls_open:
            card = _pr_to_card(pr)
            if card is None:
                continue
            key = (card.repo, card.number)
            if key in seen_pr_keys:
                continue
            seen_pr_keys.add(key)
            column = _classify_open_pr(pr)
            if column == "wip":
                card_dict = card.to_dict()
                card_dict["color_bar"] = "gray"
                _attach_link(card_dict, "pr")
                columns["wip"].append(card_dict)
            elif column == "under_review":
                card_dict = card.to_dict()
                card_dict["color_bar"] = "blue"
                _attach_link(card_dict, "pr")
                columns["under_review"].append(card_dict)
            elif column == "reviewing":
                card_dict = card.to_dict()
                card_dict["color_bar"] = "purple"
                _attach_link(card_dict, "pr")
                columns["reviewing"].append(card_dict)

        for pr in pulls_closed:
            if not _is_recent_merged(pr):
                continue
            card = _pr_to_card(pr)
            if card is None:
                continue
            key = (card.repo, card.number)
            if key in seen_pr_keys:
                continue
            seen_pr_keys.add(key)
            card_dict = card.to_dict()
            card_dict["color_bar"] = "orange"
            _attach_link(card_dict, "pr")
            columns["reviewed"].append(card_dict)

        issues_row: list[dict[str, Any]] = []
        seen_issue_keys: set[tuple[str, int]] = set()
        for issue in issues_open:
            card = _issue_to_card(issue)
            if card is None:
                continue
            key = (card.repo, card.number)
            if key in seen_issue_keys:
                continue
            seen_issue_keys.add(key)
            card_dict = card.to_dict()
            card_dict["color_bar"] = "blue"
            _attach_link(card_dict, "issue")
            issues_row.append(card_dict)

        return {
            "scope": scope,
            "updated_at": _utc_now_iso(),
            "stale": False,
            "columns": columns,
            "issues_row": issues_row,
        }


# ---------------------------------------------------------------------------
# 分类规则 (plan §4.2)
# ---------------------------------------------------------------------------

def _classify_open_pr(pr: dict[str, Any]) -> str:
    """对 open PR 落到 wip / under_review / reviewing 之一.

    MVP 用 search 响应字段, 不打 reviews endpoint:
    - draft=true → wip
    - 非 draft + 有 requested_reviewers → under_review
    - 其余 (无 requested_reviewers 或字段缺失) → reviewing
    """
    if pr.get("draft") is True:
        return "wip"
    requested = pr.get("requested_reviewers")
    if isinstance(requested, list) and requested:
        return "under_review"
    return "reviewing"


def _is_recent_merged(pr: dict[str, Any]) -> bool:
    """PR closed=true + merged_at 在最近 N 天."""
    merged_at = pr.get("merged_at") or pr.get("closed_at")
    if not merged_at:
        return False
    try:
        ts = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    delta = datetime.now(timezone.utc) - ts
    return delta.days <= REVIEWED_WINDOW_DAYS


def _pr_to_card(pr: dict[str, Any]) -> BoardCard | None:
    repo = _extract_repo(pr)
    if not repo:
        return None
    try:
        number = int(pr.get("number"))
    except (TypeError, ValueError):
        return None
    return BoardCard(
        kind="pr",
        repo=repo,
        number=number,
        title=str(pr.get("title", "")),
        state=str(pr.get("state", "open")),
        tags=_derive_pr_tags(pr),
        url=str(pr.get("html_url", "")),
        updated_at=str(pr.get("updated_at", "")),
    )


def _issue_to_card(issue: dict[str, Any]) -> BoardCard | None:
    if issue.get("pull_request") is not None:
        return None
    repo = _extract_repo(issue)
    if not repo:
        return None
    try:
        number = int(issue.get("number"))
    except (TypeError, ValueError):
        return None
    return BoardCard(
        kind="issue",
        repo=repo,
        number=number,
        title=str(issue.get("title", "")),
        state=str(issue.get("state", "open")),
        tags=_derive_issue_tags(issue),
        url=str(issue.get("html_url", "")),
        updated_at=str(issue.get("updated_at", "")),
    )


def _derive_pr_tags(pr: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if pr.get("draft") is True:
        tags.append("wip")
    if pr.get("state") == "open":
        tags.append("open")
    requested = pr.get("requested_reviewers")
    if isinstance(requested, list) and requested:
        tags.append("review")
    return tags


def _derive_issue_tags(issue: dict[str, Any]) -> list[str]:
    if issue.get("state") == "open":
        return ["open"]
    return []


def _extract_repo(item: dict[str, Any]) -> str | None:
    repo_obj = item.get("repository")
    if isinstance(repo_obj, dict):
        full = repo_obj.get("full_name")
        if isinstance(full, str) and "/" in full:
            return full
    # Gitea 有时 issues_search 不返完整 repository 对象, 走 html_url 解析
    html = item.get("html_url") or ""
    parts = str(html).split("/")
    # https://git.k2lab.ai/<owner>/<repo>/(issues|pulls)/<num>
    if len(parts) >= 6 and parts[2] and parts[3] and parts[4]:
        return f"{parts[3]}/{parts[4]}"
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
