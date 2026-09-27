---
title: "CIP-13: Runner Stake Delegation (v2)"
description: Code-aligned v2 — opcode reassignment against the current protocol codec, explicit amendment to CIP-2 §5 VRF formula, slashing routing reuse, CIP-23 interaction
---

# CIP-13 v2

> **Versioning.** This is v2 of CIP-13. v1 is the canonical document `cip-13-runner-delegation.md` (preserved verbatim as Part I). v2 = v1 + the alignment revision (Part II).
>
> **Conflict rule:** Part II is canonical wherever it contradicts Part I.
>
> **Summary of v2 changes**
>
> - **Opcode renumbering (deferred to activation).** v1 §3.3's 40–44 collide with code (`UpdateSettlementConfig` through `UpgradeActor`); earlier v2 drafts proposed 44–48 (still in code's 44–51 band) and then 52–56 (now taken by CIP-8 Session). §1 below records the **code-authoritative** master table as of 2026-05-26 and lists CIP-13 delegation handlers as pending assignment against the current protocol codec; no open-ended numeric range is declared free. Concrete numbers will be pinned in the implementation PR that lands CIP-13 v2 handlers in `node/types/src/execution.rs`.
> - **Explicit amendment to CIP-2 §5/§6** for VRF selection and max-job-value: both now use `effective_stake = registration.stake + delegation_totals.total_active`. v1 §3.2 implies this; v2 states it as a normative cross-CIP amendment.
> - **Slashing routing** reuses CIP-3 `SettlementConfig.slash_*_percent` (governance-tunable) instead of hard-coding 50/50 treasury/burn. Per-tranche math is unchanged.
> - **CIP-23 interaction**: TEE eligibility is categorical (`measurement_binding.status`), not stake-thresholded. Delegation increases VRF weight but does not confer or remove TEE eligibility.
> - Carries forward v1 §6.1 zero-vote-weight rule for runner-delegated CBY.

---

## Part I — v1 Specification (verbatim from `cip-13-runner-delegation.md`)


<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-04-12
  **Requires:** CIP-12 (Governance)
</Note>

## 1. Abstract

This CIP adds **stake delegation** to the Cowboy runner marketplace. Any CBY holder can lock tokens on behalf of a registered runner, increasing the runner's effective stake (VRF selection weight and maximum job value) in exchange for a runner-configured share of the 89% settlement payout. The protocol provides only the delegation hook — registration, payout splitting, slashing cascade, and unbonding. Higher-order products (liquid staking pools, yield tokens, fleet management vaults) are built by third-party actors on top of this primitive.


---

## 2. Motivation

In a self-bonded protocol, a runner must hold `max(10,000 CBY, 1.5× declared_max_job_value)` in its own account to register (whitepaper §1.9, §5). This creates two problems:

1. **Capital lock-out.** The best GPU operators may not have large CBY positions. The largest CBY holders may not operate hardware. Self-bonding fuses capital and operations into one party, limiting both.

2. **No passive yield path.** CBY holders who are not runners have no way to earn runner marketplace revenue. 

Runner delegation decouples **capital supply** from **compute operations**, allowing each to specialize.

### 2.1 Design philosophy

The protocol provides the **minimal hook**. It does not implement pools, share tokens, diversification strategies, or yield wrappers. Following the precedent:

| Chain | Protocol provides | Ecosystem builds |
|-------|------------------|-----------------|
| Ethereum | Validator delegation, beacon chain rewards | Lido, Rocket Pool, Pendle, EigenLayer |
| Solana | Validator delegation, stake accounts | Jito, Marinade, Sanctum |
| Cowboy (this CIP) | Runner delegation, payout splitting | Liquid staking pools, stCBY, yield products |

---

## 3. Specification

### 3.1 New data structures

#### DelegationConfig

Added to `RunnerRegistration`. Runners opt into delegation by setting this field. The config carries the fields required by both the update flow (cooldown) and the settlement flow (epoch-delayed commission changes). All fields MUST be present; there is no ambiguity about where cooldown or pending commission data lives.

```
DelegationConfig {
    accept_delegation:            bool          // false by default; runner must opt in
    commission_bps:               u16           // current runner commission in basis points (0–10000)
    pending_commission_bps:       Option<u16>   // next commission, or None if no change queued
    pending_effective_epoch:      u64           // epoch at which `pending_commission_bps` becomes `commission_bps`;
                                                //   undefined when pending_commission_bps == None
    max_delegated_stake:          u64           // cap on total Active delegated CBY (0 = no cap)
    min_delegation:               u64           // minimum CBY per individual tranche
    last_updated:                 u64           // block height of the last RunnerUpdateDelegationConfig call;
                                                //   used to enforce DELEGATION_COOLDOWN_BLOCKS
}
```

`commission_bps` is the runner's cut of delegator-attributed revenue. If a runner earns 100 CBY on a job and 60% of the runner's effective stake is delegated, then 60 CBY is the delegator-attributed portion. The runner keeps `60 × commission_bps / 10000` and delegators share the remainder pro-rata.

**Epoch-delayed commission changes.** A runner submits `RunnerUpdateDelegationConfig` to queue a new commission. The update writes `pending_commission_bps = new_value` and `pending_effective_epoch = current_epoch + 1`. The old `commission_bps` remains authoritative for every settlement that finalizes in the current epoch. At the first settlement in epoch `pending_effective_epoch` or later, the runtime promotes the pending value into `commission_bps` and clears the pending fields (see §3.4 `effective_commission_for_epoch`). This ensures commission changes are deterministic, observable one full epoch in advance, and resolvable with only the state above — no auxiliary history table is required.

The non-commission fields (`max_delegated_stake`, `min_delegation`, `accept_delegation`) take effect immediately on update, because they only gate future transactions; no past settlement depends on them.

#### DelegationTranche

A delegator's position toward a single runner consists of one or more **tranches**. A tranche is an indivisible unit of delegated stake with its own lifecycle, status, and (when unbonding) its own claim timestamp. Partial undelegation, top-ups, and slashing all operate on tranches — never on a monolithic per-pair record.

```
DelegationTranche {
    delegator:       Address
    runner:          Address
    tranche_id:      u64          // monotonically increasing per (delegator, runner); assigned by the Registry
    amount:          u64          // CBY wei locked in this tranche (after any slashing)
    created_at:      u64          // block height at which this tranche entered its current status
    status:          TrancheStatus
    claimable_at:    Option<u64>  // Some iff status == Unbonding; the block height at which slashability ends and claimability begins
}

enum TrancheStatus {
    Active,           // counts toward effective stake, earns revenue, always slashable while Active
    Unbonding,        // does NOT count toward effective stake, does NOT earn revenue; see derived flags below
}
```

The Registry stores only two statuses; there is no separate `Claimable` state. Instead, **claimability** and **slashability** are computed as pure functions of `status`, `claimable_at`, and the current block height:

```
is_slashable(T, current_block)  := T.status == Active
                                OR (T.status == Unbonding AND current_block < T.claimable_at)

is_claimable(T, current_block)  := T.status == Unbonding
                                AND current_block >= T.claimable_at
```

These predicates are the authoritative definitions used by §3.4 (settlement), §3.6 (slashing), and `RunnerClaimUnbonded` (§3.3). Because they depend only on values already stored on the tranche and the block height at the moment of the operation, there is no state transition to schedule, no queue that could be delayed, and no window in which a tranche's slashability or claimability disagrees with its advertised `claimable_at`. This is a deliberate simplification over earlier drafts that stored `Claimable` as a persisted status; see §3.8 for the bookkeeping implications.

A delegator MAY hold multiple Active tranches against the same runner (each top-up creates a new tranche) and MAY simultaneously hold Unbonding tranches at various stages of maturity. All tranches are tracked separately so that amounts, timestamps, and slashing state remain unambiguous.

#### Storage layout

Within the `0x01 Runner Registry` system actor (see §9 of the whitepaper for system actor addresses):

