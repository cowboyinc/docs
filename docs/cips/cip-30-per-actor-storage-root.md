---
title: "CIP-30: Per-Actor Storage Root"
description: A normative storage layout in which each actor's storage forms its own Merkle subtree with a root committed on the Actor record, enabling O(1) cloning, per-actor proofs, and correct enumeration over storage keys of any length.
icon: database
---

<Note>
  **Status:** Draft<br/>
  **Type:** Standards Track<br/>
  **Category:** Core / Storage<br/>
  **Created:** 2026-05-26<br/>
  **Requires:** CIP-3 (Fee Model), CIP-4 (Storage / QMDB), CIP-6 (Actor record)<br/>
  **Required by:** CIP-27 (Actor `fork()`)<br/>
</Note>

## 1. Abstract

This proposal redefines how a Cowboy actor's storage is laid out on chain: instead of a flat key-value space `(actor_address, key) → value` with no per-actor commitment, each actor's storage MUST form its own Merkle subtree whose root — `storage_root` — is committed on that actor's `Actor` record.

Three downstream consequences follow directly:

- `fork()` (CIP-27) can clone state in **O(1)** by sharing the parent's `storage_root` (copy-on-write subtree), with no enumeration of keys.
- Cowboy's per-block, per-actor `state_root` (CIP-17) and the Gateway's verifiable-read flow (CIP-15 v2.r2) become trivially derivable.
- Snapshots, rollback boundaries, and cross-actor proofs gain a single committed object — `storage_root` — to refer to, instead of a physical key set that must be enumerated or copied wholesale.

This CIP is a prerequisite for CIP-27 and a foundation for any future primitive that needs to refer to "an actor's state" as a single committed object.

## 2. Motivation

### 2.1 Cowboy actor storage today

Today, actor storage is a flat key-value mapping committed into the chain's single global state trie:

```
state-store: (actor_address, key) → value
```

There is no *live, per-block* per-actor commitment. The `Actor` record carries `code_hash`, `manifest`, `nonce`, `children_count`, `parent` (CIP-27), and `pre_evict_storage_hash` (CIP-4 §12.3) — the last a hash over the actor's entire storage, but computed *only at eviction* and as a flat blob (no Merkle structure, so no per-key proofs and no structural sharing). The chain has one global `state_root` covering everything, and there is no `Actor.storage_root` committed every block. This CIP adds one.

### 2.2 Three problems this causes

**(a) `fork()` copies state in O(parent-size).** CIP-27 specifies a `fork()` primitive that copies a parent actor's storage to a child actor at a new address. Without a per-actor storage root, the implementation must physically clone every storage row, re-keyed under the child's address. The actor-storage slot is address-independent, so the copy reaches hashed long keys (>32 bytes) as well as verbatim ones — but it is still a *full* copy: **O(parent-storage)** in both work and persistent bytes, paid in full on every fork.

A per-actor `storage_root` lets `fork()` take `parent.storage_root` as the child's base (then apply CIP-27 §3.3's birth writes — see §3.4) instead of enumerating keys. The child sees the *entire* parent state as its base, regardless of key length, because the subtree is referenced by hash, not by enumeration. Cloning becomes **O(1)** in work; structural sharing keeps it O(1) in storage too until either actor diverges.

