"""agent-court — court-peer HTTP receiver daemon (per project).

Listens on the address configured by ``--bind`` / ``COURT_PEER_BIND``
(default ``0.0.0.0:8765``).

Endpoints:
- ``GET  /healthz``  — liveness probe for ``list_peers.reachable``.
- ``POST /inbox``    — accept a signed inter-court message and drop it into
                       ``$COURT_ROOT/projects/<project>/bus/<from_court>/inbox/``.

Per-project model:
- The daemon is started with ``court-peer <project>``.
- It reads ``court.yaml``'s ``federation:`` block. If ``enabled: false``
  (the default), the daemon refuses to start — federation is off for
  that project.
- It loads ``peers.yaml`` from the *same* project directory; peers
  registered there are the only ones permitted to POST.
- It loads the project's own keypair (the public key is what other
  peers verify against; the daemon itself only uses the public key
  identity for fingerprint reporting at startup).

PR-1 scope: signature verification + role whitelist enforcement
(``expose_roles``). PR-2 adds policy engine and path-level enforcement.
"""

from __future__ import annotations

import argparse
import os
import sys

from aiohttp import web

import policy
from peer_lib import (
    append_peer_error,
    court_root,
    iso_now,
    load_federation,
    load_identity,
    load_peers,
    project_court_yaml_path,
    project_dir,
    verify_signature,
    write_inbound_to_bus,
)


REQUIRED_FIELDS = ("from", "from_court", "to", "body", "ts", "id", "signature")


def _log(project: str, line: str) -> None:
    print(f"[{iso_now()}] [{project}] {line}", file=sys.stderr, flush=True)


def make_app(project: str) -> web.Application:
    """Build the aiohttp app for a project. Caller is expected to have already
    validated that federation is enabled — but we re-check on each request so a
    flipped flag in court.yaml takes effect without restart."""
    app = web.Application()
    app["project"] = project
    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/inbox", _inbox)
    return app


async def _healthz(request: web.Request) -> web.Response:
    project = request.app["project"]
    fed = load_federation(project)
    return web.json_response({
        "status": "ok",
        "project": project,
        "court_id": fed.court_id,
        "federation_enabled": fed.enabled,
    })