```
delegation:{runner_hex}:{delegator_hex}:{tranche_id_be8}  →  DelegationTranche (JSON)
delegation_delegator_index:{runner_hex}:{delegator_hex}   →  Vec<tranche_id>   (JSON, all statuses)
delegation_runner_index:{runner_hex}                      →  Vec<(delegator_hex, tranche_id)> (JSON, all statuses)
delegation_tranche_counter:{runner_hex}:{delegator_hex}   →  u64 (next tranche_id to assign)
delegation_delegator_summary:{runner_hex}:{delegator_hex} →  DelegationDelegatorSummary (JSON)
delegation_totals:{runner_hex}                            →  DelegationTotals (JSON)
```

`DelegationDelegatorSummary` caches the per-(runner, delegator) counts and totals required by O(1) precondition checks:

```
DelegationDelegatorSummary {
    active_tranche_count:  u32   // number of this delegator's tranches with status == Active for this runner
    active_amount:         u64   // sum of this delegator's Active tranche amounts for this runner
}
```

The summary is maintained alongside `DelegationTotals` on every mutation (`Delegate`, `Increase`, `Undelegate`, `Claim`, slashing). When it would otherwise be zero/empty, the summary key is deleted to bound storage. `active_tranche_count` is the sole authoritative counter used to enforce `MAX_ACTIVE_TRANCHES_PER_DELEGATOR` in preconditions (§3.3); no instruction needs to iterate `delegation_delegator_index` to determine the count.

Active-delegator counts (at the runner level) are tracked in `DelegationTotals`: a delegator is counted once in `delegator_count` so long as `delegation_delegator_summary.active_tranche_count > 0`.

`DelegationTotals` is a cached aggregate to avoid iterating all tranches on every job dispatch or settlement:

```
DelegationTotals {
    total_active:     u64     // sum of amounts across all Active tranches
    delegator_count:  u32     // number of distinct delegators with active_tranche_count > 0
}
```

Only `total_active` is cached at the runner level because that is what VRF selection weight, max-job-value, and settlement all require. The slashable base is computed lazily by the slashing routine itself (§3.6), which must iterate tranches anyway to apply per-tranche slashes; adding a `total_slashable` cache would provide no speedup and would require block-height-dependent invariants that are hard to maintain correctly. Amounts in Unbonding status are recoverable by iterating `delegation_runner_index` when needed (indexer reads and deregistration only).

### 3.2 Effective stake

A runner's **effective stake** replaces the current `registration.stake` in all protocol-level calculations:

```
effective_stake = registration.stake + delegation_totals.total_active
```

Where `registration.stake` is the runner's self-bonded stake (unchanged from current behavior).

**VRF selection weight** (dispatcher.rs): the existing weight function `w = effective_stake · max(√(reputation / REPUTATION_NORMALIZER), w_min_floor)` (CIP-2 §5.3, `v3_runner_weight` in dispatcher.rs) uses `effective_stake` instead of `registration.stake`. Weight is **linear** in effective stake; the √reputation term (not stake compression) dampens pure-stake dominance.

**Maximum job value**: a runner's `rate_card.max_job_value` is bounded by `effective_stake × STAKE_JOB_MULTIPLIER_DENOM / STAKE_JOB_MULTIPLIER_NUM` (currently `effective_stake / 1.5`). Delegation directly increases a runner's capacity to accept higher-value jobs.

**Minimum self-bond**: runners MUST maintain `self_stake >= max(MIN_STAKE_CBY_WEI, effective_stake × MIN_SELF_BOND_BPS / 10000)`. This ensures the runner always has meaningful skin in the game. See §4.2 for the parameter value.

### 3.3 New system instructions

Five new `SystemInstruction` variants. Redelegation (moving stake atomically between runners) is intentionally deferred — it requires a dual-liability accounting model that is out of scope for v1 (see §9.5).

> **TODO (opcode reassignment):** The opcodes below (40–44) collide with currently-assigned System instructions in `node/types/src/execution.rs` (40 `UpdateSettlementConfig`, 41 `FundActor`, 42 `KeyDelivery`, 43 `UpgradeActor`). These values MUST be reassigned to verified unassigned slots in the pinned protocol codec (no numeric range in this draft is declared free) before implementation; the opcodes in the table are placeholders pending that reassignment. This does not affect the semantics specified elsewhere in this CIP.

| Instruction | Opcode | Sender | Description |
|-------------|--------|--------|-------------|
| `RunnerUpdateDelegationConfig` | 40 | Runner | Set or update `DelegationConfig` on the runner's registration |
| `RunnerDelegateStake` | 41 | Delegator | Lock CBY and create a new Active tranche on a runner |
| `RunnerIncreaseDelegation` | 42 | Delegator | Lock additional CBY and create a new Active tranche on a runner |
| `RunnerUndelegateStake` | 43 | Delegator | Initiate unbonding for some or all Active amount held against a runner |
| `RunnerClaimUnbonded` | 44 | Delegator | Withdraw CBY from tranches whose unbonding period has completed |

#### RunnerUpdateDelegationConfig

```
RunnerUpdateDelegationConfig {
    accept_delegation:    bool
    commission_bps:       u16          // proposed new commission
    max_delegated_stake:  u64
    min_delegation:       u64
}
```

Sender MUST be the runner itself (registration owner). Used both to opt in to delegation on first use and to adjust terms later. All four fields are always provided (no "partial update"); on first use, the prior `DelegationConfig` is treated as absent and the cooldown precondition is skipped.

**Preconditions:**
1. `RunnerRegistration` exists for `sender`.
2. Let `prev = registration.delegation_config`. If `prev` is present (i.e., the runner has configured delegation before), `block_height >= prev.last_updated + DELEGATION_COOLDOWN_BLOCKS`.
3. `MIN_COMMISSION_BPS <= commission_bps <= MAX_COMMISSION_BPS`.
4. `min_delegation >= MIN_DELEGATION_AMOUNT`.
5. If `max_delegated_stake > 0`, then `max_delegated_stake >= delegation_totals.total_active` (cannot retroactively break an existing cap).

**Effects:**
1. Compute the commission transition:
   - If `prev` is absent: `new_current = commission_bps`, `new_pending = None`. (First-use takes effect immediately; there is no prior revenue stream to protect.)
   - Otherwise if `commission_bps == prev.commission_bps` AND `prev.pending_commission_bps != Some(commission_bps)`: clear any queued change — `new_current = prev.commission_bps`, `new_pending = None`.
   - Otherwise (a genuine commission change): `new_current = prev.commission_bps`, `new_pending = Some(commission_bps)`, `new_pending_effective_epoch = current_epoch + 1`.
2. Write the resulting `DelegationConfig`:
   ```
   accept_delegation:       from tx
   commission_bps:          new_current
   pending_commission_bps:  new_pending
   pending_effective_epoch: new_pending_effective_epoch   (undefined when new_pending == None)
   max_delegated_stake:     from tx                       (takes effect immediately)
   min_delegation:          from tx                       (takes effect immediately)
   last_updated:            block_height
   ```
3. Emit `DelegationConfigUpdated { runner, accept_delegation, current_commission_bps: new_current, pending_commission_bps: new_pending, pending_effective_epoch, max_delegated_stake, min_delegation, block_height }`.

Replacing a still-pending change (queuing a new commission before the previous one has taken effect) is allowed but resets `pending_effective_epoch` to `current_epoch + 1` — this cannot short-circuit the one-epoch delay.

**Gas:** `UPDATE_DELEGATION_CONFIG_CYCLES` (governance-tunable, default 15,000 cycles, 500 cells).

#### RunnerDelegateStake

```
RunnerDelegateStake {
    runner: Address,
    amount: u64,
}
```

Creates a new Active tranche. Used both for a delegator's first stake to a runner and for subsequent top-ups; see §3.3.1 for the rule distinguishing this from `RunnerIncreaseDelegation`.

Let `summary = delegation_delegator_summary:{runner, sender}` (empty struct if key is absent).

**Preconditions:**
1. `RunnerRegistration` exists for `runner`, `health ∈ {Healthy, Paused}`, and `registration.delegation_config.accept_delegation == true`.
2. `amount >= max(MIN_DELEGATION_AMOUNT, registration.delegation_config.min_delegation)`.
3. If `registration.delegation_config.max_delegated_stake > 0`, then `delegation_totals.total_active + amount <= registration.delegation_config.max_delegated_stake`.
4. `sender_account.balance >= amount`.
5. `summary.active_tranche_count < MAX_ACTIVE_TRANCHES_PER_DELEGATOR`.
6. If `summary.active_tranche_count == 0`: `delegation_totals.delegator_count < MAX_DELEGATORS_PER_RUNNER`.

