"""SY-2 (#19): workspace_manager 测试 — 用真实 git 在 tmp_path 验证."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace_manager as wm  # noqa: E402


def _init_repo(path: Path) -> Path:
    """tmp 里 init 一个真 git repo (1 commit), 返路径."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# safe_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("K2Lab/moras-finder#7", "K2Lab-moras-finder-7"),
        ("foo/bar#1", "foo-bar-1"),
        ("AITCX08/agent-court#15", "AITCX08-agent-court-15"),
        ("repo with spaces#9", "repo-with-spaces-9"),
        ("noslash#1", "noslash-1"),
    ],
)
def test_safe_key_normalizes_unsafe_chars(raw, expected):
    assert wm.safe_key(raw) == expected


def test_safe_key_empty_raises():
    with pytest.raises(ValueError):
        wm.safe_key("")
    with pytest.raises(ValueError):
        wm.safe_key("///###")


# ---------------------------------------------------------------------------
# create_worktree (真 git)
# ---------------------------------------------------------------------------

def test_create_worktree_makes_real_worktree_with_branch(tmp_path):
    repo = _init_repo(tmp_path / "source")
    root = tmp_path / "wt-root"
    mgr = wm.WorkspaceManager(root=root)
    wt = mgr.create_worktree(repo, "K2Lab/moras-finder#7")
    assert wt.path == root / "K2Lab-moras-finder-7"
    assert wt.path.is_dir()
    assert wt.branch == "auto/issue-K2Lab-moras-finder-7"
    # 是真 git worktree (.git 是文件指向 source/.git/worktrees/...)
    assert (wt.path / ".git").exists()
    # README 在 (因为 base_ref=HEAD)
    assert (wt.path / "README.md").is_file()
    # source repo 看得到分支
    out = subprocess.check_output(["git", "branch"], cwd=repo, text=True)
    assert wt.branch in out


def test_create_worktree_reuses_existing_when_reuse_true(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    wt1 = mgr.create_worktree(repo, "foo/bar#1")
    wt2 = mgr.create_worktree(repo, "foo/bar#1", reuse_if_exists=True)
    assert wt1.path == wt2.path
    assert wt1.branch == wt2.branch


def test_create_worktree_raises_when_exists_and_reuse_false(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    mgr.create_worktree(repo, "foo/bar#1")
    with pytest.raises(wm.WorkspaceError, match="already exists"):
        mgr.create_worktree(repo, "foo/bar#1", reuse_if_exists=False)


def test_create_worktree_missing_source_raises(tmp_path):
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    with pytest.raises(wm.WorkspaceError, match="source_repo not found"):
        mgr.create_worktree(tmp_path / "nope", "x/y#1")


def test_create_worktree_repurposes_residue_dir(tmp_path):
    """目录在但不是 worktree (残骸) → 删了重建."""
    repo = _init_repo(tmp_path / "source")
    root = tmp_path / "wt-root"
    root.mkdir()
    residue = root / "foo-bar-1"
    residue.mkdir()
    (residue / "stale.txt").write_text("old")
    mgr = wm.WorkspaceManager(root=root)
    wt = mgr.create_worktree(repo, "foo/bar#1")
    assert wt.path.is_dir()
    assert (wt.path / "README.md").is_file()
    assert not (wt.path / "stale.txt").exists()


def test_create_worktree_with_preexisting_branch_attaches(tmp_path):
    """上次没清干净分支已存在 → 不新建分支, attach 复用."""
    repo = _init_repo(tmp_path / "source")
    # 预先建分支
    subprocess.run(["git", "branch", "auto/issue-foo-bar-1"], cwd=repo, check=True, capture_output=True)
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    wt = mgr.create_worktree(repo, "foo/bar#1")
    assert wt.path.is_dir()
    # 仍是同一分支
    out = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt.path, text=True)
    assert out.strip() == "auto/issue-foo-bar-1"


def test_cleanup_worktree_removes_dir_and_git_metadata(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    wt = mgr.create_worktree(repo, "foo/bar#1")
    assert wt.path.is_dir()

    cleaned = mgr.cleanup_worktree(repo, "foo/bar#1")
    assert cleaned is True
    assert not wt.path.exists()
    # git worktree list 里不应再有
    out = subprocess.check_output(["git", "worktree", "list"], cwd=repo, text=True)
    assert "foo-bar-1" not in out


def test_cleanup_worktree_handles_already_gone_gracefully(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    # 没建过, 直接 cleanup 不抛
    cleaned = mgr.cleanup_worktree(repo, "never-existed/x#1")
    assert cleaned is False


def test_cleanup_worktree_cleans_orphan_dir_even_without_git_metadata(tmp_path):
    """worktree git 元数据已被 git gc 清掉, 但工作目录还在 → cleanup 应该收尾."""
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    # 手造一个孤立目录 (不是真 worktree)
    orphan = mgr.root / "stale-x-1"
    orphan.mkdir(parents=True)
    (orphan / "junk.txt").write_text("x")
    cleaned = mgr.cleanup_worktree(repo, "stale/x#1")
    assert cleaned is True
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# list_orphans
# ---------------------------------------------------------------------------

def test_list_orphans_returns_dirs_not_in_active_set(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    mgr.create_worktree(repo, "foo/bar#1")
    mgr.create_worktree(repo, "foo/bar#2")
    mgr.create_worktree(repo, "x/y#9")
    orphans = mgr.list_orphans(repo, active_safe_keys={"foo-bar-1"})
    orphan_names = {p.name for p in orphans}
    # 只有 foo-bar-1 是 active; 其它两个算 orphan
    assert orphan_names == {"foo-bar-2", "x-y-9"}


def test_list_orphans_empty_when_root_missing(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "nope")
    assert mgr.list_orphans(repo, active_safe_keys=set()) == []


def test_worktree_for_returns_path_without_creating(tmp_path):
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root")
    p = mgr.worktree_for("foo/bar#1")
    assert p == tmp_path / "wt-root" / "foo-bar-1"
    assert not p.exists()


# ---------------------------------------------------------------------------
# git binary missing
# ---------------------------------------------------------------------------

def test_missing_git_binary_raises_workspace_error(tmp_path):
    repo = _init_repo(tmp_path / "source")
    mgr = wm.WorkspaceManager(root=tmp_path / "wt-root", git_bin="git-does-not-exist-xyz")
    with pytest.raises(wm.WorkspaceError, match="git invocation failed"):
        mgr.create_worktree(repo, "foo/bar#1")
