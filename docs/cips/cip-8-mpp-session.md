---
title: "CIP-8: MPP Session (Retroactive)"
description: Formal specification of the on-chain MPP Session actor (`0x0C`) that escrows payer funds, settles cumulative off-chain vouchers, and arbitrates disputes. Retroactive — code is already deployed at `node/runner/src/system_actors.rs:35` + `node/types/src/session.rs` + `runner/crates/runner-common/src/voucher.rs`.
icon: arrow-rotate-left
---

<Note>
  **Status:** Draft (Retroactive — code-first)
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-05-11
  **Requires:** CIP-2 (Off-Chain Compute), CIP-3 (Dual-Metered Gas), CIP-18 (Payments) — wire-format alignment
  **Companions:** CIP-7 (Stream Protocol) — independent feed usage billing
  **Code references (authoritative):**
  - `node/runner/src/system_actors.rs:35` (`SESSION_ACTOR = 0x0C`)
  - `node/types/src/session.rs` (Session record, storage key, types)
  - `node/types/src/session_eip712.rs` (EIP-712 domain)
  - `node/execution/src/runner/session.rs` (six on-chain handlers)
  - `runner/crates/runner-common/src/voucher.rs` (off-chain voucher / sign / recover)
</Note>

## 1. Abstract

This CIP retroactively specifies the **MPP Session** protocol that has been committed to code as `SESSION_ACTOR = 0x0C` along with full Rust-side handler scaffolding and runner-side voucher library. The session protocol implements the **Session mode** of the IETF Machine Payment Protocol (MPP) — the wire complement to MPP's `intent="charge"` mode covered by [CIP-18](./cip-18-payments).

Where CIP-18 covers single-call payments (HTTP request → 402 → credential → settle, **one chain tx per call**), CIP-8 covers high-frequency micro-billing — typical for LLM token streaming, paid HTTP/MCP loops, or long-lived AI sessions — where the payer opens an on-chain escrow once, exchanges payer-signed accumulating `SessionVoucher`s off-chain with the Runner over an arbitrary number of micro-calls, and settles on-chain in **three transactions** (Open + Settle + Finalize) instead of N.

This CIP is **retroactive**: code precedes specification. The intent here is to capture and ratify what is already deployed (as PoC at `runner-common/voucher.rs` / `system_actors.rs:35`), close the spec-vs-code gap surfaced in `wiki/drift.md` V-11 / V-12 / V-13, and provide a stable normative reference for follow-on work (dispute mechanism, cross-chain bridges, CIP-20 fungible-token sessions, fiat rails).

### Dependencies

The `Requires` header lists the hard dependencies; this subsection pins the precise revision and the specific surface each one is leaned on, so a reviewer can see the full chain at a glance and knows not to ratify CIP-8 ahead of them:

| Dependency | Pinned at | CIP-8 leans on |
|---|---|---|
| **CIP-2** (Off-Chain Compute) v3 | Result Verifier `0x03` + commit-reveal | Dispute resolution: the `Disputed` path (§6.2, §11) is arbitrated by CIP-2's existing verifier + commit-reveal mechanism — CIP-8 adds no new dispute machinery. |
| **CIP-3** (Dual-Metered Gas) | Cell/cycle metering | The three on-chain handlers (Open / Settle / Finalize) are metered under CIP-3; the `fee_payer` and escrow accounting assume CIP-3's model. |
| **CIP-18** (Payments) | `intent="charge"` wire format + PaymentGate `0x12` | Wire-format alignment (§4): the `SessionVoucher` and the single-call MPP charge flow are the two complementary halves of the same IETF MPP wire format and MUST stay byte-compatible. |

CIP-7 (Stream Protocol) is a **companion**, not a dependency — it defines feed delivery and provider-reported cumulative usage settlement, with separate authorization and no shared on-chain state with CIP-8.

---

## 2. Motivation

CIP-18's `intent="charge"` flow requires one HTTP round-trip per request and **one PaymentGate settle** per request. For high-frequency machine-to-machine workloads — agent A streaming LLM tokens from agent B, an MCP tool loop, a metered HTTP API consumed in a tight loop — this is too coarse:

