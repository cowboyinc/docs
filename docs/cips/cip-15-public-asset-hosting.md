---
title: "CIP-15: Public Asset Hosting and HTTP Routing"
icon: image
description: Verb-aware HTTP routing for actors with per-route payment policy, plus Gateway-side static asset serving from CBFS public volumes, caching, CORS, and Gateway fetch protocols for both routes and assets
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-03-07
  **Requires:** CIP-9 (Runner Attached Storage), CIP-14 (DNS-Addressable Actors); workload registry records for `Workload`-target routes only
</Note>

## 1. Abstract

This proposal defines **Public Asset Hosting and HTTP Routing** — a unified system for serving static files (HTML, CSS, JavaScript, images, fonts) from CBFS `Visibility::Public` volumes, dispatching dynamic HTTP requests to named actor handlers, proxying requests to off-chain runners, and relaying long-lived streaming connections (WebSocket / SSE) to registered workloads — all via the CIP-14 Gateway network. The core primitive is a **routes table** stored in the actor's KV state that declares, for each `(verb, path)` pair, whether the request is served from a CBFS volume, dispatched to a named actor handler, or proxied to an off-chain runner or workload, and whether the actor or the caller pays for the request. An actor publishes its `Workload` URL routes in `__cowboy/routes`; the Gateway does not need a per-actor configuration change (§8.15).

This CIP specifies:

- A unified route schema (verb, path, target, payment policy, priority, enabled) stored in actor KV at the reserved key `__cowboy/routes`.
- A verb-aware route resolution algorithm that determines per-request dispatch (volume mount, actor handler, or off-chain runner) and payment policy.
- A `Runner` target that proxies a route off-chain to a selected runner (authorized by a `runner_bindings` entitlement), with optional caller authentication and actor- or caller-owned CBFS mounts, for endpoints that cannot run in consensus — LLM inference, outbound API calls, streaming.
- A `Workload` target that relays an actor-declared route — including WebSocket upgrades and unbuffered SSE — to a single long-lived registered workload backend (§8.15), for streaming app endpoints: inference SSE, agent chat UIs, application websockets.
- A per-route payment policy (`pays = actor | caller`) with `actor` as the default. `caller` opts the route into payment gating per CIP-18.
- An extension to the `ingress.http` entitlement with `static_volumes` and `max_static_response_bytes` parameters.
- A Gateway-to-Relay-Node fetch protocol for retrieving, reconstructing, caching, and serving public-volume objects.
- A Gateway-to-Runner fetch protocol for reading the routes table from actor state with Merkle-proof verification.
- A CORS configuration schema (`_meta/cors.json`) with sensible defaults for static assets.
- Cache invalidation driven by on-chain state-root and `manifest_root` changes.
- Runtime mutability of routes via the actor's normal KV write path, with narrow typed SDK helpers (toggle, re-price, re-prioritize, re-target).

This CIP intentionally defers the following to future CIPs:

- Pre-compressed asset variants (`.gz`, `.br` files in the volume).
- Small object inlining (bypassing erasure coding for tiny files).
- Image optimization or resizing at the Gateway edge.
- Range requests and chunked transfer for streaming large assets.
- Caller-pays static-asset downloads (paid public-volume content).

---

## 2. Motivation

CIP-14 makes actors reachable over HTTP through a single `http.request` handler. Real applications, though, are heterogeneous: one actor serves static assets (bundled JavaScript, CSS, images, fonts), dynamic API endpoints, and — increasingly — work that belongs off-chain (LLM inference, outbound API calls, streaming). These have very different needs. A static file should come straight from storage. A state-changing call needs the actor's deterministic handler under consensus. A model call cannot run in the PVM at all.

A **routes table** lets the actor make that explicit: for each `(verb, path)`, it declares how the request is served — from a CBFS volume, by a named actor handler, or by an off-chain runner — and who pays. Each request then takes the cheapest correct path, and routing, asset serving, and payment logic move out of handler code into a declarative table that expresses them better.

Concretely, this gives:

1. **Static served straight from storage**: Static assets come directly from the Gateway's cache or are reconstructed from Relay Node shards — CDN-fast, with no actor handler involved.
2. **Consensus spent only where it's needed**: The actor's deterministic handler and `max_query_cycles` budget are reserved for requests that actually compute or change state; static and runner routes don't draw on them.
3. **CDN-like performance**: Gateways cache reconstructed objects locally, serve conditional requests via ETags, and set proper `Cache-Control` headers — all without actor involvement.
4. **Verb-aware dispatch to named handlers**: Routes target named actor handlers, not a single `http.request` catchall. `GET /post` and `POST /post` go to different methods. Path parameters (`/users/{id}`) bind to handler arguments. Actors no longer reimplement routing.
5. **Per-route payment fallback**: Each route declares `pays = actor | caller`. `actor` is the default and needs no caller wallet when no enabled CIP-18 `price_table` overrides it; `caller` selects 402/x402 gating under the same fallback. An enabled table's rule or `default_mode` is authoritative.
6. **Runtime mutability without redeploys**: Toggling a route, raising a price, or activating a maintenance redirect does not require shipping new assets. The routes table is small structured state, mutated via the actor's normal KV write path.
7. **Familiar developer ergonomics**: Routes are declared via decorators on handler functions in the actor's source code (FastAPI / axum / Hono style). The SDK lowers them to the canonical schema at deploy time. A developer who has shipped a Flask or Express app can ship a Cowboy actor in minutes.
8. **Off-chain compute as a route**: Endpoints that cannot run in the deterministic PVM — LLM inference, outbound API calls, streaming responses — are declared as `Runner` targets and proxied off-chain to a runner, rather than being forced through consensus or bolted on as out-of-band jobs. The same routing table decides, per `(verb, path)`, what runs in consensus, what serves static, and what runs off-chain.
9. **Streaming app backends as a route**: Long-lived connections — websockets for live apps, SSE for streamed inference and agent chat — terminate at a registered persistent workload behind the same URL space (`Workload` target, §8.15), instead of a sidecar server outside the platform. One domain serves static from a volume, consensus methods from the actor, and live streams from a workload the actor owns.

---

## 3. Design Goals

- Serve static assets without invoking any actor handler or consuming PVM cycles.
- Let actors declare routes per `(verb, path)`, targeting a CBFS volume mount, a named actor handler, or an off-chain runner, with explicit priority ordering.
- Let a route execute off-chain on a runner (`Runner` target) for work that cannot or should not run in consensus, with optional caller authentication and actor- or caller-owned CBFS mounts.
- Relay long-lived streaming connections (WebSocket, SSE) unbuffered to a registered persistent workload (`Workload` target), with explicit idle-timeout and connection-lifetime policy.
- Make `pays = actor` the fallback default so that browser GETs do not require a wallet absent an enabled CIP-18 `price_table`; let actors opt specific routes into `pays = caller` as the client-paid fallback for payment-gated endpoints.
- Support SPA (single-page application) fallback patterns (`index.html` for all non-file paths).
- Reuse CIP-9's existing `_meta/content_types.json` and `_meta/cache_config.json` for HTTP header generation.
- Provide CORS headers for static assets by default (browsers need them).
- Define the Gateway-to-Relay-Node fetch protocol for static assets and the Gateway-to-Runner fetch protocol for routes: retrieval, integrity verification, and caching.
- Extend the existing `ingress.http` entitlement — no new entitlement type.
- Allow runtime mutation of route metadata (priority, pays/price, enabled, target handler) via narrow typed SDK helpers, without redeploying assets or actor code.

## 4. Non-Goals

- Replacing actor handlers for dynamic requests. Volume and method targets coexist; the routes table controls which `(verb, path)` pair goes where.
- Server-side rendering. Actors that need SSR use a method target.
- Pre-compressed asset variants (`.gz`, `.br`). Compression is on-the-fly only.
- Image optimization, resizing, or transformation at the Gateway edge.
- Range requests (`Range` header) for partial content delivery.
- Verifiable or multi-operator runner serving. A `Runner`-target route is trusted to the selected runner in this CIP; attested / trustless serving and on-chain settlement of off-chain work are deferred (§13). The same applies to `Workload`-target routes: a workload-served route is exactly as trustworthy as its host (§8.15).
- Metered billing of streamed content. A `Workload` route can payment-gate connection *establishment* (`pays = caller`, CIP-18); usage-metered streaming settlement (per-token, per-message) is the streaming-payments protocol's scope, not this CIP's.

---

## 5. Definitions

- **Route**: An entry in the actor's routes table that maps a `(verb, path)` pattern to a `Target`, with a payment policy, priority, and enabled flag.
- **Verb**: An HTTP method (`GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`) or `ANY` (matches any verb at lowest specificity).
- **Target**: The destination of a route — a `Method` (named actor handler, executed via actor RPC under consensus), a `Volume` (CBFS public volume mount), a `Runner` (the request proxied off-chain to a selected runner endpoint for non-consensus execution), or a `Workload` (a registered persistent service).
- **Runner target**: A route whose handler runs off-chain on a selected runner (the CIP-9 / RAS execution tier), not in consensus. Distinct from the *state-read runner role* of §8.12 (`GET_STATE`), which only serves verified reads of the actor's KV state; a `Runner` *target* serves the route's request and response itself.
- **Workload target**: A route relayed to a single long-lived registered service — a persistent workload with a control-plane registry record (identity, owning actor, serving endpoint). Distinct from a `Runner` target: no per-request runner selection, no handler dispatch, no route mounts — the Gateway relays bytes to one durable backend, including upgraded WebSocket connections and streamed SSE responses (§8.15).
- **Workload registry record**: The control-plane record describing a persistent workload. This CIP consumes exactly three of its fields — `workload_id`, `owning_actor`, and `serving_endpoint`. `Workload`-target routes are unavailable where that registry does not exist.
- **Pays**: The fallback route payment gate when no enabled CIP-18 `price_table` applies — `actor` (default; the caller pays nothing at establishment) or `caller` (Gateway requires payment proof; on missing/invalid proof returns `402 Payment Required`). An enabled table's matching rule or `default_mode` takes precedence. Workload hosting costs and any actor-funded serving-budget charge follow their own policies.
- **Routes table**: The CBOR-encoded `Routes` value stored in actor KV at the reserved key `__cowboy/routes`.
- **Mount**: A `Volume`-target route with a wildcard path, declared at the SDK module level (e.g., `actor.mount("/assets", volume="web-assets")`).
- **Object**: A single file stored in a CBFS volume (e.g., `assets/logo.png`). Objects are erasure-coded into shards and distributed across Relay Nodes.
- **Shard**: One piece of an erasure-coded object, stored on a single Relay Node. Any K of K+M shards are sufficient to reconstruct the original object.

---

## 6. Routes

### 6.1 Storage Location and Rationale

The routes table is stored in the actor's KV state at the reserved key `__cowboy/routes`. It is NOT stored in the CBFS volume.

The KV location was chosen over alternatives:

- **Volume `_meta/routes.json`**: atomic with assets but couples cadences that should be independent. Routes change far more often than assets — toggles, prices, A/B paths, abuse-mitigation flips — and forcing a `commit_manifest` for every metadata change is friction. It also requires API-only actors (no static files) to provision a CBFS volume just to publish routing rules.
- **Actor `routes()` method** called by the Gateway: defeats the static-serving performance goal because every dispatch would need a `read_handler` PVM execution to read the table.
- **Entitlement parameters** (CIP-2): immutable after deployment, so changing any route would require redeploying the actor. Website route structures change too frequently.
- **Convention-based** (e.g., `/static/*` always from volume): too rigid. A path like `/app.js` might be static in one actor and dynamic in another.

KV storage avoids these problems:

- **Routes are state, not assets.** KV is the natural home for small, structured, mutable actor state.
- **Verifiable by the Gateway with no actor execution.** The actor's state root is committed on-chain. The Gateway fetches the value at `__cowboy/routes` along with a Merkle proof against the state root and verifies it without invoking the actor (§8.12).
- **Update cadence decoupled from asset deploys.** Most route changes (toggle, pays, price) don't touch assets and don't require `commit_manifest`.
- **Atomicity recoverable when needed.** A single block-level transaction can write to `__cowboy/routes` and call `commit_manifest`, restoring atomic asset+route deploys for the cases that need them.

### 6.2 Schema