**Effects:**
1. `sender_account.balance -= amount`.
2. Allocate `tranche_id := delegation_tranche_counter:{runner,sender}++`.
3. Write `DelegationTranche { delegator: sender, runner, tranche_id, amount, created_at: block_height, status: Active, claimable_at: None }`.
4. Append `tranche_id` to `delegation_delegator_index:{runner, sender}`.
5. Append `(sender, tranche_id)` to `delegation_runner_index:{runner}`.
6. Update `delegation_delegator_summary:{runner, sender}`:
   - `active_tranche_count += 1`
   - `active_amount += amount`
7. Update `DelegationTotals`:
   - `total_active += amount`
   - If `summary.active_tranche_count` was 0 before this tx: `delegator_count += 1`.
8. Emit `DelegationCreated { delegator, runner, tranche_id, amount, block_height }`.

**Gas:** `DELEGATE_STAKE_CYCLES` (governance-tunable, default 25,000 cycles, 2,000 cells).

##### 3.3.1 Relationship between `Delegate` and `Increase`

`RunnerIncreaseDelegation` is semantically `DelegateStake` for a delegator who already holds at least one Active tranche (i.e., `summary.active_tranche_count > 0`) for the target runner. It is a separate opcode purely to make wire-level intent explicit and to let execution skip the `MAX_DELEGATORS_PER_RUNNER` check and the `delegator_count` bump.

Both instructions MUST enforce `MAX_ACTIVE_TRANCHES_PER_DELEGATOR` — it is the cap that bounds per-delegator settlement cost in §3.4, and it is always enforced by reading `summary.active_tranche_count`.

#### RunnerIncreaseDelegation

```
RunnerIncreaseDelegation {
    runner: Address,
    amount: u64,
}
```

Let `summary = delegation_delegator_summary:{runner, sender}`.

**Preconditions:**
1. Preconditions (1)–(5) of `RunnerDelegateStake` apply unchanged.
2. `summary.active_tranche_count >= 1` (the sender already has at least one Active tranche for this runner; otherwise `RunnerDelegateStake` is the correct instruction).

Note: unlike `RunnerDelegateStake`, this instruction does NOT check `MAX_DELEGATORS_PER_RUNNER` because the delegator already counts toward `delegator_count`.

**Effects:**
1. `sender_account.balance -= amount`.
2. Allocate a fresh `tranche_id` (existing tranches are never mutated — top-ups always create a new Active tranche so that per-tranche accounting remains unambiguous).
3. Write the new tranche, update indices.
4. Update `delegation_delegator_summary`: `active_tranche_count += 1`, `active_amount += amount`.
5. Update `DelegationTotals`: `total_active += amount`; `delegator_count` unchanged.
6. Emit `DelegationIncreased { delegator, runner, tranche_id, amount, block_height }`.

**Gas:** `INCREASE_DELEGATION_CYCLES` (governance-tunable, default 20,000 cycles, 1,500 cells).

#### RunnerUndelegateStake

```
RunnerUndelegateStake {
    runner: Address,
    amount: u64,
}
```

Initiates unbonding of `amount` CBY from the sender's delegation to `runner`. Amount is drawn from the sender's Active tranches in FIFO order by `created_at` (oldest first; ties broken by ascending `tranche_id`), splitting the last tranche touched if necessary. Each tranche drawn from contributes either its full amount (that tranche transitions to Unbonding) or a partial amount (the tranche is split: the remaining Active portion keeps its original `tranche_id`; the split-off Unbonding portion receives a fresh `tranche_id`).

Let `summary = delegation_delegator_summary:{runner, sender}`.

**Preconditions:**
1. `summary.active_tranche_count >= 1`.
2. `1 <= amount <= summary.active_amount`.
3. The residual Active balance `summary.active_amount - amount` MUST be either `0` OR `>= max(MIN_DELEGATION_AMOUNT, registration.delegation_config.min_delegation)` (prevents dust Active balances).

**Effects:**
1. Walk the sender's Active tranches in FIFO order. Let `r = amount`. For each Active tranche `t` iterated:
   - If `r >= t.amount` (consume the whole tranche): set `t.status = Unbonding`, `t.created_at = block_height`, `t.claimable_at = block_height + UNBONDING_BLOCKS`. Decrement `r -= t.amount` and `summary.active_tranche_count -= 1`.
   - Otherwise (`r < t.amount`, final partial split): reduce `t.amount -= r`; the tranche keeps `status == Active` with its original `tranche_id`. Allocate a fresh `tranche_id` and write a new tranche `u` with `amount = r`, `status = Unbonding`, `created_at = block_height`, `claimable_at = block_height + UNBONDING_BLOCKS`; append `u.tranche_id` to the delegator and runner indices. Set `r = 0`. `summary.active_tranche_count` is unchanged by the split.
2. Update `summary.active_amount -= amount`. If `summary.active_tranche_count == 0`, delete the `delegation_delegator_summary` key.
3. Update `DelegationTotals`: `total_active -= amount`. If the sender's `summary.active_tranche_count` fell from `>0` to `0` in this tx, `delegator_count -= 1`.
4. Emit `UndelegationInitiated { delegator, runner, amount, tranche_ids, claimable_at }` listing every tranche that transitioned to or was created as Unbonding.

No queue insert is performed. There is no scheduled transition: a tranche's slashability and claimability are computed functions of `claimable_at` and the current block height (§3.1).

**During unbonding**, an Unbonding tranche:
- Does NOT count toward `effective_stake` for VRF selection or job value limits (§3.2).
- Does NOT earn revenue share (§3.4 restricts payouts to Active tranches).
- IS slashable while `current_block < claimable_at`, and ceases to be slashable at exactly `claimable_at`. This prevents slash-and-run.

**Gas:** `UNDELEGATE_STAKE_BASE_CYCLES + UNDELEGATE_STAKE_PER_TRANCHE_CYCLES × tranches_touched` (defaults: 20,000 base + 5,000 per tranche).

#### RunnerClaimUnbonded

```
RunnerClaimUnbonded {
    runner: Address,
    tranche_ids: Vec<u64>,   // 1..=CLAIM_MAX_TRANCHES
}
```

The delegator supplies the specific tranche IDs to claim. This is explicit (rather than "claim everything claimable") so that gas consumed by the instruction is bounded and predictable, and so that partial claims are first-class. Tranche IDs can be read from the indexer (§7.2) or computed locally by the delegator.

**Preconditions:**
1. `1 <= tranche_ids.len() <= CLAIM_MAX_TRANCHES` (default: 32).
2. For each `tranche_id`:
   - `DelegationTranche { delegator: sender, runner, tranche_id, .. }` exists.
   - `tranche.status == Unbonding` AND `block_height >= tranche.claimable_at` (equivalently: `is_claimable(tranche, block_height)` per §3.1).

**Effects:**
1. Let `total = Σ tranche.amount` over the supplied tranches.
2. `sender_account.balance += total`.
3. For each supplied tranche: delete the `DelegationTranche` record, remove its `tranche_id` from `delegation_delegator_index:{runner, sender}` and `delegation_runner_index:{runner}`.
4. `DelegationTotals` is unchanged (`total_active` was decremented at undelegation time; there is no cached `total_unbonding` to update). `delegator_count` is unchanged by this instruction (it tracks delegators with `active_tranche_count > 0`, not delegators with any remaining tranche).
5. `delegation_delegator_summary:{runner, sender}` is unchanged (summary tracks Active only).
6. Emit `DelegationClaimed { delegator, runner, tranche_ids, amount: total }`.

**Gas:** `CLAIM_UNBONDED_BASE_CYCLES + CLAIM_UNBONDED_PER_TRANCHE_CYCLES × tranche_ids.len()` (defaults: 10,000 base + 3,000 per tranche).

Because claimability is a pure function of block height and tranche state, there is no scenario in which a tranche that has passed its `claimable_at` cannot be claimed — the instruction can be submitted at any block `≥ claimable_at` without a separate pre-processing pass.

### 3.4 Settlement payout splitting


```
per_runner = runner_share_total / num_consensus_runners
```

