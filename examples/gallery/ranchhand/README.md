# Ranchhand — store-hireable multi-tenant Slack assistant

Status: gallery showcase for CIP-33 (Actor Hiring & Distribution). Ranchhand
is the first actor built to be **hired in the General Store**: one deployed
actor, many tenants, each tenant a Trading Post hire with its own Slack
workspace, credentials, and state.

Lineage: the Slack mechanics (gateway ingress, secret-free prefilters,
runner-side `verify=` MAC, CBFS-backed agent) come from
[`../cowbot`](../cowbot); what's new is everything multi-tenant and
everything hire-gated.

## Architecture

```
                        ┌──────────────────────────────────────────────┐
Slack workspace A ──┐   │ ranchhand (listed actor — ZERO secrets)      │
Slack workspace B ──┼──▶│  slack_http: prefilters → team_id → tenant   │
Slack workspace C ──┘   │  tenant_answer: hire-bound runner.agent      │
   (one gateway URL)    │   volumes: brain (ro, private) + state (rw)  │
                        └───────┬──────────────────────────▲───────────┘
                        relay_event                  tenant_verified
                                │                          │
                        ┌───────▼──────────────────────────┴───────────┐
                        │ slackrelay proxy — ONE PER HIRE, hirer-owned │
                        │  manifest names exactly the hirer's 2 keys   │
                        │  1. verify= MAC gate + ":thinking:" post     │
                        │  2. post_answer: chat.update w/ hirer token  │
                        └──────────────────────────────────────────────┘
```

Per Slack event: ranchhand acks fast and relays the verbatim body + MAC
headers to the tenant's proxy; the proxy's first `runner.http` carries
`verify=` (the runner recomputes the Slack v0 HMAC and aborts fail-closed,
CIP-24 §5.5) and posts the placeholder; on `tenant_verified` ranchhand runs
the **hire-bound** agent job and hands the answer back to the proxy to post.

## Why a per-hire credential proxy

CIP-24 manifests name secret keys **exactly** (`account:KEY`) and the CIP-6
§12.1 subset rule means an actor can never add keys after deploy. A shared
actor therefore cannot be entitled to unknown future hirers' secrets — by
design. The composition that works on today's rails: each hirer deploys a
~300-line proxy whose manifest names exactly their own two keys, stored
under their own account and ACL'd to the proxy. Consequences:

- The **listed actor holds zero secrets entitlements** — nothing to review
  but logic.
- Tenant credentials are isolated per hire **by construction** (CBSS's
  two-sided check: actor manifest ∧ owner ACL).
- The hirer can kill their integration unilaterally: revoke the ACL or
  delete the secrets. No store or creator involvement.

## How the hire gates service (CIP-33)

Every agent job carries the `hire=` binding
`{hire_id, declaration_id, version, artifact_id}`:

1. **Chain, at dispatch** — `validate_hire_binding` rejects the job unless
   the hire exists, is Active, its pins match, and the submitter is the
   hired actor or the hirer.
2. **Runner, before execution and at every lease-epoch boundary** — the
   runner queries `/trading-post/lease-authorize` and refuses to start (or
   continue past a boundary) without an allow. Fails closed on a missing
   expiry.
3. **CBSS, on key release** — `tradingpost/{decl}/{ver}/{artifact}` labels
   release only with a signed `cip33{hire_id}` context that the committee
   verifies against the same lease-authorize predicate. The declaration's
   escrow envelope is a real threshold ciphertext produced at declare time.

Pause or revoke a hire ⇒ the tenant's next event produces no compute.

## Files

| File | What |
|---|---|
| `cowboy.toml` | project manifest; targets are discovered from `actors/<name>/main.py` |
| `actors/ranchhand/main.py` | `ranchhand` target: webhook, tenant registry, hire-bound agent |
| `actors/relay/main.py` | `relay` target: per-hire credential proxy (hirer-deployed) |
| `manifest.json` | ranchhand deploy manifest (`storage.kv` only) |
| `proxy/manifest.template.json` | proxy manifest (HIRER_EOA placeholders) |
| `capability-schema.json` | CIP-33 §2.6.1 capability document |
| `slack-app-manifest.yaml` | per-tenant Slack app template |
| `tests/` | `SimulatedChain` tests, run with `uv run pytest` |
| `deploy.sh` | creator: volumes → deploy → routes → escrow → declare |
| `connect-workspace.sh` | hirer: secrets → proxy → ACL → connect_workspace |
| `e2e-local.sh` | local devnet end-to-end incl. revoke (see below) |