```
Routes {
  version:  u8,             // schema version (1 for this CIP)
  routes:   list<Route>     // resolution sorts by priority — order in storage is canonical (§6.5)
}

Route {
  verb:      Verb,          // HTTP method this route matches
  path:      string,        // URL path pattern (see §6.3)
  target:    Target,        // where matching requests go
  pays:      Pays,          // who pays — default "actor"
  price:     string?,       // required when pays = "caller", e.g., "0.05 CBY"
  priority:  u16,           // higher value wins; default 0
  enabled:   bool           // default true; allows toggling without removing
}

Verb     = "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "ANY"

Target   = Method | Volume | Runner | Workload

Method {
  kind:    "method",
  name:    string           // CIP-14 actor handler name (e.g., "users.get", "http.request")
}

Volume {
  kind:                "volume",
  volume_name:         string,    // must match a static_volumes entitlement binding (§7)
  strip_prefix:        bool,      // strip matched path prefix before volume lookup
  volume_path_prefix:  string,    // prepended to remaining path
  fallback:            string?,   // object path to serve when not found
  fallback_status:     u16        // HTTP status for fallback (200 for SPA, 404 otherwise)
}

Runner {
  kind:        "runner",
  handler:     string,             // runner-side entrypoint this route invokes (deployed runner code / registered executor); how that code is deployed is out of scope
  requires:    RunnerRequirements?, // OPTIONAL selector — omitted (or empty fields) ⇒ any runner in the actor's runner_bindings (§7.5); set to narrow. Intersected with runner_bindings + RAS health (§8.14)
  affinity:    Affinity,           // soft stickiness: prefer the same runner per key. Default "caller" when auth = "account", else "none"
  runner:      string?,            // OPTIONAL hard pin (escape hatch); when set it overrides selection and MUST be in the eligible set
  auth:        Auth,               // caller identity required to reach the handler — default "public"
  mounts:      list<RouteMount>,   // CBFS volumes the runner is issued CapTokens for, on this route (§7.5)
  timeout_ms:  u32                 // gateway wait before 504 Gateway Timeout; default per §10
}

RunnerRequirements {
  pool:          string?,          // CIP-2 required_runner_pool (entitlement-scoped runner cohort)
  capabilities:  list<string>,     // capability tags the runner must advertise (job-class, model, feature); empty = unconstrained
  region:        string?,          // optional locality constraint
  tee:           bool              // require a TEE-attested runner; default false
}

Workload {
  kind:                    "workload",
  workload:                bytes32,    // workload registry record id (§8.15);
                                       // text encodings MUST be the canonical 64-char lowercase hex
  strip_prefix:            bool,       // strip matched path prefix before forwarding
  auth:                    Auth,       // caller identity required to ESTABLISH the connection — default "public"
  idle_timeout_ms:         u32,        // tear down an established stream after this much silence; default per §10; 0 is invalid
  max_connection_ms:       u32?,       // optional hard ceiling on one connection's lifetime; unset = no ceiling; 0 is invalid
  max_concurrent_streams:  u32?        // per-route open-connection cap; unset = DEFAULT_MAX_CONCURRENT_STREAMS; 0 is invalid (§6.8)
}

Affinity = "none" | "caller" | "session"   // HRW key: stable per caller account, per client session, or unkeyed (pure load-balance)
Auth     = "public" | "account"             // "account" = runner or workload verifies the caller's Cowboy-account (secp256k1) signature

RouteMount {
  volume:       string,           // volume name
  mode:         "ro" | "rw",
  owner:        "actor" | "caller", // "actor": granted in the entitlement (§7.5); "caller": the authenticated caller's own volume (requires auth = "account")
  path_prefix:  string            // least-privilege scope within the volume; default "" (root). Maps to the CIP-9 attachment canonical_path_prefix
}

Pays     = "actor" | "caller"
```

`pays = "actor"` (the default) lets the caller establish the route without a payment proof when no enabled CIP-18 `price_table` applies. CIP-14 query and command paths debit the actor's hot balance under their existing rules. A `Workload` route does not execute the actor per request: without an enabled table, the Gateway treats `pays = "actor"` as free. An enabled table's matching rule or `default_mode` can instead require caller payment or charge the actor's serving budget. Workload hosting and bandwidth costs are governed separately; the route declaration alone does not fund serving.

`pays = "caller"` selects the client-paid fallback when no enabled CIP-18 `price_table` applies. Under that model the Gateway returns `402 Payment Required` with x402-compatible headers until the caller submits a valid payment proof. `price` is required and is the per-request fallback charge interpreted by CIP-18. An enabled table's matching rule or `default_mode` can instead make the route free or actor-funded.

For `Volume` targets, `pays` MUST be `"actor"`. Caller-pays static downloads are deferred to a follow-on CIP.

A `Runner` target proxies the request off-chain to a selected runner, which executes the route handler **outside consensus** — the body and any stream never enter a transaction — then streams the response back through the Gateway. Dispatch does not bind the route to a node: the route declares an optional `requires` selector — omitted, it may run on any runner in the actor's `runner_bindings` (§7.5); set it to narrow — and the Gateway picks a healthy runner from the eligible set at call time, preferring the same one per `affinity` key (§8.14). An optional `runner` pin overrides selection for a route that needs one specific node. This is the path for endpoints that cannot or should not run in the deterministic PVM: LLM inference, outbound API calls, long-running or streaming work. `pays = "caller"` composes normally (paid compute), and `auth = "account"` lets the handler act on the authenticated caller's behalf — e.g., reading or writing the caller's own CBFS volume via a `caller`-owned mount. On-chain state changes still belong behind a `Method` target; a `Runner` handler's execution is never consensus.

A `Workload` target relays the request to one **long-lived registered workload** — a persistent service (model server, agent chat backend, app relay) carrying a control-plane registry record — rather than dispatching per-request to a selected runner. This is the streaming path: the Gateway upgrades and pipes WebSocket connections, relays SSE unbuffered, and applies stream-lifetime policy (`idle_timeout_ms`, `max_connection_ms`) instead of the request-scoped `timeout_ms` (§8.15). The resolved client-paid model gates connection establishment; `pays = "caller"` selects that model only when no enabled `price_table` overrides it. `auth = "account"` authenticates the establishing caller. There is no runner selection, no handler name, and no route mounts — the workload's volume mounts (and any future capabilities) travel with its registry record. The actor publishes the route in `__cowboy/routes`; the Gateway resolves the serving endpoint from the workload registry (§8.15).

### 6.3 Path Patterns

Route paths use the following syntax:

- `/api/users/{user_id}` — named path parameter; binds to a single segment.
- `/static/*rest` — wildcard; matches zero or more segments and binds the remainder to `rest`.
- `/about` — exact match.
- `/assets/*` — anonymous wildcard; matches anything under the prefix without binding.

The Gateway extracts named path parameters from the matched route and passes them to the actor handler in the `HttpRequestEnvelope.path_params` field (§15.2). Static (`Volume`-target) routes do not bind path parameters; the matched suffix is handled per §6.7.

### 6.4 Example: Full-Stack Application

This CIP defines the canonical wire format (the `Routes` value at `__cowboy/routes`) and the Gateway's resolution algorithm. SDK ergonomics are an implementer's choice; this section shows one such surface — a Python SDK that follows the canonical `@actor` / `@actor_method` style established by CIP-6 — to illustrate how a typical full-stack actor would be authored.

```python
import json
from cowboy_sdk import actor, runtime, Pays
from cowboy_sdk.routes import get, post, mount, runner, mount_ro

@actor
class MyApp:
    # Static asset mounts: served directly from the "web-assets" CBFS volume,
    # which must be in this actor's static_volumes entitlement binding.
    routes = [
        mount("/assets", volume="web-assets"),
        mount("/", volume="web-assets", fallback="index.html"),  # SPA fallback
    ]

    @get("/api/users/{user_id}")
    def users_get(self, request):
        user_id = request["path_params"]["user_id"]
        row = self.storage.get(f"user:{user_id}")
        if row is None:
            return {"status": 404, "headers": {}, "body": b""}
        return {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(row).encode(),
        }

    @post("/api/users")
    def users_create(self, request):
        body = json.loads(request["body"]) if request.get("body") else {}
        seq = self.storage.get("user_seq", 0) + 1
        self.storage["user_seq"] = seq
        record = {"id": str(seq), "email": body.get("email"), "name": body.get("name")}
        self.storage[f"user:{seq}"] = record
        return {
            "status": 201,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(record).encode(),
        }

    # Runner target: runs OFF-CHAIN on a runner selected at call time
    # (capability: llm) — not the actor PVM — so it may call an LLM, hit
    # external APIs, or stream.
    @post(
        "/api/inference",
        target=runner(requires={"capabilities": ["llm"]}, mounts=[mount_ro("models")]),  # handler = this method
        pays=Pays.CALLER, price="0.05 CBY",
    )
    def inference_run(self, request):
        body = json.loads(request["body"]) if request.get("body") else {}
        result = run_model(body["prompt"])
        return {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"output": result}).encode(),
        }
```

The SDK lowers the decorators and `mount(...)` declarations to the canonical `Routes` value, written to `__cowboy/routes` at deploy time:

```json
{
  "version": 1,
  "routes": [
    {
      "verb": "GET", "path": "/api/users/{user_id}",
      "target": {"kind": "method", "name": "users_get"},
      "pays": "actor", "priority": 100, "enabled": true
    },
    {
      "verb": "POST", "path": "/api/users",
      "target": {"kind": "method", "name": "users_create"},
      "pays": "actor", "priority": 100, "enabled": true
    },
    {
      "verb": "POST", "path": "/api/inference",
      "target": {"kind": "runner", "handler": "inference_run", "requires": {"capabilities": ["llm"]}, "affinity": "none", "auth": "public",
                 "mounts": [{"volume": "models", "mode": "ro", "owner": "actor"}], "timeout_ms": 60000},
      "pays": "caller", "price": "0.05 CBY",
      "priority": 100, "enabled": true
    },
    {
      "verb": "GET", "path": "/assets/*",
      "target": {
        "kind": "volume", "volume_name": "web-assets",
        "strip_prefix": false, "volume_path_prefix": "assets/",
        "fallback": null, "fallback_status": 404
      },
      "pays": "actor", "priority": 10, "enabled": true
    },
    {
      "verb": "GET", "path": "/*",
      "target": {
        "kind": "volume", "volume_name": "web-assets",
        "strip_prefix": false, "volume_path_prefix": "",
        "fallback": "index.html", "fallback_status": 200
      },
      "pays": "actor", "priority": 0, "enabled": true
    }
  ]
}
```

In this configuration:

- `GET /api/users/abc` → matches `/api/users/{user_id}` (priority 100) → dispatched to `users_get` handler with `path_params = {"user_id": "abc"}`. Actor pays.
- `POST /api/inference` → matches `/api/inference` → Gateway returns `402` with x402 headers until caller pays `0.05 CBY`, then **proxies the request off-chain** to a runner selected from the eligible set (§8.14); `inference_run` runs on the runner, not the actor PVM.
- `GET /assets/logo.png` → matches `/assets/*` (priority 10) → served from `web-assets` volume at `assets/logo.png`. No actor invocation.
- `GET /about` → matches `/*` (priority 0) → looks up `about` in `web-assets`. Not found → serves `index.html` with status `200` (SPA fallback).
- `GET /_cowboy/health` → reserved path, always Gateway-intercepted (CIP-14 §8.6), never consults the routes table.

**Single-verb sugar vs. multi-verb routing.** `@get`/`@post`/etc. are sugar for the underlying `@route(verbs, path)` decorator; both can coexist:

```python
@route(["GET", "POST"], "/foo")
def foo(self, request):
    if request["method"] == "GET":
        ...
    else:
        ...
```

This compiles to two entries in the canonical table — one per verb — both targeting `foo`.

**Runner selector ergonomics (informative).** `requires` is optional, so `runner()` with no selector inherits the actor's `runner_bindings` — the zero-config path when the bound pool is already capable. An SDK SHOULD also offer intent-sugar that lowers to capability tags — e.g. `runner(model="claude-sonnet")` → `requires.capabilities = ["llm", "model:claude-sonnet"]`, or typed helpers like `runner.llm(...)` / `runner.mcp(...)` — so common routes need no tag strings, while `requires={…}` stays the explicit form. A hardcoded default *capability* is deliberately avoided: routes vary (LLM, image generation, webhooks, plain compute), so the default is "inherit the entitlement," not a specific capability.

### 6.5 Canonical Encoding

The `Routes` value is encoded as CBOR (RFC 8949) with deterministic key ordering (CBOR §4.2.1 Core Deterministic Encoding). The `routes` list is sorted before encoding by:

1. `priority` descending,
2. then `path` descending (UTF-8 byte order),
3. then `verb` ascending in **declaration order**:

   ```
   GET < HEAD < POST < PUT < PATCH < DELETE < ANY
   ```

This canonical form ensures that two semantically identical routes tables produce the same CBOR bytes and the same Merkle leaf in the actor's state tree.

> **Errata (COW-3005).** This clause previously read "`verb` ascending (alphabetical)". **No encoder in the Rust stack has done that**: both encoders — `cowboy-protocol-codec`'s and the gateway's — sort tied verbs by the order the `Verb` enum declares them, which is the request-lifecycle order above, not the lexical order (`ANY < DELETE < GET < HEAD < PATCH < POST < PUT`).
>
> One encoder outside that stack did implement the old clause faithfully: `gateway/examples/site-actor/tools/lower_routes.py`, which sorts the verb strings and therefore lexicographically. It writes `routes.cbor` for operators to install, so its output *is* embedded in signed history and this amendment makes it non-conforming; correction pending in cowboyinc/gateway#85. The divergence had not surfaced because that tool's static-route branch emits only `GET`/`HEAD`, one of the eight verb pairs (of twenty-one) the two orders happen to agree on — luck, not design. It is reachable from the method branch: a single `@route(["POST", "PATCH"], …)` ties two verbs the orders disagree on, and the tool then emits `PATCH, POST` where both Rust encoders emit `POST, PATCH`. Tables it already installed keep working: `Routes::decode` does not re-check the ordering, so a table stored in the old order is still read back and served correctly — it is only re-encoding it that would move the digest.
>
> The spec text is what changed here, and deliberately. Canonicalization decides the encoded bytes, the bytes decide the signing digest, and that digest is what account holders have already signed and what deployed actors already carry in their state. Amending the implementation to match the old text would silently re-encode every existing route table: signatures over historical `set_routes` transactions would stop reproducing, and every actor whose table has two verbs at one `(priority, path)` would change its Merkle leaf without anyone touching it. The asymmetry is the whole argument — the implementation's behaviour is embedded in signed history and cannot be recalled; this sentence was not.
>
> Clients are unaffected either way: they decode the exact wire bytes for the signing preview, so a signer always saw the truth. This is a spec-conformance correction, not a change to what any user was shown.

