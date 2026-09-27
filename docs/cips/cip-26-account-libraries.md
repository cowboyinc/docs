---
title: "CIP-26: Account-Scoped Actor Libraries"
description: Per-account Python module publishing and import resolution. Actors deployed by an account may import that account's published libraries; the runtime silently pins each import to the library's code-hash when the actor is created (deploy, or CIP-27 fork initialization), so an immutable actor sees an immutable lib.
icon: package
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-05-04
  **Requires:** CIP-3 (Fee Model), CIP-6 (SDK), CIP-27 (fork pin initialization; error-code allocation `PinSetMismatch` = 134; `CodeMeta` marker), Atomic-init rollback (COW-870)
</Note>

## 1. Abstract

This proposal introduces **account-scoped actor libraries**: a per-account namespace of Python modules that the account's actors may `import` from. A library is published once (`cowboy lib publish`) and stored on chain under the publisher's account. When an actor is deployed, the runtime resolves the actor's import statements against the **deployer's** library namespace, silently pins each resolved name to the library's content hash, and records the pin set in the actor's metadata. At handler execution the PVM allowlist is extended with the pinned modules; lookup goes by hash, not by name, so the actor always sees the exact bytes that existed at deploy time even if the account later replaces the library.

Libraries are **strictly account-scoped**. An actor deployed by account `A` cannot import a library published by account `B`. This is intentional: actors are immutable and consequential, and the cost of accidentally pulling in unverified third-party code is severe. If a library should be reusable across accounts, the consumer publishes their own copy (or pins the upstream hash via the same publish flow against their own account).

## 2. Motivation

Today the PVM allowlist (`pvm-runtime/src/guard.rs::_is_allowed`) admits only the stdlib whitelist (`determinism.rs::default_whitelist`) plus `cowboy_sdk`. Any other import is rejected as `NonDeterministicError: module not allowed: <name>`. Multi-actor systems that share rules end up with one of two unsatisfying patterns:

1. **Inline-and-duplicate.** Copy the shared code into every actor file. The Texas Hold'em example notes this directly: *"hand evaluation inlined — PVM cannot import external modules"*. Drift, duplication, no single source of truth.
2. **Build-time bundling.** A relayer or Makefile concatenates the shared module into each actor before deploy. Works (some examples do this), but the deployed actor's code stops resembling its source, hash-pinning is per-deployer rather than per-library, and audit/review tooling has to reverse the bundle.

Neither pattern survives growth past a handful of small actors. Account-scoped libraries make the obvious thing work: write one `engine.py`, publish it once, import it from N actors.

### 2.1 Why account-scoped (not global)

A global library registry — anyone publishes, anyone imports, content-addressed — looks tempting. We reject it for two reasons:

1. **Trust.** An actor that imports `crypto_helpers` from a global namespace is one TOCTOU window away from importing whatever a stranger published under that name first. Hash-pinning at the deploy site mitigates this for the deploy itself, but the *human* writing the actor source is now responsible for verifying every dependency hash, which is a security review burden with very poor ergonomics. Account scoping makes "did I write or audit this code?" trivially answerable.
2. **Operational sovereignty.** With account scoping, each account controls its own dependency surface. With a global registry, a third party publishing buggy or malicious code under a popular name affects every consumer simultaneously.

Hash pinning at deploy time (§3.4) addresses the second-order risk that an account's own libraries change after the actor was deployed — the runtime always serves the bytes that existed at deploy time, regardless of subsequent `cowboy lib publish` calls.

### 2.2 Why immutability of actors makes this simple

Cowboy actors are immutable: no `upgrade_self` outside the `sys.upgrade` entitlement (very rare and gated), no in-place code rewrite. Atomic initialization rollback ensures an actor that ever runs handlers with library `L@hash_x` will always run with `L@hash_x`. There is no migration story for "the lib changed under me" because the actor's pin set is part of its immutable metadata.

If actor code were mutable, the design would have to deal with version negotiation, dual-pin-set transitions, lib-side compat guarantees, and a much larger spec surface.

---

## 3. Specification

### 3.1 Library data structure

