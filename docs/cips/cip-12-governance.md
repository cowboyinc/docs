---
title: "CIP-12: On-Chain Governance & System Actor Upgrades"
description: Bicameral stake + validator governance with a Security Council emergency authority and built-in system actor upgrade mechanism
icon: gavel
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-04-09
</Note>

## 1. Abstract

This CIP specifies Cowboy's on-chain governance system and the mechanism by which **system actors** and governance-tunable parameters are upgraded. The system-actor space is the deployed registry `node/runner/src/system_actors.rs` (see the WP §9.1 canonical allocation table): `0x01`–`0x11` are code-deployed (including `0x0E` ROUTE_REGISTRY, `0x0F` GATEWAY_REGISTRY, `0x10` RECEIPT_REGISTRY, `0x11` VALIDATOR_SET), `0x12` is reserved (PaymentGate, spec-allocated), and `0x13` CONTAINER_REGISTRY / `0x14` INTENT_SETTLEMENT / `0x1D` EVENT_SUBSCRIPTION (virtual) / `0x1E` TRADING_POST are also deployed. Not every deployed address is upgradeable/pausable — the governance-actionable set is the explicit allowlist in §7.2. Governance is implemented as the `0x09 Governance` system actor and supports:

- **Bicameral voting** from day one: a proposal must pass both a staked-CBY chamber (economic weight) and a validator chamber (one-validator-one-vote operational weight).
- **Tiered proposals** (Tier 0–4) with thresholds, quorums, and timelocks scaled to blast radius.
- **On-chain temperature checks** as non-binding signal votes before formal submission.
- A permanent **Security Council** 7-of-9 multisig that can cancel queued proposals, trigger a fast-track path during emergencies, and circuit-break a system actor. Circuit-break is the Council's one unilateral protocol power and is strictly time-limited: a pause auto-reverts after 7 days unless the community ratifies an extension via a Tier 3 proposal, and extensions are capped (§7.7). The Security Council is not the Foundation.
- Built-in **system actor upgrades** via content-addressed PVM bytecode activation, with a rollback slot and optional migration functions.
- A separate **Labs Multisig** controlling only the governance portal frontend — never the protocol.

Whitepaper §11 provides a summary of the governance model; this CIP is the full specification.

---

## 2. Motivation

Cowboy ships with many explicitly **governance-tunable** parameters (CIP-1, CIP-3, CIP-5, CIP-9, CIP-10, whitepaper §13). Without a specified governance mechanism, every parameter change would require either a hard fork or a centralized admin key — neither is acceptable for a live L1.

The governance system must satisfy three design constraints:

1. **Separation of Foundation and protocol authority.** The Foundation is a legal entity that holds the treasury and runs ops. It should have **zero** protocol powers and act only as a recipient of governance-authorized disbursements. This separation is important for the regulatory posture and prevents capture.
2. **No arbitrary sunset.** Token governance has well-documented failures (voter apathy, plutocracy, bribery markets). A permanent emergency authority is a feature, not training wheels. Removal of that authority should itself be a governance decision, not a calendar event.
3. **Operational voice for validators.** Token-weighted voting alone ignores the judgment of validators who actually run infrastructure. A parameter change that token holders approve but validators deem unsafe should not pass.

This CIP addresses all three: the Foundation has no protocol powers; bicameral voting (staked CBY + validator majority) is live from day one; and the Security Council is a permanent but removable-by-Tier-4 emergency brake.

---

## 3. Entities

Three distinct entities, intentionally separated:

### 3.1 The Foundation

A legal entity (e.g., "Cowboy Foundation") that holds a treasury address on-chain and runs off-chain operations (legal, accounting, payroll). The Foundation has **no protocol authority** by charter: it does not propose, vote, execute, or cancel. This is a governance/legal constraint on the Foundation entity, **not** an on-chain address denylist — the protocol does not special-case the Foundation's treasury address; enforcement is the charter plus the general staking/tier requirements every proposer faces. It receives funds only when a treasury disbursement proposal passes governance.

Foundation employees MAY serve as Security Council members in their individual capacity, but the Foundation SHOULD hold **at most one (1)** Security Council seat. Exceeding this is grounds for a Tier 4 rotation proposal.

### 3.2 The Security Council

A 7-of-9 multisig of named individuals serving fixed terms. The Council's **only** powers are:

1. **Cancel** a queued proposal during its timelock period (Tier 0–3 only; cannot cancel Tier 4).
2. **Trigger fast-track** execution on a Tier 3 (system actor upgrade) proposal in an emergency — skipping the temperature check, compressing voting to 24 hours, compressing the timelock to 6 hours, and raising thresholds to compensate (see §7.6 for full parameters).
3. **Circuit-break** specific system actors by toggling a `paused` flag (requires retroactive ratification within 7 days or the pause auto-reverts).

The Council **cannot**:
- Submit proposals (anyone with the proposal deposit can)
- Vote in either chamber
- Change governance parameters directly
- Upgrade actors directly
- Spend treasury funds
- Modify its own membership (only Tier 4 meta-governance can)

