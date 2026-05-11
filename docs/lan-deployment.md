# LAN deployment — two-machine quickstart

Walk-through for getting two `agent-court` projects talking on the same
local network. No public IPs, no VPN — works the moment both machines
can ping each other on the LAN.

> Status: PR-1 (HTTP + signing + role whitelist) and PR-2 (policy
> engine + path/keyword gating + pending-approval bin) are live.
> Still ahead: PR-3 LLM judge, PR-4 sudo-style temp authorization,
> PR-5 multi-channel human approval (FeiShu/WeChat), PR-6 IM
> redundancy, and TLS. PR-2's `tier_b → judge` branch currently
> passes through to inbox with a warning log — it will route to
> `llm_judge` once PR-3 lands.

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

  # PR-2: paths the policy engine checks any inbound `attaches:` field
  # against. Allow non-empty + attach not covered → human_required.
  # Deny match (here OR in the hardcoded list) → denied.
  allow_paths:
    - "bus/foreman/inbox/**"
    - "shared/notes-public.md"
  deny_paths:
    - "prompts/**"
    - "shared/notes-private.md"
```

The `expose_roles` whitelist and `allow_paths` / `deny_paths` are
both enforced. After the role check passes, the policy engine grades
the message and routes it to inbox / pending-approval / denied — see
the "Policy gating" section below.

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

Bob's foreman (running under `court-up example`) sees the file via
court-watcher and reacts.

## Policy gating (PR-2)

Once an inbound message clears signature + role checks, the policy
engine grades it and routes it to one of three on-disk locations:

| Decision | Lands at | Means |
|---|---|---|
| `auto_pass` / `judge` | `bus/<peer>/inbox/` | Delivered to foreman normally |
| `human_required` | `bus/<peer>/pending-approval/` | Waiting — a human must `mv` it into inbox |
| `denied` | `bus/<peer>/denied/` | Audit-only; never reaches foreman |

The response from `dispatch_to_peer` always shows the decision so the
sender's LLM can react:

```json
{
  "http_status": 200,
  "response": {
    "status": "pending_approval",
    "decision": "human_required",
    "tier": "hard_rule",
    "reasons": ["sensitive keyword 'password' in body → human_required"],
    "file_path": ".../bus/alice-laptop-example/pending-approval/...md"
  }
}
```

### Optional: `policy.yaml`

Add `~/.agent-court/projects/example/policy.yaml` to tune the default
tier and add custom sensitive keywords:

```yaml
default_tier: tier_b           # tier_a (human) | tier_b (judge) | tier_c (auto)
sensitive_keywords:
  - "wire transfer"
  - "merger"
```

If the file is missing the defaults are `tier_b` + no extra keywords.

### Per-peer tier (in `peers.yaml`)

```yaml
peers:
  - name: "External vendor"
    court_id: "vendor-build-bot"
    relation: "sibling"
    policy_tier: "tier_a"          # untrusted: everything → pending-approval
```

### Trying it: `attaches` + `dispatch_to_peer`

```python
dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="please review the diff",
    attaches=["bus/foreman/inbox/diff.md"],   # passes allow_paths
)
# → decision: judge (or auto_pass if tier_c)

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="here is the prod password=hunter2",
)
# → decision: human_required (keyword)

dispatch_to_peer(
    project="example",
    peer_court_id="bob-laptop-example",
    message="have a look",
    attaches=["~/.ssh/id_ed25519"],
)
# → decision: denied (hardcoded path)
```

The decision trail is appended to
`~/.agent-court/projects/example/logs/policy-log.jsonl`:

```bash
tail -f ~/.agent-court/projects/example/logs/policy-log.jsonl
```

### Approving a `pending-approval` message

There is no approval UI yet (PR-5 adds terminal + FeiShu + WeChat).
For now, eyeball the file and move it manually:

```bash
cd ~/.agent-court/projects/example/bus/alice-laptop-example
cat pending-approval/*.md           # read the body + policy_reasons
mv pending-approval/<file>.md inbox/   # release to foreman
```

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

### `decision: denied` in response

- An attach matched a deny rule (yours or hardcoded). The message is
  *not* delivered — it sits in `bus/<your-court-id>/denied/` on the
  receiver for audit. Inspect the `reasons` field in the response:
  ```
  "reasons": ["attach '/etc/passwd' hits hardcoded deny '/etc/**'"]
  ```
- Hardcoded denies cannot be lifted from `court.yaml` — by design.
  If you genuinely need that path, restructure the dispatch (e.g.
  paste the relevant content into the body) or have the peer add a
  manual sudo grant (PR-4 will productize this).

### `decision: human_required` / `status: pending_approval`

- Either the sender's peer entry is `policy_tier: tier_a`, or the
  body triggered a sensitive-keyword match, or an attach landed
  outside `allow_paths`. The file is in
  `bus/<your-court-id>/pending-approval/` on the receiver; a human
  there must `mv` it to `inbox/` to actually deliver.
- Check the receiver's `logs/policy-log.jsonl` — every decision has a
  `reasons` array explaining which rule fired.

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
