"""Gitea webhook 内网接收器 (PR-14).

设计要点 (跟 PR-12 轮询的关系):
- 主进程独立, 只校验签名 + 落盘, 不调 GiteaClient/shenli
- 走 ``$COURT_ROOT/gitea-watcher/pending-webhook/<unix_ts>-<delivery>.json``
- watcher loop 优先消费这个目录, polling 5min 兜底
- Gitea 重试风暴防御: 内部错误一律 ack 200, 自己 stderr 记日志

部署要点:
- 内网部署, ``bind=0.0.0.0`` 仅供同内网 git.k2lab.ai POST, 不公网暴露
- secret 走 macOS Keychain (service=git.k2lab.ai-webhook), 不落 plist
- launchd KeepAlive=true 保活

Gitea 协议:
- header ``X-Gitea-Event: issues``, ``X-Gitea-Signature: <hex>`` (HMAC-SHA256, 纯 hex 不带前缀)
- ``X-Gitea-Delivery: <uuid>`` (事件 ID, 用作 dedup)
- payload ``{action, issue, repository, sender}``
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from webhook_secret import WebhookSecretMissing, get_webhook_secret


ALLOWED_ACTIONS = {"opened", "assigned", "edited", "reopened"}
ALLOWED_EVENTS = {"issues"}


def _state_dir() -> Path:
    return Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court"))) / "gitea-watcher"


def _pending_dir(state_dir: Path | None = None) -> Path:
    return (state_dir or _state_dir()) / "pending-webhook"


def _log(msg: str) -> None:
    print(f"[webhook-receiver] {msg}", file=sys.stderr, flush=True)


def _verify_signature(secret: str, raw_body: bytes, header_sig: str) -> bool:
    """Gitea 用 HMAC-SHA256(body, secret), 纯 hex 编码, 不带 'sha256=' 前缀 (跟 GitHub 不同)."""
    if not header_sig:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig.strip())


def _atomic_write_payload(dest: Path, payload: dict[str, Any]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=dest.parent,
        prefix=f".{dest.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, dest)


def _resolve_assignees(payload: dict[str, Any]) -> set[str]:
    """payload.issue.assignees[].login (可能空) + payload.issue.assignee.login (老格式)."""
    issue = payload.get("issue") or {}
    names: set[str] = set()
    for a in issue.get("assignees", []) or []:
        login = (a or {}).get("login")
        if login:
            names.add(login)
    assignee = issue.get("assignee") or {}
    if assignee.get("login"):
        names.add(assignee["login"])
    return names


def _resolve_allowed_users(env: dict[str, str] | None = None) -> set[str] | None:
    """决定哪些 assignee 算 '指派给我'.

    优先: env ``WEBHOOK_ASSIGNEE`` (逗号分隔多账号) → GiteaClient.whoami()
    都拿不到返 None (代表 '不做 assignee 过滤, 一律接', 给 receiver 启动期容错).

    PR-14 review W1: Fail-Open 风险——whoami 失败时不过滤会让 receiver 吃下
    所有指派给任何人的 issue. 实际影响: 不严重 (watcher 后面会按 assigned_to=me
    过滤), 但日志会乱. 缓解: env ``WEBHOOK_REQUIRE_ASSIGNEE_FILTER=1`` 时,
    whoami 失败将退出而不是 fail-open.
    """
    env = env if env is not None else os.environ
    raw = env.get("WEBHOOK_ASSIGNEE", "").strip()
    if raw:
        return {x.strip() for x in raw.split(",") if x.strip()}
    try:
        from gitea_client import GiteaClient

        whoami = GiteaClient().whoami()
        user = whoami.get("login") or whoami.get("username")
        if user:
            return {user}
    except Exception as exc:
        _log(f"WARNING whoami 失败, fall back to fail-open (接收所有 assignee): {exc!r}")
        if env.get("WEBHOOK_REQUIRE_ASSIGNEE_FILTER", "").strip() == "1":
            _log("WEBHOOK_REQUIRE_ASSIGNEE_FILTER=1, 拒绝 fail-open, raise.")
            raise
    return None


def create_app(*, state_dir: Path | None = None, secret: str | None = None, allowed_users: set[str] | None | object = ...) -> web.Application:
    """构造 aiohttp app. 单测里可以直接 passing 参数."""
    state = state_dir or _state_dir()
    pending = _pending_dir(state)
    pending.mkdir(parents=True, exist_ok=True)

    if secret is None:
        secret = get_webhook_secret()

    if allowed_users is ...:
        allowed_users = _resolve_allowed_users()

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def webhook(request: web.Request) -> web.Response:
        try:
            raw_body = await request.read()
        except Exception as exc:
            _log(f"read body failed: {exc!r}")
            return web.Response(status=200, text="bad-body-acked")

        sig = request.headers.get("X-Gitea-Signature", "")
        if not _verify_signature(secret, raw_body, sig):
            # PR-14 review C1 fix: 按 plan D8 一律 ack 200 防 Gitea 重试风暴.
            # 安全性靠 secret 校验 + 内网部署, 不靠 401 status code.
            _log(f"invalid signature dropped; delivery={request.headers.get('X-Gitea-Delivery', '?')}")
            return web.Response(status=200, text="invalid-signature dropped")

        event = request.headers.get("X-Gitea-Event", "")
        if event not in ALLOWED_EVENTS:
            return web.Response(status=200, text=f"event={event} dropped")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            _log(f"invalid json body: {exc!r}")
            return web.Response(status=200, text="bad-json-acked")

        action = (payload.get("action") or "").lower()
        if action not in ALLOWED_ACTIONS:
            return web.Response(status=200, text=f"action={action} dropped")

        if isinstance(allowed_users, set):
            assignees = _resolve_assignees(payload)
            if not (assignees & allowed_users):
                return web.Response(status=200, text="not-assigned-to-me dropped")

        delivery = (request.headers.get("X-Gitea-Delivery") or "").strip() or f"missing-{int(time.time())}"
        # 文件名安全: delivery 是 uuid 但保险起见过滤
        safe_delivery = "".join(c for c in delivery if c.isalnum() or c in "-_") or "anon"
        dest = pending / f"{int(time.time())}-{safe_delivery}.json"
        wrapper = {
            "received_at": time.time(),
            "delivery": delivery,
            "event": event,
            "action": action,
            "issue": payload.get("issue"),
            "repository": payload.get("repository"),
            "sender": payload.get("sender"),
        }
        try:
            _atomic_write_payload(dest, wrapper)
        except OSError as exc:
            _log(f"failed to write payload: {exc!r}")
            # 内部错误也 ack 200 防 Gitea 重试风暴
            return web.Response(status=200, text="internal-error-acked")

        _log(f"accepted action={action} delivery={delivery} dest={dest.name}")
        return web.json_response({"ok": True, "delivery": delivery})

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/gitea/webhook", webhook)
    return app


async def _run_app(host: str, port: int) -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _log(f"listening on {host}:{port}")
    while True:
        await asyncio.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gitea_webhook_receiver")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8765")))
    parser.add_argument("--bind", default=os.environ.get("WEBHOOK_BIND", "0.0.0.0"))
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run_app(args.bind, args.port))
    except WebhookSecretMissing as exc:
        _log(str(exc))
        return 1
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        # PR-14 review C2 fix: bind 失败 (端口被占) 等 OSError 必须非零退出,
        # 否则 launchd 以为正常退出不会重启. 用 4 跟 plan 文档约定一致.
        _log(f"startup failed (OSError): {exc}")
        return 4
    except Exception as exc:
        _log(f"unexpected startup error: {exc!r}")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