async def _inbox(request: web.Request) -> web.Response:
    project = request.app["project"]

    # Re-check federation on every request so toggling the flag in court.yaml
    # takes effect without restart.
    fed = load_federation(project)
    if not fed.enabled:
        append_peer_error(project, f"federation-disabled: rejecting inbound (project={project})")
        _log(project, "reject 403: federation disabled for this project")
        return web.json_response({"error": "federation_disabled"}, status=403)

    try:
        msg = await request.json()
    except Exception as e:
        append_peer_error(project, f"bad-json: {e}")
        _log(project, f"reject 400: bad JSON ({e})")
        return web.json_response({"error": "bad_json"}, status=400)

    missing = [k for k in REQUIRED_FIELDS if k not in msg]
    if missing:
        append_peer_error(project, f"missing-fields: {missing} from={msg.get('from_court')}")
        _log(project, f"reject 400: missing fields {missing}")
        return web.json_response({"error": "missing_fields", "fields": missing}, status=400)

    from_court = msg["from_court"]

    # 403 — sender not registered as a peer of this project.
    peers_cfg = load_peers(project)
    peer = peers_cfg.by_court_id(from_court)
    if peer is None:
        append_peer_error(project, f"unknown-sender: from_court={from_court}")
        _log(project, f"reject 403: unknown sender '{from_court}'")
        return web.json_response(
            {"error": "unknown_sender", "from_court": from_court},
            status=403,
        )

    # 401 — no key to verify against, or signature doesn't match.
    pub_b64 = peer.pub_key_b64
    if not pub_b64:
        append_peer_error(
            project, f"no-pubkey: peer '{from_court}' missing pub_key_b64 in peers.yaml"
        )
        _log(project, f"reject 401: no pub_key_b64 for peer '{from_court}'")
        return web.json_response(
            {"error": "missing_peer_pub_key", "from_court": from_court}, status=401,
        )

    signature = msg["signature"]
    if not verify_signature(msg, signature, pub_b64):
        append_peer_error(
            project, f"bad-signature: from_court={from_court} id={msg.get('id')}"
        )
        _log(project, f"reject 401: bad signature from '{from_court}' id={msg.get('id')}")
        return web.json_response(
            {"error": "bad_signature", "from_court": from_court}, status=401,
        )

    # 403 — target role not in expose_roles whitelist.
    target = msg["to"]
    if fed.expose_roles and target not in fed.expose_roles:
        append_peer_error(
            project,
            f"role-not-exposed: from_court={from_court} to={target} "
            f"allowed={fed.expose_roles}",
        )
        _log(
            project,
            f"reject 403: role '{target}' not in expose_roles {fed.expose_roles}",
        )
        return web.json_response(
            {
                "error": "role_not_exposed",
                "to": target,
                "expose_roles": fed.expose_roles,
            },
            status=403,
        )

    # PR-2 policy layer — runs after signature + role whitelist pass.
    # peer_tier comes from peers.yaml entry, falls back to policy.default_tier.
    policy_cfg = policy.load_policy(project)
    decision = policy.evaluate(
        msg,
        peer_tier=peer.policy_tier,
        policy=policy_cfg,
        allow_paths=fed.allow_paths,
        deny_paths=fed.deny_paths,
    )
    policy.log_decision(project, msg, decision)

    subdir = policy.subdir_for(decision.action)
    fpath = write_inbound_to_bus(
        project,
        msg,
        subdir=subdir,
        policy_decision=decision.action,
        policy_reasons=decision.reasons,
    )

    _log(
        project,
        f"accepted: from {from_court} ({msg.get('from')}) -> {target} "
        f"id={msg['id']} decision={decision.action} tier={decision.tier} "
        f"file={fpath}",
    )

    # Map decision → outer status. We always return 200: signature + role checks
    # already passed, so the network exchange itself is fine. The policy
    # outcome is conveyed in the decision field so the sender's MCP tool can
    # surface it back to the upstream LLM.
    status_map = {
        "auto_pass": "accepted",
        "judge": "accepted",
        "human_required": "pending_approval",
        "denied": "denied",
    }
    return web.json_response({
        "status": status_map.get(decision.action, "accepted"),
        "decision": decision.action,
        "tier": decision.tier,
        "reasons": decision.reasons,
        "file_path": str(fpath),
        "id": msg["id"],
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="court-peer HTTP receiver daemon")
    parser.add_argument("project", help="project name under $COURT_ROOT/projects/")
    parser.add_argument(
        "--bind",
        default=os.environ.get("COURT_PEER_BIND", "0.0.0.0:8765"),
        help="address:port to bind (default 0.0.0.0:8765, env COURT_PEER_BIND)",
    )
    args = parser.parse_args()

    project = args.project
    if not project_dir(project).is_dir():
        print(
            f"[court-peer] project '{project}' not found at {project_dir(project)}",
            file=sys.stderr,
        )
        return 1

    if not project_court_yaml_path(project).is_file():
        print(
            f"[court-peer] missing court.yaml at {project_court_yaml_path(project)}",
            file=sys.stderr,
        )
        return 1

    fed = load_federation(project)
    if not fed.enabled:
        print(
            f"[court-peer] federation is disabled for project '{project}'.",
            file=sys.stderr,
        )
        print(
            f"[court-peer] enable it in {project_court_yaml_path(project)} under the "
            f"`federation:` block (see projects/example/court.yaml for the schema).",
            file=sys.stderr,
        )
        return 1

    try:
        identity = load_identity(project)
    except FileNotFoundError as e:
        print(f"[court-peer] {e}", file=sys.stderr)
        return 1

    host, _, port_s = args.bind.partition(":")
    if not port_s:
        print(f"[court-peer] --bind must be host:port, got '{args.bind}'", file=sys.stderr)
        return 2
    port = int(port_s)

    app = make_app(project)
    _log(project, f"court-peer listening on {host}:{port}")
    _log(project, f"court_root={court_root()}")
    _log(project, f"court_id={fed.court_id} fingerprint={identity.fingerprint}")
    _log(project, f"expose_roles={fed.expose_roles or 'ALL'}")
    web.run_app(app, host=host, port=port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
