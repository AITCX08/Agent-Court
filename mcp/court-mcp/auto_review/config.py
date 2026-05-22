"""Env-driven configuration for the auto-review subsystem.

All ``A2A_GITEA_*`` environment variables are read here; downstream modules
(polling worker, webhook listener, light/deep router) consume a single
``AutoReviewConfig`` instance instead of touching ``os.environ`` directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class AutoReviewConfigError(RuntimeError):
    """Raised when required env vars are missing or malformed."""


_TRUE_TOKENS = {"1", "true", "yes", "on", "y"}
_FALSE_TOKENS = {"0", "false", "no", "off", "n", ""}


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    raise AutoReviewConfigError(f"{name} must be a bool token, got {raw!r}")


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AutoReviewConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_positive_int(name: str, raw: str | None, default: int, min_value: int = 1) -> int:
    """Parse int env var, enforcing a lower bound (default min=1)."""
    value = _parse_int(name, raw, default)
    if value < min_value:
        raise AutoReviewConfigError(
            f"{name} must be >= {min_value}, got {value}"
        )
    return value


def _parse_watch_repos(raw: str | None) -> list[str]:
    if raw is None:
        raise AutoReviewConfigError(
            "A2A_GITEA_WATCH_REPOS is required (e.g. 'K2Lab/agent-court,K2Lab/moras-brain')"
        )
    items = [s.strip() for s in raw.split(",") if s.strip()]
    if not items:
        raise AutoReviewConfigError("A2A_GITEA_WATCH_REPOS is empty after parsing")
    for item in items:
        parts = item.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise AutoReviewConfigError(
                f"A2A_GITEA_WATCH_REPOS entry {item!r} is not a valid owner/repo"
            )
    return items


@dataclass(frozen=True, slots=True)
class AutoReviewConfig:
    """All knobs for the auto-review pipeline. Loaded once at process start."""

    bot_username: str
    watch_repos: list[str] = field(default_factory=list)
    pr_auto_post: bool = True
    issue_auto_post: bool = True
    webhook_triggers_enabled: bool = False
    worker_count: int = 2
    light_deep_threshold: int = 10
    gitea_base_url: str = "https://git.k2lab.ai"
    poll_discovery_interval_sec: int = 60
    poll_active_interval_sec: int = 30


def load_config(env: dict[str, str] | None = None) -> AutoReviewConfig:
    """Load AutoReviewConfig from environment (defaults to os.environ).

    Raises AutoReviewConfigError if required fields are missing or malformed.
    """
    e = env if env is not None else dict(os.environ)

    bot_username = (e.get("A2A_GITEA_USERNAME") or "").strip()
    if not bot_username:
        raise AutoReviewConfigError("A2A_GITEA_USERNAME is required")

    watch_repos = _parse_watch_repos(e.get("A2A_GITEA_WATCH_REPOS"))

    return AutoReviewConfig(
        bot_username=bot_username,
        watch_repos=watch_repos,
        pr_auto_post=_parse_bool(
            "A2A_GITEA_PR_AUTO_POST", e.get("A2A_GITEA_PR_AUTO_POST"), True
        ),
        issue_auto_post=_parse_bool(
            "A2A_GITEA_ISSUE_AUTO_POST", e.get("A2A_GITEA_ISSUE_AUTO_POST"), True
        ),
        webhook_triggers_enabled=_parse_bool(
            "A2A_GITEA_WEBHOOK_TRIGGERS", e.get("A2A_GITEA_WEBHOOK_TRIGGERS"), False
        ),
        worker_count=_parse_positive_int(
            "A2A_GITEA_WORKER_COUNT", e.get("A2A_GITEA_WORKER_COUNT"), 2
        ),
        light_deep_threshold=_parse_positive_int(
            "A2A_GITEA_LIGHT_DEEP_THRESHOLD",
            e.get("A2A_GITEA_LIGHT_DEEP_THRESHOLD"),
            10,
            min_value=0,  # 0 = always deep is allowed
        ),
        gitea_base_url=(e.get("A2A_GITEA_BASE_URL") or "https://git.k2lab.ai").strip(),
        poll_discovery_interval_sec=_parse_positive_int(
            "A2A_GITEA_POLL_DISCOVERY_SEC",
            e.get("A2A_GITEA_POLL_DISCOVERY_SEC"),
            60,
        ),
        poll_active_interval_sec=_parse_positive_int(
            "A2A_GITEA_POLL_ACTIVE_SEC", e.get("A2A_GITEA_POLL_ACTIVE_SEC"), 30
        ),
    )
