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

PR-19g: ``related`` / ``all`` 这种 union scope (3+ boolean filter OR 在一起)
在 Gitea 侧搜索极慢, 10s 默认 timeout 经常打挂. 改成在客户端拆: 多 boolean
union 时, 每个 boolean 独立发一次, 按 issue ``id`` 在客户端 dedupe.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gitea_client import (
    GiteaClient,
    GiteaClientError,
    GiteaRateLimitError,
    GiteaServerError,
    GiteaTransportError,
)


CACHE_TTL_SECONDS = 30.0
REVIEWED_WINDOW_DAYS = 7
# 看板专属 timeout. default GiteaClient 10s 在 union scope 12 次串行里偶发挂;
# 这里只影响看板, auto-review / watcher 仍走 10s.
BOARD_GITEA_TIMEOUT = 30.0

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
        self._client = client or GiteaClient(timeout=BOARD_GITEA_TIMEOUT)
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

    # Gitea search 支持的 boolean filter key 集合 (跟 _SCOPE_PARAMS 一致).
    _BOOLEAN_KEYS = ("assigned", "created", "review_requested", "mentioned", "reviewed")

    # 哪些错算"瞬态可跳过": 网络超时 / 服务器 5xx / 限流. 401/403/404/422
    # 是确定性 bug, 必须冒泡 (不应静默吞).
    _TRANSIENT_ERRORS = (GiteaTransportError, GiteaServerError, GiteaRateLimitError)

    def _search_union(
        self, type_: str, state: str, scope_params: dict[str, str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """串行 fan-out: scope 含 2+ 个 boolean 时, 每个 boolean 单发一次, 客户端 dedupe.

        Gitea ``/repos/issues/search`` 把多个 boolean OR 在一起时, 后端 SQL plan
        会爆 (实测 4 个 boolean + state=open 10s 超时). 拆开后每发只有 1 个
        boolean, 各自走索引, 总耗时反而更短.

        - ``scope_params`` 只能含 boolean key (来自 ``_SCOPE_PARAMS[scope]``);
          ``type`` / ``state`` 由参数显式指定避免被覆盖.
        - 单 boolean (created / assigned / participating) 保持原行为, 不拆.
        - dedupe by ``id`` (Gitea 全局唯一 issue id), 测试 fixture 缺 id 时回落
          到 ``html_url``.
        - partial-failure 容错: 多 boolean 时, 单个 boolean 子请求 transient
          失败 (timeout / 5xx / rate-limit) → 跳过该 boolean 继续, 返回 ok=False;
          所有 boolean 都失败才 raise (让上层走 stale-cache fallback).

        Returns:
            (items, ok) — ok=False 意味某个 boolean 被跳过 (数据不完整).
        """
        boolean_keys = [k for k in self._BOOLEAN_KEYS if scope_params.get(k) == "true"]
        # 把 boolean key 之外的 (理论上没有, 防御性写法) 也带上, 类型/状态用参数覆盖
        carry = {k: v for k, v in scope_params.items() if k not in self._BOOLEAN_KEYS}
        base = {**carry, "type": type_, "state": state}

        if len(boolean_keys) <= 1:
            params = dict(base)
            for k in boolean_keys:
                params[k] = "true"
            return self._client.search_issues(params), True

        seen_keys: set[Any] = set()
        out: list[dict[str, Any]] = []
        success_count = 0
        last_transient: Exception | None = None
        for bk in boolean_keys:
            params = dict(base)
            params[bk] = "true"
            try:
                items = self._client.search_issues(params)
            except self._TRANSIENT_ERRORS as exc:
                last_transient = exc
                continue
            success_count += 1
            for item in items:
                # 真实 Gitea response 一定有 id; 测试 fixture 可能没设, 回落到
                # html_url (Gitea 也保证 URL 唯一)
                key = item.get("id") or item.get("html_url")
                if key is None or key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append(item)

        if success_count == 0:
            assert last_transient is not None  # boolean_keys 非空 → 至少 1 次尝试
            raise last_transient
        return out, success_count == len(boolean_keys)

    def _collect(self, scope: str) -> dict[str, Any]:
        params_base = dict(_SCOPE_PARAMS[scope])
        # 跟 plan §4.2 一致: PR 列查 open + closed-recent; issue 行只查 open
        pulls_open, ok_open = self._search_union("pulls", "open", params_base)
        pulls_closed, ok_closed = self._search_union("pulls", "closed", params_base)
        issues_open, ok_issues = self._search_union("issues", "open", params_base)
        all_ok = ok_open and ok_closed and ok_issues

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
            # union scope 任一 boolean 子请求 transient 失败 → stale=True 提示
            # 前端 (前端已渲染 "Stale" 标签). 完整成功才 stale=False.
            "stale": not all_ok,
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
