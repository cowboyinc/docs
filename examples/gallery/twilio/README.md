# Twilio — store-hireable multi-tenant two-way SMS assistant

Status: gallery showcase for CIP-33 (Actor Hiring & Distribution). This is a
sibling of [`../ranchhand`](../ranchhand) (Slack) built on the same
store-hireable pattern, swapping the transport for **Twilio SMS**: one
deployed actor, many tenants, each tenant a Trading Post hire with its own
Twilio number, credentials, and conversation state.

Lineage: the mechanics (gateway ingress, secret-free prefilters, runner-side
`verify=` MAC, hire-bound CBFS agent) come from `ranchhand`/`cowbot`. What's
Twilio-specific is the inbound MAC (`X-Twilio-Signature`, HMAC-SHA1/base64
over `URL + sorted params`, expressed as the neutral `VerifyAlgo::Hmac{Sha1,
Base64, none}` — no provider name on the wire) and
the outbound REST send (`POST Messages.json`, HTTP Basic with a scoped API
key). Conversation state is two-way: the runner agent keeps per-correspondent
memory keyed by `(tenant number, sender number)`.

## Architecture

```
                        ┌──────────────────────────────────────────────┐
Twilio number A ────┐   │ twilio (listed actor — ZERO secrets)         │
Twilio number B ────┼──▶│  twilio_http: prefilters → To → tenant       │
Twilio number C ────┘   │  tenant_answer: hire-bound runner.agent      │
   (one gateway URL)    │   session_id = (To,From); brain(ro)+state(rw) │
                        └───────┬──────────────────────────▲───────────┘
                        relay_event                  tenant_verified
                                │                          │
                        ┌───────▼──────────────────────────┴───────────┐
                        │ twiliorelay proxy — ONE PER HIRE, hirer-owned│
                        │  manifest names exactly the hirer's 2 keys   │
                        │  1. verify= MAC gate (GET Accounts/{SID})     │
                        │  2. post_answer: POST Messages.json w/ Basic  │
                        └──────────────────────────────────────────────┘
```

Per inbound SMS: the actor acks fast (empty TwiML), builds the canonical
Twilio signature preimage (`webhook_url + sorted(params)`), and relays it +
the `X-Twilio-Signature` to the tenant's proxy. The proxy's first
`runner.http` carries `verify=` (the runner recomputes the Twilio HMAC-SHA1
signature and aborts fail-closed, CIP-24 §5.5); on `tenant_verified` the
actor runs the **hire-bound** agent job (with the conversation's `session_id`
for memory) and hands the answer back to the proxy, which sends it via the
hirer's Twilio API key. SMS has no editable placeholder, so the gate op is a
harmless authenticated account fetch rather than a "thinking…" post.

### Why each runner op is launched by a funded self-timer (D36)

A runner job's *submitter* is the actor of the enclosing top-level transaction,
and it governs which manifest the runner resolves `verify=`/`secrets=` against
and who the `hire=` binding authorizes. Each of the three runner ops therefore
must run from a specific actor — the proxy for the `verify=` MAC gate and the
outbound send (its manifest names the Twilio keys), and the listed twilio actor
for the hire-bound agent — but each is reached through an authenticated
cross-actor `call()`, whose frame carries the *caller's* actor as submitter. To
re-enter with the right submitter, the callee schedules a **timer on itself**
(`runtime.schedule_timer_ex(..., access_limit=322)`) that fires the op next
block. A plain self-send would also re-enter with the right submitter, but as a
*deferred child* capped at 162 accesses — it cannot both do its own work and
fund the nested `submit_job` the runner call mints (`DEFERRED_ACCESS_LIMIT` =
`REQUIRED_DEFERRED_CHILD_ACCESSES` = 162), so an access-metered node refuses the
child (`E1005`) and the SMS is never answered (decision **D36**, COW-3894). A
timer fired on self keeps the same `from == self` attribution *and* runs at
`TIMER_ACCESSES_LIMIT` (322 = 160 handler + 162), which reserves one full
deferred child for the runner job. This is why both `manifest.json` and
`proxy/manifest.template.json` declare the `timer.schedule` entitlement.

## The two-key split (Option B)

Twilio uses **two** distinct credentials so the long-lived Auth Token is never
used outbound:

- `TWILIO_AUTH_TOKEN` — **verify-only**. Twilio signs inbound webhooks with it;
  the runner MAC-checks with it. Never substituted into a request.
- `TWILIO_API_BASIC` — **outbound-only**. `base64("SK<sid>:<secret>")` for a
  scoped Standard API Key, substituted into the `Authorization: Basic …` of
  the outbound `Messages.json` send.

## Why a per-hire credential proxy

CIP-24 manifests name secret keys **exactly** (`account:KEY`) and the CIP-6
§12.1 subset rule means an actor can never add keys after deploy. A shared
actor therefore cannot be entitled to unknown future hirers' secrets — by
design. Each hirer deploys a small proxy whose manifest names exactly their
own two keys, stored under their own account and ACL'd to the proxy.
Consequences:

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

Pause or revoke a hire ⇒ the tenant's next SMS produces no compute.

## Files

