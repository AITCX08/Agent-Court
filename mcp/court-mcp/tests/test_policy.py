"""Tests for the policy engine (PR-2).

Covers:
- pure evaluate() decision matrix (tier_a/b/c, hard rules, peer override)
- HARDCODED deny paths and keywords (non-overridable)
- user-defined allow_paths / deny_paths from court.yaml
- policy.yaml extra_keywords appended to hardcoded list
- end-to-end HTTP round-trip with attaches → correct subdir on disk
- policy-log.jsonl audit trail

Run with:
    cd mcp/court-mcp && .venv/bin/pytest tests/test_policy.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import peer_daemon  # noqa: E402
import peer_lib  # noqa: E402
import policy  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (deliberately separate from test_peer.py — both files share the
# same root_dir style but we don't want a regression in one to silently
# break the other through fixture coupling)
# ---------------------------------------------------------------------------


def _seed(root: Path, project: str, *,
          expose_roles: list[str] | None = None,
          allow_paths: list[str] | None = None,
          deny_paths: list[str] | None = None,
          policy_yaml: dict | None = None) -> Path:
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed = {
        "enabled": True,
        "expose_roles": expose_roles if expose_roles is not None else ["foreman"],
        "expose_read": ["foreman"],
    }
    if allow_paths is not None:
        fed["allow_paths"] = allow_paths
    if deny_paths is not None:
        fed["deny_paths"] = deny_paths

    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "foreman",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
        "federation": fed,
    }
    (pdir / "court.yaml").write_text(yaml.safe_dump(court_yaml))
    if policy_yaml is not None:
        (pdir / "policy.yaml").write_text(yaml.safe_dump(policy_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    root = tmp_path / "alice"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


@pytest.fixture
def project_with_self_peer(root_dir):
    """Single project + a peer 'bob' whose pubkey is this project's own
    keypair, so the test can sign + the daemon can verify in one process."""
    _seed(root_dir, "p", expose_roles=["foreman", "auditor"])
    identity = peer_lib.generate_keypair("p", force=True)
    return _setup_peer(identity, "p")


def _setup_peer(identity, project, *, policy_tier=None):
    entry = {
        "name": "Bob",
        "court_id": "bob",
        "url": "http://127.0.0.1:0",
        "pub_key_fingerprint": identity.fingerprint,
        "pub_key_b64": identity.pub_b64,
        "relation": "child",
    }
    if policy_tier:
        entry["policy_tier"] = policy_tier
    peer_lib.project_peers_yaml_path(project).write_text(yaml.safe_dump({
        "peers": [entry],
    }))
    return identity


def _signed(identity, *, body="hello", to="foreman", attaches=None,
            from_court="bob"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": from_court,
        "to": to,
        "body": body,
        "ts": peer_lib.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = peer_lib.sign_message(msg, identity.priv)
    return msg


async def _post(app, payload):
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
    return asyncio.run(_post(app, payload))


# ---------------------------------------------------------------------------
# Pure evaluate() — decision matrix
# ---------------------------------------------------------------------------


def _msg(**overrides):
    base = {"from_court": "bob", "to": "foreman", "body": "ok", "id": "x"}
    base.update(overrides)
    return base


def test_tier_a_pins_human_required():
    d = policy.evaluate(_msg(), peer_tier="tier_a", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert d.tier == "tier_a"


def test_tier_b_pins_judge():
    d = policy.evaluate(_msg(), peer_tier="tier_b", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "judge"


def test_tier_c_pins_auto_pass():
    d = policy.evaluate(_msg(), peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "auto_pass"


def test_default_tier_when_peer_omits():
    cfg = policy.PolicyConfig(default_tier="tier_a")
    d = policy.evaluate(_msg(), peer_tier=None, policy=cfg,
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_unknown_tier_falls_back_to_human_required():
    d = policy.evaluate(_msg(), peer_tier="tier_z", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert any("unknown tier" in r for r in d.reasons)


# ---------------------------------------------------------------------------
# Hard rules — paths
# ---------------------------------------------------------------------------


def test_hardcoded_ssh_path_is_denied_even_for_tier_c():
    msg = _msg(attaches=["/home/alice/.ssh/id_ed25519"])
    d = policy.evaluate(msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "denied"
    assert d.tier == "hard_rule"


def test_hardcoded_env_path_is_denied():
    msg = _msg(attaches=["app/.env"])
    d = policy.evaluate(msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "denied"


def test_user_deny_path_is_denied():
    msg = _msg(attaches=["prompts/foreman.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=[], deny_paths=["prompts/**"],
    )
    assert d.action == "denied"


def test_allow_paths_force_human_required_when_attach_outside():
    msg = _msg(attaches=["src/random.py"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"], deny_paths=[],
    )
    assert d.action == "human_required"
    assert d.tier == "hard_rule"


def test_allow_paths_pass_when_every_attach_covered():
    msg = _msg(attaches=["bus/foreman/inbox/x.md", "bus/foreman/inbox/y.md"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"], deny_paths=[],
    )
    assert d.action == "auto_pass"


def test_one_bad_attach_among_many_blocks_the_whole_message():
    msg = _msg(attaches=["bus/foreman/inbox/ok.md", "/etc/passwd"])
    d = policy.evaluate(
        msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
        allow_paths=[], deny_paths=[],
    )
    assert d.action == "denied"


# ---------------------------------------------------------------------------
# Hard rules — keywords
# ---------------------------------------------------------------------------


def test_hardcoded_keyword_forces_human_required():
    msg = _msg(body="here is my api_key=abcdef1234")
    d = policy.evaluate(msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert d.tier == "hard_rule"


def test_keyword_match_is_case_insensitive():
    msg = _msg(body="here is my PASSWORD")
    d = policy.evaluate(msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_policy_yaml_extra_keyword_is_honoured():
    cfg = policy.PolicyConfig(extra_keywords=["merger", "wire transfer"])
    msg = _msg(body="re: the merger plan")
    d = policy.evaluate(msg, peer_tier="tier_c", policy=cfg,
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_clean_body_with_tier_c_is_auto_pass():
    msg = _msg(body="please review the new auth changes")
    d = policy.evaluate(msg, peer_tier="tier_c", policy=policy.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "auto_pass"


# ---------------------------------------------------------------------------
# load_policy
# ---------------------------------------------------------------------------


def test_load_policy_missing_file_returns_defaults(root_dir):
    _seed(root_dir, "blank")
    cfg = policy.load_policy("blank")
    assert cfg.default_tier == "tier_b"
    assert cfg.extra_keywords == []


def test_load_policy_reads_yaml(root_dir):
    _seed(root_dir, "tweaked", policy_yaml={
        "default_tier": "tier_a",
        "sensitive_keywords": ["acme", "alpha"],
    })
    cfg = policy.load_policy("tweaked")
    assert cfg.default_tier == "tier_a"
    assert "acme" in cfg.extra_keywords


def test_load_policy_swallows_malformed_yaml(root_dir):
    _seed(root_dir, "broken")
    # write garbage
    (root_dir / "projects" / "broken" / "policy.yaml").write_text(": :: not yaml ::")
    cfg = policy.load_policy("broken")
    assert cfg.default_tier == "tier_b"   # falls back to defaults


# ---------------------------------------------------------------------------
# peers.yaml policy_tier parsing
# ---------------------------------------------------------------------------


def test_peers_yaml_policy_tier_loaded(root_dir):
    _seed(root_dir, "scoped")
    identity = peer_lib.generate_keypair("scoped")
    _setup_peer(identity, "scoped", policy_tier="tier_a")
    peers = peer_lib.load_peers("scoped")
    bob = peers.by_court_id("bob")
    assert bob is not None
    assert bob.policy_tier == "tier_a"


def test_peers_yaml_policy_tier_optional(root_dir):
    _seed(root_dir, "scoped")
    identity = peer_lib.generate_keypair("scoped")
    _setup_peer(identity, "scoped", policy_tier=None)
    bob = peer_lib.load_peers("scoped").by_court_id("bob")
    assert bob.policy_tier is None


# ---------------------------------------------------------------------------
# HTTP end-to-end — daemon routes by decision
# ---------------------------------------------------------------------------


def test_e2e_clean_message_lands_in_inbox(project_with_self_peer):
    identity = project_with_self_peer
    app = peer_daemon.make_app("p")
    msg = _signed(identity, body="just a plain review request")
    status, body = _round_trip(app, msg)
    assert status == 200
    # default tier_b → judge → inbox
    assert body["decision"] == "judge"
    assert body["status"] == "accepted"
    inbox = peer_lib.project_bus_dir("p") / "bob" / "inbox"
    assert len(list(inbox.glob("*.md"))) == 1


def test_e2e_keyword_routes_to_pending_approval(project_with_self_peer):
    identity = project_with_self_peer
    app = peer_daemon.make_app("p")
    msg = _signed(identity, body="the prod password is hunter2")
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "human_required"
    assert body["status"] == "pending_approval"
    pending = peer_lib.project_bus_dir("p") / "bob" / "pending-approval"
    files = list(pending.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "policy_decision: human_required" in content
    # nothing should have leaked into inbox
    inbox = peer_lib.project_bus_dir("p") / "bob" / "inbox"
    assert not list(inbox.glob("*.md"))


def test_e2e_attach_to_ssh_routes_to_denied(project_with_self_peer):
    identity = project_with_self_peer
    app = peer_daemon.make_app("p")
    msg = _signed(identity, body="have a look",
                  attaches=["~/.ssh/id_ed25519"])
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "denied"
    assert body["status"] == "denied"
    denied = peer_lib.project_bus_dir("p") / "bob" / "denied"
    files = list(denied.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "policy_decision: denied" in content
    assert "id_ed25519" in content


def test_e2e_per_peer_tier_a_blocks_otherwise_clean_message(root_dir):
    _seed(root_dir, "strict")
    identity = peer_lib.generate_keypair("strict", force=True)
    _setup_peer(identity, "strict", policy_tier="tier_a")

    app = peer_daemon.make_app("strict")
    import secrets
    msg = {
        "from": "upstream",
        "from_court": "bob",
        "to": "foreman",
        "body": "fully clean message",
        "ts": peer_lib.iso_now(),
        "id": secrets.token_hex(4),
    }
    msg["signature"] = peer_lib.sign_message(msg, identity.priv)
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "human_required"
    assert body["tier"] == "tier_a"


def test_e2e_attaches_field_is_in_signed_payload(project_with_self_peer):
    """An attacker stripping/forging `attaches` after signing must fail verify."""
    identity = project_with_self_peer
    app = peer_daemon.make_app("p")
    msg = _signed(identity, body="hi", attaches=["bus/foreman/inbox/x.md"])
    # Forge: drop the attaches field but keep the signature.
    forged = dict(msg)
    forged.pop("attaches")
    status, body = _round_trip(app, forged)
    assert status == 401
    assert body["error"] == "bad_signature"


def test_policy_log_jsonl_captures_decision(project_with_self_peer):
    identity = project_with_self_peer
    app = peer_daemon.make_app("p")
    msg = _signed(identity, body="ok")
    _round_trip(app, msg)

    log_path = peer_lib.project_logs_dir("p") / "policy-log.jsonl"
    assert log_path.is_file()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["from_court"] == "bob"
    assert entry["to"] == "foreman"
    assert entry["action"] == "judge"
    assert entry["tier"] == "tier_b"
    assert isinstance(entry["reasons"], list)
