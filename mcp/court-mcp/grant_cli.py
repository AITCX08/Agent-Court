"""agent-court — ``court-grant`` command-line entry point (PR-4).

Three subcommands:

.. code-block:: shell

    court-grant <project> <peer> <path> [<path>...]  [--ttl 30m]
    court-grant <project> list
    court-grant <project> revoke <grant-id>

The bare three-arg form ``<project> <peer> <path>`` is treated as
``add`` so daily use stays terse.

Lives next to the MCP server because they share a venv. The
``bin/court-grant`` shell wrapper just exec's the venv's python
against this module.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Make sibling modules importable when invoked directly via venv python.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import grants  # noqa: E402
from peer_lib import project_dir  # noqa: E402


def _resolve_issuer() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    host = os.environ.get("HOSTNAME") or ""
    if host:
        return f"{user}@{host}"
    return user


def _cmd_add(args) -> int:
    if not Path(project_dir(args.project)).is_dir():
        print(f"[court-grant] project '{args.project}' not found at {project_dir(args.project)}",
              file=sys.stderr)
        return 1
    try:
        grant = grants.mint_grant(
            args.project,
            args.peer,
            args.paths,
            ttl=args.ttl,
            issued_by=args.issued_by or _resolve_issuer(),
        )
    except ValueError as e:
        print(f"[court-grant] {e}", file=sys.stderr)
        return 2

    print(f"granted to    : {grant.granted_to}")
    print(f"paths         : {grant.paths}")
    print(f"id            : {grant.id}")
    print(f"issued_ts     : {grant.issued_ts}")
    print(f"expires_ts    : {grant.expires_ts}")
    print(f"issued_by     : {grant.issued_by}")
    print(f"file          : {grants.grants_dir(args.project) / (grant.id + '.json')}")
    return 0


def _cmd_list(args) -> int:
    if not Path(project_dir(args.project)).is_dir():
        print(f"[court-grant] project '{args.project}' not found", file=sys.stderr)
        return 1
    rows = grants.list_grants(args.project)
    if not rows:
        print(f"[court-grant] no grants for project '{args.project}'")
        return 0

    now = datetime.now().astimezone()
    print(f"{'STATE':<8} {'ID':<10} {'PEER':<30} {'EXPIRES':<27} PATHS")
    for g in rows:
        state = "active" if g.is_active() else "expired"
        peer = g.granted_to
        if len(peer) > 28:
            peer = peer[:27] + "…"
        print(f"{state:<8} {g.id:<10} {peer:<30} {g.expires_ts:<27} {', '.join(g.paths)}")
    return 0


def _cmd_revoke(args) -> int:
    ok = grants.revoke_grant(args.project, args.grant_id)
    if ok:
        print(f"[court-grant] revoked {args.grant_id}")
        return 0
    print(f"[court-grant] no such grant: {args.grant_id}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="court-grant",
        description="Mint / list / revoke temporary path-access grants for a federated peer.",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    add = sub.add_parser("add", help="Mint a new grant (also the default action).")
    add.add_argument("project")
    add.add_argument("peer", help="The peer's court_id, as it appears in peers.yaml.")
    add.add_argument("paths", nargs="+", help="One or more path globs to grant access to.")
    add.add_argument("--ttl", default="30m",
                     help="How long the grant is valid (e.g. 30m, 1h, 2h30m, 1d). Default 30m.")
    add.add_argument("--issued-by", default="",
                     help="Free-form issuer tag for the audit log. Default: $USER@$HOSTNAME.")
    add.set_defaults(func=_cmd_add)

    lst = sub.add_parser("list", help="List all grants (active + expired) for a project.")
    lst.add_argument("project")
    lst.set_defaults(func=_cmd_list)

    rev = sub.add_parser("revoke", help="Revoke a grant by id.")
    rev.add_argument("project")
    rev.add_argument("grant_id")
    rev.set_defaults(func=_cmd_revoke)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # Docs put the project first: `court-grant <project> <subcmd-or-peer> ...`.
    # argparse puts the subcommand first, so we reorder before parsing.
    #   <project> list                → list <project>
    #   <project> revoke <id>         → revoke <project> <id>
    #   <project> <peer> <path>...    → add <project> <peer> <path>...
    if argv and argv[0] not in ("-h", "--help"):
        if len(argv) >= 2 and argv[1] in ("list", "revoke"):
            argv = [argv[1], argv[0], *argv[2:]]
        else:
            argv = ["add", *argv]
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
