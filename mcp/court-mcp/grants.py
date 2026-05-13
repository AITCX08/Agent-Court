"""agent-court — temporary path-access grants (PR-4).

A *grant* is a project-scoped record saying "for the next N minutes, this
peer court is allowed to reference these extra paths in inbound
messages". It extends ``court.yaml``'s static ``allow_paths`` whitelist
*at runtime*, without anyone editing yaml.

The intended workflow mirrors sudo:

.. code-block:: bash

    court-grant example bob-laptop-example notes/2026-Q2.md --ttl 30m
    # ^ Bob may now attach 'notes/2026-Q2.md' for the next 30 min.

    court-grant example list
    court-grant example revoke <grant-id>

Storage
-------

One JSON file per grant, under
``$COURT_ROOT/projects/<p>/grants/<grant-id>.json``. A separate file
per grant means ``court-grant list`` is just ``ls`` and revoke is just
``rm`` — no central index, nothing to corrupt. ``ReplayCache`` -style
in-memory state is *not* used: grants are durable across daemon
restarts because they live on disk.

Security model
--------------

A grant ONLY widens ``allow_paths``. It cannot:

- override ``HARDCODED_DENY_PATHS`` (system secrets stay blocked);
- override ``deny_paths`` from ``court.yaml`` (user blacklist wins);
- change the policy tier (a tier_a peer is still tier_a; grants only
  affect the path check inside ``policy.evaluate``).

Granularity is ``(peer_court_id, [paths])`` — the same peer entry that
matches ``from_court`` on inbound. Per-role grants (e.g. "only Bob's
foreman, not his backend") are deliberately out of scope; the
``expose_roles`` whitelist already covers that.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def grants_dir(project: str) -> Path:
    from peer_lib import project_dir
    return project_dir(project) / "grants"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Grant:
    """A single time-bounded path grant for one peer court.

    Attributes
    ----------
    id : str
        Random 8-char hex identifier. Used as filename and revoke handle.
    granted_to : str
        ``court_id`` of the peer this grant applies to. Must match
        ``from_court`` on an inbound message for the grant to widen
        that message's allow_paths.
    paths : list[str]
        Path globs the peer may attach. Same glob dialect as
        ``allow_paths`` in ``court.yaml`` (``**/X`` understood, but
        absolute paths and ``..`` segments are rejected by
        ``policy.normalize_attach`` regardless of what's in here).
    issued_ts : str
        ISO 8601 timestamp the grant was minted.
    expires_ts : str
        ISO 8601 timestamp the grant becomes invalid.
    issued_by : str
        Free-form human-readable hint (e.g. ``alice@laptop``). Goes into
        the audit trail; not used for any access check.
    """
    id: str
    granted_to: str
    paths: list[str]
    issued_ts: str
    expires_ts: str
    issued_by: str = ""

    def is_active(self, *, now: Optional[datetime] = None) -> bool:
        try:
            exp = datetime.fromisoformat(self.expires_ts)
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now is None:
            now = datetime.now(timezone.utc)
        return now < exp


# ---------------------------------------------------------------------------
# TTL parser
# ---------------------------------------------------------------------------

_TTL_PART_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>[smhd])", re.IGNORECASE)
_TTL_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(spec) -> int:
    """Parse ``"30m"``, ``"1h"``, ``"2h30m"``, ``"1d"`` → seconds.

    Accepts an int (seconds) directly. Raises ``ValueError`` for garbage.
    Minimum granted TTL: 1 second. No upper bound — issuing a 1-year
    grant is silly but technically allowed; the audit log makes it
    visible.
    """
    if isinstance(spec, int) and not isinstance(spec, bool):
        if spec < 1:
            raise ValueError(f"ttl must be ≥ 1 second, got {spec}")
        return spec
    if not isinstance(spec, str):
        raise ValueError(f"ttl must be a string or int, got {type(spec).__name__}")
    spec = spec.strip().lower()
    if not spec:
        raise ValueError("ttl is empty")
    # Plain integer means seconds.
    if spec.isdigit():
        return parse_ttl(int(spec))
    total = 0
    consumed = 0
    for m in _TTL_PART_RE.finditer(spec):
        total += int(m.group("num")) * _TTL_UNIT_SECS[m.group("unit").lower()]
        consumed += len(m.group(0))
    # Allow whitespace between parts but nothing else.
    if total == 0 or consumed != len("".join(spec.split())):
        raise ValueError(
            f"ttl {spec!r} not recognized — use forms like '30m', '1h', '2h30m', '1d'"
        )
    return total


# ---------------------------------------------------------------------------
# Minting + persistence
# ---------------------------------------------------------------------------

def _new_grant_id() -> str:
    """8 hex chars — collision space is 32 bits, fine for ~10⁴ live grants
    per project; the daemon never auto-mints, only humans/MCP tools do."""
    return secrets.token_hex(4)


def mint_grant(
    project: str,
    granted_to: str,
    paths: list[str],
    *,
    ttl,
    issued_by: str = "",
) -> Grant:
    """Create a grant and persist it to disk. Returns the Grant object.

    ``ttl`` may be a string (``"30m"``) or an int (seconds). Path list
    must be non-empty; empty paths or non-string entries are rejected
    up front so a malformed grant never lands on disk.
    """
    from peer_lib import assert_safe_path_component

    # Validate granted_to as a peer court_id — it's also a filesystem
    # component once the daemon goes to load grants for it.
    assert_safe_path_component(granted_to, field_name="granted_to")

    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list")
    cleaned_paths: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"path entry not a non-empty string: {p!r}")
        cleaned_paths.append(p.strip())

    ttl_seconds = parse_ttl(ttl)
    now = datetime.now(timezone.utc).astimezone()
    expires = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=now.tzinfo)

    grant = Grant(
        id=_new_grant_id(),
        granted_to=granted_to,
        paths=cleaned_paths,
        issued_ts=now.isoformat(timespec="seconds"),
        expires_ts=expires.isoformat(timespec="seconds"),
        issued_by=str(issued_by or "")[:128],
    )

    gdir = grants_dir(project)
    gdir.mkdir(parents=True, exist_ok=True)
    fpath = gdir / f"{grant.id}.json"
    fpath.write_text(json.dumps(asdict(grant), ensure_ascii=False, indent=2) + "\n")
    return grant


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_grant_file(p: Path) -> Optional[Grant]:
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Grant(
            id=str(raw["id"]),
            granted_to=str(raw["granted_to"]),
            paths=list(raw["paths"]),
            issued_ts=str(raw["issued_ts"]),
            expires_ts=str(raw["expires_ts"]),
            issued_by=str(raw.get("issued_by", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def list_grants(project: str) -> list[Grant]:
    """Return every grant on disk for the project (active + expired).

    Sorted by ``issued_ts`` so ``court-grant list`` shows newest last.
    Malformed JSON files are silently skipped — we never refuse to list
    grants because of one corrupted entry.
    """
    gdir = grants_dir(project)
    if not gdir.is_dir():
        return []
    grants: list[Grant] = []
    for f in gdir.glob("*.json"):
        g = _read_grant_file(f)
        if g is not None:
            grants.append(g)
    grants.sort(key=lambda g: g.issued_ts)
    return grants


def load_active_grants(project: str) -> list[Grant]:
    """Like :func:`list_grants` but filters out anything past its TTL."""
    now = datetime.now(timezone.utc)
    return [g for g in list_grants(project) if g.is_active(now=now)]


def load_grants_for_peer(project: str, peer_court_id: str) -> list[str]:
    """Return the union of allowed path globs from every active grant
    addressed to ``peer_court_id``. Empty list if none.

    This is the shape :func:`policy.evaluate` expects — a flat list of
    globs to OR into ``allow_paths``.
    """
    out: list[str] = []
    for g in load_active_grants(project):
        if g.granted_to == peer_court_id:
            out.extend(g.paths)
    return out


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def revoke_grant(project: str, grant_id: str) -> bool:
    """Delete the grant file. Returns True if removed, False if absent.

    No graveyard — once revoked there's nothing to audit beyond the
    policy log entries that were issued while the grant was live.
    """
    from peer_lib import assert_safe_path_component
    try:
        assert_safe_path_component(grant_id, field_name="grant_id")
    except Exception:
        return False
    fpath = grants_dir(project) / f"{grant_id}.json"
    if not fpath.is_file():
        return False
    try:
        fpath.unlink()
    except OSError:
        return False
    return True
