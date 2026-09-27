---
title: "CIP-17: Verifiable State Read RPC"
description: A single-KV-with-Merkle-proof read RPC against a Cowboy full node or Runner, returning `(value, qmdb_mmr_proof, state_root, block_height)`. Required by CIP-15 v2.r2 Gateway routes-table fetch and CIP-19 `tools/list` derivation. No new on-chain state; pure read-path addition.
icon: shield-check
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core (RPC)
  **Created:** 2026-05-11
  **Requires:** CIP-4 (State Commitment — QMDB / MMR proofs)
  **Required by:** CIP-15 v2.r2 §6 (Gateway routes-table fetch), CIP-15 gateway-implementation r2 §2.2 (Phase 1 routes resolver), CIP-19 §10.1 (MCP `tools/list` derivation)
  **Companions:** CIP-14 §8.3 (`POST /actor/read` RPC — complementary, distinct purpose)
</Note>

## 1. Abstract

This CIP specifies `GET_STATE` — a verifiable single-key state-read RPC exposed by Cowboy full nodes (Runner-side re-serving is anticipated by the §6.1 Gateway flow but is **not yet implemented** — only node RPC exposes `/state/*` today). Given an `actor_address` and a storage `key`, it returns the current value plus a QMDB/MMR operation-inclusion proof against the node's unified `state_root` at a known block height. Clients (Gateways, off-chain indexers, light clients) verify the proof locally before trusting the value.

`GET_STATE` is **not** a handler-invocation RPC — that role belongs to CIP-14 §8.3 `POST /actor/read`. `GET_STATE` is a raw, deterministic, proof-attached KV read. It is the building block CIP-15 v2.r2 needs to fetch a target actor's `__cowboy/routes` table without invoking the actor (no PVM cycles), and that CIP-19 §10.1 needs to derive `tools/list` deterministically from on-chain routes.

The current RPC layer (`node/rpc/src/rpc.rs:370-384`) exposes `/actor/{address}` and `/actors/{address}/storage` for unverified state reads. `GET_STATE` adds the Merkle-proof attachment that closes the trust model: a Gateway no longer has to trust a single Runner — it verifies the response against the unified `state_root` from a recent block.

---

## 2. Motivation

CIP-15 v2.r2 Gateway implementation depends on a per-actor `Routes` table stored at the actor's KV key `__cowboy/routes`. The Gateway fetches this table on every block of activity, caches it, and resolves incoming HTTP requests against it. Without a verifiable read:

1. **Single point of trust.** A Gateway fetching from one Runner has no way to detect a malicious or stale response. The Runner could return a forged routes table redirecting traffic.
2. **Cache invalidation theory only.** CIP-15 v2.r2 §6 says "poll `manifest_root` every `MANIFEST_POLL_INTERVAL` blocks." But polling for `state_root` changes requires a verifiable read in the first place — otherwise the polled value is itself a trust assumption.
3. **CIP-19 `tools/list` consequence.** CIP-19 §10.1 step 1 reads the routes table identically. The same trust gap applies.

The current `/actor/{address}` and `/actors/{address}/storage` endpoints (`node/rpc/src/rpc.rs:370-384`) return raw KV values with no proof. CIP-15 gateway-implementation r2 §2.2 explicitly flags this as the **single hardest blocker** for Phase 1 shipping.

The fix is a small, well-scoped addition: take the existing KV-read path, attach the QMDB/MMR operation proof against the unified state root (already maintained per CIP-4), and expose the bundle through a new RPC endpoint.

---

## 3. Design Goals

- **Verifiable.** Response carries everything a client needs to reconstruct the actor's state-root commitment for the read leaf, with no further round-trip.
- **Cheap on the server.** The proof is a single QMDB/MMR operation proof; the node maintains the authenticated state anyway (CIP-4) so generation is microseconds, not milliseconds.
- **Stateless on the client.** No subscription, no session, no pagination. One KV → one response.
- **Reusable.** Applicable to any system actor or user actor; not specialized to Gateway routes.
- **Compatible with light clients.** A future Cowboy light client (per CIP-25 §1.4 native-light-client backend) consumes the same proofs.