Council membership: 9 named individuals selected for diverse expertise (core protocol developers, independent security researchers, validator operators, ecosystem builders, community-elected seats). At most 1 seat may be Foundation-affiliated. Signers use hardware wallets; each key is held individually, never shared. Annual rotation of at least 2 seats is encouraged, enforced via Tier 4 proposal.

### 3.3 The Labs Multisig

A separate multisig (e.g., 3-of-5) controlled by Cowboy Labs. Its **only** authority is upgrades to the governance portal frontend (static assets in CBFS) and the portal gateway actor under CIP-11. It has **no** protocol authority — a compromised Labs Multisig can only serve a bad UI, not change governance rules. Users retain the ability to vote via CLI or third-party portals pointed at the same `0x09` state.

The Labs Multisig signer set MUST NOT overlap with the Security Council signer set by more than one person, to prevent correlated compromise.

---

## 4. The `0x09 Governance` System Actor

### 4.1 Storage layout

```
params:              map<ParamKey, ParamValue>           // live values read by other system actors
param_history:       map<ParamKey, Vec<ParamChange>>     // audit trail
config_version:      u64                                 // bumped on every params mutation

proposals:           map<ProposalId, Proposal>
proposals_by_status: map<Status, Vec<ProposalId>>        // TempCheck|Voting|Queued|Executed|ExecutionFailed|Rejected|Cancelled

temp_check_votes:    map<ProposalId, map<Address, Signal>>
stake_votes:         map<ProposalId, map<Address, Vote>>
validator_votes:     map<ProposalId, map<ValidatorId, Vote>>

actor_versions:      map<Address, VersionRecord>         // active code per system actor
rollback_slot:       map<Address, VersionRecord>         // previous version (grace window)
pending_upgrades:    map<Address, PendingUpgrade>        // queued but not yet activated

security_council:    SignerSet                           // 7-of-9
labs_multisig:       Address                             // reference only, no authority here
paused_actors:       map<Address, PauseRecord>           // circuit-breaker state
council_cancellations: Vec<CancellationRecord>           // rolling log for griefing-cap review (§7.8)
```

### 4.2 Config consumer pattern

Other system actors MUST read governance-tunable parameters from `0x09.params` rather than maintaining local copies. Two acceptable patterns:

- **Direct read** on every access (simple, one extra cross-actor message per call).
- **Cached read**, invalidated on the `ConfigVersionBumped(old, new)` event emitted by `0x09` whenever `config_version` increments.

Scalar governance params set via `SetGovParam` do live under `0x09` (prefix `system:gov:param:`). **However**, the deployed structured `BasefeeConfig` (`T_c`, `T_b`, `alpha`, `delta`, and the other EIP-1559 fields) is stored at the **`DualBasefee` actor `0x06`** under `BASEFEE_CONFIG_KEY = "system:basefee_config"` (`node/execution/src/basefee.rs`, loaded via `BasefeeConfig::load_from_storage()`), **not** at `0x09.params`. Treat this section's "read from `0x09.params`" as the intended consumer pattern for scalar params; structured per-actor configs currently live at their owning actor and are mutated by governance-enacted instructions (e.g. `UpdateBasefeeConfig`).

### 4.3 Upgradeability of `0x09` itself

The Governance actor is upgradeable **only** via a Tier 4 `MetaGovernance { op: UpgradeGovernance { ... } }` proposal (see §7.3). This path has no fast-track, no Security Council cancellation authority, and no Circuit-breaker pause (which is forbidden on `0x09`, §7.7). There is no out-of-band escape hatch.

---

## 5. Proposals

### 5.1 Tiers

| Tier | Name | Scope |
|---|---|---|
| **0** | Parameter tuning | Scalar updates to any `governance-tunable` parameter (see genesis defaults and CIP-1/3/5/9/10/13/31) — includes e.g. CIP-3 lane fee multipliers, CIP-1 v3 priority-tier multipliers, CIP-31 CBFS rent rates and slashing magnitudes, CIP-13 unbonding & commission bounds |
| **1** | Registry & whitelist | Python stdlib whitelist additions, approved runner images, model flags/bans, TEE attestation roots, relay operator allowlist |
| **2** | Treasury & disbursement | Grants, liquidity incentives, audit payments, Foundation operating budgets, burns |
| **3** | System actor upgrade | Hot-swap PVM bytecode for any deployed system actor (currently `0x01`–`0x0D` in code; future `0x0E`–`0x13` once activated). Excludes virtual / intercepted actors such as `0x1D` (CIP-29), which are upgraded via protocol-level code changes, not bytecode swap. With optional migration (§7) |
| **4** | Constitutional / meta | Upgrade `0x09` itself, modify Security Council membership, change tier parameters, pause an actor **permanently**, or extend a Tier 3 pause **past the consecutive-renewal cap** (default 3 extensions ≈ 90 days; see §7.7) |

### 5.2 Tiered parameters

All values are initial defaults. Every number in this table is itself governance-tunable via Tier 4.

