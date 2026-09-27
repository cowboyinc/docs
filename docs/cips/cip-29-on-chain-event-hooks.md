# On-chain Event Hooks Proposal

## 1. Motivation

### 1.1 The Gap

In Ethereum, "events" are semantically just log writes — every actual subscribe-and-react flow lives off-chain (indexers, relayers, bots). This means any "on-chain reaction triggered by an on-chain event" requires at least one off-chain intermediary, which adds trust assumptions and latency.

The primitives we already have:

- `call()` — synchronous, atomic, directed; the caller fully decides the callee
- `send()` — asynchronous, directed, cross-block delivery; point-to-point
- `defer transaction` — future block or explicit dispatch; delayed execution
- `timer` — system-scheduled time-based firing

What's missing is a primitive that is **multi-subscriber, fired synchronously inside the same tx, with subscriber failure isolated from the emitter** — the equivalent of EVM's `try { external.call(...) } catch { ... }` pattern.

### 1.2 Flagship Use Case: Pre-liquidation

A typical cascade in a DeFi lending protocol:

1. User A locks ETH as collateral, borrows stablecoin
2. An on-chain swap pushes ETH below A's liquidation threshold
3. Liquidation logic fires, A's collateral enters the liquidation flow
4. Liquidity providers B/C/D had previously subscribed to "A enters liquidation"
5. **B/C/D must get a reaction window inside the same swap tx, before liquidation executes** — otherwise a third-party liquidator front-runs them on the spread

This cannot be done natively on Ethereum: B/C/D have to run off-chain bots watching mempool / pending logs and compete via MEV. This is a clean differentiation we can claim: **subscription and reaction as a same-tx synchronous on-chain primitive.**

Other use cases in the same shape:

- Oracle price update → multiple dependent contracts re-evaluate state in the same tx
- DAO vote passes → multiple execution modules apply the result synchronously
- NFT mint → multiple marketplace/index contracts reflect inventory in the same tx

### 1.3 Design Principles

- **Same-tx, synchronous**: hooks run to completion inside the emit call stack; control returns to the emitter only after every subscriber has run
- **Failure isolation**: a single subscriber's failure (panic / OOG / revert) affects neither the emitter nor any other subscriber
- **Reuse, don't rebuild**: ride on top of existing cross-actor `call()`, PVM snapshot, the unified QMDB storage layer, and cycle/cell accounting
- **Don't disturb cross-block invariants**: propose/verify shared path, `tx_root` ↔ receipt 1:1 mapping, basefee feedback, admission gate, and lane isolation all remain untouched

## 2. Proposal

### 2.1 Core Abstraction

The protocol-level primitives:

```python
# emitter side
rt.emit_event(topic: bytes, payload: bytes, schema: bytes | None = None) -> EmitResult
    # payload length is bounded by MAX_EVENT_PAYLOAD_BYTES (default 4096); overflow fails the call
    # payload bytes are billed 1 cell/byte against the emitter (same model as calldata)
    # EmitResult contains the in-block synchronous fire outcomes
    # plus the list of sub_ids overflowed to H+1
    # Ordering: descending by bid; top MAX_SYNC_FIRES_PER_TOPIC fire synchronously,
    # the rest are split into multiple system defer txs

rt.force_unsubscribe_event(sub_id: SubscriptionId) -> ()
    # Callable only by the emitter; used for eviction.
    # gas_remaining and occupied cells are both refunded to the subscriber
    # (the emitter neither pays nor receives anything).
    # bid has already been sunk and is NOT refunded (see §2.6).

# subscriber side
rt.subscribe_event(
    emitter: ActorAddr,
    topic: bytes,
    handler: str,
    gas_prepaid: u64,
    bid: u64 = 0,                        # bid amount that drives rank; 0 means pure first-come-first-served
                                         # (placed after every positive bid)
    schema: bytes | None = None,         # optional 32-byte payload schema identifier
) -> SubscriptionId
    # Subscriber is debited four items at once:
    #   - SUBSCRIPTION_REGISTRATION_FEE  cycles  (write compute, NOT refunded)
    #   - delta cells                            (EventSub record + index entry growth;
    #                                              fully refunded on unsubscribe / reap)
    #   - gas_prepaid                    cycles  (fire prepay pool;
    #                                              residual refunded on unsubscribe)
    #   - bid                            cycles  (already sunk, NOT refunded;
    #                                              recorded in the subscription's bid field)

rt.update_bid(sub_id: SubscriptionId, additional_bid: u64) -> u64
    # Callable only by the subscriber. Adds to bid (cannot decrease). Returns the new cumulative bid.
    # Takes effect immediately by reinserting into the (emitter, topic) ordered index.
    # Note: for an async segment whose ordering was already locked by an emit in the current tx,
    #       update_bid does NOT affect that emit's fire order — the new bid only affects this
    #       subscription's rank in subsequent emits (see §2.3 "async segment ordering is locked").

rt.topup_subscription(sub_id: SubscriptionId, additional_gas: u64) -> u64
    # Any account may top up a subscription's gas_remaining. Returns the new balance.
    # Top-ups are non-refundable, do not change bid or REGISTRATION_FEE; used to keep
    # long-lived subscriptions alive before gas_remaining is drained.
    # The funder pays additional_gas cycles, which enter the subscription prepay pool.

rt.unsubscribe_event(sub_id: SubscriptionId) -> u64
    # Callable only by the subscriber. Returns the refunded gas_remaining.
    # Cells occupied by the subscription are also refunded;
    # SUBSCRIPTION_REGISTRATION_FEE and bid are NOT refunded.
```

An explicit `schema` is a 32-byte identifier agreed by the emitter and subscriber. The host compares identifiers; it does not parse a schema or validate the payload bytes. An explicit emit declares the identifier for its `(emitter, topic)` on the event subscription index. An explicit subscription whose identifier differs from an existing declaration fails with `HostError::EventSchemaMismatch` (`event_schema_mismatch`, code 117) before registration fees or subscription writes. A subscription may precede the first declaration. When an explicit emit changes or first sets a declaration, the host checks every explicit subscriber identifier. If any differ, it fails with the same error before publishing the event or changing the declaration; otherwise it MUST write the new declaration. Emits and subscriptions without a schema retain opaque-payload behavior. The declaration survives removal of the last subscription; `update_bid` preserves each subscription's identifier. Declaration and subscription changes follow transaction and nested-call rollback rules. Each new or changed declaration charges the emitter 32 storage cells for the identifier and uses the existing Access meter for index reads and writes. Schema identifiers must be exactly 32 bytes; malformed raw host arguments fail with `InvalidInput` (2), while the SDK rejects malformed values before the host call.

