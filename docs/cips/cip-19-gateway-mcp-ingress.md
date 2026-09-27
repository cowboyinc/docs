---
title: "CIP-19: Gateway MCP Ingress (r2)"
description: Expose every CIP-14 actor as a Model Context Protocol (MCP) server through the Gateway. Tools are auto-derived from the CIP-15 routes table, tool calls dispatch to the same handlers as the equivalent HTTP request, and payment gating reuses CIP-18 wire formats over MCP's JSON-RPC transport.
icon: plug
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-04-28
  **Updated:** 2026-05-11 (r2)
  **Requires:** CIP-14 v2 (DNS-Addressable Actors), CIP-15 v2 (Public Asset Hosting and HTTP Routing), CIP-18 (Payments — §12 normatively defers to CIP-18 §13 for payment wire format and `_meta` field semantics)
</Note>

> **Revision history**
>
> - **r2 (2026-05-11)** — **CIP-18 elevated from Companions to Requires.** §12 (Authentication and Payment) normatively defers to CIP-18 §13 for the MCP JSON-RPC payment wire format, `_meta.payment-authorization` carrier, and `-32402` error mapping; without CIP-18, §12 has no implementable definition. The current CIP-18 header retains CIP-19 as a native-interface dependency for §13 MCP gating.
> - **r1 (2026-04-28)** — Initial draft.


