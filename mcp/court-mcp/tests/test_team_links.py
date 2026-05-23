"""TeamLinks 双向索引 + 持久化测试 (PR-17b)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import team_links as tl  # noqa: E402


def test_empty_state_has_two_indexes(tmp_path):
    links = tl.TeamLinks(court_root=tmp_path)
    assert links.list_by_team() == {}
    assert links.lookup_by_target("pr", "K2Lab/foo", 441) is None
    assert links.lookup_by_team("agent-team-xxxx") is None


def test_set_link_writes_both_indexes(tmp_path):
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-abc12345", "K2Lab/foo", 441, "pr",
                   "https://git.k2lab.ai/K2Lab/foo/pulls/441")
    assert links.lookup_by_team("agent-team-abc12345") == {
        "repo": "K2Lab/foo", "number": 441, "kind": "pr",
        "url": "https://git.k2lab.ai/K2Lab/foo/pulls/441",
    }
    assert links.lookup_by_target("pr", "K2Lab/foo", 441) == "agent-team-abc12345"


def test_set_link_persists_to_disk_chmod_600(tmp_path):
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-abc12345", "K2Lab/foo", 441, "pr", "url")
    path = tmp_path / "team-links.json"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    raw = json.loads(path.read_text())
    assert "by_team" in raw and "by_target" in raw


def test_load_existing_state(tmp_path):
    links1 = tl.TeamLinks(court_root=tmp_path)
    links1.set_link("agent-team-abc12345", "K2Lab/foo", 441, "pr", "url")
    links2 = tl.TeamLinks(court_root=tmp_path)
    assert links2.lookup_by_team("agent-team-abc12345") is not None


def test_remove_link_clears_both(tmp_path):
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-abc12345", "K2Lab/foo", 441, "pr", "url")
    links.remove_link("agent-team-abc12345")
    assert links.lookup_by_team("agent-team-abc12345") is None
    assert links.lookup_by_target("pr", "K2Lab/foo", 441) is None


def test_corrupt_file_treated_as_empty(tmp_path):
    (tmp_path / "team-links.json").write_text("not json")
    links = tl.TeamLinks(court_root=tmp_path)
    assert links.list_by_team() == {}


def test_set_link_overwrite_clears_old_target(tmp_path):
    """重新分配同一 team_id 到新 target 时, 旧 by_target 条目应被清理 (防僵尸)."""
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-t1", "K2Lab/foo", 100, "pr", "url1")
    links.set_link("agent-team-t1", "K2Lab/foo", 200, "pr", "url2")
    assert links.lookup_by_target("pr", "K2Lab/foo", 100) is None
    assert links.lookup_by_target("pr", "K2Lab/foo", 200) == "agent-team-t1"
    # by_team 也应反映最新 target
    assert links.lookup_by_team("agent-team-t1") == {
        "repo": "K2Lab/foo", "number": 200, "kind": "pr", "url": "url2",
    }


def test_remove_link_nonexistent_no_disk_write(tmp_path):
    """remove_link 对不存在的 team_id 不应触发不必要的磁盘写入."""
    links = tl.TeamLinks(court_root=tmp_path)
    # 还没 set 任何东西, links 文件应不存在
    path = tmp_path / "team-links.json"
    assert not path.exists()
    links.remove_link("agent-team-never-existed")
    # remove_link 不该建文件
    assert not path.exists()


# ---- PR-19c-1: cleanup_orphans ----

def test_cleanup_orphans_removes_dead_tmux_sessions(tmp_path):
    """tmux 不存在的 agent-team-* link 被清掉; live 的保留."""
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-alive1", "K2Lab/foo", 1, "pr", "u1")
    links.set_link("agent-team-dead1", "K2Lab/foo", 2, "pr", "u2")
    links.set_link("agent-team-dead2", "K2Lab/bar", 3, "issue", "u3")

    cleaned = links.cleanup_orphans(live_sessions={"agent-team-alive1"})

    assert sorted(cleaned) == ["agent-team-dead1", "agent-team-dead2"]
    assert links.lookup_by_team("agent-team-alive1") is not None
    assert links.lookup_by_team("agent-team-dead1") is None
    assert links.lookup_by_team("agent-team-dead2") is None
    # by_target 也应同步清掉
    assert links.lookup_by_target("pr", "K2Lab/foo", 2) is None
    assert links.lookup_by_target("issue", "K2Lab/bar", 3) is None
    assert links.lookup_by_target("pr", "K2Lab/foo", 1) == "agent-team-alive1"


def test_cleanup_orphans_skips_non_agent_team_prefix(tmp_path):
    """非 agent-team- 前缀的 link 不在清理范围 (将来 ghostty 可能挂 link)."""
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-dead", "K2Lab/foo", 1, "pr", "u1")
    # 模拟一个不是 dashboard spawn 的 link
    links._by_team["ghostty:ttys001"] = {"repo": "K2Lab/foo", "number": 99, "kind": "pr", "url": "x"}
    links._by_target["pr:K2Lab/foo#99"] = "ghostty:ttys001"
    links._save()

    cleaned = links.cleanup_orphans(live_sessions=set())

    assert cleaned == ["agent-team-dead"]
    # ghostty 来源的 link 不动
    assert links.lookup_by_team("ghostty:ttys001") is not None


def test_cleanup_orphans_no_disk_write_when_all_alive(tmp_path):
    """全 live 时 cleanup 不触发磁盘写入."""
    links = tl.TeamLinks(court_root=tmp_path)
    links.set_link("agent-team-a", "K2Lab/foo", 1, "pr", "u1")
    path = tmp_path / "team-links.json"
    mtime_before = path.stat().st_mtime
    import time; time.sleep(0.01)
    cleaned = links.cleanup_orphans(live_sessions={"agent-team-a"})
    assert cleaned == []
    assert path.stat().st_mtime == mtime_before  # 没动


def test_cleanup_orphans_empty_store_returns_empty(tmp_path):
    links = tl.TeamLinks(court_root=tmp_path)
    assert links.cleanup_orphans(live_sessions=set()) == []