## 4. Non-goals

- **Handler invocation.** That is CIP-14 §8.3 `POST /actor/read`.
- **Streaming subscriptions.** A future CIP may add `SUBSCRIBE_STATE` (push notifications when a key changes); v1 here is pull-only.
- **Multi-key proofs.** v1 returns one proof per call. Batch reads with a combined proof are a future optimization.
- **Cross-actor proofs.** v1 proves inclusion of one `(actor_address, key, value)` operation against the unified state root. Cross-actor relations require multiple `GET_STATE` calls.
- **Full archival history.** v1 serves the latest committed block plus a single retained snapshot height (`GET /state/at/{height}/...`, §5.1). Arbitrary-depth historical reads across all pruned heights (needed by CIP-25 §1.4 cross-chain backends) remain out of scope.

---

## 5. RPC Endpoint

### 5.1 HTTP route

```
GET  /state/{actor_address}/{key_hex}
```

Path parameters:
- `actor_address` — 20-byte hex-encoded address with `0x` prefix (e.g. `0x0000...000D`)
- `key_hex` — hex-encoded raw KV key bytes with `0x` prefix; same encoding as used by the actor when writing via `state_set`

Query parameters:
- `prove`: boolean, default `true`. If `false`, behaves identically to the existing `/actors/{address}/storage` lookup — value only, no proof. Provided for parity with cheap unverified reads.

A **height-pinned** variant `GET /state/at/{height}/actor/{actor_address}/key/{key_hex}` (`node/rpc/src/handlers/proof.rs::get_state_at`) returns the same envelope proven against the snapshot root at `{height}`; only the latest retained snapshot height is queryable (other heights → `404`).

### 5.2 Response

```json
{
  "actor_address":  "0x0000…000D",
  "key":            "0x...",
  "value":          "0x..."  | null,
  "state_root":     "0x...",
  "block_height":   12345678,
  "block_hash":     "0x...",
  "proof": {
    "proof_version":        1,
    "loc":                  42,
    "chunk":                "0x...",
    "mmr_leaves":           100,
    "inactive_peaks":       0,
    "digests":              ["0x...", "0x...", ...],
    "partial_chunk_digest": "0x..."  | null,
    "ops_root":             "0x..."
  },
  "encoded_operation": "0x...",
  "exclusion_proof":   null,
  "absent":            false
}
```

Fields:
- **`value`** — raw bytes of the KV value, hex-encoded with `0x` prefix. `null` if the key does not exist (see `absent` below).
- **`state_root`** — the node's global unified state root at `block_height` (the single QMDB authenticated keyspace covering accounts, actors, and all actor-storage slots). Cowboy has **no** per-actor storage trie; the proof authenticates the `(actor_address, key)` operation directly against this one root. Equals the block header's state root at `block_height`.
- **`block_height`** — the block height at which the proof was generated; always the latest committed block at the time of RPC handling.
- **`block_hash`** — block-header hash for cross-verification with the node's block-explorer view.
- **`proof`** — the QMDB/MMR operation-inclusion proof (`SerializableStateProof`), the implementation-pinned proof form. It authenticates the codec-encoded state operation (the MMR leaf) against the operations-MMR root, which is then compared to `state_root`. It is **not** an MPT Merkle path. Sub-fields:
  - **`proof.proof_version`** — wire-format version (currently `1`).
  - **`proof.loc`** — the operation's location (leaf index) in the QMDB operations MMR.
  - **`proof.chunk`** — the activity-bitmap chunk (hex, 32 bytes) covering `loc`.
  - **`proof.mmr_leaves`** — the MMR leaf count the proof is taken against.
  - **`proof.inactive_peaks`** — number of inactive peaks committed by the proof, required to recompute the root under commonware's `BackwardFold` peak-bagging policy.
  - **`proof.digests`** — the MMR range-proof digests (hex, each 32 bytes Blake3).
  - **`proof.partial_chunk_digest`** — partial chunk digest from the status-bitmap tail (hex); `null` when absent.
  - **`proof.ops_root`** — the operations-MMR root (hex, 32 bytes Blake3).
