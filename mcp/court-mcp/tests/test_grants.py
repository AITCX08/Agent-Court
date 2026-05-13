"""Tests for the PR-4 grants layer.

Covers:
- mint / load / revoke round-trip
- TTL parser edge cases
- expired grants are filtered from load_active_grants
- grants are peer-scoped (peer A's grant doesn't widen peer B)
- grants widen allow_paths inside policy.evaluate
- grants do NOT bypass HARDCODED_DENY_PATHS or user deny_paths
- grants do NOT relax expose_roles or change tier action
- end-to-end HTTP: a path covered by a grant lets the message through
- revoke takes effect immediately
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import grants  # noqa: E402
import peer_daemon  # noqa: E402
import peer_lib  # noqa: E402
import policy  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed(root: Path, project: str, *,
          allow_paths: list[str] | None = None,
          deny_paths: list[str] | None = None) -> Path:
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed = {
        "enabled": True,
        "expose_roles": ["foreman"],
    }
    if allow_paths is not None:
        fed["allow_paths"] = allow_paths
    if deny_paths is not None:
        fed["deny_paths"] = deny_paths

    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "foreman",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
        "federation": fed,
    }
    (pdir / "court.yaml").write_text(yaml.safe_dump(court_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    root = tmp_path / "court-root"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


# ---------------------------------------------------------------------------
# TTL parsing
# ---------------------------------------------------------------------------


def test_parse_ttl_seconds_int():
    assert grants.parse_ttl(30) == 30


def test_parse_ttl_minutes():
    assert grants.parse_ttl("30m") == 1800


def test_parse_ttl_hours():
    assert grants.parse_ttl("2h") == 7200


def test_parse_ttl_compound():
    assert grants.parse_ttl("2h30m") == 9000
    assert grants.parse_ttl("1d12h") == 86400 + 12 * 3600
    assert grants.parse_ttl("1d 6h") == 86400 + 6 * 3600


def test_parse_ttl_seconds_string():
    assert grants.parse_ttl("90") == 90
    assert grants.parse_ttl("45s") == 45


def test_parse_ttl_case_insensitive():
    assert grants.parse_ttl("30M") == 1800
    assert grants.parse_ttl("1H") == 3600


def test_parse_ttl_rejects_garbage():
    with pytest.raises(ValueError):
        grants.parse_ttl("forever")
    with pytest.raises(ValueError):
        grants.parse_ttl("")
    with pytest.raises(ValueError):
        grants.parse_ttl(0)
    with pytest.raises(ValueError):
        grants.parse_ttl("-30m")


# ---------------------------------------------------------------------------
# Mint + load + revoke
# ---------------------------------------------------------------------------


def test_mint_writes_file_with_expected_shape(root_dir):
    _seed(root_dir, "p")
    g = grants.mint_grant(
        "p", "bob", ["notes/x.md", "notes/y.md"], ttl="1h", issued_by="alice@host",
    )
    path = grants.grants_dir("p") / f"{g.id}.json"
    assert path.is_file()
    raw = json.loads(path.read_text())
    assert raw["id"] == g.id
    assert raw["granted_to"] == "bob"
    assert raw["paths"] == ["notes/x.md", "notes/y.md"]
    assert raw["issued_by"] == "alice@host"
    # issued_ts < expires_ts and roughly 1h apart
    issued = datetime.fromisoformat(raw["issued_ts"])
    expires = datetime.fromisoformat(raw["expires_ts"])
    delta = (expires - issued).total_seconds()
    assert 3590 <= delta <= 3610


def test_mint_rejects_empty_paths(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        grants.mint_grant("p", "bob", [], ttl="30m")


def test_mint_rejects_non_string_path(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        grants.mint_grant("p", "bob", ["ok.md", 42], ttl="30m")


def test_mint_rejects_hostile_peer_name(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(peer_lib.UnsafeNameError):
        grants.mint_grant("p", "../shared", ["x.md"], ttl="30m")


def test_list_grants_returns_sorted_by_issue_time(root_dir):
    _seed(root_dir, "p")
    g1 = grants.mint_grant("p", "a", ["x.md"], ttl="1h")
    time.sleep(1.1)  # ensure issued_ts differs
    g2 = grants.mint_grant("p", "b", ["y.md"], ttl="1h")
    rows = grants.list_grants("p")
    assert [g.id for g in rows] == [g1.id, g2.id]


def test_revoke_removes_file(root_dir):
    _seed(root_dir, "p")
    g = grants.mint_grant("p", "bob", ["x.md"], ttl="30m")
    assert grants.revoke_grant("p", g.id) is True
    assert grants.revoke_grant("p", g.id) is False  # idempotent
    assert grants.list_grants("p") == []


def test_revoke_rejects_unsafe_id(root_dir):
    _seed(root_dir, "p")
    assert grants.revoke_grant("p", "../../etc") is False


def test_load_active_grants_filters_expired(root_dir):
    _seed(root_dir, "p")
    g_live = grants.mint_grant("p", "alive", ["x.md"], ttl="1h")
    g_dead = grants.mint_grant("p", "dead",  ["y.md"], ttl="1h")
    # Manually backdate the second grant.
    p = grants.grants_dir("p") / f"{g_dead.id}.json"
    raw = json.loads(p.read_text())
    raw["expires_ts"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    p.write_text(json.dumps(raw))

    active = grants.load_active_grants("p")
    assert [g.id for g in active] == [g_live.id]


def test_load_grants_for_peer_isolates(root_dir):
    _seed(root_dir, "p")
    grants.mint_grant("p", "alice", ["alice/**"], ttl="1h")
    grants.mint_grant("p", "bob",   ["bob/**"],   ttl="1h")
    assert grants.load_grants_for_peer("p", "alice") == ["alice/**"]
    assert grants.load_grants_for_peer("p", "bob")   == ["bob/**"]
    assert grants.load_grants_for_peer("p", "ghost") == []


# ---------------------------------------------------------------------------
# policy.evaluate integration
# ---------------------------------------------------------------------------


def _msg(**overrides):
    base = {"from_court": "bob", "to": "foreman", "body": "ok", "id": "x"}
    base.update(overrides)
    return base


def test_grant_widens_allow_paths():
    """A path NOT in allow_paths but covered by grant_paths must pass
    instead of being upgraded to human_required."""
    msg = _msg(attaches=["notes/secret.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"],
        deny_paths=[],
        grant_paths=["notes/**"],
    )
    assert d.action == "auto_pass"
    assert any("active grant" in r for r in d.reasons)


def test_grant_does_not_bypass_hardcoded_deny():
    msg = _msg(attaches=[".ssh/id_rsa"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"],
        deny_paths=[],
        grant_paths=[".ssh/**"],         # peer "granted" ssh — must still deny
    )
    assert d.action == "denied"


def test_grant_does_not_bypass_user_deny():
    msg = _msg(attaches=["prompts/foreman.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**", "prompts/**"],
        deny_paths=["prompts/**"],         # deny wins
        grant_paths=["prompts/**"],
    )
    assert d.action == "denied"


def test_empty_grants_behave_like_no_grants():
    msg = _msg(attaches=["bus/foreman/inbox/x.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"],
        deny_paths=[],
        grant_paths=[],
    )
    assert d.action == "auto_pass"


def test_grant_cannot_invent_allow_paths_when_none_exist():
    """If allow_paths is empty (no static whitelist), a grant alone
    does NOT start enforcing a whitelist — the policy still falls
    through to the tier check. This matches the documented semantics:
    grants are a *widening*, not a replacement."""
    msg = _msg(attaches=["random.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=[],                 # no static whitelist
        deny_paths=[],
        grant_paths=["only-this.md"],
    )
    # No allow_paths to enforce, so the grant is irrelevant.
    assert d.action == "auto_pass"


# ---------------------------------------------------------------------------
# End-to-end through the daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_self_peer(root_dir):
    """Project with allow_paths=['bus/foreman/inbox/**'] and a peer 'bob'
    whose pubkey is this project's own keypair (so test can sign and
    daemon can verify in one process)."""
    _seed(root_dir, "p", allow_paths=["bus/foreman/inbox/**"])
    identity = peer_lib.generate_keypair("p", force=True)
    peer_lib.project_peers_yaml_path("p").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob",
            "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "child",
            "policy_tier": "tier_c",   # auto_pass when allow_paths OK
        }],
    }))
    return identity


def _signed(identity, *, attaches=None, body="hi"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": "bob",
        "to": "foreman",
        "body": body,
        "ts": peer_lib.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = peer_lib.sign_message(msg, identity.priv)
    return msg


async def _post(project, payload):
    """Fresh app per call — aiohttp Application can't be reused across loops."""
    import aiohttp
    from aiohttp.test_utils import TestServer
    app = peer_daemon.make_app(project)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/inbox"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as r:
                return r.status, await r.json()
    finally:
        await server.close()