- Each call carries challenge / verify / settle overhead.
- Per-call settle generates one chain tx; an 8-hour LLM session can generate millions.
- Failure mid-session leaves accounting ambiguous unless restart-safe.

The MPP Session model — already in production for Stripe + Tempo Labs IETF submissions — solves this by:

1. **Escrow once.** Payer locks `max_amount` (a session ceiling) on chain at `OpenSession`. This is the upper bound on what the runner can ever extract.
2. **Cumulative voucher off-chain.** Each off-chain call returns a payer-signed EIP-712 `SessionVoucher` with a monotonically increasing `cumulative_amount` — the **total** owed for the session up to and including this call. The latest voucher fully supersedes all previous ones; restart-safe.
3. **Batch on-chain settle.** Runner submits the latest voucher (any time, or periodically, or at end) via `Settle`. PaymentGate-style splits apply: 89% runner, 10% burn, 1% treasury.
4. **Close + dispute window + Finalize.** Payer (or contract on `expires_at_block`) triggers `Close` → opens dispute window → `Slash` available within window → after `DISPUTE_WINDOW_BLOCKS=75` blocks, `Finalize` returns unused `(max_amount - spent)` to payer.

End-to-end: **3 chain tx + N off-chain HTTP**, vs. CIP-18 charge mode's **N chain tx + N off-chain HTTP**.

---

## 3. Why This CIP Is Retroactive

The MPP Session research (`refs/runner/2026-04-28_MPP_Session_Research.md`) and implementation plan (`refs/plans/2026-05-06_mpp_session_implementation.md`) were intended to be a PoC. However:

1. The v2 CIP alignment round (2026-04-21) had not yet drafted a Session model — the payments proposal then excluded streaming metered billing; the current CIP-18 §3 still excludes within-request streaming billing.
2. The PoC went directly to code: `node/runner/src/system_actors.rs:35` allocates `SESSION_ACTOR = 0x0C`; six handlers in `node/execution/src/runner/session.rs`; full off-chain voucher library in `runner/crates/runner-common/src/voucher.rs`; EIP-712 domain in `node/types/src/session_eip712.rs`.
3. The v2.r2 system actor sequence (May 2026) accepted `0x0C = SESSION_ACTOR` and renumbered CIP-14 v2 / CIP-10 v2 / CIP-18 around it, rather than asking the code to relocate.

This CIP therefore formalizes what code already does. It supersedes both the MPP Session research/plan documents and `wiki/concepts/mpp-session.md` (which now points here for the normative spec).

---

## 4. Architecture

```
Payer (signer)                   Runner (HTTP endpoint)             SESSION_ACTOR (0x0C)
       │                                  │                                  │
       │── tx: OpenSession (max_amount) ──────────────────────────────────▶  │
       │                                  │                                  │ escrow max_amount
       │                                  │   ◀──── SessionOpened event ───  │
       │                                  │   (cached locally)               │
       │                                  │                                  │
       │── HTTP request + voucher_k ─────▶│                                  │
       │   ◀────── response_k + ack ──────│                                  │
       │   (repeat N times, voucher_k.cumulative_amount monotonic)           │
       │                                  │                                  │
       │                                  │── tx: Settle (voucher_max) ─────▶│
       │                                  │                                  │ verify voucher → split
       │                                  │                                  │   89% → runner
       │                                  │                                  │   10% → burn (0x00)
       │                                  │                                  │   1%  → treasury (0x08)
       │                                  │   (repeat at runner discretion)  │
       │── tx: CloseSession ────────────────────────────────────────────────▶│
       │                                  │                                  │ status = Closing
       │   (DISPUTE_WINDOW_BLOCKS = 75 blocks)
       │                                  │                                  │
       │── tx: Finalize  (anyone) ──────────────────────────────────────────▶│
       │                                  │                                  │ refund (max_amount - spent)
       │                                                                     │ status = Refunded | Settled
```

---

## 5. Address & Storage

### 5.1 System Actor

```
SESSION_ACTOR_ADDRESS = 0x0C
```

(per `node/runner/src/system_actors.rs:35`; declared in `wiki/entities/system-actors.md`.)

### 5.2 Storage layout

Sessions persist under `SESSION_ACTOR`'s actor-KV at key:

