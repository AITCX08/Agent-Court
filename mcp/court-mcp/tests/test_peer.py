"""Tests for the peer-network layer (PR-1).

Covers:
- per-project keypair generation + reload roundtrip
- canonical JSON determinism
- sign / verify happy path + tamper detection
- peers.yaml loader (with `relation` field, backward-compatible with `role`)
- federation enable/disable gating
- HTTP POST /inbox round-trip: good signature, bad signature, unknown
  sender, missing fields, role-not-exposed, federation-disabled

Run with:
    cd mcp/court-mcp && .venv/bin/pytest -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

# Make the parent dir importable as flat modules (peer_lib, peer_daemon).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import peer_lib  # noqa: E402
import peer_daemon  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_project(root: Path, project: str, *, federation_enabled: bool = True,
                  expose_roles: list[str] | None = None,
                  court_id: str | None = None) -> Path:
    """Create a minimal project skeleton at <root>/projects/<project>/."""
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed_block: dict = {}
    if federation_enabled:
        fed_block = {
            "enabled": True,
            "expose_roles": expose_roles if expose_roles is not None else ["foreman"],
            "expose_read": ["foreman"],
        }
        if court_id is not None:
            fed_block["court_id"] = court_id

    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "foreman",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
    }
    if fed_block:
        court_yaml["federation"] = fed_block

    (pdir / "court.yaml").write_text(yaml.safe_dump(court_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    """Set COURT_ROOT to a fresh tmp dir."""
    root = tmp_path / "alice"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    # macOS hostname suffix would make court_id unstable across machines;
    # pin it for deterministic tests.
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


@pytest.fixture
def example_project(root_dir):
    _seed_project(root_dir, "example")
    return "example"


@pytest.fixture
def example_identity(root_dir, example_project):
    return peer_lib.generate_keypair(example_project, force=True)


# ---------------------------------------------------------------------------
# Identity round-trip
# ---------------------------------------------------------------------------


def test_keygen_creates_priv_and_pub(root_dir, example_project):
    identity = peer_lib.generate_keypair(example_project)
    assert peer_lib.project_priv_key_path(example_project).is_file()
    assert peer_lib.project_pub_key_path(example_project).is_file()
    assert oct(peer_lib.project_priv_key_path(example_project).stat().st_mode)[-3:] == "600"
    assert len(identity.fingerprint) == 32
    assert identity.project == example_project


def test_keygen_refuses_overwrite_without_force(root_dir, example_project):
    peer_lib.generate_keypair(example_project)
    with pytest.raises(FileExistsError):
        peer_lib.generate_keypair(example_project)
    new = peer_lib.generate_keypair(example_project, force=True)
    assert new.fingerprint


def test_load_identity_matches_generated(example_identity, example_project):
    loaded = peer_lib.load_identity(example_project)
    assert loaded.pub_b64 == example_identity.pub_b64
    assert loaded.fingerprint == example_identity.fingerprint


def test_projects_isolated(root_dir):
    """Project A's keypair is invisible to project B and vice versa."""
    _seed_project(root_dir, "a")
    _seed_project(root_dir, "b")
    id_a = peer_lib.generate_keypair("a")
    id_b = peer_lib.generate_keypair("b")
    assert id_a.pub_b64 != id_b.pub_b64
    assert id_a.fingerprint != id_b.fingerprint
    # default court_id derived from project name
    fed_a = peer_lib.load_federation("a")
    fed_b = peer_lib.load_federation("b")
    assert fed_a.court_id == "testhost-a"
    assert fed_b.court_id == "testhost-b"


# ---------------------------------------------------------------------------
# Canonical JSON + signatures
# ---------------------------------------------------------------------------


def test_canonical_payload_is_deterministic(root_dir):
    msg1 = {"from": "u", "from_court": "a", "to": "f", "body": "h",
            "ts": "2026-05-11T10:00:00+08:00", "id": "abc123",
            "signature": "should-not-be-included"}
    msg2 = {"ts": "2026-05-11T10:00:00+08:00", "id": "abc123",
            "body": "h", "to": "f", "from_court": "a", "from": "u",
            "extra": "ignored", "signature": "totally-different"}
    assert peer_lib.canonical_payload(msg1) == peer_lib.canonical_payload(msg2)