With delegation, the Result Verifier (`0x03`) is modified. Each runner's portion is split between the runner and their delegators. Revenue is attributed only to **Active** tranches; Unbonding tranches earn no revenue (per §3.3).

**Resolving the effective commission.** Because commission changes are epoch-delayed (§3.3 `RunnerUpdateDelegationConfig`), settlement reads the authoritative rate via:

```
effective_commission_for_epoch(R, epoch):
    cfg = R.registration.delegation_config
    if cfg.pending_commission_bps is Some(v) and epoch >= cfg.pending_effective_epoch:
        // Promote: the pending change has matured. This is a lazy one-shot write.
        cfg.commission_bps         = v
        cfg.pending_commission_bps = None
        cfg.pending_effective_epoch = undefined
        persist(cfg)
        return v
    return cfg.commission_bps
```

Promotion is idempotent: the first settlement in the new epoch writes the new `commission_bps` and clears the pending fields; subsequent settlements in the same block see the clean state. A settlement that finalizes in any epoch strictly before `pending_effective_epoch` reads `cfg.commission_bps` unchanged.

```
for each consensus runner R:
    per_runner_share = runner_share_total / num_consensus_runners

    R_self_stake     = R.registration.stake
    R_delegated      = R.delegation_totals.total_active   // Active only
    R_effective      = R_self_stake + R_delegated
    R_commission_bps = effective_commission_for_epoch(R, current_epoch)

    // Delegator-attributed portion of this job's share
    delegator_pool = per_runner_share × R_delegated / R_effective

    // Runner's commission on delegator-attributed revenue
    runner_commission = delegator_pool × R_commission_bps / 10000

    // Runner total = self-stake portion + commission
    runner_payout = per_runner_share - delegator_pool + runner_commission
    credit(R.address, runner_payout)

    // Distribute remainder pro-rata across ACTIVE tranches
    delegator_distributable = delegator_pool - runner_commission
    for each Active tranche T of R:
        T_share = delegator_distributable × T.amount / R_delegated
        credit(T.delegator, T_share)
```

Note that the pro-rata weight is the tranche `amount`, not the per-delegator total. A delegator with multiple Active tranches is paid proportionally on each, which falls out of the sum-of-tranches equalling the per-delegator total.

**Integer rounding:** any remainder from the runner-vs-delegator split accrues to the runner. Any remainder from the per-tranche distribution accrues to the lowest `tranche_id` among Active tranches. This keeps the total exactly equal to `runner_share_total` with no stray dust.

**Gas impact:** with `T` Active tranches per runner and `M` consensus runners, settlement performs `M × T` additional balance writes. The per-runner tranche count is bounded by `MAX_DELEGATORS_PER_RUNNER × MAX_ACTIVE_TRANCHES_PER_DELEGATOR` (see §4.2). At the defaults (200 × 8 = 1,600 tranches per runner), a 5-runner consensus pays out 8,000 tranche writes in the worst case, well within a single block's system lane budget (`LANE_SYSTEM_CYCLES = 25,000,000`).

### 3.5 Settlement events

The `0x03` system actor MUST emit structured events on every job settlement. These events are the canonical data source for delegation yield tracking and downstream compute finance products.

> **Implementation status (2026-07).** The **settlement** events below (`JobSettled` / `DelegatorPayout`) are **not yet emitted** — the Result Verifier settlement path is `&self` and cannot push events; `settle_delegated_split` returns the payout data for a future event-wiring pass. The **delegation-lifecycle** events, by contrast, **are** emitted today under a `cip13.` topic namespace: `cip13.delegation.created` / `cip13.delegation.increased` (DelegateStake / IncreaseDelegation), `cip13.delegation_config.updated`, `cip13.undelegation.initiated`, and `cip13.delegation.claimed` (`node/execution/src/runner/delegation.rs`). These topic names are consensus-visible (they enter `receipt_root`) and differ from the bare `JobSettled`/`DelegatorPayout` names used in this section.

#### JobSettled event

Emitted once per job settlement:

```
topic: "JobSettled"
data: {
    job_id:               bytes32
    block_height:         u64
    entitlement_class:    string      // derived from job_type + tee_required + model_id
    model_id:             bytes32     // zero if not an LLM job
    tee_required:         bool
    verification_mode:    u8
    total_settlement:     u64         // max_price + tip
    runner_share_total:   u64         // 89% portion
    burn_share:           u64         // 10% portion
    treasury_share:       u64         // 1% portion
    consensus_runners:    u32         // count
    submitter:            Address
}
```

Cost: 50 + data_length cells (existing `emit_event` pricing).

#### DelegatorPayout event

Emitted once per delegator payment within a settlement:

```
topic: "DelegatorPayout"
data: {
    job_id:       bytes32
    runner:       Address
    delegator:    Address
    amount:       u64
    block_height: u64
}
```

To avoid O(N) events per job for large delegator sets, this event MAY be batched as a single event with a list of `(delegator, amount)` pairs when the delegator count exceeds `DELEGATION_EVENT_BATCH_THRESHOLD` (default: 20).

### 3.6 Slashing cascade

When a runner is slashed for dishonesty (fabricated results, wrong model under TEE), the slash is distributed proportionally across the runner's self-stake **and every slashable tranche** under the `is_slashable(T, current_block)` predicate from §3.1. Slashable tranches are: all Active tranches, plus all Unbonding tranches whose `claimable_at > current_block`. Unbonding tranches that have already reached `claimable_at` are no longer slashable — they have aged out of the slashing window — even though the delegator has not yet submitted `RunnerClaimUnbonded`.

Because the slashable base depends on `current_block` (and matures lazily as blocks advance), there is no cached `total_slashable`. The slashing routine computes the base on the fly during its iteration over tranches:

```
slash_runner_with_delegation(runner, requested_slash, current_block):
    // Single pass: collect every slashable tranche and sum its amount.
    slashable_tranches = []
    delegated_slashable = 0
    for each tranche T in delegation_runner_index:{runner}:
        if is_slashable(T, current_block):
            slashable_tranches.append(T)
            delegated_slashable += T.amount

    base = runner.self_stake + delegated_slashable
    if base == 0:
        return  // nothing to slash

    // Per-epoch cap on delegator liability
    cap = delegated_slashable × MAX_DELEGATION_SLASH_PER_EPOCH_BPS / 10000
    delegation_headroom = max(0, cap - epoch_slashed_so_far(runner))

    // Self-stake proportional slice (self-stake is NEVER capped — full liability)
    self_slash = floor(requested_slash × runner.self_stake / base)
    self_slash = min(self_slash, runner.self_stake)
    runner.self_stake -= self_slash

    // Delegator proportional slice, capped by per-epoch headroom
    delegation_slash_uncapped = requested_slash - self_slash
    delegation_slash = min(delegation_slash_uncapped, delegation_headroom)

    // Apply per-tranche
    actual_delegation_slash = 0
    affected = []
    if delegated_slashable > 0 and delegation_slash > 0:
        for T in slashable_tranches:
            T_slash = floor(delegation_slash × T.amount / delegated_slashable)
            T_slash = min(T_slash, T.amount)
            T.amount -= T_slash
            actual_delegation_slash += T_slash
            if T_slash > 0:
                affected.append(T.tranche_id)
            // If T.amount drops to 0: delete the tranche record and remove from indices

    // Update cached totals. For each mutated tranche, adjust total_active (if T was Active)
    // and, for Active tranches owned by some delegator, adjust that delegator's summary
    // (active_amount and, if T was deleted, active_tranche_count). Then decrement
    // DelegationTotals.delegator_count for any delegator whose active_tranche_count
    // reached 0.
    recompute_delegation_totals_and_summaries(runner, slashable_tranches)

    epoch_slashed_so_far(runner) += actual_delegation_slash

    // Uncapped remainder is NOT deferred to a future epoch.
    uncapped_shortfall = delegation_slash_uncapped - actual_delegation_slash
    if uncapped_shortfall > 0:
        emit("DelegationSlashCapped", { runner, shortfall: uncapped_shortfall, epoch: current_epoch })

    // Route slashed funds: 50% treasury, 50% burn (unchanged; applies to actually-slashed amount)
    slashed_total = self_slash + actual_delegation_slash
    route_to_treasury(slashed_total / 2)
    route_to_burn(slashed_total - slashed_total / 2)

    emit("RunnerSlashed", {
        runner,
        self_slashed:        self_slash,
        delegation_slashed:  actual_delegation_slash,
        tranches_affected:   affected,
        current_block,
    })
```