```
key = b"session:" || session_id   // 8-byte prefix + 32-byte id; total 40 bytes
val = bincode(Session)             // see §6.1
```

Per `node/types/src/session.rs:217-221` (`session_storage_key`).

Vouchers are **never** stored on chain. They live only:
- in the payer's off-chain wallet (last-signed)
- in the runner daemon's local cache (latest received per session)

Only the most recently `Settle`-d voucher's `cumulative_amount` and `nonce` survive on-chain, as fields of the `Session` record.

---

## 6. On-Chain Types

### 6.1 Session record

Per `node/types/src/session.rs:75-90`:

```rust
struct Session {
    session_id:            [u8; 32],       // payer-chosen, see §6.3
    payer:                 Address,        // signs vouchers; funded escrow
    runner:                Address,        // receives 89% of settled amount
    asset:                 SessionAsset,   // CBY (PoC) | Cip20(token_addr) | future
    max_amount:            u128,           // escrow ceiling; spent <= max_amount enforced
    deposit:               u128,           // current escrow balance
    spent:                 u128,           // cumulative settled so far
    last_voucher_nonce:    u64,            // monotonicity guard
    price_advert_digest:   [u8; 32],       // hash of off-chain price advert
    expires_at_block:      u64,            // block height after which Close is enforced
    opened_at_block:       u64,
    status:                SessionStatus,  // see §6.2
}
```

### 6.2 SessionStatus

```rust
enum SessionStatus {
    Open,                                     // accepting Deposit / Settle
    Closing { closed_at: u64 },               // dispute window, still accepting Settle
    Disputed { ... },                         // transient — Slash invoked, awaiting CIP-2 verdict
    Settled,                                  // terminal — runner took everything
    Refunded,                                 // terminal — payer got change back
    Slashed { ... },                          // terminal — dispute won by payer; future work
}
```

The terminal states (`Settled` / `Refunded` / `Slashed`) accept no further handlers. `Disputed` is transient and is resolved by the existing CIP-2 `Result Verifier (0x03)` + commit-reveal mechanism (see §11).

### 6.3 SessionId

```
session_id = keccak256(payer || runner || nonce || open_block_height)
```

The payer supplies `session_id` on `OpenSession`. The handler verifies `!exists(session_id)` and rejects collisions. `nonce` is a payer-chosen u64 — typically a monotonic counter inside the payer wallet, but anything that avoids collision with prior `(payer, runner)` sessions is acceptable.

### 6.4 SessionAsset

```rust
enum SessionAsset {
    Cby,                       // native CBY (PoC default)
    Cip20(Address),            // CIP-20 fungible token (future)
}
```

PoC implementation only supports `Cby`. `Cip20(...)` is a typed placeholder — handlers reject it with `ExecutionError::Unsupported` until follow-on work wires it in.

---

## 7. Off-Chain Types

### 7.1 SessionVoucher

Per `node/types/src/session.rs:105-120` + `runner/crates/runner-common/src/voucher.rs:51`:

```rust
struct SessionVoucher {
    session_id:        [u8; 32],
    cumulative_amount: u128,            // total owed at this point; >= previous voucher
    nonce:             u64,             // strictly increasing per session
    expires_at:        u64,             // block height; settle rejects after this
    usage_digest:      [u8; 32],        // keccak256(off-chain usage_log); auditable
    signature:         [u8; 65],        // secp256k1 (r, s, v) over EIP-712 digest
}
```

**Cumulative semantics.** `cumulative_amount` is the **total** the payer owes the runner across all calls in the session up to and including the call that produced this voucher. It is **monotonically increasing** across vouchers for a given `session_id`. The latest voucher therefore fully replaces all previous ones — a power-on idempotency property: if the runner crashes and loses prior vouchers but keeps voucher_k, settling voucher_k captures everything voucher_{1..k} would have settled.

**Nonce.** Strictly increasing per session. The on-chain `Settle` handler rejects any voucher with `nonce <= session.last_voucher_nonce`. Combined with cumulative monotonicity this prevents replay across nonces.

**Expires_at.** Block-height ceiling. `Settle` reverts after `current_block > expires_at`. Forces fresh vouchers if the runner sits on stale ones.

