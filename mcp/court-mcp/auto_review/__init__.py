"""Auto-review subsystem (PR-18 series).

Polling + webhook driven Gitea review pipeline that mirrors
KAXY-3022/Agent-manager's A2A runner design. PR-18a establishes the config
foundation; later PRs add the polling worker (18b), webhook listener (18c),
light/deep router (18d), and frontend status badges (18e).
"""

from auto_review.config import AutoReviewConfig, AutoReviewConfigError, load_config
from auto_review.bot_account import BotAccount, BotAccountMismatch, identify_bot

__all__ = [
    "AutoReviewConfig",
    "AutoReviewConfigError",
    "load_config",
    "BotAccount",
    "BotAccountMismatch",
    "identify_bot",
]