| Tier | Proposal deposit | Temp check | Voting window | Stake quorum | Stake approval | Validator majority | Timelock | Fast-track |
|---|---|---|---|---|---|---|---|---|
| 0 | 1,000 CBY | 3 days | 5 days | 5% of active stake | > 50% | > 50% of active validators | 3 days | No |
| 1 | 5,000 CBY | 3 days | 5 days | 10% | > 50% | > 50% | 5 days | No |
| 2 | 5,000 CBY | 5 days | 7 days | 15% | > 55% | > 50% | 7 days | No |
| 3 | 25,000 CBY | 7 days | 7 days | 15% | > 60% | > 50% | 7 days | Yes — see §7.6 |
| 4 | 100,000 CBY | 14 days | 14 days | 20% | > 66% | > 66% | 14 days | No |

Deposit is **refunded** if the proposal reaches the voting stage (temperature check passed and deposit holder did not withdraw). Deposit is **burned** if the temperature check fails or if the proposal is withdrawn after entering the voting stage.

> **⚠️ On-chain status (2026-07) — deployed timing is demo-scale, and errors are generic.** The day-scale durations in this table and in §7.7 are the **intended** values; the current genesis defaults are **compressed to demo-scale block counts** (`node/types/src/constants.rs`, `runner/src/types.rs`): e.g. `TEMP_CHECK_BLOCKS = 100`, `MIN_VOTING_BLOCKS = 5`, per-tier `timelock_blocks` = 5/5/7/7/14, `PAUSE_DURATION_BLOCKS = 7`, `PAUSE_EXTEND_BLOCKS = 30`, `MIN_ROLLBACK_WINDOW_BLOCKS = 7`, `CANCELLATION_REVIEW_WINDOW_BLOCKS = 90` — all governance-tunable. Separately, the on-chain code defines **no governance-specific error taxonomy**: every governance rejection surfaces as a generic `ExecutionError` → structured code `Unauthorized (1211)`, `InvalidData (1213)`, or `ActorPaused (1219)` (`node/execution/src/structured_error_map.rs`). A proposal/council/tier/quorum-specific error set would need to be added if desired.

### 5.3 Proposal body

Proposals reference a content-addressed body stored in CBFS:

```
Proposal {
  id:                 u64            // monotonic counter (system:proposal_counter @ 0x09, read-and-bump at submission); BE-encoded in the proposal storage key. NOT a hash.
  tier:               Tier
  submitter:          Address
  deposit:            u256
  title:              string
  body_ref:           CbfsRef        // rationale, spec text, links to forum discussion
  payload:            Payload        // machine-readable change set (§5.4)
  status:             Status
  submitted_at:             Block
  temp_check_snapshot_block: Block          // stake weights for temp check denominator; fixed at submission
  temp_check_ends:           Block
  voting_snapshot_block:     Option<Block>  // stake weights for formal voting; fixed when temp check passes
  voting_starts:             Option<Block>
  voting_ends:               Option<Block>  // may be extended per §6.5
  voting_extensions_used:    u8             // bounded by max_voting_extensions (§6.5)
  executable_at:             Option<Block>  // set on entering Queued
}
```

### 5.4 Payload types

```
enum Payload {
    ParamUpdate { changes: Vec<(ParamKey, ParamValue)> },
    RegistryUpdate { add: Vec<RegistryEntry>, remove: Vec<RegistryEntry> },
    TreasuryDisbursement { recipient: Address, amount: u256, memo: string },
    SystemActorUpgrade { target: Address, new_code_hash: bytes32, code_ref: CbfsRef, migration: Option<MigrationSpec>, activation_delay_blocks: u64, rollback_window_blocks: u64, spec_ref: CbfsRef },
    MetaGovernance { op: MetaOp },
    Circuit { target: Address, action: PauseAction },
}
```

§7.1 is the authoritative schema for the `SystemActorUpgrade` variant (including field types, default values, and validation rules). The summary row above omits internal field structure for readability.

Payloads are executed deterministically by `0x09` when the timelock expires. Execution is idempotent and failures are logged without consuming the proposal; a failed execution may be retried within a grace window of `execution_retry_blocks` (Tier 0 param, default 1 day). If retries are exhausted, the proposal transitions to `ExecutionFailed` and its deposit is refunded — the proposal passed governance but could not be applied, which is a bug in the payload, not a spam signal.

---

## 6. Voting

> **⚠️ On-chain status (2026-07) — the deployed tally is simplified and fail-closed.** The stake-weight **snapshot integration is not yet wired** (COW-1028): `voting_snapshot_total` / `validator_snapshot_total` are defined on the proposal but currently written as `0`. Consequences in current code (`runner/src/types.rs::resolve_at` / `participation_below_floor`): (a) with a zero denominator a proposal resolves **`Defeated`** — governance is effectively **inert until the snapshot writer lands**; (b) the stake tally is an **unweighted distinct-address count** (1 vote per address), **not** the token/stake-weighted model described in §6.2; (c) the per-tier `stake_quorum_pct` thresholds exist in `TierParams` but are not enforceable while the denominator is 0. The weighted/snapshotted model below is the intended design; treat it as pending COW-1028.

### 6.1 Bicameral rule

A proposal passes if and only if **all** of:

```
stake_quorum_met     : total_stake_voted >= stake_quorum_pct * active_stake_at_snapshot
stake_approval_met   : yes_stake / (yes_stake + no_stake) > stake_approval_pct
validator_majority   : yes_validator_votes / active_validators_at_snapshot > validator_majority_pct
not_cancelled        : security_council did not cancel during timelock
```