### 6.6 Route Resolution Algorithm

When a Gateway receives an HTTP request for a registered actor:

1. **Reserved paths**: If the path starts with `/_cowboy/`, handle per CIP-14 §8.6. Highest priority, not overridable by the routes table.

2. **Read cached routes** for this actor. Refresh on state-root change (§8.12). Filter to routes with `enabled = true`.

3. **Match candidates.** A route matches if:
   - Its `path` pattern matches the request path (literal segments + named params + wildcards).
   - Its `verb` matches the request method, or `verb = "ANY"`.

4. **Select winner.** Among matching routes:
   - Highest `priority` wins.
   - Tie: longest concrete prefix (path with wildcards/params replaced by their match length) wins.
   - Tie: more specific verb (any non-`ANY`) wins over `ANY`.
   - Tie: `Method` > `Runner` > `Workload` > `Volume` (safety: prefer the consensus handler, then per-request off-chain compute, then the long-lived proxy, over static).

5. **Volume target wins.**
   - The verb MUST be `GET` or `HEAD`. Other verbs on a route whose only match is a `Volume` target return `405 Method Not Allowed`.
   - The Gateway resolves the volume object path per §6.7.
   - If the object exists, serve it from the named volume (§8).
   - If the object does not exist and `fallback` is set, serve the fallback object from the same volume with `fallback_status`.
   - If no fallback, return `404 Not Found`.

6. **Method target wins.**
   - Resolve the payment model using the enabled CIP-18 `price_table` first, falling back to `route.pays` only when no enabled table applies. If the policy read or decode fails, return `503 Service Unavailable`.
   - **Resolved free model**: Dispatch without a payment check. **Resolved actor-funded model**: Check the actor budget before dispatch; a depleted budget MUST NOT authorize free service. CIP-18 §7.2 defines the fallback and deduction rules. The Gateway invokes the named handler, passing the `HttpRequestEnvelope` with `path_params` populated. Unless the route sets `mode`, `GET`/`HEAD` use the CIP-14 query path and `POST`/`PUT`/`PATCH`/`DELETE` use the command path; an explicit `mode` selects the path instead.
   - **Client-paid model** (selected by an enabled `price_table` or fallback `pays = "caller"`): Per CIP-18, verify the caller's payment proof. On missing or invalid proof, return `402 Payment Required` with x402-compatible headers (`X-PAYMENT-Required`, `X-PAYMENT-Receipt`, etc., per CIP-18). On valid proof, dispatch to the named handler.

7. **Runner target wins.**
   - If `auth = "account"`, the request MUST carry the caller's Cowboy-account signature; the runner verifies it (§8.14) and the Gateway returns `401 Unauthorized` if the runner rejects it. Identity is `401`, payment is `402` — they compose.
   - If the resolved payment model is client-paid, apply CIP-18 payment gating exactly as for `Method` (return `402 Payment Required` until a valid proof is presented).
   - The Gateway selects an eligible runner (§8.14) and **proxies the request off-chain** to it; the runner mounts the route's authorized volumes (via dispatcher-issued CapTokens), executes the `handler` outside consensus, and streams the response back. The actor's PVM is **not** invoked.

8. **Workload target wins.**
   - If `auth = "account"`, the establishment request MUST carry the caller's Cowboy-account signature; the workload verifies it (§8.15) and the Gateway returns `401 Unauthorized` when it is rejected.
   - If the resolved payment model is client-paid, apply CIP-18 payment gating to connection **establishment** (return `402 Payment Required` before any upgrade or first streamed byte).
   - The Gateway resolves the workload's serving endpoint from its registry record and relays the connection per §8.15 — WebSocket upgrade + transparent byte-pipe, or unbuffered streamed HTTP response. The actor's PVM is **not** invoked.

9. **No match.** Return `404 Not Found`. Explicit routes are required; there is no fallback `default_behavior`.

### 6.7 Volume Object Path Resolution

Given a `Volume`-target route match and a request path:

```
request_path = "/docs/getting-started"
matched_route.target = {
  volume_name: "docs-site",
  strip_prefix: true,
  volume_path_prefix: "documentation/"
}
matched_route.path = "/docs/*"

1. Extract remainder:
   prefix = path-prefix-of(matched_route.path)   // "/docs/"
   remainder = request_path.removePrefix(prefix) // "getting-started"

2. Build volume path:
   volume_path = matched_route.target.volume_path_prefix + remainder
   // volume_path = "documentation/getting-started"

3. Look up volume_path in the canonical, root-verified manifest path index (§8.5) and read the file's ObjectDescriptor.
   - If found: serve the object.
   - If not found and target.fallback is set: serve the fallback object with target.fallback_status.
   - If not found and no fallback: return 404.
```

If `strip_prefix` is `false`, the full request path (minus leading `/`) is used:

```
request_path = "/assets/logo.png"
matched_route.target = {
  volume_name: "web-assets",
  strip_prefix: false,
  volume_path_prefix: "assets/"
}

// strip_prefix=false → use request path directly (minus leading "/")
volume_path = "assets/logo.png"
```

### 6.8 Validation

Gateways MUST validate the routes table on load:

- `version` MUST be `1`. Unknown versions cause the Gateway to fall back to all-CIP-14 dispatch (no routes-driven dispatch; HTTP ingress still works for actors with an `http.request` handler).
- `routes` MUST NOT exceed `MAX_ROUTES` entries.
- Total CBOR-encoded size MUST NOT exceed `MAX_ROUTES_SIZE`.
- Each `path` MUST start with `/`.
- `path` MUST NOT equal `/_cowboy/` or start with `/_cowboy/` (reserved by CIP-14).
- `verb` MUST be one of the seven valid values.
- `target.kind` MUST be `"method"`, `"volume"`, `"runner"`, or `"workload"`.
- For `Method` targets: `name` MUST be a non-empty string. (Method existence is validated at deploy time by the SDK lowering pipeline; runtime missing-method errors return `502 Bad Gateway`.)
- For `Volume` targets: `volume_name` MUST match one of the actor's `static_volumes` entitlement bindings; `pays` MUST be `"actor"`; `fallback_status` MUST be a valid HTTP status code (100–599).
- For `Runner` targets: `handler` MUST be a non-empty string; `requires`, if present, MUST be covered by some `runner_bindings` entry (§7.5); if omitted, the route inherits the actor's `runner_bindings` — which MUST be non-empty for any `Runner` route; either way the eligible set MUST be non-empty; if the optional `runner` pin is set it MUST fall within that eligible set; `affinity` MUST be `none` / `caller` / `session`, and `affinity = "caller"` REQUIRES `auth = "account"`; `auth` MUST be `"public"` or `"account"`; every `mounts[]` with `owner = "actor"` MUST be authorized in that binding, with its `mode` and `path_prefix` within the bound grant; any mount with `owner = "caller"` REQUIRES `auth = "account"` (its `mode` / `path_prefix` are checked against the caller's CIP-9 grant at dispatch, §8.14); `timeout_ms` MUST be ≤ `MAX_RUNNER_TIMEOUT_MS` (§10).
- For `Workload` targets, route-table/config validation is structural: `workload` MUST be exactly 32 bytes (a text form MUST be canonical 64-char lowercase hex; wrong length or noncanonical encodings reject); `auth` MUST be `"public"` or `"account"`; `idle_timeout_ms` MUST be in `1..=MAX_WORKLOAD_IDLE_TIMEOUT_MS`; `max_connection_ms`, if present, MUST be at least 1; and `max_concurrent_streams`, if present, MUST be in `1..=MAX_CONCURRENT_STREAMS_CEILING` (§10). Registry existence, ownership, route eligibility, and endpoint validity are establishment-time checks (§8.15), not route-load checks: a transiently unavailable registry MUST NOT make the Gateway discard an otherwise valid actor route table.
- `pays` MUST be `"actor"` or `"caller"`.
- When `pays = "caller"`: `price` MUST be present and parseable per CIP-18.

If validation fails, the Gateway logs a warning, evicts the cached routes table, and falls back to all-CIP-14 dispatch. The actor's HTTP ingress continues to work — only routes-driven dispatch is disabled until the next valid update.

### 6.9 Runtime Mutability

The routes table can be modified at runtime via the actor's normal KV write path. Two API levels are exposed:

**Wholesale rewrites.** The actor can replace the entire `__cowboy/routes` value. This is heavyweight and intended for maintenance scenarios (e.g., swapping in a static "we'll be back" routes table during a migration). Rewrites are subject to the same validation as deploy-time route tables (§6.8).

**Narrow mutators.** The SDK SHOULD expose typed helpers that compile to atomic, scoped KV writes:

- `routes.disable(verb, path)` — sets `enabled = false` for the matching route.
- `routes.enable(verb, path)` — sets `enabled = true`.
- `routes.set_pays(verb, path, pays, price?)` — updates payment policy.
- `routes.set_priority(verb, path, priority)` — re-prioritizes.
- `routes.replace_handler(verb, path, name)` — points a route at a different method.

These mutators avoid the foot-guns of full-table rewrites (accidentally dropping all routes, breaking unrelated entries) and are the recommended runtime API.

**Caching impact.** Each commit that changes `__cowboy/routes` advances the actor's state root. Gateways pick up changes within `MANIFEST_POLL_INTERVAL` blocks. To bound cache churn, the protocol enforces a minimum interval between routes-affecting commits: a commit that mutates `__cowboy/routes` more often than `MIN_ROUTES_UPDATE_INTERVAL_BLOCKS` (default: 6 blocks) is rejected by the runtime with a `RoutesUpdateRateLimited` error.

**Threat model.** The routes table is owner-controlled. A malicious actor owner can rewrite their own routes — that is outside the protocol's threat model (see §12.5). Reserved `/_cowboy/*` paths remain Gateway-intercepted regardless of the routes table.

---

## 7. Entitlement Extension

### 7.1 New Parameters

This CIP extends the `ingress.http` entitlement (CIP-14 §6.2) with two new parameters:

| Param | Type | Description | Default |
| --- | --- | --- | --- |
| `static_volumes` | `array<StaticVolumeBinding>` | Public volumes the Gateway may serve as static assets. | `[]` (no static serving) |
| `max_static_response_bytes` | `u64` | Maximum size of a single static asset response. | `10_485_760` (10 MiB) |

```
StaticVolumeBinding {
  volume_name:      string,    // must be a Visibility::Public volume owned by the same account
  max_cache_bytes:  u64        // maximum Gateway cache space this volume may consume (per Gateway)
}
```

The existing CIP-14 parameters (`allowlist_methods`, `max_request_bytes`, `max_response_bytes`, `max_query_cycles`) are unchanged and continue to govern Method-target route behavior. `max_static_response_bytes` is separate from `max_response_bytes` because static assets (images, video thumbnails, font files) are typically much larger than dynamic API responses.

### 7.2 Example Manifest

```json
{
  "entitlements": [
    {"id": "ingress.http", "params": {
      "allowlist_methods": ["GET", "HEAD", "POST"],
      "max_request_bytes": 1048576,
      "max_response_bytes": 1048576,
      "max_query_cycles": 10000000,
      "static_volumes": [
        {"volume_name": "web-assets", "max_cache_bytes": 104857600}
      ],
      "max_static_response_bytes": 10485760
    }},
    {"id": "storage.kv", "params": {"max_bytes": 10485760}},
    {"id": "econ.hold_balance"},
    {"id": "econ.transfer"}
  ]
}
```

### 7.3 Enforcement

- **Deployment-time**: The deployment transaction verifies that each `volume_name` in `static_volumes` references a `Visibility::Public` volume owned by the deploying account. If any volume does not exist or has `Visibility::Private`, deployment is rejected. The `volume_id` is deterministic per CIP-9 §11.1 (`keccak256(account_address || volume_name)`), so cross-account references are impossible.
- **Gateway enforcement**: Gateways MUST only serve static assets from volumes listed in `static_volumes`. Objects larger than `max_static_response_bytes` return HTTP `413 Content Too Large`. Gateways MUST respect `max_cache_bytes` per volume when allocating cache space.
- **Volume lifecycle**: The Gateway's serving authority follows the volume's CIP-9 status (§8.13). If a volume listed in `static_volumes` is `DELETED` or `GARBAGE_COLLECTING`, the Gateway returns the corresponding HTTP status without invoking the actor. The entitlement remains valid — the volume reference is stale. The actor owner must redeploy with updated `static_volumes` (or restore the volume) to resume static serving.

### 7.4 Why `max_cache_bytes` Is in the Entitlement

Cache limits are a resource commitment by the Gateway. Entitlements are the established mechanism for declaring resource quotas that the protocol enforces. Placing cache limits in the routes table — which is actor-mutable at runtime without redeployment — would let actors arbitrarily expand their cache footprint on Gateways without any on-chain governance check.

### 7.5 Runner Bindings

`Runner`-target routes require the actor to authorize, at deploy time, which runners may serve them and which actor-owned volumes those runners may be issued CapTokens for. This CIP adds a third `ingress.http` parameter:

