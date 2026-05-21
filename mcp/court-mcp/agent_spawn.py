"""PR-17b: AgentSpawner — 后端起 tmux session 跑 claude + 投递任务文本.

流程:
1. 校验 kind in {pr, issue}
2. 查 team_links: 已 linked -> 直接返已有 team_id (dedup)
3. 生成 team_id = "agent-team-<8字符 uuid>"
4. tmux new-session -d -s <team_id>
5. send-keys: 启 claude (1 行) + 投递任务文本 (1 行)
6. team_links.set_link 落盘
"""
from __future__ import annotations

import subprocess
import uuid
from typing import Any

from team_links import TeamLinks

VALID_KINDS = {"pr", "issue"}


class SpawnError(RuntimeError):
    pass


def _generate_team_uuid() -> str:
    return uuid.uuid4().hex[:8]


def _build_task_text(repo: str, number: int, kind: str, url: str) -> str:
    if kind == "pr":
        return f"请处理 {repo} PR #{number}: 拉下来 review 并给出改建议. URL: {url}"
    # issue
    return f"请处理 {repo} issue #{number}: 分析问题并给方案. URL: {url}"


class AgentSpawner:
    def __init__(self, *, team_links: TeamLinks, cwd_for_session: str | None = None) -> None:
        self.team_links = team_links
        self.cwd = cwd_for_session

    def spawn(self, *, repo: str, number: int, kind: str, url: str) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

        existing = self.team_links.lookup_by_target(kind, repo, number)
        if existing:
            record = self.team_links.lookup_by_team(existing)
            return {
                "team_id": existing,
                "session": existing,
                "already_spawned": True,
                "linked": record,
            }

        team_id = f"agent-team-{_generate_team_uuid()}"

        # tmux new-session -d -s <team_id> [-c <cwd>]
        new_args = ["tmux", "new-session", "-d", "-s", team_id]
        if self.cwd:
            new_args += ["-c", self.cwd]
        proc = subprocess.run(new_args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SpawnError(f"tmux new-session failed: {proc.stderr.strip()}")

        # send-keys: 启 claude
        subprocess.run(
            ["tmux", "send-keys", "-t", team_id, "claude", "Enter"],
            capture_output=True, text=True,
        )
        # send-keys: 投递任务
        task_text = _build_task_text(repo, number, kind, url)
        subprocess.run(
            ["tmux", "send-keys", "-t", team_id, task_text, "Enter"],
            capture_output=True, text=True,
        )

        self.team_links.set_link(team_id, repo, number, kind, url)
        return {
            "team_id": team_id,
            "session": team_id,
            "already_spawned": False,
            "linked": {"repo": repo, "number": number, "kind": kind, "url": url},
        }