**(b) No clean per-actor proofs.** CIP-17 (verifiable state proofs) and CIP-15 (the Gateway's verifiable-read flow) want to produce, at every block, a per-actor commitment that a light client or off-chain verifier can inspect. With a flat global trie, "the proof for actor X's storage" is a set of inclusion proofs against the global root, scoped to the `X | key` prefix — a per-key proof, not a per-actor proof. A reader that wants "give me actor X's whole state as of block N" must either enumerate X's keys (back to the enumeration limit) or chase per-key proofs separately.

With `Actor.storage_root`, the chain commits one digest per actor per block. Anyone can ask "give me the proof for actor X's storage at block N" and receive a single inclusion proof of `Actor.storage_root` against the global state root, plus opens against that subtree for whichever keys they read.

**(c) Future composability.** A per-actor commitment is the foundation for state snapshots, rollback boundaries, cross-actor proofs ("at block N actor X's storage_root was Y; therefore at block N+k actor X's storage included key K=V if and only if the Merkle path holds"), and (eventually) actor-state migration / transfer primitives. None of these are buildable cleanly without per-actor roots.

## 3. Specification

### 3.1 The `storage_root` field

The `Actor` record (CIP-6 §6, codec defined in `cowboy_types::execution`) MUST carry an additional field:

```
storage_root: Digest    // 32-byte root of this actor's storage subtree
```

For an actor with no storage entries, `storage_root` MUST equal the empty-trie root `D_256` defined in §3.2.

A new codec version (compatible with the codec versioning convention used by `Actor`) MUST be assigned for the field addition.

### 3.2 Storage layout: per-actor subtrie (normative)

Each actor's storage MUST be a Merkle trie keyed by storage-key (of any length), with values being the stored bytes. The chain's global state MUST commit each actor's `Actor` record — including its `storage_root` — into the global state root. The actor's storage subtree itself MAY be persisted as part of the same physical state DB, but logically each actor's keys live under their own root.

The trie scheme is **normatively fixed** (resolving the former §7 open question) as a **binary Sparse Merkle Trie of depth 256 over BLAKE3-derived paths**:

- **Hash function:** BLAKE3 with 32-byte output, for every node hash below. BLAKE3 is the hash the chain already uses for its global state root, so the collision/second-preimage argument of §8 transfers directly.
- **Path derivation:** `path(key) = BLAKE3(key)`, 32 bytes. The trie is traversed by the bits of `path` most-significant-bit first; bit `d` (0-indexed from the MSB of byte 0) selects the branch at depth `d`. Leaves sit at depth 256: a node at depth 256 **is** the leaf hash itself — it is never wrapped in an `internal` node, and there is no bit 256 to consume.
- **Leaf hash:** `leaf(key, value) = BLAKE3(0x00 || u64_be(len(key)) || key || value)`. The leaf commits to the **key preimage**, not just its path — a proof therefore authenticates the exact key bytes, and an implementation that stores `(key, value)` in the leaf record can enumerate every entry (including keys longer than 32 bytes) by walking the trie, which is what §2.2(a) requires of `fork()`-adjacent tooling.
- **Internal node hash:** `internal(left, right) = BLAKE3(0x01 || left || right)`.
- **Empty-subtree defaults:** `D_0 = BLAKE3(0x02)` (the single byte `0x02`), and `D_{i+1} = BLAKE3(0x01 || D_i || D_i)`. A missing subtree whose leaves would sit `i` levels below the current node contributes `D_i` — equivalently, an empty subtree rooted at depth `d` contributes `D_{256-d}`, so an absent leaf slot contributes `D_0`. The **empty-trie root** is `D_256`:

  ```
  D_0   = ab13bedf42e84bae0f7c62c7dd6a8ada571e8829bed6ea558217f0361b5e25d0
  D_256 = 1d6280720f011147106d9086a21764ba0c2baaa27cb29b8474ef20ee649e5fb9   // storage_root of an actor with no entries
  ```

The three domain-separation tags (`0x00` leaf, `0x01` internal, `0x02` empty) make the node kinds mutually non-confusable under a single hash function.

**Representation freedom.** The depth-256 trie is a *logical* definition. Implementations MAY (and for performance SHOULD) use a compressed/sparse representation — skipping default subtrees, collapsing single-child paths, caching the `D_i` table — provided the resulting root equals the logical definition's root bit-for-bit. Structural sharing between two actors whose subtrees are identical is what makes O(1) `fork()` work in storage as well as in CPU; a persistent (immutable-node) representation provides it naturally.

**Why not the alternatives.** Patricia/MPT is rejected: its extension/branch node layout and RLP-era encoding rules are a substantially larger conformance surface for no benefit here. A sorted-leaf binary Merkle tree is rejected: inserting a key shifts the leaf indices of its neighbors, so two nearly-identical actor states share almost no internal nodes — destroying the structural sharing that O(1) `fork()` depends on. Reusing the chain's global state structure (QMDB's internal merkleization) is not available as a per-actor scheme: it is not a key-pathed trie and does not expose prefix-rooted subtree roots or copy-on-write sharing between two named roots. What *is* shared with the global state is the hash function (BLAKE3).

### 3.3 Storage syscall semantics

Every state-write host syscall — `state_set(key, value)`, `state_delete(key)`, and any wrapper thereof — MUST update the calling actor's storage trie and recompute `Actor.storage_root` in the cross-call write-set. The recomputed root is committed on transaction commit and rolled back on transaction revert via the existing snapshot machinery (CIP-6 §state, the `sync_called_actors`/`sync_called_writesets` path in the host).