```rust
struct ActorLibrary {
    publisher: Address,    // account that owns this library entry
    name: String,          // import name; 1..=64 bytes; ASCII-only, `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` (stricter than Python's Unicode identifiers — see §3.3)
    code_hash: Digest,     // keccak256 of code bytes; canonical id
    code_size: u32,        // bytes of `code` (cached for gas accounting); u32 (bounded by MAX_LIBRARY_CODE_BYTES = 131_072)
    published_at: u64,     // block height of the publish
}
```

Library code blobs themselves live in the existing `StatePrefix::Code` table (CIP-9 Model A.4) — the same content-addressed, idempotent, garbage-free storage used for actor code. A library entry stores only the metadata + hash; the bytes are shared with any other actor or library that happens to have the same hash.

### 3.2 Storage layout

Two new state prefixes:

| Prefix | Key | Value |
|--------|-----|-------|
| `StatePrefix::Library` | `(publisher: Address, name: String)` | `ActorLibrary` |
| `StatePrefix::ActorLibPin` | `(actor: Address, name: String)` | `Digest` (the pinned `code_hash`) |

The `ActorLibPin` table is the actor's deploy-time-frozen view of its dependencies. Lookup at handler execution is `(self.address, import_name) → code_hash → code_bytes`. The library publisher's `Library` table can change freely (re-publish replaces); the actor's `ActorLibPin` table is write-once at actor creation — `DeployActor`, or CIP-27 fork initialization — and read-only thereafter.

### 3.3 Publishing

A new instruction:

```rust
enum LibraryInstruction {
    PublishLibrary {
        name: String,
        code: Vec<u8>,
    },
}
```

Semantics:

1. Validate `name` matches the Python identifier regex `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`.
2. Run `validate_actor_code(code)` (CIP-3 §2.2.4) — same determinism gate as actor code; bans `os.system`, `pickle`, `import time` at module top, large integer literals, etc.
3. Compute `code_hash = keccak256(code)`.
4. `set_code(code_hash, code)` — idempotent; shared blob storage.
5. Write `Library { publisher: sender, name, code_hash, code_size, published_at: block_height }` to `(sender, name)`. Replaces any prior entry under the same `(sender, name)`.

Gas: `LIB_INSTRUCTION_BASE_CYCLES (200) + LIB_PUBLISH_PER_BYTE_CYCLES (100) × len(code)` Cycles; Cells: `LIB_PUBLISH_PER_BYTE_CELLS (200) × len(code)` if the code blob is new (cache miss), zero **code-blob** Cells if the blob already existed (idempotent `set_code`) — the catalog+metadata row charge below applies on every publish regardless, and the two are different charges for different writes (blob vs catalog/CodeMeta rows; the 128-Cell row charge covers both the `Library` row and the `CodeMeta` marker write). These are the canonical values, mirrored verbatim from `node/execution/src/gas.rs`; every other figure previously printed in this CIP or CIP-3 is superseded — one table, one owner (§3.6).

CLI:

```sh
cowboy lib publish --name engine --code ./engine.py
# → "Library published: name=engine code_hash=0xabcd... size=4321"
```

A `LibraryPublished` event is emitted: `{ publisher, name, code_hash, code_size }`.

### 3.4 Resolution and pinning at actor deploy

When `ActorInstruction::DeployActor` runs:

1. Existing flow validates the actor code, reserves the address, etc.
2. **New step**: scan the actor's source AST for top-level `import X` and `from X import ...` statements where `X` is not in the SDK / stdlib whitelist. Call this set the **candidate import set**.
3. For each name `X` in the candidate import set:
   - Look up `Library` at `(sender, X)`.
   - If absent → `Err(ExecutionError::UnresolvedImport { name: X })`. Deploy fails atomically (atomic-init rollback already covers the actor record per COW-870; the same rollback path applies here before any storage is committed).
   - If present → record `(actor_address, X) → library.code_hash` in the actor's pin set.
4. Persist the pin set as a sequence of writes to `ActorLibPin` in the same atomic deploy.
5. The pin set's total size is added to the actor's deploy gas cost (`cells_per_pin = 64` cells for the hash + key overhead — matches §3.6 and `gas.rs::LIB_PIN_OVERHEAD_CELLS`).

Once pinned, the actor's import map is immutable for the actor's lifetime.

