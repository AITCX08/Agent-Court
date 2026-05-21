"""BOOT-1 (#20) tracker client factory.

根据 WORKFLOW.md ``tracker.provider`` 字段选 ``GiteaClient`` 或 ``GithubClient``.
caller (watcher / im_router 等) 通过 ``build_tracker_client(config.tracker)``
拿到 client, 不直接 ``import GiteaClient``.

设计哲学:
- factory 不抢 caller 的 DI 灵活性: 仍支持 ``GiteaWatcher(..., client=mock)`` 测试
- 不引 ABC / interface; 两个 client 的公开方法签名一致 (duck typing)
- WORKFLOW.md 没配 tracker / 配错 → 默认 Gitea (保持向后兼容)
"""
from __future__ import annotations

from typing import Any


SUPPORTED_PROVIDERS = ("gitea", "github")


def build_tracker_client(tracker_config: Any = None) -> Any:
    """根据 ``tracker_config`` (来自 WorkflowConfig.tracker) 实例化对应 client.

    ``tracker_config`` 是 ``workflow_loader.TrackerConfig`` 实例 (或 ``None``).
    None / 字段缺失 → 默认 Gitea (向后兼容).
    """
    provider = "gitea"
    base_url: str | None = None
    if tracker_config is not None:
        provider = getattr(tracker_config, "provider", "gitea") or "gitea"
        base_url = getattr(tracker_config, "base_url", None) or None

    if provider == "github":
        from github_client import GithubClient

        if base_url:
            return GithubClient(base_url=base_url)
        return GithubClient()

    if provider == "gitea":
        from gitea_client import GiteaClient

        if base_url:
            return GiteaClient(base_url=base_url)
        return GiteaClient()

    raise ValueError(
        f"unsupported tracker provider: {provider!r}; supported: {SUPPORTED_PROVIDERS}"
    )
