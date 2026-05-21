"""Gitea webhook receiver 单元测试 (PR-14).

用 aiohttp.test_utils 起 TestServer + TestClient.
"""
from __future__ import annotations

import hmac
import json
from hashlib import sha256
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gitea_webhook_receiver import create_app


SECRET = "testsecret-pr14"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, sha256).hexdigest()


def _issue_payload(*, action: str = "assigned", assignee: str = "wjx", repo: str = "K2Lab/demo", number: int = 7) -> dict:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "fixture issue",
            "html_url": f"http://git.k2lab.ai/{repo}/issues/{number}",
            "body": "test body",
            "assignees": [{"login": assignee}],
            "labels": [],
        },
        "repository": {"full_name": repo},
        "sender": {"login": "tester"},
    }


@pytest.mark.asyncio
async def test_healthz_returns_200(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_signature_valid_writes_file(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users={"wjx"})
    body = json.dumps(_issue_payload()).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-test-1",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
    # 验证 pending-webhook 落盘
    pending = tmp_path / "pending-webhook"
    files = list(pending.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["delivery"] == "uuid-test-1"
    assert payload["issue"]["number"] == 7


@pytest.mark.asyncio
async def test_signature_invalid_returns_200_drops(tmp_path):
    """PR-14 review C1 fix: 签名错按 plan D8 返 200 ack (防 Gitea 重试风暴), 但仍不落盘."""
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps(_issue_payload()).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": "deadbeef" * 8,  # 错的
        "X-Gitea-Delivery": "uuid-bad",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
        text = await resp.text()
        assert "invalid-signature" in text
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


@pytest.mark.asyncio
async def test_non_issues_event_returns_200_no_write(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps({"action": "opened", "issue": {"number": 1}}).encode()
    headers = {
        "X-Gitea-Event": "pull_request",  # 不接
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-pr",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


@pytest.mark.asyncio
async def test_action_not_in_whitelist_drops(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps(_issue_payload(action="label_updated")).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-label",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


@pytest.mark.asyncio
async def test_issue_not_assigned_to_me_drops(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users={"wjx"})
    body = json.dumps(_issue_payload(assignee="some-other-user")).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-not-mine",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


@pytest.mark.asyncio
async def test_assignee_filter_disabled_accepts_all(tmp_path):
    """allowed_users=None 表示不做 assignee 过滤 (启动期 whoami 失败的兜底)."""
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps(_issue_payload(assignee="random-user")).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-no-filter",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
    assert len(list((tmp_path / "pending-webhook").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_atomic_write_pending_webhook(tmp_path):
    """落盘必须先写 tempfile 再 rename, 不能存在半写文件."""
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps(_issue_payload()).encode()
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-atomic",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
    pending = tmp_path / "pending-webhook"
    # 不应有半写状态 .tmp 文件残留
    tmp_files = list(pending.glob("*.tmp"))
    assert tmp_files == []
    # 最终的 json 是合法的
    files = list(pending.glob("*.json"))
    assert len(files) == 1
    json.loads(files[0].read_text())  # parse 不抛即成功


def test_main_returns_nonzero_on_port_bind_failure(monkeypatch, tmp_path):
    """PR-14 review C2 fix: 端口被占 OSError, main 必须返非零 (launchd 才会 KeepAlive 重启)."""
    import gitea_webhook_receiver as mod
    monkeypatch.setenv("K2LAB_WEBHOOK_SECRET", "test")

    def _boom(*args, **kwargs):
        raise OSError(48, "address already in use")

    monkeypatch.setattr(mod.asyncio, "run", _boom)
    exit_code = mod.main(["--port", "1", "--bind", "127.0.0.1"])
    assert exit_code != 0, f"main 必须返非零让 launchd 重启, 实际: {exit_code}"


def test_resolve_allowed_users_fail_open_default(monkeypatch):
    """默认 (没 WEBHOOK_REQUIRE_ASSIGNEE_FILTER): whoami 失败 → fall back to None (fail-open)."""
    import gitea_webhook_receiver as mod
    monkeypatch.delenv("WEBHOOK_ASSIGNEE", raising=False)
    monkeypatch.delenv("WEBHOOK_REQUIRE_ASSIGNEE_FILTER", raising=False)

    class BoomClient:
        def whoami(self):
            raise RuntimeError("simulated token failure")

    monkeypatch.setattr("gitea_client.GiteaClient", lambda *a, **kw: BoomClient())
    result = mod._resolve_allowed_users()
    assert result is None  # fail-open


def test_resolve_allowed_users_strict_mode_raises(monkeypatch):
    """WEBHOOK_REQUIRE_ASSIGNEE_FILTER=1 时, whoami 失败应该 raise 而不是 fail-open."""
    import gitea_webhook_receiver as mod
    monkeypatch.delenv("WEBHOOK_ASSIGNEE", raising=False)
    monkeypatch.setenv("WEBHOOK_REQUIRE_ASSIGNEE_FILTER", "1")

    class BoomClient:
        def whoami(self):
            raise RuntimeError("simulated token failure")

    monkeypatch.setattr("gitea_client.GiteaClient", lambda *a, **kw: BoomClient())
    with pytest.raises(RuntimeError):
        mod._resolve_allowed_users()


@pytest.mark.asyncio
async def test_malformed_json_body_acked_200(tmp_path):
    """坏 JSON body 内部错误也 ack 200, 避免 Gitea 重试风暴."""
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = b"not-json-body"
    headers = {
        "X-Gitea-Event": "issues",
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Delivery": "uuid-bad-json",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        # 内部错误 200 ack, 不让 Gitea 重试
        assert resp.status == 200
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


# ---------------------------------------------------------------------------
# BOOT-1 #20: GitHub webhook 格式兼容
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_webhook_with_sha256_prefix_is_accepted(tmp_path):
    """GitHub 用 X-Hub-Signature-256: sha256=<hex>, 应跟 Gitea 一样通过校验."""
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps({
        "action": "opened",
        "issue": {"number": 7, "assignees": [{"login": "alice"}]},
        "repository": {"full_name": "AITCX08/agent-court"},
        "sender": {"login": "alice"},
    }).encode("utf-8")
    headers = {
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": f"sha256={_sign(body)}",
        "X-GitHub-Delivery": "github-uuid-abc",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert data.get("ok") is True
        assert data.get("delivery") == "github-uuid-abc"
    files = list((tmp_path / "pending-webhook").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["delivery"] == "github-uuid-abc"
    assert payload["event"] == "issues"
    assert payload["action"] == "opened"


@pytest.mark.asyncio
async def test_github_webhook_with_wrong_signature_dropped(tmp_path):
    app = create_app(state_dir=tmp_path, secret=SECRET, allowed_users=None)
    body = json.dumps({"action": "opened", "issue": {"number": 1}}).encode("utf-8")
    headers = {
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": "sha256=" + ("deadbeef" * 8),  # 假签名 64 hex
        "X-GitHub-Delivery": "github-uuid-bad",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/gitea/webhook", data=body, headers=headers)
        # ack 200 防止 GitHub 重试风暴
        assert resp.status == 200
        text = await resp.text()
        assert "invalid-signature" in text
    # 不应该落盘
    assert not list((tmp_path / "pending-webhook").glob("*.json"))


def test_verify_signature_accepts_both_formats():
    """_verify_signature 单测: Gitea 裸 hex + GitHub sha256= 前缀都过, 大小写都接."""
    from gitea_webhook_receiver import _verify_signature
    body = b'{"action":"opened"}'
    expected = hmac.new(SECRET.encode(), body, sha256).hexdigest()
    # Gitea 裸 hex
    assert _verify_signature(SECRET, body, expected) is True
    # GitHub sha256= 前缀
    assert _verify_signature(SECRET, body, f"sha256={expected}") is True
    # 大写 SHA256= 也接 (HTTP header case-insensitive 习惯)
    assert _verify_signature(SECRET, body, f"SHA256={expected}") is True
    # 错签拒绝
    assert _verify_signature(SECRET, body, "deadbeef" * 8) is False
    assert _verify_signature(SECRET, body, f"sha256={'0'*64}") is False
    # 空 header 拒绝
    assert _verify_signature(SECRET, body, "") is False