`abstain` contributes to quorum but not to approval ratio. A proposal that passes the stake chamber but fails the validator chamber is **rejected** — validators have an effective veto on operational parameters.

### 6.2 Stake chamber

**Weight source:** CBY staked to validators (self-stake and delegated stake both count). Unstaked CBY held in wallets has zero weight. This deliberately forces participation through the staking system.

**Snapshot:** Stake weights for formal voting are frozen at `voting_snapshot_block`, taken when the temperature check concludes successfully and the proposal enters the voting stage. (The temperature check uses a separate earlier snapshot, `temp_check_snapshot_block`, fixed at submission — see §6.4.) Late stakers cannot vote on the proposal; earlier unstakers who are still in the 7-day unbonding queue at the snapshot **still count** for the full weight they had when they initiated unbonding (prevents griefing by flash-exiting mid-proposal).

**Delegation:** Delegators to validators MAY override their validator's vote per proposal. Absent an override, the delegated weight votes with the validator. Override messages are cheap (System lane).

**Vote types:** `Yes | No | Abstain`. For multi-choice proposals (Tier 0 parameter-setting with a menu), the ballot is an index into the options list; the first option exceeding the approval threshold wins.

**Vote mutability:** Votes may be changed until the last epoch of the voting window, at which point they are sealed.

### 6.3 Validator chamber

**Weight source:** Exactly one vote per active validator, independent of stake. A validator running 1% of total stake has the same chamber weight as a validator running 10%.

**Snapshot:** Active validator set at the proposal's `voting_snapshot_block` (taken when temp check passes). Jailed or exited validators at that block do not count.

**Delegation:** Not applicable — validators vote themselves, not their delegators (who already vote in the stake chamber).

**Vote types:** Same as stake chamber.

### 6.4 Temperature checks

Before a proposal enters the formal voting stage it MUST pass a temperature check: a non-binding on-chain signal vote with a low participation bar. Duration per tier is given in §5.2.

**Purpose.** Gauge sentiment early, kill proposals with no demonstrated support before they consume a voting window, and surface controversial splits so proposers can refine before formal submission. Anti-spam is primarily enforced by the tier deposit (§5.2); the temperature check is an additional filter so that proposals with a non-zero deposit but zero demonstrated interest do not waste validator attention.

**Snapshot.** The temperature check has its own earlier stake snapshot, `temp_check_snapshot_block`, fixed at the block the proposal is submitted. This is distinct from the formal-voting snapshot (§6.2), which is only taken if the temperature check passes. Using a distinct pre-voting snapshot resolves the denominator: the participation percentage is measured against stake active at submission, not against a snapshot that does not yet exist.

**Pass conditions.** A proposal passes the temperature check if **all** of:

```
participation_met    : total_signal_stake >= temp_check_min_participation_pct * active_stake_at_temp_check_snapshot
approval_met         : yes_signal / (yes_signal + no_signal) >= temp_check_approval_pct
```

Initial defaults (Tier 0 params, governance-tunable):

- `temp_check_min_participation_pct` = 1% of active stake
- `temp_check_approval_pct` = 33%

Abstain contributes to participation only. If either condition fails at the end of the temp check window, the proposal is rejected and the deposit is burned.

Temperature check tallies are displayed on the portal alongside the formal vote tally.

### 6.5 Tallying, finalization, and participation-triggered extension

Running tallies are computed by a tick handler invoked each block while any proposal is in `Voting`. At the scheduled `voting_ends` block, `0x09` performs a **participation check** before freezing the tally.

**Participation check.** Let:

- `validator_participation = (validators_voted / active_validators_at_snapshot)`
- `stake_participation = (total_stake_voted / active_stake_at_voting_snapshot)`

The proposal may finalize if **both** of:

- `validator_participation >= voting_min_validator_participation_pct`
- `stake_participation >= voting_min_stake_participation_pct`

**Extension.** If either participation measure is below its floor, `0x09` extends `voting_ends` by `voting_extension_blocks` and emits `VotingWindowExtended { proposal_id, new_voting_ends, stake_participation, validator_participation }`. The snapshot blocks do **not** change — stake and validator weights remain as captured at the original voting start, so the extension cannot be exploited to change who is eligible to vote.

Extensions are capped at `max_voting_extensions`. After the cap is reached, `0x09` finalizes the tally at the extended `voting_ends` regardless of participation — the proposal either passes (if quorum and approval thresholds were met by whoever participated) or is rejected. This prevents an indefinite-extension attack where a participant simply stays offline to stall finalization.

**Parameters (Tier 0, governance-tunable):**

- `voting_min_validator_participation_pct` = 60%
- `voting_min_stake_participation_pct` = 60% of the tier's stake quorum (i.e., at Tier 3 with a 15% quorum requirement, extension triggers when observed stake participation is below 9%)
- `voting_extension_blocks` = 1 day
- `max_voting_extensions` = 3

**Rationale.** The window exists to give honest participants time to observe and respond. If the chain or the validator set is under pressure during the scheduled window such that participation is meaningfully below the expected floor, the extension bounds the damage a timing attack can do. Capping extensions at 3 ensures governance still terminates — an attacker who keeps participation below the floor pays at most 3 extra days before the tally proceeds against them.

