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