- **`encoded_operation`** — hex of the codec-encoded MMR leaf (`Operation::Update{key, value, next_key}`) that `proof` authenticates. Supplied so a client verifies without re-deriving commonware's wire codec. Present only for a present key (an absent key ships its leaf inside `exclusion_proof`).
- **`absent`** — `true` iff the key does not exist at `block_height`. When absent, `proof` is omitted and a separate **`exclusion_proof`** object is returned instead (the two are mutually exclusive). `exclusion_proof` is a tagged (`kind`) union:
  - `kind: "span"` — the absent key falls strictly between two adjacent active keys: `{ op_proof, span_key, span_value, span_next_key, encoded_operation }`, where `op_proof` is a `SerializableStateProof` authenticating the bounding operation and `encoded_operation` is the codec-encoded bounding `Operation::Update` leaf.
  - `kind: "empty"` — the keyspace is empty through the latest commit: `{ op_proof, commit_value?, encoded_operation }` (bounding `Operation::CommitFloor`).
  A client verifies exclusion by MMR-checking `op_proof` over `encoded_operation` against `state_root` (same primitive as inclusion) and confirming the absent key lies in the proven span.

### 5.3 Proof verification (client-side)

```python
def verify_state_read(response, trusted_state_root):
    # 1. Confirm response.state_root == the state root of the block the client
    #    trusts out-of-band (block subscription / block_hash from a trusted source).
    if response.state_root != trusted_state_root:
        return False
    # 2. Take the codec-encoded MMR leaf: response.encoded_operation for a present
    #    key, or response.exclusion_proof.encoded_operation for an absent key.
    if response.absent:
        op, proof = response.exclusion_proof.encoded_operation, response.exclusion_proof.op_proof
    else:
        op, proof = response.encoded_operation, response.proof
    # 3. MMR-verify the operation proof against the unified state root. This
    #    reconstructs the operations-MMR root from (loc, digests, chunk,
    #    inactive_peaks, ...) and checks it equals response.state_root.
    #    (node/proof-verifier/src/state.rs::verify_state_proof →
    #     mmr::verify_operation_proof)
    if not mmr_verify_operation_proof(op, proof, response.state_root):
        return False
    # 4. For an absent key, additionally confirm the queried key lies in the
    #    proven span (span_key < key < span_next_key) or that the keyspace is empty.
    return True
```

There is a **single unified state root** — no separate per-actor storage-trie root exists (§5.2), so the earlier draft's "second proof against the block's account trie" step is moot: one MMR operation proof against `state_root` fully authenticates the `(actor_address, key)` read. The client only needs to trust the block's `state_root` out-of-band (which a Gateway already maintains for CIP-14 v2.r2 cache invalidation, and compares with `block_hash`).

### 5.4 Error responses

```
404 Not Found  (code: not_found)      — actor_address does not exist (non-system user actor), or state at this height has been pruned ("use an archive node")
400 Bad Request (code: invalid_request / invalid_address) — malformed/odd-length/oversized key hex, or malformed address
500 Internal Server Error (code: storage_error / internal_error) — node-side proof generation failure (should not occur)
503 Service Unavailable  (code: node_not_synced) — node's latest block is staler than STATE_READ_STALENESS_GRACE_MS (default 60000 ms); a stale read is refused rather than served
```

All error responses carry a structured JSON body `{ error_code, message, ... }` (Cowboy `ApiError` / `ErrorCode`), not a bare status. System / precompile actors (addresses `0x01`–`0x10`) are exempt from the "actor does not exist" 404 and return `200` + an exclusion proof for absent keys.