**Why is the slashable base computed lazily?** Whether an Unbonding tranche is slashable depends on `current_block < claimable_at`, which changes each block. Caching a `total_slashable` would require a per-block invariant maintenance pass (the exact thing the prior draft's overflow problem was caused by) with no performance benefit — slashing already iterates all tranches to apply per-tranche reductions.

**Why all slashable tranches, not just Active?** A delegator who initiated unbonding at block `B` and sees the runner misbehave at block `B+10` must not be able to front-run the resulting slash transaction. Unbonding tranches remain slashable until `claimable_at` — which is the full `UNBONDING_BLOCKS` window, same as advertised when the delegator initiated unbonding.

**Rounding:** per-tranche `floor` division always under-slashes slightly; the rounding residue remains with the delegator. Per CIP principle ("never over-slash under ambiguity"), this is the safe direction.

**Per-epoch cap (`MAX_DELEGATION_SLASH_PER_EPOCH_BPS`, default 500 = 5%):** the cap is a **ceiling on cumulative delegator liability per epoch**, not a deferred-slash queue. Runner self-stake is NOT subject to this cap — misconduct always fully slashes the self-bond; the cap exists only to bound cascading damage to passive capital. If a runner's self-stake is exhausted and further slashing would exceed the delegator cap, the excess is not reapplied in a future epoch. This is a deliberate design choice: uncapped slashes across epochs create cascading-attack surface, and passive capital should have a predictable per-epoch floor.

### 3.7 Runner deregistration with active delegations

`RunnerDeregister` MUST handle delegations. There is no unbonding queue in this CIP (§3.8); deregistration coordinates solely by writing `claimable_at` values onto the relevant records.

1. The runner MAY first set `accept_delegation = false` (via `RunnerUpdateDelegationConfig`) and wait for all delegators to undelegate voluntarily, OR
2. The runner MAY submit `RunnerDeregister`, which **force-initiates unbonding** for all Active tranches on the runner. For each Active tranche `T` held against the runner:
   - Set `T.status = Unbonding`, `T.created_at = block_height`, `T.claimable_at = block_height + UNBONDING_BLOCKS`.
   - Decrement `total_active` by `T.amount` and update each affected delegator's `DelegationDelegatorSummary` (`active_tranche_count`, `active_amount`).
   - When a delegator's `active_tranche_count` drops to zero, decrement `DelegationTotals.delegator_count` and delete the summary key.
3. The runner's **self-stake** enters its own unbonding window by writing `registration.self_stake_unbonding_claimable_at = block_height + UNBONDING_BLOCKS` on the `RunnerRegistration`. The self-stake amount remains in the registration (not on the runner's balance) until claimed; it is slashable while `block_height < self_stake_unbonding_claimable_at` per the same rule that applies to tranches (§3.1).
4. `registration.health` transitions to `Deregistered` immediately. The runner is excluded from VRF selection and receives no further settlements.
5. Delegators claim their matured tranches via `RunnerClaimUnbonded` at any block `≥ T.claimable_at`. The runner claims self-stake via the separate runner registration lifecycle flow at any block `≥ self_stake_unbonding_claimable_at`.

No queue insertion is performed by any step. Maturity for every unbonding record — delegator tranches and the runner's self-stake — is governed by the pure block-height rule specified in §3.1 and §3.8.

### 3.8 Unbonding maturity (no state transition required)

Earlier drafts of this CIP scheduled an end-of-block pass that mutated Unbonding tranches into a separate `Claimable` state and maintained a global `unbonding_queue` for this purpose. That design created an overflow hazard: if the pass could not process every matured tranche in a single block's cycle budget, some tranches would remain `Unbonding` past their advertised `claimable_at`, extending their slashability window beyond what was promised and blocking claims until the queue caught up.

This CIP resolves the hazard by **removing the scheduled transition entirely**. A tranche's `claimable_at` is the authoritative moment at which it ceases to be slashable (§3.6) and becomes claimable (`RunnerClaimUnbonded`, §3.3). Both properties are pure functions of `current_block` and `tranche.claimable_at` — they flip atomically at the block boundary with no processing required, no queue to drain, no budget to exhaust. There is no `unbonding_queue` storage key.

Consequences:

- **No window where `claimable_at` is passed but claiming is blocked.** The `RunnerClaimUnbonded` precondition is satisfied by any block `≥ claimable_at`.
- **No window where a tranche is slashable past `claimable_at`.** §3.6's `is_slashable` predicate returns `false` at exactly `claimable_at`.
- **Runner deregistration force-unbonding** (§3.7) sets `claimable_at = block_height + UNBONDING_BLOCKS` on each affected tranche; nothing else needs to be scheduled.
- **Indexers** SHOULD index tranches by `(runner, claimable_at)` to answer "what is claimable now?" queries efficiently, but this is an indexer concern, not a consensus concern.

Runners' **own** self-stake unbonding (initiated via `RunnerDeregister`) is bookkept with the runner's registration record rather than as a tranche. Its maturity is governed by the same block-height-derived rule: self-stake is slashable while `block_height < runner.self_stake_unbonding_claimable_at` and claimable at or after that block.

---

## 4. Parameters

All parameters are governance-tunable via CIP-12 Tier 0 proposals.

### 4.1 Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `UNBONDING_BLOCKS` | 86,400 (~24 hours at 1s blocks per WP §6.1) | Blocks between undelegation initiation and claim eligibility |
| `DELEGATION_COOLDOWN_BLOCKS` | 7,200 (~2 hours at 1s blocks) | Minimum blocks between `RunnerUpdateDelegationConfig` calls by a runner |

### 4.2 Stake limits

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_SELF_BOND_BPS` | 1000 (10%) | Runner self-stake as percentage of effective stake. A runner with 100K effective stake must self-bond at least 10K. |
| `MAX_DELEGATORS_PER_RUNNER` | 200 | Cap on distinct Active delegators per runner. Enforced in `RunnerDelegateStake` preconditions. Bounds settlement gas. |
| `MAX_ACTIVE_TRANCHES_PER_DELEGATOR` | 8 | Cap on Active tranches a single delegator may hold against a single runner. Bounds per-delegator settlement work and protects against tranche-spam. Enforced in `RunnerDelegateStake` and `RunnerIncreaseDelegation` preconditions. |
| `MIN_DELEGATION_AMOUNT` | `1,000 CBY × 10^9 wei` | Protocol-level floor. Runners may set higher via `DelegationConfig.min_delegation`. |
| `CLAIM_MAX_TRANCHES` | 32 | Maximum tranches claimable in a single `RunnerClaimUnbonded` call. Bounds instruction gas. |

### 4.3 Slashing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_DELEGATION_SLASH_PER_EPOCH_BPS` | 500 (5%) | Maximum percentage of delegated stake that can be slashed per runner per epoch |
| `SLASH_DELEGATION_ROUTING` | 50/50 treasury/burn | Same routing as self-stake slashing |