| Param | Type | Description | Default |
| --- | --- | --- | --- |
| `runner_bindings` | `array<RunnerBinding>` | The runner pools / capabilities the actor's `Runner` routes may dispatch to, and the actor-owned volumes those runners may mount. | `[]` (no runner serving) |

```
RunnerBinding {
  pool:          string?,         // CIP-2 required_runner_pool the actor may dispatch routes to
  capabilities:  list<string>,    // capability tags the actor may require (job-class, model, feature)
  mounts:        list<RouteMount>  // actor-owned volumes runners under this binding may mount (owner = "actor")
}
```

**Mount authorization is by principal, not by runner.** CIP-9's mount-allowlist grants access to a `MountPrincipal` (`Actor` or `Account`) — never to a runner directly. The runner holds no standing grant; at dispatch it receives a *scoped CapToken* (and, for a private volume, a CBSS-sealed DEK) issued by the same dispatcher path that attaches volumes to a job. `runner_bindings` does not create a CIP-9 grant — it authorizes the Gateway to dispatch this actor's `Runner` routes to runners in the bound pool / capability set and bounds which actor-owned volumes a route may reference.

**Enforcement.**

- *Deployment-time (actor-owned mounts).* Each binding's `pool` / `capabilities` MUST be ones the actor is entitled to (CIP-2). Each `owner = "actor"` mount volume MUST be owned by the deploying account (or carry an `Actor`-principal grant to it). A route's `requires` MUST be covered by some `RunnerBinding` (its `pool` / `capabilities` a subset of the binding's), and its `owner = "actor"` mounts MUST appear in that binding with the route's `mode` and canonicalized `path_prefix` within the grant. Runners chosen at dispatch (§8.14) must be RAS-registered, healthy, and advertise a route-serving endpoint.
- *Request-time (caller-owned mounts).* `owner = "caller"` mounts are authorized by the **caller**, not the actor: the caller must own the volume and have granted the agent **actor** (`MountPrincipal::Actor`) mount access via the CIP-9 allowlist. After the runner authenticates the caller (§8.14), the dispatcher issues the runner a CapToken (+ sealed DEK) for that caller's volume *under the actor principal*; the authenticated caller's identity is what **selects** which caller-owned volume. This lets one endpoint give every caller scoped access to their own owned storage without the actor hosting it.

The mount machinery — CIP-9 allowlist grants to a principal, dispatcher-issued per-runner CapTokens, and CBSS threshold-seal of the DEK for private volumes — already ships and is exercised by actor jobs. What this CIP adds is routing a *request* through that issuance path and selecting a `caller`-owned volume from the authenticated HTTP caller per request.

---

## 8. Gateway-to-Relay-Node Fetch Protocol

### 8.1 Request Flow

When a Gateway receives an HTTP request that resolves to a `Volume`-target route:

```
Client                    Gateway                         Relay Nodes
  |                         |                                 |
  |--- GET /app.js -------->|                                 |
  |                         |--- 1. Check object cache        |
  |                         |   key: (volume_id, "app.js")    |
  |                         |                                 |
  |                         |   [CACHE HIT + fresh]           |
  |<-- 200 + body ----------|                                 |
  |                         |                                 |
  |                         |   [CACHE HIT + If-None-Match]   |
  |<-- 304 Not Modified ----|                                 |
  |                         |                                 |
  |                         |   [CACHE MISS]                  |
  |                         |--- 2. Fetch volume manifest --->|
  |                         |   (public, no CapToken)         |
  |                         |<--- manifest bytes -------------|
  |                         |                                 |
  |                         |--- 3. Look up file entry for    |
  |                         |   "app.js" in manifest          |
  |                         |                                 |
  |                         |--- 4. Fetch K shards in ------->|
  |                         |   parallel from K Relay Nodes   |
  |                         |<--- shard 0 --------------------|
  |                         |<--- shard 1 --------------------|
  |                         |<--- shard 2 --------------------|
  |                         |<--- shard 3 --------------------|
  |                         |                                 |
  |                         |--- 5. Reed-Solomon reconstruct  |
  |                         |--- 6. Verify content_hash       |
  |                         |--- 7. Read content-type + cache |
  |                         |--- 8. Store in object cache     |
  |                         |                                 |
  |<-- 200 + headers + body-|                                 |
```

### 8.2 Gateway-Side Caching

The Gateway maintains a layered cache:

**Layer 1a: Per-Volume Metadata Cache** (always warm for active volumes)

```
VolumeMetadataCache {
  content_type_map:     ContentTypeMap,       // _meta/content_types.json (CIP-9 §7.6.5)
  cache_config:         CacheConfig,          // _meta/cache_config.json (CIP-9 §7.6.6)
  cors_config:          CorsConfig?,          // _meta/cors.json (this CIP §9)
  volume_manifest:      VolumeManifest,       // verified path-ordered ManifestEntry map
  manifest_root:        bytes32,              // on-chain StorageCommitment.manifest_root
  last_verified_block:  BlockHeight           // block at which manifest_root was checked
}
```

Keyed by `volume_id`. Refreshed when the on-chain `manifest_root` changes.

**Layer 1b: Per-Actor Routes Cache** (always warm for active actors)

```
ActorRoutesCache {
  routes:               Routes,               // CBOR Routes from __cowboy/routes
  state_root:           bytes32,              // on-chain Account.state_root
  last_verified_block:  BlockHeight           // block at which state_root was checked
}
```

Keyed by `actor_address`. Refreshed when the on-chain `state_root` changes (§8.12).

**Layer 2: Object Cache** (LRU, bounded)

```
ObjectCacheEntry {
  body:             bytes,          // reconstructed plaintext
  content_hash:     bytes32,        // BLAKE3, used as ETag
  content_type:     string,         // resolved MIME type
  content_length:   u64,
  cached_at:        BlockHeight,
  max_age:          u32             // from cache_config
}
```

Keyed by `(volume_id, object_path)`. Bounded by `max_cache_bytes` per volume (from entitlement) and `MAX_GATEWAY_CACHE_BYTES` total across all volumes. Eviction follows LRU with frequency weighting — frequently accessed objects are retained longer.

### 8.3 Cache Invalidation

Cache invalidation is driven by changes to the on-chain `StorageCommitment.manifest_root`. When volume contents change (new deploy via `commit_manifest`), the manifest root changes.

**Protocol:**

1. The Gateway polls the on-chain `StorageCommitment.manifest_root` for each actively-cached volume every `MANIFEST_POLL_INTERVAL` blocks (default: 6, ~6 seconds at 1 block/sec).
2. If `manifest_root` has changed:
   a. Fetch the new volume manifest from any Relay Node.
   b. Diff old and new manifests to identify changed, added, and removed objects.
   c. Evict all changed and removed objects from the object cache.
   d. Update the per-volume metadata cache (content-type map, cache config, CORS config).
3. If `manifest_root` is unchanged, all cached objects remain valid.

Per-actor routes (Layer 1b) follow the analogous protocol against `Account.state_root`; see §8.12.

**Why polling, not push:** Relay Nodes are dumb storage without push notification capability. Subscribing to on-chain events for every cached volume creates scaling concerns. Polling the `manifest_root` (a single 32-byte on-chain read) is cheap and sufficient — static asset invalidation latency of a few seconds is acceptable.

### 8.4 Relay Node RPC

Gateways interact with Relay Nodes using the existing CIP-9 `GET_SHARD` RPC (CIP-9 §16.3) plus `GET_MANIFEST` for direct manifest retrieval.

#### `GET_MANIFEST`

Retrieves the canonical serialized volume manifest for a given volume.

```
GET_MANIFEST { volume_id: bytes32, manifest_root: bytes32, erasure_k: u8, erasure_m: u8 }
  → { manifest: bincode(map<path → ManifestEntry>) }
```

**CIP-9 §5.3.2 is normative for this RPC** — shape, authorization and verification
alike. This section describes only what is specific to a Gateway. Duplicating the
schema here is what let the two documents drift; the block above is a convenience
copy and CIP-9 wins on any disagreement.

What a Gateway needs to know:

- **Public volumes only.** A Gateway is an unauthenticated public reader (§8.13 and CIP-9 §7.6.3) and holds no CapToken or DEK. A private manifest is read node-by-node via `GET_SHARD` by a DEK holder. CIP-9 §5.3.2 distinguishes this assembly restriction from the reference implementation's admission of the volume owner's `READ_WRITE` token; grant-scoped tokens do not admit `GET_MANIFEST`.
- **The caller supplies the root.** The Gateway reads `manifest_root`, `erasure_k` and `erasure_m` from the volume's on-chain `StorageCommitment` and passes them in the request. The response carries no root: the Gateway MUST rebuild the manifest from the returned entries, recompute its DAG root, and reject unless it equals the root it supplied. On mismatch, try another Relay Node and mark the offending one suspect.

If `GET_MANIFEST` is not available on a given Relay Node, the Gateway MAY fall back to node-by-node traversal via `GET_SHARD` from the on-chain root, fetching each manifest node at `manifest_node_shard_id(volume_id, locator)` as defined in CIP-9 §5.3.2 and the storage whitepaper’s [Manifest](../whitepaper/cowboy-storage-whitepaper.md#manifest) section. There is no well-known single manifest blob fallback.

A Relay Node does **not** hold the whole manifest, and no push propagates one to it: each DAG node is itself an erasure-coded object spread across a placement set, and a relay stores only its assigned shard indices. It answers `GET_MANIFEST` by *assembling* the DAG from peers, or from a cache it built for itself — see CIP-9 §5.3.2, "Server-side model". A relay that cannot assemble it does not implement the RPC, and the Gateway falls back to node-by-node traversal as above.

#### `GET_SHARD` (existing CIP-9 §16.3)

```
GET_SHARD { shard_id: bytes32, shard_index: u8, volume_id: bytes32 }
  → shard_bytes
```

`volume_id` is **required**, not decorative: CIP-9 §7.6.2/§7.6.3 make an anonymous
read conditional on the shard's recorded volume matching the volume the request
names, and a request that names none is refused. The response is the shard bytes
alone — the relay returns no hash, because a hash it chose would verify nothing.
The Gateway verifies each shard against the `shard_hash` carried in the object's
own descriptor from the manifest it has already verified against the chain
(§8.5). Shards that fail verification are discarded and replacements are fetched
from alternative Relay Nodes.

For public volumes, no CapToken is required (CIP-9 §7.6.3) — subject to the
status gate there: a volume that is not `ACTIVE` or `GRACE_PERIOD` is not served
anonymously.

### 8.5 Canonical Manifest Serialization and Verification

`GET_MANIFEST` returns `bincode(BTreeMap<String, ManifestEntry>)`, an entry map ordered by path. The map is not the stored DAG encoding and its serialized bytes MUST NOT be hashed directly as the manifest root. Verification MUST rebuild the canonical public manifest DAG specified in CIP-9 §3.1: group entries into leaf nodes, build interior nodes, and compute each locator from its canonical stored bytes. The top node's locator is the computed root. A flat `ShardMap` list or an RFC-6962 tree is not this format.

**Gateway verification protocol:**

1. Read `manifest_root`, `erasure_k` and `erasure_m` from the volume's authoritative on-chain `StorageCommitment`. Require public visibility and a serveable status as specified in CIP-9 §5.3.2.
2. Fetch `GET_MANIFEST { volume_id, manifest_root, erasure_k, erasure_m }`, using the values read in step 1.
3. Decode the entry map with a bincode byte budget bounded by the received payload length and construct its canonical path index using CIP-9 §5.3.2, **Entry-map decoding and lookup**. Every lookup key must come from the corresponding value's `entry.path()`. Reject inconsistent keys or duplicate entry paths, or normalize them exactly as specified there before verification.
4. Rebuild the canonical public DAG from that canonical path index and compute its root.
5. Require the computed root to equal the authoritative root from step 1. On mismatch, reject the manifest without caching or serving its content and try another Relay Node.

The response carries no root. A root supplied or echoed by the same relay is not a substitute for the independent chain commitment. After verification, the Gateway MUST use that same canonical path index for lookups and caching, never the original wire keys. It resolves file content and shard hashes from the file's `ObjectDescriptor`; a PlacementRecord supplies shard locations, not the content trust anchor.

### 8.6 Integrity Verification

The Gateway MUST verify every layer of the integrity chain:

1. **Manifest integrity**: Verify the fetched manifest against the on-chain `manifest_root` using the Merkle verification protocol (§8.5). A manifest that does not match the on-chain root MUST be rejected.
2. **Shard integrity**: For each fetched shard, verify `BLAKE3(shard_bytes) == shard_hash` from the verified ObjectDescriptor. Shards that fail verification are discarded; the Gateway fetches a replacement from an alternative Relay Node.
3. **Object integrity**: After Reed-Solomon reconstruction, verify `BLAKE3(reconstructed_bytes) == content_hash` from the verified ObjectDescriptor. If verification fails, the Gateway MUST NOT serve the object and MUST return HTTP `502 Bad Gateway`.

### 8.7 Parallel Fetch and Hedging

For latency-sensitive serving, the Gateway employs adaptive parallel fetch:

1. **Initial fetch**: Request K shards from the K lowest-latency Relay Nodes (based on historical RTT for that volume's shard assignments).
2. **Hedged requests**: If any shard request takes longer than `HEDGE_THRESHOLD_MS` (default: 100ms), issue a speculative request to an alternative Relay Node for a parity shard. Use whichever response arrives first.
3. **Shard selection**: The Gateway prefers data shards (indices `0..K-1`) but accepts parity shards. Any K of the K+M shards suffice for reconstruction.
4. **Concurrency cap**: No more than `MAX_CONCURRENT_SHARD_FETCHES` (default: 8) outstanding shard requests per object reconstruction.

### 8.8 HTTP Response Headers

When serving a static asset, the Gateway sets the following headers:

```
HTTP/1.1 200 OK
Content-Type: text/javascript; charset=utf-8
Content-Length: 45231
ETag: "b3_a1b2c3d4e5f6..."
Cache-Control: public, max-age=86400, immutable
X-Cowboy-Block: 1234567
X-Cowboy-Manifest-Root: a1b2c3d4...
X-Cowboy-Volume: web-assets
X-Cowboy-Source: static
Vary: Accept-Encoding
```

| Header | Source | Notes |
|--------|--------|-------|
| `Content-Type` | `_meta/content_types.json` (CIP-9 §7.6.5). Falls back to extension-based MIME inference. | |
| `ETag` | `"b3_" + hex(content_hash)`. The `b3_` prefix distinguishes BLAKE3 from other hash formats. | |
| `Cache-Control` | `_meta/cache_config.json` (CIP-9 §7.6.6). Falls back to `public, max-age=3600`. | |
| `X-Cowboy-Block` | Block height of the manifest used to resolve the object. | Same header as CIP-14 dynamic responses. |
| `X-Cowboy-Manifest-Root` | Hex of the `manifest_root` used to resolve the object. Static responses only. | Lets clients pin a specific manifest version; closes the dual-versioning gap with `X-Cowboy-Block`. |
| `X-Cowboy-Volume` | Volume name (informational). | |
| `X-Cowboy-Source` | `"static"` for objects served from a `Volume` target, `"dynamic"` for `Method` targets. | |
| `Vary` | `Accept-Encoding` if compression is applied. | |

Clients requiring strict consistency MAY send `X-Cowboy-Manifest-Root: <hex>` as a request header; the Gateway returns `409 Conflict` if its cached manifest does not match.

### 8.9 Conditional Requests

- **`If-None-Match`**: The Gateway compares the client's ETag against the cached object's `content_hash` (`"b3_" + hex(content_hash)`, §8.8) under RFC 7232 §3.2 weak comparison. If it matches, return `304 Not Modified` with no body — the decision is gated on the content-hash ETag **alone**. The manifest is deliberately not consulted here: byte-identical content under a changed `manifest_root` still returns `304`, which is HTTP-correct because the representation the client holds is unchanged. Manifest-root coherence is enforced separately by the `X-Cowboy-Manifest-Root` request-pin → `409` mechanism (§8.8), not as a condition on the ETag match; the `304` still echoes `x-cowboy-manifest-root` so a pinning client still observes a manifest change.
- **`If-Modified-Since`**: Not directly supported. Volume objects do not have per-file modification timestamps. Clients SHOULD use `If-None-Match` (ETag-based) for conditional requests.

### 8.10 Compression

Gateways SHOULD support `Accept-Encoding: gzip, br` and compress responses on-the-fly for compressible content types: `text/html`, `text/css`, `application/javascript`, `application/json`, `image/svg+xml`, `text/plain`, `application/xml`.

Binary content types (`image/png`, `image/jpeg`, `font/woff2`, `application/octet-stream`) MUST NOT be compressed — they are already optimally encoded and compression wastes CPU.

Gateways MAY cache compressed variants alongside the uncompressed object to avoid re-compressing on subsequent requests.

### 8.11 Relay Node Bandwidth Economics

CIP-9 specifies that Relay Nodes earn storage fees proportional to the shards they store (CIP-9 §10). This CIP introduces Gateways as a major reader class — potentially fetching shards at high volume for popular static sites. The bandwidth cost model:

**Who pays for shard reads:**

- The **volume owner** (actor's account) pays for Relay Node storage via per-epoch storage fees (CIP-9 §10). These fees cover the cost of storing *and serving* shards.
- Gateways do NOT pay per-shard-fetch fees directly. The bandwidth cost is absorbed into the Relay Node's storage fee revenue, analogous to how a CDN origin server absorbs bandwidth from CDN edge pulls.

**Rationale:** Introducing per-read micropayments between Gateways and Relay Nodes would add significant protocol complexity (payment channels, per-request accounting) for marginal benefit. The storage fee model already compensates Relay Nodes for both storage and bandwidth — nodes that serve more popular volumes earn proportionally more storage fees because those volumes persist (the owner keeps paying). If a volume is not worth paying storage fees for, it gets garbage-collected.

**Relay Node abuse protection:**

Relay Nodes MAY enforce local rate limits on `GET_SHARD` and `GET_MANIFEST` requests to prevent bandwidth abuse:

- Per-source-IP rate limiting (suggested: `MAX_SHARD_READS_PER_SECOND = 1000` per IP).
- Per-volume rate limiting (suggested: `MAX_SHARD_READS_PER_VOLUME_PER_SECOND = 500` per volume per IP).
- Total bandwidth throttling per connection.

These limits are locally enforced by each Relay Node and are not protocol-mandated constants. Relay Node operators MAY adjust them based on their infrastructure capacity.

**Future work:** A follow-on CIP may introduce actor-funded bandwidth budgets — actors deposit CBY into a bandwidth pool that compensates Relay Nodes for read traffic proportional to actual serving volume. This would align incentives more precisely but requires a per-read accounting mechanism that is not justified in this initial CIP.

### 8.12 Routes Fetching (Gateway ↔ Runner)

The routes table is fetched from the actor's KV state, not from a Relay Node. The Gateway uses a state-read RPC against any Runner serving the actor:

#### `GET_STATE` (state read with Merkle proof)

```
GET_STATE {
  actor_address:  bytes20,
  key:            bytes,            // "__cowboy/routes" for the routes table
  block_height:   u64?              // optional pin; default = latest finalized block
}
→ {
  value:          bytes,            // CBOR-encoded Routes (or empty if unset)
  proof:          MerkleProof,      // proof from leaf to state_root
  state_root:     bytes32,          // actor.state_root at the resolved block
  block_height:   u64               // block at which state_root was read
}
```

**Verification protocol:**

1. The Gateway fetches via `GET_STATE(actor_address, "__cowboy/routes")`.
2. The Gateway verifies `state_root` matches the on-chain `Account.state_root` for `actor_address` at `block_height`.
3. The Gateway verifies the Merkle proof from `value` to `state_root`.
4. If both checks pass, the Gateway deserializes `value` as `Routes` and validates per §6.8.
5. The verified routes table is cached, keyed by `(actor_address, state_root)`.

If the response cannot be verified, the Gateway MUST reject it and try another Runner.

**Cache invalidation:** Identical to §8.3 but driven by `Account.state_root` instead of `StorageCommitment.manifest_root`. The Gateway polls every `MANIFEST_POLL_INTERVAL` blocks; on a state-root change, it refetches `__cowboy/routes` and replaces the cached routes table.

**Empty routes:** If `value` is empty (the actor has never written to `__cowboy/routes`), the Gateway falls back to all-CIP-14 dispatch. The actor's HTTP ingress works exactly as if this CIP did not exist — every request reaches the `http.request` handler. This is the migration path for existing actors.

**Bandwidth economics:** State reads against a Runner are part of normal protocol operation (block production, RPC) and do not incur additional fees. Per-read rate limits MAY be enforced locally by Runners (suggested: same `MAX_SHARD_READS_PER_SECOND` as Relay Nodes).

### 8.13 Serving Authority by Volume Status

Gateway HTTP serving authority maps to the CBFS volume lifecycle (CIP-9 §13): `ACTIVE → GRACE_PERIOD → DELETED → GARBAGE_COLLECTING`.

| `StorageCommitment.status` | Gateway behavior |
|---|---|
| `ACTIVE` | Serve normally. |
| `GRACE_PERIOD` | Serve, with advisory header `X-Cowboy-Storage-Status: grace`. |
| `DELETED` | `503 Service Unavailable` + `X-Cowboy-Error: VOLUME_DELETED`. |
| `GARBAGE_COLLECTING` | `410 Gone` + `X-Cowboy-Error: VOLUME_GC`. |

Continuing to serve in `GRACE_PERIOD` is intentional — the owner may top up storage fees at any moment, and abrupt 503s would be a worse user experience than serving with an advisory header. The hard halt happens at `DELETED` (intentional removal) and `GARBAGE_COLLECTING` (irreversible). This bounds the "free CDN" externality (Gateway caches continuing to serve after the owner stops paying) within `STORAGE_GRACE_EPOCHS` per CIP-9.

### 8.14 Runner-Target Dispatch (Gateway → Runner)

For a `Runner`-target route (resolution §6.6 step 7), the Gateway does not invoke the actor's PVM. It proxies the request off-chain to a runner and relays the response.

**Runner selection.** The Gateway resolves the route to a runner per request. It reads runner registry state (as it already does to fetch routes) and applies the **same eligibility filters the dispatcher applies to jobs** (the registry's `filter_job_candidates`): capability / job-type, the CIP-2 `required_runner_pool` entitlement, TEE, rate card, active-job capacity, and health / heartbeat (derived from `last_heartbeat`). It then makes the pick itself:

1. Build the **eligible set** `E` = runners passing those filters, intersected with the route's `requires`, the actor's `runner_bindings` entitlement (§7.5), and current RAS health. (`requires.region` is checked **route-side** against the runner's advertised `capabilities.regions` — the job filter doesn't apply region today.) If `E` is empty, return `503 Service Unavailable`.
2. If the optional `runner` pin is set: use it if it is in `E`, else `503`.
3. Else if `affinity ≠ none`: pick `argmax(r in E) of H(affinity_key, r.id)` — **rendezvous (HRW) hashing**. The same key maps to the same runner while it stays in `E`; when a runner leaves `E`, only the keys that were on it move to their next-best, leaving every other key undisturbed. The `affinity_key` is the caller account (`caller`), a client-supplied session id (`session`), or unused (`none`).
4. Else: spread across `E` — least-loaded by advertised active-job count, or round-robin.

The selected runner advertises a `route_serving_endpoint` in its RAS registration — a new registration field this CIP requires. The Gateway reuses existing registry data and mirrors the job-dispatch eligibility filters where they exist; the **route-only additions** are `region` filtering, HRW `affinity`, optional route-pin validation, and the `route_serving_endpoint` proxy.

**Request forwarding.** The Gateway dispatches to the selected runner's `route_serving_endpoint`:

```
POST {route_serving_endpoint}/_cowboy/route
  <forwarded request headers>
  X-Cowboy-Route-Context: base64url(CBOR(RouteContext))
  <buffered request body, raw bytes>

RouteContext {
  actor_address, handler, verb, path, path_params,
  mounts:         list<RouteMount>,
  auth:           Auth,       // the route's caller-auth requirement, so the runner self-enforces
  payment_proof:  bytes?,     // untrusted raw client X-Payment header, if supplied; not a settlement result
  account_sig:    bytes?,     // §8.14 caller-auth, when auth = "account"
  account_expiry: uint?,      // unix-seconds expiry bound into account_sig
  account_nonce:  bytes?,     // single-use nonce bound into account_sig
  chain_id:       uint,       // target deployment; the runner rejects a mismatch
  network:        string      // target deployment (see chain_id)
}
```

The runner maps `(actor_address, handler)` to deployed code; it MUST already have a handler registered for `(actor_address, handler)` before the Gateway may dispatch — how that handler is deployed and registered is out of scope, and the runner returns `502` if it is absent. The runner returns a normal HTTP response (status, headers, body or chunked stream); the Gateway relays it verbatim, applying only the CORS rule (§9.4), `timeout_ms`, and the `502`/`504` mapping. The Gateway is **untrusted transport** and asserts no caller identity.

**Caller authentication (`auth = "account"`).** This is a **new** signed-request scheme — NOT the existing EIP-712 `SessionVoucher` (which is payment-specific). The caller signs, with their Cowboy-account secp256k1 key, a canonical preimage over:

```
domain_tag                                   // fixed, unique to CIP-15 route auth
  ‖ chain_id ‖ len‖network ‖ actor_address
  ‖ len‖handler ‖ len‖verb ‖ len‖path
  ‖ body_sha256 ‖ expiry ‖ len‖nonce
```

The preimage MUST be **domain-separated** (a fixed tag distinct from every other Cowboy signing context) and MUST **length-prefix** each variable-length field, so that no two distinct field assignments can serialize to the same bytes (otherwise e.g. `handler="a",verb="bc"` and `handler="ab",verb="c"` would share a signature). The signature is over `keccak256(preimage)`, recoverable to the caller's account. The recovered signer is the **caller** — any Cowboy account, authenticating *as itself*, **not** the actor — so a single endpoint serves every authenticated user under their own identity (the multi-user model). The runner recovers the signer (reusing the runner's existing secp256k1 recovery utilities), **verifies `chain_id` and `network` against its own deployment** (rejecting a signature minted for another chain — closing cross-deployment replay, which per-runner `nonce` stores would not), checks `expiry` (a bounded validity window) and the single-use `nonce` for replay, forwards the recovered caller to the handler, and binds the request to that account; on any failure it returns `401`, which the Gateway relays. The Gateway never asserts caller identity. (`host` is gateway-forwarded and not independently verifiable by the runner under the untrusted-gateway model, so it is not bound.) The recovered account is what authorizes a `caller`-owned mount (§7.5).

The runner **self-enforces** `auth` from `RouteContext.auth`: a route with `auth = "account"` and no `account_sig` is rejected `401` even when a request reaches the runner directly — so the Gateway's `account_sig` presence check is defense-in-depth, not the sole gate.

**Mounts.** The runner holds no standing grant; the dispatcher issues it a CapToken (and, for a private volume, a CBSS-sealed DEK) per mount, scoped to the mount's `volume`, `mode`, and `path_prefix` and never broader than the relevant grant — the same issuance path that attaches volumes to a job:

- `owner = "actor"`: issued under the actor principal, bounded by the actor's `runner_bindings` (§7.5).
- `owner = "caller"`: issued under the actor principal for the **authenticated caller's** volume — valid only because the caller granted the agent actor mount access (§7.5) and the runner verified the caller's signature. Writes commit back to CBFS (advancing the volume `manifest_root`) as normal, leaving the data owned by the caller.

**Request body and execution.** Consistent with current Gateway behavior, v0 **buffers the request body** (subject to the `ingress.http` `max_request_bytes` cap) before forwarding — this is also what lets `body_sha256` bind the signed request. The runner executes the `handler` off-chain (no consensus, no deterministic-PVM constraint: LLM inference, outbound HTTP, streaming) within `timeout_ms`, and the Gateway **streams the response** back as it arrives. Streaming *request* bodies (with a `stream_session_id`-bound signature) are deferred. On `timeout_ms` expiry the Gateway returns `504 Gateway Timeout`; on a runner transport/handler error, `502 Bad Gateway`.

**Payment.** A resolved client-paid model is gated exactly as for `Method` (§6.6): the Gateway enforces CIP-18 (`402`) before forwarding. Metering of the off-chain work itself (tokens, wall-time) is a runner/RAS concern, out of scope here.

**CORS.** Runner-target CORS follows the `Method` rule (§9.4): the runner's response headers are authoritative; the Gateway fills CORS only when the runner sets none.

**Trust (v0).** The runner is trusted to execute the route faithfully — the same trust an actor already places in any runner it dispatches a job to. Verifiable / multi-operator runner serving (attested execution, on-chain settlement) is deferred (§13).

**Reused vs. new.** Reused (ships today): CIP-9 mount-allowlist grants to a principal, dispatcher-issued per-runner CapTokens, and CBSS DEK seal for private volumes. New in this CIP: the `Runner` route target; the Gateway→runner proxy; a route-serving endpoint advertised in RAS registration; a route caller-auth signing scheme (distinct from the payment voucher); and selecting a `caller`-owned volume from the authenticated HTTP caller per request.

### 8.15 Workload-Target Dispatch (Gateway → Workload)

For a `Workload`-target route (resolution §6.6 step 8), the Gateway relays the connection to one long-lived registered workload. There is no per-request selection: the route names a **workload registry record**, and the record names the backend.

**The registry record.** A persistent workload carries a control-plane workload registry record. This CIP consumes exactly three of its fields — `workload_id` (what `Workload.workload` names), `owning_actor` (what the §6.8 validation binds routes to), and `serving_endpoint` (where the Gateway relays) — plus the record's **route eligibility** rule: the Gateway relays only to a workload whose `desired_state` and `observed_status` are both `Running` with a fresh liveness lease; an ineligible workload returns `503`. The initial record carries volume mounts only — secrets and egress capabilities are deferred there, and there is no route-binding field: route binding lives in the actor's `__cowboy/routes` table. A `Workload` target has no `mounts` of its own. `Workload`-target routes are a hard dependency on that registry: no record, no route.

**Actor route binding.** The actor writes a `Workload` target into its proof-backed `__cowboy/routes` table. The Gateway resolves that table through its normal actor-route cache and requires the registry record's `owning_actor` to equal the URL actor at every connection establishment. A route-table update can replace a `Volume` target with a `Workload` target under the same actor URL, without a Gateway configuration edit or restart. The operator may still configure fallback routes for infrastructure-owned cases; their schema is:

```
StaticWorkloadRoute {
  actor:     Address,        // the actor whose URL space this route lives in — resolved from the request
                             // host exactly as actor routes are, BEFORE lookup; bound as the actor for
                             // auth context and actor-funded payment policy lookup
  verb:      Verb,
  path:      string,         // §6.3 pattern
  target:    Workload,       // the §6.2 Workload shape — normative here (see below)
  expected_workload_owner: Address?,  // cross-owner override: when set, MUST exactly equal the resolved
                                      // record's owning_actor (and differ from `actor`); a repointed record
                                      // whose owner no longer matches fails closed
  pays:      Pays,
  price:     string?,        // required when pays = "caller"
  priority:  u16,
  enabled:   bool
}
```

The embedded `Workload` target shape is the same for actor-owned and operator fallback routes. `expected_workload_owner` exists only in operator configuration; an actor-declared route cannot override the registry owner check.

The operator configuration key is `(actor, verb, path)` — a static entry exists *inside a specific actor's URL space*, never as a hostless global. Duplicate keys are invalid. Ownership is validated at every establishment: an actor-declared route MUST resolve only a record whose `owning_actor` equals that actor. For an operator route, absent `expected_workload_owner`, the named record's `owning_actor` MUST equal the entry's `actor`; present, `owning_actor` MUST equal `expected_workload_owner`, which MUST differ from `actor` — the explicit cross-owner form (one actor's URL space fronting another principal's workload), a typed principal rather than a boolean, so a record later repointed to a different owner fails closed instead of inheriting the override (§8.15 tests require the record-swap negative).

**Collisions are rejected, not shadowed, and collision is semantic overlap, not key equality**: an operator static entry collides when its verb set intersects an actor-declared route's verb set (`ANY` intersects every verb) *and* the two path patterns can match any common path (wildcard, parameter, and literal segments considered — `/api/*` overlaps `/api/admin`; `ANY /chat` overlaps `GET /chat`). The Gateway MUST reject such entries when loading configuration against the current actor-route snapshot. Because actor routes can change after startup, it MUST recheck overlap against the current actor-route snapshot before serving any operator fallback candidate. If a newly observed actor route overlaps an already loaded static workload route, the actor-declared route wins and the Gateway MUST reject the conflicting fallback while that actor-route snapshot applies, emitting a high-severity diagnostic identifying both patterns when the fallback is attempted. A stale cache or lookup order MUST NOT let operator configuration override a consensus `Method` route or its payment policy. Both binding modes share the dispatch semantics below, so promoting a static route to an actor-declared one changes where the route lives, not how it serves.

**Request mapping and HTTP/SSE relay.** The registry `serving_endpoint` is a base URL. The Gateway appends the matched request path (after applying `strip_prefix`; preserving a leading `/`) to the endpoint's base-path prefix and forwards the original query string verbatim. It MUST NOT follow backend redirects. It forwards end-to-end request headers, replaces `Host` with the backend authority, removes the standard hop-by-hop headers and every header named by `Connection`, and adds `X-Cowboy-Route-Context`; response headers receive the same hop-by-hop filtering before relay. The original `Origin` is end-to-end and MUST NOT be rewritten. The Gateway forwards `Authorization` byte-for-byte. It MAY parse and validate the `Payment` scheme for CIP-18 gating; other application schemes remain opaque. It MUST NOT log the header value.

For a plain or streamed HTTP exchange the Gateway buffers the request body per §8.14's `max_request_bytes` rule, establishes the backend response within `WORKLOAD_CONNECT_TIMEOUT_MS`, then relays the response body **unbuffered**: each non-empty backend body chunk is yielded downstream as it arrives, without applying the Runner response-byte cap. SSE (`Content-Type: text/event-stream`) is the motivating case: events MUST NOT be accumulated into a complete body, deliberately coalesced, or compressed by the Gateway. `idle_timeout_ms` bounds response-body silence after headers arrive — each non-empty body chunk resets it; on expiry the Gateway drops the body stream. `max_connection_ms`, when set, starts when the route is admitted and hard-caps establishment plus the response body's lifetime. Client disconnect MUST immediately drop the backend response/body, which cancels upstream reads, and release the route's concurrency permit.

**WebSocket relay.** For a valid WebSocket upgrade request the Gateway applies `auth` / `pays` gating, resolves and checks the workload record, and completes the **backend handshake before sending `101 Switching Protocols` to the client**. A backend HTTP rejection (including `401`) is relayed as an HTTP response; timeout and transport failures use the mappings below. The Gateway forwards the client's `Sec-WebSocket-Protocol` offers and returns only the protocol selected by the backend. The Gateway does not negotiate WebSocket extensions: it removes `Sec-WebSocket-Extensions` on both handshakes rather than claiming an extension independently on either leg.

After both upgrades, the Gateway terminates the two WebSocket legs and relays data messages and close semantics in both directions without inspecting application payloads. Message bytes and text/binary opcode MUST be preserved, but fragmentation and masking are hop-local and MAY be re-encoded. Ping and pong are handled hop-locally by each terminated leg (not forwarded, which would create duplicate automatic pong responses) and count as traffic for `idle_timeout_ms`; silence means no frame in either direction. `max_connection_ms` starts at route admission and includes both handshakes. On backend transport death without a close frame the Gateway sends the client close code `1011` and closes; a normal backend close is forwarded, and client disconnect or close closes the backend side. Streaming request bodies outside an upgraded connection remain deferred (§13).

**Caller authentication.** `auth = "account"` follows the §8.14 signing scheme's construction — length-prefixed fields, `keccak256`, secp256k1 recovery, `chain_id`/`network` binding, expiry + single-use nonce — but under a **distinct fixed domain tag** unique to workload route auth: the literal ASCII bytes `"cowboy/cip15/workload-route-auth/v1"`, pinned here so independent client and workload implementations can produce matching golden vectors (§8.15 tests require them, alongside the cross-target negatives). The workload record id (32 bytes) occupies the slot `handler` occupies in §8.14; for a body-less upgrade request `body_sha256` is the hash of the empty body. The distinct tag means a signature minted for a `Runner` route can never validate on a `Workload` route or vice versa, even if a handler string collides with a workload id — the two targets have separate verifiers and nonce stores, so shared-domain reuse would replay once in each (§8.15 tests require the cross-target negative vector).

The Gateway forwards `X-Cowboy-Route-Context: base64url(CBOR(WorkloadRouteContext))`, where `WorkloadRouteContext` contains `actor_address: bytes20`, `workload: bytes32`, `verb: string`, the forwarded `path: string`, `path_params: list<(string,string)>`, `auth`, optional `payment_proof`, optional `account_sig`, optional `account_expiry`, optional `account_nonce`, `chain_id: u64`, and `network: string`. It has no `handler` or `mounts` fields. `payment_proof` is only raw client `X-Payment` header material when supplied; it is not an authenticated payment result and may be absent even when another payment credential was accepted. The **workload** verifies the signature and self-enforces `auth` from this context — the Gateway's complete-field presence check is defense-in-depth, and the Gateway asserts no caller identity. Per-message authentication inside an established stream is the workload's own concern (it holds the authenticated identity from establishment).

**Payment.** A resolved client-paid model gates **establishment** exactly as for `Method` (§6.6): `402` before the upgrade or first streamed byte. A successful payment gate authorizes establishment of that connection; a pass or subscription may clear it without a new per-connection charge. Metering the stream itself — tokens generated, messages relayed, wall-time — is out of scope here: usage-metered settlement is the streaming-payments protocol, layered above this transport.

**CORS.** SSE and plain HTTP responses follow the `Method` rule (§9.4): the workload's response headers are authoritative; the Gateway fills CORS only when the workload sets none. WebSocket is **not** subject to CORS — browsers do not enforce cross-origin restrictions on WS connections — so an `auth = "public"` WS route is reachable from any origin's JavaScript: the workload MUST validate the `Origin` header itself for browser-facing endpoints (§12.12), and user-bound streams SHOULD use `auth = "account"`.

**Intermediary configuration.** A Gateway deployed behind a load balancer or reverse proxy (ALB, nginx) MUST have that layer configured for long-lived streams, or the layer silently breaks what this section specifies: idle timeout above the workload's heartbeat interval (or the LB kills healthy connections), response buffering disabled on streaming routes (or SSE events arrive in bursts after the fact), and request-body size caps reviewed (an intermediary defaulting to small body caps rejects requests far below the application's own limits — an observed failure class on public RPC endpoints). These are deployment requirements, not protocol parameters.

**Admission, resolution, and errors.** For actor-declared routes, the concurrency key includes actor, verb, path, workload ID, and route limit so concurrent establishments of the same route share a cap; an updated workload binding receives its own cap. Operator fallback entries use their static `(actor, verb, path)` identity. The Gateway acquires a permit before registry resolution or backend dialing and holds it through the complete HTTP body or WebSocket lifetime; handshake attempts therefore count against the cap and cannot form an unbounded slow-establishment queue. At `max_concurrent_streams` (default `DEFAULT_MAX_CONCURRENT_STREAMS`) it returns `503 Service Unavailable` immediately.

For every admitted establishment, the Gateway reads the current registry record and checks: the id exists; `owning_actor` satisfies the rule above; route eligibility is true at the record snapshot's current block; and `serving_endpoint` passes workload-registry validation plus the Gateway's transport policy. Missing, ineligible, owner-mismatched, or invalid records return `503 Service Unavailable` without dialing. Backend DNS/connect/TLS failure or connection refusal returns `502 Bad Gateway`. A backend HTTP or WebSocket handshake not completed within `WORKLOAD_CONNECT_TIMEOUT_MS` returns `504 Gateway Timeout`. After response headers or `101` have been sent, later timeout or transport failure terminates the stream because its HTTP status can no longer be changed.

**Trust.** A workload-served route is exactly as trustworthy as the host running the workload — nothing about relaying through the Gateway adds verifiability. Trust-minimized serving is therefore a **per-route choice**, not a platform property: the same actor's URL space can mix `Volume` routes (content-addressed, verifiable), `Method` routes (consensus), and `Workload` routes (host-trusted). The actor's route authority does not attest to the host's execution; verifiable workload serving is deferred (§13).

---

## 9. CORS

### 9.1 Why CORS Is Specified Here

This CIP is the first specification where browsers will directly consume Gateway responses. Static HTML pages served from one actor's domain will fetch JavaScript, CSS, and API endpoints from the same or other actors. Without CORS headers, browsers block cross-origin requests. Deferring CORS any further would make this CIP unusable for real web applications.

### 9.2 Configuration

CORS is configured via `_meta/cors.json` in the CBFS public volume:

```
CorsConfig {
  rules:  list<CorsRule>    // evaluated in order; first matching path_prefix wins
}

CorsRule {
  path_prefix:       string,           // URL path prefix to match
  allowed_origins:   list<string>,     // "*" permits all origins
  allowed_methods:   list<string>,     // HTTP methods permitted
  allowed_headers:   list<string>,     // request headers permitted
  expose_headers:    list<string>,     // response headers exposed to browser
  max_age:           u32,              // seconds for Access-Control-Max-Age
  allow_credentials: bool              // Access-Control-Allow-Credentials
}
```

### 9.3 Example

```json
{
  "rules": [
    {
      "path_prefix": "/api/",
      "allowed_origins": ["https://myagent.cowboy.network"],
      "allowed_methods": ["GET", "POST", "OPTIONS"],
      "allowed_headers": ["Content-Type", "Authorization", "X-Cowboy-Min-Block"],
      "expose_headers": ["X-Cowboy-Block", "X-Cowboy-Request-Id"],
      "max_age": 86400,
      "allow_credentials": false
    },
    {
      "path_prefix": "/",
      "allowed_origins": ["*"],
      "allowed_methods": ["GET", "HEAD", "OPTIONS"],
      "allowed_headers": [],
      "expose_headers": ["X-Cowboy-Block"],
      "max_age": 86400,
      "allow_credentials": false
    }
  ]
}
```

### 9.4 Default CORS Policy and Precedence

When no `_meta/cors.json` exists, the Gateway applies a **permissive default** for routes whose target is a `Volume`:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, HEAD, OPTIONS
Access-Control-Max-Age: 86400
```

**Rationale:** Static assets in a `Visibility::Public` volume are public by definition. Restricting their CORS policy by default would break most web applications. The permissive default matches CDN behavior (Cloudflare, Fastly, S3).

**Precedence rules:**

- **`Method`-target routes:** the actor's `HttpResponseEnvelope` is authoritative. If the response includes any `Access-Control-*` headers, the Gateway passes them through unmodified and adds nothing. Otherwise, the Gateway applies matching `cors_config` rules. Otherwise, no CORS headers are added. This preserves the actor's authority over its own dynamic-response CORS — a load-bearing security posture for many applications.
- **`Volume`-target routes:** the Gateway applies `cors_config` rules. If no rule matches, the default policy above applies. The actor is not invoked.
- **`Runner`-target routes:** the runner's response is authoritative, exactly as for `Method`. If it sets any `Access-Control-*` headers, the Gateway passes them through unmodified; otherwise the Gateway applies matching `cors_config` rules, else no CORS headers are added. The permissive `Volume` static-asset default is never applied to a `Runner` route.
- **`Workload`-target routes:** the workload's response is authoritative, exactly as for `Method` and `Runner` — pass-through if it sets any `Access-Control-*` headers, else matching `cors_config` rules, else none; the `Volume` default is never applied. WebSocket upgrades are outside CORS entirely (browsers do not enforce it on WS): Origin validation is the workload's responsibility (§12.12).

### 9.5 Preflight Handling

The Gateway handles `OPTIONS` requests for all routes directly, without dispatching to the actor or runner:

1. Match the request path against `_meta/cors.json` rules. The permissive default policy applies only when the matched route's target is a `Volume`; for `Method`, `Runner`, and `Workload` targets, preflight uses `cors_config` rules or, failing a match, no `Access-Control-*` headers — never the `Volume` default and never the backend's response headers.
2. If the `Origin` header matches `allowed_origins` and the `Access-Control-Request-Method` matches `allowed_methods`, return `204 No Content` with the appropriate `Access-Control-*` headers.
3. If no match, return `204 No Content` with no `Access-Control-*` headers (the browser will block the actual request).

This prevents actors from needing to implement preflight handling in any of their handlers, which would require listing `OPTIONS` in the `allowlist_methods` entitlement parameter and waste query-path cycles on a pure CORS check.

---

## 10. Protocol Constants

```
// Routes table
MAX_ROUTES                          = 200             // maximum entries in routes
MAX_ROUTES_SIZE                     = 65_536          // bytes; maximum CBOR-encoded Routes value
MIN_ROUTES_UPDATE_INTERVAL_BLOCKS   = 6               // minimum blocks between routes-affecting commits
MAX_RUNNER_TIMEOUT_MS               = 300_000         // ms; max gateway wait for a Runner-target response
DEFAULT_RUNNER_TIMEOUT_MS           = 60_000          // ms; default Runner-target timeout_ms

// Workload streaming (per-Gateway operator-tunable; the bounds are normative)
WORKLOAD_CONNECT_TIMEOUT_MS         = 10_000          // ms; establishment deadline before 504
DEFAULT_WORKLOAD_IDLE_TIMEOUT_MS    = 120_000         // ms; default Workload-target idle_timeout_ms
MAX_WORKLOAD_IDLE_TIMEOUT_MS        = 3_600_000       // ms; ceiling on route-declared idle_timeout_ms
DEFAULT_MAX_CONCURRENT_STREAMS      = 1_000           // per Workload route when max_concurrent_streams unset; 503 beyond, no queueing
MAX_CONCURRENT_STREAMS_CEILING      = 10_000          // ceiling on route-declared max_concurrent_streams

// Manifest and state caching
MANIFEST_POLL_INTERVAL              = 6               // blocks between manifest_root / state_root checks (~6s)
METADATA_CACHE_TTL                  = 60              // seconds; _meta/* cached between root changes

// Object caching
MAX_GATEWAY_CACHE_BYTES             = 10_737_418_240  // 10 GiB total per Gateway
DEFAULT_MAX_CACHE_PER_VOLUME        = 104_857_600     // 100 MiB default if not set in entitlement

// Static serving limits
DEFAULT_MAX_STATIC_RESPONSE_BYTES   = 10_485_760      // 10 MiB default max single asset
PROTOCOL_MAX_STATIC_RESPONSE_BYTES  = 104_857_600     // 100 MiB hard ceiling

// Fetch optimization
HEDGE_THRESHOLD_MS                  = 100             // ms before issuing hedged shard requests
MAX_CONCURRENT_SHARD_FETCHES        = 8               // max parallel shard requests per object

// CORS
DEFAULT_CORS_MAX_AGE                = 86_400          // seconds for preflight cache (24 hours)
MAX_CORS_RULES                      = 50              // maximum entries in _meta/cors.json
```

---

## 11. Rationale

### 11.1 Why KV Storage for Routes (Not Volume `_meta/`)

Routes are state, not assets. They are small (under 64 KiB), structured, frequently mutated, and tied to actor logic — none of which fits the volume model, which is optimized for byte-large public objects with infrequent atomic batch updates.

Storing routes in `__cowboy/routes` (KV):

- **Lets API-only actors function without a CBFS volume.** An actor that exposes only dynamic methods should not need to provision a public volume just to publish routing rules.
- **Decouples route updates from asset deploys.** Toggling a path, adjusting a price, or activating a maintenance redirect does not require a `commit_manifest` cycle or republishing assets.
- **Reuses the actor's existing state-root commitment for verifiability.** The Gateway reads `__cowboy/routes` via `GET_STATE` (§8.12) and verifies a Merkle proof against the on-chain `Account.state_root`. No new on-chain primitive is required, and no actor execution is invoked on the dispatch path.
- **Permits runtime mutation via narrow typed SDK helpers** (§6.9), matching how operational web apps are actually run — feature flags, gradual rollouts, abuse mitigation, dynamic pricing.

The atomic asset+route property — useful for the cases where you do want a single-commit deploy that updates both — is recovered when needed by writing to `__cowboy/routes` and calling `commit_manifest` in the same block-level transaction.

A briefly considered alternative was an actor `routes()` method called by the Gateway. This was rejected because every dispatch would require PVM execution to read the table, defeating the static-serving performance goal. Reading a Merkle-verified KV value has the same Gateway-side cost as reading the volume manifest does today (one RPC, one verification, cached).

### 11.2 Why Extend `ingress.http` Instead of a New Entitlement

Routes are a mode of HTTP ingress, not a fundamentally different capability. The actor still receives HTTP requests — some are dispatched to named handlers, some served from a volume. A separate `ingress.static` entitlement would require checking two entitlements on every request and complicate the Gateway's dispatch logic. Extending `ingress.http` keeps the parameter space unified.

### 11.3 Why Polling-Based Cache Invalidation

Relay Nodes and Runners do not support push notifications for cached state. Subscribing to on-chain events for every cached volume and actor creates scaling concerns as the number of active actors grows. Polling each cached object's anchor — `StorageCommitment.manifest_root` for volumes, `Account.state_root` for routes — is cheap (a single 32-byte read per anchor per `MANIFEST_POLL_INTERVAL` blocks) and sufficient. The resulting invalidation latency (up to ~6 seconds) is acceptable for both asset deploys and route updates.

### 11.4 Why Include CORS

This CIP is the first specification where browsers will directly consume Gateway responses at scale. Without CORS headers, a static HTML page served from `myapp.cowboy.network` cannot load JavaScript from `api.cowboy.network` or fetch data from its own `/api/` endpoints if they are on a different subdomain. Deferring CORS to yet another CIP would make this one impractical for real web applications.

### 11.5 Why Permissive CORS Default for Static Assets

Public volume assets are, by definition, publicly readable. Any party can fetch them from Relay Nodes directly without authentication. Restricting CORS origins by default would create a mismatch: the data is public, but browsers cannot access it. This matches the behavior of every major CDN and static hosting provider.

### 11.6 Why Actor-Wins CORS Precedence on Method-Target Routes

For `Method`-target routes, the actor's response envelope is authoritative for CORS. The Gateway only fills in CORS headers when the actor sets none. Reversing this — letting `cors_config` override actor-set headers — would silently strip security-critical CORS that the actor may consider load-bearing (e.g., narrowing `Access-Control-Allow-Origin` to a single trusted origin for a specific endpoint).

### 11.7 Why `b3_` ETag Prefix

ETags are opaque strings in HTTP, but clients and intermediary caches may compare them. The `b3_` prefix:

- Distinguishes BLAKE3 hashes from MD5, SHA-256, or other ETag formats used by other servers.
- Enables clients that understand BLAKE3 to verify content integrity end-to-end by stripping the prefix and comparing against `BLAKE3(response_body)`.
- Avoids collision with weak ETags (which use the `W/` prefix per RFC 7232).

### 11.8 Why a `Runner` Target (Off-Chain Compute Belongs Off Consensus)

The deterministic PVM cannot run an LLM, call an external API, or stream a long response — and even where it could, doing so would force non-deterministic or heavy work through consensus, metered against query cycles and bounded by block time and the RPC body cap. Actors already work around this by dispatching off-chain *jobs* to runners; the `Runner` target makes that a first-class **route**: the Gateway proxies straight to the runner, the body never enters a transaction, and the response streams back. It reuses the runner's existing execution and mount machinery (and its secp256k1 recovery utilities) rather than inventing a parallel one — the route caller-auth signing scheme itself is new (§8.14) — and keeps a clean split: `Method` for anything that must be consensus, `Runner` for anything that must not.

---

## 12. Security Considerations

### 12.1 Cache Poisoning

**Threat**: A compromised Relay Node serves corrupted shards, causing the Gateway to cache and serve incorrect content.

**Mitigation**: The Gateway verifies every layer of the integrity chain (§8.5–§8.6):

- Manifest against on-chain `manifest_root` (Merkle root).
- Each shard against its `shard_hash` (BLAKE3) from the verified ObjectDescriptor.
- Reconstructed object against its `content_hash` (BLAKE3).

To poison the cache, an attacker would need to produce a BLAKE3 collision (computationally infeasible with 256-bit output) or forge the on-chain `manifest_root` (requires consensus compromise).

### 12.2 Volume Impersonation

**Threat**: An actor declares `static_volumes` pointing to a volume it does not own, serving another account's data under its own domain.

**Mitigation**: Deployment-time validation (§7.3) ensures each `volume_name` references a `Visibility::Public` volume owned by the deploying account. The `volume_id` is deterministic per CIP-9 §11.1: `keccak256(account_address || volume_name)`. Cross-account references are impossible because the `account_address` component differs.

### 12.3 Large Asset DoS

**Threat**: An actor stores very large objects in a public volume and drives traffic to them, exhausting Gateway resources.

**Mitigations**:

- `max_static_response_bytes` caps individual object sizes (default 10 MiB, ceiling 100 MiB). Objects exceeding this limit return `413 Content Too Large`.
- `max_cache_bytes` per volume caps Gateway cache consumption.
- `MAX_GATEWAY_CACHE_BYTES` caps total cache across all volumes.
- CIP-14 rate limits apply: `MAX_REQUESTS_PER_SECOND = 100` per actor per Gateway.
- CIP-9 storage billing makes hosting large volumes expensive for the account owner.

### 12.4 Stale Manifest

**Threat**: A Gateway serves content from an old manifest after the volume owner has deployed an update (e.g., serving a version with a known security vulnerability).

**Mitigation**: `MANIFEST_POLL_INTERVAL` (6 blocks) bounds the maximum staleness to ~6 seconds. The `X-Cowboy-Block` and `X-Cowboy-Manifest-Root` response headers tell the client which block and manifest were used. Clients requiring strict freshness can include `X-Cowboy-Min-Block` (CIP-14 §8.3) or `X-Cowboy-Manifest-Root` (§8.8) request headers; mismatch yields `409 Conflict`.

### 12.5 Routes Table Manipulation

**Threat**: A malicious or compromised actor rewrites its routes table — at deploy time or at runtime — to redirect handler-served paths to a `Volume` target (bypassing authentication logic), to change the fallback payment gate from `pays = caller` to `pays = actor` when no enabled `price_table` applies, or to point a route at a different handler than expected.

**Mitigation**: The routes table is authored and mutated by the actor owner. If the routes table is malicious, the actor owner is the attacker, which is outside the protocol's threat model. The protocol cannot protect users from malicious actors they choose to interact with. Several invariants nonetheless hold:

- `/_cowboy/*` paths are always Gateway-intercepted (CIP-14 §8.6) regardless of the routes table.
- `Volume` targets can only reference volumes in the actor's `static_volumes` entitlement bindings (§6.8, §7.3). Cross-account volume references remain impossible.
- `MIN_ROUTES_UPDATE_INTERVAL_BLOCKS` rate-limits routes-affecting commits, preventing an actor from churning gateway caches faster than ~1 update per 6 seconds.
- Validation (§6.8) rejects routes tables that are empty, oversized, reference unknown volumes, or set `pays = caller` without a `price`. On rejection, the Gateway falls back to all-CIP-14 dispatch with the previously-cached table.

### 12.6 Routes Cache Poisoning

**Threat**: A compromised Runner serves a tampered routes value via `GET_STATE`, causing the Gateway to dispatch requests according to attacker-controlled routes.

**Mitigation**: The Gateway verifies every `GET_STATE` response against the on-chain `Account.state_root` using the supplied Merkle proof (§8.12). A response that does not verify is rejected; the Gateway tries another Runner. To poison the cache, an attacker would need to forge the on-chain state root (consensus compromise) or produce a Merkle proof collision (computationally infeasible).

### 12.7 Free-CDN Externality on Delinquent Storage

**Threat**: A Gateway continues to serve cached objects for a volume whose owner has stopped paying storage fees, externalizing bandwidth onto Relay Nodes that are no longer compensated.

**Mitigation**: §8.13 ties Gateway serving authority to the volume's CIP-9 status. Serving terminates at `DELETED` and `GARBAGE_COLLECTING`. The `GRACE_PERIOD` window is bounded by `STORAGE_GRACE_EPOCHS`, beyond which the volume transitions to `DELETED` regardless of cache state.

### 12.8 CORS Misconfiguration

**Threat**: An overly permissive CORS configuration (`allowed_origins: ["*"]` with `allow_credentials: true`) could enable credential-leaking cross-origin attacks.

**Mitigation**: Gateways MUST reject CORS configurations where `allowed_origins` includes `"*"` and `allow_credentials` is `true` — this is explicitly forbidden by the CORS specification (the browser would reject it anyway, but the Gateway should catch it at validation time). If detected, the Gateway falls back to the default CORS policy.

### 12.9 Runner-Target Trust

**Threat**: A selected runner executes a `Runner`-target route dishonestly — returns a wrong result, leaks the request, or misuses a mounted volume.

**Mitigation (v0)**: The runner is trusted to execute the route faithfully — the same trust an actor already extends to any runner it dispatches a job to. The blast radius is bounded by what the route authorizes: a runner may only mount volumes named in the actor's `runner_bindings` (`owner = "actor"`) or a volume the **caller** granted it (`owner = "caller"`), each under a scoped CIP-9 mount-allowlist grant and, for private volumes, a per-job CBSS-sealed DEK. The Gateway is untrusted transport and asserts no result authority. Verifiable runner execution (attestation, fraud proofs) and on-chain settlement of off-chain work are deferred to a follow-on CIP (§13).

### 12.10 Caller Authentication and the Untrusted Gateway

**Threat**: For an `auth = "account"` route, a malicious Gateway forges or strips the caller's identity to reach a handler as the wrong account (e.g., to mount another user's `caller`-owned volume).

**Mitigation**: Caller authentication is performed by the **runner**, not the Gateway. The runner verifies the caller's secp256k1 signature (`ecrecover`) over the request and binds the `caller`-owned mount to that verified account. A Gateway cannot forge a signature it does not hold, and a stripped signature simply yields `401`. Confidential request bodies SHOULD be encrypted to the runner so the Gateway sees only ciphertext (mechanism out of scope).

### 12.11 Streaming Connection Exhaustion

**Threat**: Long-lived `Workload` connections are held open en masse (slow-loris on upgrade requests, idle websockets, abandoned SSE streams), exhausting Gateway or workload connection capacity that request-scoped timeouts never reclaim.

**Mitigation**: Every established stream is bounded by `idle_timeout_ms` (silence in both directions tears it down) and optionally `max_connection_ms`; establishment is bounded by `WORKLOAD_CONNECT_TIMEOUT_MS`; per-route concurrency is capped at `max_concurrent_streams` with `503` beyond it — no queueing, so a flood cannot build unbounded backlog. A resolved client-paid policy gates establishment.

### 12.12 Cross-Origin WebSocket Hijacking

**Threat**: Browsers do not apply CORS to WebSocket connections, so any origin's JavaScript can open a WS to an `auth = "public"` `Workload` route and act with the visiting user's ambient context.

**Mitigation**: The workload MUST validate the `Origin` header on browser-facing WS endpoints and reject unexpected origins. User-bound streams SHOULD require `auth = "account"`, which binds establishment to a caller signature no foreign origin can produce. The Gateway relays `Origin` verbatim and never rewrites it.

### 12.13 Workload Endpoint Hijack

**Threat**: An attacker redirects a `Workload` route's traffic by tampering with the serving endpoint or substituting another workload ID, turning a trusted route into an attacker-controlled backend.

**Mitigation**: The registry record is the single binding authority for the endpoint. Current Node registry writes require a trusted system deployer; `owning_actor` binds the workload to an actor but does not itself authorize registry updates. The Gateway resolves the endpoint from the record at connection time and never accepts an endpoint from request data. An actor-owned route can name only a workload whose registry owner is that actor; a wrong-owner ID fails closed. Operator static configuration for first-party workloads is operator-trusted by definition. Gateway↔workload transport SHOULD be private-network or mutually authenticated so a repointed DNS name alone is not sufficient.

---

## 13. Future Work

| Item | Scope | Status |
|------|-------|--------|
| **Pre-Compressed Assets** | Store `.gz` and `.br` variants alongside source files in the volume. Gateway selects the pre-compressed variant matching `Accept-Encoding`, avoiding on-the-fly compression CPU cost. | Deferred to a follow-on CIP. |
| **Small Object Inlining** | For objects under a threshold (e.g., 64 KiB), store the object directly in its manifest entry rather than erasure-coding into K+M shards. Eliminates multi-Relay-Node fetch overhead for tiny files. | Deferred. Requires CIP-9 manifest schema extension. |
| **Image Optimization** | Gateway-side image resizing, format conversion (WebP/AVIF), and responsive `srcset` generation. | Deferred to a follow-on CIP. |
| **Range Requests** | Support `Range` header for partial content delivery (`206 Partial Content`). Required for video streaming and large file downloads. | Deferred to a follow-on CIP. |
| **Gateway Cache Warming** | Protocol for Gateways to proactively fetch and cache objects from newly-deployed volumes before the first user request arrives. | Deferred. |
| **CDN Peering** | Integration with external CDN providers (Cloudflare, Fastly) for edge caching beyond the Gateway network. | Deferred. |
| **Caller-Pays Static Downloads** | Allow `Volume`-target routes with `pays = caller` for paid public-volume content (premium downloads). | Deferred. |
| **Per-Read Bandwidth Accounting** | Actor-funded bandwidth budgets that compensate Relay Nodes for read traffic in proportion to actual serving volume. | Deferred. |
| **Verifiable / Multi-Operator Runner Serving** | Attested `Runner`-target execution and on-chain settlement of off-chain work, so a route can be served trustlessly by an open runner market rather than a trusted operator. | Deferred to a follow-on CIP. |
| **Streaming Request Bodies to Runners** | `Runner`-target routes that stream the *request* body (large uploads, bidirectional streams), with a `stream_session_id`-bound caller-auth signature in place of `body_sha256`. | Deferred. |
| **Verifiable Workload Serving** | Attested `Workload`-target execution, so a streaming route can be served by an untrusted host with a verifiable claim. | Deferred; per-route trust is explicit until then (§8.15). |

---

## 14. Backwards Compatibility

This CIP is fully backwards compatible with CIP-14:

- Actors without a `__cowboy/routes` value in their KV state are unaffected. Gateways fall back to all-CIP-14 dispatch — every request reaches the `http.request` handler. This is the migration path for existing actors: they continue to work, and they opt into routes-driven dispatch by writing to `__cowboy/routes` (typically via the SDK's deploy step).
- The new entitlement parameters (`static_volumes`, `max_static_response_bytes`) are optional. When absent, defaults preserve baseline behavior: `static_volumes = []` (no `Volume`-target routes can be served) and `max_static_response_bytes = 10_485_760`.
- The new `runner_bindings` entitlement parameter is optional and defaults to `[]` (no `Runner`-target routes can be served). Actors opt in by declaring bindings and writing `Runner`-target routes; existing actors are unaffected.
- `Workload` targets are additive. Actors opt in by writing a `Workload` target to `__cowboy/routes`; existing actors and routes are unaffected. Operator static routes remain available as a fallback, but actor deployments require no per-actor Gateway configuration or restart.
- Existing `Visibility::Public` volumes gain no new behavior unless an actor explicitly references them in `static_volumes` and writes a routes table that mounts them.
- The `_meta/cors.json` file is optional. Volumes without it use the default CORS policy (§9.4).
- Gateway nodes must be upgraded to support routes-driven dispatch. Gateways that have not been upgraded will ignore `__cowboy/routes` and dispatch all requests to the `http.request` handler — safe degradation with no data loss or protocol violations.

---

## 15. Host Resolution and HTTP Envelopes

### 15.1 Actor resolution

The operated Gateway resolves an actor using its configured `resolution.mode`:

| Mode | Resolution order |
|------|------------------|
| `host` | Exact `host_to_actor` mapping, then an actor address parsed from the leftmost DNS label. |
| `path` | Actor address parsed from the first URL path segment. The actor receives the remaining path. |
| `both` | Host resolution first, then path resolution if the host did not resolve an actor. |

For host resolution, the Gateway strips the optional port from the `Host` header before looking up the static map. A configured mapping takes precedence over actor-address subdomain parsing. The operator supplies mappings such as:

```toml
[resolution]
mode = "host"

[resolution.host_to_actor]
"shop.example.com" = "0x9b80c2fbc7b277e2cc12a2b4c50386218fa0ed1c"
```

DNS records and TLS certificates are operator responsibilities. Host resolution does not query an on-chain naming registry, register first-party TLD labels, or perform a protocol-managed domain-attachment or reverification flow. These features are outside this CIP's scope.

### 15.2 HTTP envelopes

The Gateway's Method-target dispatch uses the following request and response shapes (`gateway-types::HttpRequestEnvelope` and `HttpResponseEnvelope`). This section is authoritative for these envelope fields.

```text
HttpRequestEnvelope {
  method:       string,
  path:         string,
  path_params:  map<string, string>,
  query:        string,
  headers:      list<(string, string)>,
  body:         bytes,
  host:         string,
  request_id:   string
}

HttpResponseEnvelope {
  status:       u16,
  headers:      list<(string, string)>,
  body:         bytes
}
```

`path_params` contains named parameters extracted from the matched route (§6). Headers are pairs so repeated header names can be represented. The envelope is CBOR-encoded for actor dispatch; `body` is a byte string.

`host` is request metadata, not proof of domain ownership or tenant authorization. The envelope contains no protocol-attested `verified_fqdn` or `namespace_kind`. Actor authorization MUST use the application's authenticated identity and policy rather than treating a hostname as a domain-control attestation.
