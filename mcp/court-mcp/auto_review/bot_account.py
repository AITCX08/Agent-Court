"""Identify the bot Gitea account at process start.

Calls ``GiteaClient.whoami()`` once and asserts the returned ``login`` matches
``AutoReviewConfig.bot_username``. Returns a ``BotAccount`` the caller holds
and passes to downstream modules; this function itself does NOT memoize, so
call it exactly once at process start.
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
    raw_login = payload.get("login")
    if raw_login is not None and not isinstance(raw_login, str):
        raise BotAccountMismatch(
            f"Gitea whoami 'login' is not a string: {type(raw_login).__name__}"
        )
    login = (raw_login or "").strip()
    if not login:
        raise BotAccountMismatch(
            "Gitea whoami response missing 'login' field; cannot verify bot account"
        )
    if login != cfg.bot_username:
        raise BotAccountMismatch(
            f"configured bot {cfg.bot_username!r} does not match Gitea token whoami {login!r}"
        )
    raw_id = payload.get("id")
    if raw_id is None:
        raise BotAccountMismatch("Gitea whoami response missing 'id' field")
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise BotAccountMismatch(
            f"Gitea whoami 'id' is not an integer: {raw_id!r}"
        ) from exc
    return BotAccount(
        login=login,
        user_id=user_id,
        email=payload.get("email") or None,
    )
