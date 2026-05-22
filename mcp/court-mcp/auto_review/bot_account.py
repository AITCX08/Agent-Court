"""Identify the bot Gitea account at process start.

Calls ``GiteaClient.whoami()`` once and asserts the returned ``login`` matches
``AutoReviewConfig.bot_username``. Caches the result in a ``BotAccount``
dataclass so downstream modules don't re-hit the API.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from auto_review.config import AutoReviewConfig


class BotAccountMismatch(RuntimeError):
    """Raised when the configured bot username does not match the token's whoami."""


class _WhoamiClient(Protocol):
    def whoami(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BotAccount:
    """Cached identity of the bot user — what Gitea reports for the API token."""

    login: str
    user_id: int
    email: str | None = None


def identify_bot(cfg: AutoReviewConfig, *, client: _WhoamiClient) -> BotAccount:
    """Verify the token's whoami matches the configured bot username.

    Returns a cached BotAccount on success. Raises BotAccountMismatch when:
    - whoami payload is missing or blank in the 'login' field
    - whoami login does not equal cfg.bot_username (case-sensitive)
    """
    payload = client.whoami()
    login = (payload.get("login") or "").strip()
    if not login:
        raise BotAccountMismatch(
            "Gitea whoami response missing 'login' field; cannot verify bot account"
        )
    if login != cfg.bot_username:
        raise BotAccountMismatch(
            f"configured bot {cfg.bot_username!r} does not match Gitea token whoami {login!r}"
        )
    return BotAccount(
        login=login,
        user_id=int(payload.get("id", 0)),
        email=payload.get("email"),
    )