**Fork initialization (CIP-27 amendment).** `DeployActor` is no longer the only writer of a pin set: a CIP-27 `fork()` **atomically initializes the child's pin table as a verbatim copy of the parent's** (same code, same imports — the pins are part of the code binding and MUST clone with it), staged and rolled back with the rest of the fork, and charged per row (CIP-3's fork schedule). This is *initialization at birth*, exactly like the deploy-time write; post-birth immutability is unchanged — no path mutates an existing actor's pins, ever.

**Upgrades must respect the pin set.** Because pins are immutable while `upgrade_self`/`UpgradeActor` replace the code, an unconstrained upgrade could leave an actor executing code whose imports the pin set no longer describes. Normative rule: every code upgrade validates that the new code's statically-extracted candidate import set is **name-identical** to the actor's existing pin names — any addition, removal, or rename rejects the upgrade with a pinned error on **both carriers** — the check runs wherever upgrades run: the `upgrade_self` host path returns `HostError::PinSetMismatch` = 134 (CIP-27's code table; actor code can branch on it), and the EOA/system `UpgradeActor` path returns a new `ExecutionError::PinSetMismatch` variant mapped 1:1 (system instructions cannot carry `HostError`). Both rows, concretely, in this CIP's error table:

| Error | Carrier | Path | Condition |
|---|---|---|---|
| `PinSetMismatch` | `HostError` = 134 | `upgrade_self` host call | new code's candidate import set ≠ pin names |
| `PinSetMismatch` | `ExecutionError::PinSetMismatch` | `UpgradeActor` (EOA/system) | same condition, system carrier |

Acceptance vectors: host-path add/drop/rename each → 134; system-path add/drop/rename each → `ExecutionError::PinSetMismatch`; identical imports pass on both paths. Re-pinning is not offered: an actor that needs different imports is a different actor. Fork/upgrade vectors: fork-then-upgrade with identical imports (accepted), upgrade adding an import (rejected), upgrade dropping one (rejected).

**Pin gas, one rule:** each pin row is charged `LIB_PIN_OVERHEAD_CYCLES = 300` Cycles + `LIB_PIN_OVERHEAD_CELLS = 64` Cells wherever pins are written — deploy and fork alike (CIP-3's fork schedule cites these constants; pin rows are not priced as generic `state_set`). Ancillary rates, same source of truth: `LIB_DEPLOY_AST_PARSE_PER_BYTE_CYCLES = 10`, `LIB_HANDLER_LOAD_PER_BYTE_CYCLES = 1`.

The candidate import set is computed **statically from the source AST** (already parsed during `validate_actor_code`). Imports gated by runtime conditions (`if condition: import lazy_thing`) are not supported — the spec mandates the full set be discoverable from the AST. This is a deliberate restriction: dynamic imports defeat the audit story and complicate gas accounting.

Conditional imports inside SDK helpers (`from cowboy_sdk import ...`) are exempt from this restriction since they resolve to the trusted SDK.

### 3.5 Resolution at handler execution

When an actor handler runs, the PVM's import guard (`pvm-runtime/src/guard.rs`) is extended:

1. Before handler entry, the runtime reads the actor's pin set from `ActorLibPin` and pre-resolves each `code_hash → code_bytes` from the shared `Code` table.
2. For each pinned `(name, code_hash, code_bytes)`, the runtime executes `code_bytes` in a fresh module object, installs it as `sys.modules[name]`, and adds `name` to the import allowlist for this PVM context.
3. The handler runs. Any `import name` resolves to the pre-loaded module.
4. After the handler returns (success or failure), the per-context allowlist additions are discarded; the global allowlist stays untouched.

This mirrors how `cowboy_sdk` is already injected.

### 3.6 Gas

Library gas charges:

| Operation | Cycles | Cells |
|-----------|--------|-------|
| `PublishLibrary` | `LIB_INSTRUCTION_BASE_CYCLES (200) + LIB_PUBLISH_PER_BYTE_CYCLES (100) × len(code)` | `LIB_PUBLISH_PER_BYTE_CELLS (200) × len(code)` **iff the blob is new** — the cache lookup precedes the Cells charge |
| Per-pin overhead (deploy AND fork) | `LIB_PIN_OVERHEAD_CYCLES (300)` | `LIB_PIN_OVERHEAD_CELLS (64)` |
| Deploy-time AST parse | `LIB_DEPLOY_AST_PARSE_PER_BYTE_CYCLES (10) × len(code)` | 0 |
| Per-handler-call lib load (cached) | `LIB_HANDLER_LOAD_PER_BYTE_CYCLES (1) × len(code)` | 0 |
| Per-handler-call lib load (cold — first load of the blob in a block, selected deterministically) | `LIB_HANDLER_LOAD_COLD_PER_BYTE_CYCLES (53) × len(code)` | 0 |
| Catalog + metadata row (every publish, new name or replace, regardless of blob cache hit) | 0 | `LIB_CATALOG_ROW_CELLS (128)` |

**Canonical gas rules.** CIP-3 and CIP-27 reference the table above. Cold/cached classification affects gas exhaustion and receipts and is therefore consensus-critical.

- **Deterministic classification:** warm/cold pricing MUST depend only on committed state and in-block access order. The physical compiled-code cache is an optimization with no pricing role; validators with different process histories MUST charge identically.
- **Block-local shadow cache:** reset it empty at each block, with capacity 32 and LRU ordering over library and actor-code accesses. A library hash absent from this set prices cold. Admission or promotion occurs only after the charge and compile succeed; a 33rd entry evicts the least recently used entry, which prices cold on its next load.
- **Synchronous charging:** every compile-relevant access, including a physical cache hit, MUST call `before_access(kind: ActorCode | Library, code_hash, len) → Result<Cold | Cached, OutOfGas>`. The engine classifies and applies the fallible charge before compile work. `OutOfGas` aborts that access. The runtime then calls `after_access(compile_succeeded: bool)`; only success admits or promotes the entry.
- **Actor-code occupancy:** actor-code accesses count toward shadow capacity but incur no library cold/cached charge. Actor-entry gas already pays for their compilation; charging again would double-bill them.
- **Access order:** each handler accesses libraries first in canonical sorted pin-name order, followed by the actor-code compile. All validators MUST derive the same shadow-cache order.
- **Failure semantics:** a failed charge or compile MUST leave a cold entry cold. A successfully charged and compiled entry stays warm even if the enclosing handler or transaction later faults; shadow membership is block-scoped metering state, not rollbackable application state.
- **Publication accounting:** every publish pays `LIB_CATALOG_ROW_CELLS = 128` for the catalog/metadata row. Code-blob Cells are charged only after the cache lookup establishes that the blob is new. Reusing code bytes does not make a new catalog entry free.
- **Code metadata:** `PublishLibrary` MUST stage the CIP-27 `CodeMeta` marker for each new Code row, derived from the bytes by the same rule as other code producers. Publish-first and deploy-first execution MUST produce the same marker.

**Implementation status:** the inspected node defines the existing gas constants, but the catalog-row constant, lookup-before-blob-charge ordering, and synchronous shadow-cache admission contract remain implementation requirements. The current handler-load helper inserts into its block set before the fallible charge, so an out-of-gas first load can leave a cheaper retry. It must satisfy the failure semantics above before conformance is claimed. Required vectors cover cold/cached pricing, catalog/cache-hit charges, first-load out-of-gas and retry, 33rd-entry eviction and reload, actor-code interleaving, transaction fault then reload, and restart parity.

The per-handler load cost is real — the lib's bytecode has to be re-executed to materialize the module each handler invocation, since the PVM does not retain Python state across calls. Caching compiled bytecode can reduce compilation work, but it does not remove per-handler module materialization or change the deterministic gas rules above.

Normative limits: `MAX_LIBS_PER_ACTOR = 8` (pin-set count cap, enforced at `DeployActor` and inherited unchanged through CIP-27 fork initialization), `MAX_LIBRARY_CODE_BYTES = 131_072` (per-library `code` cap, enforced at `PublishLibrary`), and `MAX_TOTAL_LIB_BYTES = 131_072` (sum of all pinned libraries' `code_size` per actor, enforced at `DeployActor`). Exceeding any yields `LibCapExceeded { kind: Count | Bytes }`. Higher caps invite footguns around actor instantiation cost; these are plenty for the patterns this CIP is designed for.

### 3.7 Replacing or removing a library

`cowboy lib publish --name X --code newer.py` overwrites the publisher's `(publisher, X)` entry with a new `code_hash`. **Existing actors that pinned the prior hash are unaffected** — their `ActorLibPin` entries continue to resolve the old `code_hash` from `StatePrefix::Code` (the old blob is reachable as long as any actor pins it; eligible for the same long-term GC story as any other code blob, currently never).

**Implementation requirements:**

- `gas.rs` defines the gas constants, including `LIB_CATALOG_ROW_CELLS`.
- `pvm-runtime/lib.rs` calls the synchronous `before_access` / `after_access` API from §3.6 on every compile-relevant access, whether the physical cache hits or misses.
- The engine's block-local shadow tracker owns warm/cold classification, the fallible gas charge, admission, LRU ordering, and capacity. `actor_instruction.rs` does not classify or charge these accesses separately.
- `library_instruction.rs` charges for the catalog row and stages `CodeMeta` for new code.
- `pvm_host.rs` and `system_instruction.rs` carry `PinSetMismatch` and its structured receipt codes on both upgrade paths. Protocol dependency pins must include the corresponding shared types.

Required tests cover pin additions, removals, and renames on both upgrade paths; first-load out-of-gas followed by retry; eviction and reload after the 33rd entry; identical `before_access` calls and pricing on physical cache hits and misses; transaction failure followed by reload; restart parity; actor-code/library interleaving; matching `CodeMeta` for publish-first and deploy-first execution; and publish charging: a cache hit avoids duplicate blob-storage Cells, while both new-blob and cache-hit publishes pay catalog-row Cells.

A separate `RemoveLibrary` instruction removes the metadata entry only — the underlying code blob persists for any actor still pinning it. Removal is purely a "stop offering this name to new deploys" operation. It emits a `LibraryRemoved` event `{ publisher, name }` (wire layout `publisher(20) ‖ name_len(1) ‖ name`), mirroring the `LibraryPublished` event of §3.3 (wire layout `publisher(20) ‖ name_len(1) ‖ name ‖ code_hash(32) ‖ code_size(8 LE)`).

### 3.7bis Error codes

The runtime error variants for CIP-26 (matching `node/execution/src/error.rs`):

| Variant | Fields | Raised at | Rule |
|---------|--------|-----------|------|
| `InvalidLibraryName` | `reason: String` | `PublishLibrary` | R1 — name failed length/ASCII/regex (§3.1, §3.3) |
| `LibraryCodeParseError` | `message: String` | `PublishLibrary` / `DeployActor` | R7 — source AST parse failure during candidate-import extraction |
| `LibraryNotFound` | `publisher, name` | `RemoveLibrary` | R8 — remove on a missing `(publisher, name)` entry |
| `UnresolvedImport` | `name: String` | `DeployActor` | R5 — a candidate import has no library under the deploying account (§3.4) |
| `LibCapExceeded` | `kind: Count\|Bytes, observed, cap` | `DeployActor` / `PublishLibrary` | R14 — pin-count, per-library-byte, or total-byte cap exceeded (§3.6) |
| `PinSetMismatch` | — (`HostError` = 134 → structured receipt code **1834**, per the existing 1700+code receipt mapping) | `upgrade_self` (host path) | §3.4 upgrade rule — new code's candidate import set ≠ pin names |
| `PinSetMismatch` | — (`ExecutionError::PinSetMismatch` → structured receipt code **1755** — 1739 is already CIP-24's `TlockTagTooLong` and this CIP's 1734–1738 block abuts occupied CIP-24 territory; 1755 is the next free value after the occupied 1750–1754 run, and the global receipt-code uniqueness gate is the binding check at pin time) | `UpgradeActor` (EOA/system path) | same condition, system carrier — receipt codes enter the consensus receipt root, so both are pinned, and they are deliberately DISTINCT codes (1834 vs 1755): the carriers differ and a false 1:1 claim would misstate the receipt root |

### 3.8 Manifest interaction

Actors that import account libraries do not need to declare anything in their `ActorManifest`. The pin set is computed from source and recorded by the runtime. This keeps the manifest focused on entitlements (capabilities the actor wants from the host) rather than duplicating dependency information that the source already encodes.

Future revisions may add a manifest field listing expected pins for human review — purely informational, not enforced — but the canonical pin set always comes from the AST scan.

**Tooling guidance (COW-1133).** Even without a manifest field, the pin set is fully discoverable on-chain: it is the set of `StatePrefix::ActorLibPin` entries keyed `(actor_address, name) → code_hash` (§3.4). Tooling SHOULD surface it from there rather than from the manifest:

- **Explorer:** for any deployed actor, scan its `ActorLibPin` rows and display `name → code_hash` (optionally resolving each `code_hash` to the publisher/library it came from) so users can see exactly which library versions the actor is frozen against.
- **CLI:** a `cowboy actor pins <address>` command lists the same `(name, code_hash)` pairs (implementation tracked separately as a CLI follow-up; this section specifies what it surfaces). Because the pin set is deploy-time-frozen and read-only, the displayed dependencies are stable for the life of the actor.

### 3.9 Determinism guarantees

A handler invocation against the same `(actor, block_state, payload)` always sees the same library code, because:

- The actor's `ActorLibPin` table is immutable post-deploy.
- The pinned `code_hash` resolves to a content-addressed blob in `StatePrefix::Code` that is written once and never mutated.
- Library code passes the same `validate_actor_code` checks as actor code, so it cannot introduce non-deterministic behavior the host would otherwise reject.

The only failure mode is if the underlying `Code` blob is somehow missing at handler time — currently impossible since `set_code` writes are durable and never deleted. If a future GC mechanism is introduced, the GC must check the union of `ActorLibPin.code_hash` (across all live actors) before reaping, the same way it would have to for actor code.

---

## 4. Worked example

Account `0xAlice` publishes a shared engine for her three game actors:

```sh
$ cowboy lib publish --name engine --code engine.py
Library published: publisher=0xAlice name=engine code_hash=0xabcd1234... size=4321
```

She deploys her three actors that import `engine`:

```python
# in world_actor.py:
from cowboy_sdk import actor, public, runtime
import engine

@actor
class World:
    def init(self, payload):
        runtime.emit_event("init", {"version": engine.VERSION})
```

```sh
$ cowboy actor deploy --code world_actor.py --salt $SALT \
    --init-handler init --init-payload '{}'
✓ Library imports resolved:
    engine → 0xabcd1234...
✓ Actor deployed: 0xWorld...
```

Later, Alice publishes `engine v2`:

```sh
$ cowboy lib publish --name engine --code engine_v2.py
Library published: code_hash=0xfeed5678... size=4520
```

The already-deployed `World` actor continues to resolve `engine → 0xabcd1234` (the v1 hash is in its pin set). Any *new* deploy from Alice resolves to v2.

Bob, working from account `0xBob`, tries to deploy an actor that imports `engine`:

```sh
$ cowboy actor deploy --code world_actor.py --salt $SALT --init-handler init
Error: deploy failed: UnresolvedImport { name: "engine" }
   no library named "engine" published under deployer account 0xBob
```

Bob has no `engine` library under his own account; Alice's is invisible to him. He must `cowboy lib publish --name engine --code <vetted_copy.py>` against his own account first.

---

## 5. Reference implementation

Code paths that change:

- `node/storage/src/state_key.rs` — add `StatePrefix::Library` and `StatePrefix::ActorLibPin`.
- `node/storage/src/traits.rs` — add `get_library`, `set_library`, `delete_library`, `get_actor_lib_pins`, `set_actor_lib_pin`.
- `node/storage/src/accounts.rs` — concrete impls of the above.
- `node/cowboy-types/src/instruction.rs` — `Instruction::Library(LibraryInstruction::{PublishLibrary, RemoveLibrary})`.
- `node/execution/src/execution/library_instruction.rs` — new file; instruction handler.
- `node/execution/src/execution/actor_instruction.rs::DeployActor` — add the AST scan + pin resolution step before atomic init.
- `node/pvm/crates/pvm-runtime/src/guard.rs` — extend `_is_allowed` and the `_pvm_import` hook to consult the per-context pin set.
- `node/pvm/crates/pvm-runtime/src/lib.rs` — pre-load pinned libraries into `sys.modules` before handler entry.
- `node/cli/src/commands.rs` — `cowboy lib publish | remove | list` subcommands.

Tests:

- Publish + import + handler call round-trip.
- Cross-account import is rejected.
- Pin survives library re-publish.
- Pin survives library removal (existing actors keep working; new deploys can't pick up the removed name).
- Atomic deploy rolls back on `UnresolvedImport` (extends the COW-870 regression test).
- Determinism: re-running the same handler at the same height with the same input returns identical state across nodes after a library re-publish.

---

## 6. Backwards compatibility

Pure addition. Existing actors don't have `ActorLibPin` rows; the import guard treats their pin set as empty, so behavior is unchanged. Existing deploys continue to be atomic with no extra cost.

---

## 7. Security considerations

### 7.1 Trust model

The trust boundary is the **deployer's account**. An actor implicitly trusts every library its deployer has published, because the deployer chose to deploy the actor knowing those libraries exist and what they contain. Cross-account imports are forbidden precisely so this boundary is unambiguous.

A compromised deployer key can publish malicious library updates, but those updates only affect *future* deploys from that account — already-deployed actors' pins are immutable. This bounds blast radius: an attacker who compromises Alice's key cannot retroactively poison her existing actors, only the next ones she (or the attacker) deploys.

### 7.2 Determinism attacks

The library code path goes through the same `validate_actor_code` checks as actor code, so it cannot reach `os.system`, `time.time()`, hardware FPU, or other non-deterministic primitives. Library authors can no more break consensus than actor authors can.

### 7.3 Storage exhaustion

Library blobs live in the same `StatePrefix::Code` table as actor code, which today never garbage-collects. The marginal cost of accepting a 1 MiB library blob is the same as accepting a 1 MiB actor blob. Gas pricing in §3.6 is set to make publishing realistic-sized libraries cheap and oversize libraries painful.

`MAX_LIBS_PER_ACTOR = 8` caps the per-handler load cost to a known maximum and prevents a deploy-time DoS against the import resolver.

### 7.4 Pin-set tampering

`ActorLibPin` is written exclusively by the chain runtime at actor creation — `DeployActor` or CIP-27 fork initialization (§3.4) — and exclusively read during handler dispatch. There is no SDK API to mutate pins, and the storage prefix is excluded from `runtime.set_state` / `self.storage[]` (mirroring the existing `__OWNER__` / `__INITIALIZED__` reserved-key protections in CIP-6).

---

## 8. Out of scope

- **Cross-account imports.** Forbidden by design (§2.1). Not a future addition.
- **Bytecode caching across handler invocations.** Performance optimization; doesn't change semantics. Worth doing eventually since the per-call lib re-execution is real cost.
- **Lazy / conditional imports.** Disallowed by §3.4 because they break the AST-driven pin discovery. Could be revisited if a compelling use case emerges.
- **Library transitive dependencies.** Transitive imports are **not resolved** by the §3.4 mechanism: the deploy-time AST scan and pin set are computed from the **actor's** source only, and only the actor's own `ActorLibPin` modules are installed into `sys.modules` at handler entry (§3.5). A library that does `import other_lib` therefore only resolves if `other_lib` is **also a direct import of the actor** (so it is independently pinned under the actor); a library's private dependency that the actor does not itself import is unresolved at runtime. Library authors must flatten their dependency closure into the actor's direct import set (or vendor it into the library source). Automatic transitive resolution is spec'd separately if pain emerges.
- **Versioning / semver.** Not built in. Account holders can publish multiple distinct names (`engine_v1`, `engine_v2`) if they want explicit version pinning visible in actor source. The hash-pinning at deploy makes "what version did this actor pin?" trivially answerable from `ActorLibPin`.

---

## 9. Open questions

- Should `cowboy lib publish` accept a directory and bundle multiple files into a single library, or strictly single-file? Single-file is simpler and matches how almost all examples are structured today; multi-file can be added if anyone hits the friction.
- Should a library be allowed to import `cowboy_sdk`? Most utility libraries won't need it (pure rules), but some will (lib that emits events). Default: yes, allow `cowboy_sdk` imports from libraries on the same terms as actors. The handler-execution import map already covers it.
- `MAX_LIBS_PER_ACTOR = 8` is a guess. Empire's example would use 1; Texas Hold'em would use 1. If real usage demands more, raising the cap is a numeric change with no spec impact.
