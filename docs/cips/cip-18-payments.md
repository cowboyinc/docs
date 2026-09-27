---
title: "CIP-18: Payments"
icon: "credit-card"
description: "Payment acceptance for Cowboy services through x402 and MPP, with separate native settlement and Cloud accounting."
---

# CIP-18: Payments

<Note>

- **Status:** Draft; native confirmation-before-execution has companion implementation PRs awaiting merge and end-to-end acceptance. External USDC and Cloud payouts are separate, incomplete release tracks.
- **Updated:** 2026-09-23
- **Requires (native interfaces):** [CIP-3](./cip-3-fee-model), [CIP-12](./cip-12-governance), [CIP-14](./cip-14-dns-addressable-actors), [CIP-19](./cip-19-gateway-mcp-ingress), [CIP-20](./cip-20-fungible-tokens)
- **Related:** [CIP-14](./cip-14-dns-addressable-actors), [CIP-19](./cip-19-gateway-mcp-ingress), [CIP-28](./cip-28-cowboy-agent-banking)

</Note>

The terms MUST, MUST NOT, SHOULD and MAY define conformance requirements. Only disposable devnet exists; the selected release uses one coordinated native contract, without an old-network migration or parallel legacy execution path. This specification does not authorize a reset or declare deployment.

## 1. Background

A client should be able to pay for a Cowboy agent's service using x402 or MPP. The service may run on Cowboy Cloud even when payment settles on an external chain. Receiving that payment does not inherently require moving the asset onto Cowboy Chain.

Payment acceptance and actor spending are separate concerns. This CIP defines how a service gets paid; CIP-28 defines how an owner authorizes an actor to make purchases. CIP-28 limits do not govern an external customer's wallet, and inbound payments do not wait for an actor allowance system.

## 2. Goal

Deliver one bounded flow: quote an operation, accept a supported payment, confirm settlement, execute the operation, and retain a durable association between payment and result. Support both x402 and MPP without requiring ordinary external USDC clients to implement Cowboy's native signing scheme.

For Cowboy-native payments, retain PaymentGate, request pricing, serving budgets, passes, and subscriptions. For Cloud, add external USDC collection and explicitly defined accounting. These settlement paths share purchase lifecycle requirements, not an invented common on-chain asset transfer.

## 3. Scope

| Included | Excluded |
| --- | --- |
| Native CBY payments through PaymentGate | A general exchange or CBY-to-USDC purchasing service |
| One selected external USDC network and payment integration, exposed through x402 and MPP | A multi-chain bridge or facilitator framework |
| Confirmed payment before paid execution; retry and recovery behavior | Streaming or usage-metered billing inside a request; CIP-7 feed usage receipts |
| Cloud receipt, credit, earned-revenue, and payout accounting boundaries | Credit cards, lending, bank registration, or a general dispute platform |
| Existing pass and subscription behavior | New pass or subscription products |

External network/provider selection and payout integration remain implementation decisions in §22. This CIP does not allocate a new system actor, mint a bridged stablecoin, or introduce a new fee schedule.

## 4. Cloud and Chain

| Concern | Cowboy Cloud | Cowboy Chain |
| --- | --- | --- |
| External USDC payment | Receive on the selected external network and associate the receipt with a service purchase | External receipt is not automatically a Chain balance |
| Native service payment | CBY consumption funding, with Cloud accounting where applicable | Native CBY transfer through PaymentGate |
| Conversion price | Proposed posted price of **$0.15 per CBY** | No fixed USD exchange rate in consensus |
| Owner proceeds | Earned revenue may qualify for Stripe payouts under the approved funds flow | Owners control their assets and may transfer through supported infrastructure |
| Future USDC custody | Does not require a Cowboy bridge for inbound collection | Requires a separately specified asset and bridge integration |

A future bridge moves assets; it does not grant spending authority. An actor's delegated purchasing authority must not bypass CIP-28 through a transfer or bridge call. Owner withdrawals are a separate authority path.

## 5. Purchase identity

A logical purchase binds the service actor, operation, request content, payer, network, asset, amount, recipient, quote expiry, and a stable purchase identifier. Record the selected payment method, settlement evidence, execution result, and any accounting obligations with that purchase.

The payment adapter MUST verify the terms actually authorized by its protocol. Where an ordinary external authorization does not sign Cowboy's request hash, the service MUST durably associate the accepted authorization with exactly one quoted purchase; it MUST NOT pretend the external signature covers additional fields. The chosen adapter must specify this association and prove it with ordinary clients before release.

Identical retries recover the existing purchase. Reusing its identifier with different terms is rejected. Retries through another gateway, another wire format, or after a process restart MUST NOT create another charge, service execution, or credit. A payer may intentionally buy the same operation twice using separate purchase identities.

### 5.1 Native identity and request binding

The coordinator namespace MUST include chain ID and an immutable network/genesis instance identifier shared by serving replicas. A devnet reset uses a new instance; losing a database on the same chain MUST NOT be handled by inventing a new namespace. Restoring a backup older than execution claims requires reconciliation before serving paid work.

The selected native coordinator IDs are lowercase hex Keccak-256 of these raw byte concatenations:

```text
ASCII("cowboy-purchase-v1intent") || payer(20) || asset(20) || nonce(32)
ASCII("cowboy-purchase-v1budget") || actor(20) || client_request_id(32)
ASCII("cowboy-purchase-v1allowancepgp:") || owner(20) || actor(20) || purchase_id(32)
```