> **Implementation gap:** `unsubscribe_event` and `force_unsubscribe_event` delete the record and index entry and compute the residual `gas_remaining`, but the residual-gas and cell refunds are currently **accounting-only** — they are recorded as `cip29.unsubscribe` / `cip29.force_unsubscribe` events for off-chain trackers rather than credited on-chain. On-chain crediting of these refunds remains required work. `bid` and `SUBSCRIPTION_REGISTRATION_FEE` are burned at subscribe time and are never refunded, matching the tables below. The **auto-expiry (zombie-reap) path differs**: on `emit`, a sub whose `gas_remaining < MIN_FIRE_COST` is silently reaped (record + index entry deleted) with **no cell refund and no `cip29.*` accounting event** — the "freed cells credited to the subscriber" behaviour of §2.3 is not yet implemented for reap (only `unsubscribe`/`force_unsubscribe` emit the refund-obligation event).

Subscribers **prepay a gas budget** at registration time, stored in the subscription record (mirroring the timer pre-fund pattern), and may optionally attach a `bid` as a priority offer. When `emit_event` runs, the PVM host reads the subscription index in descending `bid` order:

- **Top `MAX_SYNC_FIRES_PER_TOPIC` (default 64)**: fire synchronously inside the emitter's call stack, each subscriber wrapped in a snapshot — failure rolls back the subscriber's state, preserves the emitter's, and proceeds to the next subscriber.
- **Rank K+1 and beyond**: automatically forked into a `defer transaction`, fired in the same bid order at block `H+1` (riding on the existing defer subsystem; no new execution channel).

This forms a **tiered same-tx synchronous + cross-block async execution model** — the total subscriber cap is raised to `MAX_SUBSCRIBERS_PER_TOPIC = 512`; bidders willing to pay win the synchronous reaction window (e.g. liquidation front-run), while everyone else still receives the notification via H+1 async fire.

There are **three exit paths**, treated differently:

| Path | Triggered by | cycles (`gas_remaining`) | bid | cells |
|---|---|---|---|---|
| Subscriber-initiated `unsubscribe` | The subscriber | Fully refunded; `REGISTRATION_FEE` not refunded | **Not refunded (sunk)** | Fully refunded to subscriber |
| `gas_remaining` depleted (auto-expiry) | Protocol (lazy GC, see §2.3) | Nothing left to refund | Not refunded (already sunk) | Fully refunded to subscriber (not the reaper) |
| Emitter forced removal | The emitter contract | Fully refunded to subscriber; emitter receives no share | Not refunded; emitter receives no share | Fully refunded to subscriber; emitter receives no share |

**Core asymmetric design**: the cycles-class costs (`REGISTRATION_FEE` and `bid`) are sunk at the moment they are paid — the former suppresses register/unregister churn, the latter blocks the "buy a slot → immediately unsubscribe → free squat" attack; `gas_remaining` (the actual fire budget) is consumed as it is used and the residual is refundable; cells (storage) is recoverable occupation and is fully refunded to the subscriber on cleanup. The emitter pays no cells for subscription records or index entries and receives no cycles under any path; an explicit schema declaration separately charges the emitter 32 cells as stated above. `EventSub` / `EventSubIndex` live under their own prefix, fully decoupling subscription cost from the emitter, and `bid` flows into the burn pool rather than to the emitter (eliminating any way for the emitter to extract rent through the bidding market).

### 2.2 Data Model

#### Storage backbone

All chain state lives in a single QMDB instance under a unified 54-byte fixed-length key: `[1B prefix][20B address][33B slot]` (see `storage/src/state_key.rs`). CIP-4 §4.2 is the authoritative prefix table; `EventSub` and `EventSubIndex` occupy `0x18` and `0x19`.

The subscription registry must support two access paths:

1. **At emit time**: `(emitter, topic) → ordered list of subscribers` (drives fire order)
2. **At subscribe / unsubscribe / gas debit time**: `sub_id → single subscription record` (direct lookup, in-place updates to `gas_remaining`)

#### StatePrefix values

The registry uses two prefixes, following the `Timer` (`0x05`) and `TimerIndex` (`0x06`) pattern:

```
StatePrefix::EventSub      = 0x18    // Single subscription record (located by sub_id)
StatePrefix::EventSubIndex = 0x19    // (emitter, topic) → ordered subscriber index
```

**Subscription identity.** `SubscriptionId` is 33 bytes:

```
sub_id = keccak256(emitter ‖ subscriber ‖ topic ‖ u64_be(sub_height)) ‖ 0x00
```

The final byte is zero. Duplicate IDs are rejected at registration; the host does not increment that byte. `storage::event_subs::make_sub_id` is the implementation source.

**Subscription record** (located directly by `sub_id`):

```
key   = 0x18 || keccak256(sub_id)[..32] || zero-pad   // 54-byte StateKey
value = ActorEventSubscription {
    emitter_addr:  Address,
    topic:         BoundedBytes<64>,
    subscriber:    Address,
    handler_name:  BoundedString,
    gas_prepaid:   u64,                  // Total prepaid budget at registration
    gas_remaining: u64,                  // Current balance; subscription expires when zero
    bid:           u64,                  // Cumulative bid (monotonically increasing via update_bid);
                                         // already sunk; drives rank
    sub_height:    BlockHeight,
}
```

A subscription's explicit schema identifier is not stored on this record; it lives on the subscription's index entry, which is the only copy the emit path reads.

**Subscription index** (one lookup yields all subscribers for `(emitter, topic)`, ordered for fire):

```
key   = 0x19 || keccak256(emitter_addr || topic)[..32] || zero-pad   // 54-byte StateKey
value = {
    declared_schema: Option<[u8; 32]>,
    subscribers: Vec<(schema: Option<[u8; 32]>, bid_inv: u64, sub_height: u64, sub_id: SubscriptionId)>,
}
    // bid_inv = u64::MAX - bid, so high bid sorts first
    // Sort key: lexicographic (bid_inv, sub_height, sub_id)
    //   ⇒ higher bid wins; on tie, earlier subscription wins; tiebreak by sub_id
```

#### Design notes

- **Deterministic order**: the index value is sorted lexicographically by `(bid_inv, sub_height, sub_id)` — drives fire order, identical across nodes, no consensus divergence; the bidding market gets price-based rank while determinism is preserved
- **Bids take effect on write**: `update_bid` reinserts into the index immediately; subscribers can bump bid at any time to claim a higher rank, and all nodes observe the change synchronously
- **Bounded emit-path cost**: the index header holds the declared schema, and each index entry holds its subscription's identifier; when an explicit emit changes the declaration, it checks the bounded index entries for conflicting identifiers before the usual `1 index lookup → take top K → N record lookups` delivery path — no record is loaded for the schema check
- **Merkleization is free**: QMDB merkleizes everything under `0x18*` / `0x19*` automatically — the subscription state lands in `state_root` alongside `Code` / `Actor`
- **No emitter actor-storage quota consumption**: `ActorKvCount` / `ActorKvBytes` (`0x12` / `0x13`) only track KVs the emitter writes itself. Subscription records are protocol-level data and don't pollute user-contract quotas

### 2.3 Execution Model