**Usage_digest.** Opaque to chain. Off-chain it is `keccak256(canonical(usage_log))` where `usage_log` is whatever the runner and payer agree on (LLM token counts, HTTP byte counts, MCP tool-call counts). The chain never inspects it; downstream auditing tools may.

### 7.2 EIP-712 domain

Per `node/types/src/session_eip712.rs` + `runner/crates/runner-common/src/voucher.rs:42-50`:

```
EIP712Domain {
    name:              "Cowboy MPP Session",       // DOMAIN_NAME
    version:           "1",                         // DOMAIN_VERSION
    chainId:           COWBOY_SESSION_CHAIN_ID,    // PoC: 1 (V-13 — see §13)
    verifyingContract: <SESSION_ACTOR_ADDR as 20-byte padded>
}
```

`SESSION_ACTOR_ADDR` is the 20-byte form of `SESSION_ACTOR = 0x0C` (19 zero bytes + `0x0C`), per `runner-common/voucher.rs:34-38`.

The voucher typehash uses the canonical EIP-712 type string:

```
Voucher(bytes32 session_id, uint128 cumulative_amount, uint64 nonce, uint64 expires_at, bytes32 usage_digest)
```

(per `voucher.rs:46`.)

---

## 8. Handlers (six)

All handlers are methods on `ExecutionEngine` per `node/execution/src/runner/session.rs:34-372`. They are **ActorMessages** to `SESSION_ACTOR=0x0C`, dispatched through the standard SystemInstruction `ActorMessage` path. They do **not** introduce new numeric SystemInstruction opcodes — they re-use the existing `ActorMessage` envelope with a typed selector. (See §12 for the opcode question.)

### 8.1 OpenSession

```
input:  session_id, payer, runner, asset, max_amount, price_advert_digest, expires_at_block
caller: payer (signed tx)
preconditions:
    - !exists(session_id)
    - runner is registered (Runner Registry 0x01)
    - max_amount > 0
    - asset == Cby (PoC); Cip20 → Unsupported
    - expires_at_block > current_block_height
effects:
    - debit payer.balance -= max_amount (escrow into SESSION_ACTOR balance account)
    - write Session{ status=Open, deposit=max_amount, spent=0, last_voucher_nonce=0, ... }
    - emit SessionOpened event
```

### 8.2 Deposit

```
input:  session_id, amount
caller: payer (signed tx)
preconditions:
    - session.status == Open
    - amount > 0
effects:
    - debit payer.balance -= amount
    - session.deposit += amount
    - session.max_amount += amount  (extends ceiling)
    - emit SessionDeposited event
```

### 8.3 Settle

```
input:  session_id, voucher: SessionVoucher
caller: any (typically runner; permissionless on the happy path because the voucher is self-authenticating)
preconditions (per node/execution/src/runner/session.rs:171-291):
    - session.status in {Open, Closing{...}}
    - voucher.session_id == session_id
    - voucher.nonce > session.last_voucher_nonce
    - voucher.cumulative_amount > session.spent  (zero-increment vouchers are rejected)
    - voucher.cumulative_amount <= session.deposit
    - voucher.expires_at >= current_block_height
    - voucher.signature recovers to session.payer (EIP-712 digest from §7.2)
effects:
    - increment = voucher.cumulative_amount - session.spent
    - session.spent = voucher.cumulative_amount
    - session.last_voucher_nonce = voucher.nonce
    - split increment per SettlementConfig (default 89% / 10% / 1%):
        - runner_share to session.runner
        - burn_share  to address(0x00)
        - treasury_share to TREASURY (0x08)
    - emit SessionSettled event
```

The settlement code path **fully reuses** `verifier.rs:351-465`'s SettlementConfig logic — there is no separate payout template.

### 8.4 CloseSession

```
input:  session_id
caller: must be session.payer
preconditions:
    - session.status == Open
effects:
    - session.status = Closing{ closed_at: current_block_height }
    - emit SessionClosing event
```

`Settle` continues to work during `Closing` so the runner can flush any final voucher.

### 8.5 Finalize