The "absent" case (key not in actor storage) is NOT a 404 — it returns 200 with `value: null` and `absent: true` plus a valid exclusion proof.

---

## 6. Use Cases

### 6.1 CIP-15 v2.r2 Gateway routes fetch (primary motivator)

Per `cip-15-gateway-implementation.md` r2 §2.3, the Gateway's poll loop calls:

```python
# NOTE: as of this revision only Cowboy full nodes expose /state/* (node/rpc/); a Runner
# does not yet proxy GET_STATE. Point the Gateway at a full node until Runner exposure ships.
state_resp = http_get(f"https://node.example.com/state/{actor_address}/0x{hex('__cowboy/routes')}")
if not verify_state_read(state_resp, actor_address):
    log_warning("Runner returned invalid proof; trying another runner")
    continue
routes_cbor = state_resp.value
if state_resp.state_root == cache[actor_address].state_root:
    continue  # nothing changed
cache[actor_address] = ActorRoutesCache(
    routes=cbor.decode(routes_cbor),
    state_root=state_resp.state_root,
    last_verified_block=state_resp.block_height,
)
```

### 6.2 CIP-19 `tools/list` derivation

The MCP `tools/list` generator (CIP-19 §10.1 step 1) uses the same call to fetch the routes table; the rest of §10.1 is independent of how the table was fetched.

### 6.3 Off-chain indexer audit

Indexers building cross-actor views (token balance trackers, governance vote tallies) issue `GET_STATE` calls against the canonical balance / vote keys and **verify each proof** before incorporating values into their derived state. Eliminates the "indexer trusts a single node" failure mode.

### 6.4 Light client support (future)

A Cowboy light client (per CIP-25 §1.4 "native light client" backend on a destination chain) consumes `GET_STATE` responses verbatim. The destination-chain verifier code is just the §5.3 procedure compiled into the destination's VM.

---

## 7. Implementation Sketch (non-normative)

The node side requires:

1. **One new HTTP route** in `node/rpc/src/rpc.rs` (after the existing `/actors/{address}/storage` handler at `rpc.rs:378`): `GET /state/{actor_address}/{key_hex}`. (This route + handler now live at `rpc.rs:716` / `node/rpc/src/handlers/proof.rs`.)
2. **One new method** on the existing storage interface that returns `(value, proof)` instead of just `value`. The QMDB store Cowboy uses for state already maintains the operation-proof primitives — exposing them is a few-line API surface addition.
3. **One new struct** in `node/types/src/rpc.rs` (or equivalent) for the response envelope.

Estimated implementation size: < 200 lines including tests. No new on-chain state, no new consensus, no new storage layout.

Existing primitives:
- Unified QMDB state maintained per `node/storage/src/blockchain_storage.rs` (per CIP-4).
- Block header / state-root view via `node/chain/src/engine.rs`.
- HTTP route boilerplate per other endpoints in `node/rpc/src/rpc.rs`.

The implementation is intentionally orthogonal to any other in-flight work; it lands as a Phase 0 deliverable ahead of CIP-15 v2.r2 / CIP-19 activation.

---

## 8. Relationship to Other CIPs

| CIP | Relationship |
|---|---|
| **CIP-4** (State Commitment) | Source of the QMDB/MMR proof primitives `GET_STATE` exposes. State is a single unified QMDB authenticated keyspace (not an MPT); CIP-17 does not redefine it, it only adds the RPC surface. |
| **CIP-14** §8.3 (`POST /actor/read`) | Complementary, distinct. `read_handler` invokes the actor's PVM in read-only mode; `GET_STATE` reads raw KV with proof. A Gateway needs both: `read_handler` for `GET /api/users/{id}`-style dispatched logic, `GET_STATE` for `__cowboy/routes` fetch. Different latency, different trust model. |
| **CIP-15 v2.r2** | Primary consumer. CIP-15-gateway-implementation r2 §2.2 lists `GET_STATE` as the single hardest Phase 1 prerequisite. |
| **CIP-19** | Secondary consumer. §10.1 step 1. |
| **CIP-25** §1.4 (native light client) | Future consumer; `GET_STATE` responses are exactly the leaf primitive a cross-chain light client verifies. |