**Finalization.** At the final `voting_ends` (original or extended), the tally is frozen and the proposal transitions to either `Queued` or `Rejected`. `Queued` proposals set `executable_at = now + timelock`.

---

## 7. System Actor Upgrades

System actor upgrades are handled natively by `0x09 Governance` — there is no separate `SystemActorUpgrader` actor. Keeping the upgrader inside Governance avoids adding another trust boundary and another upgrade target.

### 7.1 Proposal shape (Tier 3)

```
SystemActorUpgrade {
  target:                  Address        // e.g. 0x02 JobDispatcher; MUST NOT be 0x09 (see §7.3)
  new_code_hash:           bytes32        // keccak256 of new PVM bytecode
  code_ref:                CbfsRef        // CBFS volume holding the blob
  migration:               Option<MigrationSpec> {
    fn_name:               string
    args:                  bytes
    expected_state_hash_after: bytes32    // post-migration invariant (§7.4)
  }
  activation_delay_blocks: u64            // blocks to wait after timelock expires (min 0)
  rollback_window_blocks:  u64            // blocks after activation during which rollback is available (min = 7 days)
  spec_ref:                CbfsRef        // human-readable upgrade notes
}
```

Timing is specified as **offsets**, not absolute blocks. Absolute block numbers are computed at queue time, when `executable_at` is known:

- `activation_block = executable_at + activation_delay_blocks`
- `rollback_deadline = activation_block + rollback_window_blocks`

This avoids the chicken-and-egg problem of validating absolute times at submission, when `executable_at` has not yet been determined.

> **⚠️ On-chain status (2026-07).** The **deployed** `SubmitUpgradeSystemActorProposal` payload carries only `{ target, new_code_hash: [u8;32] (keccak256 of bytecode), activation_delay_blocks, rollback_window_blocks }` (plus the generic proposal fields `description_hash`, `voting_blocks`, `tier`). There is **no `code_ref`, no `migration` spec, and no `spec_ref`** in the on-chain struct (`cowboy-protocol-codec` `SubmitUpgradeSystemActorProposal`; stored fields in `runner/src/types.rs`; enacted record `ActorVersionRecord { version, code_hash, activated_at_block, rollback_deadline_block }`). Code is referenced **solely by its 32-byte hash**; the CBFS `code_ref`/`spec_ref` and the `migration` machinery below (§7.4.b) are part of the intended design but are **not yet on-chain** — enactment (`gov_enact.rs::apply_upgrade_system_actor`) only writes the `code_hash`/rollback pointer, and the bytecode fetch + dispatcher code-swap are explicitly external/future.

### 7.2 Validation at submission

Before accepting the proposal, `0x09` verifies:

1. `target` is a **pausable/upgradeable** system actor. The code enforces an explicit allowlist (`is_pausable_actor`, `node/types/src/pause.rs`), **not** a contiguous range: `{0x04, 0x05, 0x06, 0x07, 0x08, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x13, 0x14, 0x1D, 0x1E}`. Excluded (and why): the job trio `0x01`–`0x03` (coupled into block construction), `0x09` GOVERNANCE (self-lock), `0x0A` STORAGE_MANAGER and `0x0B` RELAY_REGISTRY (epoch-boundary unconditional writes → brick risk), `0x11` VALIDATOR_SET (consensus liveness), and reserved `0x12`. (`0x06` and `0x13` are pausable because their write paths were isolated so a pause no longer halts block production.) `target` MUST NOT be `0x09` (self-upgrade goes through Tier 4, §7.3).
2. `code_ref` resolves to bytecode whose hash matches `new_code_hash`.
3. Bytecode passes the PVM determinism whitelist (no forbidden imports, no non-deterministic ops).
4. Bytecode size is within `max_system_actor_bytecode_size` (Tier 0 param, default 512 KiB).
5. `rollback_window_blocks >= blocks_per_day * 7` (minimum 7-day rollback window).

There is deliberately no precondition on the current state of `target` at submission time. The actor is almost certainly still in service during temp check, voting, and timelock, so any state-at-submission assertion would typically be stale by activation. Migration safety is checked at activation time (§7.4), not submission.

Failing any check rejects the proposal before the temperature check stage; the deposit is returned.

### 7.3 Upgrading `0x09` itself

`0x09` is explicitly excluded from `SystemActorUpgrade.target`. Self-upgrade goes through a dedicated `MetaGovernance { op: UpgradeGovernance { ... } }` payload, which uses the same bytecode/migration fields but is Tier 4 (not Tier 3), has no fast-track path, and has no Security Council cancellation authority. This prevents a compromised or misguided fast-track from replacing the rules that define fast-track.

### 7.4 Execution

There are two execution paths, selected by whether the proposal's `migration` field is set. Validators in both paths pre-fetch new bytecode from CBFS during the timelock period so activation is zero-downtime. A validator that cannot fetch before `activation_block` fails to execute messages to `target` until it catches up — identical to any other "missing code" lag.

#### 7.4.a Code-pointer-only upgrade (no migration)

When `migration` is `None`, there is no state transformation and no quiesce. At `activation_block`, `0x09` performs a single atomic transition in one system transaction:

