"""agent-court — policy engine (PR-2).

Decides what to do with an inbound peer message *after* signature
verification and role-whitelist checks have already passed.

Decision actions
----------------
- ``auto_pass``      drop the message straight into ``bus/<peer>/inbox/``.
  Foreman picks it up via the existing court-watcher routing.
- ``judge``          (PR-2 stub) pass through to inbox + emit warning log.
  PR-3 will replace this branch with an llm_judge call that returns a
  confidence score and may downgrade to ``auto_pass`` or upgrade to
  ``human_required``.
- ``human_required`` park in ``bus/<peer>/pending-approval/`` and do
  *not* deliver until a human explicitly moves the file to ``inbox/``.
  PR-5 wires this branch to multi-channel approvals (terminal +
  FeiShu + WeChat).
- ``denied``         park in ``bus/<peer>/denied/`` for audit and stop.
  Never reaches the foreman, ever.

Rule layers
-----------
**Hard rules** — written in code, NOT overridable from ``policy.yaml``.
These exist so that a misconfigured ``policy.yaml`` cannot accidentally
expose system-level secrets.

1. ``HARDCODED_DENY_PATHS`` (e.g. ``**/.ssh/**``, ``**/.env``,
   ``**/id_rsa*``). Any attach matching one of these → ``denied``.
2. ``HARDCODED_KEYWORDS``  (e.g. ``password``, ``api_key``, ``sk-``).
   Any case-insensitive substring match in ``body`` → upgrade to
   ``human_required``.

**Project rules** — read from ``court.yaml`` (paths) and
``policy.yaml`` (tiers + extra keywords).

3. User ``deny_paths`` in ``court.yaml`` → ``denied``.
4. User ``allow_paths`` in ``court.yaml`` non-empty: every attach must
   match at least one allow glob, otherwise → ``human_required``.
5. Extra ``sensitive_keywords`` from ``policy.yaml`` are appended to
   the built-in list at evaluation time.

**Soft tier** — the final layer when nothing harder fires:

6. ``peers.yaml`` may pin ``policy_tier`` per peer. If absent, the
   ``policy.yaml`` ``default_tier`` applies. The tier maps to an
   action: ``tier_a → human_required``, ``tier_b → judge``,
   ``tier_c → auto_pass``.

Evaluation order
----------------
Hard layer first (1 → 2 → 3 → 4 → 5) so a single matching deny path
short-circuits everything; soft layer last (6). The first rule to fire
wins; reasons accumulate so the audit log can show *why* a decision
was made.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml


# ---------------------------------------------------------------------------
# Hardcoded layer
# ---------------------------------------------------------------------------

# Paths that no policy.yaml can re-allow. These are common locations for
# system secrets; if an inbound peer message attaches one of these we treat
# it as a deliberate attempt to exfiltrate.
HARDCODED_DENY_PATHS: tuple[str, ...] = (
    "/etc/**",
    "**/.ssh/**",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.env",
    "**/.env.*",
    "**/credentials.json",
    "**/secrets/**",
    "**/.aws/**",
    "**/.kube/config",
)

# Substrings (case-insensitive) whose appearance in ``body`` forces a
# human to look at the message. Not overridable from config; only
# extendable via ``policy.yaml`` ``sensitive_keywords:``.
HARDCODED_KEYWORDS: tuple[str, ...] = (
    "api_key", "apikey", "api-key",
    "password", "passwd",
    "secret", "token", "auth_token",
    "private_key", "privatekey",
    "AKIA",   # AWS access key prefix
    "sk-",    # OpenAI / Anthropic key prefix
)


# Tier → action mapping. Unknown tier defaults to the safest action.
_TIER_ACTION: dict[str, str] = {
    "tier_a": "human_required",
    "tier_b": "judge",
    "tier_c": "auto_pass",
}


# ---------------------------------------------------------------------------
# Config + decision dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PolicyConfig:
    default_tier: str = "tier_b"
    extra_keywords: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str                       # auto_pass | judge | human_required | denied
    tier: str                         # tier_a/b/c, or "hard_rule" when a hard layer fired
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_policy(project: str) -> PolicyConfig:
    """Read ``$COURT_ROOT/projects/<p>/policy.yaml``.

    Missing file, empty file, or malformed yaml all collapse to defaults
    (``tier_b`` + no extra keywords) so a half-set-up project keeps
    working — failing closed here would also kill the receiver, which is
    worse than mildly-permissive defaults that already get upgraded by
    the hardcoded layer.
    """
    # Local import to keep policy.py importable in environments that don't
    # have peer_lib's full dependency graph available (e.g. tests that
    # exercise pure logic).
    from peer_lib import project_dir

    cfg_path = project_dir(project) / "policy.yaml"
    if not cfg_path.is_file():
        return PolicyConfig()

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        return PolicyConfig()

    return PolicyConfig(
        default_tier=raw.get("default_tier") or "tier_b",
        extra_keywords=list(raw.get("sensitive_keywords") or []),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _match_any(path: str, patterns: Iterable[str]) -> Optional[str]:
    """Return the first pattern that matches ``path``, else None."""
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return pat
    return None


def evaluate(
    msg: dict,
    *,
    peer_tier: Optional[str],
    policy: PolicyConfig,
    allow_paths: list[str],
    deny_paths: list[str],
) -> Decision:
    """Return the policy ``Decision`` for an inbound message.

    Parameters
    ----------
    msg : dict
        The verified inbound message. Reads ``body`` and ``attaches``.
        ``attaches`` is optional; missing → empty list.
    peer_tier : str or None
        Per-peer tier override from ``peers.yaml``. None → use
        ``policy.default_tier``.
    policy : PolicyConfig
        Loaded from ``policy.yaml``.
    allow_paths, deny_paths : list[str]
        User-configured globs from ``court.yaml`` ``federation:`` block.
        HARDCODED_DENY_PATHS is checked in addition (not as a
        replacement).
    """
    reasons: list[str] = []
    attaches: list[str] = list(msg.get("attaches") or [])
    body = msg.get("body") or ""

    # --- Hard layer ---------------------------------------------------------

    # 1. Hardcoded deny paths — non-overridable system locations.
    for path in attaches:
        hit = _match_any(path, HARDCODED_DENY_PATHS)
        if hit:
            reasons.append(f"attach '{path}' hits hardcoded deny '{hit}'")
            return Decision(action="denied", tier="hard_rule", reasons=reasons)

    # 2. User deny paths from court.yaml.
    for path in attaches:
        hit = _match_any(path, deny_paths)
        if hit:
            reasons.append(f"attach '{path}' hits deny rule '{hit}'")
            return Decision(action="denied", tier="hard_rule", reasons=reasons)

    # 3. User allow paths: if specified, every attach must match one.
    if allow_paths and attaches:
        for path in attaches:
            if not _match_any(path, allow_paths):
                reasons.append(
                    f"attach '{path}' not covered by allow_paths {allow_paths} "
                    f"→ forcing human_required"
                )
                return Decision(
                    action="human_required", tier="hard_rule", reasons=reasons,
                )

    # 4. Sensitive keywords (hardcoded + policy extras).
    all_keywords = list(HARDCODED_KEYWORDS) + list(policy.extra_keywords)
    body_lower = body.lower()
    for kw in all_keywords:
        if kw and kw.lower() in body_lower:
            reasons.append(f"sensitive keyword '{kw}' in body → human_required")
            return Decision(
                action="human_required", tier="hard_rule", reasons=reasons,
            )

    # --- Soft layer (tier-based) -------------------------------------------

    tier = peer_tier or policy.default_tier
    action = _TIER_ACTION.get(tier, "human_required")
    if tier not in _TIER_ACTION:
        reasons.append(f"unknown tier '{tier}' → falling back to human_required")
    reasons.append(f"tier={tier} → action={action}")
    return Decision(action=action, tier=tier, reasons=reasons)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _policy_log_path(project: str) -> Path:
    from peer_lib import project_logs_dir
    return project_logs_dir(project) / "policy-log.jsonl"


def log_decision(project: str, msg: dict, decision: Decision) -> Path:
    """Append one JSON line to ``logs/policy-log.jsonl``. Returns path."""
    from peer_lib import iso_now, project_logs_dir

    project_logs_dir(project).mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": iso_now(),
        "from_court": msg.get("from_court"),
        "id": msg.get("id"),
        "from": msg.get("from"),
        "to": msg.get("to"),
        "attaches": msg.get("attaches") or [],
        "action": decision.action,
        "tier": decision.tier,
        "reasons": decision.reasons,
    }
    log_path = _policy_log_path(project)
    with log_path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# Subdir routing helper
# ---------------------------------------------------------------------------

# Where each action lands on disk, relative to bus/<from_court>/.
ACTION_SUBDIR: dict[str, str] = {
    "auto_pass": "inbox",
    "judge": "inbox",            # PR-2 stub passes through; PR-3 will refine
    "human_required": "pending-approval",
    "denied": "denied",
}


def subdir_for(action: str) -> str:
    return ACTION_SUBDIR.get(action, "denied")