Pseudo-code for `emit_event` at the PVM host layer:

```rust
fn emit_event(&mut self, topic: &[u8], payload: &[u8], schema: Option<&[u8]>) -> HostResult<EmitResult> {
    // 1. payload length + caps: per-tx emit count, per-tx total synchronous fire count, fan-out depth
    require!(payload.len() <= MAX_EVENT_PAYLOAD_BYTES);    // 4096 byte cap (a bound, not a charge)
    self.check_emit_caps(topic)?;

    // 2. Load the subscription index (already sorted by (bid_inv, sub_height, sub_id))
    let sub_ids = self.load_sub_index(self.current_actor(), topic)?;

    // 3. For an explicit schema, apply §2.1's declaration and mismatch rule
    //    BEFORE any cell charge: a mismatch returns EventSchemaMismatch here,
    //    so a failing typed emit consumes no payload cells. A new or changed
    //    declaration separately charges 32 cells to the emitter.
    if let Some(schema) = schema {
        self.check_or_declare_schema(self.current_actor(), topic, schema)?;   // may charge 32 cells
    }

    // 4. Payload cells, charged only once the emit is going to publish
    self.charge_emitter_cells(payload.len() as u64)?;      // 1 cell/byte against emitter

    // 5. Tier split: top K sync, rest queued async (ordering locked at this point)
    let k = MAX_SYNC_FIRES_PER_TOPIC.min(sub_ids.len());
    let (sync_subs, async_subs) = sub_ids.split_at(k);

    let mut sync_results = Vec::with_capacity(k);
    let mut deferred_subs = Vec::new();
    let mut zombies = Vec::new();

    // --- Sync segment: top K fire inside the emitter call stack ---
    for sid in sync_subs {
        let sub = self.load_sub_record(*sid)?;

        // 3a. Lazy GC: gas_remaining can't even cover one minimal fire, skip + mark zombie
        if sub.gas_remaining < MIN_FIRE_COST {
            zombies.push(*sid);
            sync_results.push(EmitOutcome::SkippedExpired(*sid));
            continue;
        }

        // 3b. Snapshot: subscriber state + global checkpoint
        let snapshot = self.pvm.snapshot();

        // 3c. Use sub.gas_remaining as this call's budget cap;
        //     consumed cycles/cells do NOT come out of the emitter's budget
        let r = self.call_actor_with_isolated_gas(
            sub.subscriber,
            &sub.handler_name,
            payload,
            sub.gas_remaining,
        );

        match r {
            Ok(res) => {
                self.deduct_subscription_budget(*sid, res.gas_used)?;
                sync_results.push(EmitOutcome::Ok(res));
            }
            Err(e) => {
                // 3d. On failure: rollback snapshot; emitter state untouched
                self.pvm.rollback(snapshot);
                self.deduct_subscription_budget(*sid, e.gas_consumed)?;
                sync_results.push(EmitOutcome::Err(e));
            }
        }
    }

    // --- Async segment: split overflow sub_ids into multiple system defer txs, all queued to H+1 ---
    //
    // **Ordering locked**: the ordering of async_subs is fixed at this emit (based on the current
    // (bid_inv, sub_height, sub_id) ordering at emit time). Even if a subscriber calls update_bid
    // between block H and block H+1 to raise its bid, the fire order of *this emit's* async
    // segment is not changed — the new bid only affects the rank for the *next* emit. This aligns
    // with the "decided at emit time" semantics of the sync segment, and rules out the
    // "emit-then-bump-bid to cut in line on the current async segment" arbitrage.
    //
    // **Split policy**: each system defer tx carries at most ASYNC_FIRES_PER_DEFER_TX = 64 subs.
    // 448 overflow subs split into ⌈448/64⌉ = 7 defer txs, all targeting H+1;
    // if H+1's total cycle budget cannot absorb them, the existing defer admission gate naturally
    // pushes the excess into H+2 / H+3.
    let emit_id = self.next_emit_id_in_tx();          // index of this emit within the tx; used in receipt causality
    for chunk in async_subs.chunks(ASYNC_FIRES_PER_DEFER_TX) {
        self.enqueue_event_defer_tx(EmitOrigin {
            emitter:      self.current_actor(),
            topic:        topic.to_vec(),
            payload:      payload.to_vec(),
            sub_ids:      chunk.to_vec(),
            origin_block: self.current_block(),
            origin_tx:    self.current_tx_hash(),
            emit_id,
        }, self.current_block() + ASYNC_FIRE_DEFERRAL_BLOCKS)?;
    }
    deferred_subs.extend_from_slice(async_subs);

    // --- Zombie cleanup: remove from index list and delete subscription records.
    //     Freed cells are credited back to each subscriber (NOT to emitter / reaper).
    if !zombies.is_empty() {
        self.trim_sub_index(self.current_actor(), topic, &zombies)?;
        for sid in &zombies {
            let sub = self.load_sub_record(*sid)?;
            self.credit_cells(sub.subscriber, self.compute_freed_cells(&sub))?;
            self.delete_sub_record(*sid)?;
        }
    }

    Ok(EmitResult {
        sync_outcomes: sync_results,
        deferred_subs,                  // caller can observe which sub_ids are queued to H+1
        emit_id,                        // index of this emit within the tx; for receipt correlation
    })
}
```

At block H+1 the async segment shares the same `call_actor_with_isolated_gas` + snapshot path as the sync segment; the only difference is that the entry point is a system defer tx, and each such tx produces an independent receipt carrying `triggered_by_emit = EmitOrigin {..}` — external light clients / indexers use this field to correlate H+1 async receipts back to the original H-block emit (see §3.2 for the receipt schema extension).

`unsubscribe_event` / `force_unsubscribe_event` share the following internal path:

```rust
fn remove_subscription(&mut self, sid: SubscriptionId, caller: Address) -> HostResult<u64> {
    let sub = self.load_sub_record(sid)?;

    // Authorization: subscriber (unsubscribe) or matching emitter (force_unsubscribe)
    require!(caller == sub.subscriber || caller == sub.emitter_addr);

    // 1. Refund residual gas (cycles) to the subscriber.
    //    REGISTRATION_FEE and bid were burned at subscribe / update_bid time;
    //    the sub.bid field is only a sort signal here, no further handling required.
    let cycles_refund = sub.gas_remaining;
    self.credit_balance(sub.subscriber, cycles_refund)?;

    // 2. Compute the storage delta freed by removing this record + index entry,
    //    and refund those cells to the subscriber regardless of who called us.
    let cells_freed = self.compute_freed_cells(&sub);
    self.credit_cells(sub.subscriber, cells_freed)?;

    self.trim_sub_index(sub.emitter_addr, &sub.topic, &[sid])?;
    self.delete_sub_record(sid)?;
    Ok(cycles_refund)
}
```

Bid handling inside `subscribe_event` and `update_bid`:

```rust
fn apply_bid(&mut self, subscriber: Address, sid: SubscriptionId, bid_delta: u64) -> HostResult<()> {
    require!(bid_delta > 0);                               // zero-delta calls are not allowed (no-op)
    self.debit_balance(subscriber, bid_delta)?;            // debit subscriber immediately
    self.burn_cycles(bid_delta)?;                          // route into burn pool (same channel as EIP-1559 basefee)
    self.bump_sub_bid(sid, bid_delta)?;                    // accumulate into sub.bid and reinsert into the index
    Ok(())
}
```

Key points:

- **Snapshot/rollback reuses the existing PVM mechanism** (`pvm/crates/vm/src/vm/snapshot.rs`) — not built from scratch

  > **Two distinct mechanisms — do not conflate (COW-1251):** event-hook *failure isolation* is the **snapshot/rollback** mechanism described here — a per-subscriber state snapshot taken before the sub-call, rolled back if that subscriber panics / OOGs / reverts, so its writes are discarded while the emitter's state is preserved. This is **not** the PVM **continuation checkpoint** (`__continuation:<cid>` state, serialized to *resume* a handler across an `await`/async boundary). Checkpoint = "save VM state so a suspended handler can continue later"; snapshot/rollback = "discard a failed sub-call's effects." A handler may use both, but they serve different purposes and have different lifetimes (a checkpoint persists across blocks; a snapshot lives only for the duration of one synchronous sub-call).
- **Gas isolation**: each subscriber executes within its own prepaid `gas_remaining` and never touches the emitter's cycles/cells. This is the precondition for failure isolation — otherwise a malicious subscriber could OOG the emitter's tx by burning emitter gas
- **`call()` reuse**: the underlying call path is the existing cross-actor call; no new execution subsystem
- **`defer` reuse**: the overflow segment rides directly on the existing `defer transaction` channel (see §3.2) — no "event async lane" or other new execution channel introduced
- **Deterministic order**: the subscription index is lexicographically ordered by `(bid_inv, sub_height, sub_id)` — every validator walks the sync segment in the same order, and the async segment is enqueued into the defer queue in the same order, ruling out consensus divergence
- **Bid never flows to emitter**: bid is burned immediately at subscribe / update_bid time; the `bid` field on the record is purely a sort signal — the emitter cannot collect any auction revenue, eliminating the attack surface where the emitter manipulates the subscription market for rent extraction
- **Lazy cleanup**: zombie subscriptions are auto-reaped when the next emit's sync segment encounters them, with no separate GC subsystem; async-segment zombies are likewise cleaned when the H+1 defer fires
- **Refund never flows to emitter**: whether `unsubscribe` or `force_unsubscribe`, the residual gas / cells always go back to the subscriber's account — preventing emitters from gaming "lure subscriber → force-remove → harvest"

### 2.4 Decorators and Explicit API (SDK)

The SDK offers **two** emit styles, both compiling down to the §2.1 `rt.emit_event` host API:

**Form A: `@emit` decorator (return-only, simple version)**

Fits "notify on function return" semantics — bound to return, at most one event per function call:

```python
@emit("liquidation_imminent")
def maybe_liquidate(self, position_id):
    if self.below_threshold(position_id):
        return ("trigger", position_id)              # decorator auto-emits ("liquidation_imminent", ...)
    return ("noop", None)                            # also emits, payload = ("noop", None)
```

**Form B: `ctx.emit` explicit API (anywhere, any number of times, any branch)**

Fits "procedural events" in complex business flows — fire at any point in the function body, inside branches, or repeatedly inside a loop:

```python
def settle_batch(self, ctx, orders):
    for order in orders:
        result = self.try_match(order)

        if result.matched:
            # Emit #1: successful match, fired inside a branch
            ctx.emit("order_matched", {"id": order.id, "price": result.price})
        elif result.expired:
            # Emit #2: expired, different topic
            ctx.emit("order_expired", {"id": order.id})
        # Neither matched nor expired → no event; the function continues

    # Emit #3: batch summary, coexisting with the N per-iteration emits above
    ctx.emit("batch_settled", {"count": len(orders)})
    return ("ok", len(orders))
```

`ctx.emit` calls `rt.emit_event` directly, so a single function can **emit an arbitrary number of events** (subject to the §2.5 `MAX_EMITS_PER_TX` total) and supports **arbitrary control flow**.

**Subscriber-side decorator** (including the `bid` parameter):

```python
@on_event(
    emitter="0xLENDING_PROTOCOL",
    topic="liquidation_imminent",
    gas=500_000,
    bid=100_000,                                     # bid 100k cycles to claim a sync-window slot
)
def on_liquidation(self, position_id):
    self.bid_for_collateral(position_id)
```

The decorator and explicit SDK forms wrap the §2.1 host API. Actors can call `rt.emit_event` directly; decorator implementation does not change protocol semantics. The proposed decorator names remain an SDK decision (§6.2).

Handlers have the signature `handler(self, payload)`. The emitter is `ctx.sender`; the topic is determined by the subscription's handler binding. A handler serving several subscriptions uses the emitter and its configured handler names to distinguish them.

### 2.5 Protocol Constants

To prevent fan-out attacks and consensus divergence, the following must be defined as protocol constants and applied identically across validators:

| Constant | Suggested initial | Purpose |
|---|---|---|
| `MAX_SUBSCRIBERS_PER_TOPIC` | **512** | Max subscribers per (emitter, topic), counting sync + async segments together |
| `MAX_SYNC_FIRES_PER_TOPIC` | **64** (conservative launch value, governance-tunable; suggested ceiling **256**) | Cap on subscribers fired synchronously inside the emitter call stack per emit (top K by bid). Constrained by lane cycle budget, validator latency, and snapshot/rollback attack surface; see §6.4 and the unpublished `ext_cip-29-sync-cap-analysis-en` analysis note (not included in this repository) |
| `MAX_EMITS_PER_TX` | 16 | Max `emit_event` calls inside a single tx |
| `MAX_SYNC_FIRE_PER_TX` | 256 | Cap on total **synchronous** fires per tx (emits × sync_subs); async segment does not count |
| `MAX_EVENT_PAYLOAD_BYTES` | 4,096 | Per-emit payload byte cap; exceeding fails the emit; payload is billed 1 cell/byte against the emitter |
| `ASYNC_FIRES_PER_DEFER_TX` | 64 | Cap on async subs per system defer tx; overflow is split into multiple defer txs at this granularity; aligned with `MAX_SYNC_FIRES_PER_TOPIC` |
| `MAX_EVENT_DEPTH` | 4 | Emit nesting depth (a handler invoked by an emit emits another topic) — **independent from PVM `max_call_depth = 32`**: K sibling handlers serialize their snapshot/rollback rather than nesting, so they do not consume call-depth slots |
| `EMIT_SAME_TOPIC_REENTRY` | `false` (hardcoded) | Same topic cannot be re-emitted inside its own emit call stack. **Not governance-tunable** — enforced unconditionally in `pvm_host.rs::emit_event`; no constant exists. |
| `MIN_SUBSCRIPTION_GAS_PREPAID` | 50,000 cycles | Floor on prepaid budget (blocks dust subscriptions spamming the registry) |
| `SUBSCRIPTION_REGISTRATION_FEE` | 10,000 cycles | One-time write-compute charge at `subscribe`, **not refunded**; suppresses register/unregister churn |
| `SUBSCRIPTION_CELL_COST_HINT` | `9` (doc hint only) | Actual charge = logical key+value byte size at 1 cell/byte (COW-1919): `(33 + 72 + 49) + topic_len + handler_len + (32 if schema is explicit else 0)` cells (`pvm_host.rs::subscribe_event`, ~160+ for a typical sub) — **not** a flat 9; the constant is a documentation hint, real cost is measured at write time. Refunded on `unsubscribe`/`force_unsubscribe` (currently accounting-only, §2.1); **not** refunded on reap. |
| `MAX_TOPIC_BYTES` | 64 | Subscription topics are nonempty raw bytes with no UTF-8 requirement; `subscribe_event` rejects topics above this bound |
| `MIN_BID_STEP` | 1,000 cycles | Minimum `additional_bid` accepted by `update_bid` |
| `MAX_CUMULATIVE_BID` | `u64::MAX / 2` | Maximum cumulative bid, checked at registration and on every increase |
| `MIN_BID` | 0 | Floor on bid — zero bids are allowed; zero-bid subs sort by `sub_height` time order, placed after every positive bid |
| `MIN_FIRE_COST` | 5,000 cycles | At emit time, subscriptions whose `gas_remaining` falls below this are skipped and marked for zombie cleanup |
| `ASYNC_FIRE_DEFERRAL_BLOCKS` | 1 | Block delay applied when forking overflow subscribers into a `defer transaction` (H+1) |

These mirror the existing `TimerConfig` pattern and are governance-tunable. **`MAX_SYNC_FIRES_PER_TOPIC = 64` is a deliberately conservative launch value**: under typical handler costs, it leaves the emitter ≥80% of its lane budget for its own logic; once testnet data on real handler-cost distributions is in, governance can evaluate higher caps against measured costs (full analysis and the validation criteria are in §6.4 and the unpublished `ext_cip-29-sync-cap-analysis-en` analysis note (not included in this repository)). 256 is a hard practical ceiling — beyond that the emitter tx loses the ability to do anything else.

### 2.6 Bidding System Actor

A dedicated system actor handles the subscription-bidding market's query and write surface. It is separate from Governance (`0x09`): bidding is frequent user activity, while governance owns `SettlementConfig` and other system parameters.

```rust
// node/types/src/constants.rs (EVENT_SUBSCRIPTION_SYSTEM_ACTOR = 0x1D)
EVENT_SUBSCRIPTION_SYSTEM_ACTOR = Address::from_low_u64(0x1D)
```

> **Address rationale.** `0x1D` is **outside** the protocol-reserved system-actor band `0x01..=0x0F` (enforced in `pvm_host.rs` against actor deploy and `fee_payer_override`). Calls to `0x1D` are intercepted in `pvm_host::call_actor` and routed to `execution::event_sub_system_actor::dispatch_rpc` — no code-bearing actor exists at this address; the slot is a "virtual" system actor managed by the host.

#### Endpoints

