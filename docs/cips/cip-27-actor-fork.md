---
title: "CIP-27: Actor fork() — Self-Cloning Primitive"
description: A runtime primitive that lets an on-chain actor clone itself — same code, same state — into a new actor at a deterministic address, with the forking actor recorded as the child's parent.
icon: code-fork
---

<Note>
  **Status:** Draft<br/>
  **Type:** Standards Track<br/>
  **Category:** Runtime / SDK<br/>
  **Created:** 2026-05-22<br/>
  **Requires:** CIP-1 (Actor Message Scheduler), CIP-3 (Fee Model — amended: fork schedule + fee denomination), CIP-4 §12 (Storage Quota, Bond & Rent — amended: shared bond routine), CIP-6 (Python SDK & Actor API), CIP-26 (Account Libraries — amended: fork pin initialization), CIP-29 (On-Chain Event Hooks — amended: `subscribe_event` emitter-existence guard, §3.2 class-B residue), CIP-30 (Per-Actor Storage Root)<br/>
</Note>

## 1. Abstract

This proposal defines **`fork()`** — a runtime syscall that lets a running on-chain actor instantiate a new actor from *its own code*, in one of two modes: **full** (same code, same storage — the byte-exact clone) or **clean** (same code, empty application state — the instance-spawning form). The child gets a fresh deterministic address, an explicitly designated or defaulted **owner**, an inheritance-validated manifest, and the forking actor recorded as its **parent**.

`fork()` is modeled on Unix `fork(2)`. It is *self-clone only* — an actor may fork itself, never some other actor. It is synchronous: the call returns the child's address to the caller within the same handler, so the parent can immediately wire the child into the running computation.

`fork()` is the deployment primitive an on-chain actor can use **with no off-chain dependency** — because the child's code is the parent's own code, already content-addressed on chain. It complements, and does not replace, the existing fresh-actor deployment path (`DeployActor`, CIP-6 §6.3).

## 2. Motivation

Cowboy actors today are brought into existence in one of two ways:

1. **`DeployActor`** — a transaction (or a manifest-`exec.spawn`-entitled actor) deploys an actor from supplied code bytes.
2. Nothing else.

Multi-actor systems — actor swarms, per-entity actors, fan-out workloads — want an actor to *bring more actors into being at runtime*. `DeployActor` can do this (the chain already accepts an actor as the deploy sender, gated by `exec.spawn`), but it has an irreducible bootstrapping problem: **a fresh-code child needs code**, and an on-chain actor has no filesystem. The deployer must reference a `code_hash` that something *off-chain* published first. You never fully escape the off-chain step.

`fork()` closes that loop. The child's code is the parent's code — already content-addressed on chain, already validated. An actor can reproduce with **zero off-chain dependency**. This is the missing primitive for genuinely self-contained on-chain actor systems: a coordinator actor that spawns workers, a template actor that stamps out instances, a game `City` that raises units.

### 2.1 `fork` is not `spawn`

`fork` and `spawn`/`DeployActor` are different operations and both are wanted:

| | `spawn` / `DeployActor` | `fork` |
|---|---|---|
| Child code | supplied by caller (a `code_hash`) | the caller's own code |
| Child state | empty; `init` constructs it | full mode: exact copy; clean mode: empty, `on_fork_clean` constructs it |
| `init` runs? | yes | no — `on_fork`/`on_fork_clean` takes its place |
| Off-chain dependency | yes (code must be pre-published) | none |

`spawn` means *fresh process from a definition* (cf. Erlang). `fork` means *copy of the caller* (cf. Unix). They are kept as distinct verbs.

## 3. Specification

### 3.1 The `fork()` syscall

The runtime MUST expose a host syscall, surfaced in the SDK as:

```python
from cowboy_sdk import fork

child_address = fork()                          # full copy, owner inherited
child_address = fork(endowment=100_000_000)     # seed child with 100_000_000 wei
child_address = fork(new_owner=hirer_addr)      # full copy, child owned by hirer
child_address = fork(mode="clean",              # code only, no storage —
                     new_owner=hirer_addr,      #   the instance-spawning form
                     payload=hire_config)
```

`fork()`:

- MUST be callable only from within an actor handler. It clones the **currently executing actor** — it takes no target argument.
- MUST be synchronous: it creates the child and returns the child's 20-byte address before the calling handler continues.
- MUST be metered (§3.8) and MUST honor the **read-only execution flag** per CIP-6 §8.1: a `read_only` call MUST reject `fork()` with `HostError::Forbidden`.
- MUST count against the cross-actor call-depth limit defined for `call()` (CIP-6 §9.1); a `fork()` whose `on_fork` callback would exceed call depth is rejected.
- MUST accept an `endowment` argument (wei, non-negative integer; default `0`). When `endowment > 0`, the runtime MUST atomically debit `endowment` from the parent's native CBY balance and credit the same amount to the child as part of the fork — see §3.3.1.
- MUST accept a `new_owner` argument (20-byte address; default `None`). When given, the child's owner slot (`__OWNER__`) is set to `new_owner` as part of the fork — after the storage copy in full mode, so the designation overrides the inherited value — and before any callback runs. The zero address is rejected at the host, mirroring `transfer_ownership`. When omitted: full mode carries the parent's owner slot **verbatim, including an explicitly renounced or never-set owner** — the child's effective owner state equals the parent's, and a parent with no owner produces a child with no owner only through this deliberate verbatim rule; clean mode defaults the owner to the **forking actor itself**, never to unset — an unowned child would be claimable by any first caller under the ownership bootstrap rule, a front-run window no fork should open. Owner state has no bearing on capability inheritance — §3.7.1 binds that to the actor-identity boundary unconditionally.
- MUST accept a `mode` argument: `"full"` (default — the copy semantics of §3.3) or `"clean"` (§3.3.2 — code only, no storage).
- MUST accept a `payload` argument (bytes; clean mode only). It is delivered verbatim to the child's `on_fork_clean` callback (§3.5.1) as its initialization input, and is bounded by `MAX_INLINE_BLOB_BYTES` (64 KiB, the standard inline-blob cap); larger initialization data goes through CBFS by reference. Its bytes are charged as `payload.len()` Cells at the standard inline-blob rate, and the seeded lifecycle and receipt slots are charged as ordinary reserved-slot storage writes — exact constants land with the implementation in the CIP-3 gas schedule. Raw-host ABI: parameter order, encodings, and per-error result codes for the new rejections are specified alongside the host function, mirroring the existing fork ABI conventions.
- MUST accept a `child_manifest` argument (the §3.7.1 fork profile; default `None` = verbatim manifest). Encoding is an explicit `Option`: omitted means verbatim, `Some(manifest)` means the profile — and `Some` of an **empty** manifest is a valid, distinct value (a child with no grants). A manifest-bearing parent can never profile a child to the *legacy no-manifest* state — `None`-manifested actors predate entitlements and fail open, so the host rejects any construction that would produce one from a manifested parent. Profile bytes are bounded by `MAX_FORK_PROFILE_BYTES` (16,384 — the one cap, defined below; no deploy-time manifest byte cap exists to reference) and metered per §3.8; parameter order and stable error codes are fixed with the host ABI.
- Every parameter rule above is enforced by the **host**, before any staging — not by the SDK: a zero or malformed `new_owner`, an unknown `mode`, `payload` in full mode, or an over-cap payload each reject the syscall. Actor code can reach the raw host interface, so SDK-side `ValueError`s are ergonomics, not the security boundary. These parameters and both modes are MUST-support for a conforming runtime, not optional extensions.

The raw host signature is **normative here**, replacing the v1 prototype's `fork_actor(u64) -> address`:

```
fork_actor(
  mode: u8,                 // 0 = full, 1 = clean; other values reject
  endowment: u64,           // wei — the native ledger (`Account.balance`, `transfer_balance`) is u64;
                            //   the SDK rejects integers exceeding u64::MAX before the host call
  new_owner: Option<[u8;20]>,   // None = 0x00 tag; Some = 0x01 tag + 20 bytes; zero address rejects
  child_manifest: Option<Bytes>,// None = 0x00 tag; Some = 0x01 tag + length-prefixed manifest codec bytes
  payload: Option<Bytes>,       // clean mode only; rejects in full mode; ≤ MAX_INLINE_BLOB_BYTES;
                                //   None and Some(empty) are both valid encodings and semantically
                                //   identical — the callback receives zero bytes and
                                //   __FORK_PAYLOAD_HASH__ = keccak256 of the empty byte string
) -> Result<[u8;20], HostError>
```

The signature above is the **logical ABI**, and its canonical carrier is what actually exists: the **in-process `HostApi` trait** — Python invokes the host directly through the `pvm-runtime` module bindings into `pvm-host`; there is no serialized host-call transport and no wire version to bump; any out-of-process transport must define its own tagged encoding. The ABI has two layers: the Rust trait signature exactly as shown (`fork_actor(mode: u8, endowment: u64, new_owner: Option<[u8;20]>, child_manifest: Option<Vec<u8>>, payload: Option<Vec<u8>>) -> Result<[u8;20], HostError>`), and the Python→Rust conversion at the binding layer: integers range-checked into `u8`/`u64` (out-of-range raises before the host is called), `bytes` → `Vec<u8>`, Python `None`/value → `Option`, and a `HostError` return surfacing as a typed exception carrying the numeric code — so actor code branches on the same pinned codes regardless of SDK sugar. Malformed-argument vectors exercise the binding layer.

