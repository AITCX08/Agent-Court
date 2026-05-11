"""agent-court — shared peer-network primitives.

Used by ``court-keygen``, ``court-peer`` (HTTP receiver) and the MCP
server's peer tools. Lives next to the MCP server because they share a
venv (cryptography, aiohttp, pyyaml).

Identity model is **per-project** (see ARCHITECTURE.md): each project
has its own keypair + peers.yaml under
``$COURT_ROOT/projects/<project>/`` so peer ``A`` of project ``work`` has
no way to know that project ``personal`` exists on the same machine.

Functions are pure: no global mutable state. ``COURT_ROOT`` is resolved
on each call via :func:`court_root` so the module is safe to import from
tests with monkeypatched env.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# ---------------------------------------------------------------------------
# Paths (all project-scoped)
# ---------------------------------------------------------------------------

def court_root() -> Path:
    return Path(os.environ.get("COURT_ROOT", str(Path.home() / ".agent-court")))


def project_dir(project: str) -> Path:
    return court_root() / "projects" / project


def project_bus_dir(project: str) -> Path:
    return project_dir(project) / "bus"


def project_identity_dir(project: str) -> Path:
    return project_dir(project) / "identity"


def project_priv_key_path(project: str) -> Path:
    return project_identity_dir(project) / "priv.key"


def project_pub_key_path(project: str) -> Path:
    return project_identity_dir(project) / "pub.key"


def project_peers_yaml_path(project: str) -> Path:
    return project_dir(project) / "peers.yaml"


def project_court_yaml_path(project: str) -> Path:
    return project_dir(project) / "court.yaml"


def project_logs_dir(project: str) -> Path:
    return project_dir(project) / "logs"


def project_peer_errors_log(project: str) -> Path:
    return project_logs_dir(project) / "peer-errors.log"


def all_projects() -> list[str]:
    base = court_root() / "projects"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "court.yaml").is_file())


# ---------------------------------------------------------------------------
# Keypair
# ---------------------------------------------------------------------------

@dataclass
class Identity:
    project: str
    priv: Ed25519PrivateKey
    pub: Ed25519PublicKey
    pub_b64: str
    fingerprint: str


def fingerprint_from_pub_b64(pub_b64: str) -> str:
    """SHA-256 of raw public key bytes, first 16 bytes hex."""
    raw = base64.b64decode(pub_b64)
    digest = hashlib.sha256(raw).digest()
    return digest[:16].hex()


def generate_keypair(project: str, *, force: bool = False) -> Identity:
    """Generate a new ed25519 keypair for ``project``.

    Returns the new identity. Raises ``FileExistsError`` if a key already
    exists and ``force`` is False.
    """
    project_identity_dir(project).mkdir(parents=True, exist_ok=True)
    priv_path = project_priv_key_path(project)
    pub_path = project_pub_key_path(project)
    if priv_path.exists() and not force:
        raise FileExistsError(
            f"keypair already exists at {priv_path}; pass force=True to overwrite"
        )

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    from cryptography.hazmat.primitives import serialization

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    priv_b64 = base64.b64encode(priv_bytes).decode()
    pub_b64 = base64.b64encode(pub_bytes).decode()

    priv_path.write_text(priv_b64 + "\n")
    os.chmod(priv_path, 0o600)
    pub_path.write_text(pub_b64 + "\n")
    os.chmod(pub_path, 0o644)

    return Identity(
        project=project,
        priv=priv,
        pub=pub,
        pub_b64=pub_b64,
        fingerprint=fingerprint_from_pub_b64(pub_b64),
    )


def load_identity(project: str) -> Identity:
    """Load the project's identity. Raises FileNotFoundError if absent."""
    priv_path = project_priv_key_path(project)
    pub_path = project_pub_key_path(project)
    if not priv_path.exists():
        raise FileNotFoundError(
            f"no keypair at {priv_path} — run `court-keygen {project}` first"
        )
    priv_b64 = priv_path.read_text().strip()
    pub_b64 = pub_path.read_text().strip()

    priv_bytes = base64.b64decode(priv_b64)
    pub_bytes = base64.b64decode(pub_b64)
    priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
    return Identity(
        project=project,
        priv=priv,
        pub=pub,
        pub_b64=pub_b64,
        fingerprint=fingerprint_from_pub_b64(pub_b64),
    )