def _round_trip(project, payload):
    return asyncio.run(_post(project, payload))


def test_e2e_grant_lets_otherwise_blocked_attach_through(project_with_self_peer):
    """Without a grant: a notes/* attach is outside allow_paths and ends
    up in pending-approval. With a grant: same message → auto_pass."""
    identity = project_with_self_peer

    # Pre-grant: blocked
    msg = _signed(identity, attaches=["notes/q2.md"])
    status, body = _round_trip("p", msg)
    assert body["decision"] == "human_required"

    # Grant access; new message should sail through
    grants.mint_grant("p", "bob", ["notes/**"], ttl="1h")
    msg2 = _signed(identity, attaches=["notes/q2.md"])
    status2, body2 = _round_trip("p", msg2)
    assert status2 == 200
    assert body2["decision"] == "auto_pass"
    assert any("active grant" in r for r in body2["reasons"])


def test_e2e_revoke_takes_effect_immediately(project_with_self_peer):
    identity = project_with_self_peer

    g = grants.mint_grant("p", "bob", ["notes/**"], ttl="1h")
    # Sanity: grant covers a message
    msg = _signed(identity, attaches=["notes/x.md"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "auto_pass"

    # Revoke and try again with a fresh id (replay cache would otherwise reject)
    assert grants.revoke_grant("p", g.id) is True
    msg2 = _signed(identity, attaches=["notes/x.md"])
    _, body2 = _round_trip("p", msg2)
    assert body2["decision"] == "human_required"


def test_e2e_grant_for_other_peer_does_not_help(project_with_self_peer):
    """Bob has no grant. Carol does. Bob's attach must still be blocked."""
    identity = project_with_self_peer
    grants.mint_grant("p", "carol", ["notes/**"], ttl="1h")

    msg = _signed(identity, attaches=["notes/x.md"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "human_required"


def test_e2e_grant_still_respects_hardcoded_deny(project_with_self_peer):
    identity = project_with_self_peer
    # Carelessly grant ssh access — hardcoded layer should still bite.
    grants.mint_grant("p", "bob", [".ssh/**"], ttl="1h")
    msg = _signed(identity, attaches=[".ssh/id_rsa"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "denied"