def test_sign_and_verify_happy_path(example_identity):
    msg = {"from": "upstream", "from_court": "alice", "to": "foreman",
           "body": "hi", "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = peer_lib.sign_message(msg, example_identity.priv)
    assert peer_lib.verify_signature(msg, sig, example_identity.pub_b64) is True


def test_verify_rejects_tampered_body(example_identity):
    msg = {"from": "upstream", "from_court": "alice", "to": "foreman",
           "body": "hi", "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = peer_lib.sign_message(msg, example_identity.priv)
    tampered = dict(msg, body="MALICIOUS")
    assert peer_lib.verify_signature(tampered, sig, example_identity.pub_b64) is False


def test_verify_rejects_wrong_pubkey(root_dir, example_identity, example_project):
    msg = {"from": "u", "from_court": "a", "to": "f", "body": "h",
           "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = peer_lib.sign_message(msg, example_identity.priv)
    # generate an unrelated keypair via a second project
    _seed_project(root_dir, "other")
    other = peer_lib.generate_keypair("other")
    assert peer_lib.verify_signature(msg, sig, other.pub_b64) is False


# ---------------------------------------------------------------------------
# peers.yaml loader
# ---------------------------------------------------------------------------


def test_load_peers_missing_file_returns_empty(example_identity, example_project):
    peers = peer_lib.load_peers(example_project)
    assert peers.peers == []
    assert peers.self_fingerprint == example_identity.fingerprint


def test_load_peers_parses_entries_with_relation(example_identity, example_project):
    peer_lib.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "self": {"court_id": "alice"},
        "peers": [{
            "name": "Bob",
            "court_id": "bob",
            "url": "http://192.168.1.50:8765/",
            "pub_key_fingerprint": "bbbb",
            "pub_key_b64": "BBBB==",
            "relation": "child",
        }],
    }))
    peers = peer_lib.load_peers(example_project)
    assert peers.self_court_id == "alice"
    bob = peers.by_court_id("bob")
    assert bob is not None
    assert bob.url == "http://192.168.1.50:8765"   # trailing slash stripped
    assert bob.relation == "child"


def test_load_peers_accepts_legacy_role_field(example_identity, example_project):
    """Older configs using `role:` (pre-PR-1 rename) should still load."""
    peer_lib.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "peers": [{
            "name": "Legacy",
            "court_id": "legacy",
            "url": "http://x",
            "pub_key_fingerprint": "x",
            "pub_key_b64": "X==",
            "role": "parent",
        }],
    }))
    peers = peer_lib.load_peers(example_project)
    legacy = peers.by_court_id("legacy")
    assert legacy is not None
    assert legacy.relation == "parent"


# ---------------------------------------------------------------------------
# Federation loader
# ---------------------------------------------------------------------------


def test_federation_disabled_by_default(root_dir):
    _seed_project(root_dir, "minimal", federation_enabled=False)
    fed = peer_lib.load_federation("minimal")
    assert fed.enabled is False
    assert fed.court_id == "testhost-minimal"


def test_federation_enabled_reads_block(root_dir):
    _seed_project(
        root_dir, "open",
        federation_enabled=True,
        court_id="custom-id",
        expose_roles=["foreman", "auditor"],
    )
    fed = peer_lib.load_federation("open")
    assert fed.enabled is True
    assert fed.court_id == "custom-id"
    assert fed.expose_roles == ["foreman", "auditor"]


# ---------------------------------------------------------------------------
# Path glob helpers (schema-wired, PR-2 will use)
# ---------------------------------------------------------------------------


def test_path_allowed_deny_wins():
    assert peer_lib.path_allowed("prompts/foreman.md", allow=["**"], deny=["prompts/**"]) is False