# ---------------------------------------------------------------------------
# Canonical JSON + signing
# ---------------------------------------------------------------------------

SIGNED_FIELDS: tuple[str, ...] = (
    "attaches",        # PR-2: explicit file/path references, must be signed so a peer
                       # can't strip or forge them after the sender signed the message
    "body",
    "from",
    "from_court",
    "id",
    "in_reply_to",
    "to",
    "ts",
)


def canonical_payload(msg: dict) -> bytes:
    """Pick the fields covered by the signature and JSON-dump them deterministically."""
    payload = {k: msg[k] for k in SIGNED_FIELDS if k in msg and msg[k] is not None}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_message(msg: dict, priv: Ed25519PrivateKey) -> str:
    sig = priv.sign(canonical_payload(msg))
    return base64.b64encode(sig).decode()


def verify_signature(msg: dict, signature_b64: str, sender_pub_b64: str) -> bool:
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(sender_pub_b64))
    try:
        pub.verify(base64.b64decode(signature_b64), canonical_payload(msg))
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------------------
# court.yaml federation block
# ---------------------------------------------------------------------------

@dataclass
class FederationConfig:
    enabled: bool = False
    court_id: str = ""
    expose_roles: list[str] = field(default_factory=list)        # roles outside peers may dispatch to
    expose_read: list[str] = field(default_factory=list)         # roles outside peers may read outboxes of
    allow_paths: list[str] = field(default_factory=list)         # glob whitelist (PR-2 enforces)
    deny_paths: list[str] = field(default_factory=list)          # glob blacklist (PR-2 enforces)


def _default_court_id(project: str) -> str:
    host = os.environ.get("COURT_HOSTNAME") or socket.gethostname() or "host"
    # strip the trailing ".local" macOS adds, looks ugly in network configs
    host = host.removesuffix(".local")
    return f"{host}-{project}"


def load_federation(project: str) -> FederationConfig:
    """Read court.yaml's ``federation:`` block.

    Returns a disabled config if the file is missing, the block is absent,
    or ``enabled: false``. court_id falls back to ``<hostname>-<project>``.
    """
    cfg_path = project_court_yaml_path(project)
    raw = {}
    if cfg_path.is_file():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}

    block = raw.get("federation") or {}
    enabled = bool(block.get("enabled", False))
    return FederationConfig(
        enabled=enabled,
        court_id=block.get("court_id") or _default_court_id(project),
        expose_roles=list(block.get("expose_roles") or []),
        expose_read=list(block.get("expose_read") or []),
        allow_paths=list(block.get("allow_paths") or []),
        deny_paths=list(block.get("deny_paths") or []),
    )


# ---------------------------------------------------------------------------
# peers.yaml
# ---------------------------------------------------------------------------

@dataclass
class Peer:
    name: str
    court_id: str
    url: str
    pub_key_fingerprint: str
    pub_key_b64: Optional[str]
    relation: str   # parent | child | sibling (was "role" pre-PR-1; renamed to disambiguate from agent roles)
    policy_tier: Optional[str] = None   # PR-2: tier_a | tier_b | tier_c. None → fall through to policy.default_tier


@dataclass
class PeersConfig:
    project: str
    self_court_id: str
    self_fingerprint: str
    peers: list[Peer]

    def by_court_id(self, court_id: str) -> Optional[Peer]:
        for p in self.peers:
            if p.court_id == court_id:
                return p
        return None


def load_peers(project: str) -> PeersConfig:
    """Load this project's peers.yaml + reconcile with the federation block & key on disk."""
    fed = load_federation(project)

    p = project_peers_yaml_path(project)
    raw = {}
    if p.is_file():
        with p.open() as f:
            raw = yaml.safe_load(f) or {}

    self_block = raw.get("self") or {}
    self_court_id = self_block.get("court_id") or fed.court_id

    try:
        identity = load_identity(project)
        self_fp = identity.fingerprint
    except FileNotFoundError:
        self_fp = self_block.get("pub_key_fingerprint", "")

    peers = []
    for entry in raw.get("peers") or []:
        peers.append(Peer(
            name=entry.get("name", entry.get("court_id", "")),
            court_id=entry["court_id"],
            url=entry["url"].rstrip("/"),
            pub_key_fingerprint=entry["pub_key_fingerprint"],
            pub_key_b64=entry.get("pub_key_b64"),
            # Accept the historical "role" key as a fallback so a stale config
            # doesn't lock anyone out.
            relation=entry.get("relation") or entry.get("role") or "sibling",
            policy_tier=entry.get("policy_tier"),
        ))
    return PeersConfig(
        project=project,
        self_court_id=self_court_id,
        self_fingerprint=self_fp,
        peers=peers,
    )