Read syscalls (`state_get`, `state_scan_prefix`) and the store-level `scan_actor_storage_paginated` MUST read against the actor's current `storage_root` as visible to the call. Their semantics are otherwise unchanged.

Gas for `state_set` / `state_delete` MUST add the cost of the trie update on top of the per-byte Cell cost already specified in CIP-3 §2.2. The additional cost is a function of the trie depth touched, which is bounded by the fixed scheme depth (256). Before the scheme becomes consensus-live, CIP-3 (or a revision of this CIP) MUST fix a **deterministic, bounded** cost formula for the trie update — deterministic so all validators charge identically, and bounded so an actor with large storage cannot make writes arbitrarily expensive for honest callers (§8). Until that formula is ratified the scheme MUST NOT be activated on any network with untrusted callers.

### 3.4 `fork()` semantics under this CIP

CIP-27 `fork()` MUST, instead of enumerating and copying keys, take the parent's `storage_root` as the child's **base** root:

```
child.storage_root  :=  parent.storage_root         # full mode: the shared base
                    (then apply the CIP-27 §3.3 birth writes — see below)
```

The child's storage IS the parent's *as the base*, as of the moment of fork. Structural sharing in the trie keeps physical storage O(1) until either actor diverges; thereafter copy-on-write at the trie node level keeps the marginal storage cost proportional to what each diverging actor writes.

