# LAN deployment — two-machine quickstart

Walk-through for getting two `agent-court` projects talking on the same
local network. No public IPs, no VPN — works the moment both machines
can ping each other on the LAN.

> This is PR-1: HTTP transport + ed25519 signing + role whitelist.
> There is **no policy engine, no human approval, no path-level
> enforcement, no encryption** yet — anything in `peers.yaml` whose
> target role is in `expose_roles` will land directly in your bus.
> Lock down `peers.yaml` accordingly until later PRs land.

## Mental model first

A "court" lives at **one project on one machine** —
`$COURT_ROOT/projects/<project>/`. Each project has its own keypair,
its own `peers.yaml`, and its own `court_id`. Two projects on the same
machine cannot infer each other's existence; they are separate courts
to the outside world.

That means **everything below must be repeated for each project pair
you want federated**. Generating a keypair once for `project-A` does
*not* let `project-B` on the same machine talk to anything.

## Pre-flight

On each machine:

1. `agent-court` checked out, `bin/` on PATH.
2. The MCP server venv installed:
   ```bash
   cd /path/to/agent-court/mcp/court-mcp
   uv venv .venv
   uv pip install --python .venv/bin/python -e .
   ```
3. The project you want to federate exists under
   `$COURT_ROOT/projects/<project>/`. The shipped example works:
   ```bash
   cp -r projects/example ~/.agent-court/projects/example
   ```

## 1. Generate the project's keypair

On **Alice's** machine, for the `example` project:

```
$ court-keygen example
[court-keygen] new keypair for project 'example':
  /Users/alice/.agent-court/projects/example/identity/priv.key  (mode 0600)
  /Users/alice/.agent-court/projects/example/identity/pub.key   (mode 0644)

public key      : MCowBQYDK2VwAyEAaG6...     # base64 ed25519 pubkey
fingerprint     : 7a4c0b9e3d2f8a16          # SHA-256 prefix, 16 hex chars

Share both with the peer who will federate with this project.
They paste them into THEIR project's peers.yaml under the entry for you.
```

On **Bob's** machine: same, for *his* `example` project. Each side now
has a per-project `priv.key` / `pub.key` under that project's
`identity/` directory.

Re-running `court-keygen example` is a no-op unless you pass `--force`.

## 2. Enable federation in `court.yaml`

By default the `federation:` block in `court.yaml` is commented out —
the daemon refuses to start. Uncomment it (or write your own) and
configure the whitelist:

```yaml
# ~/.agent-court/projects/example/court.yaml
federation:
  enabled: true

  # Your court_id on the network. Defaults to "<hostname>-<project>".
  court_id: "alice-laptop-example"

  # Which roles outside peers may dispatch *to*. Default: only foreman.
  expose_roles:
    - foreman

  # PR-2 will enforce these against any path referenced in inbound
  # messages. Schema is wired in PR-1; enforcement lands with policy.
  allow_paths:
    - "bus/foreman/inbox/**"
    - "shared/notes-public.md"
  deny_paths:
    - "prompts/**"
    - "shared/notes-private.md"
```

The `expose_roles` whitelist is enforced *now*: anything inbound whose
`to:` field is not in this list gets a `403 role_not_exposed` and the
attempt is logged.

## 3. Exchange fingerprints + public keys

Out of band (Signal, in-person, etc.), share for each project:

| Field | Value to send |
|---|---|
| `court_id` | What the other side will reference you as. Defaults to `<hostname>-<project>`; override under `federation.court_id` in `court.yaml`. |
| `fingerprint` | The 16-byte hex `court-keygen` printed. Lets the other side eyeball-verify the key on first paste. |
| `pub_key_b64` | The full base64 public key (also from `court-keygen` output, or `cat $COURT_ROOT/projects/<project>/identity/pub.key`). **Required at runtime** — without it the peer cannot verify your signatures. |

## 4. Write `peers.yaml` on each side

This file lives **inside the project**, not in a shared config dir.

`~/.agent-court/projects/example/peers.yaml` on Alice:

```yaml
self:
  court_id: "alice-laptop-example"
  pub_key_fingerprint: "7a4c0b9e3d2f8a16"     # informational

peers:
  - name: "Bob"
    court_id: "bob-laptop-example"
    url: "http://192.168.1.50:8765"
    pub_key_fingerprint: "f0e1d2c3b4a59687"
    pub_key_b64: "MCowBQYDK2VwAyEAhV0z..."     # Bob's public key
    relation: "sibling"                        # parent | child | sibling
```

`~/.agent-court/projects/example/peers.yaml` on Bob — symmetric, listing
Alice (with `relation: sibling` on his side too).

Replace IPs with your own. `ip addr` on Linux or `ipconfig getifaddr en0`
on macOS to find your LAN address.