## Simulate Locally

The tests drive both targets through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire bytes,
hex-encoded; this call is `health()` on a fresh actor and expects the
CBOR-encoded 200 "ok" envelope:

```bash
cowboy test --target ranchhand --handler health \
  --payload 0x \
  --expect 0xa364626f6479426f6b6673746174757318c8676865616465727381826c636f6e74656e742d747970657819746578742f706c61696e3b20636861727365743d7574662d38
```

`cowboy actor check ranchhand` and `cowboy actor check relay` validate the
targets offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on
`PATH` or at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner`
in a node checkout).

## Run On A Network

`./deploy.sh` (creator) deploys the `ranchhand` target with
`cowboy actor deploy ranchhand`; `./connect-workspace.sh` (hirer) deploys the
`relay` target with `cowboy actor deploy relay`. A project deploy checks the
node's actor runtime profile, so use the CLI release that matches the network
you target. The sections below walk through both sides.

## Creator: deploy + list

```bash
RANCHHAND_PRIVATE_KEY=… RANCHHAND_CREATOR_PUBKEY=… ./deploy.sh
```

deploys, declares (Free term, Private code), and records the pin. Listing in
the General Store is the standard store-admin flow: submit the declaration
version, review, `register_artifact`, `attest`, Live.

**Per-hire state volumes** are creator-provisioned (CIP-33 §2.6.4 — no
in-actor deploy/provision syscall):

```bash
cowboy volume create --name ranchhand-state-h<hire_id> --private --private-key <creator-key>
cowboy volume allow-actor --name ranchhand-state-h<hire_id> --actor <ranchhand-addr> --private-key <creator-key>
```

Watch `HireCreated` events (or the store's my-hires feed) and provision on
demand; the hirer's `connect_workspace` names the volume.

## Hirer: connect your workspace

1. Hire ranchhand (General Store or `cowboy trading-post hire … --term free`).
2. Create your Slack app from `slack-app-manifest.yaml`; capture the signing
   secret + bot token.
3. `HIRER_PRIVATE_KEY=… RANCHHAND_ACTOR=… RANCHHAND_HIRE_ID=… SLACK_TEAM_ID=…
   SLACK_SIGNING_SECRET=… SLACK_BOT_TOKEN=… RANCHHAND_STATE_VOLUME=…
   ./connect-workspace.sh`
4. Point the Slack app's event subscription at the gateway URL; Slack's
   `url_verification` ping is answered by the actor.

## Trust model, stated plainly

- `connect_workspace` binds a `team_id` **once** (rebind: original binder or
  owner). There is no in-PVM Trading Post read surface yet, so the handler
  cannot check `sender == hire.hirer` on-chain — a squatter can bind a
  team_id they don't own, but can never serve it (their proxy fails the
  Slack MAC for that workspace) and the owner can prune
  (`admin_disconnect`). In-PVM `get_hire` is a CIP-33 follow-up; when it
  lands, this becomes a hard check.
- The artifact-equivalence claim (escrow ↔ served volume) is CIP-33 §4's
  signed claim, as everywhere in v1.
- Tenant state volumes are per-hire; the shared brain volume is read-only
  to the agent. Cross-tenant isolation of conversation memory is by
  volume boundary, not prompt discipline.

## Local end-to-end

`./e2e-local.sh` drives the full loop on a local devnet — declare → hire →
connect → signed synthetic Slack event → MAC verify on the runner → hire-
bound agent job → answer posted to a local Slack stub → `revoke_hire` →
the next event is refused at job admission. Requirements: the CIP-33 node
branch, the merged CIP-33+CIP-24 runner branch, and the cbss authorizer
branch (see the script header).
