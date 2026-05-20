"""SY-2 (#19) git worktree workspace manager.

让每个 issue 拿独立 git worktree, 真物理隔离 (并发改同 repo 不互踩). 之前
dashboard 模式所有 court 共用同一个 repo checkout (tmux window 只 UI 隔离),
两个 court 同时改同一文件会后写覆盖前写.

设计:
- 元数据 (git 内部) 走 ``<source_repo>/.git/worktrees/<safe_key>/``
- 工作目录走 ``<root>/<safe_key>/`` (默认 ``~/.agent-court/worktrees/``)
- 分支名 ``<branch_prefix><safe_key>`` (例如 ``auto/issue-K2Lab-moras-finder-7``)
- safe_key 把 ``<repo>#<num>`` 里的 ``/`` ``#`` 全换 ``-`` (tmux 安全 + 路径安全)

失败恢复:
- 工作目录还在但分支已删 → cleanup 时 ``git worktree remove --force``
- 分支冲突 (上次没清干净) → ``git worktree add --force`` 重用; 仍冲突就抛
- 残留 worktree (active issue 已 DONE 但目录还在) → ``list_orphans`` + caller
  调 ``cleanup_worktree`` 收尾 (orchestrator SY-3 启动期 reconcile 时用)

跟 WORKFLOW.md 的 ``working_dir_strategy`` 字段对齐:
- ``inplace`` (默认): caller 不调本模块, 走老路径
- ``worktree``: caller 调 ``create_worktree`` 起 court, ``cleanup_worktree``
  在 report-back done 时收尾
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BRANCH_PREFIX = "auto/issue-"
DEFAULT_ROOT_NAME = "worktrees"

# safe key 字符集: 字母 / 数字 / 下划线 / 短横; 其它一律换 "-"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


class WorkspaceError(RuntimeError):
    """create/cleanup 失败的统一封装."""


@dataclass(frozen=True)
class Worktree:
    issue_key: str
    safe_key: str
    branch: str
    path: Path
    source_repo: Path


def safe_key(issue_key: str) -> str:
    """``K2Lab/moras-finder#7`` → ``K2Lab-moras-finder-7``.

    保留大小写 (避免不同 owner 撞 lowercase 名); 多个 unsafe char 折叠成一个 ``-``;
    前后 ``-`` 修剪掉.
    """
    if not issue_key:
        raise ValueError("issue_key must not be empty")
    sk = _UNSAFE_CHARS.sub("-", issue_key).strip("-")
    if not sk:
        raise ValueError(f"issue_key {issue_key!r} produced empty safe_key")
    return sk


class WorkspaceManager:
    def __init__(
        self,
        root: Path | None = None,
        branch_prefix: str = DEFAULT_BRANCH_PREFIX,
        *,
        git_bin: str = "git",
    ) -> None:
        self.root = root or (Path.home() / ".agent-court" / DEFAULT_ROOT_NAME)
        self.branch_prefix = branch_prefix
        self.git_bin = git_bin

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def worktree_for(self, issue_key: str) -> Path:
        return self.root / safe_key(issue_key)

    def create_worktree(
        self,
        source_repo: Path,
        issue_key: str,
        *,
        base_ref: str = "HEAD",
        reuse_if_exists: bool = True,
    ) -> Worktree:
        """在 ``source_repo`` 里 git worktree add 一个新 worktree.

        ``reuse_if_exists=True`` 时, 目录已存在 → 验证它确实是个 worktree 直接重用;
        ``False`` 时存在即抛.
        """
        if not source_repo.is_dir():
            raise WorkspaceError(f"source_repo not found: {source_repo}")
        sk = safe_key(issue_key)
        wt_path = self.root / sk
        branch = f"{self.branch_prefix}{sk}"
        self.root.mkdir(parents=True, exist_ok=True)

        if wt_path.exists():
            if not reuse_if_exists:
                raise WorkspaceError(f"worktree already exists: {wt_path}")
            # 已存在 → 校验它是合法 worktree (内部有 .git 文件 / 目录)
            if (wt_path / ".git").exists():
                return Worktree(issue_key=issue_key, safe_key=sk, branch=branch,
                                path=wt_path, source_repo=source_repo)
            # 目录在但不是 worktree → 残骸, 删了重建
            shutil.rmtree(wt_path)

        # 先尝试新建分支; 分支已存在 (上次没清干净) → 重用现有分支
        rc, stdout, stderr = self._run_git(
            source_repo, "worktree", "add", "-b", branch, str(wt_path), base_ref
        )
        if rc != 0:
            # 分支已存在 → 不新建分支, attach 模式
            if "already exists" in stderr or "already used" in stderr:
                rc2, _, stderr2 = self._run_git(
                    source_repo, "worktree", "add", str(wt_path), branch
                )
                if rc2 != 0:
                    raise WorkspaceError(
                        f"git worktree add failed (branch reuse): {stderr2.strip()}"
                    )
            else:
                raise WorkspaceError(f"git worktree add failed: {stderr.strip()}")
        return Worktree(
            issue_key=issue_key, safe_key=sk, branch=branch,
            path=wt_path, source_repo=source_repo,
        )

    def cleanup_worktree(self, source_repo: Path, issue_key: str) -> bool:
        """``git worktree remove --force`` + 清残留目录. 返 True 表示有东西被清."""
        sk = safe_key(issue_key)
        wt_path = self.root / sk
        cleaned = False
        if source_repo.is_dir():
            rc, _, _ = self._run_git(
                source_repo, "worktree", "remove", "--force", str(wt_path)
            )
            if rc == 0:
                cleaned = True
            # rc != 0 一般是 "not a working tree"; 不抛, 走下面 fallback
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
            cleaned = True
        return cleaned

    def list_orphans(self, source_repo: Path, active_safe_keys: set[str]) -> list[Path]:
        """扫 root 下所有目录, 返不在 ``active_safe_keys`` 里的 (orchestrator 启动期 reconcile 用).

        ``source_repo`` 当前没用到 (只扫目录名); 留作将来对比 ``git worktree list``
        实际状态的预留参数.
        """
        del source_repo  # 当前未用; 保留参数为后续 reconcile 扩展
        if not self.root.is_dir():
            return []
        orphans: list[Path] = []
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name not in active_safe_keys:
                orphans.append(entry)
        return orphans

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_git(self, cwd: Path, *args: str) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [self.git_bin, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git invocation failed: {exc}") from exc
        return proc.returncode, proc.stdout, proc.stderr