> The `relation:` field replaces the legacy `role:` field. The loader
> still accepts `role:` for backward compatibility, but write new
> configs with `relation:`. It's informational in PR-1; PR-2 will use
> it to let policy rules vary by relation (e.g. a parent court's
> dispatches are auto-allowed without approval).

## 5. Start the receiver daemon

On each side, **per project** you want to receive:

```bash
court-peer example
```

This binds `0.0.0.0:8765` and serves `POST /inbox` + `GET /healthz`.
Override the bind with `--bind` or `COURT_PEER_BIND`:

```bash
COURT_PEER_BIND=192.168.1.50:9000 court-peer example
```

If you federate multiple projects on the same machine, give each its
own port:

```bash
COURT_PEER_BIND=0.0.0.0:8765 court-peer example   &
COURT_PEER_BIND=0.0.0.0:8766 court-peer client-a  &
```

If federation is disabled in that project's `court.yaml`, the daemon
refuses to start with a pointer to the config block.

## 6. Send a test message from Alice → Bob

From any MCP-aware client connected to Alice's `court-mcp` server
(Claude Code, Cursor, Zed, a custom assistant), call:

```python
list_peers(project="example")
# returns: {project, self: {court_id, fingerprint, federation_enabled, ...},
#           peers: [...]}.  reachable=true once Bob's daemon is up.

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="hi from Alice — please look at issue #42",
    target_role="foreman",
)
# returns: {
#   http_status: 200,
#   response: {
#     status: "accepted",
#     file_path: ".../bus/alice-laptop-example/inbox/<file>.md",
#     id: ...
#   }
# }
```

On Bob's machine the file shows up at:

```
~/.agent-court/projects/example/bus/alice-laptop-example/inbox/1715432400-7f3d2e1a-upstream-to-foreman.md
```

Bob's foreman (if running under `court-up example`) can read that
manually for now. PR-2 will add a policy engine that decides whether
to route inbound peer messages straight into the foreman's main inbox
or hold them for approval.

## Firewall checklist

`court-peer` is plain HTTP, no TLS. Open the port both ways on each
machine's firewall — most home LANs are wide open already.

| OS | Allow inbound TCP 8765 |
|---|---|
| macOS | `System Settings → Network → Firewall → Options → Add court-peer's python binary "Allow"` |
| Ubuntu | `sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp` |
| Windows | `New-NetFirewallRule -DisplayName "agent-court" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow` |

## Troubleshooting

### "transport_error" in `dispatch_to_peer` response

- Verify the URL is reachable: `curl http://192.168.1.50:8765/healthz` from Alice.
- If `curl` hangs → firewall is dropping. See firewall checklist.
- If `Connection refused` → daemon isn't running on that IP/port. Check
  `ps aux | grep peer_daemon`.

### 401 `bad_signature` or `missing_peer_pub_key`

- The peer rejected your signature. Almost always one of:
  - `pub_key_b64` for your `court_id` in *their* project's
    `peers.yaml` doesn't match your current `priv.key`. Did you
    regenerate `court-keygen`? You must re-share the new public key.
  - You and the peer disagree on which fields go into the signed
    payload. Both sides must be on the same `agent-court` version.
  - You pointed `dispatch_to_peer` at the wrong `project=...`, so the
    private key being used to sign belongs to a different court than
    the peer expects.
- Check `$COURT_ROOT/projects/<project>/logs/peer-errors.log` on the
  receiving side for the specific failure reason.

### 403 `federation_disabled`

- The peer's `court.yaml` has no `federation:` block, or
  `federation.enabled: false`. The flag is re-read per-request, so the
  peer can flip it back to `true` without restarting the daemon.

### 403 `unknown_sender`

- Your `court_id` isn't in the peer's `peers.yaml`. Ask them to add
  you, or check that the `court_id` you're using matches what they
  configured. Remember: each project has its own `peers.yaml` — being
  listed in their `project-A/peers.yaml` does not grant access to
  `project-B`.

### 403 `role_not_exposed`

- You dispatched to a role not in the peer's `federation.expose_roles`
  list. By default only `foreman` is exposed; ask the peer to either
  route via foreman or add your target role to `expose_roles`.

### `list_peers` shows `reachable: false`

- Other side's daemon down or unreachable. Same as the transport_error
  checks above.

## Going beyond LAN

For machines on different networks:

- **Recommended**: install [tailscale](https://tailscale.com) on both
  machines, use the tailscale-assigned IP in `peers.yaml`. Same as LAN
  from then on, plus end-to-end encryption.
- **Self-hosted**: run `frp` or `cloudflared` to expose your court-peer
  port. Put the public URL in `peers.yaml`. Pair it with a real TLS
  reverse proxy if the traffic crosses the open internet (PR-1 doesn't
  ship TLS).

Both will be documented under `docs/networking.md` in a follow-up PR.