The last form is the CIP-28 allowance path. Each is stored within the network namespace above. Native intent IDs derive from the signed payer/asset/nonce, not a client-chosen result-cache key. Actor-funded budget calls MUST carry a client-generated random 32-byte ID as 64 hex characters in HTTP `x-cowboy-purchase-id` or MCP `params._meta["cowboy/purchase-id"]`. The same original client ID MUST be resent after a lost response; the coordinator's returned diagnostic ID is a different value. Allowance purchase IDs follow CIP-28 and are not budget IDs.

The native signed request hash is `keccak256(UTF8(method) || 0x00 || UTF8(path_and_query) || 0x00 || body_bytes)`. HTTP uses the exact effective request target, including query order and encoding, and the exact body bytes; there is no JSON reserialization or query sorting on retry. MCP uses the deterministic synthesized envelope from CIP-19 and the hash returned in its challenge. The gateway MUST recompute that envelope/hash at redemption. Different JSON-RPC transport IDs alone do not identify another operation.

The coordinator MUST additionally bind the selected route, full unsigned authorization/settlement terms and application headers. This is a server-side durable binding, not extra fields secretly covered by a payer signature. Header names are compared lowercase; values remain exact. Authorization Bearer values, cookies and vendor authentication fields remain bound. Payment carrier headers, MCP transport session headers, hop-by-hop/length/host fields and an explicit finite list of tracing/forwarding headers do not enter the common binding. Wildcard exclusion of vendor prefixes is forbidden. The exact current exclusion list and route serialization are pinned in [Gateway #104 `purchase_context` at `09099881`](https://github.com/cowboyinc/gateway/blob/0909988198a895a0dea9ae0cf0f5521c7955a35b/crates/gateway-server/src/lib.rs); changing this persistent binding requires a coordinated release, not replica-local behavior.

Reusing an ID with different bound terms MUST return `terms_conflict` without a second charge or dispatch. MPP and x402 native carriers normalize to the same purchase; simultaneous conflicting credentials reject. A changed route/policy cannot turn an existing paid retry into a fresh free execution. Look up authenticated recovery before applying new-purchase admission rules: a consumed nonce or expired intent may recover its existing result but MUST NOT authorize new work.

### 5.2 Result access, retention and replica recovery

Purchase IDs are public references, not secrets. A saved result MUST require the original payer authorization and bound application context. Actor-funded HTTP creates no user authentication: private routes MUST require and bind application credentials; anonymous routes do not gain confidentiality from an idempotency ID. Opaque credential refresh is a terms change requiring reconciliation, not an excuse to release a saved private result.

Actor-funded MCP has no signed payer credential, so v1 also binds its server-issued session. Another session MUST NOT retrieve its saved result. Expired-session recovery is operator-mediated. A signed native intent may recover across transport sessions while retaining its payer and application binding.

The native release uses a shared durable PostgreSQL purchase record with conditional ownership/version updates. It MUST record signed settlement bytes before submission, attach authoritative confirmation, and atomically claim dispatch. Result bodies are buffered and persisted before a completed response is acknowledged, up to 16 MiB and 300 seconds. Oversized/unreadable bodies leave execution unknown; they do not trigger another invocation. A backend may already have produced side effects.

v1 performs no automatic result/history deletion: signed attempts, identity bindings, dispatch claims, responses and unresolved obligations remain durable for the network instance. Operators MUST monitor storage and reject new paid admissions if durable writes cannot be supported. A future pruning contract must retain enough authenticated identity/consumption evidence to prevent replay; deleting an old result MUST NOT make that purchase executable again. There is no unauthenticated result endpoint.

Replicas MUST share purchase storage and challenge-verification material. Each live signing account MUST have one process owning its transaction nonce state; distinct concurrent signing replicas use distinct accounts. Other replicas may recover committed receipts and results, but pending signed-byte rebroadcast uses the original signing account's nonce custody. The purchase database is not a cross-process signer nonce allocator. The configured validator committed-receipt RPC is a trusted confirmation source, not a cryptographic inclusion-proof interface.

## 6. Settlement lifecycle

1. Resolve the service's payment policy and return a quote/challenge when payment is required.
2. Validate the credential and durably claim the logical purchase before submitting settlement.
3. Settle through the selected adapter and establish successful confirmation under that network's documented confirmation rule.
4. Record the confirmed payment before dispatching paid work.
5. Dispatch the operation once under the purchase's execution identity; persist its outcome and any credit or recovery obligation.
6. Return the result and an accurate receipt. A payment receipt proves payment, not completion of an asynchronous operation.

Signature verification, a balance precheck, a transaction hash, and mempool admission are not confirmation. A settlement timeout is pending until reconciled; it is not permission to execute or to request another payment.

Recovery MUST distinguish unpaid, payment pending, paid but not dispatched, execution pending, completed, and paid execution failure. These are observable outcomes, not a required new protocol enum. A crash after dispatch MUST be reconciled against the execution identifier before another dispatch; recording only that payment succeeded is insufficient to prevent duplicate side effects.

A paid execution failure MUST follow §6.1 before release. Unknown execution is not proven failure. Refunds MUST be idempotent and reconcile with service execution and Cloud credits; this CIP does not create a general dispute system.

### 6.1 Failure and recovery contract

| Observable outcome | Evidence and permitted recovery |
| --- | --- |
| `settlement_pending` | Recorded transaction(s) lack successful committed receipt; no dispatch, no new payment; retry original purchase |
| `settlement_unknown` | Confirmation remains absent beyond the 60-second automatic-wait window; reconcile all recorded attempts; absence/nonce progress is not terminal failure |
| `preparation_failed` | No signed attempt or only durable proven admission refusals; after a fenced 60-second backoff the same purchase may be prepared again |
| `settlement_failed` | All recorded attempts have authoritative committed failures; no dispatch; a missing receipt for any possibly admitted attempt keeps the outcome unknown |
| Paid, not dispatched | Successful committed evidence for exact settlement terms; claim original execution once and continue |
| `execution_unknown` | Dispatch claimed but outcome not durably known; reconcile backend effects; MUST NOT automatically reset the claim, re-execute or refund |
| `execution_pending` | Saved 202/continuation acknowledgement; recover that acknowledgement and track the same operation, not a second paid invocation |
| `responded` | Authenticated replay of the saved response; no new settlement or execution |
| `recovery_required` | Confirmed payment associated with a recorded service error; retain receipt/result for the failure policy below |
| `terms_conflict` | Different terms/context under one identity; reject with HTTP 409 or equivalent MCP error, preserving original record |

Pending/unknown/recovery outcomes use the same purchase reference and structured status. A payment receipt proves payment or entitlement consumption, not successful application completion. An idempotent budget/allowance settlement no-op is not authorization for another execution; the original coordinator record must be recovered. A consumed marker without its corresponding record MUST lead to reconciliation.

A durable Gateway claim alone cannot make arbitrary downstream effects exactly once. A backend with execution-ID lookup MUST be reconciled through that identity. Without such a facility, v1 retains `execution_unknown` for operator reconciliation; it sacrifices automatic recovery rather than risking duplicate effects. There is no lease-expiry redispatch or automatic refund of unknown work.

For v1, a proven failure before any business effect is eligible for a full service-payment refund, or an explicitly accepted recovery of the same operation. Unknown/partial effects require operator review and customer agreement; an HTTP error alone does not prove zero effects. The recipient authorizes/funds refunds, never an unrestricted Gateway debit. CIP-28 supplies the full-total recipient-funded refund primitive. Other native methods require an authorized, recorded refund/reinstatement procedure before their paid-failure release acceptance: pass credits may be reinstated once, budget deductions returned once, and native per-request refunds must be durably associated with the original debit. These are acceptance gaps where no such procedure exists, not newly implemented APIs. Gas already consumed is not automatically refunded. A refund MUST NOT both restore spendable credit and leave the same amount payable as Cloud earnings.

### 6.2 Superseded ordering and unsupported transports

This release replaces the former D1/fire-and-forget rule that settled only after observing a successful response. HTTP status, including fallback 2xx, does not decide whether payment is submitted: confirmation precedes initial paid dispatch. CIP-14 governs routing and continuation responses; this section governs payment ordering and recovery, avoiding a circular delegation of settlement policy.

Paid protocol upgrades/WebSocket 101 and resumable paid SSE are outside this release. The gateway MUST reject unsupported paid upgrade requests before settlement and before opening the backend connection. A stored response MUST NOT be replayed as a new protocol-upgrade handshake. Free transport streaming remains governed by its ingress specification.

## 7. Payment models

### 7.1 Per-request

The client purchases one quoted operation. Settlement is confirmed before execution, for both queries and commands. A runner continuation is part of that logical operation; an initial `202 Accepted` is not proof of continuation completion.

### 7.2 Serving budget

An actor-funded PaymentGate budget subsidizes requests **to that actor**. It is not the outbound purchase allowance in CIP-28. Budget availability must be authoritative before dispatch; a local balance read alone is insufficient under concurrency. No new automatic funding mechanism is specified here.

### 7.3 Passes

A prepaid pass grants request credits for its configured actor and beneficiary. A pass identifier is not a secret or authorization. Redemption requires the applicable payer signature, request binding, expiry and credit checks, and authoritative consumption before dispatch. Preserve the existing purchase and credit model.

### 7.4 Subscriptions

An epoch subscription grants access while its entitlement is active. Authenticate the subscriber and check the committed entitlement for the actor and operation. Purchasing or extending a subscription uses the existing native purchase path; this CIP adds no recurring wallet debit.

### 7.5 Selection

Evaluate an explicitly free endpoint first, then an authenticated active subscription, a valid pass, an available serving budget, and finally per-request payment. Never combine payment paths to charge twice for one operation. Consumed pass credits and budget deductions need the same result/recovery association as a paid request.

## 8. PaymentGate

PaymentGate remains the native system actor at **`0x12`**. Its responsibilities include policy storage, serving budgets, pass and epoch state, native verification and settlement, and nonce replay protection.

Native amounts are integer CBY atomic units: **1 CBY = 10^9 units**. The existing payment structures use `u128` amounts while native account debits require checked conversion to the ledger's `u64` range. An asset field in a codec is not proof of working token settlement; this release's native path supports CBY only.

In the inspected node, policy mutation and budget withdrawal require the authenticated sender to equal the policy actor; budget deposits debit their sender, and deductions require the policy treasury. An owner wallet or keyless actor must use a valid existing authority path. Neither this CIP nor CIP-28 grants authority merely by naming an actor in a payload.

External USDC is settled by its external adapter. `PaymentGate.credit_inbound` was a deferred inbound-bridge proposal and is not implemented at the inspected node revision; it is not part of this Cloud collection flow. Native replay protection is defined in §8.4.

### 8.1 PaymentPolicy and price_table

This section retains the native policy codec referenced by node
[`execution/src/payment_gate/mod.rs` at `44e1eb129`](https://github.com/cowboyinc/node/blob/44e1eb129/execution/src/payment_gate/mod.rs).
It describes existing stored bytes; it does not introduce a migration requirement
or complete the payment-before-execution flow. All integers below are unsigned,
fixed-width big-endian; addresses are 20 raw bytes, and there is no ABI padding.

The existing **v0** value is exactly 117 bytes with no version byte:

```text
actor(20) ‖ enabled(1) ‖ treasury(20) ‖ asset(20)
‖ per_request_price(u128) ‖ fee_per_epoch(u128)
‖ epoch_blocks(u64) ‖ pass_min_credits(u64) ‖ pass_max_credits(u64)
```

The **v1** value starts with a 137-byte header, followed by `rule_count` entries:

```text
version(1, value 0x01) ‖ actor(20) ‖ enabled(1) ‖ treasury(20)
‖ default_asset(20) ‖ default_mode(1) ‖ min_price(u128)
‖ pass_credit_price(u128) ‖ fee_per_epoch(u128) ‖ epoch_blocks(u64)
‖ pass_min_credits(u64) ‖ pass_max_credits(u64) ‖ rule_count(u16)

PriceRule = path_byte_length(u16) ‖ path_pattern(UTF-8 bytes)
          ‖ methods(1) ‖ model(1) ‖ amount(u128) ‖ asset(20)
```

The current decoder selects v0 by the **exact 117-byte length**, otherwise it
requires v1. v1 rejects unknown versions, truncation, trailing bytes, invalid
UTF-8, more than `MAX_PRICE_TABLE_ENTRIES = 100` rules or a total value larger than
`MAX_POLICY_BYTES = 65,536`. Encoders write enabled as 0/1; the inspected decoder
interprets any nonzero enabled byte as true. `default_mode` and each rule's `model`
use `Free=0`, `ClientPaid=1`, `ActorFunded=2`, `Pass=3`, `Epoch=4`; other tags reject.
`methods` uses bits 0–5 for GET, HEAD, POST, PUT, PATCH and DELETE respectively;
bits 6–7 are reserved and reject. The node validates these fields but does not
match HTTP paths at settlement.

The gateway evaluates `price_table` in stored order, taking the first matching
path/method rule; if none matches, `default_mode` applies, followed by the payment
selection rules in §7.5. Matching and quote selection are gateway responsibilities.
The native node settlement view uses v1 `min_price` as the client-paid floor and
`pass_credit_price` as the pass unit price; v0 `per_request_price` supplies both.
Policy admission requires `min_price` not to exceed the cheapest ClientPaid rule
when one exists, and enforces the bounds in §19. The encoding alone does not prove
gateway rollout or payment-before-execution conformance.

### 8.2 Asset boundary

Node references to per-rule multi-asset settlement as a “§8.2 follow-up”
identify an unimplemented extension. The current settlement view selects v0
`asset` or v1 `default_asset`; a per-rule asset field is not independent on-chain
settlement support. Native settlement in this release is CBY, while external
USDC collection uses the separate adapter described in §§4–6. Any CUSD acceptance
under CIP-36 needs its own implementation and release decision.

### 8.3 Native API and verification versus settlement

These are the existing native PaymentGate operations at the node revision in
§8.1. Names identify handler/instruction behavior, not new HTTP endpoints.

| Operation | Inputs and effect |
| --- | --- |
| `set_policy` | Bounded policy bytes; authenticated sender must equal the policy actor; validate and store the policy. |
| `get_policy` | Actor; read its policy without mutation. |
| `deposit_budget` | Actor and amount; debit the authenticated sender and credit the actor's serving budget. |
| `withdraw_budget` | Actor and amount; actor-authorized withdrawal of available budget. |
| `budget_balance` | Actor; read its serving budget without mutation. |
| `deduct_budget` | Actor, amount and request ID; policy-treasury authority, amount bounded by available budget; deduplicate by actor/request ID. |
| `purchase_pass` | Actor, credits and beneficiary; charge the authenticated buyer, create a pass bound to the beneficiary. |
| `pass_balance` | Pass ID; read the pass record without mutation. |
| `purchase_epoch` | Actor, epoch count, beneficiary and payer; require sender = payer, charge that buyer and extend the beneficiary's entitlement. |
| `epoch_status` | Actor and account; read entitlement status without mutation. |
| `verify_payment` | Signed native intent; read-only validation, returning success or rejection. Does **not** consume the nonce, reserve funds, transfer funds or consume pass credits. |
| `settle_payment` | Signed native intent; revalidate at execution, transfer per-request payment or consume a pass credit as applicable, and consume the payment nonce. An epoch entitlement was paid at purchase. |

A successful verification is not settlement confirmation: balances, entitlement
and nonce state can change before settlement. Gateways must wait for confirmed
settlement before dispatch as required by §6. Transfer/credit and nonce updates
must satisfy the all-or-nothing and recovery requirements; the table does not
claim that validation alone makes fallible storage writes atomic.

### 8.4 Native nonce namespace

PaymentGate's consumed marker is keyed by `(payer, asset, nonce)`, with the exact
storage key `ASCII("pgn:") ‖ payer(20) ‖ asset(20) ‖ nonce(32)` and value `[1]`.
The namespace is per payer and asset, **not per recipient**: reuse for another
actor on the same asset is still a replay while that marker is retained. Intent
validation checks the signed expiry and consumption state. Settlement consumes
the nonce; `verify_payment` only reads it. Deadline-bounded garbage collection
must not permit an expired authorization to become valid again.

The bare “§12.4” citation in the inspected `payment_gate/storage.rs` refers to
this nonce rule from the former inbound-bridge proposal. Its current definition
is §8.4; it does not refer to Cloud accounting or require implementing
`credit_inbound`. Update that source comment to §8.4 when touching the module.
External adapters have their own network replay rules and the purchase-level
idempotency requirement in §5; native nonce storage does not enforce them.

## 9. MPP

MPP and x402 are supported interfaces. Neither is treated as a temporary substitute for the other. Advertise only the combinations the deployment can verify and settle.

### 9.1 Challenge

MPP uses `WWW-Authenticate: Payment` with the selected method, intent, request, expiry, and challenge binding. Return `Cache-Control: no-store` for payment challenges. Challenge terms must agree with the durable quote.

### 9.2 Credential

Clients retry with `Authorization: Payment <base64url-credential>`. Validate the selected method's credential and its binding to the challenge. An optional source identity is not a substitute for payer authentication.

### 9.3 Receipt

Return `Payment-Receipt` according to the selected method. Report success only with the settlement or entitlement evidence appropriate to that method. Preserve the purchase reference so clients can recover after losing a response.

### 9.4 Challenge binding

The Cowboy adapter binds the challenge with its gateway HMAC. Its canonical seven-slot input is:

```text
realm | method | intent | request | expires | digest | opaque
id = base64url(HMAC-SHA256(gateway_secret, input))
```

This authenticates the issuing gateway's challenge; it does not reserve payer funds. Cross-replica retry handling must retain or recover the issuing challenge's binding and the shared purchase record.

### 9.5 Methods

#### 9.5.1 Cowboy

The native adapter uses `method="cowboy"`. Its canonical PaymentIntent signing digest is:

```text
keccak256("cip18:payment:v1" || chain_id || kind || payer || actor ||
          asset || amount || nonce || valid_before || request_hash || pass_id)
```

Encode chain ID and `valid_before` as big-endian `u64`, kind as one byte, addresses as 20 bytes, amount as big-endian `u128`, and nonce/request hash/pass ID as 32 bytes each. Pin shared gateway/node golden vectors. `kind` is the byte `0` for per-request, `1` for pass, or `2` for epoch. Sign with the payer's 65-byte recoverable secp256k1 signature; the recovered address must equal the payer. Native CBY uses the zero asset address. Do not assume that unsigned presentation fields such as a separate recipient or `valid_after` are included in this digest.

`request_hash` binds the native request envelope, and settlement consumes the payer's nonce atomically. The gateway verifies the exact quoted route terms against the policy; node verification alone is not an exact endpoint-pricing check. A valid signature is not a reservation and does not authorize a runner to sign for a keyless actor.

#### 9.5.2 External USDC

Select a supported MPP payment method and implementation with a proven external USDC charge path. The [MPP EVM charge specification](https://mpp.dev/payment-methods/evm/charge) is the candidate to validate alongside x402 `exact`. Pin the method version, network, asset contract, recipient, credential type, and confirmation rule before implementation is considered complete.

Do not wrap a Cowboy PaymentIntent in an EVM method label. External settlement and native PaymentGate settlement have different signatures, evidence, and trust boundaries.

### 9.6 Native intents

- **`charge`:** a fund-moving native per-request authorization.
- **`pass`:** the full native authorization with pass kind, `pass_id`, zero amount, fresh nonce, and signed request hash. The signer must satisfy the pass's beneficiary rule; signing only a short `(pass_id, request_hash)` tuple is invalid.
- **`subscription`:** the full native epoch authorization for entitlement verification, with zero amount, fresh nonce, and signed request hash. Purchase/extension is the existing `purchase_epoch` operation, not a new gateway subscription product.

These pass and subscription intents are Cowboy-specific. This CIP does not claim they interoperate with ordinary external USDC clients.

## 10. x402

Use `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and `PAYMENT-RESPONSE` according to the selected x402 version and scheme. Pin that version with interoperable client fixtures; Cowboy's custom schemes do not establish standard EVM interoperability.

The existing native schemes are `cowboy:exact`, `cowboy:pass`, and `cowboy:epoch`, corresponding to the native intents in §9.6. External USDC requires the supported standard scheme, network, asset, and facilitator implementation. The [x402 facilitator contract](https://docs.x402.org/core-concepts/facilitator) distinguishes verifying a payload from submitting and confirming payment. This CIP requires confirmation before paid execution even where a generic middleware example executes first.

A response may advertise both interfaces when they describe supported equivalent terms. If a request supplies both credentials, resolve them to one purchase and one settlement; reject conflicting payment terms rather than charging both.

## 11. Adapter boundary

Normalize the service purchase and result association, while retaining method-specific credentials and settlement evidence. Only native Cowboy authorizations become PaymentGate PaymentIntents. An external receipt is not a native intent, native balance, or proof that Chain assets were minted.

A service may share quote, request identity, replay protection, recovery, and receipt handling across adapters. A generalized multi-chain framework is not a prerequisite.

## 12. Cloud accounting

Confirmed external receipt and internal credit delivery are separate operations. Cowboy Cloud must durably record any credit obligation and retry delivery without another USDC charge. This path trusts Cowboy's receipt verification and accounting; it is not an atomic cross-chain exchange.

The proposed Cloud price is **$0.15 per CBY**. A **$1.50 amount eligible for conversion yields 10 CBY**. Pin the quote's rate, fees, beneficiary, integer units, and rounding rule. The eligible amount and credit recipient remain decisions, not an assumption that all gross receipts become the customer's spendable CBY.

Separate these accounting categories:

- **Purchased CBY:** consumption funding. This scope offers no general redemption of purchased CBY for money or other assets.
- **Earned revenue:** proceeds from actual service sales that may become eligible for owner payout. Record origin, beneficiary, deductions, and payout eligibility.

The same value MUST NOT be independently available as both spendable CBY and a payable owner balance. Refunds, credit delivery, spending, and payout must reconcile. Related-actor sales and transfers need an enforceable policy before payout: relabeling purchased funding as earnings does not resolve circular cash-out.

Owner payouts are intended to use **Stripe payouts**, not invoice credits. Connect versus Global Payouts, recipient onboarding, eligibility, and Stripe's acceptance of the actual funds flow remain unselected. [Stripe's comparison](https://docs.stripe.com/global-payouts/compare-with-connect) identifies separate payout models; availability of either product is not approval of Cowboy's design or a completed USDC-to-fiat funding route.

These are Cloud commercial rules. They do not impose Stripe eligibility, a fixed conversion rate, or restricted asset ownership on Cowboy Chain.

## 13. MCP Gating

[CIP-19](./cip-19-gateway-mcp-ingress) owns the MCP endpoint and dispatch contract. This section owns Cowboy's payment carrier and request binding. Apply the settlement-before-execution requirement of §6 to paid MCP calls as well as HTTP calls.

### 13.1 Endpoint

Under CIP-19, an actor with the `ingress.http` and `payment.gate` entitlements automatically exposes:

```
https://<actor>.cowboy.network/_cowboy/mcp
```

This is an MCP server using streamable HTTP transport (MCP 2025-11-25). The Gateway terminates the connection.

### 13.2 `tools/list` generation

The Gateway generates the MCP tool list from the actor's HTTP route table (per CIP-14) and the OpenAPI document of §14. Each declared HTTP endpoint becomes an MCP tool. Authors do not write a separate MCP manifest.

### 13.3 `tools/call` dispatch

A `tools/call` request maps to the same actor dispatch (query path or command path per CIP-14 §8) that the equivalent HTTP request would have used. The actor handler is unchanged; it does not know whether it was invoked via HTTP or MCP.

### 13.4 Payment challenges over JSON-RPC

When a `tools/call` requires payment and no valid credential is supplied, the Gateway returns a JSON-RPC error:

```json
{
  "jsonrpc": "2.0",
  "id": <request-id>,
  "error": {
    "code": -32402,
    "message": "Payment Required",
    "data": {
      "challenges": [
        {
          "id": "...",
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

Error code `-32402` mirrors HTTP's 402 status. The `data.challenges` array uses the same MPP challenge schema as §9.1, with **one MCP-specific addition: `request_hash`**.

Over HTTP the client controls the exact request bytes it sends, so it computes the §9.5.1 `request_hash(method, path, body)` itself. Over MCP it cannot: the client sends only `{name, arguments}`, and the Gateway *synthesizes* the HTTP `method`/`path`/`body` from those arguments (CIP-19 §11.2 — path parameters URL-encoded, remaining arguments split into query or a JSON body, a minimal header set). The client cannot reproduce those bytes, so it cannot pre-compute a matching `request_hash`. The Gateway therefore includes the `request_hash` it will enforce — computed over the envelope it synthesizes from the tool call's `arguments` — in the challenge, and the client signs that value into its authorization.

At redemption the client re-sends the **identical `arguments`**; the Gateway re-synthesizes the same envelope, re-derives `request_hash` (this re-derivation is authoritative — the Gateway does not trust the value echoed back), and `verify()` enforces the match. This preserves §9.5.1's cross-endpoint / cross-request replay protection over MCP without weakening it: the binding is still to the real synthesized envelope, the `nonce` is one-shot, and a tampered challenge `request_hash` only causes the client to sign a value that fails verification (no funds move).

### 13.5 Credentials and receipts via `_meta`

Clients submit credentials in the JSON-RPC request `_meta` field per `draft-payment-transport-mcp`:

```json
{
  "jsonrpc": "2.0",
  "id": <request-id>,
  "method": "tools/call",
  "params": {
    "name": "fetch_data",
    "arguments": { ... },
    "_meta": {
      "payment-authorization": "<base64url(MPP-credential)>"
    }
  }
}
```

Receipts are returned in the response `_meta`:

```json
{
  "jsonrpc": "2.0",
  "id": <request-id>,
  "result": {
    "content": [...],
    "_meta": {
      "payment-receipt": "<base64url(MPP-receipt)>"
    }
  }
}
```

### 13.6 Out of scope here

- The streamable HTTP endpoint, MCP capability negotiation, and `tools/list` generation rules are specified in CIP-19.
- Cowboy MCP gating uses the MPP carrier specified here. HTTP x402 support does not imply support for a separate x402 MCP transport.

---

## 14. Discovery

`/_cowboy/payment/openapi.json` describes the actor's registered routes, schemas, and payment policy. Its payment metadata must advertise supported method/network/asset combinations and the actual quoted amounts. CIP-19 uses the same route and schema information to construct tools and their request envelopes.

Discovery is not a settlement capability check. A method must not appear usable merely because a policy or codec can name it.

## 15. Gateway integration

### 15.1 Request admission

The gateway resolves the actor and route, authenticates any entitlement, selects one payment model, and applies §6 before paid dispatch. Payment checks must be shared across HTTP and MCP entry points and apply to actor and runner targets.

### 15.2 Queries

Confirm payment or authoritative entitlement consumption before invoking the query handler. Query execution being outside consensus does not justify settling after delivering the service.

### 15.3 Commands and continuations

Confirm payment before admitting the command. Persist the command/continuation execution identity with the purchase and reconcile its result after interruption. Payment and application execution are not represented as one atomic transaction when they actually use separate submissions.

An acknowledgement such as `202 Accepted` reports command admission, not completion. Its eventual failure follows the purchase's recovery/refund rule; it does not trigger another charge. Routing and continuation behavior remain defined by CIP-14.

### 15.4 Policy and quote consistency

A quote MUST bind the selected route, model, asset, total, recipient, expiry and the selected policy snapshot/digest in its durable terms. The native signed intent does not contain a policy version or separate recipient: the coordinator's stored binding and node's live policy checks are distinct authorities and MUST NOT be described as extra signed fields.

Before initial submission, a policy/route change that changes quoted terms MUST reject the unpaid stale quote and require a new quote; no automatic price increase is allowed. Native node settlement rechecks its applicable policy, while an already reserved CIP-28 purchase uses its committed terms. If a race changes terms after submission, the purchase MUST retain its signed attempts and reconcile the actual committed debit/recipient. Once paid, never request a second payment solely because current policy differs: serve the committed operation if authorized and still possible, otherwise record recovery/refund liability. A later free policy MUST still recover an existing purchase rather than rerun it.

## 16. Identity

Authenticate the payer through the selected payment method, and authenticate a pass/subscription beneficiary before granting access. `did:cowboy:<lowercase-hex-address>` may identify a Cowboy account in MPP's `source`; it does not provide authentication by itself. An external payer need not possess a Cowboy account unless the selected flow explicitly requires one.

Browser sessions and actor signing delegation are separate mechanisms. Neither is implied by parsing an MPP credential.

## 17. Reserved paths

The payment namespace remains under `/_cowboy/payment/`: `policy`, `openapi.json`, `quote`, `pass/{pass_id}`, `subscription`, and `budget`. `/_cowboy/mcp` remains the MCP endpoint defined by CIP-19. Expose only implemented operations; path reservation alone is not a working API.

## 18. Revenue distribution

Native PaymentGate uses the existing **500 basis point (5%)** protocol payment fee. Do not apply a different fee merely because the client chooses MPP instead of x402. Gateway gas recovery follows CIP-14 and must not be confused with the service price.

The selected fresh-devnet native per-request contract MUST use this fee basis. The companion protocol sets activation to the first post-genesis block (height 1); coordinated node/Gateway deployment is required and is not performed by this document:

```text
protocol_fee = floor(actor_fee × 500 / 10_000)
total = actor_fee + protocol_fee + gateway_recovery
```

The signed `amount` is the total debit, not an additional actor fee. For pass purchases, `total = credits × per_credit_price`; for subscriptions, `total = epochs × fee_per_epoch`. In those two purchase models, `protocol_fee = floor(total × 500 / 10_000)` and `actor_receives = total - protocol_fee`. Serving-budget deductions use their applicable model's fee basis.

`gateway_recovery` is zero for queries. The inspected native payment wire has no signed command-recovery field and settles with zero recovery. Command gas MUST use the separately authorized CIP-14 funding path; a gateway MUST NOT append an unsigned recovery debit to a service payment. Quote and settlement conformance MUST be proven with the selected height-1 profile; the historical inactive default is not an allowed release alternative.

The protocol [shared integer arithmetic at `3529c47c`](https://github.com/cowboyinc/cowboy-protocol/blob/3529c47ce50181d6f23c9bf2c9398febb8891958/crates/cowboy-protocol-codec/src/payment_fees.rs) is the single quote/settlement rule. For signed total T and authorized recovery R (zero in this wire), let B=T-R, D=10,000 and r=500. Compute `q=floor(B*D/(D+r))` with checked/wide-safe arithmetic; test q and q+1 and accept only the unique a for which `a + floor(a*r/D) = B`. If neither matches, reject the total; never round upward or divert a remainder.

| Net actor price | Protocol fee | Signed total | Result |
| --- | --- | --- | --- |
| 0 | 0 | 0 | Arithmetic vector only; endpoint admission still applies |
| 19 | 0 | 19 | Exact |
| 20 | 1 | 21 | Exact |
| 100 | 5 | 105 | Exact |
| 953 | 47 | 1,000 | Exact |
| — | — | 20 | Reject: no exact decomposition |

Pass/epoch total 1,000 instead yields actor 950 and protocol 50 under their total-based model. Method labels MUST NOT change the fee basis. Native debits MUST fit u64, despite wider intent fields.

Historical evidence only: node `44e1eb129` used `PAYMENT_REVENUE_ACTIVATION_HEIGHT=u64::MAX` and the old total-based split for requests. That snapshot is not a supported alternative release profile. No compatibility activation ladder is required for disposable devnet; quotes and settlements MUST move together to the selected profile.

The exact quote-to-debit and revenue accounting must conserve value across payer, actor, protocol treasury, and any separately authorized gas recovery. External USDC collection must specify how existing fees apply before advertising a net CBY credit or owner payout. This CIP proposes no new fee schedule.

## 19. Identifiers and enforced bounds

| Identifier | Value |
| --- | --- |
| PaymentGate | `0x12` |
| Native CBY asset | Zero address |
| CBY atomic units | `10^9` per CBY |
| Protocol payment fee | `500` basis points |
| MCP payment-required error | `-32402` |
| `MAX_PRICE_TABLE_ENTRIES` | `100` rules; policy decoding rejects more |
| `MAX_PREPAID_CREDITS` | `1_000_000` credits per pass, also bounded by the actor policy |
| `PASS_EXPIRY_BLOCKS` | `31_536_000` blocks; inspected purchase sets expiry to saturating `purchase_height + PASS_EXPIRY_BLOCKS`, and redemption rejects expired passes |

No bridge actor, universal confirmation depth, or fiat payment method is allocated by this scope.

## 20. Entitlements

`payment.gate` controls native payment policy capability alongside `ingress.http`. Its method/intent declarations and per-request price ceiling do not constitute an outbound actor allowance. The native `max_price_per_request` is `u128` and bounds a per-request or per-credit price, not a pass's aggregate purchase or a subscription epoch fee.

When entitlement enforcement applies, the actor must hold `ingress.http` and `payment.gate`; the policy's implied intents must be granted. `set_policy` requires `accepted_methods` to contain `cowboy`, otherwise it rejects with `Unauthorized`: method declarations do not enable an unimplemented external settlement path.

The effective price ceiling is an explicit grant's `max_price_per_request`, if supplied; otherwise it is the live CIP-12 parameter `payment.gate.max_price_cap` under governance actor `0x09`. Its binary fallback is `PAYMENT_GATE_GOVERNANCE_MAX_PRICE = 10^24` atomic units. An omitted grant ceiling follows governance changes; an explicit grant is a fixed value. At `10^9` units/CBY the fallback exceeds the `10^18`-unit genesis supply, so its presence alone is not a practical consumer price limit. Configure the intended ceiling and verify enforcement before treating it as protection.

The selected native release MUST enforce the applicable ingress/payment grants and price ceilings from its first post-genesis block. The older inspected node had an inactive default; companion node #1715 changes the release profile. Code presence or this document is not proof of network activation: acceptance MUST verify both policy admission and settlement rejection through the deployed node. No fallback to the old unenforced profile is part of this release.

## 21. Implementation status

The earlier snapshots gateway `8441ce77` / node `44e1eb129` describe historical gaps. As of 2026-09-23 the companion work is:

| Component | Implementation evidence | Remaining release evidence |
| --- | --- | --- |
| Native fee codec and profile | [protocol #150, `3529c47c`](https://github.com/cowboyinc/cowboy-protocol/pull/150) | Merge, durable consumer pins and coordinated deployment |
| Committed receipts, native atomic settlement, entitlement/profile and allowances | [node #1715, `40f05626`](https://github.com/cowboyinc/node/pull/1715) | Merge and live acceptance, including actual grant and fee enforcement |
| Shared purchase coordinator, signed-attempt persistence, confirmation-before-dispatch and saved results | [Gateway #104, `09099881`](https://github.com/cowboyinc/gateway/pull/104) | Review/merge, deployment topology and real node/backend failure recovery |
| Actor allowance credential integration | [Gateway #106](https://github.com/cowboyinc/gateway/pull/106) / [SDK #164](https://github.com/cowboyinc/python-sdk/pull/164) | Matching pins, merge and actor-to-service acceptance |
| External USDC | Candidate only | Network/adapter selection and ordinary clients for both protocols |
| Cloud accounting/payout | Requirements only | Product funds-flow decisions, provider integration and reconciliation |

These implementation PRs were open when checked. Code CI, review, merge, activation and live-network acceptance are distinct. Automatic refunds, arbitrary-backend exactly-once effects and public result-recovery APIs are not claimed implemented.

## 22. Release decisions and proof

The three tracks have independent acceptance. Native CBY may ship once its own evidence is complete, without waiting for an external asset or payout product. External USDC collection does not claim completed Cloud payout support.

| Track | Decision owner role | Enablement evidence |
| --- | --- | --- |
| Native CBY | Protocol/node/Gateway maintainers | Matching profile/pins, real committed settlement before execution, authenticated retries and paid-failure procedure |
| External USDC | Payments integration owner | One network/asset/recipient/adapter version and confirmation rule, ordinary x402 and MPP clients, durable receipt association |
| Cloud credit and payout | Cloud product/accounting and payout integration owners | Approved beneficiary/fee/rounding/funds flow, no double availability, refund/related-party controls and actual provider payout reconciliation |

These are responsibility roles, not a claim that a named person or provider has approved a launch. `$0.15/CBY` remains a commercial proposal. Until the external/Cloud decisions are made, their collection, credit-conversion and payout capabilities MUST NOT be advertised as enabled. Associated accounts cannot turn purchased consumption funding into withdrawable earnings merely by selling to themselves; payout remains disabled until that policy is enforceable.


| Work | Decision or evidence required |
| --- | --- |
| External adapter | Select one USDC network, provider, method/version, recipient and confirmation rule; test an ordinary client through **each** of x402 and MPP. Base USDC with Base Sepolia testing is a candidate, not a claimed integration. |
| Purchase coordinator | Validate §§5–6 identity, shared retry handling, reconciliation and execution claims against the companion implementation; prove confirmation precedes dispatch. |
| Paid failure | Implement and prove §6.1 recovery/refund policy and authorized per-method procedures; unknown outcomes remain pending reconciliation without duplicate charge or refund. |
| Cloud accounting | Fix beneficiary, fee basis, rate/rounding, purchased-versus-earned treatment, related-actor policy, and prevention of double availability. |
| Stripe payouts | Verify product fit and eligibility, recipient onboarding and USDC-to-fiat funding; reconcile payout retries with the earnings ledger. |
| Native paths | Verify native fee conservation, effective entitlement enforcement, and pass/budget/subscription behavior through real gateway/node paths. |

Release proof must include mismatched terms, stale quotes, nonce replay, retries across replicas, crash after external receipt but before credit, crash after dispatch but before result persistence, and a paid operation that fails. Mock settlement tests alone do not establish these flows.

Inbound payment acceptance may ship independently of CIP-28. Cloud payouts must remain unavailable until their accounting and provider prerequisites are satisfied; that does not require a general banking system or block receipt-and-service testing. Stop expanding this CIP when the bounded collection flow and its failure handling are specified and proven.