def test_path_allowed_no_allow_means_open():
    assert peer_lib.path_allowed("any/path.md", allow=[], deny=["other/**"]) is True


def test_path_allowed_must_match_allow():
    assert peer_lib.path_allowed(
        "bus/foreman/inbox/x.md",
        allow=["bus/foreman/inbox/**"],
        deny=[],
    ) is True
    assert peer_lib.path_allowed(
        "shared/leak.md",
        allow=["bus/foreman/inbox/**"],
        deny=[],
    ) is False


# ---------------------------------------------------------------------------
# HTTP round-trip (POST /inbox)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_self_peer(root_dir, example_project, example_identity):
    """Register a peer named 'bob' that *happens to use Alice's key* so we can
    sign + verify in one process. Also wire example_project's federation."""
    peer_lib.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "self": {"court_id": "alice"},
        "peers": [{
            "name": "Bob",
            "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": example_identity.fingerprint,
            "pub_key_b64": example_identity.pub_b64,
            "relation": "child",
        }],
    }))
    return example_identity


def _build_signed_msg(identity, *, from_court="bob", to="foreman", body="hello"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": from_court,
        "to": to,
        "body": body,
        "ts": peer_lib.iso_now(),
        "id": secrets.token_hex(4),
    }
    msg["signature"] = peer_lib.sign_message(msg, identity.priv)
    return msg


async def _post_and_read(app, payload):
    import aiohttp
    from aiohttp.test_utils import TestServer

    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/inbox"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return resp.status, await resp.json()
    finally:
        await server.close()


def _round_trip(app, payload):
    return asyncio.run(_post_and_read(app, payload))


def test_inbox_accepts_valid_signature(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = peer_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    status, body = _round_trip(app, msg)

    assert status == 200, body
    assert body["status"] == "accepted"
    assert body["id"] == msg["id"]
    bus = peer_lib.project_bus_dir(example_project) / "bob" / "inbox"
    files = list(bus.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "from_court: bob" in content
    assert "to: foreman" in content
    assert msg["body"] in content


def test_inbox_rejects_bad_signature(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = peer_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    msg["body"] = "TAMPERED"

    status, body = _round_trip(app, msg)
    assert status == 401
    assert body["error"] == "bad_signature"


def test_inbox_rejects_unknown_sender(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = peer_daemon.make_app(example_project)
    msg = _build_signed_msg(identity, from_court="stranger")
    msg["signature"] = peer_lib.sign_message(msg, identity.priv)

    status, body = _round_trip(app, msg)
    assert status == 403
    assert body["error"] == "unknown_sender"


def test_inbox_rejects_missing_fields(project_with_self_peer, example_project):
    app = peer_daemon.make_app(example_project)
    status, body = _round_trip(app, {"from": "x"})
    assert status == 400
    assert body["error"] == "missing_fields"


def test_inbox_rejects_when_federation_disabled(root_dir):
    """A request to a project whose federation.enabled is false → 403."""
    _seed_project(root_dir, "private", federation_enabled=False)
    app = peer_daemon.make_app("private")
    status, body = _round_trip(app, {
        "from": "u", "from_court": "x", "to": "foreman",
        "body": "h", "ts": "now", "id": "1", "signature": "z",
    })
    assert status == 403
    assert body["error"] == "federation_disabled"


def test_inbox_rejects_role_not_exposed(root_dir):
    """Even with valid signature, dispatching to a role not in expose_roles → 403."""
    _seed_project(root_dir, "scoped", expose_roles=["foreman"])
    identity = peer_lib.generate_keypair("scoped")
    peer_lib.project_peers_yaml_path("scoped").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Sibling",
            "court_id": "sibling-court",
            "url": "http://x",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "sibling",
        }],
    }))
    app = peer_daemon.make_app("scoped")
    # signed message targeting `backend` instead of foreman
    msg = _build_signed_msg(identity, from_court="sibling-court", to="backend")
    status, body = _round_trip(app, msg)
    assert status == 403
    assert body["error"] == "role_not_exposed"
    assert body["expose_roles"] == ["foreman"]