**The final root is base + birth writes, never the parent root verbatim.** CIP-27's amended §3.3 seeds host-written birth slots (`__INITIALIZED__`, `__FORK_MODE__`, `__FORK_PAYLOAD_HASH__`, and conditionally `__OWNER__`) into every child; those are ordinary copy-on-write writes on top of the shared base, so the committed `child.storage_root` is a deterministic function of `parent.storage_root` plus those writes. Clean mode's base is the empty root, and its final root is exactly the seeded slots. This is stated normatively in **both** this CIP and CIP-27 §3.3 (per the resolution of closed issue #236). The O(1) structural-sharing property is unaffected — birth writes touch a fixed, tiny number of slots.

CIP-27 §3.8's gas schedule for fork is revised accordingly: the O(parent-storage-size) "per-key copy" cost disappears (replaced, until the CIP-30 subtree share is implemented, by a snapshot-precharged interim per §3.8), but **the base actor-record write and the constant-time root share are not the whole cost** — CIP-27 §3.8 is the authoritative, complete fork schedule (birth slots, clean-mode payload, fork profile, inherited library-pin rows, endowment/quota-bond legs, residue purge, each as it applies). This CIP does not restate those line items; it defers to CIP-27 §3.8 so the two cannot drift.

### 3.5 Proofs

A verifier that holds the chain's global state root for block N can, given an inclusion proof of an `Actor` record into the global root, derive `Actor.storage_root`. From that root the verifier MAY open any number of (key, value) pairs in the actor's storage with standard Merkle proofs. Implementations MUST expose an RPC endpoint that returns `Actor.storage_root` and accepts requests for opens against it; the exact endpoint shape is out of scope for this CIP and is specified in CIP-15 / CIP-17.

### 3.6 Determinism

The trie's root MUST be a deterministic function of the (key, value) set in the actor's storage. §3.2 fixes the scheme — hash function, path derivation, node layout, domain tags, and the empty-trie root constant — so two conforming implementations MUST produce identical roots for identical entry sets regardless of insertion order or internal representation. Cross-client divergence is a conformance-test failure (§3.7), not a configuration option.

### 3.7 Test vectors

Conforming implementations MUST reproduce the following roots (keys/values are raw ASCII bytes):

| # | Entries | `storage_root` |
|---|---------|----------------|
| 1 | (none) | `1d6280720f011147106d9086a21764ba0c2baaa27cb29b8474ef20ee649e5fb9` |
| 2 | `"k" → "v"` | `294874ee5b76fc77cdb18a48e412898d1ea12488f5ef3db0dfa7aae1ea092e5b` |
| 3 | `"a" → "1"`, `"b" → "2"` | `1caab568adb5f773a0f70d9119eb000ccb979780f79adf221ea5d028ce98d460` |

Intermediate values for vector 2: `path("k") = 5cbcb0cee824b91866cd67f57a6643ddcb7cd4382029039043513953c759246b`, `leaf("k","v") = 5297891bd179324abddda465abf23a8ea661108ba2cedc077512ff6af0a425cd`; the root folds the leaf against the default `D_i` ladder along the path. The reference implementation used to generate these vectors is a direct transcription of §3.2 (recursive split on path bits, `D_i` for missing subtrees).

### 3.8 Relationship to CIP-4 (state architecture)

CIP-4 (rev 2026-03-27) adopts **QMDB** — a flat key-value store with BLAKE3 Merkle commitments and fixed 54-byte keys — as the chain's **canonical global state repository**, explicitly choosing it over the original Merkle-Patricia-Trie design to avoid MPT rebalancing cost. CIP-30 does **not** reverse that decision and does **not** reintroduce a trie for global state:

- **QMDB remains the global store.** The `Actor` record (with its new `storage_root` field, §3.1) and every other state object continue to be committed into the global state root exactly as CIP-4 specifies. CIP-30 changes what the `Actor` leaf *contains*, not how global state is stored or merkleized.
- **`storage_root` is an additional per-actor *logical* commitment**, not a replacement store. It is the root of the per-actor Sparse Merkle Trie of §3.2 (keyed by `BLAKE3(key)`, address-independent), maintained *alongside* QMDB and folded into the actor leaf that QMDB already commits. It adds a commitment; it does not move the source of truth off QMDB.
- **Why a separate structure rather than a QMDB sub-root.** QMDB's internal merkleization is not a key-pathed trie and exposes neither prefix-rooted subtree roots nor copy-on-write sharing between two independently named roots (§3.2, "Why not the alternatives"). A per-actor `storage_root` that supports O(1) `fork()` (§3.4) therefore cannot be derived as a range/prefix commitment over QMDB's keyspace; it requires the dedicated per-actor SMT defined here. Both structures share the chain's hash function (BLAKE3), so CIP-4's collision/second-preimage argument transfers unchanged.

CIP-30 thus **amends CIP-4** only by extending the `Actor` record and adding the per-actor commitment; it leaves CIP-4's QMDB-as-global-store decision in force. The storage invariants in §4 apply to every actor.

## 4. Storage Initialization

Every actor MUST carry the address-independent storage commitment defined in §3. New actors begin with the canonical empty root plus any atomic initialization writes. Under §3.4, full-mode forks share the parent root, while clean-mode forks start from the canonical empty root. Both apply their own birth writes before becoming visible.

All storage producers, readers, snapshots, and rollback paths MUST use the same commitment scheme and actor-record encoding. Initialization and conformance tests MUST include long hashed keys; omitting them from a commitment would make the root disagree with the actor's accessible state.

## 5. Rationale

### 5.1 Why per-actor, not one global trie

Cowboy already has one global state trie. Why give each actor its own subtree?

- **Locality of writes.** An actor's handler touches only that actor's keys (cross-actor writes go through `call()` and surface as that callee's writes). A per-actor subtree localizes the update path, keeps subtree depth bounded by per-actor key count, and avoids re-hashing nodes shared with unrelated actors on every write.
- **Cloning.** Structural sharing only makes sense within a subtree that's named as a unit. Without `Actor.storage_root`, there's nothing for `fork()` to copy by reference.
- **Proof shape.** Per-actor proofs are the natural unit of "this is what this actor's state was." A flat global trie forces every per-actor question into a per-key shape.
- **Snapshot / rollback boundaries.** Future primitives that want "actor X's state, atomically" want a single root to refer to.

### 5.2 Why the implementation can still share one physical DB

A logical per-actor subtree does NOT require a separate physical store per actor. Because the trie is keyed by address-independent paths (`path = BLAKE3(key)`, §3.2), an actor's `storage_root` depends only on that actor's own `(key, value)` set — which is exactly what lets `fork()` share it by reference (§3.4). Implementations MAY persist every actor's trie nodes in a single content-addressed node store (nodes keyed by their own hash) within the same physical state DB; identical subtrees across actors are then naturally shared, and a persistent (immutable-node) representation gives copy-on-write divergence for free.

What an implementation MUST NOT do is derive `storage_root` as a prefix-scoped subtree of a global `(actor_address || key)` trie. Such a root is address-*dependent*: two actors with identical `(key, value)` sets but different addresses would commit to different roots, so `child.storage_root = parent.storage_root` (§3.4) would be meaningless and the O(1) `fork()` structural sharing this CIP exists to enable would be lost. This CIP constrains the logical commitment (§3.2), not the storage representation — but the representation MUST reproduce that address-independent root bit-for-bit.

### 5.3 Why this CIP, not folded into CIP-27

`fork()` is the proximate consumer, but per-actor storage roots are a general primitive — they unlock CIP-15 verifiable reads, CIP-17 proofs, and future composability beyond fork. Keeping the CIPs separate lets the storage-root design proceed on its own merits and be cited (by `Requires:`) by every primitive that needs it.

## 6. Reference implementation

Implementation responsibilities in `cowboyinc/node`:

- `cowboy_types::execution::Actor` — encode `storage_root: Digest` in the canonical Actor record.
- `cowboy_storage` — add a per-actor trie commitment to the state store, with cross-call write-set tracking the root delta alongside the existing key-value writes.
- `cowboy_execution::pvm_host` — `state_set` / `state_delete` recompute and stage the actor's `storage_root` on each call; commit/rollback paths propagate it identically to other write-set entries.
- `cowboy_execution::execution::actor_instruction::DeployActor` and the existing `fork_actor` host syscall (`cowboy_execution::pvm_host`, CIP-27) — initialize / propagate `storage_root` correctly at actor creation and on clone.

The `fork_actor` path MUST use O(1) subtree sharing, followed by the child-specific initialization writes, rather than copying every parent storage row.

## 7. Open questions

- **Trie scheme conformance.** §3.2 normatively fixes a depth-256 Sparse Merkle Trie over BLAKE3 paths, with BLAKE3 node hashing, domain-separated leaf/internal/empty tags, and a fixed empty-trie root constant. The deciding factors: BLAKE3 is already the chain's state-root hash; the global state's QMDB merkleization is not a key-pathed trie and so could not be reused per-actor; and the SMT is the smallest conformance surface that still gives the structural sharing O(1) `fork()` needs. Conformance is enforced by the §3.7 test vectors.
- **Pre-image table for hashed keys.** Even with per-actor roots, debugger / explorer flows that want to enumerate human-readable keys still need pre-images for hashed-long-key entries. That is a tooling question, orthogonal to fork correctness, but worth raising.
- **Gas attribution for trie updates.** Recomputing a root on write touches trie nodes along the path (bounded by the scheme depth, 256). Per §3.3, CIP-3 (or a revision of this CIP) MUST fix a deterministic, bounded cost formula before the scheme goes consensus-live; the exact constants are to be set from measurement against the reference scheme.
- **Node garbage collection.** A persistent (immutable-node) representation is what gives structural sharing and O(1) `fork()` (§3.2, §3.4), but it means superseded trie nodes are never overwritten, so orphaned nodes accumulate as actors write. An implementation MUST choose a reclamation strategy (reference counting, versioned pruning, or archival compaction); the choice is a representation concern (it does not affect the committed root) but it MUST be settled before the scheme goes consensus-live to bound state growth.

## 8. Security considerations

- **Trie scheme determinism.** Validators that disagree on the trie scheme — node ordering, hash function, empty-trie root — fork the chain. The scheme MUST be consensus-fixed; client teams MUST cross-test root computation against the reference implementation.
- **Commitment completeness.** Root construction MUST include every actor key, including hashed long keys. Initialization, snapshot restoration, writes, and rollback MUST preserve the same complete commitment (§4).
- **Proof forgery.** Standard Merkle-trie security applies. The hash function MUST be the same one the chain uses for its global state root (collision-resistance and second-preimage-resistance argumentation transfers).
- **DoS via deep tries.** An actor that stores many keys produces a deep trie; writes traverse all the depth they touch. Gas (§3.3) MUST bound this so an adversary cannot make an actor's writes arbitrarily expensive for honest callers.
- **State growth via orphaned nodes.** The immutable-node representation that enables structural sharing (§3.2, §5.2) never overwrites superseded nodes, so churny writes leave permanent orphaned nodes behind. Without the reclamation strategy required in §7, an adversary can inflate persistent state cheaply (each write is gas-bounded, but the residue is unbounded over time). The node-GC/pruning scheme MUST be in place before activation, and its cost model MUST be consistent with the §3.3 gas so that leaving garbage is not economically free.