1. Copy the current `VersionRecord` for `target` to `rollback_slot[target]`.
2. Write the new `VersionRecord` to `actor_versions[target]`.
3. Emit `SystemActorUpgraded(target, old_version, new_version)`.

The next message delivered to `target` in any subsequent block invokes the new bytecode. No messages are dropped or delayed, no queue is drained, and `target` remains available across the transition. This path is appropriate for pure code refactors, bug fixes, and additions that do not rewrite persistent state.

#### 7.4.b Migration-accompanied upgrade

When `migration` is `Some(spec)`, the migration needs a consistent state snapshot, so `target` is briefly quiesced. The flow at `activation_block`:

1. `0x09` places `target` in **quiesce** state: new inbound messages to `target` are held in a queue; in-flight messages already dispatched in the current block drain against the old code.
2. Once the drain completes (typically the next block), `0x09` performs these steps in one atomic system transaction:
   a. Copy the current `VersionRecord` for `target` to `rollback_slot[target]`.
   b. Write the new `VersionRecord` to `actor_versions[target]`.
   c. Invoke `target.migration.fn_name(migration.args)` under the new code, logged as `MigrationExecuted`. **⚠️ Normative (security):** this migration call MUST run under a bounded gas/cycle cap (a governance-tunable `max_migration_cycles`), **not** "unlimited gas" — an uncapped migration that infinite-loops would halt block production. A migration that exceeds the cap is treated as a step-(c) failure and triggers the atomic rollback in step 3. *(This entire migration path is **not yet implemented on-chain** — see the §7.1 on-chain-status note; when built, the cap is mandatory.)*
   d. Compute the post-migration state hash of `target` and compare to `migration.expected_state_hash_after` (if supplied).
3. **Atomic rollback on failure.** If step (c) reverts, or if step (d) disagrees with the expected hash, `0x09` reverts steps (a)–(c) in the same atomic transaction. After rollback, `target` is back on its prior (known-working) code and state. `0x09` then automatically releases `target` from quiesce and emits `MigrationFailed { target, reason, reverted_to_version }`. Queued messages resume dispatch against the old code. No human action is required to restore service — the actor is already back in the state it was in immediately before activation. Operators MAY prepare a corrected migration proposal at their own pace.
4. On success, `0x09` emits `SystemActorUpgraded(target, old_version, new_version)` and releases `target` from quiesce. Queued messages resume dispatch against the new code.

Migration-accompanied upgrades are designed for infrequent, planned state-shape changes. Proposers whose target cannot tolerate quiesce (long-running timers, latency-critical dispatch) SHOULD structure the change as a sequence of code-pointer-only upgrades (§7.4.a) that internally handle schema coexistence — or, in the rare case where true quiesce is required, schedule the activation during a known low-traffic window and accept the brief unavailability.

**Quiesce is distinct from Circuit-breaker pause (§7.7).** Quiesce is a migration-atomicity mechanism internal to `0x09`'s upgrade flow, strictly bounded in duration (a single block of drain plus one atomic transaction), and always self-released — either by a successful migration or by the atomic rollback on failure. It is not a governance lever. The Council's pause authority operates on the separate `paused_actors` state and has its own lifecycle (§7.7).

### 7.5 Rollback slot

Until `rollback_deadline`, a Tier 3 fast-track proposal (§7.6) MAY revert `target` to the version in `rollback_slot` without re-validating the old bytecode (it was previously live). Rollback uses the same quiesce/atomic mechanism as forward migration. After the deadline, a rollback requires a full Tier 3 upgrade proposal pointing at the old `code_hash`.

### 7.6 Fast-track (Tier 3 only)

The Security Council MAY trigger fast-track on a submitted Tier 3 proposal by signing a 7-of-9 endorsement. Fast-track compresses **all three phases** — not just the timelock — and modestly raises the stake-approval threshold:

| Phase | Normal Tier 3 | Fast-track |
|---|---|---|
| Temperature check | 7 days | **Skipped** (Council endorsement substitutes) |
| Voting window | 7 days | **24 hours** |
| Timelock | 7 days | **6 hours** |
| Stake quorum | 15% | **15%** |
| Stake approval | > 60% | **> 66%** |
| Validator majority | > 50% | **> 50%** |

Total fast-track path: ~30 hours from Council endorsement to activation, vs. ~21 days normally. The approval bump (60% → 66%) is the only numerical protective bar; the real protection is the Council endorsement + the bicameral requirement. Fast-track is an emergency path, not an end-run around review — it must still pass both chambers, just on a compressed clock.

Fast-track is designed for known-bad-actor-code scenarios where a patched version exists and the community broadly agrees it should ship now. For scenarios where the patch does not yet exist or consensus is unclear, the Council's Circuit-breaker (§7.7) is the appropriate tool — it stops the bleeding while a normal Tier 3 proposal is prepared.

### 7.7 Circuit-breaker (Security Council only)

The Security Council MAY pause a system actor via a 7-of-9 signed `Circuit { target, action: Pause }` message to `0x09`. Effects:

- All messages to `target` revert with `ActorPaused`.
- `paused_actors[target]` records the pausing signers, the pause block, and an `expires_at` block (initially `pause_block + 7 days`).
- `0x09` **automatically creates** a Tier 3 ratification proposal on behalf of the pause:
  - Payload: `Circuit { target, action: RatifyExtend }`
  - Submitter: `0x09` (system-generated)
  - Deposit: 0 (waived for auto-generated ratifications)
  - Skips the temperature check and enters Voting immediately
  - Voting window: standard Tier 3 (7 days), or fast-track (24 hours) if the Council also endorses the ratification

**Pause durations are bounded, and Tier 3 extensions are capped.** Ratification does not produce an indefinite pause; it extends the pause by a bounded renewable period, and the number of consecutive Tier 3 extensions is capped so that continued freeze past the cap forces a constitutional decision at Tier 4:

- `paused_actors[target]` tracks an `extension_count: u32` alongside `expires_at`. `extension_count` starts at 0 when the pause is first invoked.
- A passing Tier 3 `RatifyExtend` proposal extends `expires_at` by `pause_extend_blocks` (Tier 0 param, default 30 days) **and increments `extension_count` by 1**.
- If `extension_count >= pause_tier3_extension_cap` (Tier 0 param, default **3**, i.e. ~90 days of cumulative Tier 3 extensions past the initial 7-day pause), further extensions MUST be Tier 4 `MetaGovernance { op: PauseExtendPermanent { target, duration_blocks } }` proposals. A Tier 3 extension attempted past the cap is rejected at the payload validation step, even if temperature check and voting thresholds are met.
- A Tier 4 `MetaGovernance { op: PausePermanent { target } }` proposal freezes the actor with no renewal schedule (this is the permanent freeze referenced in §5.1's scope table).
- A Tier 4 extension or permanent pause **resets `extension_count` to 0** if the Tier 4 vote also transitions the actor back to Tier 3 renewal (via a supplied `new_expires_at`); otherwise the permanent flag takes over and `extension_count` is no longer consulted.
- An **unpause** at any time requires a Tier 3 `Circuit { target, action: Unpause }` proposal. On successful unpause, `extension_count` is reset to 0 and `paused_actors[target]` is cleared.

- If the initial ratification proposal fails, or if `expires_at` is reached with no active extension proposal, the pause auto-reverts and the Council signers are flagged in the portal for community review.

**Exclusions.** `Circuit.target` MUST NOT be `0x09`. Pausing the Governance actor would deadlock governance itself (including the ratification proposal required to validate the pause). The ability to pause other liveness-critical actors is permitted but is a recognized weapon — a pause of `0x02 JobDispatcher`, for example, halts the runner marketplace — and is why ratification is mandatory, bounded, and renewable only at Tier 3 (for ongoing incident response) or Tier 4 (for constitutional freezes).

Pause is the Council's only unilateral protocol power and it is strictly time-limited without community ratification.

### 7.8 Cancellation authority and griefing cap

The Security Council MAY cancel a queued Tier 0–3 proposal during its timelock by submitting a 7-of-9 signed `Circuit { target: proposal_id, action: Cancel }` message to `0x09`. Cancellation is immediate: the proposal transitions to `Cancelled`, its deposit is refunded, and execution never occurs.

Cancellation itself is never rate-limited — the Council's ability to stop a queued proposal in a real emergency is preserved. But sustained use of the cancellation power triggers automatic community review:

**Storage (added to §4.1):**

```
council_cancellations: Vec<{ proposal_id: bytes32, cancelled_at: Block, signers: SignerSet }>
```

**Mechanism.** Let `cancellation_review_window_blocks` (Tier 0 param, default 90 days) and `cancellation_review_threshold` (Tier 0 param, default 3) be governance-tunable.

1. On every Council cancellation, `0x09` appends a record to `council_cancellations` and emits `CancellationExecuted { proposal_id, signers, block }`.
2. `0x09` then counts entries in `council_cancellations` whose `cancelled_at` falls within `(now − cancellation_review_window_blocks, now]`. Call this count `recent_count`.
3. If `recent_count >= cancellation_review_threshold`, `0x09` **automatically creates** a Tier 4 `MetaGovernance { op: ReviewCouncil }` proposal with:
   - Payload body referencing the `recent_count` cancellations and their `proposal_id`s
   - Submitter: `0x09` (system-generated)
   - Deposit: 0 (waived for auto-generated accountability proposals)
   - Standard Tier 4 voting parameters (§5.2)
4. The auto-generated proposal can pass with `ReviewOutcome::Affirm` (Council behavior judged appropriate; no action), `ReviewOutcome::Warn` (event logged, no removal), or `ReviewOutcome::Rotate` (replaces a named subset of Council signers; requires the replacement signer list in the payload).
5. While an auto-generated review proposal for the current Council is active, subsequent cancellations do **not** stack new review proposals — the existing review is extended to cover additional cancellations that occur during its lifecycle.

**Why not rate-limit cancellations directly.** A rate limit prevents the Council from responding to coordinated attacks (e.g., an attacker spamming Tier 3 upgrade proposals to exhaust cancellation budget). The escalation design preserves unlimited cancellation power for real emergencies while ensuring any pattern that looks like griefing triggers a community check, not a hardcoded veto.

**Cancellations vs Tier 4.** The Council cannot cancel Tier 4 proposals (§3.2). An attempted `Circuit { target: tier4_proposal_id, action: Cancel }` message is rejected at payload validation. This means the auto-generated `ReviewCouncil` proposal itself is not cancellable by the Council being reviewed.

### 7.9 Non-upgradeable elements

Certain protocol rules are **not** upgradeable via `0x09` and require a validator-coordinated hard fork:

- Block format, transaction format, signature schemes.
- Simplex BFT consensus rules.
- The `0x09` upgrade mechanism itself (Tier 4 can change `0x09` bytecode, but the execution layer must still recognize `0x09` as the authority — changing *that* is a fork).
- Genesis allocations.

These are listed so that proposers do not try to route them through CIP-12.

---

## 8. Portal and Discoverability

### 8.1 On-chain portal

The governance portal is a web application served directly from Cowboy:

- **Static assets** (HTML, JS, CSS) live in a public CBFS volume owned by the Labs Multisig.
- **Served by** a CIP-11 DNS-addressable gateway actor, also owned by the Labs Multisig.
- **Reads from** the `0x09` actor for all state; writes vote transactions on behalf of the connected wallet.
- **Runs anywhere**: the portal is a thin explorer + wallet bridge. Third parties can run their own portal pointed at the same `0x09` state; this is encouraged.

### 8.2 What the portal shows

- Active, queued, executed, and rejected proposals
- Proposal body rendered from CBFS `body_ref`
- Running stake-chamber tally, validator-chamber tally, and temperature-check tally, updated each block
- Voter list (address + stake + choice) for transparency
- Security Council membership and current signers on pending cancellations
- Live `params` table with history
- System actor version table with links to bytecode in CBFS

### 8.3 Portal upgrade path

Portal upgrades (UI, gateway actor bytecode) are signed by the Labs Multisig and do **not** go through governance. If the Labs Multisig is compromised, users can still:

1. Use the Cowboy CLI to submit votes and read proposal state.
2. Run a local portal from the CBFS volume at any historical hash.
3. Point a third-party portal at the same `0x09` contract.

The protocol itself is unaffected because the Labs Multisig has no `0x09` authority.

---

## 9. Open Questions

1. **Validator chamber weight ties.** With one-vote-per-validator, ties in the validator chamber are possible. Current rule: ties fail the validator majority check. Should a tie fall through to a Security Council tie-break? Probably not — failing closed is safer.
2. **Multi-choice ballot semantics.** First option above approval threshold, or highest-ranked option that clears a minimum? Default is the former.
3. **Minimum Security Council rotation cadence.** Is 2 seats per year enough? Should it be proportional to Council size?
4. **Portal censorship.** If the Labs Multisig delists a proposal from the default portal UI, is there a protocol-level mitigation or is "run your own portal" sufficient? Default: the latter.
5. **Delegation override cost.** Delegators who override their validator's vote pay a small fee (System lane). What fee? Default: basefee only, no premium.
6. **Liveness-critical pause targets beyond `0x09`.** §7.7 forbids pausing `0x09` but permits pausing other system actors. In practice some pauses are economically disruptive (e.g., `0x02` halts the runner marketplace, `0x06` would halt fee computation). Should additional actors be on a "pause-with-extra-approval" list requiring, say, stake-chamber signal as well as Council signatures? Current default: no, the 7-day auto-revert and mandatory ratification are sufficient protection.
7. **Migration quiesce duration caps.** §7.4 drains the message queue before migration. An actor with a very large queue could take many blocks to drain. Should there be a max-drain-blocks limit that aborts the migration if exceeded?

These are all parameters or policies that can be resolved in implementation without changing the core design.

---

## 10. Rationale

**Why bicameral?** Requiring both a stake chamber and a validator chamber means a proposal must be both economically and operationally defensible. Neither token holders nor validators alone can push through changes without the other's consent.

**Why a permanent Security Council?** The Council can be removed by the community via Tier 4, so it is not irrevocable. But it defaults to present because an emergency brake operated by named, accountable individuals is strictly safer than having no fast-response mechanism at all. The community opts in to removing its own safety net when ready — not on an arbitrary calendar date.

**Why separate the Foundation from protocol authority?** The Foundation is a legal entity with employees, bank accounts, and regulatory obligations. Granting it protocol powers creates capture risk and a regulatory target. The Security Council is a group of named individuals chosen for expertise and independence, none of whom hold the role by virtue of employment.

**Why build upgrades into `0x09`?** A separate `SystemActorUpgrader` actor would itself need an upgrade path, adding a trust boundary without reducing any. Keeping upgrades in `0x09` means the upgrade mechanism is protected by the same meta-governance rules as everything else.

**Why staked-only vote weight?** Unstaked CBY has no skin in the game and is cheap to accumulate (exchange floats, lost wallets). Staked CBY is committed to network security and earns/loses rewards based on network health. Requiring stake aligns voting power with long-term interest.

---

## 11. References

- Whitepaper §11 — summary of the governance model
- `node/runner/src/system_actors.rs` — canonical system actor addresses
- CIP-1, CIP-3, CIP-5, CIP-9, CIP-10 — sources of governance-tunable parameters
- CIP-11 — DNS-addressable gateway actor used by the portal frontend