```
input:  session_id
caller: any (permissionless)
preconditions:
    - session.status == Closing{closed_at} OR (status == Open AND current_block > session.expires_at_block)
    - current_block >= closed_at + DISPUTE_WINDOW_BLOCKS  (default 75)
    - no active Disputed{...}
effects:
    - refund_amount = session.deposit - session.spent
    - credit session.payer.balance += refund_amount
    - session.status = Refunded   (if refund_amount > 0) | Settled  (if refund_amount == 0)
    - emit SessionFinalized event
```

### 8.6 Slash (deferred)

```
input:  session_id, dispute: SessionDispute
caller: must be session.payer
status (PoC): handler returns ExecutionError::Unsupported
future:       wires into CIP-2 commit-reveal via Result Verifier (0x03);
              successful slash transfers session.deposit - session.spent
              proportions to payer + burn + treasury per slashing policy.
```

Slash is the dispute hook. PoC is no-op; the design space is the same as CIP-2's existing N-of-M consensus + slashing path; integration is follow-on work (V-12 follow-up).

---

## 9. Settlement Reuse

Settlement on `Settle` (§8.3) uses `SettlementConfig` from `GOVERNANCE_SYSTEM_ACTOR=0x09` exactly as CIP-2 runner job settlement does. The same `SettlementConfig{runner_percent, burn_percent, treasury_percent}` value (default 89/10/1 per `node/runner/src/types.rs:746-777`) governs both paths. Governance updates via `UpdateSettlementConfig` propagate automatically to both single-job and session settle paths.

The CIP-14 §9.7 `target_pool` enum **does** apply to MPP Session settlement: the implicit pool is `MAIN`. A future revision may introduce `target_pool: SESSION` if session-specific economics emerge (e.g. different runner/burn split for high-frequency vs. coarse jobs).

---

## 10. Relationship to Other CIPs

### vs. CIP-18 (Payments)

Orthogonal but complementary:

| Dimension | CIP-18 charge mode | CIP-8 session mode |
|---|---|---|
| On-chain tx per N off-chain calls | N | 3 (Open + Settle + Finalize) |
| Suited to | one-shot paid HTTP / MCP / pass / subscription | high-frequency micro-billing (LLM tokens, streaming) |
| State | PaymentGate `0x12` | SessionActor `0x0C` |
| Wire | MPP `Authorization: Payment` + x402 (Gateway-enforced) | Off-chain `SessionVoucher` (HTTP body, runner-verified) |
| Funds custody | per-tx through PaymentGate nonce + valid_after/before | escrow at Open, drawn down across settles |
| Dispute | none (settle is atomic) | `DISPUTE_WINDOW_BLOCKS` window + `Slash` hook |

A future revision may unify them at the wire layer (both share `intent="charge"` for the per-voucher cumulative semantics, just at different rates).

### vs. CIP-2 (Off-Chain Compute / Runners)

Reuses **all** of CIP-2:
- Runner registration (`Runner Registry 0x01`) is a precondition for `OpenSession` (the runner must be registered).
- Result Verifier (`0x03`) is the dispute path's settle/slash arbiter (future).
- VRF runner selection does **not** apply — MPP Session picks runners by URL (off-chain DNS-addressable agent endpoints), not VRF.

### vs. CIP-7 (Stream Protocol)

Orthogonal — both can coexist on the same actor:

| Dimension | CIP-7 Stream | CIP-8 Session |
|---|---|---|
| Pricing axis | Delivered body bytes (cumulative usage receipt) | Consumption (cumulative voucher) |
| Funds path | Subscriber balance under a cumulative allowance | Escrow + voucher drawdown |
| Typical use | live stream / Substack-style subscription | LLM API / metered HTTP / MCP loop |

The CIP-7 replacement retires the old Stream Key Manager at `0x0D` and permanently reserves the address. It does not reuse CIP-8 vouchers or introduce a new SessionActor funding mode.

### vs. CIP-14 v2.r2 / CIP-15 v2.r2 (Ingress)

