---
title: "CIP-14: DNS-Addressable Actors"
description: HTTP ingress, actor naming, read-only execution, gateway dispatch, and request receipts
---

# CIP-14: DNS-Addressable Actors

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-03-07
  **Requires:** CIP-2 (Off-Chain Compute), CIP-3 (Gas), CIP-5 (Timers), CIP-6 (SDK)
</Note>

This document specifies the required protocol behavior. Implementation coverage and unresolved contracts are listed in §14; a normative requirement is not a claim that the corresponding service or instruction is implemented.

---

## 1. Abstract

This proposal defines **DNS-Addressable Actors** — a system for assigning human-readable domain names to Cowboy actors and routing internet HTTP traffic to them through a dedicated Gateway network. The core primitive is an **ingress entitlement** (`ingress.http`) that allows an actor to receive inbound HTTP requests, and an **on-chain Route Registry** that maps domain names to actor addresses.

This CIP specifies:

- The `ingress.http` entitlement grant and its parameter schema.
- A Route Registry system actor that maps names to actor addresses.
- A canonical HTTP request/response envelope for actor message handlers.
- An explicit **query path** (read-only, no consensus) using the read-only handler RPC (§8.3), distinct from the **command path** (state-mutating, consensus-required).
- A Gateway role — a dedicated ingress node type that bridges HTTP to actor messaging.
- Subdomain-first naming under `cowboy.network`.

Related specifications cover the following independently:

- Public asset hosting from CBFS `Visibility::Public` volumes (CIP-15).
- CIP-7 Watchtower stream bridging to SSE/WebSocket; replacement service authorization and key delivery remain outside this CIP.
- Payment gating via x402 or other protocols.

---

## 2. Motivation

Cowboy actors are autonomous programs with persistent state, message handlers, timers, LLM inference, and off-chain compute. HTTP ingress allows browsers and ordinary API clients to reach actors without constructing blockchain transactions themselves.

Existing blockchain naming systems (ENS, Handshake, Unstoppable Domains) resolve names to passive addresses or content hashes. They do not route traffic to running programs. Cowboy actors are fundamentally different — they are **active endpoints** that can handle requests, not just receive tokens. Making them DNS-addressable creates a new class of internet service:

1. **AI agents with web presence**: An LLM-powered actor with its own API and identity — no hosting provider, no cloud account.
2. **Verifiable APIs**: Anyone can audit the code behind a DNS-addressable actor because the actor code is on-chain and deterministic.
3. **Self-sovereign web services**: The actor *is* the server. It persists without infrastructure management, scales through the protocol, and bills natively.
4. **Autonomous economic agents**: An actor can earn revenue from its API, use that to fund its own compute, and operate indefinitely without human intervention.

---

## 3. Design Goals

- Introduce HTTP ingress without changing actor execution semantics.
- Respect the existing async actor messaging model — no synchronous assumptions.
- Use the read-only handler RPC for fast, read-only requests.
- Use standard `ActorMessage` transactions for state-mutating requests.
- Define Gateways as a first-class ingress role, separate from Runners and Relay Nodes.
- Model the entitlement using the canonical `EntitlementGrant` format (dotted string ID with params).
- Keep the scope tight: ingress routing only. Asset hosting, stream bridging, and payment are specified separately. Protocol-managed custom domains are outside scope.

## 4. Non-goals

- Protocol-managed custom-domain binding and first-party TLD registration.

- Replacing native actor-to-actor messaging. HTTP is the **external** ingress protocol. Internal actor composition MUST use `send_message`.
- Synchronous off-chain compute within HTTP handlers. LLM inference and HTTP egress remain asynchronous (CIP-2 `submit_job` + callback).
- Public static asset hosting (CIP-15).
- Real-time stream bridging (CIP-17).
- Payment gating (CIP-18).
- Custom TLDs or alternative DNS roots.

---

## 5. Definitions

- **Gateway**: A network node that terminates TLS, resolves actor names, and bridges HTTP requests to the actor message protocol. Gateways are a dedicated ingress role, distinct from Runners, Validators, and Relay Nodes.
- **Route Registry**: A system actor that maintains the authoritative mapping from domain names to actor addresses.
- **Query path**: Read-only request execution via the read-only handler RPC — the actor handler runs against committed state without creating a transaction or requiring consensus.
- **Command path**: State-mutating request execution via a standard `ActorMessage` transaction that goes through consensus.

---

## 6. The `ingress.http` Entitlement

### 6.1 Registry Entry

```rust
RegistryEntry {
    id: "ingress.http",
    inheritable: false,
    attested: false,
    quota: false,
    params: &[
        ParamSchema { name: "allowlist_methods",     param_type: ParamType::StrArray, required: false },
        ParamSchema { name: "max_request_bytes",     param_type: ParamType::Uint,     required: false },
        ParamSchema { name: "max_response_bytes",    param_type: ParamType::Uint,     required: false },
        ParamSchema { name: "max_query_cycles",      param_type: ParamType::Uint,     required: false },
        ParamSchema { name: "receipt_ttl_blocks",    param_type: ParamType::Uint,     required: false },
    ],
},
```

The entry is non-inheritable and unattested. `quota: false` means the `max_*` parameters are per-request limits, not a cumulative quota. The registry MUST remain lexicographically sorted and its entry-count assertion MUST match the entries. Current code differs on the quota flag; see §14.

### 6.2 Parameters

All five parameters are optional. Omitted values use the defaults below; `allowlist_methods = ["*"]` permits all methods.

| Param | Default | Hard ceiling |
|---|---|---|
| `allowlist_methods` | `["GET", "HEAD", "POST"]` | none |
| `max_request_bytes` | `1_048_576` | `PROTOCOL_MAX_REQUEST_BYTES = 10_485_760` |
| `max_response_bytes` | `1_048_576` | `PROTOCOL_MAX_RESPONSE_BYTES = 10_485_760` |
| `max_query_cycles` | `10_000_000` | `PROTOCOL_MAX_QUERY_CYCLES = 100_000_000` |
| `receipt_ttl_blocks` | `RECEIPT_TTL_BLOCKS = 3_600` | `RECEIPT_TTL_MAX = 86_400` |

The Gateway MUST enforce `min(actor_param, hard_ceiling)`. The deploy-time validator (`node/types/src/manifest_validate.rs`) MUST reject unknown methods, zero-value caps, and any param exceeding its hard ceiling.

### 6.3 Example Actor Manifest

```json
{
  "entitlements": [
    {"id": "ingress.http", "params": {
      "allowlist_methods": ["GET", "HEAD", "POST"],
      "max_request_bytes": 1048576,
      "max_response_bytes": 1048576,
      "max_query_cycles": 10000000
    }},
    {"id": "storage.kv", "params": {"max_bytes": 10485760}},
    {"id": "econ.hold_balance"},
    {"id": "econ.transfer"}
  ]
}
```

