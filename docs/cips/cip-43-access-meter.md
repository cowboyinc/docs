---
title: "CIP-43: Access Meter"
description: Adds a consensus store-I/O meter, a third EIP-1559 fee track, Transaction v2 fields, and gas-vectors.v2
icon: gauge
---

<Note>
  **Status:** Landed (regenesis-only)<br />
  **Type:** Standards Track<br />
  **Category:** Core<br />
  **Created:** 2026-08-26<br />
  **Requires:** CIP-3<br />
  **Related:** CIP-30, CIP-31, CIP-39
</Note>

## Status / landed state

This CIP now describes the access meter **as it landed**, not a provisional design.

- **Landed at** node devnet `65a6518f24e2734c3289f0230f92f70d072ee6b0` (node PR #1457, a merge-commit integration bundle of the metering-audit line; devnet sync-3 CBQS-v2 folded in as #1533). Reflected here on 2026-09-07.
- **Amendment (2026-09-08, COW-3896, decision 2d).** Access payer lanes were rebalanced at node devnet `22e325b90ab03cd1d4dc4e7e3b1cccb036211242` (node #1549): `LANE_USER_ACCESS` 2,294 → 2,051 and `LANE_RUNNER_ACCESS` 901 → 1,144 (with `RUNNER_TX_ACCESS_LIMIT`), and two runner-callback reservation constants were added — `RUNNER_CALLBACK_HANDLER_ACCESS_ALLOWANCE = 152` and `RUNNER_CALLBACK_ACCESSES_LIMIT = 314`. Timer (901) and System (4,096) are unchanged and the three payer lanes still sum to 4,096. The rows and prose amended for this decision cite node devnet `22e325b9`; the rest of the capacity table remains pinned to the `65a6518f` landed state above. See "Two-results / two-users lane derivation and the runner-callback tier" below for the derivation and the aggregate-policy qualification.
- **CBSS amendment (2026-09-22, COW-4074 / COW-4073).** The genesis version-allocation and deletion rules are landed at node `55022cf80e71475bf0d0f7c00041a401ba10fea9` (`ACCESS_WEIGHT_TABLE_VERSION = 11`). Appendix A.2 imports that pin's `SetSecret`, `DeleteSecretVersion` and `DeleteSecret` rows. The durable version high-water mark, delete cascade and retained evidence are specified in [CIP-24 §3.2](cip-24-secrets-manager.md#32-storage-layout-in-0x04) and §3.5. Deletion reserves both fixed work and committed-state-dependent evidence operations; the displayed rows include the common pause read. Client-facing access limits are maintained in node's [CLIENT-LIMITS.md](https://github.com/cowboyinc/node/blob/55022cf80e71475bf0d0f7c00041a401ba10fea9/docs/access/CLIENT-LIMITS.md). Other appendix rows retain the baseline provenance below.
- **Baseline `ACCESS_WEIGHT_TABLE_VERSION = 4`** (`execution/src/access_weights.rs:14` at `65a6518f`). The two fingerprints in §5 identify that baseline. Appendix A also includes three CBSS rows from version 11, three registration rows from proposed version 16, the artifact-retirement row and three RAS rows from version 13, two hire rows from version 14, the direct `CommitManifest` row from proposed version 16, and `DeleteVolume` from proposed version 15; it is a mixed-provenance reference, not the baseline table.
- **Public Store amendment (2026-09-23, CIP-33 §2.1.2).** Node [PR #1710](https://github.com/cowboyinc/node/pull/1710) merged on devnet as `5cf8ce13` with access-weight table version 13. Intermediate PR code at version 12 added the Public destination-commitment read; version 13 retains that read and adds the active Public-volume pin. `TradingPostRegisterArtifact` is `Rk(12)+Wk(5)` and `TradingPostRetireArtifact` is `Rk(6)+Wk(4)`; direct and staged manifest commits and volume transfer also gain a pin read. Version 13 requires a new coordinated regenesis. Merge does not establish deployment. The version-4 fingerprints below retain their historical provenance; the version-13 fingerprints are stated in §5.
- **Hire-lifetime pin correction (2026-09-23, CIP-33 §2.1.2).** Node [PR #1708](https://github.com/cowboyinc/node/pull/1708) commit `194f400f` records access-weight table version 14. It keeps a Public volume pinned while a non-`Revoked` Store hire uses it. `TradingPostHire` becomes phase-one `Rk(12)+Wk(6)` plus the existing split term; `TradingPostRevokeHire` becomes `Rk(9)+Wk(3)`. Hire already holds the artifact, so it adds one pin-count read and one write. A hire record does not store its volume, so revocation adds the version and artifact reads, the pin-count read, and one write. This is a measured proposal for a coordinated regenesis, not a deployed or merged release.
- **Pinned-volume deletion (2026-09-24, CIP-33 §2.1.2).** Node [PR #1708](https://github.com/cowboyinc/node/pull/1708) commit `d60babed` records access-weight table version 15. `DeleteVolume` gains one Public Store pin read, becoming `Rk(10)+Wk(8)`. The earlier artifact, hire, commit, and transfer rows remain. The existing occupied-ID check and permanent garbage-collection tombstone prevent reuse without a new pin-specific creation row. The three version-15 fingerprints are in §5. This is a proposed regenesis table, not a deployed or merged release.
- **Permanent volume registration (CIP-33 §2.2).** Node PR #1708 commit `13de6a65` records proposed table version 16. It adds the permanent volume reverse index for artifact 0 and Store artifacts. `TradingPostDeclare` is `Rk(3)+Wk(4)`, `TradingPostDeclareVersion` is `Rk(5)+Wk(4)`, and `TradingPostRegisterArtifact` is `Rk(13)+Wk(6)`. Artifact 0 creation in `Declare` and `DeclareVersion` adds the creator `StorageCommitment` ownership read, the reverse-index uniqueness read, and the reverse-index write. Store registration already read the destination commitment, so it adds only the reverse-index read and write. The three version-16 fingerprints in §5 identify `13de6a65` only. This is a proposed regenesis table, not a deployed release.
- **Devnet-merge repricing (node PR #1708 at `4294f24b`).** The merge of devnet into node PR #1708 repriced the per-shard terms of direct `CommitManifest` and `CommitManifestFinalize` and changed the table-16 fingerprints; §5.5 gives the new terms and §5 the new fingerprints. Table numbers after 13 are PR-local and collide with devnet numbering; this CIP identifies those tables by node commit (§5).
- **Activation is regenesis-only.** The meter is genesis-active from block 0 of the launch regenesis; there is no live-chain activation height and no observe-only ramp on the landed chain (§11).
- **Live scalar** `access_weight_scalar_ppm = 1_350_000` at genesis (`COW3659_ACTIVATION_ACCESS_WEIGHT_SCALAR_PPM`, `types/src/constants.rs:273`); the identity scalar `1_000_000` is the compatibility default (`DEFAULT_ACCESS_WEIGHT_SCALAR_PPM`, `types/src/constants.rs:142`).

The values below are **committed protocol constants**, not benchmark-gated placeholders. Where a constant is still marked benchmark-gated in the source (⟨benchmark-gated⟩), it is called out; the number is nevertheless the one that ships and is consensus-critical at version 4. The normative source of truth is the node code at `65a6518f` and `docs/access/CLIENT-LIMITS.md`.

### Changelog since the 2026-08-27 draft

| Area | Draft (version 1) | Landed (version 4) | Ticket / PR |
| :---- | :---- | :---- | :---- |
| Table version | `1` | `4` | C0 COW-3659 (#1500), CBQS-v2 (#1533) |
| Scan slope | `5 + ceil(n/2)` | `5 + ceil(n/8)` | C0 COW-3659 (#1500) |
| Nested frame | `1` | `75`, plus a `40` first-use setup | C0 COW-3659 (#1500) |
| Top frame | not priced | `99` (`W_TOP_FRAME`) | A6 COW-3657/COW-3659 |
| Fan-out per item | `1` | `ceil(k/4)` (`2/8`) | COW-3639 |
| Native STF | intrinsic access cost zero | user-signed System charges structural rows; the tx envelope charges `E=5`; host crossings charge `1` | A1 COW-3631 (#1479), A3 COW-3648 (#1476), A2 COW-3647 (#1483) |
| User tx ceiling | none (node cap 512) | `USER_TX_ACCESS_LIMIT = 1_024` charged (block validity) | D40 |
| Ordinary System ceiling | none | `ORDINARY_SYSTEM_TX_ACCESS_LIMIT = 1_500`; selection sub-lane `2_596` | Decision 21 |
| Per-block aggregate | not enforced | `BLOCK_AGGREGATE_ACCESS_CEILING = 4_096` (block validity) | C1 COW-3632 (#1511) |
| Scalar governance bound | codec `1 ..= 1e9`, fit `7,039,062` | governed `[1_000_000, 1_350_135]` | Decision 21 / COW-3659 |
| Read-side bytes | (proposed to leave cells for access) | priced in **cycles** at `7`/KiB, not access | COW-3633 (#1481) |
| Nested exhaustion | unspecified | EIP-150 all-but-1/64 caller reserve | B7 COW-3557 (#1482/#1495) |
| CBQS dispatch | cycle-only reads/writes | native rows priced in both cycles and access (CBQS-v2) | COW-3712 (#1497), sync-3 (#1533) |

## Abstract

Cowboy prices execution in three consensus dimensions: compute in `cycles`, store I/O in `accesses`, and persisted bytes in `cells`. One `access` is one cold `StateGet`; every other store operation has a deterministic weight relative to that unit. `memory_cells` remains an unpriced liveness bound, while recurring storage `rent` remains a separate ledger. This CIP adds the access meter, its transaction and receipt fields, an independent EIP-1559 fee track, the enforced store gate, and the versioned weight schedule. On the landed chain the meter is not intrinsic-zero for native work: user-signed System instructions charge structural reservations before dispatch, the per-transaction envelope charges a fixed row, and each guest host-call crossing charges one unit. (Sources: CIP-3 §§2.1, 2.4; Technical Whitepaper §§2.3, 17.1; `docs/access/CLIENT-LIMITS.md`; `execution/src/access_weights.rs`; `types/src/constants.rs`.)

## 1. Why this is not the third meter §8.5 forbids

The Storage Whitepaper §8.5 states:

> RAS MUST NOT introduce a third on‑chain meter. On‑chain operations related to storage MUST consume Cycles and Cells per Technical Whitepaper §4; storage rent MUST be maintained in the Storage Manager's separate billing ledger.

That prohibition scopes the off-chain RAS/CBFS rent mechanism. Recurring rent for retained bytes MUST remain in the Storage Manager billing ledger described by CIP-31; it MUST NOT be folded into transaction gas. (Sources: Storage Whitepaper §8.5; CIP-31 §§1–5.)

The `accesses` dimension is categorically different. It prices consensus compute-domain work: transient QMDB reads that can block proposal execution and writes that are durably flushed at `apply_finalized`. It is charged and settled inside the state-transition function in the same EIP-1559 family as cycles and cells; it is not a rent ledger and does not price bytes held over time. (Sources: Technical Whitepaper §§State Rent, 17.1, 17.5.)

CIP-30 §3.3 already requires CIP-3 or a revision to fix a deterministic, bounded cost for trie-update work on top of per-byte Cell cost. This CIP supplies the missing store-I/O dimension and gate; it does not alter CIP-31's off-chain billing ledger. (Source: CIP-30 §3.3.)

## 2. Motivation

1. A transaction pays for compute (cycles) and stored bytes (cells), but the cost that dominates validator wall-time — cold state reads during block proposal and the writes flushed at finalization — was charged only as a flat cycle token, so a read-heavy actor could consume seconds of I/O for almost nothing and push the block past the proposal deadline.
2. The `access` meter prices that store I/O in its own unit (one cold read = 1, other store operations weighted), with its own EIP-1559 basefee, per-transaction and per-lane limits, and a per-block aggregate ceiling, charged before each store operation through a single enforced gate that every host-visible read and write must pass.
3. With compute, store I/O and persisted bytes each metered in the resource they consume, every transaction's validator time is bounded by protocol constants calibrated on the slowest supported validator, instead of by a wall-clock reap that different machines trigger at different points.

(Sources: `docs/access/README.md`; `docs/access/CLIENT-LIMITS.md`; Technical Whitepaper §§4.1–4.3, 17.)

The existing schedule also mis-denominated store work across two markets. Technical Whitepaper §17.3 assigned 100 cycles to a read and 200 cycles to a write, while §17.4 assigned reads 0.01 cells per byte as "bandwidth metering." CIP-3 is normative over that architecture reference, so this CIP removes the flat store-I/O charges from cycles and moves store I/O into `accesses`. Read-side **bytes** are priced separately in cycles by measured bandwidth (`STORAGE_READ_CYCLES_PER_KIB = 7`, `execution/src/gas.rs:413`); they do not enter cells and do not enter the access set (§6). (Sources: CIP-3 §§2.1, 2.2.1; Technical Whitepaper §§17.3–17.4; `docs/access/design/DESIGN-cow3633-read-bytes.md`.)

The resulting resource model is orthogonal:

| Resource | Dimension | Economic treatment |
| :---- | :---- | :---- |
| Bytecode and pure host compute | `cycles` | Independent basefee |
| QMDB reads during propose and writes flushed at `apply_finalized` | `accesses` | Independent basefee |
| Read-side value bytes moved across the PVM boundary | `cycles` | `7` cycles/KiB (`gas.rs:413`) |
| Bytes written or allocated persistently | `cells` | Independent basefee, plus rent where applicable |
| Peak execution RAM | `memory_cells` | Liveness bound; not priced |
| Retained bytes over time | rent | Separate ledger |

(Sources: Technical Whitepaper §§3.2, State Rent, 17.1, 17.5; Storage Whitepaper §8.5; `execution/src/gas.rs:413`.)

### How host operations are priced

The meter MUST NOT charge by operation name. Each host call MUST be decomposed into the store operations it performs, and the transaction pays for the committed state it touches:

- The landed primitives are `R = 1` per cold point read (`W_READ`, `types/src/constants.rs:150`), `W = 1` per cold staged write (`W_WRITE`, `:151`), `Scan(n) = 5 + ceil(n/8)` per bounded scan (`W_SCAN_BASE = 5` `:153`, items-per-unit `8` `:154`), `Copy(n) = ceil(n/8)` per storage copy (`access_weights.rs:22–23`), `Fanout(k) = ceil(k/4)` per derived-key fan-out (`access_weights.rs:27–28`), `NestedFrame = 75` per nested actor frame (`W_NESTED_FRAME`, `constants.rs:156`) with a one-time `40`-unit first-nested setup per frame chain (`W_FIRST_NESTED_SETUP`, `:157`), and a top execution frame `W_TOP_FRAME = 99` (`:155`). Cold/warm accounting is transaction-scoped: the first read of a key and the first write of a key charge, while repeats in the same direction are free.
- Every guest host-call crossing charges `W_HOST_CALL = 1` (`constants.rs:152`) through an invocation-scoped credit: a successful positive row already includes that unit, so it is not an additional surcharge; warm, no-op, and caught early-reject calls still retain exactly one crossing unit, and unused credit is never refunded across invocations (COW-3647; `docs/access/CLIENT-LIMITS.md` "COW-3647 host-crossing consequences").
- Every host call MUST have a weight-table row in §5.1 or §5.1.1; execution-envelope and deferred-work rows are in §5.4. CI SHOULD reject any host method without a row. Representative decompositions are unchanged from the draft except that a nested frame now costs `40 + 75` on first use and `75` thereafter, a scan uses the `ceil(n/8)` slope, and each crossing carries its `1`-unit credit.
- `Scan`, `Copy`, and `Fanout` MUST charge from a committed or input-visible bound before dispatch, never from what the operation happens to find. A charge MUST NOT depend on cache state, hardware, latency, or timeouts. The fixed `MAX_LIBS_PER_ACTOR` scan bound applies because no O(1) pin count provides a smaller preflight bound.
- Only trusted runtime **sidecars** are zero-access: `gas_left`, `charge_gas`, and the instruction-gas flush/refresh (`RuntimeSidecar → 0`, `execution/src/access_weights.rs:521`). Every other guest crossing still charges its one-unit `W_HOST_CALL` (§2, A2) — including `context` and the cryptographic host calls (`context`/`keccak256`/`ed25519_verify` each call `charge_host_call_base` first, `pvm_host.rs:5271,6510`; `GuestZero → W_HOST_CALL`, `access_weights.rs:522`). What stays outside the access meter is the *underlying work* of those calls: bytecode and crypto compute (charged in cycles), persisted byte volume (cells), and transient RAM (the memory bound). Native STF reads are charged as specified in §3.

In short, a host call costs the number of distinct state keys it cold-reads and cold-writes, plus a fixed structural charge for each scan, copy, fan-out, nested frame, top frame, and guest crossing, all bounded before the call runs. (Sources: `docs/access/CLIENT-LIMITS.md`; `execution/src/access_weights.rs`; `types/src/constants.rs`.)

## 3. The `access` dimension

The unit is one cold `StateGet` = one access. Other operations consume the relative weights in §5.

Cold/warm accounting MUST use one transaction-scoped access set with EIP-2929 semantics. The first logical in-access and the first logical out-access for a key are charged; repeats of the same direction are warm and free. Read-then-write of one key charges once in each direction. The access set MUST be shared by nested frames and MUST NOT roll back when a nested frame reverts.

The transaction warm set and the committed-value cache MUST survive a nested revert. Only cache mutations caused by staged writes roll back, in reverse order, to their pre-write values. A committed read learned inside a reverted frame therefore remains cached and cannot cause a later uncharged physical read.

An `AccessKey` is the tuple `(committed store namespace/prefix, key bytes as stored)`. Within the in-access or out-access direction, two operations are warm-equivalent if and only if they resolve to the same physical committed key. `Code(hash)` and `Library(hash)` use different prefixes and are distinct keys; same-address account reads from sender, transfer-target, or balance-flush paths resolve to one key; system-record wrappers alias the underlying record key they read.

### Native work is no longer intrinsic-zero

On the landed chain, native state-transition work is metered wherever a signer or the envelope owns it. The intrinsic access cost is no longer zero.

- **Per-transaction envelope (A3, COW-3648).** Every signed transaction charges a fixed envelope row `E = 5` structural accesses = `4R + 1W` (`SIGNED_TX_ENVELOPE_STRUCTURAL_ACCESSES = 5`, `types/src/constants.rs:282`; `BASE_ENVELOPE_ACCESSES_SIGNED_TX`, `execution/src/access_weights.rs:120–121`). The four reads are `Account[from]`, the lane-fee multipliers, the sender vesting-admission read, and the sender-tail vesting-floor backstop read; the write is the sender-account tail. This row is deliberately outside the transaction-scoped cold/warm set. Protocol-minted deferred work does **not** traverse the signed envelope, so its `E = 0` (`SAFETY_MARGIN_ACCESS = 0`, `constants.rs:2939`).
- **User-signed System instructions (A1, COW-3631).** Native System-instruction handlers charge in two phases (`execution/src/execution/system_instruction.rs:108-117`). A fixed, input-visible structural row is reserved **before any admission gate, actor-pause/bank check, or handler dispatch**. A handler that then decodes a committed live-set index reads that cardinality and reserves the **exact cardinality-dependent row before the first per-item walk or mutation** (`gov_snapshot.rs:114-120`; the write side reserves the full committed `N` even when fewer rows are ultimately selected). Each reservation, once actually made, is charged in full and **never refunded** — on success or rejection — so committed state cannot create a refund oracle while exhaustion stays state-preserving; a rejection *before* the second phase simply never incurs the phase-two reservation. Both phases stand outside the transaction-scoped cold/warm access set. (Sources: `docs/access/CLIENT-LIMITS.md` "D41 deferred-child failures", "Production signers"; `docs/access/design/DESIGN-cow3631-native-system-access.md`.)
- **Host-call crossings (A2, COW-3647).** Each physical non-sidecar guest crossing charges `W_HOST_CALL = 1` (`constants.rs:152`), credited into the row it belongs to as described in §2. Instruction-gas flush/refresh are the only trusted zero-access runtime sidecars.
- **CBQS native rows (COW-3527 note / CBQS-v2).** CBQS dispatch is priced in **both** cycles and access. Its eight decoded v2 rows reserve the complete structural row before the first admission check and never refund it (§5, §12).

Every reserved read charges one structural `R` and each reserved write one structural `W`, accumulated in the same structural meter as every other §5 primitive and converted to charged units once at the boundary (§5.0) — not scaled per reservation. Remaining native STF reads that no signer or envelope owns — pure admission, nonce, and basefee bookkeeping charged elsewhere — stay out of `accesses`; only pure in-memory work is free. Read-side value bytes are priced in cycles (§2, §6), not access. (Sources: CIP-39 §15.1; `execution/src/gas.rs:413`.)

Reads MUST charge before the latency-bearing store operation on the proposal path. Writes MUST charge at stage time, before their later durable flush at `apply_finalized`. Read weights protect the proposal deadline; write weights protect fairness and finalization cadence.

## 4. The enforced gate

Every actor-reachable store operation MUST route through one before-dispatch charge point. A conforming client MUST make the raw state reader/writer inaccessible to host handlers, nested frames, system-route helpers, and post-effect code except through that gate. (Sources: CIP-3 §2.2.1.1 deterministic-ordering rule.)

The reference sealed-capability design has these portable requirements:

- A `MeteredHostStore`-equivalent capability owns the only raw store handle. Raw state construction and the typed access trait MUST be sealed outside the gate module.
- Sync and async entry points MUST share one charging core, and the blocking adapter MUST exist only behind the gate.
- Every scan, copy, or fanout MUST precharge from a committed or input-visible bound before dispatch. `Scan(n)` MUST charge from the validated bound and insert its returned keys, at most `n`, into the in-access warm set; a later point read of a returned key is warm, while a key outside the returned set MUST charge `R`. `Copy(n)` MUST insert the `n` copied child keys into the out-access warm set, so those keys are not charged `W` separately. `Fanout(k)` MUST insert the `k` derived per-item record keys into the in-access warm set; every per-item nested dispatch MUST still charge `NestedFrame`. Each aggregate MUST charge from its validated or derived bound irrespective of prior warmth of any individual key; implementations MUST NOT reduce the aggregate to `n - 1` or `k - 1`. A key outside the exact precharged or returned set that is read anyway MUST pass through the gate and charge cold, so a bound mismatch cannot create free I/O.
- CI SHOULD reject raw-store imports outside the gate and any HostApi method without a weight-table row. Equivalent static enforcement is conforming if it provides the same non-bypass property.

## 5. Weight table

The baseline table has `ACCESS_WEIGHT_TABLE_VERSION = 4` (`execution/src/access_weights.rs:14` at `65a6518f`). Appendix A combines that baseline with three CBSS rows from version 11, three Trading Post registration rows from proposed version 16, the artifact-retirement row and three RAS rows from version 13, two hire rows from version 14, direct `CommitManifest` from proposed version 16, and `DeleteVolume` from proposed version 15; §5.5 and Appendix A name the sources. The table version is a protocol constant. Changing relative weights requires a coordinated release under CIP-3 §4; governance may scale the whole table through `access_weight_scalar_ppm` within the bound in §8 but may not rewrite individual rows.

The historical baseline table is identified by two FNV-128 fingerprints
computed over the closed row set (`execution/src/access_weights.rs`):

- **Executable-access fingerprint** `145510929673014535888950164850774767257` (`access_weights.rs:3339`).
- **Composed tri-cost fingerprint** `202721689210127662310138855754496845765` (`access_weights.rs:3474`).

The two fingerprints above identify the baseline `65a6518f` table. At node devnet `5cf8ce13` (table version 13), the access fingerprint is `87050058421315405739685876342088125776`, the Gate-1 prefix tri-cost fingerprint is `21174235201325959043110468468392591213`, and the session-extension tri-cost fingerprint is `118638424846733099013270632665704235802`. The node test `user_system_executable_rows_are_pinned_for_every_wire_variant` computes the first from the closed executable access rows, extends that snapshot with System wire opcodes, three cost dimensions, and phase-two classifications for the Gate-1 prefix, then extends it with session-work values for the third fingerprint. Appendix A retains the baseline except for three CBSS rows from node `55022cf80` (version 11), three Trading Post registration rows from `13de6a65` (proposed version 16), the retirement row from `5cf8ce13` (version 13), and two hire-lifetime rows from `194f400f` (proposed version 14). The version-13 RAS stage, finalize, and transfer rows and the version-16 direct `CommitManifest` and version-15 `DeleteVolume` rows are stated in §5.5 and A.5. This mixed appendix is not a complete copy of any one node table. The node's full table version and row set enter the fingerprint; any row change requires explicit provenance.

At node PR #1708 commit `194f400f` (table version 14), the access fingerprint is `178321879494598796522930407062259511195`, the Gate-1 prefix tri-cost fingerprint is `173192047303472327226438143062800880918`, and the session-extension tri-cost fingerprint is `274138959066111445865989563955173012941`. Version 14 inherits version 13's rows and adds the Public Store hire and revoke rows below. The merged version-13 values above remain historical; they do not identify the proposed version-14 table.

At node PR #1708 commit `d60babed` (table version 15), the access fingerprint is `254168922177092646589884203421089811033`, the Gate-1 prefix tri-cost fingerprint is `13691609927054203654716208221041767188`, and the session-extension tri-cost fingerprint is `27621886539640747869149653721981724283`. Version 15 adds the `DeleteVolume` pin read below; the version-14 fingerprints remain historical.

At node PR #1708 commit `13de6a65` (table version 16), the access fingerprint is `186358957115666140290847872422173554630`, the Gate-1 prefix tri-cost fingerprint is `161809815397502607899677594500796353387`, and the session-extension tri-cost fingerprint is `257838973940684647473105831716727650200`. Version 16 adds the permanent artifact-volume index rows below; the version-15 fingerprints remain historical. These three fingerprints identify `13de6a65` only. After its devnet merge, node PR #1708 at `4294f24b` still declares `ACCESS_WEIGHT_TABLE_VERSION = 16`, but pins access fingerprint `91843525370429136499481528771868231467`, Gate-1 prefix tri-cost fingerprint `244711882150756174567760762831538975818`, and session-extension tri-cost fingerprint `93920369198531761669027632991107518713` (`access_weights.rs:4058` and `:4857`–`4858` at `4294f24b`).

**Table version numbers after 13 are PR-local.** The numbers 14, 15, and 16 in this CIP are the labels that node PR #1708 used at the named commits. Devnet uses the same numbers for different tables: devnet `abdfa6e7` declares its own version 15, which prices the native inventory revision and does not include the `d60babed` `DeleteVolume` pin read, and its version 14 is not the `194f400f` hire-lifetime table. At `4294f24b`, the #1708 version comment combines both meanings under 15. In this CIP, a table after version 13 is identified by its node commit, not by its number. The numbers will be replaced when the final table is selected (§11).


For `DeleteSecretVersion`, the args-only handler row is `fixed(5, 8)`. The common pause gate adds one committed read, giving the displayed phase-one `Rk(6)+Wk(8)` before dynamic evidence operations. `DeleteSecret` has a `fixed(2, 2)` handler row, displayed as `Rk(3)+Wk(2)` with pause, then reserves `V*(Rk(3)+Wk(6))` for the committed active versions and the exact indexed evidence operations for each override. Appendix A.2 imports these complete descriptive rows.

The artifact retirement row comes from node PR #1710; the three registration
rows and the hire and revoke rows come from node PR #1708. The other changed RAS rows are stated in §5.5 and A.5.
These changes are not part of the version-11 pin or either baseline fingerprint.

### 5.0 Primitives (landed)

| Protocol constant | Landed value | Source (`types/src/constants.rs` unless noted) |
| :---- | :---- | :---- |
| `W_READ` | `1` | `:150` (`ACCESS_WEIGHT_POINT_READ`) |
| `W_WRITE` | `1` | `:151` (`ACCESS_WEIGHT_WRITE`) |
| `W_HOST_CALL` | `1` | `:152` (`ACCESS_WEIGHT_HOST_CALL`) |
| `W_SCAN_BASE` | `5` | `:153` (`ACCESS_WEIGHT_SCAN_BASE`) |
| scan / copy / fanout items-per-unit | `8` | `:154` (`ACCESS_WEIGHT_ITEMS_PER_UNIT`) |
| `Scan(n)` | `5 + ceil(n/8)` | `access_weights.rs:17–21` |
| `Copy(n)` | `ceil(n/8)` | `access_weights.rs:22–23` |
| `Fanout(k)` | `ceil(k/4)` (`2/8`) | `access_weights.rs:27–28` |
| `W_NESTED_FRAME` | `75` | `:156` |
| `W_FIRST_NESTED_SETUP` | `40` | `:157` |
| `W_TOP_FRAME` | `99` | `:155` |
| signed envelope `E` | `5` (`4R + 1W`) | `:282`; `access_weights.rs:120–121` |

The access meter accumulates **structural** units. The gate charges each distinct cold `R`, distinct cold `W`, `NestedFrame`, and each aggregate `Scan`/`Copy`/`Fanout` preflight (its base and per-item units summed) in raw structural weight, with **no scalar applied at the gate** (`execution/src/host_read_gate.rs:406`). The `access_weight_scalar_ppm` scalar is applied **once, at a meter boundary — never per host operation or per allowance** (`types/src/constants.rs:286`; `execution/src/execution/transaction.rs:641`):

```text
charged_accesses(structural, ppm)        = ceil(structural * ppm / 1_000_000)    # structural -> charged, at settlement (constants.rs:290)
structural_access_capacity(charged, ppm) = floor(charged * 1_000_000 / ppm)      # charged -> structural, on signed-limit entry (constants.rs:353)
```

On entry, a signed transaction's charged `access_limit` is **floor-converted** to a structural meter capacity (`transaction.rs:653-659`); at settlement the structural total is **ceiling-converted** once into the charged `accesses_used` written to the receipt (`transaction.rs:1178-1183`). The per-block aggregate ceiling, the System-selection sub-lane, and each deferred child are separately owned meters that convert at their own boundaries. All arithmetic is unsigned 128-bit; `charged_accesses` maps structural zero to charged zero and any non-zero structural total to at least one charged unit. Implementations MUST NOT apply the scalar per primitive charge: a per-charge ceiling (e.g. `ceil(1·ppm) + ceil(1·ppm)`) disagrees with the boundary conversion `ceil(2·ppm)` and is a consensus divergence, which the implementation asserts against by name (`incorrectly_scaled_per_charge`, `execution/src/access_weights.rs:4870`). `access_weight_scalar_ppm = 1_000_000` is the identity (structural ≡ charged); the genesis live scalar is `1_350_000`, so a structural total `s` settles to `ceil(s · 1.35)` charged.

### 5.1 PVM HostApi

The host-call rows are unchanged in shape from the draft — each host call decomposes into `R`/`W`/`Scan`/`Copy`/`Fanout`/`NestedFrame` primitives plus its `W_HOST_CALL` crossing credit — but every numeric weight now uses the §5.0 landed primitives. The complete per-method decomposition is the executable `HOST_API_ACCESS_ROWS` table (`execution/src/access_weights.rs`), reproduced verbatim in **Appendix A.1** and bound by the executable-access fingerprint above. Representative rows:

| Host call | Class | Landed weight |
| :---- | :---- | :---- |
| `state_get` | R | `R = 1` |
| `state_set` / `state_delete` | W | `W = 1`; bytes remain cells |
| `state_scan_prefix` | Scan | `5 + ceil(min(limit, MAX_PREFIX_SCAN_LIMIT)/8)` |
| `emit_event` (to `k` sync subscribers) | R + Fanout + k×Nested + W | `R(EventIndex) + Fanout(k) + k*NestedFrame + W(out-keys)` |
| `call_actor` | R + Scan + Nested | point reads + `Scan(MAX_LIBS_PER_ACTOR)` + `NestedFrame` |
| `transfer_balance` | Rk + W | `Rk(2) + W(out-keys)`; self-transfer deduplicates |
| `fork_actor` (launch; CIP-30 inactive) | R + Copy + Scan + Nested + W | `Copy(n)` on the parent's committed `kv_count`, `n <= MAX_ACTOR_KV_COUNT` (`10_000`, `constants.rs:4833`) |
| `token_mint` / `token_burn` | Rk + W | `Rk(2) + W(out-keys)` |
| `token_transfer` | Rk + Nested + W | `Rk(5) + Cond[NestedFrame] + W(out-keys)`; live hook frames add `40` setup once + `75` per frame (`40 + 75·H`) |

Timer host-call declarations carry a per-fire access authority. The persisted `Timer` record carries `access_limit_per_fire: u64` adjacent to `gas_limit_per_fire`; this is a regenesis-only codec change — decoders MUST accept the current version exactly and MUST NOT infer a default for an older record. `schedule_timer` stores the classic default `DEFAULT_TIMER_ACCESS_PER_FIRE = 160` (`constants.rs:1841`); `schedule_timer_ex` may store a larger opt-in request (§5.3). The launch `fork_actor` row prices the physical `copy_actor_storage` implementation: `Copy(n)` is one aggregate out-access charge, `n` being the parent's committed `kv_count` read through the gate at preflight; CIP-30's O(1) storage-root fork is not consensus-live, and its activation MUST amend the fork row to `R(child collision) + W(root)`.

#### 5.1.1 `call_actor` system subroutes

These rows replace the ordinary `call_actor` actor/code/pin route when intercepted (EventSub `get_rank`/`get_orderbook`/`get_min_bid`/`update_bid`/`topup`, SKM `list_account_keys`/`initialize_paid_stream_config`/`register_content_keys`/`revoke_account_key`). SKM `register_content_keys` is `Rk(4 + 2N) + W(out-keys)` with `N <= CIP7_MAX_CONTENT_KEYS_PER_CALL = 256`. The user-signed System row set (one entry per wire variant) is reproduced in **Appendix A.2** (`USER_SYSTEM_ACCESS_ROWS`) with its CBSS, artifact, and hire substitutions; the exact source for each substitution is in the Appendix A recipe. This mixed table does not reproduce a single Node table version.

### 5.2 Scalar-safe allowances

Structural quantities — gate charges, the deferred-child and Timer tiers of §5.3, and the native reservation rows of §3/Appendix A — stay in structural units inside their meter; the `charged_accesses` / `structural_access_capacity` conversion of §5.0 is applied **once per meter boundary** (transaction settlement, the aggregate ceiling, each selection/child boundary), never per charge. Already-charged authorities — `USER_TX_ACCESS_LIMIT`, `ORDINARY_SYSTEM_TX_ACCESS_LIMIT`, `DEFAULT_ACCESS_LIMIT`, the lane budgets, and the aggregate ceiling — are stated in charged units and are floor-converted to a structural meter capacity on entry, not re-scaled. At the identity scalar structural and charged coincide; at the `1_350_000` genesis scalar a structural total `s` settles to `ceil(s · 1.35)` charged.

### 5.3 Natural-Timer and actor-minted deferred envelopes

The deferred-child tiers are the calibrated C0 constants:

| Tier | Structural value | Source (`types/src/constants.rs`) |
| :---- | ----: | :---- |
| Deferred pre-engine gate | `2` (`2R`) | `:1741` |
| General child execution allowance (`DEFERRED_EXECUTION_WORK_ACCESS_LIMIT`) | `160` | `:2898` |
| General child complete tier (`DEFERRED_ACCESS_LIMIT`) | `162` (`2 + 160`) | `:2899` |
| Required deferred-child reserve (`REQUIRED_DEFERRED_CHILD_ACCESSES`) | `162` (margin `0`) | `:2956`, `:2939` |
| Actor-Intent custody child (`ACTOR_INTENT_CHILD_ACCESS_LIMIT`) | `215` (`2` gate + `213` exec) | `:1797` (asserted `== 215` `:1848`) |
| Timer handler allowance (`TIMER_HANDLER_ACCESS_ALLOWANCE`) | `160` | `:1729` |
| Default Timer per-fire request (`DEFAULT_TIMER_ACCESS_PER_FIRE`) | `160` | `:1841` |
| Child-producing Timer cap (`TIMER_ACCESSES_LIMIT`) | `322` (`160 + 162`) | `:1826` |
| Actor-Intent Timer opt-in (`TIMER_ACTOR_INTENT_ACCESSES_LIMIT`) | `375` (`160 + 215`) | `:1810` |
| Max synchronous fires per tx (`MAX_SYNC_FIRE_PER_TX`) | `256` | `:2109` |
| Max synchronous fires per topic (`MAX_SYNC_FIRES_PER_TOPIC`) | `64` | `:2095` |
| Async fires per defer-tx (`ASYNC_FIRES_PER_DEFER_TX`) | `64` | `:2828` |

A minimal delivering deferred actor needs `10 + 99 + 1 = 110` structural accesses; a one-subscriber async event fire needs `4 + 40 + 75 + 1 = 120` (`constants.rs:2915–2917`). The `160`-unit child execution allowance therefore delivers work but cannot itself admit a nested actor call (`109 + 115 = 224 > 162`; D36, `constants.rs:1860–1875`). At the genesis scalar, `charged(215) = 291` and `charged(375) = 507`, so a `901`-unit Timer phase admits exactly one actor-Intent opt-in fire per block.

The actor-minted deferred System class (an outgoing message to the Job Dispatcher → `JobSubmit`; `intent_deposit`/`intent_withdraw`/`intent_broadcast[_ex]` → `IntentDeposit`/`IntentWithdraw`/`IntentBroadcast`) is classified from the child's provenance/sender read (`host_read_gate.rs:634-637,677`); it then reserves its **fixed** native row from the child's meter before the handler's dependent store work, in full, never refunded; if the reservation does not fit, execution returns `OutOfGas` (code `1005`) before any raw-store dispatch and commits no partial state. Conditional nested hook frames are charged live, only when they materialize. The hooked `IntentDeposit`/`IntentWithdraw` native structural models are `202`/`200` structural (`constants.rs:1857–1858`); the fixed reserved rows are `Rk(8)+Wk(4)` / `Rk(7)+Wk(3)` with the two frames live-only (`ACTOR_SYSTEM_ACCESS_ROWS`). `JobSubmit`'s metering is **not** a runner-registry scan: its actor-minted child row is `Rk(1)` sender ingress (`ACTOR_SYSTEM_ACCESS_ROWS`, `access_weights.rs:877-891`), while its selection cost is a fixed+variable `37 + 5N + 6M` row (`constants.rs:955-958`) reserved from the **System-selection sub-lane** — a second consensus meter disjoint from the transaction/child access meter, drained even if the transaction later fails. `N` is the bounded frontier-plus-tail candidate pool `min(eligible, MAX_SELECTION_CANDIDATES = 69)` and `M` the committee (`dispatcher.rs:1388`); the `MAX_REGISTERED_RUNNERS = 10_000` registration cap bounds the set but is not the metering row.

### 5.4 Execution envelope and deferred work

The `ExecuteActor`, `DeployActor`, and deferred event-fire rows keep their draft shapes with the §5.0 landed weights (top frame `99`, nested `40 + 75`, scan `5 + ceil(n/8)`). Every library-pin scan in `call_actor`, `fork_actor`, `ExecuteActor`, and `DeployActor` MUST charge the fixed worst-case `Scan(MAX_LIBS_PER_ACTOR)` bound because there is no O(1) pin-count primitive. The engine-minted deferred event-fire batch, `unsubscribe_event`/`force_unsubscribe_event` bookkeeping-topic re-fire, and `Copy(n)` fork rule keep their draft semantics with the landed slopes (COW-3527 items 4–7 row fixes).

### 5.5 Signed RAS / CBFS rows

Signed RAS is `GasLane::System`, so its per-transaction ceiling is the ordinary-System `1_500`. The executable RAS sub-variant schedule is in node `execution/src/access_weights.rs`. The client table in `docs/access/CLIENT-LIMITS.md` first gives the corrected direct `CommitManifest` fixed term at node `bb45363a` ("docs: correct manifest native and signed access limits"); at `13de6a65` that table still states `57+T+K+2X_m` native. §5.5 uses the executable row. Appendix A.5 lists selected amended rows; two RAS rows bind the ceiling and CommitManifest staging cap (`types/src/constants.rs:846–867`):

- **`SettlePorChallenge` third-miss initial row** = `1_106` native / `1_111` signed structural (`+2N` recipient-distribution phase), where `1_106 = 52R + 20W + 2·Scan(4_096)` and `Scan(4_096) = 5 + ceil(4_096/8) = 517`. This is the largest mandatory signed ordinary-System initial row (`MAX_SIGNED_ORDINARY_SYSTEM_INITIAL_ROW_STRUCTURAL_ACCESSES = 1_111`, `constants.rs:866`), and it is exactly what fixes `MAX_ACCESS_WEIGHT_SCALAR_PPM` (§8): at `1.35×` it charges exactly `1_500`. At launch `cip31.cbfs.por_slashing_enabled = 0`, so settlement is alarm-only; the `+2N` distribution phase is COW-3672 staged/cursor work. `SubmitDrainAudit` (`1,067+2N` native) and `ReportUnreachableRelay` (`557+2N`) are the sibling redistribution rows.
- **`CommitManifestFinalize`** funding envelope (COW-3855, #1532, node `5cf8ce13`): the finalize predicate `65 + 3H + 2D + 2A + 2R + 2·ceil(A/512) + 2·ceil(R/512) <= 1_111` (structural, evaluated at the max governed scalar) sets the hard public staged-chunk cap `MAX_COMMIT_STAGE_CHUNKS = 306`: with the maximum `D = 64` relay deltas and no shards, `306` chunks require `1_111` structural accesses (fit) and `307` require `1_114` (fail). `CommitManifestStage` remains `12R/5W` on its maximum path; finalize fails closed with the existing `QuotaExceeded` receipt. The former `307`, `948`, and `1_024` caps are superseded.
- **Finalize variables.** `H` is the committed staged-chunk count, `D` is the header's relay-delta count, and `A` and `R` are the added and removed shard-reference counts decoded from the chunks. Each `ceil(…/512)` term counts event pages for its shard references. The 306-chunk cap is a maximum only when the other variable terms fit: at `H=306`, `D=64`, `A=R=0`, the row uses all `1_111` structural accesses, so any positive shard term rejects. At `5cf8ce13` and `13de6a65`, node priced these committed quantities before mutation with the per-shard weights above. The devnet merge into node PR #1708 changed those weights; see the repricing bullet below.
- **Cap provenance.** The previous `60 + X_f` figure in this CIP was stale before the Public-volume pin. Node PR #1710 at version 12 (`21aa3d4f`) already asserted `64 + X_f` and a cap of 306. Version 13 adds one pin read to make `65 + X_f`; the five-access difference from this CIP's old figure is four earlier node accesses plus that one pin read. The cap did not first change because of the version-13 pin.
- **Public artifact pin read (table 13).** Direct `CommitManifest` and `CommitManifestFinalize` each add one fixed read for the active Public-artifact count; both reject whenever the volume's CIP-33 pin count is nonzero, including a staged finalize that proposes the current root. `TransferVolume` changes from `15R/10W` to `16R/10W`. Stage admission remains `12R/5W` on its maximum path. The direct `CommitManifest` reservation at node `13de6a65` is `Rk(41+T+X_m)+Wk(11+X_m)` before committed pending-epoch reads, where `T=1` for a nonempty token and `0` otherwise, and `X_m=D+A+R+ceil(A/512)+ceil(R/512)` for input-visible relay deltas and shard references. Settlement reserves another `K` point reads for the committed pending-epoch queue. The complete signed structural row is `57+T+K+2X_m`. The `CLIENT-LIMITS.md` direct-commit row at `bb45363a` gives the same `52+T+K+2X_m` native row and `57+T+K+2X_m` signed identity row. The complete per-operation row source is node `execution/src/access_weights.rs` at `13de6a65`.
- **Direct-commit bound.** Neither `K` nor `X_m` has a protocol cap. The direct row is priced before any write: at the `1,500` ordinary-System limit, the first failing signed row at `13de6a65` is `K+2X_m = 1,444-T` (node `CLIENT-LIMITS.md` at `bb45363a`). The repricing bullet below gives the bound at the current node PR #1708 head. Such a commit fails closed with access exhaustion and changes no state (node test `pending_epoch_queue_k2_exhausts_before_deposit_or_manifest_settlement_write`). Clients submit a manifest delta that does not fit through `CommitManifestStage` and `CommitManifestFinalize`, which the 306-chunk cap bounds.
- **Hire-lifetime pin (table 14, retained in 16).** CIP-33 §2.1.2 requires a Public volume reference for each non-`Revoked` hire pinned to a Store artifact. Node PR #1708 commit `194f400f` adds a count update at Shared hire creation and irrevocable revocation. Its `TradingPostHire` phase-one row is `Rk(12)+Wk(6)` plus the exact selected split `P*(Rk(2)+Wk(1))`; `TradingPostRevokeHire` is `Rk(9)+Wk(3)`. Table 13 has only the Active-artifact count. Forked spawning remains outside the implemented Node path.
- **Pinned-volume deletion (proposed table 15).** Node PR #1708 commit `d60babed` adds one fixed read to `DeleteVolume`, from `Rk(9)+Wk(8)` to `Rk(10)+Wk(8)`, to reject deletion while a Public Store pin remains. The complete signed identity row is 23 structural accesses, including the five-access envelope. `CreateVolume` already rejects every occupied deterministic `volume_id`; the GC commitment tombstone preserves that occupancy. No new creation row is attributed to this amendment.
- **Devnet-merge repricing (node PR #1708 at `4294f24b`).** The shard terms above describe `5cf8ce13` and `13de6a65` only. Node PR #1708 at `4294f24b` merged devnet, including node #1774 (native shard inventory), and repriced them. An added shard is `2R+2W` (`RAS_COMMIT_MANIFEST_FINALIZE_PER_ADDED_SHARD_*`). A removed shard reserves the non-tail swap-delete maximum `4R+4W` (`RAS_COMMIT_MANIFEST_FINALIZE_PER_REMOVED_SHARD_*`). When `A != R`, the PoR directory revision adds `2R+1W` (`RAS_POR_DIRECTORY_REVISION_*`). Every direct commit and finalize adds the native inventory revision `1R+1W` (`RAS_NATIVE_INVENTORY_REVISION_*`). A direct commit or stage with a workload-effect binding adds one read (`RAS_OWNER_EFFECT_WORKLOAD_POINT_READS`), and finalize reserves that read in its fixed row. The finalize fixed row is still `46R/14W`, so the fixed signed term is still `65`; at that head, `X_f = 3H+2D+4A+8R+2·ceil(A/512)+2·ceil(R/512)+3·[A≠R]`. The 306-chunk cap at `D=64`, `A=R=0` is unchanged. The direct `CommitManifest` signed row at that head is `56+O+T+K+2D+4A+8R+2·ceil(A/512)+2·ceil(R/512)+3·[A≠R]`, where `O=1` for a workload-effect binding. With one relay delta, the node test `user_system_scaling_rows_have_nonzero_cardinality_constructions` pins native `Rk(41)+Wk(12)` with no shards and `Rk(49)+Wk(20)` with one added and one removed shard. At the `1,500` identity limit, the first failing direct row is where the variable terms after `56+O+T` sum to `1,445-O-T`. `CLIENT-LIMITS.md` at `4294f24b` still states the `bb45363a` direct and finalize formulas. The final table MUST be measured again (§11).

## 6. De-conflation

The `Storage KV Read` and `Storage KV Write` rows leave CIP-3 §2.2.1's cycle schedule. The legacy 100-cycle read and 200-cycle write terms are removed from `cycles`. Read-side **bytes** are priced in cycles by measured bandwidth — `read_cycles(bytes) = 7 * ceil(bytes / 1024)` (`STORAGE_READ_CYCLES_PER_KIB = 7`, `execution/src/gas.rs:413,417`) — tracked as `storage_read_bytes` in `GasCategory::Storage`. `state_get` charges the returned **value** bytes (`pvm_host.rs:5024-5034`); `state_scan_prefix` charges the sum of **key and value** bytes across the returned rows (`pvm_host.rs:5143-5155`). A read persists nothing, so it consumes no cells, and the byte charge does not enter the cold/warm access set (COW-3633). Persisted write bytes continue to consume cells. (Sources: CIP-3 §2.2.1–2.2.2; Technical Whitepaper §§17.3–17.4; `execution/src/gas.rs`.)

After the change, `cycles` measures pure compute plus read-side byte bandwidth, `accesses` measures store I/O structure, and `cells` measures persistent bytes. `memory_cells` MUST remain a hard per-execution liveness bound and MUST NOT enter fee settlement.

## 7. Transaction fields and wire encoding

`Transaction` version 2 adds four fields in these exact canonical wire positions:

| Field | Type and encoding | Position |
| :---- | :---- | :---- |
| `access_limit` | `u64`, minimal `UInt` varint | immediately after `cells_limit` |
| `max_fee_per_access` | `u128`, minimal `UInt<u128>` varint | immediately after `max_fee_per_cell` |
| `max_priority_fee_per_access` | `u128`, minimal `UInt<u128>` varint | immediately after `max_priority_fee_per_cell` |
| `origin_remaining_access` | `Option<u64>`, encoded like its siblings | immediately after `origin_remaining_cells` |

The leading `CURRENT_TX_VERSION` is `2`, and the four fields participate in `Write`, `Read`, `EncodeSize`, signing preimages, signatures, and strict decode in the frozen order above. `CURRENT_TX_VERSION` and `DEFAULT_ACCESS_LIMIT` are defined in the pinned `cowboy-protocol-codec` dependency (`rev 0d5e8b69`, `types/Cargo.toml:55`) and re-exported at `types/src/lib.rs:82,89`; `origin_remaining_access` is carried on the receipt/response types (`rpc/src/responses.rs:156`). (Sources: Technical Whitepaper §§2.1, 2.5.)

Deferred transactions MUST carry access parity: `origin_remaining_access` is optional exactly like `origin_remaining_cycles`/`origin_remaining_cells`, and a child MUST satisfy `access_limit <= origin_remaining_access`. Like its siblings, a deferred transaction MUST carry `max_fee_per_access == 0` and `max_priority_fee_per_access == 0`.

The existing `Transaction.access_list` (EIP-2930 scheduling/prefetch) and dashboard/CBFS `accessMode` (a storage read-cap) are unrelated and are not reused by this meter.

## 8. Basefee, fees, access lanes, and governance

`access_basefee` is denominated in attoCBY and uses the same independent EIP-1559 update family as the other meters:

```text
basefee_{a,i+1} = max(MIN_BASEFEE,
  basefee_{a,i} * (1 + clamp((U_a - T_a) / (T_a * 96), -1/96, +1/96)))
```

`U_a = successful_bucket + min(failed_bucket, T_a)` over the ordinary transactions in the block transaction list (`storage/src/speculative.rs:208-219`). The **successful (uncapped) bucket** holds both `Success` and `OutOfGas` transactions; **only** an `ExecutionError` transaction goes to the **failed (capped) bucket** — an OutOfGas-heavy block is therefore demand-uncapped. Each bucket adds the transaction's own `accesses_used` **and** the System-selection access it caused, credited to the same bucket as its parent status (`speculative.rs:213-215,4408-4413`). Natural-Timer transactions execute in a fresh end-of-block phase and are excluded from `U_a` (and `U_c`); the dedicated timer basefee remains cycle-priced, and timer accesses pay the live access price without contributing to a later block's `U_a`. The **payerless** per-block allowances (block-start and epoch sweep) add charged usage to System-lane capacity reporting but are excluded from payer-congestion demand `U_a` (D19). The inherited constants are `alpha = 96`, a `±1/96` per-block clamp, and `MIN_BASEFEE = 10_000`.

The landed capacity and genesis values:

| Parameter | Landed value | Source (`types/src/constants.rs` unless noted) |
| :---- | ----: | :---- |
| `BLOCK_ACCESS_TARGET` (`T_a`) | `2_048` | `:136` |
| `LANE_USER_ACCESS` | `2_051` | `:408` |
| `LANE_RUNNER_ACCESS` | `1_144` | `:585` |
| `LANE_TIMER_ACCESS` | `901` | `:595` |
| `LANE_SYSTEM_ACCESS` | `4_096` (`2 * T_a`) | `:607` |
| `IN_BLOCK_SELECTION_ACCESS_LIMIT` (System selection sub-lane) | `2_596` | `:820` |
| `SYSTEM_TX_ACCESS_RESERVE` (ordinary System reserve) | `1_500` | `:826` |
| `USER_TX_ACCESS_LIMIT` (charged, block validity) | `1_024` | `:910` |
| `RUNNER_TX_ACCESS_LIMIT` | `1_144` (`= LANE_RUNNER_ACCESS`) | `:920` |
| `TIMER_TX_ACCESS_LIMIT` | `901` | `:924` |
| `RUNNER_CALLBACK_HANDLER_ACCESS_ALLOWANCE` (resume own-work) | `152` | `:1744` |
| `RUNNER_CALLBACK_ACCESSES_LIMIT` (callback structural tier) | `314` (`= 152 + REQUIRED_DEFERRED_CHILD_ACCESSES 162`) | `:1753` |
| `ORDINARY_SYSTEM_TX_ACCESS_LIMIT` (charged) | `1_500` | `:874` (asserted `:1034`) |
| `PROTOCOL_TX_ACCESS_LIMIT` (outer envelope) | `4_096` | `:991` |
| `BLOCK_AGGREGATE_ACCESS_CEILING` (block validity) | `4_096` | `:647` (asserted `:775`, `:1025`) |
| Largest mandatory signed ordinary-System initial row | `1_111` | `:866` |
| `BLOCK_START_ACCESS_BUDGET` (A4 payerless) | `1_000` | `:417` |
| `EPOCH_SWEEP_UNITS_PER_BLOCK` (A5 payerless) | `1_000` | `:5065` |
| `OUTSIDE_SHARED_POOL_ACCESS_UNITS` | `730` | `:637` |
| `LEADER_ACCESS_CAPACITY_UNITS` | `5_826` | `:621` |
| `INITIAL_ACCESS_BASEFEE` | `100_000` attoCBY/access | `:1240` |
| `MIN_ACCESS_WEIGHT_SCALAR_PPM` | `1_000_000` | `:2973` |
| `MAX_ACCESS_WEIGHT_SCALAR_PPM` | `1_350_135` | `:2989` (asserted `:3405`) |
| genesis live scalar (`COW3659_ACTIVATION...`) | `1_350_000` | `:273` |
| `MAX_GOVERNED_MIN_BASEFEE` | `1_346_204_307_369` | `:3165` (asserted `:3181`) |

**Per-class ceilings are block validity (D40 / D37 / Decision 21).** Signed User transactions are capped at `USER_TX_ACCESS_LIMIT = 1_024` charged accesses (two maximal User transactions fit the `2_051` User lane with `3` charged left; at the genesis scalar `1_024` charged buys `758` structural). `DEFAULT_ACCESS_LIMIT` remains `512` (the SDK/CLI/RPC stamp; `docs/access/CLIENT-LIMITS.md:6,111`), so an estimate-then-sign client may fill up to `1_024` while a static stamp above `512` is deliberate. Ordinary native-System transactions are capped at `ORDINARY_SYSTEM_TX_ACCESS_LIMIT = 1_500` — which is also the entire ordinary-System block reserve — and the independent selection side-channel owns the other `2_596` of the `4_096` System lane. `JobSubmit`/`JobResultSubmit`/`JobResultCommit`/`JobCancel` use the `1_144`-unit Runner lane; zero-origin deferred work uses the `901`-unit Timer lane. No client-signed instruction is assessed against `PROTOCOL_TX_ACCESS_LIMIT = 4_096`; that is the outer envelope and full System-lane total.

**Aggregate ceiling (C1, COW-3632).** Block validation MUST enforce `BLOCK_AGGREGATE_ACCESS_CEILING = 4_096` charged accesses across the whole block, derived as `LEADER_ACCESS_CAPACITY_UNITS(5_826) − EPOCH_SWEEP_UNITS_PER_BLOCK(1_000) − OUTSIDE_SHARED_POOL_ACCESS_UNITS(730)` (`constants.rs:647`). It preserves either full System saturation or full User+Runner+Timer co-saturation while rejecting their unsafe `8_192` co-saturation.

**Two-results / two-users lane derivation and the runner-callback tier (COW-3896, decision 2d).** The User and Runner payer-lane budgets are each sized to hold two reference transactions, and the three payer lanes still sum to `BLOCK_AGGREGATE_ACCESS_CEILING` (const-asserted in `constants.rs`: `2_051 + 1_144 + 901 = 4_096`). The User lane holds two **maximal** `1_024`-charged signed transactions with a three-unit tail (`1_024` is `USER_TX_ACCESS_LIMIT`; `2 × 1_024 = 2_048 ≤ 2_051`; `LANE_USER_ACCESS`, `:408`). The Runner lane is sized to hold two single-runner, no-delegation `JobResultSubmit` **reference** suggestions (`2 × 572 = 1_144`; `LANE_RUNNER_ACCESS`, `:585`); the `572` is that COW-3875 estimate fixture's suggestion, **not** a per-transaction Runner cap — `RUNNER_TX_ACCESS_LIMIT = LANE_RUNNER_ACCESS = 1_144` (`:920`), so one Runner transaction may claim the whole lane, and multi-runner, delegation, and offender work exceeds `572`. A `JobResultSubmit` that produces a runner callback reserves the **structural callback tier** `RUNNER_CALLBACK_ACCESSES_LIMIT = RUNNER_CALLBACK_HANDLER_ACCESS_ALLOWANCE (152) + REQUIRED_DEFERRED_CHILD_ACCESSES (162) = 314` (`:1753`): the `152`-unit resume own-work allowance is the measured warm-resume ceiling `ceil(173 / 1.35) = 129` structural plus `23` policy headroom (~17.8%) (`:1744`), and the `162`-unit child reserve is D36's exact one-child threshold — the callback tier funds **exactly one** nested `submit_job` child per callback, never a second. At the live `1.35×` scalar the `314`-unit tier charges `424`, and with the result's own `71`-structural work (charged `96`) the estimate-then-sign suggestion is `ceil((96 + 424) × 1.1) = 572` live and `424` at the identity scalar.

**Lane fit is not aggregate-block admission (qualification).** These budgets size each lane in isolation; they do **not** promise that two `JobResultSubmit` suggestions and two maximal User stamps are all selected into one block. Under the current proposer reservations — a charged block-start seed (`BLOCK_START_ACCESS_BUDGET = 1_000`, `1_350` at the live scalar) reserved ahead of payer rows, plus the complete Timer-lane hold — the payer remainder does not fit that full set; the exact selected/deferred counts at both scalars are pinned in node truth-table tests. A planned follow-up (COW-3898) is expected to reduce the Timer reservation to the parent-state due-timer bound, changing that aggregate picture but not these lane budgets.

**Lane admission and the tail guard.** For each transaction in canonical order, block validation runs a class-specific pipeline (`storage/src/speculative.rs`), all of whose rejections are **block validity**, not proposer preference:

1. **Class ceiling** (`speculative.rs:3662-3699`; branch on `is_engine_deferred = non_system_deferred_origin(tx).is_some()`). An **engine-minted (nonzero-origin) deferred** envelope is funded by authenticated origin authority, not by the child instruction's lane: its `access_limit` is converted once — `charged_accesses(access_limit, ppm).unwrap_or(u64::MAX)` — and compared against `PROTOCOL_TX_ACCESS_LIMIT = 4_096`, regardless of lane. **Every other** transaction compares its raw `access_limit` against `tx_access_limit_for_lane(lane)` — signed requests are charged (User `1_024`, Runner `1_144`, Timer `901`, ordinary System `1_500`); a zero-origin Timer request retains its raw structural Timer ceiling. Either excess yields the structured `1900` (E1900) no-execution result and continues the block loop. The split is by **origin, not by signing**: a transaction carrying an `origin_tx_hash` is deferred and unsigned — `is_deferred() = origin_tx_hash.is_some()`, and `verify()` accepts it only with `signature == EthSignature::ZERO` (`cowboy-protocol-codec` `transaction.rs:201-202,451-464` @ rev `0d5e8b69`; both signed constructors set `origin_tx_hash: None`) — so a validly signed envelope never carries an origin. Thus a **nonzero** origin is always engine-minted deferred (the `4_096` protocol branch), while a **zero-origin** System/Timer deferred envelope (native/Timer-minted) takes the `tx_access_limit_for_lane` structural-class arm.
2. **Live cap.** `execution_access_cap` is the charged conversion of `access_limit` for deferred transactions and the raw `access_limit` for signed ones. `live_cap = min(lane_remaining, sub_lane_remaining_or_u64::MAX, aggregate_remaining.saturating_sub(held_finalization_charged))` (`speculative.rs:784-797`): the per-block aggregate ceiling and the reserved finalization tail constrain execution, not only the lane.
3. **Ordered tail guard.** A signed transaction with `0 < live_cap < charged(E)` (charged `E = 5` → `7` at the genesis scalar) makes the **block invalid** (`speculative.rs:3753-3760`). Every deferred transaction must fit its **complete** effective reservation `min(execution_access_cap, full_lane_budget, 1_500 if System else u64::MAX)` — if it exceeds `live_cap` the block is invalid, **including when `live_cap == 0`** (`:3768-3781`); it is not admitted merely because a few charged units remain, and is not partially run against a short remainder. **Only after** both checks pass does a `live_cap == 0` take the E1900 no-execution path (`:3783+`).
4. **Execution cap.** Otherwise the transaction runs capped at `min(execution_access_cap, live_cap)` (`:4190`), the later authorization checks are preserved, and the accumulator increases by actual `accesses_used`.

A transaction that fails after committed-store reads is charged to its lane for the reads it made; only a transaction that consumed nothing anywhere is a pre-execution failure outside lane accounting. Nothing is credited back, and unused capacity MUST NOT move between lanes.

The access contribution to the transaction fee:

```text
tip_a = min(max_priority_fee_per_access, max_fee_per_access - access_basefee)
fee_a = accesses_used * (access_basefee + tip_a)
```

Unused access limit MUST be refunded at `max_fee_per_access`, and 100% of the access-basefee portion MUST be burned.

**Governance.** `UpdateBasefeeConfig` and `SubmitProposal` MUST each carry `block_access_target` and `access_weight_scalar_ppm`, recorded under `system:basefee_config` as Tier-0 parameters. Protocol validation MUST bound the governed scalar to `[MIN_ACCESS_WEIGHT_SCALAR_PPM = 1_000_000, MAX_ACCESS_WEIGHT_SCALAR_PPM = 1_350_135]`. The maximum is derived from the mandatory `1_111`-unit ordinary-System initial row inside its `1_500` reserve — `floor(1_500 * 1_000_000 / 1_111) = 1_350_135` — at which the row charges exactly `1_500`; at `1_350_136` it charges `1_501`. The `1_350_000` live scalar leaves `135` ppm of integer-rounding headroom. Governance MAY change the target and scalar within this bound but MUST NOT change an individual table row without the coordinated CIP-3 §4 release. The operational commitment (COW-3774) is that no post-genesis retune is performed until the deferred-child scalar-straddle fix lands. (Sources: `constants.rs:2989`, `:2973`, `:273`; `docs/access/CLIENT-LIMITS.md`.)

**Fee-policy hardening (D21 / D24).** The scalar and tri-basefee are loaded from `BasefeeConfig`/`system:basefee_config` at startup, submission, estimation, finalization, and proposal selection. The sole governance persistence path validates before writing; node-local consumers use `try_load_from_storage` and refuse an invalid committed config without panicking — engine startup returns that refusal to `main`, which logs and exits nonzero. RPC surfaces that depend on committed fee state (`/basefee`, `/governance/params`, `/estimate_gas`, submission, node-authored runner transactions) fail closed with HTTP 500 when the committed tri-basefee or `BasefeeConfig` is malformed; clients MUST treat this as *fee policy unavailable / do not sign* and MUST NOT fabricate a default fee policy. (Sources: `docs/access/design/DESIGN-cow3648-signed-envelope.md`; `docs/access/CLIENT-LIMITS.md` "Fee-policy failure contract".)

The governance minimum-basefee ceiling `MAX_GOVERNED_MIN_BASEFEE = 1_346_204_307_369` per Cycle/Cell/charged-Access unit is derived so the complete four-transaction Tier-0 recovery path stays payable by the largest schedule-free genesis account: `floor((GENESIS_MAX_LIQUID_ACCOUNT_WEI − MIN_STAKE_CBY_WEI) / GOVERNANCE_RECOVERY_TOTAL_AUTHORITY)`, with total recovery authority `99_086` (`constants.rs:3144,3180`). A split-Treasury genesis weakens the account-level guarantee, so clients MUST NOT infer that every valid genesis funds one account to the ceiling.

## 9. Exhaustion and receipts

`ExecutionStatus::OutOfGas` carries `accesses_used`. Access exhaustion uses that existing status and does not introduce a new structured-error code, because structured-error bytes participate in `receipt_root`.

- A transaction whose `max_fee_per_access` is below the block's `access_basefee` fails with the existing `BASEFEE_TOO_LOW` structured error code **`1008`** (`execution/src/structured_error_map.rs:181`). Its context appends `max_fee_per_access` and `access_basefee` after `cell_basefee`, in that order.
- A deferred child that cannot reserve its complete structural tier fails with `DeferredChildOutOfAccesses` code **`1005`** (E1005, `structured_error_map.rs:161`), context `used`, `limit`, `child_kind`, `child_access_tier_structural`, rolling back the requesting transaction atomically (D41).

Transaction receipts add `accesses_used`, `remaining_access`, and `cumulative_accesses_used` adjacent to their cycle/cell siblings in canonical `Write`, `Read`, `EncodeSize`, and RLP encodings, and receipt-root computation commits the resulting bytes (`storage/src/types.rs:1085,1093,1106` with `Write` `:1223,1234,1246`, `Read` `:1268,1290,1352`, `EncodeSize` `:1423,1432,1445`; RPC DTOs `rpc/src/responses.rs:199,211,214`). The D37 receipt-leaf semantic property digest binds `tx_hash` and `remaining_access` so a divergent `remaining_access` cannot pass unnoticed.

**Nested exhaustion (B7, COW-3557).** An omitted nested-call access allowance follows the EIP-150 caller-reserve shape: `reserve = ceil(remaining / 64)`, and the nested call receives `remaining − reserve` (the all-but-1/64 rule), so a caller always retains `1/64` of its remaining access authority (`nested_call_access_allowance`, `execution/src/pvm_host.rs:1823`). Exhaustion of a smaller nested cap is an isolated subscriber/callee failure and cannot suppress later work while the live lane still has headroom; provenance is carried on the receipt.

**Estimate-then-sign (D22 / D24).** `POST /estimate_gas` runs the same child-affordability formulas, ordering, and partial-prefix policy as execution and returns `access_limit_suggested` (`rpc/src/handlers/chain.rs:992`) so a client can estimate → read `access_limit_suggested` → sign. Static daemons that cannot round-trip an estimate MUST sign explicit, bounds-validated limits; a fee-policy-unavailable response (§8) means do not sign.

## 10. Determinism and safety

Every charge MUST derive only from committed or input-visible quantities and MUST NOT depend on observed latency, hardware cache warmth, validator model, wall-clock time, or timeouts. Charging MUST occur before dispatch; exhaustion MUST fail closed into the committed `OutOfGas` outcome, while a wall-clock reap remains a non-committing infrastructure backstop. In-process and zygote execution MUST produce byte-identical committed `accesses_used`, receipt, and state-root surfaces for the same transaction (§12). Access accounting MUST use the same transaction snapshot boundary as state and messages. The settlement inequality holds by **denomination** (§5.0): structural meter usage never exceeds the structural meter capacity, and the charged `accesses_used` written to the receipt never exceeds the corresponding charged effective authority. The direct wire-field inequality `accesses_used <= access_limit` applies to **signed** transactions, whose `access_limit` is charged. An engine-minted **deferred** transaction instead carries a *structural* `access_limit` (`constants.rs:920-922`; `transaction.rs:2664-2674`), so a fully consumed `162`-structural child reports `219` charged accesses at the genesis scalar — greater than its wire `162`, yet within its structural capacity. The unused limit is refunded per §8.

## 11. Activation

The meter is genesis-active from block 0 of the launch regenesis; there is no live-chain activation height. `ACCESS_WEIGHT_TABLE_VERSION = 4` documents the historical baseline schedule. Node #1710 merged table version 13 with Public artifact validation and an Active-artifact volume pin. Node #1708 commit `194f400f` records table version 14 with the Shared-hire lifetime correction (§5.5); commit `d60babed` records table version 15 with the pinned-volume deletion guard; commit `13de6a65` records table version 16 with the permanent `volume_id` reverse index. **Version 16 and its §5 fingerprints identify that measured source (`13de6a65`), not a final launch table.** The table numbers after 13 are PR-local, and the devnet merge at `4294f24b` already changed the version-16 rows and fingerprints (§5, §5.5). The existing occupied-ID rule and GC commitment tombstone block deterministic ID reuse without a new pin-specific creation row. CIP-33 §2.1.2 also leaves rent-driven garbage collection of a pinned volume and perpetual OneShot availability unresolved; any selected consensus guard would require measured rows. The final table version, rows, and fingerprints MUST be measured against the complete implementation and selected together before the next coordinated regenesis. Validators on that network MUST use that selected table from block 0. Old transactions cannot be replayed under the new table, including Private registrations. A hot-merge is out of policy (node #1457, #1533, #1710).

The D50 dormant continuation and deferred gates are flipped to height `0` (regenesis-active) at this landing (`types/src/constants.rs`):

- `DEFERRED_RECEIPT_AUTH_REMOVAL_ACTIVATION_HEIGHT = 0` (`:4299`)
- `JOB_CALLBACK_ACTOR_BINDING_ACTIVATION_HEIGHT = 0` (`:4667`)
- `RAS_GC_IS_TERMINAL_ACTIVATION_HEIGHT = 0` (`:2706`)
- `SDK_CONTINUATION_PERMISSION_ACTIVATION_HEIGHT = 0` (`:4481`)
- `SDK_CONTINUATION_DECODE_REJECTION_ACTIVATION_HEIGHT = 0` (`:4595`)
- `SDK_CONTINUATION_RESUME_PERMISSION_ACTIVATION_HEIGHT = 0` (`:4642`)

The cross-engine PVM zygote gate remains dormant (`PVM_ZYGOTE_ACTIVATION_HEIGHT = u64::MAX`, `:3881`): the in-process engine is the live consensus engine, while the zygote path is the cross-engine conformance oracle (§12). Other dormant activation constants are unchanged by this line.

## 12. Conformance vectors: `gas-vectors.v2`

`cip-39-gas-vectors-v2.json` (`execution/tests/fixtures/cip39/cip-39-gas-vectors-v2.json`) has schema `cowboy.cbqs.gas-vectors.v2`. It removes `state_read_cycles`/`state_write_cycles`, adds one `R` per charged read and one `W` per charged write at the identity scalar, and records `accesses = charged_reads + charged_writes`. The normative SHA-256 of the v2 artifact is:

```text
e097e2a3b49af0b6bd0b949853c52fd43757c8a6d44b2c9387926c23f023907a
```

(`docs/access/integration/MERGE-LOG-integ.md:188`.)

The CBQS-v2 family (wire tag `218` v2) is an executing, state-writing, fee-charging surface priced in both cycles and access. Its eight decoded rows carry structural (engine-minted, native) `[9, 5, 19, 3, 13, 12, 9, 11]` and, as a complete signed transaction (native `+ E=5` envelope) at the `1.35×` genesis scalar, charged `[19, 14, 33, 11, 25, 23, 19, 22]` (RegisterProvider `6R/3W`, UpdateProvider `4R/1W`, CreateStream `11R/8W`, UpdateStream `2R/1W`, CloseStream `7R/6W`, TopUpAccount `7R/5W`, SettleAccount `5R/4W`, WithdrawAccount `6R/5W`; `execution/src/access_weights.rs:1936`, tabulated in **Appendix A.4**); the top-level CBQS classifier stays `(0,0)`, and the reservation is charged in full before the first admission check and never refunded. Devnet sync-3 also removed six CIP-16 variants (`SubmitRegistrySettlementConfigProposal`, `RegisterTldLabel`, `RenewTldLabel`, `TransferTldLabel`, `SetTldActor`, `UpdateRegistrySettlementConfig`), shrinking the wire corpus `204 → 198`.

Cross-engine conformance runs the in-process engine against the zygote engine and MUST produce identical `accesses_used`, receipt, and state root, including alias-key, nested-revert, and non-identity-scalar cases. The consensus golden corpus is **55** re-blessed rows (21 added, 34 modified; `docs/access/integration/MERGE-LOG-integ.md:190`); devnet sync-3 moved zero `.golden` files and only re-derived the two `access_weights.rs` fingerprint constants.

## 13. Security considerations

The access table and its version are consensus-critical. Any individual weight change requires a coordinated release under CIP-3 §4, and all clients MUST implement the exact same table, identified by the applicable version's fingerprints in §5. Only `block_access_target` and `access_weight_scalar_ppm` are live-governable, within `[1_000_000, 1_350_135]` ppm for the scalar.

Missing a store path permits unmetered I/O denial of service; charging after dispatch makes the halt depend on wall-clock work already consumed; non-canonical key aliasing makes cold/warm results diverge; an unbounded native reservation or refundable native charge creates a content-dependent refund oracle. Implementations MUST therefore enforce a non-bypassable gate, charge before dispatch, reserve native and deferred rows in full without refund, and conformance-test alias keys, nested-tree sharing, speed invariance, cross-engine parity, and write-flush cadence. (Sources: CIP-3 §2.2.1.1; Technical Whitepaper §§Determinism, 12.1, 13.1.)

## Implementation map

The full implementation and integration record lives at `docs/access/` on node (`README.md`, the normative `CLIENT-LIMITS.md`, the 33 `design/DESIGN-*.md` records, and `integration/MERGE-LOG-integ.md`). Landing bundle: node #1457 (47 constituent PRs; history tag `access-line-history`); devnet sync-3: #1533.

| Workstream | Ticket(s) | PR(s) |
| :---- | :---- | :---- |
| Native user-signed System family (A1) | COW-3631 | #1479 |
| Per-transaction signed envelope `E=5` (A3) | COW-3648 | #1476 |
| Zero-row guest host crossings, `W_HOST_CALL` (A2) | COW-3647 | #1483 |
| Top-level execution frame (A6) | COW-3657, COW-3659 | — |
| Read-side bytes in cycles | COW-3633 | #1481 |
| Nested access sub-limits, sync-fire cap, provenance (B7) | COW-3557 | #1482, #1495 |
| Final access-weight calibration (C0) | COW-3659 | #1500 |
| Per-class per-tx access ceilings as block validity (D37/D40) | COW-3730 | #1503 |
| Aggregate per-block access ceiling (C1) | COW-3632 | #1511 |
| Payerless block-start / epoch-sweep (A4/A5) | COW-3574, COW-3649, COW-3692 | #1478 |
| Mailbox finalization payerless phase | COW-3696 | #1520 |
| D41 partial-fire / deferred-child failure classifier | COW-3785, COW-3837 | #1522, #1530 |
| RAS terminal/settlement reconciliation; coupled commit admission (chunk cap 306) | COW-3836, COW-3855 | #1529, #1532, #1710 |
| CBQS-v2 access rows and CIP-39 v2 fixture | COW-3712, COW-3634/COW-3659 | #1497, #1533 |
| Dormant continuation/deferred gate activation (D50) | COW-3821, COW-3830 | — |

(Source: `docs/access/integration/MERGE-LOG-integ.md`.)

## Appendix A: Baseline access rows with CBSS, Public Store, and RAS amendments

This appendix reproduces the baseline row schedule from node `65a6518f`, with
`SetSecret`, `DeleteSecretVersion` and `DeleteSecret` in A.2 imported from node
`55022cf80`, the Trading Post retirement row from node `5cf8ce13`, three
registration rows from `13de6a65`, and two hire-lifetime rows in A.2 from node PR #1708 commit `194f400f`. The RAS rows
in A.5 use node `13de6a65` for direct `CommitManifest`, node `5cf8ce13`
for `CommitManifestFinalize`, `CommitManifestStage`, and `TransferVolume`,
and node `d60babed` for `DeleteVolume`.
The baseline fingerprints in §5 identify the baseline only. This appendix
does not reproduce the complete version-11, version-13, version-14, version-15, or version-16 node schedule; its
other rows retain their stated baseline pin.

**Generator recipe:** extract the blocks below at `N=65a6518f`, then replace
the three named CBSS rows in A.2 with their entries from
`USER_SYSTEM_ACCESS_ROWS` at `55022cf80`, then replace
`TradingPostDeclare`, `TradingPostDeclareVersion`, and
`TradingPostRegisterArtifact` from `USER_SYSTEM_ACCESS_ROWS` at `13de6a65`,
`TradingPostRetireArtifact` from `USER_SYSTEM_ACCESS_ROWS` at `5cf8ce13`, and `TradingPostHire` and
`TradingPostRevokeHire` from `USER_SYSTEM_ACCESS_ROWS` at `194f400f`.
Check the three earlier named A.5 RAS rows against `ras_user_system_reservation_for_decoded`
at `5cf8ce13`, `DeleteVolume` against that function at `d60babed`, and direct
`CommitManifest` plus its committed-queue reservation at `13de6a65`.
Compare those generated blocks with this appendix to check
the stated mixed provenance.

```text
N=65a6518f24e2734c3289f0230f92f70d072ee6b0
S=execution/src/access_weights.rs
git show $N:$S | sed -n '641,850p'    # A.1  HOST_API_ACCESS_ROWS
git show $N:$S | sed -n '865,868p'    # A.1  HOST_ENVELOPE_ACCESS_ROWS
git show $N:$S | sed -n '1933,2333p'  # A.2  USER_SYSTEM_ACCESS_ROWS (incl. Cbqs :1935, Ras :2317)
git show $N:$S | sed -n '877,891p'    # A.3  ACTOR_SYSTEM_ACCESS_ROWS
```

### A.1 PVM HostApi rows

`HOST_API_ACCESS_ROWS` (`execution/src/access_weights.rs:641-850`):

```rust
pub const HOST_API_ACCESS_ROWS: &[HostApiAccessEntry] = &[
    HostApiAccessEntry::inclusive(
        HostApiOp::StateGet,
        StructuralRow::Point,
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 7), rejected(6)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::StateSet,
        StructuralRow::Write,
        probe(resources(q(0, 1, 0, 0, 0, 0, 0), 40), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::StateSetCheckpoint,
        StructuralRow::Write,
        probe(resources(q(0, 1, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::StateDelete,
        StructuralRow::Write,
        probe(resources(q(0, 1, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::StateScanPrefix,
        StructuralRow::Scan,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::EmitEvent,
        StructuralRow::Composite(POINT_FANOUT_NESTED),
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::runtime_sidecar(
        HostApiOp::ChargeGas,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 1), exhausted(0)),
    ),
    HostApiAccessEntry::runtime_sidecar(
        HostApiOp::GasLeft,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 0), exhausted(0)),
    ),
    HostApiAccessEntry::guest_zero(
        HostApiOp::Context,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 0), exhausted(0)),
    ),
    HostApiAccessEntry::guest_zero(
        HostApiOp::Randomness,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::SendMessage,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 80), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::CallActor,
        StructuralRow::Composite(POINT_NESTED),
        probe(resources(q(2, 0, 0, 0, 0, 0, 0), 2000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::ForkActor,
        StructuralRow::Composite(FORK_ROW),
        probe(resources(q(1, 1, 0, 64, 0, 1, 0), 50001), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TransferBalance,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::ScheduleTimer,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(4, 5, 0, 0, 0, 0, 0), 212), rejected(200)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::ScheduleTimerEx,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(4, 5, 0, 0, 0, 0, 0), 212), rejected(200)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::CancelTimer,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(5, 5, 0, 0, 0, 0, 0), 200), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::ExtendTimer,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(1, 1, 0, 0, 0, 0, 0), 200), rejected(200)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::SubmitJob,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(1, 1, 0, 0, 0, 0, 0), 80), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::IntentDeposit,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 1, 0, 0, 0, 0, 0), 1000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::IntentWithdraw,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 1, 0, 0, 0, 0, 0), 1000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::IntentBroadcast,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 1, 0, 0, 0, 0, 0), 1000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::IntentBroadcastEx,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 1, 0, 0, 0, 0, 0), 1000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::UpgradeSelf,
        StructuralRow::OptionalPoint,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 53600), rejected(0)),
    ),
    HostApiAccessEntry::guest_zero(
        HostApiOp::CreateDeferredTx,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 1070), rejected(0)),
    ),
    HostApiAccessEntry::guest_zero(
        HostApiOp::Keccak256,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 6), rejected(0)),
    ),
    HostApiAccessEntry::guest_zero(
        HostApiOp::Ed25519Verify,
        probe(resources(q(0, 0, 0, 0, 0, 0, 0), 5000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::AcquireEpochAccess,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(25, 1, 0, 0, 0, 0, 0), 5000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::RegisterAccountKey,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(3, 1, 0, 0, 0, 0, 0), 2000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenCreate,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 10422), rejected(10000)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenTransfer,
        StructuralRow::Composite(POINT_WRITE_NESTED),
        probe(resources(q(4, 3, 0, 0, 0, 0, 0), 1000), rejected(1000)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenTransferFrom,
        StructuralRow::Composite(POINT_WRITE_NESTED),
        probe(resources(q(5, 4, 0, 0, 0, 0, 0), 1500), rejected(1500)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenApprove,
        StructuralRow::Write,
        probe(resources(q(0, 1, 0, 0, 0, 0, 0), 500), rejected(500)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenBalanceOf,
        StructuralRow::Point,
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenAllowance,
        StructuralRow::Point,
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenTotalSupply,
        StructuralRow::Point,
        probe(resources(q(1, 0, 0, 0, 0, 0, 0), 0), exhausted(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenMint,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 1000), rejected(1000)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TokenBurn,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 500), rejected(500)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::SubscribeEvent,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 60001), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::UnsubscribeEvent,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(3, 2, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::ForceUnsubscribeEvent,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(3, 2, 0, 0, 0, 0, 0), 0), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::UpdateBid,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(2, 2, 0, 0, 0, 0, 0), 1000), rejected(0)),
    ),
    HostApiAccessEntry::inclusive(
        HostApiOp::TopupSubscription,
        StructuralRow::Composite(POINT_WRITE),
        probe(resources(q(1, 1, 0, 0, 0, 0, 0), 1), rejected(0)),
    ),
];
```

`HOST_ENVELOPE_ACCESS_ROWS` (`execution/src/access_weights.rs:865-868`):

```rust
pub const HOST_ENVELOPE_ACCESS_ROWS: &[(&str, &str)] = &[(
    "timer_effect_flush",
    "R(TimerConfig) once per tx when a schedule/extend effect survives",
)];
```

### A.2 User-signed System instruction rows

BankActor instruction definitions and the `MAX_CARDS_PER_OWNER_BANK = 64` bound are retained in the [BankActor data reference](../references/bankactor-interface#2-data-storage-and-codec). Pricing an instruction here does not enable it; `SubmitRegisterBankProposal` remains rejected by the inspected node.

`USER_SYSTEM_ACCESS_ROWS` (`execution/src/access_weights.rs:1933-2333`), one row per wire variant with its `Rk`/`Wk`/`Scan`/`NestedFrame` shape. This table contains the CBQS-v2 row (`Cbqs`, :1935) and the RAS sub-variant pointer (`Ras`, :2317); the CBQS-v2 numeric rows are in A.4 and the exhaustive RAS sub-variant rows are in A.5.

```rust
pub const USER_SYSTEM_ACCESS_ROWS: &[(&str, &str)] = &[
    (
        "Cbqs",
        "outer Rk(0)+Wk(0); handler-local fixed table: RegisterProvider 6R/3W, UpdateProvider 4R/1W, CreateStream 11R/8W, UpdateStream 2R/1W, CloseStream 7R/6W, TopUpAccount 7R/5W, SettleAccount 5R/4W, WithdrawAccount 6R/5W",
    ),
    ("CreateAccount", "Rk(1)+Wk(1)"),
    ("Transfer", "Rk(2)+Wk(2)"),
    (
        "RunnerRegister",
        "phase1 Rk(42)+Wk(34), then committed active-runner N*Rk(1)",
    ),
    ("RunnerUpdateRateCard", "Rk(1)+Wk(1)"),
    ("RunnerUpdateCapabilities", "Rk(102)+Wk(80)"),
    ("RunnerUpdateEndpoint", "Rk(1)+Wk(1)"),
    ("PaymentGateSetPolicy", "Rk(2)+Wk(1)"),
    ("PaymentGateGetPolicy", "Rk(1)"),
    ("PaymentGateDepositBudget", "Rk(1)+Wk(1)"),
    ("PaymentGateWithdrawBudget", "Rk(1)+Wk(1)"),
    ("PaymentGateBudgetBalance", "Rk(1)"),
    ("PaymentGateDeductBudget", "Rk(3)+Wk(2)"),
    ("PaymentGatePurchasePass", "Rk(8)+Wk(6)"),
    ("PaymentGatePassBalance", "Rk(1)"),
    ("PaymentGatePurchaseEpoch", "Rk(8)+Wk(6)"),
    ("PaymentGateEpochStatus", "Rk(1)"),
    ("PaymentGateVerifyPayment", "Rk(2)"),
    (
        "PaymentGateSettlePayment",
        "Rk(11)+Wk(41)+Scan(SETTLE_GC_BUDGET=16)",
    ),
    ("BankIssueCard", "Rk(9)+Wk(6) [includes pause read]"),
    (
        "BankDeposit",
        "Rk(9)+Wk(3) [includes pause read; transfer-hook frames charge live]",
    ),
    (
        "BankWithdraw",
        "Rk(9)+Wk(3) [includes pause read; transfer-hook frames charge live]",
    ),
    (
        "BankCloseCard",
        "Rk(12)+Wk(10)+2*Scan(MAX_CARDS_PER_OWNER_BANK) [includes pause read]",
    ),
    ("BankSetDefaultCard", "Rk(5)+Wk(1) [includes pause read]"),
    ("BankSetPolicy", "Rk(3)+Wk(2) [includes pause read]"),
    ("BankFreeze", "Rk(4)+Wk(2) [includes pause read]"),
    ("BankUnfreeze", "Rk(4)+Wk(1) [includes pause read]"),
    ("BankPauseBank", "Rk(3)+Wk(1) [includes pause read]"),
    ("BankUnpauseBank", "Rk(3)+Wk(1) [includes pause read]"),
    (
        "BankMintFromFiatVoucher",
        "Rk(7)+Wk(3) [includes pause read]",
    ),
    ("BankSettleProvider", "Rk(6)+Wk(3) [includes pause read]"),
    ("BankIssueCardV2", "Rk(11)+Wk(8) [includes pause read]"),
    ("SetBankOperator", "Rk(3)+Wk(1) [includes pause read]"),
    ("SetBankFiatMintSigner", "Rk(3)+Wk(1) [includes pause read]"),
    (
        "BankTransferOwnership",
        "Rk(7)+Wk(7)+Scan(MAX_CARDS_PER_OWNER_BANK) [includes pause read]",
    ),
    ("RegisterWorkload", "Rk(6+M)+Wk(3), M=volume_mounts.len"),
    (
        "UpdateWorkload",
        "Rk(7+M)+Wk(5), M=changed volume_mounts.len",
    ),
    ("DeregisterWorkload", "Rk(3)+Wk(3)"),
    ("ReportWorkloadStatus", "Rk(1)+Wk(1)"),
    ("RunnerHeartbeat", "Rk(43)+Wk(35)"),
    (
        "RunnerDeregister",
        "phase1 Rk(76)+Wk(57), then committed active-runner N*Rk(1)",
    ),
    (
        "JobSubmit",
        "Rk(37) [selection lane independently owns 37+5N+6M]",
    ),
    (
        "JobResultSubmit",
        "phase1 Rk(24)+Wk(18), then committed committee Rk(14N+1)+Wk(8N), then each result runner p Rk(1+2T_p)+Wk(T_p), then each dissenting or non-reveal runner o Rk(82+4T_o+2G_o)+Wk(61+4T_o+G_o)",
    ),
    ("JobCancel", "Rk(10)+Wk(8)"),
    ("JobResultCommit", "Rk(8)+Wk(5)"),
    ("TokenCreate", "Rk(2)+Wk(3)"),
    (
        "TokenTransfer",
        "Rk(5)+Wk(2); transfer-hook frames charge live",
    ),
    (
        "TokenTransferFrom",
        "Rk(6)+Wk(3); transfer-hook frames charge live",
    ),
    ("TokenApprove", "Rk(1)+Wk(1)"),
    ("TokenIncreaseAllowance", "Rk(2)+Wk(1)"),
    ("TokenDecreaseAllowance", "Rk(2)+Wk(1)"),
    ("TokenPermit", "Rk(2)+Wk(2)"),
    ("TokenMint", "Rk(2)+Wk(2)"),
    ("TokenBurn", "Rk(2)+Wk(2)"),
    ("TokenBurnFrom", "Rk(2)+Wk(2)"),
    ("TokenFreeze", "Rk(1)+Wk(1)"),
    ("TokenUnfreeze", "Rk(1)+Wk(1)"),
    ("TokenSetHook", "Rk(1)+Wk(1)"),
    ("TokenTransferOwnership", "Rk(1)+Wk(1)"),
    ("TokenTransferBatch", "Rk(10*N)+Wk(2*N), N=transfers.len"),
    ("EntitlementGrant", "Rk(3)+Wk(2)"),
    ("EntitlementRevoke", "Rk(4)+Wk(2)"),
    ("EntitlementDelegate", "Rk(3)+Wk(2)"),
    ("EntitlementCreateRole", "Wk(1)"),
    ("EntitlementAssignRole", "Rk(2)+Wk(2)"),
    ("EntitlementRevokeRole", "Rk(1)+Wk(2)"),
    ("UpdateSettlementConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("FundActor", "Rk(2)+Wk(1)"),
    (
        "KeyDelivery",
        "kind: Cip9VolumeDek phase1=Rk(9)+Wk(4), then input partials P*Rk(1), committed merged partials M*Rk(1), assigned runners N*(Rk(3)+Wk(3))+readiness Rk(N-current_in_list), and completed pending-seal Q*Wk(1); Cip7ContentKey=Rk(5)+Wk(2), Cip9ServiceVolumeDek=0",
    ),
    ("UpgradeActor", "Rk(4)+Wk(2)"),
    ("UpdateBasefeeConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("SubmitProposal", GOVERNANCE_TIERED_SUBMIT_USER_SHAPE),
    (
        "SubmitDrainRelayProposal",
        GOVERNANCE_IMMEDIATE_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitAutoDrainPolicyProposal",
        GOVERNANCE_IMMEDIATE_SUBMIT_USER_SHAPE,
    ),
    ("CastVote", GOVERNANCE_VOTE_USER_SHAPE),
    ("EndorseProposal", GOVERNANCE_IMMEDIATE_SUBMIT_USER_SHAPE),
    ("SetGovParam", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("SetCip31FeeSplit", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("UpgradeSystemActor", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("RollbackSystemActor", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("CastBallot", GOVERNANCE_VOTE_USER_SHAPE),
    ("CastValidatorVote", GOVERNANCE_VOTE_USER_SHAPE),
    ("CircuitBreaker", "Rk(6)+Wk(4)"),
    ("CancelProposal", "Rk(5)+Wk(4)"),
    (
        "SubmitTreasuryProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    ("SubmitGrantReleaseProposal", "Rk(0)+Wk(0)"),
    (
        "SubmitRegistryProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    ("SubmitRegisterBankProposal", "Rk(0)+Wk(0)"),
    (
        "SubmitSystemDkgProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    ("SubmitParamProposal", GOVERNANCE_TIERED_SUBMIT_USER_SHAPE),
    (
        "SubmitRentConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitLaneFeeMultipliersProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitUpgradeSystemActorProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitRollbackSystemActorProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitContainerSettlementConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitSemanticSimilarityModelProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitExecutorRegistryPinProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitExecutorRegistryUnpinProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitCip31FeeSplitProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitSettlementConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitReputationConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitAggregatorConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitSlashDistributionProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitNonRevealConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    (
        "SubmitCommitteeConfigProposal",
        GOVERNANCE_TIERED_SUBMIT_USER_SHAPE,
    ),
    ("RunnerUpdateDelegationConfig", "Rk(6)+Wk(1)"),
    ("RunnerDelegateStake", "Rk(16)+Wk(8)"),
    ("RunnerIncreaseDelegation", "Rk(16)+Wk(8)"),
    (
        "RunnerUndelegateStake",
        "phase1 Rk(13)+Wk(7), then committed tranche-index T*Rk(1) and exact touched tranche K*Wk(1)",
    ),
    ("RunnerClaimUnbonded", "Rk(6+N)+Wk(3+N), N=tranche_ids.len"),
    (
        "ExecuteProposal",
        "phase1 Rk(18)+Wk(16), then committed snapshot-voter N*Wk(1) [includes CBSS proposal-classification and conditional pause reads]",
    ),
    (
        "CancelTimer",
        "phase1 Rk(1)+Wk(1), then existing timer worst-case Rk(7)+Wk(7) [includes COW-3693 rent-quarantine actor read]",
    ),
    ("UpdateTimerConfig", "Rk(2)+Wk(2)"),
    (
        "ExtendTimer",
        "phase1 Rk(1)+Wk(1), then existing timer Rk(3)+Wk(0) [includes COW-3693 rent-quarantine actor read and TimerConfig actor+value reads]",
    ),
    ("DeployCode", "Wk(1)"),
    ("RegisterTeeTrustedKey", "Rk(2)+Wk(1) [includes pause read]"),
    ("RevokeTeeTrustedKey", "Rk(2)+Wk(1) [includes pause read]"),
    ("SubmitTeeAttestation", "Rk(5)+Wk(1) [includes pause read]"),
    ("RevokeTeeAttestation", "Rk(2)+Wk(1) [includes pause read]"),
    ("VerifyCae", "Rk(8)+Wk(4) [includes pause read]"),
    ("UpdateCollateral", "Rk(3)+Wk(2) [includes pause read]"),
    (
        "GcNonces",
        "Rk(1)+Wk(9*CBSS_GC_PER_PREFIX_BUDGET)+4*Scan(CBSS_GC_PER_PREFIX_BUDGET) [includes pause read]",
    ),
    ("DeprecateBinding", "Rk(2)+Wk(2) [includes pause read]"),
    (
        "SetSecret",
        "Account=Rk(7)+Wk(5); SecretSpecific preferred=Rk(7+preferred.len)+Wk(7), automatic phase1 Rk(8)+Wk(7) then committed proxy-registry P*Rk(1); includes pause read",
    ),
    ("UpdateSecretPolicy", "Rk(2)+Wk(1) [includes pause read]"),
    (
        "DeleteSecretVersion",
        "phase1 Rk(6)+Wk(8), including evidence index, sweep and outstanding-marker deletes; then indexed epochs E*Rk(1), changed/new U*Wk(1) and expired D*Wk(1), E<=64 [includes pause read]",
    ),
    (
        "DeleteSecret",
        "phase1 Rk(3)+Wk(2), then committed active-version V*(Rk(3)+6*Wk(1)), including each evidence index, sweep and outstanding-marker deletes; per override indexed epochs E*Rk(1), changed/new U*Wk(1) and expired D*Wk(1), E<=64 [includes pause read]",
    ),
    ("RegisterCbssProxy", "Rk(7)+Wk(6) [includes pause read]"),
    (
        "DeregisterCbssProxy",
        "no scopes Rk(4)+Wk(2); non-empty reverse index Rk(5)+Wk(4), then bounded CBSS cadence continuation owns per-scope reshare work [includes pause read]",
    ),
    (
        "ForcedDeregisterCbssProxy",
        "Rk(3)+Wk(2) [includes pause read]",
    ),
    (
        "RotateCommittee",
        "normal=Rk(5+3*new_committee.len)+Wk(6+new_committee.len); force_rekey phase1=Rk(7+3*new_committee.len)+Wk(7+new_committee.len), then next owner-volume cursor page V*(Rk(1)+Wk(1)), V<=64 and owner cap=200; completed reshare deletes the outstanding marker; includes pause read",
    ),
    (
        "SlashCbssProxy",
        "phase1 Rk(7)+Wk(4), then scopes S*Rk(7), health-checked old committees A*C_i*Rk(1), and warranted reshares R*(Rk(1+P_i+C_i)+Wk(1)); failed reshare persists only warranted recovery markers [includes pause read]",
    ),
    ("SubmitReleaseReceipt", "Rk(19)+Wk(5) [includes pause read]"),
    ("RegisterTlockRelease", "Rk(4)+Wk(3) [includes pause read]"),
    ("SubmitTlockRelease", "Rk(4)+Wk(1) [includes pause read]"),
    (
        "OpenAuction",
        "Rk(7)+Wk(6) [pending grief-slash queue blocks re-open]",
    ),
    ("SubmitSealedBid", "Rk(3)+Wk(2)"),
    (
        "RevealAuction",
        "reveal phase1 Rk(7)+Wk(3)+Scan(SETTLE_GC_BUDGET), then bidders B*(Rk(1)+Wk(1)), each attempted request-intent candidate Rk(3+2D)+Wk(5+2D+2*SETTLE_GC_BUDGET), offenders O*Wk(1)+queue Wk(1), with stable bidder-identity resume; follow-up plan page Rk(3+32)+Wk(1) plus one-time lock Wk(1), apply page K<=32 Rk(6+8K)+Wk(5+5K) including cumulative routing, then fixed registry/self-routing/queue/FIFO/lock tail Rk(83)+Wk(66)",
    ),
    (
        "GovRequestSystemDkg",
        "phase1 Rk(11)+Wk(3), then committed proxy-registry P*Rk(1) [includes pause read]",
    ),
    (
        "RequestAccountDkg",
        "phase1 Rk(10)+Wk(3), then committed proxy-registry P*Rk(1) [includes pause read]",
    ),
    (
        "RequestReshare",
        "phase1 Rk(10)+Wk(2), then outstanding Rk(1), committed proxy-registry P*Rk(1), old-committee C*Rk(1), outstanding Wk(1), and outbox custody [includes pause read; pending recovery marker makes this permissionless]",
    ),
    ("FinalizeSecretVersion", "Rk(3)+Wk(1) [includes pause read]"),
    (
        "SubmitLivenessChallenge",
        "Rk(5)+Wk(4) [includes pause read]",
    ),
    (
        "LivenessChallengeResponse",
        "Rk(4)+Wk(4) [includes pause read]",
    ),
    (
        "ExpireLivenessChallenge",
        "Rk(5)+Wk(6) [includes pause read]",
    ),
    (
        "ExpireDkgPending",
        "Rk(7)+Wk(7) [includes pause read; reads marker binding and clears only an exact ceremony match]",
    ),
    ("SessionOpen", "Rk(4)+Wk(3)"),
    ("SessionDeposit", "Rk(2)+Wk(2)"),
    ("SessionSettle", "Rk(6)+Wk(5)"),
    ("SessionClose", "Rk(1)+Wk(1)"),
    ("SessionFinalize", "Rk(3)+Wk(3)"),
    ("SessionSlash", "Rk(0)+Wk(0)"),
    ("ExecutorRegistryPin", "Rk(0)+Wk(0)"),
    ("RegisterBaseImage", "Rk(3)+Wk(3)"),
    ("DeregisterBaseImage", "Rk(2)+Wk(2)"),
    ("RegisterResourceClass", "Rk(2)+Wk(1)"),
    ("DeregisterResourceClass", "Rk(1)+Wk(1)"),
    (
        "UpdateContainerSettlementConfig",
        UNREACHABLE_GOVERNANCE_USER_SHAPE,
    ),
    (
        "DisputeContainerBilling",
        "phase1 Rk(2)+Wk(1), then committed pending-runner N*(Rk(3)+Wk(2))+Fanout(N)",
    ),
    ("ExecutorRegistryUnpin", "Rk(0)+Wk(0)"),
    (
        "UpdateLaneFeeMultipliers",
        UNREACHABLE_GOVERNANCE_USER_SHAPE,
    ),
    ("UpdateRentConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    (
        "SettleActorRent",
        "non-evict Rk(4)+Wk(5); evict adds cardinality Rk(2)+Scan(K)+K*Wk(1)+T*Wk(1), K=committed actor KV count,T=committed actor timer-index len",
    ),
    ("SetActorQuota", "Rk(3)+Wk(2)"),
    (
        "RestoreActor",
        "Rk(2)+Wk(1+declared_entries), declared_entries<=MAX_ACTOR_KV_COUNT",
    ),
    ("UpdateRouteManifest", "Rk(2)+Wk(1)"),
    ("UpdateReputationConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("UpdateAggregatorConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("UpdateSlashDistribution", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("SubmitCrashAttestation", "Rk(4)+Wk(1)"),
    ("UpdateNonRevealConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    ("UpdateCommitteeConfig", UNREACHABLE_GOVERNANCE_USER_SHAPE),
    (
        "UpdateSemanticSimilarityModel",
        UNREACHABLE_GOVERNANCE_USER_SHAPE,
    ),
    ("TradingPostDeclare", "Rk(3)+Wk(4)"),  // permanent volume index, table 16
    ("TradingPostDeclareVersion", "Rk(5)+Wk(4)"),  // permanent volume index, table 16
    ("TradingPostRetireDeclaration", "Rk(1)+Wk(1)"),
    ("TradingPostRegisterStore", "Rk(1)+Wk(2)"),
    ("TradingPostGrant", "Rk(5)+Wk(3)"),
    ("TradingPostRevokeGrant", "Rk(2)+Wk(2)"),
    ("TradingPostRegisterArtifact", "Rk(13)+Wk(6)"),  // permanent volume index, table 16
    ("TradingPostRetireArtifact", "Rk(6)+Wk(4)"),
    (
        "TradingPostHire",
        "phase1 Rk(12)+Wk(6), then exact selected split P*(Rk(2)+Wk(1)), P<=MAX_TRADING_POST_SPLIT_ENTRIES",
    ),
    ("TradingPostDeposit", "Rk(4)+Wk(2)"),
    ("TradingPostWithdrawUnused", "Rk(4)+Wk(2)"),
    (
        "TradingPostRenew",
        "phase1 Rk(6)+Wk(2), then committed hire split P*(Rk(2)+Wk(1)), P<=MAX_TRADING_POST_SPLIT_ENTRIES",
    ),
    ("TradingPostSetRenewal", "Rk(1)+Wk(1)"),
    ("TradingPostPauseHire", "Rk(3)+Wk(1)"),
    ("TradingPostResumeHire", "Rk(3)+Wk(1)"),
    ("TradingPostRevokeHire", "Rk(9)+Wk(3)"),
    (
        "TradingPostSettlePerCall",
        "phase1 Rk(5)+Wk(2), then committed hire split P*(Rk(2)+Wk(1)), P<=MAX_TRADING_POST_SPLIT_ENTRIES",
    ),
    ("TradingPostAttest", "Rk(2)+Wk(4)"),
    ("Ras", "RasInstruction(kind,payload) exhaustive subtable"),
    (
        "IntentDeposit",
        "Rk(8)+Wk(4); transfer-hook frames charge live",
    ),
    (
        "IntentWithdraw",
        "Rk(7)+Wk(3); transfer-hook frames charge live",
    ),
    (
        "IntentSettle",
        "Rk(3I+2D)+Wk(5I+2D+2*SETTLE_GC_BUDGET)+Scan(SETTLE_GC_BUDGET)",
    ),
    ("IntentBroadcast", "Rk(5)+Wk(6)"),
    ("IntentRequestWithdraw", "Rk(0)+Wk(0) [unsupported]"),
    ("IntentCreditDeposit", "Rk(0)+Wk(0) [unsupported]"),
];
```

### A.3 Actor-minted deferred System rows

`ACTOR_SYSTEM_ACCESS_ROWS` (`execution/src/access_weights.rs:877-891`), reserved in full before the first raw-store dispatch and never refunded:

```rust
pub const ACTOR_SYSTEM_ACCESS_ROWS: &[(&str, &str)] = &[
    (
        "JobSubmit",
        "Rk(1 deferred sender ingress); native handler is System selection 37+5N+6M; actor attachments rejected before state",
    ),
    (
        "IntentDeposit",
        "Rk(8)+Wk(4) fixed; FirstNestedSetup+NestedFramek(2) live-only",
    ),
    (
        "IntentWithdraw",
        "Rk(7)+Wk(3) fixed; FirstNestedSetup+NestedFramek(2) live-only",
    ),
    ("IntentBroadcast", "Rk(5)+Wk(6), reserved in full"),
];
```

### A.4 CBQS-v2 decoded rows

Structural (engine-minted, native) and, as a complete signed transaction (native `+ E=5` envelope) at the `1.35×` genesis scalar, charged (`execution/src/access_weights.rs:1936`; `docs/access/integration/MERGE-LOG-integ.md:188`):

| CBQS-v2 variant (wire order) | native shape | structural | charged (E=5, 1.35×) |
| :---- | :---- | ----: | ----: |
| `RegisterProvider` | `6R / 3W` | 9 | 19 |
| `UpdateProvider` | `4R / 1W` | 5 | 14 |
| `CreateStream` | `11R / 8W` | 19 | 33 |
| `UpdateStream` | `2R / 1W` | 3 | 11 |
| `CloseStream` | `7R / 6W` | 13 | 25 |
| `TopUpAccount` | `7R / 5W` | 12 | 23 |
| `SettleAccount` | `5R / 4W` | 9 | 19 |
| `WithdrawAccount` | `6R / 5W` | 11 | 22 |

### A.5 Signed RAS / CBFS sub-variant rows

The `Ras` entry in A.2 dispatches to the exhaustive RAS sub-table in node `execution/src/access_weights.rs`. The client table in `docs/access/CLIENT-LIMITS.md` at `13de6a65` includes the version-15 `DeleteVolume` value; the corrected direct `CommitManifest` fixed term first appears in that table at `bb45363a`. The `CommitManifest` and `CommitManifestFinalize` rows below are the `13de6a65` and `5cf8ce13` measurements; §5.5 gives their repriced shard terms at `4294f24b`. Selected amended rows and the rows that bind the ordinary-System `1_500` ceiling and staging cap follow:

| RAS sub-variant | native structural row | complete identity (`E=5`) | note |
| :---- | :---- | ----: | :---- |
| `SettlePorChallenge` | `1,106 + 2N` (`52R+20W+2·Scan(4096)`, `Scan(4096)=517`) | `1,111 + 2N` | third-miss initial row; sets `MAX_SIGNED_ORDINARY_SYSTEM_INITIAL_ROW`; `+2N` phase is COW-3672 staged (`types/src/constants.rs:846-867`) |
| `SubmitDrainAudit` | `1,067 + 2N` | `1,072 + 2N` | `+2N` COW-3672 staged |
| `ReportUnreachableRelay` | `557 + 2N` | `562 + 2N` | `+2N` COW-3672 staged |
| `CommitManifest` | `52 + T + K + 2X_m` | `57 + T + K + 2X_m` | direct row at node `13de6a65` (repriced at `4294f24b`, §5.5); `T=1` for nonempty token; `K` committed pending epochs; `X_m=D+A+R+ceil(A/512)+ceil(R/512)` |
| `CommitManifestFinalize` | `60 + X_f` | `65 + X_f` | complete signed row `65 + X_f <= 1,111` (i.e. `X_f <= 1,046`), where `X_f = 3H+2D+2A+2R+2·ceil(A/512)+2·ceil(R/512)`; sets `MAX_COMMIT_STAGE_CHUNKS = 306` (COW-3855, node `5cf8ce13`); shard terms repriced at `4294f24b` with the same fixed term and cap (§5.5) |
| `CommitManifestStage` | `17` on the maximum path | `22` | `12R/5W`; stage intake uses the version-13 cap of 306 chunks |
| `TransferVolume` | `26` | `31` | `16R/10W`, including the active Public-artifact pin read (node `5cf8ce13`) |
| `DeleteVolume` | `18` | `23` | `10R/8W`, including the Public Store pin read (node `d60babed`) |
| `RegisterRelay` | `25 + 2·Scan(4096) = 1,059` | `1,064` | two bounded registry scans |
| `UpdateRelay` | `7 + 2·Scan(4096) = 1,041` | `1,046` | two bounded registry scans |
| `SetRelayStatus` | `30 + Scan(4096) = 547` | `552` | one bounded registry scan |