| File | What |
|---|---|
| `cowboy.toml` | project manifest; targets are discovered from `actors/<name>/main.py` |
| `actors/twilio/main.py` | `twilio` target: webhook, tenant registry, hire-bound agent |
| `actors/relay/main.py` | `relay` target: per-hire credential proxy (hirer-deployed) |
| `manifest.json` | twilio deploy manifest (`storage.kv` + `timer.schedule`; no secrets) |
| `proxy/manifest.template.json` | proxy manifest (HIRER_EOA placeholders) |
| `capability-schema.json` | CIP-33 §2.6.1 capability document |
| `tests/` | `SimulatedChain` + hardening tests, run with `uv run pytest` |
| `deploy.sh` | creator: volumes → deploy → routes → escrow → declare |
| `connect-workspace.sh` | hirer: secrets → proxy → ACL → connect_number |
| `e2e-local.sh` | local devnet end-to-end incl. revoke (see below) |

## Simulate Locally

The tests drive both targets through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node; the
DoS-hardening suite additionally drives the real handlers with an in-process
SDK stub.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire bytes,
hex-encoded; this call is `health()` on a fresh actor and expects the
CBOR-encoded 200 "ok" envelope:

```bash
cowboy test --target twilio --handler health \
  --payload 0x \
  --expect 0xa364626f6479426f6b6673746174757318c8676865616465727381826c636f6e74656e742d747970657819746578742f706c61696e3b20636861727365743d7574662d38
```

`cowboy actor check twilio` and `cowboy actor check relay` validate the
targets offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on
`PATH` or at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner`
in a node checkout).

## Run On A Network

`./deploy.sh` (creator) deploys the `twilio` target with
`cowboy actor deploy twilio`; `./connect-workspace.sh` (hirer) deploys the
`relay` target with `cowboy actor deploy relay`. A project deploy checks the
node's actor runtime profile, so use the CLI release that matches the network
you target. The sections below walk through both sides.

## Runner/protocol prerequisite

The inbound MAC uses the PROVIDER-NEUTRAL `VerifyAlgo::Hmac { hash, encoding,
prefix }` primitive — Twilio is just `{Sha1, Base64, none}`, chosen by the
actor. No provider name touches the wire or consensus, so no new protocol
variant is needed per provider. The three repos each carry the neutral form:

- **cowboy-protocol** `job_spec` — the codec `VerifyAlgo::Hmac` primitive.
- **node** `job_envelope.rs` — parse `{"hmac":{hash,encoding,prefix}}` → `Hmac`.
- **runner** `runner-node` — one generic HMAC arm (hash/encoding/prefix).

The actor supplies the canonical preimage (`URL + sorted params`) so the
runner stays preimage-agnostic.

## Creator: deploy + list

```bash
TWILIO_PRIVATE_KEY=… TWILIO_CREATOR_PUBKEY=… \
  TWILIO_WEBHOOK_URL=https://<gateway-host>/twilio/sms ./deploy.sh
```

deploys, declares (Free term, Private code), and records the pin. The
`webhook_url` is load-bearing: it is the first field of the Twilio signature
preimage and must match the URL configured on the Twilio number **exactly**.
Listing in the General Store is the standard store-admin flow.

**Per-hire state volumes** are creator-provisioned (CIP-33 §2.6.4 — no
in-actor deploy/provision syscall):

```bash
cowboy volume create --name twilio-state-h<hire_id> --private --private-key <creator-key>
cowboy volume allow-actor --name twilio-state-h<hire_id> --actor <twilio-addr> --private-key <creator-key>
```

Watch `HireCreated` events and provision on demand; the hirer's
`connect_number` names the volume.

## Hirer: connect your number

1. Hire twilio (General Store or `cowboy trading-post hire … --term free`).
2. Provision a Twilio number and a scoped **Standard API Key** (SID + secret);
   note your Account SID and Auth Token.
3. ```bash
   HIRER_PRIVATE_KEY=… TWILIO_ACTOR=… TWILIO_HIRE_ID=… TWILIO_NUMBER=+1… \
     TWILIO_ACCOUNT_SID=AC… TWILIO_AUTH_TOKEN_VALUE=… \
     TWILIO_API_KEY_SID=SK… TWILIO_API_KEY_SECRET=… \
     TWILIO_STATE_VOLUME=… ./connect-workspace.sh
   ```
4. Set your Twilio number's "A message comes in" webhook to the gateway URL
   (`https://<gateway-host>/twilio/sms`) — it must equal the actor's
   `webhook_url`.

## Trust model, stated plainly

- `connect_number` binds a Twilio number **once** (rebind: original binder or
  owner). There is no in-PVM Trading Post read surface yet, so the handler
  cannot check `sender == hire.hirer` on-chain — a squatter can bind a number
  they don't own, but can never serve it (their proxy fails the Twilio MAC for
  that number) and the owner can prune (`admin_disconnect`). In-PVM `get_hire`
  is a CIP-33 follow-up; when it lands, this becomes a hard check.
- The `webhook_url` is set by the creator/owner; a wrong value only makes the
  MAC fail (a liveness bug), never a bypass — the listed actor holds no token.
- The artifact-equivalence claim (escrow ↔ served volume) is CIP-33 §4's
  signed claim, as everywhere in v1.
- Tenant state volumes are per-hire; the shared brain volume is read-only to
  the agent. Cross-tenant isolation of conversation memory is by volume
  boundary, not prompt discipline.

## Local end-to-end

`./e2e-local.sh` drives the full loop on a local devnet — declare → hire →
connect → signed synthetic Twilio webhook (real `X-Twilio-Signature`) → MAC
verify on the runner → hire-bound agent job → answer sent to a local Twilio
stub → forged-signature rejection → `revoke_hire` → the next SMS is refused at
job admission. Requirements: node + runner worktrees carrying the neutral
`VerifyAlgo::Hmac` primitive and the `[patch]` to the local cowboy-protocol
checkout (see the script header).