### 6.4 Enforcement

- **Deploy-time** (`manifest_validate.rs`): param shape and bounds check.
- **Gateway**: rejects requests to actors lacking `ingress.http`; enforces request/response size; sets the actor cycle limit on read-handler calls and command dispatch. Command execution also remains subject to the transaction gas limits; the dispatch encoding for the actor limit needs definition (§14).

---

## 7. Route Registry

### 7.1 System Actor

`ROUTE_REGISTRY=0x0E` stores the authoritative name-to-actor mapping.

```
RouteRegistration {
  name:             string,                 // canonical lowercase, ≤ 64 bytes
  fqdn:             string,                 // computed: name || ".cowboy.network"
  actor_address:    Address,
  owner:            Address,
  registered_at:    BlockHeight,
  expires_at:       BlockHeight,
  subdomain_policy: u8,                     // 0 = OWNER_ONLY (default), 1 = ACTOR_MANAGED, 2 = OPEN
}
```

Storage layout: keyed by canonical `name`; reverse index `actor_address → [name]` maintained on write.

### 7.2 Naming Hierarchy

Actors register names under `cowboy.network`:

```
<name>.cowboy.network                     → top-level actor name
<sub>.<name>.cowboy.network               → subdomain of the actor
<deep>.<sub>.<name>.cowboy.network        → nested subdomain
```

**Subdomain ownership**: When an actor registers `myagent`, it owns the entire subtree `*.myagent.cowboy.network`. Subdomain resolution depends on the `subdomain_policy`:

- `OWNER_ONLY` (0, default): Only the registration owner can add subdomain records that map to other actors.
- `ACTOR_MANAGED` (1): The actor handles all subdomain routing internally via its `http.request` handler. The full `Host` header is passed to the actor.
- `OPEN` (2): Any actor with `ingress.http` can register subdomains under this name (community namespaces).

Defaulting to `OWNER_ONLY` prevents arbitrary subdomain crawls from consuming the parent actor's read-execution budget. An owner can opt into `ACTOR_MANAGED` when the parent is intended to serve the subtree.

### 7.3 Name Constraints

- Names are lowercase alphanumeric with hyphens: `[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]`.
- Minimum 3 characters, maximum 64 characters.
- Names MUST NOT start or end with a hyphen.
- Reserved names (`www`, `api`, `dns`, `gateway`, `relay`, `node`, `cowboy`, `system`, `admin`) are held by governance.

### 7.4 Registration

```python
# Actor or owner calls the Route Registry system actor
send_message(ROUTE_REGISTRY, "register", {
    "name": "myagent",
    "actor_address": self_address(),
    "duration_blocks": 31_536_000,  # ~1 year at 1 block/sec
})
```

**Requirements:**

1. Caller is the actor or its deployer account, or holds a delegation authorizing `Action::ActorExecuteHandler(b"register")`.
2. Target actor has the `ingress.http` entitlement.
3. Name is not already registered (or has expired past grace + auction).
4. Registration fee is paid in `CBY`.

### 7.5 Registration Economics

Registration uses a **fee schedule** based on name length to discourage squatting:

| Name Length | Annual Fee (CBY) |
|:-----------:|:----------------:|
| 3 characters | `PREMIUM_3_FEE` (governance-set, high) |
| 4 characters | `PREMIUM_4_FEE` |
| 5 characters | `PREMIUM_5_FEE` |
| 6+ characters | `BASE_FEE` |

Registration fees scale by the registration period:

```
registration_fee = annual_fee * duration_blocks / BLOCKS_PER_YEAR
```

The fee split is **not** computed locally in CIP-14; it is read from `system:registry_settlement_config` at `GOVERNANCE_SYSTEM_ACTOR=0x09` (§9.7). The required default allocation is:

```
RegistrySettlementConfig {
  burn_percent:     7000,   // 70%
  treasury_percent: 1500,   // 15%
  gateway_percent:  1500,   // 15%  → flows into the gateway pool (§9.4)
}
```

The allocation values are basis points and MUST sum to 10,000. Governance updates use `UpdateSettlementConfig` with `target_pool: REGISTRY` (§9.7). The current node type and execution path do not implement this three-way allocation; see §14.

### 7.6 Renewal and Expiry

- Names can be renewed at any time by paying the fee for an additional period.
- Renewal extends `expires_at` from the current expiry (not from the current block), preventing gaps.
- After expiry, a **grace period** of `NAME_GRACE_PERIOD` blocks (default: 2,592,000, ~30 days) allows the owner to renew at the standard rate.
- After the grace period, the name enters a **release auction** — a descending-price Dutch auction starting at `10x` the annual fee and declining linearly to `1x` over `NAME_AUCTION_DURATION` blocks (default: 604,800, ~7 days). This prevents sniping at the exact expiry block.

### 7.7 Route Registry API

The Route Registry system actor exposes the following message handlers:

| Method | Args | Returns | Description |
|--------|------|---------|-------------|
| `register` | `name`, `actor_address`, `duration_blocks` | `RouteRegistration` | Register a new name |
| `renew` | `name`, `duration_blocks` | `RouteRegistration` | Extend an existing registration |
| `transfer` | `name`, `new_owner` | `RouteRegistration` | Transfer ownership |
| `set_actor` | `name`, `actor_address` | `RouteRegistration` | Point name to a different actor |
| `set_subdomain_policy` | `name`, `policy` | `RouteRegistration` | Change subdomain policy |
| `resolve` | `name` | `Address \| null` | Resolve name to actor address |
| `lookup` | `actor_address` | `[string]` | Reverse lookup: actor → names |

`resolve` and `lookup` MUST support read-only invocation against the canonical registry state, without requiring a transaction.

### 7.8 Actor Updates

A registration owner can point a name at another actor with `set_actor` (§7.7). Two actor-level patterns also support application updates:

- **Router actor:** a stable proxy stores the current implementation address. Its owner updates that address through an authenticated handler. Read-only requests must be served from the proxy's own state, because `call_actor` and `send_message` are trapped on the read path. Command forwarding requires the backend to authenticate the router explicitly (§8.4.2).
- **`upgrade_self`:** an actor holding `sys.upgrade` can replace its `code_hash` while retaining its address and storage. Storage-schema and ABI compatibility remain the actor author's responsibility.

Choose one actor-level update pattern per application actor. This CIP does not specify how to combine proxy routing and in-place code replacement.

---

## 8. Request Execution

### 8.1 Canonical HTTP Request Envelope

When a Gateway receives an HTTP request for a registered actor, it translates it into a canonical message envelope:

```
HttpRequestEnvelope {
  method:       string,           // HTTP method (uppercase): GET, POST, etc.
  path:         string,           // URL path (no query string, no fragment)
  query:        map<string, array<string>>, // parsed query parameters (repeated keys → array)
  headers:      map<string, array<string>>, // HTTP headers (lowercase keys; repeated headers → array)
  body:         bytes | null,     // request body (POST/PUT/PATCH only)
  host:         string,           // full Host header (for subdomain routing)
  request_id:   string,           // unique request ID (UUID v4, gateway-generated)
}
```

### 8.2 Canonical HTTP Response Envelope

Actors return responses in a canonical envelope:

```
HttpResponseEnvelope {
  status:       u16,              // HTTP status code (100-599)
  headers:      map<string, array<string>>, // response headers (repeated headers → array)
  body:         bytes | null,     // response body
}
```

#### 8.2.1 Command Responses and Fallbacks

On the command path the Gateway submits the handler invocation as a transaction (an `ExecuteActor` submit-and-wait against a validator) and acts on the validator's result. When the validator reports a **succeeded** result whose handler output is **absent or empty** — no `result`, or a `result` that decodes to zero bytes — the Gateway MUST NOT treat the missing envelope as an invalid response. It MUST instead answer with the route's declared `fallback_response` (returned unmodified) or, if the route declares none, with **`202 Accepted`, empty headers, and an empty body**. A succeeded result whose output is **non-empty but does not decode** to an `HttpResponseEnvelope` (above) is still an invalid envelope and MUST yield `502 Bad Gateway`. A succeeded result that carries a decodable envelope always takes precedence over `fallback_response`.

An empty succeeded response is the **normal shape** for a route whose handler suspends to a runner in its initial segment (a `@runner.continuation` handler): the initial segment returns no value, so the PVM emits no output and the succeeded result carries no `result`. The work continues asynchronously on the runner and the client retrieves the eventual result by polling — so acknowledging the accepted request with `202`/the route fallback, rather than `502`, is correct. This follows the `PENDING ⇒ 202 Accepted` convention of the receipt read API (§8.8.3) and the asynchronous-handler pattern (§8.8.5).