Error variants, each with a **pinned numeric code** in the shared `HostError` table (codes 1–8 and 100–117 are occupied, including `NestedOutOfGas` = 116 and CIP-29's `EventSchemaMismatch` = 117; the fork block takes 120+): `InvalidForkMode` = 120, `InvalidOwner` = 121, `PayloadInFullMode` = 122, `PayloadTooLarge` = 123, `ForkProfileNotSubset` = 124, `NonInheritableGrant` = 125 (code 126 is deliberately unassigned — a withdrawn draft error; later codes keep their pinned values), `MissingForkInitializer` = 127, `ForkCountOverflow` = 128, `AddressCollision` = 129, `InsufficientQuotaBond` = 130 (§3.3.3), `QuotaExceedsMax` = 131 (§3.3.3), `KvCountExceedsMax` = 132 (§3.3.3), `BalanceOverflow` = 133 (every funding-path checked-add overflow: parent-side `endowment + bond_wei` at the order's parent funding step, child-side `pre-funded + endowment` at its child funding step). `PinSetMismatch` = 134 belongs to CIP-26's upgrade rule (import-name equality on `upgrade_self`/`UpgradeActor`) and is allocated from this table so codes stay collision-free. Overflow inside the **bond computation itself** (`bond_rate_atto × delta` or the wei conversion, §3.3.3) is `QuotaExceedsMax` = 131 at the order's cap step — the bond is unpayable at any balance, which is a size problem, not a funds problem. Endowment failures reuse the existing `InsufficientCbyBalance` = 107; `Forbidden` = 5 covers read-only and call-depth rejections as it does for `call()`; malformed or invalid `child_manifest` content reuses `InvalidInput` = 2; gas exhaustion is `OutOfGas` = 1. There is no dirty-binding error: §3.3.2's frame-entry rule makes fork after a same-handler `upgrade_self` *succeed* from the entry tuple — rejection is explicitly not an allowed implementation.

Bounds: `payload ≤ MAX_INLINE_BLOB_BYTES` (64 KiB); `child_manifest ≤ MAX_FORK_PROFILE_BYTES` = 16,384 — a new constant this amendment defines (no deploy-time manifest byte cap exists today to reference). Exact-max and cap+1 vectors are pinned for both.

**Validation order is normative and total** — actor code can catch and branch on `HostError`, so validators disagreeing on which of several simultaneous defects reports first is consensus divergence. Every rejection path the syscall has appears in this one order (no other text in this CIP establishes precedence; where earlier sections say a check happens "before X", they are restating this order, which governs). First failure wins:

1. binding-layer argument conversion (range/type failures raise before the host runs — the §3.1 conversion rules);
2. `read_only` flag → `Forbidden` (5);
3. cross-actor call depth (§3.1) → `Forbidden` (5), as for `call()`;
4. `mode` valid → 120;
5. `payload` present in full mode → 122;
6. `payload` over cap → 123;
7. `child_manifest` over cap → 123 (`PayloadTooLarge` covers both over-cap byte fields; the error detail names the field);
8. `new_owner` zero address → 121 — wrong length is unrepresentable in `Option<[u8;20]>` and fails at step 1 (binding conversion), a precedence pinned by a wrong-length vector;
9. `exec.fork` entitlement and `max_children` quota → 7 / 8 (existing codes); `econ.transfer` **presence** when `endowment > 0` → 7 (its quota consumption waits for step 14, when the bond is known);
10. callback-marker existence (clean without `on_fork_clean`) → 127;
11. `child_manifest` **content**: canonical decode (strict, no trailing bytes) and full manifest validation — registry/schema conformance, duplicate and unsorted entries — → `InvalidInput` (2), exactly the `DeployActor` sequence (decode, `validate_manifest`, *then* inheritance — inheritance is not a parser); then inheritance validation — subset → 124, non-inheritable grant → 125, `sec.*` retention (§3.7.1) → 125;
12. §3.3.3 storage snapshot, birth deltas, and both caps → 131 / 132;
13. aggregate funding, parent side — `econ.transfer` presence when a bond is required (§3.3.3 step 6) → 7; then the combined amount is formed as a **provisional u128 sum** (`endowment as u128 + bond_wei as u128` — cannot overflow), the `max_amount`/`max_per_block` quota comparison runs **over that u128** → 8, then the sum converts to u64 → 133 on overflow, then sufficiency → 107 then 130 (§3.3.3 precedence) — quota state is **not mutated until every check in this step passes** (so an overflow can never surface as a quota code, or vice versa, and a failed fork never consumes transfer quota) — all before any address derivation, preserving §3.3.1's guarantee;
14. copy-cost gas precharge (§3.8) — the order's first metered point;
15. creation-counter checked increments — `fork_count` AND the parent's `children_count` (§3.4's no-saturation rule) → 128 (`ForkCountOverflow` covers both creation counters; the error detail names which);
16. address derivation and collision → 129; then the §3.2 residue census and per-row purge charge — the order's second and last metered point (`OutOfGas` (1) can surface at steps 14 and 16 only); the purge deletes are STAGED into the fork savepoint like every other fork write — census and charge precede staging, the deletes live inside it, so a later fault (callback or enclosing frame) rolls planted rows back into place with everything else, covered by a caught-later-failure test plus the commit-and-reload test;
17. child-side funding, which **cannot precede derivation** (it reads the derived address's pre-existing account): `pre-funded + endowment` checked add → 133.

Staging and the callback follow; callback-time faults are governed by §3.5, not this list. Steps 13 and 17 are deliberately split — parent-side sufficiency is pre-derivation (§3.3.1's promise), child-side overflow is post-derivation (its input does not exist earlier). Overlapping-defect vectors (single calls violating 3+ rules, including depth+mode, malformed-profile+over-quota, insufficient-funds+collision, and quota-exceeded+prefund-overflow) pin the order.

### 3.2 Child address derivation

The child address MUST be deterministic and collision-free across forks by the same parent:

```
child_address = keccak256(b"cip27.fork.addr.v1"
                          ‖ parent_address           # 20 bytes
                          ‖ u64_be(fork_count)       # 8 bytes
                          ‖ code_hash)[12..]         # frame-entry tuple's hash, 32 bytes
```

Fork addresses use a **distinct derivation function**, not `derive_actor_address` with a special salt: `child = keccak256(b"cip27.fork.addr.v1" ‖ parent_address ‖ u64_be(fork_count) ‖ code_hash)[12..]` — keccak256, the exact domain bytes shown, the counter as an 8-byte big-endian unsigned integer, checked arithmetic (overflow rejects the fork), pinned by golden vectors. An inner salt tag would NOT achieve disjointness: `derive_actor_address` hashes any caller-supplied salt into the same `0xff`-prefixed outer preimage, so a `DeployActor` caller could pass the literal fork-salt bytes and occupy the parent's next fork address, permanently wedging the sequence. Only a domain tag in the **outer** preimage (equivalently, a separate derivation) makes the two address spaces structurally disjoint.

`fork_count` is a **new, fork-only counter** on the actor record. It is deliberately not `children_count`: that existing field is already incremented by `DeployActor` when the deployer holds `exec.spawn`, so reusing it would put two consensus producers behind one field with different meanings — deploys would create gaps in the fork address sequence. `fork_count` advances only on successful fork; `children_count` keeps its existing shared "descendants" semantics and continues to advance on both creation paths (it remains the `max_children` quota basis, covering forks and spawns alike).

Successive forks by one parent receive successive deterministic addresses — the on-chain analogue of sequential Unix PIDs. The caller supplies **no salt**; this removes a footgun and makes the Nth fork's address knowable in advance.

Codec: the encoded `Actor` record advances to **version 8** (current is v7; `ACTOR_CODEC_VERSION` in `types/src/execution.rs`). v7's field order ends `… parent, storage_root` — `storage_root` is the final field — so v8 appends `fork_count` **after `storage_root`** (inserting it after `parent` would not be append-compatible), followed by CIP-4's amendment field `delinquent_since_epoch: Option<u64>` — one bump, two amendments, one field order. Its defaults are pinned alongside `fork_count`'s: a v7 blob decodes with `delinquent_since_epoch = None` **unconditionally** (under the reset policy no pre-release delinquency can exist post-re-genesis, so there is no v7-with-debt case to migrate — stated, not assumed); a forked child is born `None` (it owes nothing yet — added to the §3.3 field enumeration); and the v7/v8 golden vectors and the exhaustive-field conformance test cover both appended fields. A v7 blob decodes with `fork_count = 0`: the v8 read path accepts exactly versions 7 and 8 (the current decoder's strict `!= 7` rejection widens to this two-version set), the write path emits v8, and golden byte vectors pin one v7 and one v8 record with decoder tests for both plus rejection of every other version. `fork_count` is covered by every rollback the fork participates in — failed-endowment rejection, callback fault, enclosing-transaction revert — restoring the pre-fork value; a leaked increment would silently shift every future fork address.

The other consensus-visible transitions land at the **same release** (pre-launch, no activation gate per the reset policy): the reserved receipt/mode key names become birth-only and host-unwritable, code validation begins recording the fork-initializer marker (§3.5.2), and **the CIP-29 `subscribe_event` emitter-existence guard ships too** — §3.2's class-B closure depends on it, so a build that carries fork without that guard is invalid exactly as one missing the reserved-key change is. Because networks reset, there is no pre-release actor holding a colliding user-written `__FORK_*` key and no un-marked deployed code to backfill — the release is the genesis of these rules, not a cutover over live state.

If the derived address already exists as an **actor**, `fork()` MUST fail rather than overwrite; an existing plain *account* at the address (pre-funded — see the balance row in §3.3) is not a collision. Beyond the account row, the pre-state a fork address may legitimately carry is exactly: the account's balance and nonce, CIP-20 token holdings, and CIP-18 PaymentGate budgets — keyed value ledgers any address accumulates, all preserved. **Every other actor-scoped row is planted residue** (fork addresses are publicly computable, so rows can be planted at a future child address by whatever mechanism ever writes them): rejecting on residue would hand the planter a permanent counter-wedge on the parent's fork sequence, so the fork MUST instead **deterministically clear** it — and the purge is a bounded, indexed, metered operation, never a database scan. A fork address is publicly computable and several write paths perform no target-existence check, so an attacker can plant state at an unborn child's address — and a child must never inherit attacker-planted inputs. Planted residue splits into **two structurally different classes**, closed two different ways:

**(A) Address-keyed rows the purge can enumerate and clear.** These key on the child's own 20-byte address, so the child's per-address structures enumerate them and a bounded, indexed, metered census clears them at birth (below). Each such class is enumerable from a per-address structure that already exists.

**(B) Hash-of-address-keyed rows the purge structurally CANNOT enumerate — closed by a write-time guard, not a purge.** CIP-29 `EventSub`/`EventSubIndex` (`0x18`/`0x19`) key on `keccak256(sub_id)` and `keccak256(emitter‖topic)` over an unbounded topic space, with no per-emitter reverse index, so no census can discover subscriptions planted against the child as a future emitter. A prefix keyed by a *hash of* an address cannot be enumerated as a per-address prefix. CIP-29 therefore requires a write-time guard: **`subscribe_event` MUST reject a not-yet-live `emitter`** (§CIP-29 Risks), so nothing can be subscribed to the child before it exists; a subscription planted pre-birth is impossible, and post-birth subscriptions are ordinary CIP-29 behavior. This class is therefore never present at birth and needs no purge.

The purge (class A) inventory. Every per-address `StatePrefix` (CIP-4 §4.2) falls into exactly one of **three** buckets, and the classification is total:

- **Cleared** — address-keyed runtime state a newborn must not carry, each enumerable from a genuinely per-address structure: (a) storage-trie rows under the address's own trie root (including hashed-mode keys — the trie enumerates them even where prefix scans cannot); (b) CIP-26 library-pin rows (`0x15`); (c) the kv accounting keys `kv_count`/`kv_bytes` (`0x12`/`0x13`); (d) mailbox rows and their head/tail pointers (`0x04`, `0x10`/`0x11`) plus dead-letter ring and tail (`0x1C`/`0x1D`) — mailbox is the confirmed plantable class (`send_message` does no callee-existence check); (e) actor event-log list (`0x09`); (f) the fairness counter (`0x1B`).
- **Preserved** — keyed value ledgers any address legitimately accumulates: account balance/nonce, CIP-20 token holdings, and **CIP-18 PaymentGate budgets** (anyone can validly prefund a predictable future address's budget with real CBY only that address can withdraw, so deleting it would burn custody; CIP-18 needs no amendment).
- **Not-applicable** — prefixes at which no row can exist for an unborn address, so there is nothing to clear or preserve: **timers** (`0x05` `keccak256(timer_id)`, index `0x06`, plus per-address `0x1A`/`0x0B`) and **deferred-tx** (`0x07` `keccak256(tx_hash)`, indexes `0x08`/`0x0D`, per-address `0x0C`) — both are hash-keyed *and* writer-gated (a timer schedules only for its own `ctx.actor_address`; deferred materialization requires the target actor to already exist), so neither is plantable pre-birth and neither is enumerable from a per-address structure (which is why they are NOT in the cleared bucket — deleting the hash-keyed data rows would strand their index companions); **CIP-14 routes** (FQDN-keyed, no per-address reverse index); the **CIP-26 `Library` catalog** (`0x14`, keyed by the *publisher signer* `tx.from`); and **CIP-29 `EventSub`/`EventSubIndex` (`0x18`/`0x19`)** — the hash-of-address (class B) prefixes, whose closure is **conditional on the same-release CIP-29 `subscribe_event` emitter-existence guard** (a `Requires:` dependency; absent that guard these ARE attacker-placeable and this bucket assignment does not hold). The **writer-gated** members (timers, deferred, routes, `Library`) share a unifying property that does NOT extend to `0x18`/`0x19`: each is written only by an authenticated signer or by the actor's own execution, and a keyless derived fork address has signed and executed nothing pre-birth — so no attacker and no accident can place a row there. `0x18`/`0x19` are the exception — written by an arbitrary third-party subscriber — and they rest solely on the CIP-29 guard, not on writer-gating.
- `0x02 Actor` itself is not in any bucket: an actor record at the derived address is a **collision**, rejected by the §3.2 collision check before the census runs, not purged.

The census **domain is precise**: a prefix is in scope iff it is **keyed by an actor address, directly or through a hash of one** — this is the predicate "per-address" only approximated, and it is exactly the H1 lesson (a `keccak256(emitter‖…)` prefix is address-keyed-through-a-hash and IS in scope, which is why `0x18`/`0x19` are bucketed rather than skipped). Prefixes keyed by something else are out of scope by construction and belong to no bucket: `0x0A SystemState` (global `keccak256(key)`), `0x0E PvmPerInstrGas` / `0x16 TxReturnData` (`tx_hash`), `0x0F Code` / `0x1E CodeMeta` (`code_hash`), `0x17 PublishRootDedup` (`be(height)`). Any prefix added after this amendment that IS address-keyed (directly or via a hash) MUST be placed in one of the three buckets with a stated reason; the conformance test enumerates the live prefix table, computes this domain predicate per prefix, and fails on any in-domain prefix assigned to no bucket — with exactly one pinned exception, asserted as such rather than silently skipped: `0x02 Actor` is in-domain (`address ‖ zeros` is directly address-keyed) but deliberately bucket-less, because a row there is a **collision** rejected by the §3.2 collision check before the census runs; the test carries a dedicated `0x02 → rejected-pre-census` assertion so the exception is itself conformance-checked. With that carve-out this release's table passes and a future unclassified address-keyed prefix cannot slip through.

Placement and metering are pinned in the §3.1 order: the census runs immediately after the collision check passes (it needs the derived address; a colliding fork never pays for a purge), each discovered cleared-bucket row is charged at the `state_delete` schedule to the forker as it is staged for deletion, and this is the order's **second** metered point — `OutOfGas` can surface at the precharge step or here, nowhere else pre-staging. The child's post-fork state is thus a pure function of the parent snapshot plus birth writes, never of pre-state beyond the preserved value ledgers. The clear is covered by the fork savepoint and verified by a **commit-and-reload** test (planted residue in every cleared class — storage, pins, kv, mailbox, events, fairness — absent after block commit, not merely invisible in the overlay), alongside the collision and counter-wedge tests and a subscribe-to-unborn-emitter rejection test (CIP-29 guard).

### 3.3 What is and is not copied — field table

`fork()` is a *copy*, but not every field of the parent's on-chain presence has the same correct copy behavior. The following table is normative; where full and clean mode differ the row says so, and every encoded `Actor` field has a defined per-mode value: `address` (derived, §3.2), `storage_root` (full: the parent's root **plus the deterministic copy-on-write of the birth writes** — receipt, owner, lifecycle — so the final root is a defined function of parent root + those writes, not the parent root verbatim; clean: the root of exactly the seeded slots; both pinned by golden vectors), `manifest` (verbatim, or the fork profile when one is supplied — §3.7.1), transfer/usage counters (fresh, as for a new deploy), rent/quota/bond fields (per §3.3.3 — **not** blanket-fresh: full mode's copied bytes carry a quota liability that must be funded, never zeroed), `dormant_since_epoch`, `pre_evict_storage_hash`, and `delinquent_since_epoch` (`None`, all three, both modes — a newborn is never dormant, evicted, or delinquent), `fork_count`/`children_count` (0). A conformance test asserts no encoded field falls through implicitly.

| Field / category | Behavior | Notes |
|---|---|---|
| **`code_hash`** | **Inherit** (same) | The child runs the parent's code. Content-addressed; no re-store, no re-validate. |
| **Actor storage (base)** | **Full: subtree-share parent root; Clean: empty** | Full mode's *base* root is `parent.storage_root` (CIP-30 O(1) subtree share, copy-on-write on divergence); clean mode's base is empty. This is the root **before** birth writes — the final `storage_root` is base + the deterministic COW of the birth writes below, per §3.3's opening paragraph and golden vectors, never the parent root verbatim. |
| **`manifest`** | **Effective manifest** | Verbatim **only when the verbatim manifest is inheritance-valid for a new identity** (§3.7.1); otherwise the required fork profile is the manifest, validated as a strict inheritable subset. Non-inheritable grants never cross a fork. See §3.4/§3.7.1 for the per-field rules. |
| **CIP-26 library pins** (`ActorLibPin` table) | **Inherit** | The child has the parent's `code_hash` and therefore the same imports; library pins are part of the binding between code and on-chain libraries and MUST clone with it. |
| **`nonce`** | **Reset to 0** | New identity. |
| **`children_count`**, **`fork_count`** | **Reset to 0** | The child has its own descendants and its own fork sequence, not the parent's. |
| **`parent`** | **Set to forker** | See §3.5. |
| **Owner (`__OWNER__` reserved slot)** | **Birth write, conditional** | Written as a birth write **when a value exists to write**: `new_owner` when supplied (both modes), else the forking actor (clean mode). In full mode with `new_owner` omitted, the slot is **not written** — the subtree-shared base already carries the parent's slot verbatim, which is how an explicitly-renounced or never-set parent owner is preserved as an absent child owner (no value can represent absence via a write). Explicit-zero, renounced, and absent therefore stay distinct. The write, when it happens, is applied to the base before any callback. |
| **Native CBY balance** | **Pre-existing balance preserved; `endowment` additive** | Balances live in the chain's balance ledger keyed by address, NOT in actor storage. Fork addresses are publicly computable in advance (§3.2), and `transfer_balance` can fund a not-yet-actor address — so the child's account may already hold CBY at fork time. CREATE2-like semantics apply: the pre-existing account balance is **preserved**, and `endowment` adds to it with **checked arithmetic** (overflow rejects the fork). "Starting balance" is therefore `pre-funded + endowment`, not guaranteed zero. On fork abort/rollback the pre-existing account row — balance and nonce — survives untouched: rollback removes the fork's writes, never the account that predated it. Two funding paths, both same-transaction atomic with the fork: (a) `fork(endowment=N)` — debits N from parent, credits N to child as part of the fork itself (§3.3.1); (b) post-fork `runtime.transfer_balance(child, N)` (CIP-6 §12.12) — separate syscall after `fork()` returns. **Cloning** balance would duplicate supply and violate the CBY conservation invariant; both funding paths move CBY between existing balances rather than cloning it. |
| **CIP-20 token holdings** | **Not copied from parent; pre-funded holdings preserved** | Token ledgers are keyed by address, not actor storage: nothing transfers from the parent, but tokens already sent to the (predictable) child address persist, as with CBY above. Supply conservation holds across fork for CBY and every CIP-20 token — fork never mints. |
| **CIP-18 PaymentGate budgets** | **Preserve** | Budgets are prefundable real-CBY custody keyed by address (anyone may deposit to a future address; only that address withdraws) — the third preserved ledger, per §3.2. Payment *authority* (policies the parent configured as a spender elsewhere) is not address-keyed state and does not copy. |
| **CIP-5 pending timers** | **Not copied** | Each scheduled timer's `actor_address` field names the parent; copying the storage view of "I scheduled a timer at H" without copying the scheduler entry would silently desynchronize. Timers stay with the parent; the child schedules its own in `on_fork` if needed. |
| **CIP-14 DNS route registrations** | **Not copied** | Route names are globally unique. The child cannot inherit a name still held by the parent; it MUST register a fresh name in `on_fork` if it wants ingress. |
| **CIP-24 secrets** | **Nothing to copy — not actor storage** | Full fork does not inspect actor storage for secret values: CIP-24 secrets live in **CBSS's own state** (prefix `0x04`, keyed by `SecretId{account, key_hash}`) — never in actor storage — so a fork copies no secret-bearing bytes, wrapped or otherwise. What governs a child's access is CIP-24's own authorization: releases are gated by the manifest's `secrets.*` entitlements (ordinary grants under §3.7.1's inheritance/attenuation rules) and bare-name refs resolve to the **actor's owning account** — a cross-owner child resolves to *its* owner's account, not the parent's. No fork-side storage predicate exists or is needed. |

> **A note on CIP-23 TEE bindings.** `sec.tee_required` is a coarse capability declaration ("this actor's handlers must run on a TEE'd runner of class X") — not an actor-identity-bound credential. The platform verifies a runner's TEE attestation at runner registration time; the actor manifest just expresses the requirement. The field therefore clones verbatim with the rest of the manifest: any TEE'd runner offering the required capability serves the child the same way it served the parent.

`fork()` MUST NOT run the child's `init`. The child begins life already-initialized — that is the definition of a fork. Identity-fixup is the job of the `on_fork` callback (§3.6).

#### 3.3.1 Atomic endowment

When `fork()` is invoked with `endowment > 0`, the runtime MUST:

1. Verify the parent has at least `endowment` wei available. If not, `fork()` MUST fail with `HostError::InsufficientCbyBalance` *before* deriving the child address, staging the child record, or invoking `on_fork`. A failed-endowment fork creates no child and leaves the parent's `children_count` unchanged.
2. Debit `endowment` from the parent's native CBY balance and credit `endowment` to the child's balance.
3. Stage both balance writes through the **same cross-call write-set machinery** used for the child's actor record and inherited `storage_root` (§3.9). On enclosing-transaction rollback, all four writes (parent debit, child credit, child record, `children_count` bump) MUST reverse together.

The endowment write-set staging requirement is normative because a direct ledger mutation bypassing the write-set leaks balance on revert: a downstream rollback inside the same transaction (`on_fork` faults, parent's handler raises later, gas exhaustion) leaves the parent debited and the child either credited-but-undone or credited-on-an-uncommitted-actor. The reference implementation explicitly verifies that an `on_fork` failure restores the parent's pre-fork balance.

`endowment == 0` (the default) is the no-balance-motion path: no debit, no credit, and **no endowment surcharge** — the base fork charges (including the mandatory lifecycle/receipt writes below) still apply; "identical to v1" is no longer true and not claimed.

Rationale for making endowment a parameter on `fork()` rather than requiring a separate `transfer_balance` call after fork:

- **Atomicity guarantee.** Some `on_fork` paths need to spend immediately (e.g. `schedule_timer_ex` with `fee_payer = self`). Without atomic endowment, the parent must `fork() → transfer_balance(child, N)` in two syscalls; if `on_fork` runs in between (which it does — see §3.6) and `on_fork` itself tries to spend, the spend fails because the credit hasn't happened yet. Folding the credit into the fork itself fixes the ordering.
- **Single failure mode.** "Insufficient parent balance" surfaces as a fork failure, not as a successful fork followed by a half-funded actor with a separate failure mode for the transfer.
- **EVM precedent.** `CREATE`/`CREATE2`'s `value` field is the same primitive — atomic seed-with-balance. `fork(endowment=N)` is the Cowboy analog.

The post-fork `transfer_balance` path (CIP-6 §12.12) remains valid for cases where the endowment amount depends on state the parent doesn't know pre-fork (e.g. computed from the child's address).

#### 3.3.2 Clean mode

`fork(mode="clean")` copies the **code binding and nothing else**: `code_hash`, `manifest` (per the §3.3/§3.7 manifest rules), and the CIP-26 library pins. The child's storage begins **empty** — no storage-root share, no owner slot inherited, no application state of any kind.

Clean mode exists because full-copy exclusion lists fail open. A parent that holds hire ledgers, volume pointers, credential references, or accumulated user data cannot make a hirer-safe instance by scrubbing in `on_fork` — a deny-list with one forgotten key is a leak, and the bytes were copied (and charged) before the delete. Clean mode is the allow-list of nothing: no state crosses, so there is nothing to scrub. The §3.3 exclusions (timers, routes) remain necessary for full mode; clean mode subsumes them trivially.

Clean mode is the **deploy analogue restricted to self-code**: it gives an actor the ability to instantiate a fresh peer of *exactly the code it is already running* — and nothing else. The security property that keeps arbitrary in-actor deployment out of the protocol (an actor must not instantiate code it does not itself run) is preserved byte-for-byte: the child's `code_hash` is the parent's, always.

"No storage" means **no application storage**. The runtime atomically seeds the SDK's reserved lifecycle slots as part of fork staging, rolled back with the rest on abort: `__INITIALIZED__` = `0x01` on every child, both modes; `__OWNER__` **only when §3.1 has a value to write** (an omitted-owner full fork writes no owner slot — see the Owner row in §3.3 — so an absent parent owner stays absent). It additionally stamps the **fork receipt**, one rule for both modes: `__FORK_MODE__` = `0x01` (full) or `0x02` (clean), and `__FORK_PAYLOAD_HASH__` = keccak256 of the bytes actually delivered to the initializer callback — in full mode that payload is the 20-byte parent address, so the slot holds `keccak256(parent_address)`; there is no sentinel value. `__INITIALIZED__` is the single byte `0x01`. In full mode the storage copy may carry the *parent's* receipt slots; staging **overwrites** them with the child's own values — a child's receipt always describes its own birth. These key names were previously unreserved: from this amendment's release the host rejects application writes to them, and a full-copied parent key with a colliding name is overwritten by the birth write (the child-local value wins).

The receipt slots are a **distinct, birth-only slot class**, not entries in the existing `RESERVED_STATE_KEYS` list. The distinction is load-bearing: the raw host accepts `set_reserved_state` / `delete_reserved_state` for every key on that list, so merely adding receipt keys there would make them *actor-writable* — and the receipt's whole value is that actor code cannot forge it. Birth-only slots are written exactly once, by the fork syscall, and the host rejects any later write or delete from any context; direct raw-host overwrite and deletion attempts carry dedicated tests. Downstream consumers (e.g. CIP-33 spawn verification) treat the payload hash as binding a domain-separated envelope, so one slot suffices for arbitrarily structured commitments. This is load-bearing, not bookkeeping: the permission layer admits `init` from **any caller** while `__INITIALIZED__` is absent (the atomic-deploy bootstrap rule), so a child without the seeded flag exposes a public re-initialization route from its first block. The same seeding requirement applies to a full-mode fork taken while the parent's own `init` has not completed.

Requirements:

- The child's code MUST define an `on_fork_clean` callback (§3.5.1); `fork(mode="clean")` against code without one MUST be rejected before any child is staged. A clean child has no application state and `init` never runs on forks, so `on_fork_clean` is the only initializer — an actor without one would produce a permanently blank instance, and fail-closed beats blank.
- The owner slot is written per §3.1 (explicit `new_owner`, else the forking actor) before `on_fork_clean` runs, so owner-gated handlers behave correctly from the first post-fork call.
- **The fork source is the frame-entry code binding**, both modes, as the single deterministic rule — implementations MUST clone from the frame-entry tuple; dirty-binding rejection is NOT an allowed alternative (two conforming validators choosing differently would fork the chain, not the actor). The captured tuple is `(code_hash, code-bytes reference, manifest, canonical library pins)` frozen at handler entry, and it drives everything derived: the child address (§3.2 uses the tuple's `code_hash`), the child record, the callback code load, and the §3.7.1 inheritance validation. `upgrade_self` mutates all of code, hash, and manifest live, mid-handler, while old code continues executing and the new blob persists only at outer commit; a fork sourced from live fields would select code not currently executing and possibly not yet loadable. `upgrade_self` is additionally forbidden inside `on_fork`/`on_fork_clean` (`ForkInit` context, §3.5.2): a callback upgrade would stage a child pointing at an uncommitted blob and break any downstream code commitment.
- Address derivation (§3.2), `parent` (§3.4), `children_count`, endowment (§3.3.1), entitlement and quota (§3.6), and rollback (§3.9) are identical to full mode. Gas: clean mode charges the fork base costs but no storage-share step.

#### 3.3.3 Storage quota, rent, and bond at fork

A fresh deploy starts with `quota_bytes = RENT_QUOTA_BASE` (1 MiB), zero locked bond, and zero bytes. A full fork does **not** start with zero bytes — it starts holding the parent's entire copied set — so "reset the rent bookkeeping as for a new deploy" would mint unfunded storage: the child would sit at potentially many MiB of live state against a base quota and no bond, a state the ordinary write path (`kv_bytes ≤ quota` enforcement) can never produce, and recursive forks would amplify bonded storage without anyone paying for it. The share being O(1) under CIP-30 changes the *copy cost*, not the *liability*.

The owning specification for actor storage quota, bond, and rent is **CIP-4 §12.4–12.5** (CIP-31 is the framework CIP; CIP-4 carries the normative schedule — CIP-4 joins this CIP's Requires). This section uses CIP-4's exact formulas, units, and rate snapshot; it defines no second schedule. Rent accrues on the **allocated quota** under CIP-4.

Normative rule, full mode — computed in this exact order:

1. **Snapshot.** At syscall entry, one atomic snapshot of the parent's storage accounting: `(storage_root, kv_count, kv_bytes)` — the syscall-time read-your-writes view (the same view the copy takes; this is deliberately the *storage* snapshot, distinct from the §3.3.2 frame-entry *code* tuple, which is captured at handler entry).
2. **Birth deltas.** Apply the deterministic birth-write deltas to the snapshot: `__INITIALIZED__`, `__FORK_MODE__`, `__FORK_PAYLOAD_HASH__` always; `__OWNER__` when §3.1 has a value to write (its key/value sizes are fixed and pinned). The deltas are **overwrite-aware**: a birth write whose key exists in the copied set (a full-copied parent's colliding receipt key, §3.3.2) replaces bytes without adding a key. The result is the child's **final** `kv_count` / `kv_bytes` at birth.
3. **Caps — both of them.** Final `kv_bytes > RENT_QUOTA_MAX` (8 MiB, CIP-4) rejects with `QuotaExceedsMax` (131); final `kv_count` above the per-actor key cap (10,000, the same constant the ordinary write path enforces) rejects with `KvCountExceedsMax` (132). Both are checked **before staging**, at the §3.1 order's cap step — never discovered mid-copy as a late `StorageFull`, which a host path bypassing the ordinary write checks would not even hit; an actor too large to deploy under the caps is too large to fork. Cap and cap+1 vectors for both dimensions, including a parent at exactly the key cap whose birth writes are pure overwrites (succeeds) versus one new key (rejects).
4. **Quota and bond.** `quota_bytes` := the minimal CIP-4 quota admitting the final `kv_bytes` (never below `RENT_QUOTA_BASE`). CIP-4 today supplies only the bounds and `bond_rate_atto` — no exact bond function — and the live `SetActorQuota` path has a unit defect (it subtracts a raw **atto** delta from the u64 **wei** balance and refunds the raw delta). This amendment therefore **amends CIP-4 §12.5 and `SetActorQuota` in the same release** with the one checked routine both paths share: `bond_atto = bond_rate_atto × (quota_bytes − RENT_QUOTA_BASE)` (checked u128 multiply; zero at or below base).
5. **Funding.** The bond debits the **parent's** native balance through the shared routine. Units are pinned: the ledger is u64 wei, the bond u128 atto-CBY, `1 wei = 10^9 atto`. `bond_wei = ceil_div(bond_atto, 10^9)`, checked conversion (a `bond_wei` that overflows u64 rejects as `QuotaExceedsMax` — unreachable under the 8 MiB cap, but pinned); the locked value is recorded as `bond_locked_atto = bond_wei × 10^9` — the **wei-quantized** amount actually debited, not the raw `bond_atto`, so release refunds exactly `bond_locked_atto / 10^9` wei and no rounding dust is ever created or stranded, in either direction, on either path. **One aggregate sufficiency check, one precedence:** `total = endowment + bond_wei` (checked add); if `parent.balance < endowment` → `InsufficientCbyBalance` (107); else if `parent.balance < total` → `InsufficientQuotaBond` (130). Both are evaluated at the §3.1 order's funding step — before address derivation or any staging, preserving §3.3.1's guarantee. The debit and lock are staged through the same write-set as every other fork write and roll back with them. Parity vectors pin that fork and an explicit `SetActorQuota` to the same quota debit identical wei and record identical `bond_locked_atto`.
6. **Authorization.** Bond funding is **value transfer to the child and is gated exactly like the endowment**: because the bond becomes the child's asset (step 7) and the child can realize it as liquid balance by reducing quota, an "`exec.fork`-only" bond would be a spend channel bypassing `econ.transfer` — fork to another owner, fund the bond, child reduces quota, counterparty holds spendable CBY the parent's manifest never authorized. Therefore a fork whose §3.3.3 computation requires a bond MUST hold `econ.transfer` in the frame-entry manifest, and the combined outflow `endowment + bond_wei` consumes `econ.transfer`'s `max_amount`/`max_per_block` quotas as one checked amount. A bondless fork (final bytes within `RENT_QUOTA_BASE`) with `endowment = 0` still requires no `econ.transfer` — §3.6's "a code-cloning grant need not imply a balance-spending grant" holds exactly when the fork moves no value.
7. **Disposition.** Once locked, the bond is the **child's** asset, exactly as if the child had posted it (the parent funded it as it funds an endowment — a gift at birth, authorized as step 6's transfer). CIP-4 §12.4's release rules apply **unchanged and asymmetric, exactly as current rent code implements them**: voluntary quota *reduction* returns the released bond to the child's balance; *eviction* **forfeits** it. This CIP does not soften the forfeiture for parent-funded bonds — the child owning the bond means owning its downside too; the parent holds no residual claim either way, and a differently-owned child changes nothing.

The parent's own bond is **never copied or split** — it stays locking the parent's quota; copying it would duplicate escrow against one payment. Rent state is fresh-but-consistent: `rent_debt = 0`, `last_settled_epoch` = the current epoch, `rate_stamped_atto` = the current *rent* rate; rent accrues from birth on the allocated quota per CIP-4.

Clean mode takes the fresh-deploy defaults verbatim (`RENT_QUOTA_BASE`, zero bond; its final bytes are just the birth writes) — nothing is copied, so there is nothing to fund.

Recursive forks compose safely under this rule: every fork in a chain debits its own child's bond from its own balance, so a fork-of-a-fork of a large actor costs the full bond each generation — storage amplification is always paid for at the CIP-4 price, and total locked bond equals total quota granted. Conformance tests: fork whose **post-birth-write** total lands exactly at `RENT_QUOTA_BASE` (no bond, succeeds), one byte over (bond required and locked), over-cap at `RENT_QUOTA_MAX` (+1 rejects with 131), insufficient parent balance for endowment alone vs endowment+bond (107 vs 130, precedence pinned), atto↔wei rounding vectors at the `ceil_div` boundary, two-generation recursive fork asserting bond/quota conservation at each step, voluntary quota reduction of a parent-funded child crediting the child, and eviction of one **forfeiting** the bond (CIP-4 §12.4's asymmetry, step 7).

### 3.4 The `parent` field

The `Actor` record MUST carry a `parent: Address` field. For an actor created by `fork()`, `parent` is the forking actor. For an actor created by an **actor-origin `DeployActor`** (an `exec.spawn`-entitled actor deploying fresh code), `parent` is likewise the deploying actor — the current implementation writes `Address::ZERO` there, which this amendment corrects: `children_count` already counts such deploys as descendants, and a lineage field that skips one creation path is not a lineage field. For genesis actors and actors deployed by an externally-owned account, `parent` is the zero address (`Address::ZERO`), meaning "no parent." `children_count` advances by **checked** increment on every creation path — at `u64::MAX` the creation rejects (no saturation; a saturated counter silently aliases descendants).

This follows Unix semantics precisely: **the child's parent is the process that called `fork()`** — not whatever created the forker. A `Piece` forked by a `Piece` has that `Piece` as its parent even if a `City` originally deployed the lineage.

Cowboy has no in-protocol actor delete, so the `parent` pointer is always resolvable. CIP-31 storage rent MAY drive an actor into a *dormant* / evicted state; an evicted parent's address remains valid and continues to be the child's `parent`. Implementations MUST NOT re-parent a child whose parent is evicted (no Unix-style "re-parent to init"); the lineage record is permanent.

Adding `parent` is a state-format change; implementations MUST bump the actor codec version and treat the field as present-and-zero for all pre-existing actors.

### 3.5 The `on_fork` callback

A byte-exact copy is also *wrong* in every self-referential way: the child's storage still names the parent's address, the parent's peers, the parent's identity. To let the child repair itself, the runtime invokes an **`on_fork` callback** on the child:

```python
@fork_initializer
def on_fork(self, payload):
    parent_addr = payload          # the forking actor's address
    # fix up self-referential state: identity, role, peer links,
    # re-register routes (CIP-14), re-schedule timers (CIP-5), etc.
```

- `on_fork` runs as a nested `call()` from the `fork()` syscall — it counts against the call-depth limit (§3.1) and shares the fork's gas budget. It is **not** a CIP-29 hook (no isolated billing, no `MAX_EVENT_DEPTH`); the word "callback" is preferred over "hook" to avoid that confusion.
- `on_fork` MUST be obeyed strictly under CIP-6's determinism rules (§3.9). A non-deterministic `on_fork` would fork the chain, not the actor.
- A fault in a **present** `on_fork` MUST abort the fork entirely: the implementation rolls back the whole staging — child record, storage copy, lib pins, endowment, `children_count` — **and every callback effect**. The state overlay's savepoints deliberately exclude the append-only effect logs — all **six** classes: events, outgoing messages, timer schedules, timer cancellations, timer extensions, and deferred-tx requests — so a bare state rollback leaves effects from a child that never existed; and because the failed fork does not advance `fork_count`, a later successful fork reuses the same deterministic address and inherits the leaked timer or message. The fork syscall therefore marks all six effect logs at entry and truncates them to the marks on callback fault. Fork-local marks are **not sufficient on their own**: a callback that *succeeds* inside a nested frame that later rolls back (A calls B; B forks C, whose initializer emits effects; B then faults; A catches) leaves the same leak with no fork-local trigger. The general rule is therefore normative: **every nested-frame savepoint covers all six effect logs** — each cross-call frame marks the logs at entry and truncates to its marks on that frame's rollback, making effects transactional with the state they were emitted alongside at every level; the fork-entry marks become one instance of the general rule. This is **new behavior this amendment requires** — no current test implements it — and lands with one test per effect class for the callback-fault case and one per class for the successful-callback-then-enclosing-fault case. (An **absent** `on_fork` skips the callback and the fork continues.) The abort-on-fault rule itself matches the current reference implementation and its property tests — only the effect truncation is new; the earlier fall-back-to-copied-state rule is superseded — silent fallback returns a bare success and can hand a new owner an un-fixed snapshot after the author explicitly supplied a fixup callback.

**`on_fork` is opt-in.** An actor that does not define a `@fork_initializer`-marked `on_fork` handler is signaling: *"my state is safe to bit-copy."* This is a deliberate assertion, not a default. Concretely, fork is permitted on such an actor — but the explicit decorator forces actor authors to face the question, especially given the secrets concerns in §7.

#### 3.5.1 The `on_fork_clean` callback

Clean-mode children are initialized by a distinct callback:

```python
@fork_initializer
def on_fork_clean(self, payload):
    # payload = the bytes the forker passed to fork(payload=...);
    # the forking parent is runtime.get_sender() — the callback runs as a
    # nested call from the parent, so no separate parent argument is needed.
    ...
```

`on_fork_clean` receives the forker-supplied `payload` (§3.1), not the parent address — the parent is already the callback's sender. It is REQUIRED for clean-forkable code (§3.3.2) and runs under the same nested-call, determinism, gas, and fault-aborts-the-fork rules as `on_fork` (§3.5). The fault rule is uniform across modes: a present initializer that faults aborts the fork; the modes differ only in whether the callback may be absent (full: yes, the state was copied; clean: no, §3.3.2).

#### 3.5.2 Fork callbacks are not public entry points

`on_fork` and `on_fork_clean` MUST be invocable **only through the `fork()` syscall**. Without this rule, any caller could re-run a child's identity fixup after the fork — re-registering routes, re-scheduling timers, or (clean mode) re-initializing a live instance over its state.

The enforcement mechanism is an **invocation-kind check at the centralized handler dispatcher**, not a name filter inside `call_actor`: the fork syscall itself delivers the callback through the ordinary cross-call path, so a name-based rejection there would break the one legitimate caller, while top-level transactions, mailbox deliveries, and event routes do not all pass through `call_actor` and would escape a filter placed there. The fork syscall marks its callback invocation with `ForkInit` — a **one-shot capability bound to the exact `{parent, child, mode, method}` tuple**, consumed on delivery: nested calls made *from inside* the callback reset to `Normal` kind and can neither re-enter the reserved entrypoints nor evade the `ForkInit` `upgrade_self` ban by indirection. The **parent** remains non-reentrant for the callback's duration under the existing suspended-actor rule (it is mid-call, like any cross-call sender); the **child's** protection is the initializing-actor guard below — the suspended-actor set alone does not cover it. The dispatcher rejects the reserved names arriving under any other kind.

Callback **existence** is decided from deploy-time metadata, not by attempting execution: code validation records which reserved callbacks the code defines. The marker is pinned concretely, **including its consensus carrier** (code state today is exactly `StatePrefix::Code → StateValue::Code(Vec<u8>)`, which offers no side-channel): a new state prefix `StatePrefix::CodeMeta` = `0x1E` (the next free prefix; CIP-4's state-prefix table, which today ends at `0x1D`, gains the row) with `StateValue::CodeMeta(u8)` at wire tag **0x12** (the tag table currently ends at `MAILBOX_MESSAGE_V2 = 0x11`; declaration order and wire tags differ, so the tag is pinned, not inferred), key = the 32-byte `code_hash` verbatim, value = the one-byte bitset (`bit 0 = on_fork`, `bit 1 = on_fork_clean`; remaining bits MUST be zero and a set reserved bit rejects at decode — strict, so future bits are a deliberate change, not drift). It is written atomically with its code record wherever one is created (`DeployActor`, `UpgradeActor`, `upgrade_self`), exists exactly as long as the code does, and the fork syscall reads it via the **frame-entry** `code_hash` (§3.3.2's tuple), never a live field. State-sync and light-client paths carry the new prefix like any other (it is ordinary keyed state; the blast radius is the state-codec enum itself, re-pinned with the release). Creation is **idempotent per code_hash** and covers **every Code producer** — `DeployActor`, `UpgradeActor`, `upgrade_self`, and `PublishLibrary` (which creates shared Code rows too) — with **one computation, from the bytes, everywhere**: the bitset is the same pure function of the code bytes on every producer, explicitly including `PublishLibrary` (library code passes ordinary actor validation and MAY define the callbacks; a producer-dependent marker would make the value depend on whether publish or deploy touched the hash first — order dependence in consensus state). Publish-first/deploy-first vectors pin the equality. Dedup of Code blobs, so writing an identical marker over an existing one is a no-op, and a *conflicting* marker for an existing hash is unreachable (the marker is a pure function of the code bytes). Golden vectors: marker bytes for code with neither/either/both callbacks, reserved-bit rejection, idempotent re-create, and recomputation on each creation path. Thus `fork(mode="clean")` checks clean-forkability without staging anything — and, critically, a *present* callback whose body happens to raise a not-found-shaped error is never misclassified as an absent handler. (The current reference implementation uses exactly that unsafe heuristic — matching on the fault message — and must migrate to the marker.)

The SDK marks these handlers with a dedicated **fork-initializer marker** rather than `@public` — the callbacks are not public entry points and must not be declared as such. Permission decorators do not apply to them (the dispatcher's kind check replaces that layer), and the marker **implies DEFERRED handler mode**: the SDK's default is PURE, which would forbid exactly the message/timer/deferred effects §3.5 names as the callback's legitimate work; no separate `@deferred` stacking is needed or permitted.

While the callback runs, the child is an **initializing actor**. The existing suspended-actor set does NOT cover this — a self-call bypasses it — so an explicit initializing-actor guard is authoritative and rejects, until the callback returns, every path into the child: direct self-call, self-event delivery, a callback-initiated chain re-entering the child through a third actor, mailbox delivery, replay of the reserved entrypoints, nested `upgrade_self` (§3.3.2's ban), and owner-gated dispatch. The `ForkInit` capability is consumed on delivery and does not propagate to nested frames, which reset to `Normal`. Note for implementers: the current `fork.py` docstring example decorates `on_fork` with `@callable_by(SELF)`, which **denies the real callback** — the callback's sender is the parent, not the child, so `SELF` never matches; the shipped example only works for actors with no permission gating at all. The marker fixes this class of confusion.

### 3.6 Entitlement and quota

`fork()` is an actor-creating effect and MUST be gated by a dedicated entitlement:

- **`exec.fork`** — a **new entitlement this amendment registers** (the registry at `docs/architecture/entitlements/entitlements-specification.mdx` lists `exec.spawn` with `max_children` and states "any entitlement not listed here is invalid"; that registry gains `exec.fork` — carrying `max_children` — in the same release, or `fork()` is unauthorizable). Read from the **frame-entry manifest** (the §3.3.2 fork-source tuple), not the live `ctx` manifest, so a same-handler `upgrade_self` cannot change whether the in-progress fork is authorized. Declared in the forking actor's manifest, covering both modes: full copy and clean (§3.3.2) are the same trust boundary — self-code instantiation — differing only in what state crosses, so one grant governs both. Distinct from `exec.spawn` (which gates fresh-code `DeployActor`). The two have different cost profiles (`fork()` does no code validation; spawn does no storage copy) and different operational properties (fork is self-only; spawn can be against arbitrary code), so operators commonly want to grant cloning without granting arbitrary deployment.
- **`max_children`** — quota carried on the `exec.fork` grant. `fork()` MUST be rejected once the parent's `children_count` reaches it. A successful `fork()` MUST increment the parent's `children_count`.
- **`econ.transfer`** — required additionally whenever the fork moves value: `endowment > 0`, or a §3.3.3 quota bond is required (the bond is value transfer to the child — §3.3.3 step 6). The combined `endowment + bond_wei` outflow consumes the grant's `max_amount`/`max_per_block` quotas as one checked amount. A value-free fork (`endowment = 0`, final bytes within base quota) does NOT require it — a code-cloning grant need not imply a balance-spending grant.

Legacy actors with no manifest are unaffected, consistent with `DeployActor`.

### 3.7 Manifest cloning details

The manifest clones verbatim (§3.3), but a handful of manifest-bound capabilities have specific clone behaviors documented above:

- **`sec.tee_required`** (CIP-23): inherit. Coarse capability requirement; no actor-identity binding.
- **CIP-14 ingress routes** declared on the manifest: clone the *entitlement* (the right to register a route), not any registered name. The child has the right but starts with no registered routes.

The general rule is: clone declarations of *capability* (what the child is allowed to do); reset registrations of *runtime state* (specific names, sealed material, attestation tokens, etc.).

#### 3.7.1 The fork inheritance boundary

A fork creates a **new actor identity**, and capability rules bind to identity boundaries — never to ownership, because ownership is mutable. An owner-equality exemption would be a laundering primitive: fork with `new_owner` equal to the current owner, clone `sys.upgrade`/`econ.transfer` under the exemption, then `transfer_ownership` the child to anyone — no inheritance revalidation runs on transfer, and the transferee holds capabilities an ordinary child deploy forbids. (Clean mode's forker-defaulted owner is a second edge the same rule erases.) `validate_inheritance` deliberately takes no owner parameter for exactly this reason.

Normative rule, unconditional: **every** fork — both modes, any owner — validates the child's effective manifest under the same inheritance rules as a child deploy. Non-inheritable grants never cross a fork. When the parent's verbatim manifest carries any non-inheritable grant, the fork MUST supply a **fork profile** — a `child_manifest` argument validated as a strict subset of the parent's manifest restricted to inheritable grants, rejected (not stripped) on any violation; a fork without a profile is valid only when the verbatim manifest already passes. The child's effective manifest — profile applied — is what any downstream executable commitment hashes.

**Manifest entries have two polarities, and "strict subset" applies only to one of them.** *Authority grants* (`exec.*`, `econ.*`, quota-bearing entitlements — things the actor is *allowed to do*) may attenuate: the profile may drop or narrow them, never widen. *Security constraints* — entitlement ids under the exact dotted prefix **`sec.`** (the delimiter is normative and includes the dot: `starts_with("sec.")`, so `sec.tee_required` and `sec.data_residency` match but an unrelated id merely beginning with the letters "sec" does not, and CIP-24's `secrets.read` / `secrets.verify` are **grants, not constraints** — they attenuate like any authority grant) — point the other way: they are **non-droppable and non-weakenable across a fork, both modes**. In v1 the comparator is **byte-exact equality**: the child's effective manifest MUST carry each parent `sec.*` entry with identical key and value bytes — no "stricter" form is accepted, because no partial order over the constraint parameters is defined (TEE class strings and residency codes are opaque to the validator; a per-constraint ordering registry is future work, and until it exists "stricter" is unverifiable). A profile that omits or alters any `sec.*` entry is rejected. A subset check that only iterates the child's entries (as the current `validate_inheritance` does) misses exactly this: a profile silently omitting `sec.tee_required` would full-fork sensitive parent state into byte-identical code whose manifest no longer requires a TEE — a confidentiality downgrade through the profile mechanism itself. Validation therefore iterates **both** manifests: child entries for grant-attenuation, parent `sec.*` entries for constraint-retention. Clean mode gets no relaxation carve-out — the code is identical and the operator claim ("this code runs under these constraints") is a property of the code's trust story, not of which bytes were copied; if a genuine clean-mode relaxation need emerges it requires an explicit per-constraint safety matrix in a future amendment, not a default. Conformance tests: a profile omitting `sec.tee_required`, one altering `sec.data_residency`'s value bytes (rejected — v1 equality admits no "tightening" either), and one carrying every `sec.*` entry byte-identical (accepted) — in both modes.

Two consequences, stated rather than discovered:

- A spawning template necessarily carries `exec.fork`; whether children may recursively fork is the template's explicit choice via the profile (hired instances normally drop it — recursive untracked instantiation is the default-off case).
- Same-owner swarms whose parent holds non-inheritable grants cannot hand them to workers. A worker that "needs" `econ.transfer` is redesigned: the parent funds children by endowment and transfers, holding the spend capability at the root — which is the safer swarm topology regardless. If a genuine need for capability-carrying fork children emerges, the escape hatch is a new explicit `fork_inheritable` registry flag plus ownership-transfer revalidation — future work, not an owner comparison.

### 3.8 Gas

`fork()` MUST be metered in Cycles and Cells per CIP-3 §2.2:

- A base cost equal to `actor_deploy_base_cycles` / `actor_deploy_base_cells` for the new actor-record write.
- **No** per-code-byte cost — no code is supplied or stored.
- Once the CIP-30 subtree share is implemented, the storage copy is O(1) and costs only the constant-time root-pointer write. The current implementations (origin and CIP-30's own present state) still **physically enumerate**, and the interim rule is normative, not MAY: the enumeration path MUST **precharge one checked aggregate** from the §3.3.3 step-1 **syscall-time read-your-writes snapshot** — the exact image the copy, cap, and birth accounting consume, so same-handler writes made before the fork are priced with everything else; charging from the *committed* counters would let the copy scan more than it priced and put unmetered work ahead of an OOG. The aggregate covers BOTH cost dimensions of the copy — `copied_bytes` Cells at the `state_set` schedule (CIP-3 §2.2.2) AND `kv_count × STORAGE_WRITE_CYCLES` per-row Cycles (the current implementation already prices per-entry cycles; a bytes-only precharge underprices many tiny rows) — plus every deterministic non-residue charge of this section (actor-record base, birth slots, payload, profile, pin rows, endowment and bond legs), one checked sum, charged at the order's first metered point.
- The `on_fork` callback is metered as an ordinary nested call within the `fork()` budget.
- When `endowment > 0`, an additional `2 × state_set` Cycle/Cell cost MUST be charged (two ledger writes: parent debit, child credit). `endowment == 0` MUST NOT incur this endowment surcharge.
- When §3.3.3 requires a quota bond (full mode, **final post-birth-write** `kv_bytes` above `RENT_QUOTA_BASE` — the step-2 total, so birth writes can never trigger uncharged bond work), the bond debit/lock legs are charged like the endowment legs (`2 × state_set`), and the child's storage-accounting initialization (`kv_count`, `kv_bytes`, quota/rent fields) is part of the base actor-record write, not separately metered.
- The birth writes are charged at the ordinary reserved-slot `state_set` schedule, both modes: `__INITIALIZED__`, `__FORK_MODE__`, `__FORK_PAYLOAD_HASH__` always; `__OWNER__` **only when it is written** (§3.3's conditional rule — an omitted-owner full fork writes no owner slot and pays no owner-slot charge).
- `payload` (clean mode) is charged `payload.len()` Cells at the inline-blob rate; a supplied `child_manifest` is charged `child_manifest.len()` Cells at the same inline-blob rate (there is no separate deploy manifest rate to reference; the profile is capped at `MAX_FORK_PROFILE_BYTES`, §3.1). The child's inherited CIP-26 library-pin rows are charged `pin_count × (LIB_PIN_OVERHEAD_CYCLES = 300 + LIB_PIN_OVERHEAD_CELLS = 64)` — both dimensions of CIP-26's canonical pin constants, not generic `state_set`; the pin table is per-actor state and its copy is real writes, not covered by the CIP-30 root share.
- The §3.2 residue purge is charged per discovered row at the `state_delete` schedule (order step 16); a residue-free fork pays zero here.
- **CIP-3 owns the fee schedule and is amended in this release** (§2.2's fork row currently prices only the actor-record base plus root-pointer copy): the amended schedule carries every conditional line above — birth slots, payload, profile bytes, pin rows, endowment legs, bond legs, residue purge — with total-cost and OOG vectors. CIP-3 joins this CIP's coordinated release set.
- **Rollback authority.** One fork savepoint covers the full staging, and every abort path restores all of it: the child `Actor` record, effective manifest, storage root (base + birth writes), lifecycle and receipt slots, library pins, both counters (`fork_count`, `children_count`), both balance legs of any endowment, the §3.3.3 quota-bond debit/lock legs and storage-accounting writes, the staged residue-purge deletes, the parent's `transfer_block`/`transfer_block_amount` consumption whenever the fork consumed `econ.transfer` quota — endowment, bond, or both, including `endowment = 0` with a bond (§3.3.3 step 6) — and the six effect-log marks of §3.5. No field of §3.3's table is exempt.

### 3.9 Determinism and rollback

Every validator replaying the transaction MUST produce the identical child: identical address, identical inherited `storage_root`, identical `on_fork` outcome. Determinism holds because the address derivation, the state-root share, and `on_fork` are all deterministic functions of committed state and the PVM's deterministic execution mode (CIP-6 §17 — no wall-clock, no unseeded randomness, no FPU). `on_fork`, being arbitrary actor code, is bound by these same rules.

`fork()` MUST be transactional. If the enclosing transaction reverts after a `fork()`, the child MUST cease to exist, the parent's `children_count` increment MUST be undone, and (when `endowment > 0`) both the parent's debit and the child's credit MUST be undone — leaving the parent's pre-fork balance intact. A conformant implementation stages the full §3.8 rollback set — child record, effective manifest, storage root (base + birth writes), lifecycle/receipt slots, library pins, both counters, both endowment balance legs, the quota-bond legs and storage-accounting writes, the staged residue-purge deletes, the parent's `transfer_block`/`transfer_block_amount` quota consumption, and the six effect-log marks — in the fork savepoint, so an ordinary transaction rollback discards all of it with no bespoke undo path. Direct ledger mutations for the endowment that bypass the write-set are non-conformant; see §3.3.1.

## 4. Rationale

### 4.1 Why self-clone only

Unix `fork()` clones the caller; there is no "fork that other process." Restricting `fork()` to self-clone keeps the parent question unambiguous (the forker *is* the thing copied *is* the child's parent) and removes any need to authorize cloning a third party. Cloning a *different* actor is better served by `DeployActor` against that actor's `code_hash`.

### 4.2 Eternal-with-rent: fork as birth

Cowboy has no in-protocol actor *delete* primitive. CIP-31 introduces per-byte-per-epoch storage rent and an eviction → dormant-state path (CIP-4 §12.3), but eviction is a state-management mechanism, not actor deletion: an evicted actor's record persists and remains addressable. Within that model, `fork()` is *birth*: an addition to the actor population. The simplifications from Unix-fork follow:

- No orphan / re-parent. The `parent` pointer is permanent; an evicted parent is still the child's parent.
- No `wait()`, no zombies, no exit status, no `SIGCHLD`. `fork()` is pure addition.
- `children_count` only ever increments.

This concentrates the safety story onto **two complementary bounds**:

1. **`exec.fork` + `max_children`** (§3.6) — bounds the *rate* of fork by gating creation per actor.
2. **CIP-31 storage rent** — bounds the *steady-state* population: an actor (forked or otherwise) that does not pay rent eventually evicts. A fork bomb of dormant actors is bounded by the cost of keeping them alive.

A manifest that grants `exec.fork` with no `max_children` quota MUST be considered unbounded by reviewers; CIP-31 is the second line of defense, not the first.

### 4.3 Why synchronous

A deferred fork would not return a usable child handle within the handler. Synchronous `fork()` lets the parent immediately `call()` the child, store its address, or hand it to a peer — the composability that makes the primitive useful. The transactional rollback (§3.9) makes synchronous creation safe.

## 5. Reference implementation

> **Amendment status.** The sections above describe the amended surface: modes, `new_owner`, fork profiles, birth-only receipt slots, `ForkInit` one-shot semantics, effect-log truncation, deploy-time callback markers, and the distinct address derivation are **specified here and not yet implemented**. The reference notes below describe the v1 prototype (full-copy fork, `on_fork` via `call_actor`, string-match absence heuristic, shared `children_count`), several of whose behaviors this amendment explicitly supersedes.

A working prototype is implemented in `cowboyinc/node`:

- `Actor.parent` field; actor codec version bump.
- `HostApi::fork_actor(endowment: u64)` host syscall and its `CowboyHost` implementation: entitlement/quota gate, address derivation, child-record creation, storage handling (physical range-walk copy; subtree share once CIP-30 lands), `children_count` bump, optional endowment debit/credit, `on_fork` invocation, write-set-staged rollback.
- `HostApi::transfer_balance(to, amount)` host syscall and its `CowboyHost` implementation (CIP-6 §12.12) — the standalone primitive that `fork(endowment=N)` composes from.
- `pvm-runtime` Python bindings and `cowboy_sdk.fork(endowment=…)` / `cowboy_sdk.runtime.transfer_balance` SDK wrappers.

The `endowment` path stages the parent debit and child credit through the cross-call write set (`stage_balance_delta`), inside the same overlay savepoint as the rest of the fork, and rolls both back when the fork aborts. An enclosing transaction revert therefore cannot leak balance, satisfying §3.3.1.

The implementation reuses the existing `DeployActor` machinery: entitlement gating, `derive_actor_address`, content-addressed code, and cross-call snapshot/rollback.

The storage-copy path is a physical range walk over the parent's `StatePrefix::ActorStorage` key range, copying rows by slot, including hashed long keys (>32 bytes). CIP-30 requires O(1) subtree sharing; that remains implementation work.

## 5.1 Acceptance criteria

- Golden vectors: child address derivation (domain bytes, u64_be count), and golden `Actor` record bytes + `storage_root` per mode (clean root = exactly the seeded lifecycle + receipt slots).
- Raw-host overwrite and delete attempts against `__FORK_MODE__` / `__FORK_PAYLOAD_HASH__` rejected from every context, including `set_reserved_state`.
- A present callback whose body raises a not-found-shaped fault aborts the fork; it is not classified as an absent handler (deploy-time marker, not string matching).
- All six effect classes (event, message, schedule, cancel, extend, deferred) truncated on callback fault, one test each; the failed fork's address receives no leaked effect on successful retry.
- `upgrade_self` inside `ForkInit` rejected, including via a nested call from the callback; fork after same-handler `upgrade_self` clones the frame-entry tuple.
- Fork (either mode, same or different owner) with a non-inheritable grant in the verbatim manifest and no fork profile rejected; profile validated as strict inheritable subset; effective manifest hash matches the profile; `transfer_ownership` of a forked child conveys no additional grants.
- Checked overflow: `fork_count`, `pre-funded + endowment`; pre-existing account row (balance, nonce) survives fork rollback.
- `DeployActor` with the literal fork-salt bytes does NOT collide with any fork address (distinct derivation).
- Golden `Actor` codec bytes for v7 and v8; v7 decodes with `fork_count = 0`; every other version rejected; `fork_count` follows `storage_root`.
- §3.3.3 quota/bond: fork whose **post-birth-write** total lands exactly at `RENT_QUOTA_BASE` succeeds bondless; one byte over requires and locks the bond (with `econ.transfer` per step 6); insufficient parent balance rejects with no child and no debit (107-vs-130 precedence); two-generation recursive fork conserves bond = f(quota) at each step; bond legs revert with the fork savepoint.
- §3.7.1 polarity: profile omitting or altering a parent `sec.*` entry rejected in both modes (v1 byte-exact equality); identical entries accepted; grant attenuation still validated as subset.
- All six effect classes truncated when a successful callback's enclosing frame later faults and is caught above (§3.5's general nested-savepoint rule), one test per class — in addition to the callback-fault six.
- Planted residue in **all six** §3.2 cleared classes — storage row, pin row, kv counters, mailbox row + head/tail and dead-letter pointers (the confirmed plantable class), event-log row, fairness counter — at a future fork address is cleared: absent after block commit and reload, not merely hidden in the overlay; account balance/nonce, token holdings, and PaymentGate budgets all survive intact.
- Enumeration path precharges from the §3.3.3 step-1 syscall-time RYW snapshot counters (same-handler pre-fork writes included in the price): an OOG fork performs no unmetered scan work and charges exactly the precharge.
- Actor-origin `DeployActor` sets `parent` to the deploying actor; `children_count` at `u64::MAX` rejects creation (checked, not saturating).
- Callback marker recomputed identically at every code-record creation (`DeployActor`, `UpgradeActor`, `upgrade_self`, `PublishLibrary` — publish-first vs deploy-first byte-equal) and read via the frame-entry `code_hash`; fork after same-handler `upgrade_self` consults the entry code's marker, not the live code's.
- §3.1 validation order pinned by overlapping-defect vectors (single calls violating three or more rules report the lowest-numbered check).

## 6. Open questions

- **`exec.fork` quota tunables.** The `max_children` parameter is the principal bound. Whether to also expose a max-storage-bytes-at-fork-time cap (so an actor cannot be cloned past a size threshold) is a future refinement.
- **`on_fork` payload extensions.** §3.5 passes only the parent's address. Whether to also pass the child's own address (the caller can query `runtime.get_actor_address()` to get it) or a structured context object is a minor ergonomic question.

## 7. Security considerations

- **Bounded growth, two ways.** Per §4.2: `exec.fork` + `max_children` bounds the rate; CIP-31 rent bounds the steady-state population. Unbounded spawn grants MUST be flagged in manifest review.
- **State-copy cost.** O(1) once the CIP-30 subtree share is implemented; until then the enumeration path prices every copied byte up front (§3.8's precharge rule), so cost tracks size either way.
- **Inherited material.** A full-mode fork copies *most* of the parent's storage (the §3.3 exceptions excluded). Authors of forkable actors MUST treat copied storage as carrying every application secret the parent held (CIP-24-managed secrets are not in actor storage and do not copy — the §3.3 row). The `on_fork` callback can re-key, re-tag, or scrub per-identity material — but scrubbing is a deny-list and fails open. When the child is destined for a different principal (instance spawning, store hires), use clean mode (§3.3.2), which fails closed: no state crosses. Full-mode-with-scrub is appropriate only when parent and child stay under one operator.
- **Owner designation windows.** `new_owner` is written before any callback runs, and a clean child never exists with an unset owner (§3.1's forker default) — so there is no block, however brief, in which a child is claimable via the ownership bootstrap rule. Implementations MUST NOT reorder the owner write after `on_fork`/`on_fork_clean`.
- **Callback re-entry.** §3.5.2's dispatch reservation is load-bearing: without it, `on_fork_clean` is a public re-initialization surface on every clean-forked instance, and `on_fork` re-runs are an idempotence landmine on every full fork. Reviewers SHOULD verify the dispatch layer rejects external invocation of the reserved names, not merely that examples decline to call them.
- **Address-sequence stability under code upgrade.** §3.2's derivation includes `parent.code_hash`. A future actor code-upgrade mechanism that mutates `code_hash` SHOULD be aware that subsequent fork addresses by the same parent will then derive from a different `code_hash` than the actor's prior forks — the deterministic sequence stays internally consistent but is not stable across an upgrade. If pre-upgrade addresses must remain enumerable post-upgrade, the upgrade mechanism MUST commit the `code_hash` sequence used at each fork.
- **Determinism scope of `on_fork`.** `on_fork` is arbitrary actor code and runs under the PVM's deterministic mode (CIP-6 §17). A non-deterministic `on_fork` forks the chain, not the actor.
- **Endowment balance-leak risk if rollback is incorrect.** §3.3.1 requires the endowment debit/credit to flow through the same write-set machinery as the rest of the fork. A non-conformant implementation that mutates account balances directly will silently leak CBY on any tx-revert path (failed `on_fork`, post-fork exception in the parent, gas exhaustion later in the same tx) — the parent stays debited and the child either never materializes or materializes with a balance that the next block's account scan still credits. Reviewers SHOULD check this path explicitly in any `fork_actor` implementation: a deliberate `on_fork` panic in a property test, followed by a balance-conservation assertion across the failing tx, is the cheapest test.
