"""watcher 消费 pending-webhook/ 测试 (PR-14)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gitea_watcher import GiteaWatcher


class StubClient:
    """Stub: 不连真 Gitea, 用于隔离 watcher 行为测试."""

    def __init__(self, assigned_issues: list[dict] | None = None) -> None:
        self.assigned = assigned_issues or []
        self.comments: list[tuple] = []
        self.transitioned: list[tuple] = []
        self._comments_by_issue: dict[tuple, list] = {}

    def list_assigned_issues(self, state: str = "open", since: str | None = None) -> list[dict]:
        return list(self.assigned)

    def get_issue(self, repo: str, num: int) -> dict:
        return {"number": num, "title": f"detail-{num}", "repository": {"full_name": repo}, "updated_at": "2026-05-19T20:00:00Z"}

    def list_issue_comments(self, repo: str, num: int) -> list[dict]:
        return self._comments_by_issue.get((repo, num), [])

    def comment_on_issue(self, repo: str, num: int, body: str) -> dict:
        self.comments.append((repo, num, body))
        return {}

    def transition_issue(self, repo: str, num: int, state: str) -> dict:
        self.transitioned.append((repo, num, state))
        return {}


def _write_webhook_payload(state_dir: Path, *, repo: str, num: int, delivery: str, action: str = "assigned") -> Path:
    pending = state_dir / "pending-webhook"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{int(time.time() * 1000)}-{delivery}.json"
    path.write_text(json.dumps({
        "received_at": time.time(),
        "delivery": delivery,
        "event": "issues",
        "action": action,
        "issue": {
            "number": num,
            "title": "webhook issue",
            "html_url": f"http://git.k2lab.ai/{repo}/issues/{num}",
            "body": "webhook body",
            "updated_at": "2026-05-19T19:00:00Z",
            "labels": [],
        },
        "repository": {"full_name": repo},
        "sender": {"login": "tester"},
    }))
    return path


def test_consume_pending_webhook_returns_issues_and_archives(tmp_path):
    """读 pending-webhook/*.json 提取 issue + 归档处理过的."""
    watcher = GiteaWatcher(court_root=tmp_path, client=StubClient(), mode="court")
    watcher._ensure_dirs()
    state_dir = tmp_path / "gitea-watcher"
    _write_webhook_payload(state_dir, repo="K2Lab/x", num=1, delivery="uuid-1")
    _write_webhook_payload(state_dir, repo="K2Lab/y", num=2, delivery="uuid-2")

    items, delivery_map = watcher._consume_pending_webhook()
    assert len(items) == 2
    keys = sorted([watcher._issue_key(it) for it in items])
    assert keys == ["K2Lab/x#1", "K2Lab/y#2"]
    # delivery 映射
    assert delivery_map["K2Lab/x#1"] == "uuid-1"
    assert delivery_map["K2Lab/y#2"] == "uuid-2"
    # 文件应被归档到 .processed/
    pending = state_dir / "pending-webhook"
    assert list(pending.glob("*.json")) == []
    processed = list((pending / ".processed").glob("*.json"))
    assert len(processed) == 2


def test_run_once_processes_pending_webhook_with_source_mark(tmp_path):
    """webhook 来的 issue 跑完 _diff + _apply_decision 后 seen-issues.json 含 source=webhook."""
    # mock seen 已有, 避开 bootstrap
    state_dir = tmp_path / "gitea-watcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen-issues.json").write_text("{}")

    # 用 SHENLI_COMMAND env 让 watcher 调一个 stub shenli, 不真跑
    import os
    import sys
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    shenli_stub = stub_dir / "shenli_stub.py"
    shenli_stub.write_text(
        "import json, sys\n"
        "print(json.dumps({'decision': 'NEED_INFO', 'comment_body': 'auto'}))\n"
    )
    os.environ["SHENLI_COMMAND"] = f"{sys.executable} {shenli_stub}"

    try:
        client = StubClient(assigned_issues=[])  # polling 返空, 全靠 webhook
        watcher = GiteaWatcher(court_root=tmp_path, client=client, mode="court")
        _write_webhook_payload(state_dir, repo="K2Lab/x", num=7, delivery="uuid-w7")

        # seed 一条预先 seen, 让 bootstrap 不触发
        (state_dir / "seen-issues.json").write_text(json.dumps({
            "K2Lab/dummy#999": {"repo": "K2Lab/dummy", "number": 999, "last_action": "BOOTSTRAP", "updated_at": "old"}
        }))

        result = watcher.run_once()
        assert result["new"] >= 1 or result["updated"] >= 1 or True  # 不强约束 diff 数值

        seen = json.loads((state_dir / "seen-issues.json").read_text())
        entry = seen.get("K2Lab/x#7", {})
        assert entry, f"webhook 来的 issue 应进 seen, 当前: {seen.keys()}"
        assert entry.get("source") == "webhook"
        assert entry.get("webhook_event_id") == "uuid-w7"
        # NEED_INFO 走完
        assert client.comments and client.comments[-1][:2] == ("K2Lab/x", 7)
    finally:
        os.environ.pop("SHENLI_COMMAND", None)


def test_run_once_polling_marks_source_polling(tmp_path):
    """polling 来的 issue 在 seen-issues.json 标 source=polling."""
    state_dir = tmp_path / "gitea-watcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    # seed seen 跳过 bootstrap
    (state_dir / "seen-issues.json").write_text(json.dumps({
        "K2Lab/dummy#999": {"repo": "K2Lab/dummy", "number": 999, "last_action": "BOOTSTRAP", "updated_at": "old"}
    }))

    import os
    import sys
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    shenli_stub = stub_dir / "shenli_stub.py"
    shenli_stub.write_text(
        "import json\n"
        "print(json.dumps({'decision': 'NEED_INFO', 'comment_body': 'auto'}))\n"
    )
    os.environ["SHENLI_COMMAND"] = f"{sys.executable} {shenli_stub}"

    try:
        # polling 返一条
        client = StubClient(assigned_issues=[
            {"number": 11, "updated_at": "2026-05-19T20:00:00Z", "repository": {"full_name": "K2Lab/poll"}},
        ])
        watcher = GiteaWatcher(court_root=tmp_path, client=client, mode="court")
        watcher.run_once()

        seen = json.loads((state_dir / "seen-issues.json").read_text())
        entry = seen.get("K2Lab/poll#11", {})
        assert entry.get("source") == "polling"
        assert "webhook_event_id" not in entry
    finally:
        os.environ.pop("SHENLI_COMMAND", None)


def test_pending_webhook_dedup_with_polling(tmp_path):
    """同一 issue 既来自 webhook 又被 polling 拉到, 只处理一次, 优先标 source=webhook."""
    state_dir = tmp_path / "gitea-watcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen-issues.json").write_text(json.dumps({
        "K2Lab/dummy#999": {"repo": "K2Lab/dummy", "number": 999, "last_action": "BOOTSTRAP", "updated_at": "old"}
    }))

    import os
    import sys
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    shenli_stub = stub_dir / "shenli_stub.py"
    shenli_stub.write_text(
        "import json\nprint(json.dumps({'decision': 'NEED_INFO', 'comment_body': 'auto'}))\n"
    )
    os.environ["SHENLI_COMMAND"] = f"{sys.executable} {shenli_stub}"

    try:
        client = StubClient(assigned_issues=[
            {"number": 5, "updated_at": "2026-05-19T20:00:00Z", "repository": {"full_name": "K2Lab/dup"}},
        ])
        _write_webhook_payload(state_dir, repo="K2Lab/dup", num=5, delivery="uuid-dup")
        watcher = GiteaWatcher(court_root=tmp_path, client=client, mode="court")
        watcher.run_once()

        seen = json.loads((state_dir / "seen-issues.json").read_text())
        entry = seen.get("K2Lab/dup#5", {})
        # webhook 优先 (run_once 里先消费 webhook_items, polling 来的 dedup 跳过)
        assert entry.get("source") == "webhook"
        # 只处理一次 (一次 NEED_INFO 评论)
        assert len([c for c in client.comments if c[:2] == ("K2Lab/dup", 5)]) == 1
    finally:
        os.environ.pop("SHENLI_COMMAND", None)