> **Payment release contract:** HTTP and MCP MUST share CIP-18 purchase identity, confirmation-before-dispatch and authenticated saved-result recovery. `202` is an acknowledgement, not completion. Unknown execution MUST NOT trigger another paid invocation. Actor-funded MCP retries are bound to the original server-issued session; a purchase ID alone is not result-access authorization. Paid 101 upgrades and resumable SSE remain outside this release. See [CIP-18 recovery](./cip-18-payments#61-failure-and-recovery-contract).

## 1. Abstract

This proposal defines **Gateway MCP Ingress** — a system for exposing every CIP-14 actor as a [Model Context Protocol](https://modelcontextprotocol.io) server, terminated at the Gateway. The core primitive is a new entitlement (`ingress.mcp`) that opts an actor into MCP exposure, and a Gateway behavior that:

- Hosts an MCP server at `https://<actor>.cowboy.network/_cowboy/mcp` using MCP's streamable HTTP transport (spec version `2025-11-25`).
- Auto-generates the `tools/list` response from the actor's CIP-15 routes table — every Method-target route becomes one MCP tool.
- Translates `tools/call` invocations into the same actor dispatch (query path or command path) that the equivalent HTTP request would have used. Actor handlers are unchanged; they do not know whether the invocation arrived as HTTP or MCP.
- Applies CIP-18 payment gating over MCP's JSON-RPC transport extension. Payment challenges ride JSON-RPC error code `-32402`; credentials and receipts ride the MCP `_meta` field.

This CIP intentionally defers the following:

- MCP **resources** and **prompts**. v1 is tools-only.
- Streaming tool results. SSE-style streaming is acknowledged but deferred to a follow-on CIP.
- Custom tool name remapping. Tool names are derived mechanically from route method names.
- Server-initiated notifications and sampling.

---

## 2. Motivation

CIP-14 made Cowboy actors HTTP-addressable. CIP-15 added verb-aware routing and per-route payment policy. Together they let agents call actor handlers as a familiar REST API.

**MCP changes the audience.** The Model Context Protocol is becoming the standard interface between LLM agents and external tools. Every major agent runtime (Claude Code, Anthropic API tool use, OpenAI Agents SDK, Microsoft Copilot Studio, the Anthropic SDK) speaks MCP. An agent that knows MCP can pick up *any* MCP server and start using its tools — without per-server SDK glue. By contrast, an agent that only knows HTTP must be told the API contract.

Cowboy actors are an unusually good fit for MCP:

1. **Verifiable tools.** An MCP tool backed by a Cowboy actor has on-chain, deterministic code. An agent can audit what the tool actually does before calling it.
2. **Natively payable.** Combined with CIP-18, an MCP tool call can require payment up front. Agents that already speak the [JSON-RPC payment transport](https://github.com/tempoxyz/mpp-specs/blob/main/specs/extensions/transports/draft-payment-transport-mcp-00.md) Just Work.
3. **Self-sovereign.** No hosting provider, no API key issuance, no DNS acrobatics. Deploy an actor, grant `ingress.http` and `ingress.mcp`, and you have a payable MCP server reachable at `<actor>.cowboy.network/_cowboy/mcp`.
4. **Discoverable.** The CIP-18 OpenAPI document and the MCP `tools/list` come from the same source — the routes table — so agents can discover Cowboy actors through whichever surface they prefer.

The alternative — making every Cowboy actor that wants MCP exposure ship its own MCP server — is a non-starter. It defeats the architectural promise of CIP-14 (the Gateway is the edge; runners are private compute) and forces every actor author to learn MCP transport mechanics that the Gateway can handle once.

---

## 3. Design Goals

- **Zero actor opt-in for the wire.** An actor author declares routes (CIP-15). The Gateway exposes those routes as MCP tools. The handler code is unchanged.
- **One source of truth.** Tool list is derived from the routes table. The CIP-18 OpenAPI document is generated from the same data. Skew is impossible.
- **Same dispatch as HTTP.** A `tools/call` invocation dispatches through CIP-14's existing query/command paths. No new actor-side execution model.
- **Payment via CIP-18.** MCP gating reuses CIP-18 wire formats over MPP's JSON-RPC transport. No new payment code path.
- **Gateway-terminated, not runner-terminated.** Mirrors CIP-14: the Gateway is the public edge, runners are private compute.
- **Standard MCP wire.** The Gateway speaks unmodified MCP `2025-11-25` over streamable HTTP. Any conformant MCP client works.

## 4. Non-goals

- **MCP resources and prompts.** Tools only in v1. CIP-15 volumes could plausibly back resources in a future revision.
- **Streaming tool results.** SSE / chunked tool results are deferred; v1 returns one response per call.
- **Server-initiated requests.** MCP supports server → client `sampling/createMessage` and `notifications/*`. v1 does not implement these.
- **Tool name customization.** Tool names are derived from the CIP-15 route's `target.name`. A future CIP may add an explicit `mcp_name` override.
- **MCP authentication outside CIP-18.** OAuth, API keys, and other MCP auth modes are out of scope. Payment is the only gating mechanism in v1.
- **Actor-side MCP **client**.** This CIP is about actors **as** MCP servers. Actor consumption of external MCP servers (the existing `runner-mcp` crate) is unrelated.

---

## 5. Definitions

- **MCP**: The Model Context Protocol, version `2025-11-25` (the spec dated 2025-11-25 on modelcontextprotocol.io). Uses JSON-RPC 2.0 framing.
- **Streamable HTTP transport**: MCP's HTTP-based transport mode (replaces the older HTTP+SSE transport from earlier MCP revisions).
- **Tool**: An MCP-discoverable, callable function with a JSON-Schema-described input and a structured output. In Cowboy, a tool corresponds 1:1 with a Method-target route from CIP-15.
- **Tool name**: The string identifying a tool. Derived from `route.target.name` per §10.
- **MCP `_meta` field**: A reserved JSON-RPC field for protocol extensions. Used by CIP-18 to carry payment credentials and receipts.

---

## 6. Architecture

```
                  ┌──────────────────────────────────────────┐
                  │           MCP client (agent)             │
                  └────────────┬─────────────────────────────┘
                               │ JSON-RPC over streamable HTTP
                               ▼
       https://<actor>.cowboy.network/_cowboy/mcp
                               │
                  ┌────────────▼─────────────────────────────┐
                  │              Gateway                     │
                  │                                          │
                  │  • terminates MCP                        │
                  │  • generates tools/list from routes      │
                  │  • enforces payment (via CIP-18 §13)     │
                  │  • translates tools/call → dispatch      │
                  └────────────┬─────────────────────────────┘
                               │  same dispatch as HTTP
                               │  (CIP-14 query/command path)
                               ▼
                  ┌──────────────────────────────────────────┐
                  │              Compute Runner              │
                  │  (actor handler, unchanged)              │
                  └──────────────────────────────────────────┘
```

The Gateway is the only new actor in this picture. Runners and actor handlers are unchanged.

---

## 7. The `ingress.mcp` Entitlement

```
EntitlementGrant {
    id: "ingress.mcp",
    params: {
        "server_name":            string?,        // overrides default
        "server_instructions":    string?,        // human-readable, shown in initialize
        "tool_name_prefix":       string?,        // optional namespace, e.g. "myapp_"
        "exclude_routes":         [string]?,      // method names not to expose
        "max_tool_input_bytes":   u32?            // governance default if omitted
    }
}
```

| Param | Type | Description | Default |
| --- | --- | --- | --- |
| `server_name` | `string?` | MCP `serverInfo.name` | `<actor>.cowboy.network` |
| `server_instructions` | `string?` | Human-readable hint for clients (`initialize.result.instructions`) | empty |
| `tool_name_prefix` | `string?` | Prepended to every generated tool name | empty |
| `exclude_routes` | `array<string>?` | Method names to NOT expose as tools | `[]` |
| `max_tool_input_bytes` | `u32?` | Limit on encoded `arguments` size | `MAX_TOOL_INPUT_BYTES_DEFAULT` |

**Prerequisites:** the actor MUST also hold `ingress.http` (CIP-14). MCP exposure without HTTP ingress is undefined.

**Composition with CIP-18:** if the actor also holds `payment.gate`, MCP tool calls inherit the route's `pays` policy from CIP-15. `pays = "actor"` routes are free over MCP. `pays = "caller"` routes require payment via §11 below.

---

## 8. MCP Endpoint

The Gateway exposes:

```
https://<actor>.cowboy.network/_cowboy/mcp
```

The endpoint speaks MCP `2025-11-25` over the streamable HTTP transport. It accepts both `POST` (for client→server JSON-RPC requests) and `GET` (for the optional server→client SSE stream that the streamable HTTP transport defines). v1 does not emit server-initiated notifications, so the SSE stream is open-but-empty.

A request to any other method on `/_cowboy/mcp` returns `405 Method Not Allowed`.

The endpoint is reserved per CIP-18 §17 (CIP-14 §8.6 reserves the `/_cowboy/*` prefix generally; the `mcp` sub-path itself is declared by CIP-18 §17). It is intercepted by the Gateway before route resolution; the routes table is not consulted to dispatch the MCP endpoint itself.

### 8.1 Session lifecycle

The streamable HTTP transport assigns a `Mcp-Session-Id` header on the initial `initialize` response. Subsequent requests in the same session MUST include this header. Sessions are terminated by an explicit `DELETE` to `/_cowboy/mcp` or by inactivity beyond `MCP_SESSION_IDLE_TIMEOUT_SECONDS`.

Session state held by the Gateway is limited to:
- The negotiated `protocolVersion` (always `2025-11-25` in v1).
- The client's `clientInfo`.
- The cached routes-table version observed at session start (for consistent `tools/list`).

Session state MUST NOT contain payment state — that is in PaymentGate, not in the Gateway's session memory.

---

## 9. Capability Negotiation (`initialize`)

The Gateway responds to `initialize` with:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-11-25",
    "serverInfo": {
      "name": "<actor>.cowboy.network",
      "version": "<actor-onchain-block-height>"
    },
    "capabilities": {
      "tools": { "listChanged": false }
    },
    "instructions": "<from ingress.mcp.server_instructions, or empty>"
  }
}
```

`capabilities.tools.listChanged: false` reflects that v1 does not emit server-initiated notifications (§4, §8) — the SSE stream is open-but-empty. Clients that need a fresh tool list MUST re-query `tools/list` periodically. Server-initiated `notifications/tools/list_changed` is deferred to a follow-on CIP.

`resources` and `prompts` capabilities are NOT advertised in v1.

`completions` and `sampling` are NOT advertised — Cowboy actors do not initiate model calls back to the client.

---

## 10. `tools/list`

The tool list is generated from the actor's CIP-15 routes table. Generation is deterministic and verifiable.

### 10.1 Generation algorithm

1. Read the routes table at `__cowboy/routes` from the actor's KV state (using the CIP-15 §8.12 Gateway-to-Runner fetch protocol).
2. Filter to routes where:
   - `target.kind == "method"` (Volume targets are not tools)
   - `enabled == true`
   - `target.name` is not in `ingress.mcp.exclude_routes`
3. Group routes by `target.name`. Each unique method name produces one tool.
4. For each group, validate that all routes share a compatible input schema (§10.3). If not, the group is skipped and a warning is logged in the Gateway's structured-error output.
5. Emit one `Tool` entry per group.

### 10.2 Tool entry shape

```json
{
  "name": "<tool_name_prefix><method-name-mcp-safe>",
  "description": "<from OpenAPI summary, or auto-generated from path/verb>",
  "inputSchema": {
    "type": "object",
    "properties": {
      "<path-param-1>": { "type": "string", "description": "Path parameter" },
      "<query-param-1>": { "type": "string", "description": "Query parameter" },
      "body":            { "$ref": "#/$defs/<body-schema>" }
    },
    "required": [...path params...]
  },
  "_meta": {
    "cowboy": {
      "verb":   "POST",
      "path":   "/api/users/{user_id}",
      "pays":   "caller",
      "price":  "0.05 CBY"
    }
  }
}
```

The `_meta.cowboy` block is non-normative metadata that lets MCP-aware Cowboy clients introspect routing details without re-fetching the OpenAPI doc.

### 10.3 Input schema derivation

For each tool, the Gateway constructs the JSON Schema from:

1. **Path parameters** from `route.path` (CIP-15 §6.3). Each `{param}` becomes a required string property.
2. **Query parameters and body** from the actor's OpenAPI document (CIP-18 §14). Path-Item schemas under the matching verb supply property types and descriptions.
3. **No declared schema** → fall back to a permissive `additionalProperties: true` object.

If multiple routes target the same method name (e.g., `GET /users/{id}` and `HEAD /users/{id}` both → `users.get`), the Gateway requires that path parameters and bodies are identical. If they differ, the routes are considered incompatible and the tool is skipped (§10.1 step 4).

### 10.4 Tool name mapping

The MCP tool name is:

```
tool_name = ingress.mcp.tool_name_prefix + sanitize(route.target.name)
```

`sanitize` replaces any character outside `[a-zA-Z0-9_]` with `_`. CIP-15 method names typically use dotted notation (e.g., `users.get`); after sanitization this becomes `users_get`.

Names beginning with `_cowboy` are reserved for system tools (§14). If `tool_name_prefix + sanitize(...)` would collide with a reserved name, the route is skipped.

---

## 11. `tools/call` (Dispatch Contract)

A `tools/call` request maps onto the same actor dispatch as the equivalent HTTP request.

### 11.1 Request shape

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "users_get",
    "arguments": {
      "user_id": "abc123",
      "verbose": "true"
    },
    "_meta": {
      "payment-authorization": "<base64url-mpp-credential>"   // optional, see §12
    }
  }
}
```

### 11.2 Translation to HTTP equivalent

The Gateway:

1. Looks up the route group for `params.name` (reverse of §10.1).
2. Picks the canonical route in the group (highest priority; ties broken by storage order).
3. Substitutes path parameters from `arguments` into `route.path`.
4. Splits remaining arguments into query parameters and body per the OpenAPI schema for the selected verb.
5. Constructs a synthetic `HttpRequestEnvelope` (CIP-14 §8.1) with:
   - `verb` = `route.verb`. A route with `verb == "ANY"` is dispatched as `POST` (the unsafe/command path, §11.3): a `tools/call` cannot signal HTTP safety, so `ANY` is treated conservatively as state-mutating. Actors wanting a safe (query-path, no-consensus, cheaper) MCP tool MUST register the route with an explicit safe verb (`GET`/`HEAD`) rather than `ANY`.
   - `path` = the substituted path
   - `query` = serialized query parameters
   - `body` = serialized body (JSON, unless OpenAPI declares otherwise)
   - `headers` = a synthesized minimal set (`content-type`, `accept`, plus `X-Cowboy-Mcp-Session-Id`)
   - `path_params` = the matched named parameters

### 11.3 Dispatch

The Gateway dispatches the envelope per CIP-14 §8:

- **Safe verbs** (`GET`, `HEAD`): CIP-14 query path via `read_handler`. No transaction, no consensus.
- **Unsafe verbs** (`POST`, `PUT`, `PATCH`, `DELETE`): CIP-14 command path via `GatewayRegistry.dispatch()`. Consensus-required.

Payment enforcement happens before dispatch per §12 below.

### 11.4 Response shape

The actor handler returns an `HttpResponseEnvelope` (CIP-14 §8.2). The Gateway maps it to MCP:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "content": [
      { "type": "text", "text": "<actor response body, decoded>" }
    ],
    "isError": false,
    "_meta": {
      "payment-receipt": "<base64url-mpp-receipt>",         // when paid
      "cowboy": {
        "status":     200,
        "block":      <block_height>,
        "tx_hash":    "<command-path only>"
      }
    }
  }
}
```

Response body interpretation:

- `content-type: application/json` → emit as a single `text` content block with the JSON, AND a sibling `structuredContent` field carrying the parsed JSON (per MCP `2025-11-25`).
- `content-type: text/*` → emit as a single `text` content block.
- Binary types (`image/*`, `application/octet-stream`) → emit as one `image` or `resource` content block per MCP's content-type rules. Bodies exceeding `MAX_TOOL_OUTPUT_BYTES` are truncated and the response is marked `isError: true` with a `truncated` flag in `_meta.cowboy`.

### 11.5 Error mapping

- HTTP 4xx (other than 402): `result.isError = true`, content describes the error, no JSON-RPC error frame.
- HTTP 5xx: JSON-RPC error frame with `code = -32603` (internal error), `message = "Actor handler failed"`, `data` carrying the response body if size-bounded.
- HTTP 402: see §12.
- Network / timeout: JSON-RPC error frame with `code = -32000`, `message = "Gateway dispatch failed"`.

---

## 12. Authentication and Payment

This section defers to **CIP-18 §13** for the wire format. CIP-19 specifies only how it integrates with the dispatch contract.

### 12.1 No payment required

Routes with `pays = "actor"` (CIP-15 default) require no payment. The Gateway dispatches per §11 and pays the actor's hot balance per CIP-14.

### 12.2 Payment required, no credential

Routes with `pays = "caller"` and no `_meta.payment-authorization` in the request: the Gateway returns a JSON-RPC error frame:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32402,
    "message": "Payment Required",
    "data": {
      "challenges": [
        {
          "id": "<hmac-bound-id>",
          "realm": "<actor>.cowboy.network",
          "method": "cowboy",
          "intent": "charge",
          "expires": "...",
          "request": "<base64url(JCS-JSON)>",
          "request_hash": "0x<32-byte-hex>"
        }
      ]
    }
  }
}
```

The `data.challenges` array follows the MPP challenge schema (CIP-18 §9.1/§13.4), with the MCP-specific `request_hash`: because the Gateway synthesizes `method`/`path`/`body` from `arguments` (§11.2), the client cannot reproduce the request bytes to compute the §9.5.1 binding itself, so the challenge carries the `request_hash` the Gateway will enforce. The client signs it; on redemption the Gateway re-derives it from the identical re-sent `arguments` and `verify()` enforces the match (CIP-18 §13.4).

Cowboy MCP gating uses the **MPP carrier** defined by CIP-18 §13.6; there is no `x402_compat` block in this transport. x402 clients are served over the HTTP transport, which advertises x402 challenges directly.

### 12.3 Payment provided

Routes with `pays = "caller"` and a credential in `_meta.payment-authorization`:

1. The Gateway validates the credential through the selected adapter; native Cowboy credentials use PaymentGate (CIP-18 §11).
2. Confirm successful payment or authoritative entitlement consumption before dispatch, for both queries and commands (CIP-18 §6 and §15). A settlement timeout remains pending and is reconciled against the same purchase.
3. On credential verification failure, return a JSON-RPC `-32402` error with a fresh challenge. Do not challenge for a second payment when the first may already have settled.
4. After confirmation, dispatch per §11.3 under the purchase execution identity and include the receipt in `result._meta.payment-receipt`. Paid execution failures follow CIP-18 recovery rules.

### 12.4 Pass and subscription credentials

`intent="pass"` and `intent="subscription"` credentials are carried in the same `_meta.payment-authorization` field. The Gateway evaluates them ahead of `intent="charge"` per the CIP-18 §7.5 fallback chain.

---

## 13. Reserved Tool Names

Tool names beginning with `_cowboy_` (after `tool_name_prefix` is applied) are reserved for system tools the Gateway exposes alongside actor-derived tools. v1 reserves but does not yet implement:

| Reserved name | Description |
| --- | --- |
| `_cowboy_payment_quote` | Get a quote for a hypothetical request without dispatching (mirrors `/_cowboy/payment/quote`). |
| `_cowboy_payment_subscribe` | Purchase or extend an epoch subscription. |
| `_cowboy_pass_purchase` | Purchase a prepaid pass. |
| `_cowboy_health` | Liveness probe (mirrors `/_cowboy/health`). |
| `_cowboy_info` | Actor metadata (mirrors `/_cowboy/info`). |

A future CIP can populate these. v1 simply reserves the namespace so actor tool names cannot collide.

---

## 14. Gateway Integration

### 14.1 New responsibilities

The Gateway gains:

- An MCP server implementation per §8 — streamable HTTP transport, JSON-RPC 2.0 framing, session management.
- A tools-list generator that materializes `Tool` entries from the routes table on session start (and, in a future revision that enables `listChanged`, on `notifications/tools/list_changed`).
- A `tools/call` translator that maps to the existing dispatch path. No new dispatch primitives.
- Hooks into CIP-18's payment enforcement: the JSON-RPC frame inspector for `_meta.payment-authorization`, the error mapper for `-32402`, and the receipt emitter.

### 14.2 No actor-side changes

Actor handler code is unchanged. An actor that already serves HTTP via CIP-14 + CIP-15 becomes MCP-callable by adding the `ingress.mcp` entitlement. The handler does not see a "tool call" — it sees an HTTP request envelope, exactly as today.

### 14.3 Caching

The Gateway caches the per-actor tool list keyed by `(actor, routes_state_root)`. Cache is invalidated when the actor's state root changes (signaled by the same `state_root`-polling cache refresh the Gateway already runs for the route registry (CIP-17 §6.1 verifiable routes fetch; CIP-15 v2.r2 §6 `MANIFEST_POLL_INTERVAL`)).

---

## 15. Reserved Paths

| Path | Method | Description |
| --- | --- | --- |
| `/_cowboy/mcp` | `POST` / `GET` / `DELETE` | MCP streamable HTTP endpoint. Already reserved by CIP-18 §17; this CIP defines its semantics. |

No new reserved paths beyond what CIP-18 already declared.

---

## 16. Protocol Constants

```
MCP_PROTOCOL_VERSION              = "2025-11-25"
MCP_SESSION_IDLE_TIMEOUT_SECONDS  = 600           // 10 min
MAX_TOOL_INPUT_BYTES_DEFAULT      = 1_048_576     // 1 MiB
MAX_TOOL_OUTPUT_BYTES             = 4_194_304     // 4 MiB
JSONRPC_PAYMENT_REQUIRED_CODE     = -32402        // mirrors CIP-18 §19
JSONRPC_INTERNAL_ERROR_CODE       = -32603
JSONRPC_GATEWAY_DISPATCH_FAILED   = -32000
TOOLS_LIST_CHANGED_DEBOUNCE_MS    = 250
```

---

## 17. Security Considerations

- **Tool name collision.** Reserved `_cowboy_*` prefix prevents actor routes from impersonating system tools. Routes that would collide are dropped from `tools/list` (§10.4). The dropped-route warning is observable in the Gateway's structured-error logs.
- **Path-parameter injection.** Path parameters from `arguments` are URL-encoded before substitution into `route.path` (§11.2 step 3). Arguments cannot smuggle additional path segments via slashes or `..`.
- **Payment replay over MCP.** Each native Cowboy payment credential carries a `nonce` consumed atomically by PaymentGate (CIP-18 §9.5.1). Submitting the same credential as both an HTTP retry and an MCP tool call settles once.
- **Cross-session credential reuse.** Payment credentials are bound to `request_hash`; an MCP `_meta.payment-authorization` issued for one tool call cannot be replayed against another.
- **Session hijacking.** `Mcp-Session-Id` is a server-issued opaque token with `MCP_SESSION_IDLE_TIMEOUT_SECONDS` lifetime. Sessions are tied to TLS-terminated connections; the Gateway rejects requests whose `Mcp-Session-Id` came from a different TLS endpoint or expired.
- **Tool argument size.** `MAX_TOOL_INPUT_BYTES` (governance default `1 MiB`) bounds attacker payload size before any actor dispatch occurs.
- **Tool output size.** `MAX_TOOL_OUTPUT_BYTES` bounds the response body. Truncation is signaled in `_meta.cowboy.truncated`.
- **Unbounded tools/list.** The MCP `tools/list` response is bounded by the routes table's `MAX_ROUTES` (CIP-15 §10). Pagination is not required.
- **Server-initiated requests are disabled.** v1 does not advertise `sampling` or `notifications/*` capabilities. A malicious client cannot induce the Gateway to call back into it.
- **Schema mismatch DoS.** Routes that target the same method name with mismatched schemas are dropped (§10.1 step 4) rather than producing inconsistent tools. An attacker cannot poison `tools/list` by registering a conflicting low-priority route, because the Gateway picks the canonical route deterministically.
- **Information leakage in errors.** HTTP 5xx response bodies are size-bounded before inclusion in JSON-RPC `error.data`. Actors that return secrets in 5xx bodies have a CIP-14 problem, not a CIP-19 problem; this CIP does not amplify the leak.
- **Prompt injection.** Tool-call arguments arrive as opaque strings from the agent's perspective. The Gateway does not interpret tool descriptions or arguments as prompts. Prompt-injection risks live in the agent runtime and are out of scope for this CIP.

---

## 18. Rationale

**Why Gateway-terminated MCP, not runner-terminated?** The Gateway is the public, DNS-addressable, payment-enforcing edge. Runners are private compute. Putting MCP on runners would require (a) exposing them publicly, (b) duplicating CIP-18 payment enforcement, (c) forcing actors to opt into "I'm an MCP server" beyond just declaring routes. Gateway termination preserves CIP-14's architectural separation and makes MCP exposure free for any actor that already has HTTP routes.

**Why derive `tools/list` from the routes table?** A separate MCP-tool declaration would create skew between HTTP and MCP surfaces. CIP-15's routes table is already the authoritative description of "what this actor exposes." Deriving the OpenAPI doc (CIP-18 §14) and the MCP tool list from the same source means an agent that uses HTTP and an agent that uses MCP see the same actor.

**Why one tool per method name, not one per route?** Routes are a transport concern (verb × path → handler). Tools are a logical concern (named callable). Multiple routes can target the same method (e.g., `GET /users/{id}` and `HEAD /users/{id}` both → `users.get`); collapsing them into one tool matches the conceptual layer MCP operates at.

**Why version-pin to MCP `2025-11-25`?** It is the current (as of 2026-04-28) MCP spec, has the streamable HTTP transport, and has stable `_meta` semantics required by MPP's JSON-RPC payment transport. Earlier versions lack `_meta`; later versions are unspecified at this CIP's creation. Future CIPs can advance the pin.

**Why not implement MCP resources?** Volume-target routes (CIP-15) are the natural backing for MCP resources (URI-addressable read-only content). Wiring this up requires careful thinking about resource templates vs. concrete URIs and the read-time consistency guarantees clients expect. Deferring keeps v1 small and lets us learn from tools-only deployment first.

**Why not stream tool results in v1?** SSE-style streaming changes the dispatch contract: the actor would need to emit chunks rather than a single envelope. This is conceptually compatible with CIP-7 streams but operationally requires Gateway buffering and cancellation propagation. Out of scope for the first MCP CIP.

**Why a new entitlement instead of riding `ingress.http`?** Some actors may want HTTP exposure but not MCP exposure — for example, web-only frontends, SEO landing pages, or actors whose route handlers were not designed for agent invocation. Making MCP opt-in via `ingress.mcp` lets authors decide explicitly.

---

## 19. Future Work

- **Resources.** Map CIP-15 Volume-target routes to MCP resources, with URI templates corresponding to wildcard paths.
- **Prompts.** Allow actors to declare reusable prompt templates as a separate KV table, exposed via MCP `prompts/list` and `prompts/get`.
- **Streaming.** Bridge CIP-7 streams to MCP streamed tool results.
- **Tool name customization.** An optional `mcp_name` field on Route entries (CIP-15 schema extension) for friendlier names.
- **Sampling and notifications.** If actor-initiated agent interaction becomes a use case, advertise the `sampling` capability and add a runner-side outbound MCP client.
- **MCP version progression.** Pin advancement as the MCP spec evolves. Deprecation policy is governed by CIP-12.
- **System tools (§13).** Implement `_cowboy_payment_quote`, `_cowboy_payment_subscribe`, etc., so MCP-only clients can interact with PaymentGate without a separate HTTP path.

---

## 20. Backwards Compatibility

This CIP is fully additive:

- Actors without `ingress.mcp` are unaffected. No MCP endpoint is exposed.
- Actors with `ingress.mcp` and existing CIP-15 routes need no code changes — the routes table is the source of truth and is already populated.
- The `/_cowboy/mcp` reserved path was reserved by CIP-18 §17. This CIP defines its semantics; it does not introduce a new reservation.
- No changes to CIP-14 dispatch semantics, CIP-15 route schema, CIP-18 PaymentGate, or any other CIP.
- The MCP version pin is a Gateway-side concern; advancing it does not require an actor-side migration.