# ---------------------------------------------------------------------------
# Path glob helpers (PR-2 will call these; defined now so the schema is wired)
# ---------------------------------------------------------------------------

def path_allowed(candidate: str, allow: list[str], deny: list[str]) -> bool:
    """Decide whether ``candidate`` (an absolute or repo-relative path) is reachable.

    Rules:
    - deny wins: if any deny glob matches, return False
    - if allow is empty, any non-denied path passes
    - if allow has entries, candidate must match at least one
    """
    for pattern in deny:
        if fnmatch.fnmatch(candidate, pattern):
            return False
    if not allow:
        return True
    return any(fnmatch.fnmatch(candidate, p) for p in allow)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_peer_error(project: str, line: str) -> None:
    project_logs_dir(project).mkdir(parents=True, exist_ok=True)
    with project_peer_errors_log(project).open("a") as f:
        f.write(f"[{iso_now()}] {line}\n")


# ---------------------------------------------------------------------------
# Bus file emission
# ---------------------------------------------------------------------------

def write_inbound_to_bus(
    project: str,
    msg: dict,
    *,
    subdir: str = "inbox",
    policy_decision: Optional[str] = None,
    policy_reasons: Optional[list[str]] = None,
) -> Path:
    """Write a verified inbound peer message into the project's bus.

    Default lands at ``$bus/<from_court>/inbox/<unix_ts>-<id>-<from>-to-<to>.md``.
    PR-2 callers may pass ``subdir="pending-approval"`` or ``"denied"`` to
    park messages that didn't auto-pass; the foreman never sees those
    files unless a human moves them into ``inbox/``.

    When ``policy_decision`` is given, frontmatter gets two extra fields
    (``policy_decision``, ``policy_reasons``) so a downstream reader
    (foreman, llm_judge, human reviewer) can see *why* the message
    landed where it did without consulting the audit log.

    The existing ``court-watcher`` only inspects ``*/outbox/*.md`` files,
    so writing into ``inbox`` / ``pending-approval`` / ``denied`` here
    does not double-route through the watcher.
    """
    from_court = msg["from_court"]
    msg_id = msg["id"]
    sender_role = msg.get("from", from_court)
    to_role = msg["to"]
    ts_epoch = int(datetime.now().timestamp())
    fname = f"{ts_epoch}-{msg_id}-{sender_role}-to-{to_role}.md"

    target = project_bus_dir(project) / from_court / subdir
    target.mkdir(parents=True, exist_ok=True)
    # Only the canonical inbox needs a .done sidecar (court-watcher pattern);
    # pending-approval / denied don't get auto-archived.
    if subdir == "inbox":
        (target / ".done").mkdir(exist_ok=True)

    fpath = target / fname
    lines = [
        "---",
        f"from: {sender_role}",
        f"from_court: {from_court}",
        f"to: {to_role}",
        f"ts: {msg.get('ts', iso_now())}",
        f"id: {msg_id}",
    ]
    in_reply_to = msg.get("in_reply_to")
    if in_reply_to:
        lines.append(f"in_reply_to: {in_reply_to}")
    attaches = msg.get("attaches") or []
    if attaches:
        lines.append(f"attaches: {json.dumps(attaches, ensure_ascii=False)}")
    if policy_decision:
        lines.append(f"policy_decision: {policy_decision}")
    if policy_reasons:
        lines.append(f"policy_reasons: {json.dumps(policy_reasons, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(msg.get("body", ""))
    fpath.write_text("\n".join(lines) + "\n")
    return fpath


def ensure_dirs(*paths: Iterable[Path]) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)
