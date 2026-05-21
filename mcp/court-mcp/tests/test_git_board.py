"""PR-16b: GitBoardAggregator 单元测试.

Mock GiteaClient.search_issues 注入不同 payload, 验证:
- 4 列分类规则 (wip / under_review / reviewing / reviewed)
- 6 个 scope 各自的 search params 映射
- cache TTL 行为
- Gitea 错误透传 + stale fallback
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import git_board as gb  # noqa: E402
from gitea_client import GiteaServerError  # noqa: E402


def _fake_pr(repo: str, num: int, *, state: str = "open", draft: bool = False,
             requested_reviewers: list | None = None, merged_at: str | None = None,
             title: str = "fix") -> dict:
    return {
        "number": num,
        "title": title,
        "state": state,
        "draft": draft,
        "requested_reviewers": requested_reviewers or [],
        "merged_at": merged_at,
        "html_url": f"https://git.k2lab.ai/{repo}/pulls/{num}",
        "repository": {"full_name": repo},
        "updated_at": "2026-05-21T10:00:00Z",
        "pull_request": {"merged": merged_at is not None},
    }


def _fake_issue(repo: str, num: int, *, state: str = "open", title: str = "bug") -> dict:
    return {
        "number": num,
        "title": title,
        "state": state,
        "html_url": f"https://git.k2lab.ai/{repo}/issues/{num}",
        "repository": {"full_name": repo},
        "updated_at": "2026-05-21T10:00:00Z",
        # pull_request 字段为 None 才算 issue
        "pull_request": None,
    }


def _recent_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# scope mapping
# ---------------------------------------------------------------------------

def test_list_scopes_covers_six():
    scopes = gb.list_scopes()
    assert set(scopes) == {"related", "created", "assigned", "review", "participating", "all"}


def test_scope_params_match_plan_intent():
    # 抽样验证: created → only created=true; review → review_requested + reviewed; all → 5 个 union
    assert gb._SCOPE_PARAMS["created"] == {"created": "true"}
    assert set(gb._SCOPE_PARAMS["review"].keys()) == {"review_requested", "reviewed"}
    assert set(gb._SCOPE_PARAMS["all"].keys()) >= {
        "assigned", "created", "review_requested", "mentioned", "reviewed",
    }


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_classify_open_pr_draft_to_wip():
    assert gb._classify_open_pr(_fake_pr("a/b", 1, draft=True)) == "wip"


def test_classify_open_pr_with_reviewers_to_under_review():
    pr = _fake_pr("a/b", 2, requested_reviewers=[{"login": "alice"}])
    assert gb._classify_open_pr(pr) == "under_review"


def test_classify_open_pr_default_to_reviewing():
    pr = _fake_pr("a/b", 3, requested_reviewers=[])
    assert gb._classify_open_pr(pr) == "reviewing"


def test_is_recent_merged_within_window():
    assert gb._is_recent_merged(_fake_pr("a/b", 1, merged_at=_recent_iso())) is True


def test_is_recent_merged_old_pr_excluded():
    assert gb._is_recent_merged(_fake_pr("a/b", 1, merged_at=_old_iso())) is False


def test_is_recent_merged_no_merged_at_excluded():
    assert gb._is_recent_merged({"merged_at": None, "closed_at": None}) is False


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_board_classifies_three_columns(monkeypatch):
    client = MagicMock()
    pulls_open = [
        _fake_pr("K2Lab/a", 1, draft=True, title="wip pr"),
        _fake_pr("K2Lab/a", 2, requested_reviewers=[{"login": "x"}], title="under"),
        _fake_pr("K2Lab/a", 3, title="reviewing"),
    ]
    pulls_closed = [_fake_pr("K2Lab/a", 4, state="closed", merged_at=_recent_iso(), title="done")]
    issues_open = [_fake_issue("K2Lab/a", 100, title="open bug")]

    def fake_search(params: dict) -> list:
        t = params.get("type")
        state = params.get("state")
        if t == "pulls" and state == "open":
            return pulls_open
        if t == "pulls" and state == "closed":
            return pulls_closed
        if t == "issues" and state == "open":
            return issues_open
        return []

    client.search_issues.side_effect = fake_search

    agg = gb.GitBoardAggregator(client=client)
    board = await agg.get_board("related")

    titles = lambda col: [c["title"] for c in board["columns"][col]]
    assert titles("wip") == ["wip pr"]
    assert titles("under_review") == ["under"]
    assert titles("reviewing") == ["reviewing"]
    assert titles("reviewed") == ["done"]
    assert [c["title"] for c in board["issues_row"]] == ["open bug"]
    assert board["scope"] == "related"
    assert board["stale"] is False


@pytest.mark.asyncio
async def test_invalid_scope_raises():
    agg = gb.GitBoardAggregator(client=MagicMock())
    with pytest.raises(ValueError):
        await agg.get_board("nonsense")


@pytest.mark.asyncio
async def test_cache_hit_skips_client_call():
    client = MagicMock()
    client.search_issues.return_value = []
    agg = gb.GitBoardAggregator(client=client, ttl=5.0)
    await agg.get_board("created")
    await agg.get_board("created")
    # 3 次调用 / 第一次 collect: pulls_open + pulls_closed + issues_open = 3 次
    assert client.search_issues.call_count == 3


@pytest.mark.asyncio
async def test_invalidate_forces_refresh():
    client = MagicMock()
    client.search_issues.return_value = []
    agg = gb.GitBoardAggregator(client=client, ttl=60.0)
    await agg.get_board("created")
    agg.invalidate("created")
    await agg.get_board("created")
    assert client.search_issues.call_count == 6  # 2 个 collect * 3 次


@pytest.mark.asyncio
async def test_gitea_error_falls_back_to_stale_cache():
    client = MagicMock()
    # 第一次成功, 第二次失败
    call_count = {"n": 0}

    def fake_search(params: dict) -> list:
        if call_count["n"] < 3:
            call_count["n"] += 1
            return []
        raise GiteaServerError("upstream 503")

    client.search_issues.side_effect = fake_search
    agg = gb.GitBoardAggregator(client=client, ttl=0.0)  # 永过期, 强制每次重拉
    await agg.get_board("created")  # 成功, cache 写入
    fallback = await agg.get_board("created")  # 失败, 走 stale fallback
    assert fallback["stale"] is True
    assert "upstream 503" in fallback["error"]


@pytest.mark.asyncio
async def test_gitea_error_no_cache_raises():
    client = MagicMock()
    client.search_issues.side_effect = GiteaServerError("upstream 503")
    agg = gb.GitBoardAggregator(client=client)
    with pytest.raises(GiteaServerError):
        await agg.get_board("created")


@pytest.mark.asyncio
async def test_dedup_pr_across_state_queries():
    """closed 查询返回的 PR 若已经在 open 列出现 (Gitea state=all 用法), 不重复入卡."""
    client = MagicMock()
    pr_open = _fake_pr("K2Lab/a", 1, title="dup")
    pr_closed = _fake_pr("K2Lab/a", 1, state="closed", merged_at=_recent_iso(), title="dup")

    def fake_search(params: dict) -> list:
        t = params.get("type")
        state = params.get("state")
        if t == "pulls" and state == "open":
            return [pr_open]
        if t == "pulls" and state == "closed":
            return [pr_closed]
        return []

    client.search_issues.side_effect = fake_search
    agg = gb.GitBoardAggregator(client=client)
    board = await agg.get_board("related")
    # 应该只进 reviewing 列, 不重复进 reviewed
    all_cards = sum((board["columns"][k] for k in ("wip", "under_review", "reviewing", "reviewed")), [])
    assert len(all_cards) == 1
    assert all_cards[0]["state"] == "open"


@pytest.mark.asyncio
async def test_card_includes_linked_team_id(tmp_path):
    import team_links as tl
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-xyz", "K2Lab/a", 1, "pr", "https://x/")

    client = MagicMock()

    def fake_search(params):
        if params.get("type") == "pulls" and params.get("state") == "open":
            return [_fake_pr("K2Lab/a", 1, title="hello")]
        return []

    client.search_issues.side_effect = fake_search
    agg = gb.GitBoardAggregator(client=client, team_links=links)
    board = await agg.get_board("created")
    assert board["columns"]["reviewing"][0]["linked_team"] == "agent-team-xyz"


@pytest.mark.asyncio
async def test_card_without_link_has_null_linked_team(tmp_path):
    import team_links as tl
    links = tl.TeamLinks(court_root=tmp_path)
    client = MagicMock()

    def fake_search(params):
        if params.get("type") == "pulls" and params.get("state") == "open":
            return [_fake_pr("K2Lab/a", 2, title="solo")]
        return []

    client.search_issues.side_effect = fake_search
    agg = gb.GitBoardAggregator(client=client, team_links=links)
    board = await agg.get_board("created")
    assert board["columns"]["reviewing"][0]["linked_team"] is None
