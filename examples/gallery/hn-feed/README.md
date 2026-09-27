# Hacker News Feed Example (CIP-7)

> **Historical implementation:** this example uses the retired on-chain/epoch-key model. It is not a conformance example for the near-chain Watchtower replacement. Do not use its key-delivery, signing, billing or automatic-ingestion behavior as the new protocol contract. Replacement examples must pass [CIP-7 acceptance](../../../docs/cips/cip-7-simple-stream-protocol.md#implementation-and-acceptance) before being advertised as current.

This historical example demonstrates the former **CIP-7 Simple Stream Protocol** using an
on-chain Hacker News stream actor and a downstream subscriber actor.

## What This Example Covers

- Inline-only stream payloads (`payload_inline_hex`, max 16 KiB)
- Sequence-based replay with `get_since(cursor, limit)`
- Push subscriptions with JSON DSL filters
- Filter schema validation (max depth 4, max 16 predicates)
- `PUSH_WITH_PULL_FALLBACK` subscriber behavior
- Deterministic on-chain demo flow via `demo.sh`
- Optional timer-driven ingestion contract for future live HN integration
- Demo-signature verification and payload hash checks in the subscriber
- Event emission for stats, replay inspection, and recent stream activity

## Files

- `cowboy.toml`: project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/stream/main.py`: CIP-7 stream actor with HN ingestion + publish (the `stream` target)
- `actors/subscriber/main.py`: Subscriber actor that generates simple signals (the `subscriber` target)
- `tests/`: `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh`: Deploys both actors, seeds deterministic Algolia-shaped hits, and
  verifies that push delivery generates subscriber signals on a local validator

## Architecture

```
┌──────────────────────┐   ingest timer   ┌─────────────────────────────┐
│      HNStream        │ ───────────────► │ HN Algolia API (runner/http)│
│   (CIP-7 publisher)  │ ◄─────────────── │   transformed into articles  │
└──────────┬───────────┘                  └─────────────────────────────┘
           │ publish(kind,tags,payload_inline,publisher_sig)
           ▼
┌──────────────────────┐ push on publish   ┌─────────────────────────────┐
│   Stream ring buffer │ ────────────────► │      TradingSubscriber      │
│ (head/floor bounded) │                   │  on_stream_update(messages) │
└──────────┬───────────┘                   └──────────────┬──────────────┘
           │ get_since(cursor,limit)                     │
           └──────────────────────────────────────────────┘
                           pull catch-up
```

## Simulate Locally

The tests drive both actors through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The
subscriber tests replay a batch the stream actor actually published, so the
payload-hash and demo-signature checks run against real data.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_head({})` on a fresh stream and expects
the CBOR-encoded empty head:

```bash
cowboy test --target stream --handler get_head \
  --payload 0xa0 \
  --expect 0xa26973747265616d5f69647840307834383465356635333534353234353431346430303030303030303030303030303030303030303030303030303030303030303030303030303030303030306d686561645f73657175656e636500
```

`cowboy actor check stream` (and `subscriber`) validates a target offline.
`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Run On A Network

### 1. Run the deterministic on-chain demo

```bash
bash demo.sh
```

This script:

- deploys the `stream` target with `cowboy actor deploy stream --init-payload '{"min_score":80}'` and the `subscriber` target; atomic `init` runs in the deploy frame and claims ownership for the deployer wallet
- subscribes the subscriber in `PUSH_WITH_PULL_FALLBACK` mode
- calls `on_ingest_result(...)` with a deterministic sample HN response
- queries `get_hn_stats()`, `get_recent_events()`, `get_signals()`, and `get_stats()`
- fails if the stream does not publish articles or the subscriber does not emit signals

### 2. Subscribe with filter DSL

```python
{
  "stream_address": "0xHN_STREAM",
  "mode": "PUSH_WITH_PULL_FALLBACK",
  "min_score": 120,
  "category": "tech"
}
```

Under the hood this queues:

```python
{
    "type": "subscribe",
    "subscriber": "0xSUBSCRIBER_ACTOR",
    "mode": "PUSH_WITH_PULL_FALLBACK",
    "filter": {
        "all": [
            {"field": "kind", "op": "eq", "value": "news"},
            {"field": "tags.category", "op": "eq", "value": "tech"},
            {"field": "tags.score", "op": "gte", "value": 120.0},
        ]
    }
}
```

The subscriber sends that envelope to the stream actor with:

```python
runtime.send_message(stream_address_bytes, envelope_bytes)
```

### 3. Ingest and publish messages

The local demo intentionally avoids live web fetches. Instead it calls:

```python
on_ingest_result({
    "hits": [
        {
            "objectID": "40001",
            "title": "OpenAI ships a smaller GPT model for local coding tools",
            "points": 182,
            ...
        }
    ]
})
```

`HNStream.on_ingest_result(...)` transforms each qualifying Algolia-style hit and
publishes one CIP-7 message per article. `on_timer({"tag":"ingest"})` still exists
as the documented future live-ingest entrypoint, but `demo.sh` does not depend on
runner or outbound network availability.

Each published message includes:

```python
{
  "version": 1,
  "stream_id": "0x...",
  "sequence": 42,
  "kind": "news",
  "content_type": "application/json",
  "tags": {"source": "hn", "score": 231.0, "category": "tech"},
  "payload_format": "PLAINTEXT",
  "payload_inline_hex": "7b226f626a6563745f6964223a22313233222c...",
  "payload_hash": "...",
  "signing_key_id": 1,
  "publisher_sig": "..."
}
```

### 4. Subscriber processing

- Push path: `handle_message({"type":"stream_update", ...})`
- Cross-actor pull replay path: `handle_message({"type":"pull_batch", ...})`
- Local demo pull fallback: `process_pull_batch({"type":"pull_batch", ...})`
- Local subscription setup: `subscribe_to_hn(stream_address, mode, min_score, category, ...)`

The public `handle_message` path only accepts batches for a stream id that the
subscriber owner has already registered. The subscriber then deduplicates by
`(stream_id, sequence)`, verifies payload hash and demo signature, decodes
`payload_inline_hex` JSON, and creates keyword signals.

Async stream unsubscribe is a signed envelope because cancellation is a
destructive state change and async delivery does not authenticate
`runtime.get_sender()`. The stream records an `unsubscribe_signer` for each
subscription, then accepts:

```python
{
  "type": "unsubscribe",
  "subscriber": "0x...",
  "nonce": 1,
  "unsubscribe_sig": "0x..."  # 65-byte r || s || v secp256k1 signature
}
```

The signer signs `keccak256` of canonical JSON:

```python
{
  "domain": "cowboy.examples.17-hn-feed.unsubscribe.v1",
  "stream_actor": "0x...",
  "stream_id": "0x484e5f53545245414d...",
  "subscriber": "0x...",
  "nonce": 1
}
```

## Important Notes

- This example uses a **demo signature** (SHA-256 over canonical JSON bytes) for portability.
- CIP-7 itself requires **Ed25519 signatures** over deterministic CBOR signing payloads.
- Async unsubscribe uses Cowboy's secp256k1 account signature format (`r || s || v`) and recovers the registered `unsubscribe_signer`.
- In paid mode, stream payloads are `CIPHERTEXT` and subscribers need key-service access.
- `demo.sh` is designed to run through the shared example sweep entrypoint
  (`run_examples.sh`, optionally with `--spawn-local`) and is deterministic by default.
- This example intentionally stops short of paid subscriptions, encryption, and
  key rotation lifecycle management.

## Security Notes

- Direct `subscribe` and `unsubscribe` use `runtime.get_sender()` because those calls execute in the caller's transaction frame; in that frame the sender is the account authorizing the row mutation.
- Deferred async deliveries are not executed inside the original caller's signed transaction frame. `runtime.get_sender()` may still expose the current delivery context, but it is not proof of the subscriber or stream authority that requested the deferred work, so `handle_message` uses explicit `subscriber` / `reply_to` envelope fields and signatures for destructive async actions.
- Async `subscribe` is creation-only for a subscriber row; unauthenticated delivery cannot replace an existing row's mode, filter, or `unsubscribe_signer`.
- Async `unsubscribe` is a signed destructive action. The stream recovers the secp256k1 signer, rejects high-S signatures, and compares the recovered address to the row's `unsubscribe_signer`.
- Each signed unsubscribe includes a per-subscriber nonce, and nonces must increase to prevent replay.
- Subscriber batch delivery is gated by registered `stream_id`, payload hash verification, demo signature verification, and `(stream_id, sequence)` deduplication.

## Useful Methods

### HNStream

- `publish(...)`
- `subscribe(...)`
- `unsubscribe()`
- `get_since(cursor, limit)`
- `get_head()`
- `handle_message(...)`
- `on_timer({"tag":"ingest"})`
- `get_recent_events(limit?)`
- `get_hn_stats()`

### TradingSubscriber

- `subscribe_to_hn(...)`
- `handle_message(...)`
- `process_pull_batch(...)`
- `get_signals(limit, signal_type?)`
- `get_stats()`

## Permission Model

Cowboy actor handlers are **deny-by-default**: a handler the SDK wraps refuses external callers unless you annotate it with `@public` or `@callable_by(...)`. Both actors here are wired with the four patterns developers reach for most often in dapps — each one is exercised every time `demo.sh` runs.

### 1. Ownable initialization (atomic deploy + `assume_ownership_if_unowned`)

Each actor's `init` calls `runtime.assume_ownership_if_unowned()`:

```python
# actors/stream/main.py, actors/subscriber/main.py
def init(self, payload):
    owner = runtime.assume_ownership_if_unowned()
    ...
```

This is safe **only because Cowboy runs `init` atomically with the deploy transaction** (`sender = deployer`, no race window). `cowboy actor deploy` defaults to `--init-handler init --init-payload {}`, so `init` fires inside the deploy frame with the deployer as the caller. `demo.sh` passes `--init-payload '{"min_score":80}'` to the stream so the publish threshold is set in the same atomic frame — no separate post-deploy configuration call needed.

After init, `runtime.get_owner()` returns the deployer's address — every `@callable_by(OWNER)` handler below resolves against that value. `demo.sh` therefore does **not** call `init` as a separate `actor execute` step (which would be denied under the strict permission model); the atomic init at deploy time covers it.

### 2. Owner-only setup (`@callable_by(OWNER)`)

Lifecycle and operator-side handlers belong to the deployer:

```python
# actors/stream/main.py
@callable_by(OWNER, decode="raw")
@deferred
def on_ingest_result(self, payload): ... # ingest Algolia-shaped hits

@callable_by(OWNER, decode="raw")
@deferred
def publish(self, payload): ...          # owner-driven single/batch publish

# actors/subscriber/main.py
@callable_by(OWNER, decode="raw")
@deferred
def subscribe_to_hn(self, payload): ...  # bind subscriber to a stream

@callable_by(OWNER, decode="raw")
@deferred
def process_pull_batch(self, payload): ... # local demo/manual pull fallback
```

This is the precedent equivalent of Solidity's `onlyOwner` (OpenZeppelin `Ownable`). The stream's *publisher* role and the subscriber's *operator* role are both naturally single-principal here, so `OWNER` is the right fit. `process_pull_batch` is owner-only because it is the local demo's manual replay path after reading `get_since`; the public cross-actor replay path remains `handle_message`, which validates the registered stream id and message contents.

### 3. Public participation (`@public`)

Anyone should be able to subscribe to a public feed and read state, while delivery handlers stay public only because cross-actor messages need an exposed entrypoint. So:

```python
# actors/stream/main.py
@public(decode="raw")
def subscribe(self, payload): ...        # external subscribers register here

@public(decode="raw")
def unsubscribe(self, payload): ...      # sender cancels their own row

@public(decode="raw")
@deferred
def handle_message(self, payload): ...   # cross-actor envelope dispatch

@public(decode="raw")
@pure
def get_since(self, payload): ...        # views
@public(decode="raw")
@pure
def get_head(self, payload): ...
@public(decode="raw")
@pure
def get_hn_stats(self, payload): ...
@public(decode="raw")
@pure
def get_recent_events(self, payload): ...

# actors/subscriber/main.py
@public(decode="raw")
@deferred
def handle_message(self, payload): ...   # receives stream_update / pull_batch

@public(decode="raw")
@pure
def get_signals(self, payload): ...
@public(decode="raw")
@pure
def get_stats(self, payload): ...
```

Direct `subscribe`/`unsubscribe` calls use the *sender* as the access-control input — the body keys subscriptions off `runtime.get_sender()`, so a direct caller can only register or cancel their own row. There's no allowlist because there shouldn't be one for a public stream. Async `subscribe` is creation-only for a subscriber row; once a row exists, unauthenticated async delivery cannot replace its mode, filter, or `unsubscribe_signer`. Async `unsubscribe` requires a nonce and a recoverable secp256k1 signature from the subscription's registered `unsubscribe_signer`.

`handle_message` is `@public` because cross-actor `send_message` deliveries arrive via the recipient's dispatcher. The async delivery path is fire-and-forget across blocks, so the receiver is no longer running inside the original caller's signed transaction frame. That is why this demo cannot just call `runtime.get_sender()` to recover "who asked for this deferred action"; in this path `get_sender()` is execution context, not authority. Instead, async messages carry the intended principal explicitly and sign destructive requests:

- On the stream, direct `subscribe`/`unsubscribe` stay sender-bound; async `subscribe` can create but not update a subscriber row; async `get_since` envelopes carry the intended `subscriber` / `reply_to` explicitly; async `unsubscribe` verifies the signed cancellation envelope and rejects nonce replay.
- On the subscriber, `stream_update` and `pull_batch` messages are accepted only for a registered stream id, then checked by payload hash and the demo signature.

The demo signature is intentionally portable SHA-256, not a production Ed25519 signature. A production stream should use the CIP-7 signing scheme or another capability/signature mechanism for async message authenticity.

### 4. Timer self-fires (`@callable_by(SELF)`)

`on_timer` is the reserved handler the host invokes when a scheduled timer fires (CIP-5). The sender is the actor's own address, so `@callable_by(SELF)` matches exactly:

```python
# actors/stream/main.py
@callable_by(SELF, decode="raw")
def on_timer(self, payload): ...         # placeholder for live-ingest scheduling
```

This example doesn't enable live ingestion in the demo, but the slot is wired correctly so a real deployment can call `runtime.schedule_after(...)` without re-decorating the handler. **Common pitfall:** `@callable_by(SELF)` does **not** allow `self.foo(...)` calls from another handler — `runtime.get_sender()` returns the outermost caller, not the actor's own address. `SELF` only matches genuine timer/host-set sender flows.

### Summary

| Pattern | Used by | Precedent |
|---|---|---|
| Atomic init + `assume_ownership_if_unowned` | Both actors | OpenZeppelin `Ownable` + Initializable |
| `@callable_by(OWNER)` for setup | `on_ingest_result`, `publish`, `subscribe_to_hn`, `process_pull_batch` | `onlyOwner` modifier |
| `@public` for participation + views | `subscribe`, `unsubscribe`, `handle_message`, `get_*` | Pub/sub topic registration; permissionless reads |
| `@callable_by(SELF)` for timer self-fires | `on_timer` | Gelato/Chainlink keeper callbacks |

For the full decorator and runtime helper reference, see [CIP-6 Handler Permissions](../../../docs/cips/cip-6-sdk.md#7-handler-permissions).

## See Also

- [CIP-7: Simple Stream Protocol](../../../docs/cips/cip-7-simple-stream-protocol.md)
- [CIP-5: Native Timers](../../../docs/cips/cip-5-timers.md)
- CIP-2: Off-Chain Compute