---

## 9. Security Considerations

- **Trust model.** A Gateway / indexer / light client verifies the Merkle proof locally before trusting the value. A malicious node returning a forged `(value, proof)` either trips proof verification (proof doesn't reconstruct to the claimed `state_root`) or returns a `state_root` that doesn't match the block header the client has from a separate trusted source (e.g. another node, or its own block subscription).
- **Stale reads.** Responses always reflect the latest committed block at RPC handling time. Clients that need monotonic reads should compare `block_height` across successive calls and reject regression.
- **DoS surface.** Proof generation is cheap (a single QMDB/MMR operation proof), but a flood of `GET_STATE` calls against large actor states could pressure a node. Mitigation: `/state/*` has a dedicated per-IP rate limiter (`state_read_limiter`, env `STATE_READ_RATE_PER_SEC`) in addition to the node's configurable global RPC limiter (`RPC_GLOBAL_RATE_LIMIT_PER_SEC`), both in `node/rpc/src/rpc.rs`. Oversized keys are rejected pre-DB (`MAX_ACTOR_KV_KEY_BYTES`), and stale-node reads are refused (503) rather than served.
- **Absent-key fingerprinting.** Returning an exclusion proof reveals "this key does not exist" deterministically. This is the same information already exposed by `/actors/{address}/storage`; no new leak.
- **Key encoding edge cases.** Implementations MUST decode `key_hex` strictly (reject non-hex chars, odd length, etc.) to prevent ambiguity between, e.g., `0x__cowboy/routes` (literal nine-byte string) and a typoed escape sequence.

---

## 10. Backwards Compatibility

Fully additive. New HTTP route; no existing RPC, on-chain state, or PVM syscall is modified. Clients that don't speak `GET_STATE` continue to use `/actor/{address}` and `/actors/{address}/storage` unchanged.

If `GET_STATE` lands in node code, the `cip-15-gateway-implementation` companion's §9 open-question item 1 is resolved.

---

## 11. Future Work

- **Full archival history.** Reads at arbitrary pruned heights — v1 retains only the latest snapshot for the height-pinned read (§5.1); needed by CIP-25 §1.4 native light client backend.
- **Batch reads.** `POST /state/batch` with multiple `(actor, key)` pairs returning combined proofs.
- **Subscribe API.** WebSocket-based push when a watched key changes — useful for Gateway cache invalidation tighter than CIP-15's `MANIFEST_POLL_INTERVAL`.
- **Bundled account-trie proof.** Resolve §5.3 limitation by returning both the actor's storage proof AND the account-trie proof for `actor_address.state_root` in one response.
- **CIP-25 cross-chain extension.** A cross-chain L1 anchor (CIP-25 §1) can carry `GET_STATE`-style proofs over the L1 mailbox, allowing destination-chain L3 apps to verify Cowboy state directly.

---

## 12. References

- `node/rpc/src/rpc.rs:370-384` — existing unverified state endpoints; `/state/*` GET_STATE routes at `rpc.rs:716` → `node/rpc/src/handlers/proof.rs::get_state`
- `node/storage/src/blockchain_storage.rs` — unified QMDB state + proof generation
- CIP-4 — QMDB / MMR state-proof primitives
- CIP-14 §8.3 — `POST /actor/read` (complementary RPC)
- CIP-15 v2.r2 §6 + `cip-15-gateway-implementation.md` r2 §2.2 — primary use case
- CIP-19 §10.1 — secondary use case
- CIP-25 §1.4 — native light client future use case
