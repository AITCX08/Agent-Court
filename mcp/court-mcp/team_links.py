"""PR-17b: team_id ⇄ (repo, number, kind) 双向索引.

落盘 ~/.agent-court/team-links.json (chmod 600). 双索引保证两边查 O(1):

    by_team: {team_id: {repo, number, kind, url}}
    by_target: {"pr:K2Lab/foo#441": team_id}

target key 格式: "{kind}:{repo}#{number}"
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LINKS_FILE = "team-links.json"
LINKS_FILE_MODE = 0o600


def _target_key(kind: str, repo: str, number: int) -> str:
    return f"{kind}:{repo}#{number}"


class TeamLinks:
    def __init__(self, court_root: Path | None = None) -> None:
        self.court_root = court_root or (Path.home() / ".agent-court")
        self._by_team: dict[str, dict[str, Any]] = {}
        self._by_target: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = self.court_root / LINKS_FILE
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        bt = data.get("by_team", {})
        bg = data.get("by_target", {})
        if isinstance(bt, dict):
            self._by_team = {k: v for k, v in bt.items() if isinstance(v, dict)}
        if isinstance(bg, dict):
            self._by_target = {k: v for k, v in bg.items() if isinstance(v, str)}

    def _save(self) -> None:
        self.court_root.mkdir(parents=True, exist_ok=True)
        path = self.court_root / LINKS_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"by_team": self._by_team, "by_target": self._by_target},
            ensure_ascii=False, indent=2,
        ))
        try:
            os.chmod(tmp, LINKS_FILE_MODE)
        except OSError:
            pass
        tmp.replace(path)

    def set_link(self, team_id: str, repo: str, number: int, kind: str, url: str) -> None:
        old = self._by_team.get(team_id)
        if old is not None:
            self._by_target.pop(_target_key(old["kind"], old["repo"], old["number"]), None)
        self._by_team[team_id] = {"repo": repo, "number": number, "kind": kind, "url": url}
        self._by_target[_target_key(kind, repo, number)] = team_id
        self._save()

    def remove_link(self, team_id: str) -> None:
        record = self._by_team.pop(team_id, None)
        if record is None:
            return
        self._by_target.pop(_target_key(record["kind"], record["repo"], record["number"]), None)
        self._save()

    def lookup_by_team(self, team_id: str) -> dict[str, Any] | None:
        record = self._by_team.get(team_id)
        return dict(record) if record else None

    def lookup_by_target(self, kind: str, repo: str, number: int) -> str | None:
        return self._by_target.get(_target_key(kind, repo, number))

    def list_by_team(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._by_team.items()}