### 4.4 Revenue

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_COMMISSION_BPS` | 500 (5%) | Minimum runner commission. Prevents race-to-zero that harms runner economics. |
| `MAX_COMMISSION_BPS` | 10000 (100%) | Maximum runner commission. 100% = runner keeps everything, delegation is purely for stake weight. |
| `DELEGATION_EVENT_BATCH_THRESHOLD` | 20 | Delegator count above which `DelegatorPayout` events are batched |

---

## 5. Entitlement interaction

Cowboy runners are heterogeneous. A runner supporting TEE + Llama 405B earns revenue only from jobs requesting those capabilities. Delegators are implicitly betting on the demand profile of the runner's entitlement set.

**The protocol does not resolve this.** Delegators SHOULD evaluate a runner's capabilities, historical job volume, and entitlement coverage before delegating. The settlement events (§3.5) provide the data for this evaluation.

Higher-order products may be created that address the entitlement problem:

- **Fleet-as-a-Fund actors** (third-party): accept CBY deposits, delegate across a diversified set of runners spanning multiple entitlement classes, issue CIP-20 share tokens. The actor operator manages fleet allocation.
- **Entitlement-specific pools** (third-party): pools scoped to a single entitlement class ("TEE H100 Pool"). Delegators choose which compute segment to back.
- **Settlement event dashboards** (third-party or Watchtower): per-entitlement-class demand aggregations help delegators assess demand before committing capital.

---

## 6. Interaction with CIP-12 governance

### 6.1 Vote weight of runner-delegated stake (v1: zero)

CIP-12 §6.2 defines stake-chamber vote weight as **CBY staked to validators** (self-stake and validator delegation both count). Validator staking is an operational, consensus-layer commitment; runner delegation is an economic, compute-market commitment. These are distinct systems, and CIP-12's voting design is not set up to mix them.

For v1 of this CIP, runner-delegated CBY has **zero governance vote weight**. The CBY is locked and economically productive, but it carries no political voice while it is attributed to a runner. A CBY holder who wants both governance voice and runner yield must split their holdings between the two systems. This resolves cleanly against CIP-12's "staked CBY has weight, unstaked has zero" rule: runner-delegated CBY is neither validator-staked nor unstaked; it simply occupies a third category with no voting rights in v1.

A future CIP MAY extend CIP-12 to grant vote weight to runner-delegated stake (voted directly by the delegator, not inherited by the runner). That extension is deliberately out of scope here to keep the governance surface stable.

### 6.2 Governance-tunable parameters

All parameters in §4 are registered in the `0x09 Governance` actor's `params` store at genesis. Changes require a Tier 0 proposal (CIP-12 §5.1).

The settlement config (`runner_percent`, `burn_percent`, `treasury_percent`) is already governance-tunable per CIP-12; this CIP does not alter it.

---

## 7. Implementation notes

### 7.1 Changes to existing code

**`node/runner/src/types.rs`:**
- Add `delegation_config: Option<DelegationConfig>` field to `RunnerRegistration`
- Add `DelegationTranche`, `DelegationTotals`, `DelegationConfig`, `TrancheStatus` types
- Serde implementations for all new types

**`node/types/src/execution.rs`:**
- Add `SystemInstruction` variants for opcodes 40–44 (§3.3)
- Codec (Encode/Decode) implementations for new instructions

**`node/execution/src/runner/registry.rs`:**
- `handle_runner_update_delegation_config`: opcode TBD (see §1 master table; v1 draft 40 collides with code's `UpdateSettlementConfig`, must use a verified unassigned opcode in the current protocol codec); validate cooldown, commission bounds, min_delegation floor, cap non-regression
- `handle_runner_delegate_stake`: opcode TBD (v1 draft 41 collides with `FundActor`); validate preconditions (including `MAX_DELEGATORS_PER_RUNNER` check), lock CBY, allocate tranche_id, write tranche, update indices and totals
- `handle_runner_increase_delegation`: opcode TBD (v1 draft 42 collides with `KeyDelivery`); same as delegate but skips `delegator_count` bump and the `MAX_DELEGATORS_PER_RUNNER` check
- `handle_runner_undelegate_stake`: opcode TBD (v1 draft 43 collides with `UpgradeActor`); FIFO consume Active tranches, transition or split to Unbonding, schedule in unbonding queue
- `handle_runner_claim_unbonded`: opcode TBD (v1 draft 44 collides with `UpdateBasefeeConfig`); validate `tranche.status == Unbonding && block_height >= tranche.claimable_at` for every supplied `tranche_id`, release CBY, remove tranches from indices
- Modify `handle_runner_register` to initialize empty `DelegationTotals` and tranche counter namespace on registration
- Implement `handle_runner_deregister` (currently `UnsupportedInstruction`) with force-unbonding of all Active tranches per §3.7

**`node/execution/src/runner/dispatcher.rs`:**
- Modify VRF weight calculation to use `effective_stake` (self_stake + delegation_totals.total_active)
- Modify max job value check to use `effective_stake`

**`node/execution/src/runner/verifier.rs`:**
- Modify settlement to read delegation records and split payouts per §3.4
- Add `emit_event` calls for `JobSettled` and `DelegatorPayout` events per §3.5
- Modify `slash_runner` to cascade to delegators per §3.6

**`node/execution/src/gas.rs`:**
- Add gas costs for new instructions

**`node/storage/src/`:**
- Delegation state lives entirely under the `0x01 Runner Registry` actor. No new storage subsystem is introduced. There is **no** unbonding queue: §3.8 specifies that unbonding maturity is a pure function of block height and per-tranche `claimable_at`, so no scheduled processing pass is required.

**No end-of-block hook is required for delegation.** Every previously-scheduled effect (status transition, totals decrement) is handled inline: `RunnerClaimUnbonded` validates maturity on-demand from `block_height` and `tranche.claimable_at`; `slash_runner_with_delegation` recomputes the slashable base from the current block during its iteration. This is an explicit design choice to avoid the overflow/timing hazards of a scheduled transition pass.

### 7.2 RPC additions

The indexer (`node/indexer/`) SHOULD expose:

- `GET /runners/{address}/delegations` — list all tranches (all statuses) grouped by delegator for a runner
- `GET /accounts/{address}/delegations` — list all tranches (all statuses) grouped by runner for a delegator, including each tranche's `tranche_id`, `amount`, `status`, and `claimable_at` (required for clients building `RunnerClaimUnbonded` transactions)
- `GET /runners/{address}/delegation_stats` — totals, APY estimate, effective commission
- `GET /delegations/unbonding` — global unbonding queue (useful for liquid staking protocols)

---

## 8. Security considerations

### 8.1 Slash-and-run prevention

Unbonding tranches are slashable per §3.1's `is_slashable` predicate: a tranche remains in the slashing base while `current_block < tranche.claimable_at`. Because slashability is derived from block height rather than from a scheduled state transition, there is no processing-queue latency and no window in which a tranche is promoted out of the slashable base earlier than its advertised `claimable_at`. A delegator cannot front-run a slash by undelegating.

### 8.2 Storage griefing via delegation

A malicious actor could split a position into many small tranches to inflate storage, or spray small delegations across many runners. Mitigated by:
- `MIN_DELEGATION_AMOUNT` protocol floor (1,000 CBY) enforced in `RunnerDelegateStake` / `RunnerIncreaseDelegation` preconditions
- `delegation_config.min_delegation` runner-set floor
- `MAX_ACTIVE_TRANCHES_PER_DELEGATOR` cap (default 8) bounds per-delegator tranche count for a single runner
- `MAX_DELEGATORS_PER_RUNNER` cap (default 200) bounds delegator count per runner
- Gas costs for delegation instructions (delegate, increase, undelegate) that scale with tranche touches

### 8.3 Commission manipulation

A runner could set commission to 0%, attract delegators, then raise commission to 100%. Mitigated by:
- `DELEGATION_COOLDOWN_BLOCKS` between config changes
- Commission updates take effect at epoch boundary, not immediately
- Delegators can monitor `RunnerUpdateDelegationConfig` events and undelegate before the new rate applies

### 8.4 Concentration risk

VRF weight is linear in effective stake (CIP-2 §5.3), so delegation scales selection probability proportionally; the √reputation factor — not stake compression — tempers concentration (a 4× reputation gap yields only a 2× weight gap).

### 8.5 Cascading slashes in multi-runner pools

A third-party pool actor that delegates across multiple runners faces correlated slash risk if multiple runners misbehave in the same epoch. The per-epoch cap (§3.6) limits exposure per runner, but pool-level risk management is the pool operator's responsibility, not the protocol's.

---

## 9. Future work

### 9.1 Liquid staking token (stCBY)

A reference pool actor that accepts CBY, delegates across a diversified runner set, and mints a CIP-20 share token (stCBY) is a natural first product built on this CIP. The protocol does not specify this — it is an actor-layer concern.

### 9.2 Prepaid compute forwards

CIP-13 delegation enables the supply side of compute forwards: a runner (or runner pool) with sufficient effective stake can commit capacity for forward delivery. The buyer deposits CBY into a forward contract actor; the runner accepts, backing the commitment with delegated + self-bonded stake. See the compute finance roadmap for design details.

### 9.3 Compute demand indices

The `JobSettled` events (§3.5) provide the raw data for any downstream compute demand index or oracle product. Third-party Watchtower feed actors can aggregate these events into per-epoch indices by entitlement class, model, or TEE status. The `DelegatorPayout` events enable yield tracking products. This CIP provides the data; aggregation is an actor-layer concern.

### 9.4 Delegation marketplace

A future CIP or actor may provide an on-chain marketplace where runners advertise delegation terms and delegators can compare runners by commission, entitlements, historical yield, uptime, and slashing history. This is analogous to validator explorer dashboards on Ethereum/Solana.

### 9.5 Atomic redelegation

An earlier draft of this CIP included a `RunnerRedelegateStake` instruction that atomically moved stake from one runner to another without a full unbonding cycle. It was removed before merging because a correct implementation requires **dual liability accounting**: until the source runner's slash-window expires, the moved stake must be simultaneously slashable by both the source runner (for misconduct that occurred before the redelegation) and the destination runner (for future misconduct) — while the underlying CBY exists only once. Getting the state model, slashing math, and per-epoch cap semantics right for dual liability is a meaningful spec exercise on its own and would bloat v1.

For v1, delegators who wish to move stake between runners MUST undelegate, wait `UNBONDING_BLOCKS` (~24h), and then delegate to the new runner. Liquid staking pool actors can smooth this over for end users by maintaining a reserve buffer. A future CIP MAY introduce atomic redelegation with a proper dual-liability model (Cosmos's "redelegation hop" is the nearest precedent).

### 9.6 Governance vote weight for delegated stake

As noted in §6.1, runner-delegated CBY has zero governance vote weight in v1. A future CIP may extend CIP-12 to grant vote weight to runner-delegated stake (voted directly by the delegator, not inherited by the runner), after live operation of CIP-13 clarifies whether such weight is desired and how to weigh it against validator-delegated stake.

---

## 10. Rationale

**Why runner-configurable commission?** A protocol-fixed rate would either overpay runners (discouraging delegation) or underpay them (discouraging runner operations). The marketplace approach (io.net, Fluence) lets supply and demand set the rate. The `MIN_COMMISSION_BPS` floor prevents a race-to-zero that would harm runner sustainability.

**Why 24-hour unbonding (not 7 days)?** Compute marketplaces are faster-moving than PoS consensus. Runner capabilities change as hardware is added or removed, demand shifts between entitlement classes, and runners can deregister on short notice. A 7-day unbonding (standard in PoS) would lock delegator capital through multiple demand regime changes. 24 hours provides sufficient slash protection (the dispute window is 15 minutes per whitepaper §5) while keeping capital responsive. This parameter is governance-tunable and can be increased if slashing dynamics require it.

**Why 10% minimum self-bond?** This ensures runners always have meaningful skin in the game. A runner operating purely on delegated capital (0% self-bond) has no personal loss from misbehavior — only reputation damage. Governance can adjust as the market matures.

**Why no automatic commission from the protocol for delegators finding runners?** Introducing a "delegation fee" split would add complexity without clear benefit. The commission is between runner and delegator. If a pool operator charges an additional management fee, that's between the pool and its depositors — an actor-layer concern.

**Why batch DelegatorPayout events above threshold?** Individual events per delegator per job settlement create O(runners × delegators) events per job. With 5 runners and 200 delegators each, that's 1,000 events per settlement. Batching above a threshold (20) keeps events useful for small delegator sets (direct delegators can see per-job payouts) while bounding gas for large pools.

**Why tranches instead of one record per (runner, delegator) pair?** Partial undelegation creates an active remainder and a separate unbonding position — with distinct amounts, timestamps, and claim states. A single record per pair cannot represent this correctly. Tranches also make the slashing math, the unbonding queue, and top-ups trivially correct: each tranche has one status, one amount, one timestamp, and is the unit of bookkeeping throughout the system. The cost is modest storage growth, bounded by `MAX_ACTIVE_TRANCHES_PER_DELEGATOR × MAX_DELEGATORS_PER_RUNNER`.

**Why is the slashable base computed lazily instead of cached?** An Unbonding tranche leaves the slashable base the moment `current_block >= claimable_at` — a transition that happens implicitly at the block boundary, per §3.1. Caching that sum would require either a scheduled maintenance pass (the approach used in an earlier draft, which introduced an overflow hazard) or a block-height-dependent invariant that is easy to get subtly wrong. Slashing already iterates every slashable tranche to apply per-tranche reductions, so computing the base in the same pass is free.

**Why no atomic redelegation in v1?** See §9.5. The short answer: dual liability accounting is a meaningful spec on its own, and delegators who need to move between runners can undelegate and redelegate in ~24h, or use liquid staking pools that absorb the delay.

---

## Part II — v2 Revision (canonical; opcode renumbering + explicit amendments)

### 0. What this revision does

CIP-13 v1 §3.3 carries an explicit TODO admitting opcodes 40–44 collide with currently-assigned `SystemInstruction` opcodes (40 `UpdateSettlementConfig`, 41 `FundActor`, 42 `KeyDelivery`, 43 `UpgradeActor`). CIP-13 v1 also implicitly amends CIP-2 §5/§6 (VRF weight + max job value formulas) but does not state this as a normative cross-CIP amendment. v2 resolves both.

### 1. Master opcode allocation table (rewritten 2026-05-26 against code reality)

**Allocation authority:** the wire opcode constants in
[`cowboy-protocol-codec/src/instruction.rs` at node's pinned protocol revision](https://github.com/cowboyinc/cowboy-protocol/blob/020cd1d9063a654098a6dd98b630eb7fcdb38685/crates/cowboy-protocol-codec/src/instruction.rs)
and the corresponding node dispatch. Recheck the revision pinned by node **and pending reservations in this table and open protocol PRs** when
allocating. A reservation remains unavailable even before node pins its codec. The whitepaper's system-actor **address** registry is a different
namespace and cannot establish opcode availability. The table below is a partial
snapshot, not a declaration that unlisted numbers are free.

**Live opcodes (in code):**

| Opcode | Instruction | Owning CIP | Status |
|---:|---|---|---|
| 0 | `CreateAccount` | core | ✅ in code |
| 1 | `Transfer` | core | ✅ in code |
| 2–5 | `RunnerRegister` / `UpdateRateCard` / `Heartbeat` / `Deregister` | CIP-2 | ✅ in code |
| 6–9 | `JobSubmit` / `JobResultSubmit` / `JobCancel` / `JobResultCommit` | CIP-2 | ✅ in code |
| 10–20 | Token ops (`TokenCreate`…`TokenTransferBatch`) | CIP-20 | ✅ in code |
| 21–29 | — | — | (free) |
| 30–35 | Entitlement (`Grant`/`Revoke`/`Delegate`/`CreateRole`/`AssignRole`/`RevokeRole`) | CIP-2 §7 | ✅ in code |
| 36–39 | — | — | (free) |
| 40 | `UpdateSettlementConfig` | CIP-2/CIP-3 | ✅ in code |
| 41 | `FundActor` | core | ✅ in code |
| 42 | `KeyDelivery` | core | ✅ in code |
| 43 | `UpgradeActor` | CIP-12 §7 | ✅ in code |
| 44 | `UpdateBasefeeConfig` | CIP-3 | ✅ in code |
| 45–47 | `SubmitProposal` / `CastVote` / `ExecuteProposal` | CIP-12 | ✅ in code |
| 48–50 | `CancelTimer` / `UpdateTimerConfig` / `ExtendTimer` | CIP-5 | ✅ in code |
| 51 | `DeployCode` | core | ✅ in code |
| **52–57** | **`SessionOpen` / `SessionDeposit` / `SessionSettle` / `SessionClose` / `SessionFinalize` / `SessionSlash`** | **CIP-8 (MPP Session)** | **✅ in code** |
| 58–59 | — | — | (free) |
| **60–63** | **`RegisterTeeTrustedKey` / `RevokeTeeTrustedKey` / `SubmitTeeAttestation` / `RevokeTeeAttestation`** | **CIP-24 §3.3 (TEE Verifier support)** | **✅ in code** |
| 64–67 | — | — | (free) |
| **68–84** | **`SetSecret` … `ForcedDeregisterCbssProxy`** (17 slots) | **CIP-24 §3.3 (CBSS main)** | **✅ in code** |
| **85** | **`SubmitDrainRelayProposal`** | **CIP-9 §13.3** | **✅ in code** |
| **86** | **`SubmitAutoDrainPolicyProposal`** | **CIP-9 §13.4** | **✅ in code** |
| 87–195 | Other allocations not enumerated in this snapshot | See current protocol codec | Not a declaration that these slots are free |
| 196–199 | Persistent workload instructions | CIP-10 | Allocated in protocol and dispatched in node `44e1eb129`; deployment acceptance is separate |
| 200–215 | BankActor lifecycle, funding and administration | [CIP-28 interface appendix §3](../references/bankactor-interface#3-instruction-contracts) | Allocated in node codec at `44e1eb129`; 16 instructions, not runtime bank registration |
| 216 | `SubmitRegisterBankProposal` | CIP-28 | Allocated; runtime bank registration remains rejected |
| 217 | `SubmitGrantReleaseProposal` | CIP-12 | Allocated in the pinned codec |
| 218 | `Cbqs` | CIP-39 | Allocated in the pinned codec |
| 219 | `RunnerUpdateEndpoint` | CIP-2 | Allocated in the pinned codec |
| 220–221 | `RoomAuthority` / `RouteInvocation` | CIP-43 | Allocated in the pinned codec |
| 222 | `SubmitFraudProof` | CIP-25 | Allocated in the pinned codec |
| 223 | `GrantSecretActor` | CIP-24 | Allocated in the pinned codec |
| 224 | `SetWorkloadResourcePolicy` | CIP-10 | **Reserved** in companion protocol #150; not available for reassignment |
| 225 | `PurchaseAllowance` | CIP-28 | **Reserved** in companion protocol #150; not available for reassignment |
| 226+ | Other allocations not enumerated in this snapshot | See current protocol codec and pending reservations | Verify availability before assignment |

The 224–225 reservations are pinned by [protocol #150 at `3529c47c`](https://github.com/cowboyinc/cowboy-protocol/blob/3529c47ce50181d6f23c9bf2c9398febb8891958/crates/cowboy-protocol-codec/src/instruction.rs). Codec allocation, node integration and deployment acceptance are separate states; absence from node's older pin never releases a pending reservation.

**Aspirational allocations (NOT in code; need renumbering against the table above before activation):**

| Originally claimed | CIP | Current state |
|---|---|---|
| 52–56 → `RunnerUpdateDelegationConfig` / `DelegateStake` / `IncreaseDelegation` / `UndelegateStake` / `ClaimUnbonded` | CIP-13 v2 (this CIP) | Collides with CIP-8 Session 52–57. Must renumber to verified unassigned slots on activation; exclude allocated/reserved slots, including 196–225. |
| 57–60 → `VerifyCae` / `UpdateCpuRoot` / `UpdateNrasRoot` / `GcNonces` | CIP-23 v2 | Collides with CIP-8 SessionSlash (57) + CIP-24 TEE keys (60). Must renumber to verified unassigned slots on activation; exclude allocated/reserved slots, including 196–225. |
| 61–64 → `RegisterBaseImage` / `DeregisterBaseImage` / `RegisterResourceClass` / `DeregisterResourceClass` | CIP-10 v2 | Collides with CIP-24 TEE keys (61–63). Must renumber to verified unassigned slots on activation; exclude allocated/reserved slots, including 196–225. |
| 65–66 → `IngressDispatch` / `CompleteReceipt` | CIP-14 v2 | Proposed only; check the protocol codec constants and dispatch before assigning wire opcodes. |

BankActor lifecycle/funding/admin instructions are already allocated at **200–215**; see the [CIP-28 interface appendix §3](../references/bankactor-interface#3-instruction-contracts). They are not pending assignments in the ≥87 range. Runtime bank registration remains rejected.

**Allocation versus implementation and deployment:**

| Opcode | Instruction family | Owning CIP | Status |
|---:|---|---|---|
| 196–199 | `RegisterWorkload` / `UpdateWorkload` / `DeregisterWorkload` / `ReportWorkloadStatus` | CIP-10 persistent workloads | Codec and node dispatch exist at the same pinned revision as the BankActor block. No new allocation is needed; actual deployment and entitlement acceptance require separate evidence. |

**CIP-29 (Event Subscription)** uses neither a SystemInstruction opcode nor a deployed actor. Instead, `pvm_host::call_actor` intercepts calls targeting the dedicated address `EVENT_SUBSCRIPTION_SYSTEM_ACTOR = 0x1D` and dispatches to `event_sub_system_actor::dispatch_rpc`. No master-table allocation is required.

**Note on this CIP's own opcodes.** CIP-13 v2's delegation handlers (originally numbered 52–56 in the v2 draft) are not yet in code. Activation work MUST verify fresh slots against the current protocol codec constants, node dispatch and pending protocol PR reservations, exclude allocated/reserved slots (including 196–225), and amend this table; the 52–56 numbering is **historical** and must not be cited as authoritative.

### 2. Explicit amendment to CIP-2 §5 (VRF selection)

CIP-2 §5.4 defines VRF selection using `weights[i] = stake_to_weight(candidates[i].stake)`. CIP-13 v2 amends:

> **Amended:** `weights[i] = stake_to_weight(effective_stake(candidates[i]))` where `effective_stake(R) = R.registration.stake + R.delegation_totals.total_active`. The weight function is unchanged from CIP-2 §5.3: `w = effective_stake · max(√(reputation / REPUTATION_NORMALIZER), w_min_floor)` — linear in `effective_stake`. This applies to every dispatcher selection that occurs at or after CIP-13 activation.

### 3. Explicit amendment to CIP-2 §6 (max job value)

> **Amended:** `rate_card.max_job_value <= effective_stake × STAKE_JOB_MULTIPLIER_DENOM / STAKE_JOB_MULTIPLIER_NUM` (default 1.5×). `effective_stake` includes Active delegated tranches per §2 above.

### 4. Slashing routing reuses CIP-3 `SettlementConfig`

CIP-13 v1 §3.6 `slash_runner_with_delegation` ends with `route_to_treasury(slashed_total / 2); route_to_burn(slashed_total - slashed_total / 2)`. v2 clarifies: the `50/50` split is the **default**, and runtime SHOULD read from `system:settlement_config.slash_treasury_percent` (CIP-3 / `SettlementConfig`) so governance can tune slashing routing alongside other settlement routing.

The per-tranche slashing math (proportional reduction by `T.amount / delegated_slashable`) is independent of the routing split and unchanged.

### 5. Vote weight in CIP-12 (carry-forward, no change)

CIP-13 v1 §6.1: runner-delegated CBY has zero governance vote weight in v1. v2 carries this forward without change. Validator-delegated stake retains full vote weight per CIP-12 §6.2; runner-delegated stake remains a third category with no governance voice. A future CIP MAY revisit; deliberately deferred.

### 6. Interaction with CIP-23 (TEE delegation)

A runner with a TEE `measurement_binding` (CIP-23 §3.7) MAY accept delegations. Properties:

- Delegated stake counts toward the runner's `effective_stake` for VRF weight (§2 above) and for max job value (§3 above).
- Delegated stake does NOT affect TEE eligibility. Eligibility is determined solely by `measurement_binding.status == Active && expires_at > submission_block` per CIP-23 §3.8 — a categorical capability check, not a stake threshold.
- Slashing of a TEE runner under CIP-23 (e.g., for forged CompositeAttestation) cascades to delegators per the v1 §3.6 algorithm, capped at `MAX_DELEGATION_SLASH_PER_EPOCH_BPS = 500` (5%). The cap protects passive capital from acute TEE-related risk concentration. Self-stake is uncapped and fully slashable for TEE forgery (CIP-23 v2 Part II §3).

### 7. Backwards compatibility

CIP-13 has not yet been deployed (v1 created 2026-04-12; v2 follows 2026-04-21). The opcode renumbering replaces a placeholder TODO with concrete values — no migration cost. The CIP-2 §5/§6 amendment is binding on new dispatcher / max-job-value computations after activation; no existing deployed jobs are affected because delegation cannot exist without CIP-13 active.
