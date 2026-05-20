"""Webhook secret 取法 helper.

跟 PR-12 ``gitea_credentials`` (oauth2 push token, service=``git.k2lab.ai``)
**严格隔离**: 本模块用 service=``git.k2lab.ai-webhook``, account=``webhook-secret``,
避免 ``git credential-osxkeychain get`` 选错条目 (user 已踩过坑).

fallback 链:
1. macOS Keychain: ``security find-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w``
2. env: ``K2LAB_WEBHOOK_SECRET``

录 secret (install 前必做):

    security add-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w <SECRET>
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


WEBHOOK_KEYCHAIN_SERVICE = "git.k2lab.ai-webhook"
WEBHOOK_KEYCHAIN_ACCOUNT = "webhook-secret"
WEBHOOK_ENV_KEY = "K2LAB_WEBHOOK_SECRET"


class WebhookSecretMissing(RuntimeError):
    """Keychain entry + env 都没找到 secret 时抛."""


def _read_from_keychain() -> str | None:
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                WEBHOOK_KEYCHAIN_SERVICE,
                "-a",
                WEBHOOK_KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    secret = (proc.stdout or "").strip()
    return secret or None


def _read_from_env() -> str | None:
    val = os.environ.get(WEBHOOK_ENV_KEY, "").strip()
    return val or None


def get_webhook_secret() -> str:
    """按 fallback 链取 secret. 都拿不到 raise WebhookSecretMissing."""
    secret = _read_from_keychain()
    if secret:
        return secret
    secret = _read_from_env()
    if secret:
        return secret
    raise WebhookSecretMissing(
        "未找到 webhook secret. 请用 `security add-generic-password -s "
        f"{WEBHOOK_KEYCHAIN_SERVICE} -a {WEBHOOK_KEYCHAIN_ACCOUNT} -w <secret>` 录入, "
        f"或者设环境变量 {WEBHOOK_ENV_KEY}."
    )


def has_webhook_secret() -> bool:
    """非异常的检查接口, 给 bin/gitea-webhook-receiver install 前置校验用."""
    try:
        get_webhook_secret()
        return True
    except WebhookSecretMissing:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webhook_secret")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="检查 secret 是否可读 (不打印 secret 内容)")
    sub.add_parser("source", help="打印 secret 来源 (keychain/env/missing)")
    args = parser.parse_args(argv)
    if args.command == "check":
        if has_webhook_secret():
            print("OK")
            return 0
        print("MISSING", file=sys.stderr)
        return 1
    if args.command == "source":
        if _read_from_keychain():
            print("keychain")
            return 0
        if _read_from_env():
            print("env")
            return 0
        print("missing")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