**Read/query responses.** This carve-out is command-path only. On the read path — `GET`/`HEAD` served through the read handler (the Gateway's `/actor/read` dispatch; §8.3.1) — an absent or empty result is still an invalid envelope and MUST yield `502 Bad Gateway`. The `X-Cowboy-Error: INVALID_RESPONSE` mapping for that case is the §8.3.4 specification; the pinned gateway returns the `502` (`actor invoke failed`) without that header — an implementation gap. Continuations belong on the command path.

**Route `fallback_response`.** A Gateway route MAY declare a `fallback_response` — a canned `{status, headers, body}` the Gateway returns unmodified. It is the declared answer for three triggers, each with a default when the field is unset:
- `SubmitNoWait` route — always (the Gateway does not wait for the handler): default `202 Accepted`, empty headers/body.
- `Submit` route, validator reports `timed_out`: default `504 Gateway Timeout`.
- `Submit` route, succeeded result with missing/empty output (this clause): default `202 Accepted`, empty headers/body.

*Settlement (paid routes).* The former settle-after-success/D1 fire-and-forget rule is superseded. Confirm the selected payment before dispatching the initial paid operation, following [CIP-18 §15.3](./cip-18-payments#153-commands-and-continuations). Associate the continuation with the same purchase and execution identity. A `202 Accepted` or successful fallback response acknowledges the initial segment; it does not prove continuation completion. An interrupted or failed continuation follows the purchase recovery/refund rule without charging the client again. Unknown execution MUST NOT be automatically redispatched or refunded. Paid WebSocket/101 upgrades and resumable paid SSE are unsupported in this release and MUST be rejected before settlement and backend connection; see [CIP-18 §6.2](./cip-18-payments#62-superseded-ordering-and-unsupported-transports).

### 8.3 Read-Only Execution

Gateway flow for `GET` / `HEAD`:

1. Resolve `fqdn → actor_address` via `STATE_GET` against `ROUTE_REGISTRY`.
2. Call `POST /actor/read` with `actor = actor_address`, `handler = "http.request"`, and the base64-encoded, serialized `HttpRequestEnvelope` as `payload`. Set `max_cycles` to the minimum of the actor's `max_query_cycles`, the ingress ceiling, and the node's per-call ceiling.
3. Node executes via PVM read-only mode. Trapped syscalls — `state_set`, `state_delete`, `send_message`, `call_actor`, `schedule_timer*`, `cancel_timer`, `submit_job`, `token_transfer*`, `create_deferred_tx`, `upgrade_self`, `emit_event`, `randomness` — are listed in §8.3.3.
4. Response: deserialize as `HttpResponseEnvelope`. If invalid, Gateway returns `502 Bad Gateway` with `X-Cowboy-Error: INVALID_RESPONSE`.
5. Check the returned `block_height` against `X-Cowboy-Min-Block`, if present (§8.3.5). Only then return the handler response with `X-Cowboy-Block: <block_height>`.

#### 8.3.1 RPC

```
POST /actor/read
{
  "actor":        string,     // hex-encoded actor address
  "handler":      string,     // e.g. "http.request"
  "payload":      base64,     // serialized HttpRequestEnvelope
  "max_cycles":   u64?,       // per-call cycle budget
  "max_cells":    u64?,       // per-call cell budget
  "max_accesses": u64?,       // per-call committed-store access budget
  "block_height": null        // latest committed state
}
→ {
  "result":        base64,    // serialized HttpResponseEnvelope
  "cycles_used":   u64,
  "cells_used":    u64,
  "accesses_used": u64,
  "block_height":  u64,
  "state_root":    string     // hex-encoded committed state root
}
```

The request and response follow `ActorReadRequest` and `ActorReadResponse` in `node/rpc/src/responses.rs`. The RPC also accepts mutually exclusive `payload_text` or `payload_json` alternatives; Gateway ingress uses `payload` to pass the serialized envelope bytes directly.

Budgets MUST be positive and within the node's per-call ceilings. The node bounds all three resources even when the optional limits are omitted. Gateway ingress MUST propagate the effective actor cycle cap, rather than relying on a fixed local default; operator-selected cell/access budgets cannot exceed node ceilings. `block_height` is omitted or null: this CIP requires latest-state execution with a minimum-height response check, not historical pinned reads. The node currently rejects a non-null `block_height`.

#### 8.3.2 Host Mode

Handler execution MUST run with a read-only host context. In this mode the host:

- Returns from `state_get` / `state_scan_prefix` from the committed snapshot.
- Traps on every mutating syscall — see §8.3.3 for the exhaustive table.
- Uses a synthetic zero sender and transaction hash, as in the node read RPC; there is no authenticated transaction caller. Read authorization is established by the runtime read-only context, not by an HTTP method or other envelope field.

The read-only flag MUST govern the host boundary, including every path that can mutate state or dispatch work. New syscalls default to trapped unless explicitly admitted as pure reads.

#### 8.3.3 Syscalls

| Syscall | Read-only | Notes |
|---|---|---|
| `state_get` | ✅ permitted | committed state read |
| `state_scan_prefix` | ✅ permitted | committed state read |
| `state_set` | ❌ trapped | mutates own KV |
| `state_delete` | ❌ trapped | mutates own KV |
| `send_message` | ❌ trapped | mutates target mailbox |
| `call_actor` | ❌ trapped | synchronous cross-actor call |
| `schedule_timer` | ❌ trapped | mutates timer queue |
| `schedule_timer_ex` | ❌ trapped | mutates timer queue |
| `extend_timer` | ❌ trapped | mutates timer queue |
| `cancel_timer` | ❌ trapped | mutates timer queue |
| `submit_job` | ❌ trapped | dispatches off-chain task |
| `token_transfer` | ❌ trapped | mutates token balances |
| `token_transfer_from` | ❌ trapped | mutates token balances |
| `create_deferred_tx` | ❌ trapped | mutates deferred tx pool |
| `upgrade_self` | ❌ trapped | replaces actor code |
| `emit_event` | ❌ trapped | appends to event log |
| `randomness` | ❌ trapped | host RNG is consensus-derived; no consensus context on read path |

Ambient context syscalls (`block_height`, `block_timestamp`, `self_address`) are permitted; they read fields from `HostContext` rather than calling the host trait.

The host traps forbidden calls with `HostError::Forbidden`; the node read RPC reports `ActorReadStateMutation`. The Gateway maps this condition to HTTP `500` with `X-Cowboy-Error: READ_ONLY_VIOLATION`.

#### 8.3.4 Errors

| Trap / condition | HTTP | `X-Cowboy-Error` |
|---|---|---|
| node `ActorReadStateMutation` | 500 | `READ_ONLY_VIOLATION` |
| node `ActorReadLimitsExceeded` | 422 | `QUERY_CYCLE_LIMIT` |
| handler panic / unhandled exception | 500 | `HANDLER_PANIC` |
| return value not a valid envelope | 502 | `INVALID_RESPONSE` |
| returned `block_height` < requested `X-Cowboy-Min-Block` | 503 | `MIN_BLOCK_NOT_REACHED` |

#### 8.3.5 Consistency

`X-Cowboy-Block` reflects the `block_height` returned by the read RPC. Clients requiring a floor send `X-Cowboy-Min-Block: N`. The Gateway MUST check the returned execution height and suppress the handler response if it is below N, returning `503` with `X-Cowboy-Error: MIN_BLOCK_NOT_REACHED`. Checking the node's latest height separately is insufficient: the check must cover the state used for this read. Reads to different Gateways may reflect different heights; the floor does not require execution at an exact historical height.

#### 8.3.6 Determinism

Two Gateways executing the same `/actor/read` request against the same committed state root and block context MUST return byte-identical bodies. This holds because:

- Read-only PVM execution is pure (no `randomness`, no `emit_event`, no time/network/filesystem).
- `HostContext` ambient values (`block_height`, `block_timestamp`, `self_address`) are committed-state quantities.
- Storage reads return the committed state at the height observed.

The Gateway absorbs read-path compute cost; the actor is not charged because there is no transaction. Cycle counting follows CIP-3 PVM metering. The Gateway MUST apply the actor limit and the protocol ceiling; exceeding the budget aborts the handler.

### 8.4 Command Execution

#### 8.4.1 Dispatch

The required `IngressDispatch` system instruction carries:

```
IngressDispatch {
  target:        Address,
  envelope:      HttpRequestEnvelope,
  request_id:    bytes16,
}
```

The Gateway submits a signed transaction to `GATEWAY_REGISTRY.dispatch`. That handler verifies the operating account before emitting `IngressDispatch`; only `GATEWAY_REGISTRY=0x0F` may emit the instruction. The execution dispatcher MUST enforce this sender allowlist. Dispatch flow:

1. Verify `tx.sender` is a registered active Gateway via `GATEWAY_REGISTRY.is_active_gateway`.
2. Verify `target` declares `ingress.http`.
3. Verify envelope size ≤ `target`'s `max_request_bytes`.
4. Synthesise an internal `ActorMessage` to the target's `http.request` selector with `ctx.sender = GATEWAY_REGISTRY=0x0F`.
5. After the actor handler returns (or traps), write the result to `RECEIPT_REGISTRY` (§8.8).

#### 8.4.2 Sender Authentication

For transactional command invocation, actors implementing the direct-ingress `http.request` selector MUST verify `ctx.sender == GATEWAY_REGISTRY=0x0F` and reject other senders. Runtime-authenticated read-only invocation (§8.3.2) does not require a registry sender. Handlers MUST determine that distinction from trusted execution context, never from the attacker-controlled `envelope.method` or another request field.

The SDK (CIP-6) `@http.handler` decorator MUST include this check by default. Actors using the raw `@actor.handler("http.request")` form MUST include it manually.

The protocol derives `ctx.sender` from the authenticated execution context; actor code cannot choose another actor's sender address. The selector itself is not reserved by the message router. A backend receiving a forwarded request must authenticate its trusted router and distinguish that call from direct Gateway ingress. SDK handlers for direct ingress retain the registry-only default. Query-path calls have no transaction sender and are subject to the read-only contract (§8.3).

#### 8.4.3 Gas Payment

The Gateway operating account pays gas as ordinary `tx.from`. Stake (held in `GATEWAY_REGISTRY` per §9.2) is **not** drawn down for fees; it remains locked collateral. A slashing policy is not yet specified (§12.2).

For actor-funded ingress, the actor grants `Action::UseOwnerBalance` (`node/types/src/entitlement.rs`) to the Gateway operating account. The transaction-level fee-payer logic then debits the actor's account instead of the Gateway's.

#### 8.4.4 Response

Response selection follows §8.2.1: a valid handler envelope, the applicable route fallback, or the specified default. The receipt-backed asynchronous path acknowledges an accepted request without waiting for its eventual result; when that acknowledgement is `202`, the polling headers identify the pending receipt:

```
HTTP/1.1 202 Accepted
X-Cowboy-Request-Id: <uuid>
X-Cowboy-Block: <height>
```

For a receipt-backed acknowledgement, the client polls `/_cowboy/requests/{request_id}` (Gateway-intercepted reserved path; served from `RECEIPT_REGISTRY` — see §8.8.3). For paid routes, §8.2.1 requires confirmed payment before dispatch. An acknowledgement is tied to that purchase; pending settlement must remain pending and cannot be acknowledged as paid or retried as a fresh purchase.

### 8.5 Actor Handlers

The SDK `@http.handler` decorator MUST enforce the command-path sender check and recognize runtime-authenticated read-only invocation (§8.4.2). Custom handlers must enforce the same boundary. The HTTP method selects application behavior only after this execution-context check; it cannot authorize a command or bypass sender authentication.

```python
from cowboy_sdk import http
import json

@http.handler(methods=["GET", "POST"])
def handle(ctx, envelope):
    if envelope.method == "GET" and envelope.path == "/api/profile":
        return http.Response(200,
            headers={"content-type": ["application/json"]},
            body=json.dumps(ctx.storage.get("profile") or {}))

    if envelope.method == "POST" and envelope.path == "/api/profile":
        ctx.storage.set("profile", json.loads(envelope.body))
        return http.Response(200, body='{"updated": true}')

    return http.Response(404, body='not found')
```

Handlers MUST NOT assume synchronous off-chain compute; LLM / HTTP egress remain async via `submit_job` + callback (§8.8.5).

### 8.6 Reserved Paths (`/_cowboy/*`)

The path prefix `/_cowboy/` is reserved for protocol-level endpoints. Gateways MUST intercept these paths **before** dispatching to the actor's `http.request` handler. Actors MUST NOT define handlers that overlap with `/_cowboy/*`.

| Path | Method | Description |
|------|--------|-------------|
| `/_cowboy/requests/{request_id}` | GET | Poll for command-path result (§8.4). |
| `/_cowboy/health` | GET | Gateway health check (returns 200 if the Gateway is operational). |
| `/_cowboy/info` | GET | Actor metadata: address, entitlement params, block height. |

Future CIPs may define additional `/_cowboy/*` endpoints. Actors receiving a request with a `/_cowboy/` path prefix via the `http.request` handler indicates a Gateway implementation bug.

### 8.7 Subdomain Routing Limitations

When an actor uses `ACTOR_MANAGED` subdomain policy (§7.2), all subdomain traffic is routed to the parent actor's `http.request` handler with the full `Host` header. The actor handles subdomain dispatch internally.

**Query-path limitation:** On the query path, subdomain routing is purely internal to the actor handler — the Gateway resolves the top-level name and passes the full `Host` to the actor. This means subdomain delegation to *other* actors is only possible on the command path (where the actor can use `send_message` to forward the request). On the query path, the actor handler must serve all subdomains itself from its own storage.

### 8.8 Receipt Registry

`RECEIPT_REGISTRY=0x10` owns command-result storage and lifetime. Registry-wide pruning avoids consuming actor KV and a timer slot for each request; actors have a bounded timer budget.

#### 8.8.1 Record

```
Receipt {
  request_id:    bytes16,                   // UUID v4 from envelope
  target_actor:  Address,
  gateway:       Address,                   // the dispatching Gateway operating account
  status:        u8,                         // 0 = PENDING, 1 = COMPLETED, 2 = FAILED
  envelope:      HttpResponseEnvelope?,     // populated on completion
  created_at:    BlockHeight,
  expires_at:    BlockHeight,                // = created_at + receipt_ttl_blocks
  private:       bool,                       // see §8.8.4
}
```

#### 8.8.2 Storage and Lifecycle

- **Written by the system instruction dispatcher**, not by actor code. After `IngressDispatch` invokes the actor handler:
  - Successful return → `complete_receipt(request_id, envelope)` instruction (sender = current handler context, verified to equal `target_actor`).
  - Handler panic / cycle limit → dispatcher writes `status = FAILED` directly with no envelope.
- **TTL**: `receipt_ttl_blocks` is read from the actor's `ingress.http` entitlement (default `RECEIPT_TTL_BLOCKS = 3_600`, max `RECEIPT_TTL_MAX = 86_400`).
- **Pruning**: a single registry-wide pruning loop scans `expires_at` per block. Per-actor timer budget is **not** consumed.
- **Fee**: receipt storage charges are folded into the command-path transaction fee under CIP-3. The precise cell/access accounting must reflect the stored response size (§14.1); there is no separate billing event.

#### 8.8.3 Reads

`GET /_cowboy/requests/{request_id}` is a Gateway-intercepted reserved path (CIP-14 §8.6). Gateway reads via the read-handler RPC against `0x10`, selector `get_receipt`. Status mapping:

| Receipt state | HTTP response |
|---|---|
| `PENDING` | `202 Accepted` (no body) |
| `COMPLETED` | `200 OK` + stored envelope |
| `FAILED` | `500 Internal Server Error` + `X-Cowboy-Error: HANDLER_FAILED` |
| absent (expired or never created) | `410 Gone` if past `expires_at`; else `404 Not Found` |

#### 8.8.4 Privacy

Receipts are dispatching-Gateway-readable + target-actor-readable by default. To restrict reads (sensitive responses), the actor sets the `private: bool = true` flag in the `HttpResponseEnvelope`. The registry then refuses reads whose caller is not the original `gateway` field of the receipt — preventing other Gateways from polling the result. The authenticated read mechanism and response-envelope field encoding are unresolved (§14.1); this access rule alone does not establish response confidentiality.

#### 8.8.5 Asynchronous Responses

```python
from cowboy_sdk import actor, http, jobs
import json

@http.handler("POST", "/api/ask")
def handle_ask(ctx, envelope):
    question = json.loads(envelope.body)["question"]

    ctx.submit_job(
        job_type    = "llm",
        params      = {"prompt": question, "max_tokens": 256},
        callback    = "_on_llm",
        context     = envelope.request_id,        # opaque to scheduler
    )

    # 202 Accepted + Receipt PENDING; client polls /_cowboy/requests/<id>
    return http.Response(202, body='{"status": "pending"}')

@actor.handler("_on_llm")
def on_llm(ctx, result):
    request_id = ctx.callback_context
    ctx.complete_receipt(request_id, http.Response(
        200,
        headers={"content-type": ["application/json"]},
        body=json.dumps({"answer": result["output"]}),
    ))
```

`complete_receipt` is a required SDK helper that emits the `complete_receipt` system call against `RECEIPT_REGISTRY=0x10`. The registry verifies `ctx.sender` equals the receipt's `target_actor`.

---

## 9. Gateway Specification

### 9.1 Gateway as a Distinct Ingress Role

A Gateway is a **dedicated ingress node** in the Cowboy network. It is a separate role from Runners (off-chain compute, CIP-2 and CIP-10), Validators (consensus), and Relay Nodes (storage, CIP-9).

Gateway routing is a separate responsibility from runner job execution and relay storage. Persistent workloads can receive traffic through the Gateway under CIP-15; this does not make runner scheduling or storage relays responsible for public HTTP ingress.

**Gateway responsibilities:**

- Participate in DNS resolution for `*.cowboy.network` (authoritative DNS or integration with external DNS).
- Terminate TLS (including certificate management via ACME).
- Use a node's committed state and read-only execution RPC; the node may be co-located or remote.
- Route HTTP requests: resolve names via the Route Registry, dispatch to query or command path.
- Submit command-path transactions to the mempool.
- Enforce entitlement parameters (`max_request_bytes`, `max_response_bytes`, `max_query_cycles`).
- Enforce rate limits.

**What Gateways do NOT do:**

- Store CIP-9 shards (that is the Relay Node role).
- Execute CIP-2 off-chain tasks (that is the Runner role).
- Participate in consensus (that is the Validator role).

A single physical node MAY operate as multiple roles simultaneously (e.g., Gateway + Relay Node + Validator), but the protocol treats each role independently.

### 9.2 Gateway Registry

The Gateway Registry is a system actor at reserved address `0x0F`. It manages Gateway registration, staking, and health.

```
GatewayProfile {
  address:          Address,
  stake_amount:     u128,           // CBY staked
  endpoint:         string,         // public endpoint (IP or hostname)
  http_port:        u16,            // port for HTTPS traffic
  last_heartbeat:   BlockHeight,
  health:           u16,            // decays per block, reset on heartbeat
  registered_at:    BlockHeight,
}
```

**Lifecycle:**

- **Register**: Gateway stakes `MIN_GATEWAY_STAKE` CBY and calls `register_gateway(endpoint, http_port)`.
- **Heartbeat**: Gateway calls `heartbeat()` periodically. Health resets to `MAX_GATEWAY_HEALTH` (default: 3,600, ~1 hour at 1 block/sec). Health decays by 1 per block.
- **Removal**: If health reaches 0, the Gateway is removed from the active list.
- **Unstake**: A Gateway may unstake after `GATEWAY_UNSTAKE_DELAY` blocks, provided it is no longer in the active list.

**Gateway Registry API:**

| Method | Args | Returns | Description |
|--------|------|---------|-------------|
| `register_gateway` | `endpoint`, `http_port` | `GatewayProfile` | Register a new Gateway (requires `MIN_GATEWAY_STAKE`). |
| `heartbeat` | *(none)* | `void` | Reset health to `MAX_GATEWAY_HEALTH`. |
| `unstake` | *(none)* | `void` | Begin unstaking (after `GATEWAY_UNSTAKE_DELAY`). |
| `dispatch` | `target: Address`, `envelope: HttpRequestEnvelope` | `void` | Forward an HTTP request to a target actor. Caller MUST be a registered, active Gateway. Reverts `ERR_UNAUTHORIZED_GATEWAY` otherwise. |
| `is_active_gateway` | `address: Address` | `bool` | Check if an address is a registered, active Gateway. |

### 9.3 Gateway Selection

When a client resolves `*.cowboy.network`, DNS returns the IP addresses of active Gateway nodes. Selection is handled at the DNS level:

- **Anycast**: All Gateways advertise the same IP prefix via BGP. Network routing selects the nearest Gateway.
- **Geo-DNS** (alternative): The authoritative DNS server returns Gateway IPs based on the client's resolver location.

Any Gateway can serve any actor. Gateways are stateless with respect to HTTP routing — they resolve names from the Route Registry and execute queries against their node's committed state.

### 9.4 Gateway Incentives

Read from `system:gateway_pool_config` at `0x09`:

```
GatewayPoolConfig {
  weight_stake:   u32,     // basis points; default 5000
  weight_uptime:  u32,     // basis points; default 5000
}
```

Each epoch, the pool (funded by §7.5 `gateway_percent` slice of registration / renewal fees) is distributed to active Gateways pro-rata to:

```
gateway_share = pool * (weight_stake * stake + weight_uptime * uptime_blocks) / Σ(...)
```

Updated via `UpdateSettlementConfig{target_pool=GATEWAY_POOL}`.

**Incentive limitation:** revenue is uncoupled from request volume. An active Gateway that serves no requests can still earn. Request metering and per-request remuneration are outside this pool model.

### 9.5 Rate Limiting

Gateways enforce rate limits to prevent abuse:

| Limit | Default | Scope |
|-------|---------|-------|
| `MAX_REQUESTS_PER_SECOND` | 100 | Per actor per Gateway |
| `MAX_CONCURRENT_CONNECTIONS` | 1,000 | Per actor per Gateway |
| `MAX_QUERY_CYCLES` | Per actor entitlement | Per request |
| `MAX_REQUEST_BYTES` | Per actor entitlement | Per request body |
| `MAX_RESPONSE_BYTES` | Per actor entitlement | Per response body |

Actors declare their own limits in their `ingress.http` entitlement params. Gateways enforce the **minimum** of the actor's declared limit and the protocol-wide maximum.

### 9.6 Gateway Authentication

Command-path ingress is **system-mediated**: Gateways call `GatewayRegistry.dispatch()` (§8.4), which verifies the caller is a registered, active Gateway before forwarding the request to the target actor. The actor receives the message with `ctx.sender == GATEWAY_REGISTRY_ADDRESS` (`0x0F`), providing a protocol-level authenticity guarantee without requiring actors to maintain their own Gateway allowlists.

```python
# On the command path, ctx.sender is always the GatewayRegistry system actor
assert ctx.sender == GATEWAY_REGISTRY_ADDRESS  # 0x0F
```

Direct-ingress handlers MUST reject transactional `http.request` messages where `ctx.sender != GATEWAY_REGISTRY_ADDRESS`. Runtime-authenticated read-only calls follow §8.3 instead; the request's HTTP method does not select the trust boundary.

On the query path, there is no on-chain transaction, so no Gateway signature. The trust model for query-path responses is equivalent to any RPC node — the client trusts the Gateway to faithfully execute the query. Clients requiring stronger guarantees can run their own node and query directly.

### 9.7 Settlement Configuration

CIP-3 specifies burn / treasury / runner-tip splits through `SettlementConfig` stored at `GOVERNANCE_SYSTEM_ACTOR=0x09` under key `system:settlement_config`, updatable via `UpdateSettlementConfig` (opcode 40, sender must be `0x09` per `node/execution/src/execution/system_instruction.rs`).

This CIP requires **two fee configurations** under the same governance actor (no new system actor required):

- `system:registry_settlement_config` — splits for name registration / renewal fees (§7.5)
- `system:gateway_pool_config` — splits for the Gateway serving fee pool (§9.4)

Both MUST be updated via the `UpdateSettlementConfig` opcode (40) with a `target_pool` discriminant. This avoids forking burn/treasury routing across multiple ad-hoc paths.

**`target_pool` discriminant (canonical enumeration).** Settlement configuration updates share this enum. Implementations MUST exhaustively switch on this value and reject unknown variants with `ERR_UNKNOWN_POOL`:

| Value | Pool name | Source CIP | Storage key |
|---:|---|---|---|
| 0 | `MAIN` | CIP-3 (existing) | `system:settlement_config` |
| 1 | `REGISTRY` | CIP-14 §7.5 | `system:registry_settlement_config` |
| 2 | `GATEWAY_POOL` | CIP-14 §9.4 | `system:gateway_pool_config` |
| 3 | `CONTAINER` | CIP-10 | `system:container_settlement_config` |
| 6+ | (reserved for future CIPs) | — | — |

Adding a new pool variant requires a CIP that explicitly extends this table. Handlers receiving an `UpdateSettlementConfig` with an unrecognized `target_pool` MUST reject the transaction; this is a soft form of governance (the variant must exist in code before it can be set).

This table defines the required pool-selection contract, not current wire support. The gateway and registration allocation must be implemented together and reviewed for conservation of fees; see §14.

---

## 10. Constants

```
ROUTE_REGISTRY_ADDRESS              = 0x0E
GATEWAY_REGISTRY_ADDRESS            = 0x0F
RECEIPT_REGISTRY_ADDRESS            = 0x10

MIN_NAME_LENGTH                     = 3
MAX_NAME_LENGTH                     = 64
NAME_GRACE_PERIOD                   = 2_592_000           // ~30 days
NAME_AUCTION_DURATION               = 604_800             // ~7 days
BLOCKS_PER_YEAR                     = 31_536_000          // ~1 year

MIN_GATEWAY_STAKE                   = <governance-set>
MAX_REQUESTS_PER_SECOND             = 100                 // per actor per Gateway
MAX_CONCURRENT_CONNECTIONS          = 1_000               // per actor per Gateway
MAX_GATEWAY_HEALTH                  = 3_600               // ~1 hour
GATEWAY_UNSTAKE_DELAY               = 604_800             // ~7 days

RECEIPT_TTL_BLOCKS                  = 3_600               // ~1 hour default
RECEIPT_TTL_MAX                     = 86_400              // ~1 day ceiling

PROTOCOL_MAX_REQUEST_BYTES          = 10_485_760          // 10 MiB
PROTOCOL_MAX_RESPONSE_BYTES         = 10_485_760          // 10 MiB
PROTOCOL_MAX_QUERY_CYCLES           = 100_000_000         // 100M cycles
```

---

## 11. Rationale

- **DNS subdomains:** names under `cowboy.network` work with ordinary browsers and resolvers. Protocol-managed custom domains and alternative DNS roots are outside this CIP.
- **Read and command separation:** reads avoid consensus while commands retain transaction ordering and gas accounting. Read responses can reflect different committed heights; clients can require a minimum height (§8.3.5).
- **Read-only host enforcement:** side effects trap immediately instead of appearing to succeed and being discarded. Randomness is excluded so the same request and committed context produce deterministic results.
- **Independent Gateway role:** TLS, DNS, HTTP routing, and rate limiting have different operational responsibilities from consensus, runner scheduling, and shard storage. Roles may share a physical machine.
- **Manifest grants:** `ingress.http` uses the entitlement registry and supported parameter shapes rather than a separate permission system.
- **Registry-owned receipts:** result retention and pruning do not consume each actor's timer budget.

---

## 12. Security Considerations

### 12.1 Query Path Isolation

Query-path execution MUST be fully isolated. A malicious actor handler MUST NOT be able to:

- **Modify state**: Side-effecting syscalls trap at the read-only host boundary (§8.3.3).
- **Affect other actors**: Execution is sandboxed per CIP-3 PVM guarantees.
- **Consume unbounded resources**: `max_query_cycles` is enforced per-request.
- **Exfiltrate data via side channels**: The PVM is deterministic (no network, no timing, fixed hash seed).

### 12.2 Gateway Misbehavior

Gateways could serve stale state, fabricate responses, or drop requests. Mitigations:

- **Stale state detection**: `X-Cowboy-Block` response header and `X-Cowboy-Min-Block` request header allow clients to detect and reject stale responses.
- **Response verification**: For high-value queries, clients can re-execute the actor handler against their own node's committed state and compare results (deterministic PVM ensures identical output for the same state root).
- **Stake accountability:** a slashing policy must define provable misconduct before stake can be treated as an economic enforcement mechanism.

**Unresolved requirement:** Gateway slashing needs an evidence format, an adjudication authority, a slash amount, and a destination for slashed funds. None is specified by this CIP. Locked stake alone does not prove that a Gateway served requests or establish that a request was withheld. This contract must be resolved before relying on stake to secure economically meaningful ingress.

### 12.3 Name Squatting

The tiered pricing model (§7.5) and Dutch auction release mechanism (§7.6) make squatting economically unfavorable:

- Short, premium names have high annual fees.
- Ongoing fees prevent indefinite warehousing.
- The Dutch auction on expiry prevents sniping.

### 12.4 DoS via Command Path

An attacker could flood an actor with command-path requests. Mitigations:

- **Rate limiting**: Gateways enforce per-actor request limits (§9.5).
- **Fee markets**: High load increases the basefee (CIP-3 EIP-1559 mechanism), making spam progressively more expensive.
- **Future: x402 pricing**: A follow-on CIP can add per-request payment requirements for command-path endpoints.

### 12.5 Ingress Authenticity

On the command path, ingress is **system-mediated** through `GatewayRegistry.dispatch()` (§8.4). The GatewayRegistry system actor verifies that the caller is a registered, active Gateway before forwarding the request. Actors receive `ctx.sender == GATEWAY_REGISTRY_ADDRESS` (`0x0F`), providing a protocol-level guarantee that the request originated from a verified Gateway.

**Residual risk:** An actor that does not check `ctx.sender == GATEWAY_REGISTRY_ADDRESS` would process any `ActorMessage` with `method: "http.request"`, including those sent by arbitrary accounts. The SDK (CIP-6) MUST include the sender check by default in its HTTP handler decorator, so that actors using the SDK are protected without explicit validation.

On the query path, the runtime supplies a synthetic zero sender with no authenticated caller. The handler runs in a read-only sandbox. The SDK must recognize that runtime context explicitly; caller-controlled envelope fields cannot opt a transaction into it.

### 12.6 Cross-Gateway Rate Limit Bypass

Per-actor rate limits (§9.5) are enforced **per Gateway**. An attacker routing requests through N different Gateways can achieve N× the intended rate limit. Mitigations:

- The command path naturally rate-limits via fee markets (CIP-3 EIP-1559 basefee increases under load).
- The query path is the exposure surface. Gateways are stateless with respect to each other, so coordinated rate limiting requires an off-chain protocol (not specified here).
- For high-value actors, the `max_query_cycles` entitlement parameter provides a per-request computation cap that applies regardless of how many Gateways are involved.
- Future CIPs may introduce on-chain rate limiting (e.g., per-actor query budgets that decay per-block).

### 12.7 TLS and Trust

Gateways terminate TLS. The client trusts the Gateway to faithfully relay the actor's response — the same trust model as CDNs (Cloudflare, Fastly). Future CIPs may define response-signing mechanisms for end-to-end verification.

---

## 13. Related Serving Specifications

| Feature | Governing specification |
|---|---|
| Static assets, route manifests, dynamic CORS, and registered workloads | CIP-15 |
| CBFS public volumes and relay storage | CIP-9 |
| SSE/WebSocket stream delivery and client decryption | CIP-7 replacement integration pending; old wrapped-key retrieval is retired |
| Payment gating | CIP-18 |

Operator-configured hostnames are covered by CIP-15 §15 and carry no on-chain domain-control attestation. These serving features do not require protocol-managed custom domains.

### 13.1 Static-Serving Dependencies

CIP-9 supplies `StorageCommitment` records, manifest commitments, deterministic volume identifiers, `Visibility::Public`, and the `ACTIVE → GRACE_PERIOD → DELETED → GARBAGE_COLLECTING` lifecycle. Static serving also requires:

- Direct manifest retrieval through `GET_MANIFEST`.
- `ManifestCommitted` events for eager cache invalidation, with periodic polling as a consistency floor.
- Canonical manifest serialization and Merkle computation from CBFS.
- Serving decisions tied to the CIP-9 volume status.

CIP-9 and CIP-15 define these storage and serving contracts. An implementation using shard reconstruction or time-based polling must state its latency and freshness limitations; those mechanisms do not establish direct-fetch or event-invalidation coverage.

### 13.2 Entitlement Shapes

Ingress parameters must fit the canonical `ParamValue` shapes in `node/types/src/manifest.rs`. The `ingress.http` fields in §6 use `Uint` and `StrArray`; nested objects and arrays of structs are not valid parameter values. Supported string/array size limits must be enforced during manifest validation.

Static serving uses CIP-15's `ingress.http.static_volumes` and `max_static_response_bytes` parameters (CIP-15 §§3, 7, and 11.2). CIP-15 owns that schema and its per-volume cache limits. The node currently represents `static_volumes` as a `StrArray` of volume names; carrying and enforcing the per-volume `max_cache_bytes` requirement remains a CIP-15 schema/enforcement gap, not a requirement for a separate ingress entitlement.

---

## 14. Implementation Requirements

The source locations below identify implementation ownership. They do not establish that every requirement in this CIP is complete.

| Area | Current source evidence | Remaining contract |
|---|---|---|
| Entitlements | `node/types/src/registry.rs` declares `ingress.http`; `node/types/src/manifest_validate.rs` validates its parameters. | The registry has `quota: true`, while §6 requires `false`. Request/response byte caps are validated as positive without the §6.2 hard ceilings. Reconcile these differences and cover deployment and Gateway enforcement. |
| Read execution | `node/rpc/src/handlers/actor.rs` implements `POST /actor/read`; `node/execution/src/pvm_host.rs` enforces a read-only context. `gateway/crates/gateway-actor/src/lib.rs` calls that endpoint. | Verify all allowed/trapped syscalls, positive bounded budgets, runtime read-context handling, and Gateway propagation of the effective actor cycle limit. The Gateway must enforce `X-Cowboy-Min-Block` against the returned execution height and map node errors to the HTTP contract in §8.3.4; using the RPC alone does not prove those Gateway checks. |
| System actors | `node/runner/src/system_actors.rs` allocates `0x0E`, `0x0F`, and `0x10`. | Reserved addresses alone do not implement the Route Registry, Gateway Registry, or HTTP Receipt Registry. Implement the records, authorization, registration/renewal/expiry, heartbeat/unstaking, dispatch, completion, and pruning required by §§7–9. |
| Commands | `gateway/crates/gateway-actor/src/lib.rs` submits actor transactions; the system-instruction dispatcher lives in `node/execution/src/execution/system_instruction.rs`. | Verify and implement the registry-mediated `IngressDispatch` and receipt-completion contract. Direct actor submission is not evidence of registry authentication or receipt support. Assign dispatch/completion opcodes through the canonical instruction registry and define command cycle-limit encoding before implementation; this CIP does not claim those wire instructions exist. |
| Settlement | `node/types/src/domain.rs` retains a two-way `RegistrySettlementConfig` type with treasury/burn fields; its documentation identifies it as unused. | Implement the §7.5 three-way allocation, §9.4 Gateway pool, and §9.7 governance configuration together. Do not treat the retained two-way type as fee-pool implementation. |
| Static serving | The node registry binds `static_volumes` and `max_static_response_bytes` under `ingress.http`, as specified by CIP-15. | CIP-15 owns the remaining per-volume `max_cache_bytes` schema and enforcement gap (§13.2). |

### 14.1 Receipt Contracts

The receipt requirements need the following details resolved before implementation can be considered complete:

- Define how an initial handler response of `202` leaves the receipt pending until an authenticated asynchronous callback completes it; ordinary successful responses can complete immediately.
- Bind request IDs to their target and dispatching Gateway, define duplicate/replay handling, and reject completion by any other actor.
- Define an authenticated read context for private receipts. The read-only RPC has no transaction sender, so comparing a nonexistent caller with the stored Gateway address does not enforce privacy. The `private` field also needs a canonical response-envelope representation.
- Define how expiry remains distinguishable from an ID that never existed after pruning, so `410` versus `404` does not depend on deleted data.
- Specify bounded pruning work and all cell/access charges for receipt writes and reads; a receipt can contain a large response envelope and cannot be assumed to cost one storage cell regardless of its size.

### 14.2 Completion Evidence

Acceptance coverage must exercise the Gateway-to-node path, not only isolated helpers: deployment validation, name ownership and expiry, read-only traps and budgets, minimum-height rejection, authenticated dispatch, actor-funded fee authorization, duplicate and private receipt handling, delayed completion, bounded pruning, and conservation of all fee shares. Gateway slashing also requires the policy in §12.2. Missing implementation remains required work; it is not waived by this document's Draft status.