MPP Session lives **off** the CIP-14/15 Gateway path. A runner with a public HTTP endpoint may itself sit behind a Cowboy Gateway (CIP-14 `ingress.http` entitlement + ROUTE_REGISTRY `0x0E`) for DNS addressability, but the session wire — voucher exchange + Settle / Finalize tx — does not pass through the Gateway. CIP-19's MCP ingress similarly handles per-tool-call payment via CIP-18; a session-mode MCP loop would be an MCP client persisting outside the Gateway scope.

---

## 11. Dispute Mechanism

PoC: `handle_session_slash` returns `ExecutionError::Unsupported`. Future activation maps the dispute to CIP-2's existing N-of-M commit-reveal:

```
1. Payer calls Slash(session_id, dispute_payload) within DISPUTE_WINDOW_BLOCKS of CloseSession
2. session.status = Disputed{ opened_at: current_block }
3. Result Verifier (0x03) runs CIP-2 verification mode chosen at session OpenSession
   (default: MajorityVote with explicit min-runner = 3)
4. On verdict:
   - Payer wins:  session.status = Slashed; deposit-spent proportions go to payer + burn + treasury
   - Runner wins: session.status = Settled; Finalize releases nothing further to payer
5. Finalize is gated behind status not Disputed.
```

The CIP-2 verification path is unchanged — MPP Session is layered on top.

---

## 12. Opcode / SystemInstruction question (V-12 closure)

`node/types/src/execution.rs` `SystemInstruction` is a Rust `enum` and uses match-arm dispatch; it does **not** expose numeric opcodes at the spec-table level (CIP-13 v2 §1 master allocation table is a spec abstraction, not a code wire format).

The MPP Session handlers (`OpenSession` / `Deposit` / `Settle` / `CloseSession` / `Finalize` / `Slash`) are not new SystemInstruction enum variants. They are **ActorMessages** dispatched to `SESSION_ACTOR=0x0C` with typed selectors:

```
ActorMessage {
    to: SESSION_ACTOR,
    selector: "session.open" | "session.deposit" | "session.settle" |
              "session.close" | "session.finalize" | "session.slash",
    args: ...
}
```

This sidesteps the original V-12 conflict entirely: there is no numeric opcode collision with CIP-13 v2 (52-56) or CIP-23 v2 (57) because MPP Session never claimed numeric opcodes — it claimed the **enum-variant-name** space, which is unconstrained and uncrowded.

SystemInstruction already has numeric codec assignments. Session operations and any future additions MUST use the current pinned protocol codec and node dispatch as allocation authority; neither ≥68 nor any other open-ended range is free. CIP-13 records allocation evidence, not a permission to reuse occupied slots.

---

## 13. EIP-712 chainId source (V-13 closure)

Per `runner/crates/runner-common/src/voucher.rs:28`, the PoC uses:

```rust
pub const COWBOY_SESSION_CHAIN_ID: u64 = 1;
```