| Method | Type | Semantics |
|---|---|---|
| `get_rank(sub_id) -> u32` | read | Returns this subscription's current rank (0-indexed) **for future emits**; rank < `MAX_SYNC_FIRES_PER_TOPIC` means the **next** emit will fire it synchronously. **Note**: does not reflect async-fire positions already locked by an in-flight emit and queued to H+1 (see §2.3 "async segment ordering is locked") |
| `get_topic_orderbook(emitter, topic, limit) -> Vec<OrderbookEntry>` | read | Returns the top-`limit` `(sub_id, subscriber, bid)` tuples — the visible orderbook of the bidding market for the **next** emit |
| `get_min_bid_for_rank(emitter, topic, target_rank) -> u64` | read | Returns the minimum bid required to claim `target_rank` (= the current holder's bid + 1); subscribers price their bumps against this |
| `update_bid(sub_id, additional_bid) -> u64` | write | Callable only by the subscriber; adds to bid (cannot decrease), burns immediately and reinserts into the index; returns the new cumulative bid. **No effect on already-locked emits** — the new bid only affects **subsequent emits** |
| `topup_subscription(sub_id, additional_gas) -> u64` | write | Any account may top up a subscription's `gas_remaining`; returns the new balance |

`update_bid` is also exposed directly as a host API (see §2.1); calling it via the system actor is the SDK-friendly wrapper — the `@on_event` decorator's runtime-upgrade API also routes through this path.

#### Subscription removal

Bid is burned at `subscribe` and `update_bid`; the stored `bid` is a ranking signal. Unsubscribe, automatic expiry, and emitter-forced removal never refund bid or pay it to the emitter.

Removal deletes by `sub_id` regardless of index order or rank. Each `update_bid` call reorders immediately; calls are not batched. An asynchronous fire whose locked `sub_id` has been removed skips that slot without debiting it again. Refund obligations follow §2.1, including its outstanding on-chain crediting work.

#### User perception / participation

Subscribers have full observability into the bidding market:

1. Call `get_topic_orderbook` to see competitors' bids and ranks
2. Call `get_min_bid_for_rank(target_rank=63)` to see the threshold for entering the sync window
3. Call `update_bid` to outbid into the sync segment, or stay with a low bid and accept the async segment's H+1 timing
4. Call `get_rank` to monitor rank movement

This forms a complete secondary market for event subscriptions — bid, query, raise, observe are all programmable primitives composable inside actor code; business actors can wrap automated bidding strategies at the contract layer.

## 3. Why This Design Is Safe

Event hooks are a derivative of `call()` (same tx, synchronous, state-isolatable) — they are **not** a derivative of `send()` or `defer transaction`. The protocol risk surfaces are categorically different.

| Subsystem | Same-tx hook (sync segment) | Cross-block hook (async segment) |
|---|---|---|
| propose / verify shared path | **Unchanged** — hooks run inside the user-tx execution stack; propose/verify still complete in a single pass | **Unchanged** — H+1 system defer txs travel the same path as any other defer tx |
| `tx_root` ↔ receipt 1:1 mapping | **Unchanged** — sync fires are intra-tx cross-actor calls, they don't produce independent receipts | **Unchanged** — each system defer tx produces its own receipt, belonging to the H+1 block's `tx_root` |
| bridge / IBC inclusion proofs | **Unchanged** | **Lightly extended** — receipts gain a `triggered_by_emit` field marking cross-block causality (§3.2); the inclusion-proof structure is not affected |
| admission gate | **Unchanged** — every sync trigger lives inside a user tx that has already passed admission | **Unchanged** — system defer txs travel the same admission as any other defer tx; H+1 overflow naturally rolls forward |
| basefee feedback loop | **Unchanged** — hook gas comes from the subscriber prepaid pool, but **does count toward the current block's block-level cycle consumption** (CIP-3 §2.4), feeding the basefee loop | **Unchanged** — async-segment cycles count toward H+1 block consumption |
| Lane budgets (User / Runner / Timer / System) | Hook gas comes from the subscription prepay pool but consumes User-lane budget | System defer txs travel the System lane (same column as any defer tx) |

The only protocol-level additions are:

- §2.2 — the subscription registry (a new merkleized table)
- §2.3 — the emit / snapshot / rollback call semantics
- §2.5 — the protocol constants

All three are **storage + host API layer** changes. None of them touches cross-block invariants.

### 3.1 Reuse Map

| Existing mechanism | Role in this proposal |
|---|---|
| PVM `snapshot.rs` | Subscriber failure rollback (direct reuse) |
| Cross-actor `call()` | Underlying call path for hook fires (shared between sync and async segments) |
| Single-QMDB + StateKey encoding | The new `EventSub` / `EventSubIndex` prefixes plug straight into the existing QMDB (see §2.2) |
| Timer pre-fund pattern | Reference implementation for subscription gas prepay |
| Cycle / cell accounting | Hook execution accounting (drawn from the subscriber prepaid pool) |
| **`defer transaction` subsystem** | **Execution channel for async-segment (rank ≥ K) overflow subscribers — no "event async lane" added** |
| **EIP-1559 basefee burn channel** | **Sink for both `bid` and `REGISTRATION_FEE`; shares the basefee burn path so no new burn subsystem is needed** |

**No subsystem is built from scratch.**

### 3.2 Relationship to `defer transaction`

Event hooks (**sync segment**) and `defer transaction` remain parallel primitives, but **the async segment (rank ≥ `MAX_SYNC_FIRES_PER_TOPIC`) reuses `defer transaction` as its execution channel** — yielding a coordinated sync + async structure:

| Dimension | `defer transaction` (standalone use case) | event hook sync segment (rank < K) | event hook async segment (rank ≥ K) |
|---|---|---|---|
| Execution block | future block | current block, current tx | H+1 block (driven by `ASYNC_FIRE_DEFERRAL_BLOCKS`) |
| Trigger mechanism | height-triggered or explicit dispatch | inside the emit call stack | host auto-enqueues a system defer tx at emit time |
| Failure semantics | independent receipt, independent commit | snapshot rollback, no receipt | independent receipt (same as any defer tx) |
| Fit | user-scheduled async tasks, scheduled work | synchronous event subscription (e.g. liquidation front-run requires same tx) | notifications, analytics, slow-path alerts that tolerate 1-block latency |
| Billing source | scheduler | subscriber's `gas_remaining` | subscriber's `gas_remaining` |

**Impact on the existing `defer transaction` implementation**: a single new trigger source (the system defer tx enqueued from inside `emit_event`) is added; all other scheduling / admission / execution paths are **unchanged**. The system defer tx carries an `EmitOrigin` tuple:

```rust
struct EmitOrigin {
    emitter:      Address,
    topic:        BoundedBytes<64>,
    payload:      BoundedBytes<MAX_EVENT_PAYLOAD_BYTES>,
    sub_ids:      Vec<SubscriptionId>,         // ≤ ASYNC_FIRES_PER_DEFER_TX = 64
    origin_block: BlockHeight,                  // block of the original emit
    origin_tx:    H256,                         // tx hash of the original emit
    emit_id:      u32,                          // index of this emit within the tx (disambiguates multiple emits)
}
```

At block H+1 the protocol unpacks this, and each `sub_id` travels the same `call_actor_with_isolated_gas` + snapshot path as the sync segment.

**Receipt schema extension**: every receipt belonging to a system defer tx born from an emit's async segment must carry:

```rust
struct AsyncEmitReceipt {
    // ... general receipt fields (gas_used, events, status, ...) ...
    triggered_by_emit: Option<EmitOriginRef>,  // set only for system event defer txs; None for any other defer tx
}

struct EmitOriginRef {
    origin_block:  BlockHeight,
    origin_tx:     H256,
    emit_id:       u32,
}
```

External light clients / indexers use `triggered_by_emit` to correlate the H+1 async receipt back to the original H-block emit. **Key invariants**:

- `triggered_by_emit` is just a receipt field; it does not break the `tx_root` ↔ receipt 1:1 mapping
- The inclusion-proof structure is unchanged (an H+1 receipt still belongs to the H+1 block's `tx_root`)
- The correlation is one-way and observable (receipt → original emit); no back-pointer needs to live in the H-block `tx_root`

## 4. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Breadth fan-out attack**: a malicious emitter accumulates many subscribers → a single emit slows the tx dramatically | §2.5's `MAX_SYNC_FIRES_PER_TOPIC` + `MAX_SYNC_FIRE_PER_TX` double cap pins the sync-segment cost; the overflow segment rides defer and is throttled a second time by the admission gate |
| **Pre-birth subscription plant on a not-yet-existing emitter** (CIP-27 fork interaction): an actor's address is publicly computable before it exists (a CIP-27 fork child, a CREATE2-style deploy), and `EventSub`/`EventSubIndex` are keyed by `keccak256(emitter‖…)` with no per-emitter reverse index — so an attacker could subscribe high-bid sync handlers to a future emitter address `C` and, on `C`'s first `emit`, occupy `C`'s sync-fire slots inside `C`'s own tx. Because the index is hash-keyed it is not enumerable and cannot be purged at birth. | **`subscribe_event` MUST reject an `emitter` that is not a currently-live actor** (`emitter`-existence check at registration, the same guard `send_message` needs) — you cannot subscribe to an address that does not yet emit anything, and the check closes the pre-birth plant structurally. Subscriptions created *after* birth are ordinary CIP-29 subscriptions (anyone may subscribe to a live actor's events); only the pre-existence plant is the defect. This is the normative fix CIP-27 §3.2 references for the hash-of-address-keyed residue class. |
| **Reentry loop**: a handler re-emits forming an A→B→A cycle | `MAX_EVENT_DEPTH = 4` + `EMIT_SAME_TOPIC_REENTRY = false` |
| **Subscription spam**: an attacker registers vast numbers of dust subscriptions to bloat an emitter's index list | `MIN_SUBSCRIPTION_GAS_PREPAID` floor (every subscription must carry a minimum prepay) + `MAX_SUBSCRIBERS_PER_TOPIC = 512` index-list cap; subscription records live under their own prefix and don't consume the emitter's actor-storage quota; **bidding naturally suppresses spam** — sync-segment slots are protected by bid ranking, and spam subscriptions can only land in the tail of the async segment with no impact on the head |
| **Unclear gas responsibility**: who pays for the registry traversal and snapshot overhead inside `emit_event`? | The emit call itself is metered as "registry read + N × snapshot" and charged to the emitter; per-subscriber fire cost is drawn from the subscriber's prepaid pool — the boundary is explicit |
| **Subscription order manipulation**: gaining fire priority through non-market means | Order is pinned to lexicographic `(bid_inv, sub_height, sub_id)`, publicly observable; subscribers gain sync-segment rank by bidding (same logic as an auction), and ties fall back to time order — a market-driven primary axis with a deterministic tiebreak |
| **Silent failure semantics** | `EmitResult` exposes per-subscriber outcomes for explicit emitter-side checks; the receipt's events field records every fire result (success / failure + gas) — observable and auditable |
| **Invisible subscription expiry** | The registry exposes a query API; subscribers can read their own `gas_remaining` and top up proactively |
| **register/unregister churn**: rapid `subscribe` + immediate `unsubscribe` to thrash storage | `SUBSCRIPTION_REGISTRATION_FEE` is charged at subscribe and not refunded on unsubscribe — the storage-write cost is collected up-front, so the more the attacker churns, the more they lose |
| **Emitter harvesting gas via forced removal** | `force_unsubscribe_event` hard-codes that both `gas_remaining` and freed cells go back to the subscriber; the emitter receives no portion of either |
| **Emitter forced to absorb subscriber cells → economic spam** | `EventSub` / `EventSubIndex` live under their own prefix and **never count against the emitter's `ActorKvCount` / `ActorKvBytes` quota**; cells are fully borne by the subscriber — the core advantage of a protocol-level primitive over an "emitter-built subscription table" approach |
| **Snap-buy rank → unsubscribe arbitrage**: an attacker bids high to claim a sync-window slot, then unsubscribes moments before the emit to claw back the money | bid is **burned immediately** at `subscribe` / `update_bid` (same channel as EIP-1559 basefee); the `bid` field on the record is purely a sort signal, and `unsubscribe` / `force_unsubscribe` always treat bid as non-refundable → snap-buy is a sunk cost, the arbitrage doesn't work |
| **Overflow subscribers silently delayed**: rank ≥ K subscribers fire one block later and don't know they fell into the async segment | (1) `EmitResult.deferred_subs` is observable to the emitter / caller during the sync segment; (2) `EVENT_SUBSCRIPTION_SYSTEM_ACTOR.get_rank(sub_id)` can be queried at any time; (3) subscribers can `update_bid` to climb back into the sync segment → falling behind is an **observable, intervenable** state, not an implicit failure |
| **Emitter manipulating the bidding market for rent**: emitter controls the subscription market to extract auction revenue | Bid flows directly into the burn pool — **the emitter gets no share**; the emitter has no `update_bid` privilege over subscribers → no auction revenue leaks out, no counterparty for the emitter to manipulate |
| **Bid inflation / cycle deflation imbalance**: heavy bidding burns excessive cycles and warps token economics | Bid uses the same burn channel as the EIP-1559 basefee and is captured by existing burn dashboards; governance can adjust `MIN_BID` and `update_bid`'s minimum step to dampen if needed |
| **Oversized payload attack**: emitter sends a 1MB payload so every sync sub receives a data copy, blowing the lane | `MAX_EVENT_PAYLOAD_BYTES = 4096` hard cap; oversize emits fail; payload is billed 1 cell/byte against the emitter, forcing the emitter to bear the propagation cost |
| **Post-emit bid bump to cut in line on the current async segment**: subscriber calls `update_bid` between emit and H+1 fire, hoping to leapfrog within the in-flight async segment | The async-segment ordering is locked at emit time (§2.3) — the new bid only affects subsequent emits; the current async segment's fire order is fixed and every validator executes in that locked order |
| **Async-segment OOM accumulation**: a single emit produces 448 overflow subs × N emits in the same block → an H+1 fire flood | The async segment is split into multiple defer txs at `ASYNC_FIRES_PER_DEFER_TX = 64`; whatever overflows the H+1 block budget naturally rolls forward to H+2 / H+3 via the existing defer admission gate — no new "event queue explosion" path is introduced |
| **Emit-call nesting depletes the stack**: handlers invoked inside an emit perform cross-actor calls, repeated nesting depletes the PVM call stack | `MAX_EVENT_DEPTH = 4` and PVM `max_call_depth = 32` are independent counters; K sibling handlers execute serially without nesting; worst case 4 × 8 = 32 still sits within the PVM bound |

## 5. Conformance and validation

Implementation must satisfy the host API, registry, execution, and accounting requirements together. Required validation includes:

- Register and query merkleized subscriptions through contract code.
- Exercise multiple synchronous and deferred subscribers, including a failing handler whose state and effects roll back without affecting the emitter or other subscribers.
- Verify emit-time ordering, locked deferred order, receipt causality, payload/depth/fan-out limits, and deterministic execution across validators.
- Verify on-chain residual-gas and cell refunds for unsubscribe and forced removal, and the subscriber's cell refund on automatic expiry. Accounting events alone do not satisfy the refund requirement (§2.1).
- Exercise the SDK host API and decorator forms in a complete liquidation example, with developer documentation that distinguishes synchronous reactions from deferred notifications.

## 6. Open Decisions

The protocol rules in §2 define ordering, receipt causality, deferred batching, payload charging, depth limits, and top-ups. The decisions and validation work below remain outstanding.

### 6.1 Initial Values for Protocol Constants

§2.5's caps need business validation:

- Does `MAX_SUBSCRIBERS_PER_TOPIC = 512` cover the expected "long-tail notifications + top-tier reaction-window" split?
- Does `MAX_SYNC_FIRE_PER_TX = 256` cover typical multi-event cascades?
- Is `MIN_SUBSCRIPTION_GAS_PREPAID = 50,000` too high or too low?
- Is `ASYNC_FIRE_DEFERRAL_BLOCKS = 1` reasonable (does the business side accept a 1-block async delay, or would they prefer a configurable longer delay to amortize H+1 pressure)?

Governance can retune these, but the launch values shape early developer experience.

### 6.2 Decorator Naming

`@emit` / `@on_event` are direct. Alternatives:

- `@event` / `@subscribe`
- `@publishes` / `@listens`
- Or a different shape that fits existing SDK conventions better

The SDK must select and document a consistent decorator surface before claiming this interface complete.

### 6.3 Remaining work

| Area | Outstanding decision or validation |
|---|---|
| Emitter eviction commitments | §2.1 authorizes the emitter to force removal. Decide whether emitters also need an optional manifest commitment restricting that authority, and define enforcement before adding it. |
| Actor lifecycle | Specify with CIP-1 how upgrade and deletion affect existing subscriptions, including cleanup and refunds. Preserving subscriptions on upgrade and removing them on deletion are candidate semantics, not settled requirements. |
| Bid price discovery | Measure bidding spikes and ordering churn. Determine whether the immediate-burn model and `MIN_BID_STEP` provide adequate stability; a second-price auction would require a separately specified economic change. |
| Registration economics | Calibrate the 10,000-cycle registration fee against observed registration churn and cost. |
| Snapshot stress | Exercise broad sibling fan-out, failure isolation, and resource exhaustion at the specified caps. |
| Fee feedback | Measure CIP-3 basefee feedback under candidate synchronous caps of 128 and 256 before adopting either. |
| Cross-topic incentives | Model MEV and bid arbitrage across topics. |

### 6.4 Fan-out Cap as a Design Boundary

`MAX_SYNC_FIRES_PER_TOPIC = 64` is **not a hardcoded physical ceiling; it is a conservative launch choice**. The full argument lives in the unpublished `ext_cip-29-sync-cap-analysis-en` analysis note (not included in this repository); the executive summary is here.

Estimating the cost of a single fully-loaded synchronous emit against the 22M-cycle User-lane budget:

| Item | Per call | × 64 subs |
|---|---|---|
| Index read | ~1K | 1K |
| `EventSub` record read | ~500 | 32K |
| snapshot creation | ~1K | 64K |
| Handler invocation overhead | ~5K | 320K |
| Handler business logic (typical) | ~50K | 3.2M |
| Gas-deduction write | ~500 | 32K |
| **Total** | | **~3.6M cycles** (≈ 16% of the lane) |

Lane occupancy at different caps:

| Sync cap | Loaded cycles | Lane share | Budget left for emitter | Verdict |
|---|---|---|---|---|
| 64 (launch) | 3.6M | 16% | 18.4M | Ample, conservative |
| 128 | 7.2M | 33% | 14.8M | Still leaves the emitter substantial room |
| 256 (suggested practical ceiling) | 14.3M | 65% | 7.7M | Tight but workable |
| 500 | 28M | **127%** | **−6M, the tx is guaranteed OOG** | Blows the protocol-level hard cap |

**500 directly exceeds the User lane's 22M cap under typical handler costs** — this is the protocol-level hard cap and cannot be crossed. 256 is the practical edge where "the emitter tx can still do other things"; beyond that, the primitive degenerates into "exists only to emit."

#### Sync segment vs defer segment: asymmetric marginal cost

| | +1 sub in sync segment | +1 sub in async segment (defer) |
|---|---|---|
| Whose budget | The emitter tx's own User lane (22M) | H+1's overall block budget |
| Validator latency | Serial execution on the propose/verify critical path | Independent tx, parallel with other user txs |
| Failure-blast surface | The emitter tx's snapshot/rollback chain | Independent receipt, no cross-impact |
| Basefee feedback | Pushes the current block's cycle feedback | Pushes H+1's feedback, distributed |

This is why "overflow → defer" works but "unbounded sync segment" does not — the two classes of subs are not equivalent.

#### Cap validation

The specified synchronous cap is 64. Higher caps require measured handler costs, propose/verify latency, snapshot stress coverage, and CIP-3 fee-feedback analysis. Candidate evaluation criteria for 128 include average handler cost below 30K cycles and added propose/verify p99 latency below 50 ms. A candidate cap of 256 also requires sustained stress coverage without snapshot-subsystem failures.

These are evaluation criteria, not a scheduled sequence of protocol changes. Under the cost model above, 256 consumes about 65% of the emitter's lane budget; a higher cap leaves less room for the emitter's own work and increases serial validation latency.

#### How the tiered model addresses "500+ subscribers"

Raising the sync cap directly is not viable, but §2.3's **tiered execution model** still serves the theoretical "500+ subscribers" scenarios:

- `MAX_SUBSCRIBERS_PER_TOPIC = 512` (total registration cap)
- `MAX_SYNC_FIRES_PER_TOPIC = 64` (arithmetic hard cap on the sync segment)
- Ranks 65–512 are auto-forked into a `defer transaction`, which fires through the same path at H+1
- Ordering is by descending bid — **those who can pay get the sync window (liquidation front-run), those who can't or don't care about timing accept a 1-block delay (notification)**, market-driven tiering

This lets the same event serve two business classes simultaneously:

| Business class | rank | Latency | Typical use case |
|---|---|---|---|
| Liquidation front-run / price-update racing | 0 – 63 | Same-tx synchronous | Must be same-tx; high bid required to enter |
| Notification / analytics / slow path | 64 – 511 | H+1 async | Tolerates 1-block latency; zero-bid acceptable |

#### Business-side escape valves still useful

Even with tiering, three business-side splitting strategies remain valuable:

**1. Topic bucketing**

Split one over-broad topic into multiple finer-grained topics; subscribers attach to the ones they care about:

- Don't: `emit("liquidation")` ← 8000 subscribers crammed into one topic will still pile up in the async segment even with tiering
- Do: `emit(f"liquidation:tier_{tier}")` by risk band, `emit(f"liquidation:asset_{asset}")` by collateral asset

Subscribers self-segment by business relevance; in most cases a single bucket has fewer than 64 subscribers and fires entirely in the sync segment.

**2. Relay pattern (multi-tier fan-out)**

The emitter registers 64 "relay actors" as synchronous subscribers; each relay then maintains its own 64 synchronous subscribers:

- Tier 1 (emitter → relays) is a synchronous emit, preserving same-tx semantics
- Tier 2 (relay → end subscribers) is also a synchronous emit
- Capacity: 64 × 64 = 4096 end subscribers, all reachable same-tx synchronously (still bounded by the lane cycle budget; pair with topic bucketing to keep handler-logic cost low)

**3. Opt-in async (zero or low bid)**

Subscribers that don't need same-tx reaction (notifications, analytics, slow-path alerts) can register with `bid=0` deliberately — they fall into the async segment naturally and don't contend with reaction-window racers.

#### Why protocol-level "expand the sync segment" paths are rejected

The alternatives below do not preserve the required semantics or resource bounds:

| Approach | Reason for rejection |
|---|---|
| Auto-split a single emit across multiple blocks (including the sync segment) | Breaks "same-tx synchronous" semantics — the very reason this primitive exists. The tiered model only async-izes the overflow; the sync segment keeps same-tx semantics |
| Raise `MAX_SYNC_FIRES_PER_TOPIC` to 500+ | Under typical handler costs this blows past the User lane's 22M cap (500 × 56K = 28M); even with ultra-cheap handlers, a single emit consumes a quarter of the block's cycle target and basefee swings hard; validator serial-fire latency physically amplifies 8× |
| Single-emit adaptive cap (dynamically split sync/async by total `gas_remaining`) | Subscribers can't predict whether they'll fire synchronously; consensus complexity grows; malicious subscribers can register dust `gas_remaining` to crowd into the sync segment |
| Index sharding (shard by hash) | 64 subs × 49 bytes ≈ 3.1 KB single blob — QMDB handles this trivially; sharding adds complexity to a non-problem |
| Round-robin sync fire (different batch of 64 per block) | Breaks the "high bid guarantees sync" contract — subscribers cannot rely on this primitive to lock the liquidation reaction window |
