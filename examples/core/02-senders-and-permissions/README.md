# Senders And Permissions

This lesson is focused on caller identity and protected state changes. It is the
reference for the two authorization layers an actor has, and which one to reach
for:

- **Protocol ownership** — the runtime tracks an owner in a reserved storage slot
  and the SDK enforces it at dispatch. Use it for admin handlers.
- **Application roles** — an allowlist the actor keeps in its own storage. Use it
  for anything the protocol has no concept of, like a `writer` role.

## What It Covers

- `runtime.assume_ownership_if_unowned()` — the deployer claims ownership from
  `init`
- `runtime.is_initialized()` — the flag that makes a repeated `init` a no-op
- `runtime.transfer_ownership(...)` — hand ownership to a different account
- `@callable_by(OWNER, decode="cbor")` — owner-only `grant_role` / `revoke_role`,
  enforced by the SDK before the handler body runs
- `@public(decode="cbor")` — `write_entry`, reachable by anyone and gated on the
  actor's own `writer` allowlist
- `self.sender` — the caller as an `Address`, and `runtime.get_owner()` for the
  owner
- `Address(...)` — normalizing a submitted address so a checksummed grant matches
  an unprefixed sender
- module-level PVM entrypoints (`init`, `grant_role`, `revoke_role`, `write_entry`,
  `get_entries`, `get_roles`)

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/roles/main.py` - the actor
- `tests/test_roles.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Why This Example Exists

- Most useful actors need to know who is calling.
- Hand-rolled owner checks drift from the owner the protocol actually enforces.
  This shows the runtime-tracked owner instead, and keeps a hand-written check
  only for the role layer, which is genuinely application state.

## Flow

1. The deployer initializes the actor and becomes the owner by default.
2. The owner grants the `writer` role to a separate account.
3. A delegated writer calls `write_entry`.
4. Public readers inspect `get_entries` and `get_roles`.

## Handler Payloads

- `init`: raw JSON from the deploy path. `{ "owner": "0x..." }` is optional; if
  omitted, the sender becomes owner. `cowboy actor deploy` calls `init` in the
  deploy transaction with `{}`, so pass an owner with
  `--init-payload '{"owner":"0x..."}'` — a later `init` call is a no-op.
- `grant_role`: `{ "role": "writer", "address": "0x..." }`
- `revoke_role`: `{ "role": "writer", "address": "0x..." }`
- `write_entry`: `{ "content": "..." }`
- `get_entries`: `{}`
- `get_roles`: `{}` — returns `{ "owner": "0x...", "roles": { ... } }`, with the
  owner read from the runtime rather than from actor storage

Every handler except `init` decodes CBOR, so the demo sends them with
`--payload-json`. `init` is the deploy boundary and reads the CLI's raw JSON.

## Caveats

- **Owner-only calls fail, they do not return.** A non-owner calling `grant_role`
  or `revoke_role` raises `PermissionDeniedError` at dispatch — there is no
  handler body to return `{"error": ...}` from. The `writer` check inside
  `write_entry` is ordinary code, so that one still returns `UNAUTHORIZED`.
- `init` stays `@public` because the demo calls it again after deploy, and
  returns `already_initialized` on every call after the first. Re-initialization
  is therefore not a path to re-taking ownership or resetting the role map.
- `assume_ownership_if_unowned()` is first-caller-wins. It is safe here because
  `cowboy actor deploy` invokes `init` in the deploy transaction; calling it from
  any other handler would let a stranger claim the actor.
- The example uses a small allowlist and role model on purpose.

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `write_entry({"content": "hello"})` on a fresh actor,
where the caller holds no `writer` role, and expects the CBOR-encoded
`UNAUTHORIZED` reply:

```bash
cowboy test --target roles --handler write_entry \
  --payload 0xa167636f6e74656e746568656c6c6f \
  --expect 0xa1656572726f726c554e415554484f52495a4544
```

`cowboy actor check roles` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` creates an owner and a writer wallet, deploys the target with
`cowboy actor deploy roles` (the deploy runs `init`, so the deployer becomes
the owner), then grants, writes and reads with the two keys. A project deploy
checks the node's actor runtime profile, so use the CLI release that matches
the network you target.