This is a **PoC placeholder**, not a normative protocol value (note `1` is Ethereum mainnet's chainId, so a PoC voucher shares an EIP-712 domain axis with L1; production MUST NOT ship `1`). The activation rule is:

1. **Mainnet / production:** `COWBOY_SESSION_CHAIN_ID` MUST be drawn from the canonical Cowboy chain id (currently TBD — likely `node/types/src/constants.rs` once a `CHAIN_ID` constant lands, or the validator config field that holds it).
2. **Devnet / testnet:** Per-network distinct value to prevent cross-network voucher replay.
3. **Coordinated client+chain release:** Bumping `COWBOY_SESSION_CHAIN_ID` requires synchronous voucher.rs + session.rs constant update on both sides (`voucher.rs:14` carries this warning verbatim).

Activation TBD; the constant remains `1` until either (a) a Cowboy `CHAIN_ID` lands in `node/types/src/constants.rs` and is plumbed through, or (b) a sibling CIP (e.g. a "Cowboy network identifier" CIP) standardizes it.

---

## 14. Security Considerations

| Threat | Mitigation |
|---|---|
| Payer fails to sign voucher mid-session but keeps using runner | Runner MUST reject any HTTP request without a fresh voucher; one bad request = drop |
| Runner takes payment without serving | Payer triggers `CloseSession` to enter dispute window; calls `Slash` if grounds; CIP-2 verification arbitrates (future) |
| Voucher replay | `nonce` strictly increasing + on-chain `last_voucher_nonce` check |
| Voucher expiry abuse | Per-voucher `expires_at` (block-height); `Settle` reverts post-expiry |
| Payer withdraws escrow mid-session | Impossible — funds locked in `SESSION_ACTOR` storage at `OpenSession`, only releasable via `Finalize` after dispute window |
| Payer key compromise | `max_amount` is the loss ceiling per session; payers should choose conservative ceilings |
| Runner DDoS via free requests | Off-chain rate limit + reject `voucher.cumulative_amount` increments below `min_increment` (runner-policy, not protocol) |
| EIP-712 domain replay across networks | `chainId` in domain separator; activation requires production chain id (see §13) |
| Session-id collision | `!exists(session_id)` check at `OpenSession`; payer-chosen ID with collision-resistant construction recommended (§6.3) |

---

## 15. Reserved Paths / Names

None at this layer. MPP Session is fully off-chain on the runner side; the on-chain surface is `SESSION_ACTOR=0x0C` actor messages only.

If a future CIP introduces a discovery path (e.g. `/_cowboy/session/{session_id}` for clients to query session status via Gateway), that path will be reserved in a follow-on CIP.

---

## 16. Future Work

- **`Slash` activation.** Wire dispute resolution to CIP-2 Result Verifier; deploy chosen verification mode (default `MajorityVote`).
- **`SessionAsset::Cip20` activation.** Enable CIP-20 fungible-token sessions (stablecoin micro-billing).
- **Cross-chain session bridge.** Pair with CIP-25 cross-chain mailbox to allow Cowboy-anchored session escrow with off-chain runners on other chains.
- **TEE attestation integration.** Composite attestation from CIP-23 v2 may be offered as a tighter verification mode for premium session classes.
- **Wire convergence with CIP-18.** Consider unifying MPP `intent="charge"` cumulative-voucher semantics under a shared `intent="session"` registration; would require IETF coordination.
- **`target_pool: SESSION` discriminant.** If session economics warrant a different runner/burn split, add to the `SettlementConfig.target_pool` enum.
- **Indexer-friendly events.** Today's `SessionOpened` / `SessionSettled` / `SessionFinalized` events form the basis of off-chain billing indexers; the deterministic big-endian wire layout is defined in `node/execution/src/runner/session.rs::session_events` (97/64/120/40/48-byte payloads, test-pinned); a future revision SHOULD lift it into a normative event-schema appendix.

---

## 17. Backwards Compatibility

Strictly additive. `SESSION_ACTOR = 0x0C` was previously unallocated in v1 CIP space; this CIP ratifies code's allocation. No existing on-chain actors, RPCs, or PVM syscalls are modified. Actors without active sessions are unaffected. Runner-side opt-in via `runner-common/voucher.rs` library; runners not implementing session voucher exchange continue to operate on the CIP-2 single-job path unchanged.

---

## 18. References

- `node/runner/src/system_actors.rs:35` — address allocation
- `node/types/src/session.rs` — record types + storage key
- `node/types/src/session_eip712.rs` — EIP-712 domain
- `node/execution/src/runner/session.rs` — six handlers (`handle_session_open` / `_deposit` / `_settle` / `_close` / `_finalize` / `_slash`)
- `runner/crates/runner-common/src/voucher.rs` — off-chain voucher + sign / recover
- `refs/runner/2026-04-28_MPP_Session_Research.md` — research origin (superseded by this CIP for normative claims)
- `refs/plans/2026-05-06_mpp_session_implementation.md` — implementation plan (superseded for normative claims)
- IETF MPP draft `draft-ryan-httpauth-payment` — wire-format ancestry
- CIP-2 §5 — Runner registration / settlement (reused)
- CIP-3 — Dual gas (orthogonal; session settle is paid by tx submitter, not metered against session escrow)
- CIP-18 — sibling Payments CIP (charge mode)

---

*Retroactive note.* If code changes after this CIP is approved (e.g. handler signature evolution, new asset variants, dispute activation), update this CIP via an r2 revision rather than starting a CIP-X v2. The v1 → v2 split convention (per CIP-14 / CIP-10 / etc.) is reserved for forward-looking redesigns; minor in-place revisions use rN suffixes only.
