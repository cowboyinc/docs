---
title: "CIP-1: Actor Message Scheduler"
description: Protocol-level scheduler — tiered Calendar Queue with EIP-1559 timer-lane basefee + priority tip, per-actor fairness weight, and Gas Bidding Agent (GBA) contracts layered atop CIP-5's per-fire fee_payer model and CIP-3's dual-meter basefees.
---

# CIP-1: Actor Message Scheduler

<Note>
  **Status:** Draft for Internal Review  
  **Type:** Standards Track  
  **Category:** Core  
  **Created:** 2025-10-01
</Note>

## Abstract

This document specifies the **Autonomous Actor Scheduler**: the protocol-level mechanism that implements chain-native timers. It combines a **tiered Calendar Queue** for scalable O(1) event scheduling with an **EIP-1559 timer-lane** pricing model (basefee + priority tip) and a **per-actor fairness weight `W(actor) ∈ [1, 2]`**. Per-actor **Gas Bidding Agent (GBA)** contracts may override the protocol-supplied default GBA to compute bids from real-time block context, enabling actors to dynamically respond to network congestion and weigh the urgency of their own scheduled tasks.

The scheduler operates **on top of** CIP-5's per-fire basefee model: every fire is a paid execution (`fee_payer` pre-charge plus refund). The EIP-1559 priority tip is **additional to** the per-fire `max_cost` pre-charge, not a replacement. Fees and metering align with CIP-3.

**Implementation status:** The inspected node runs CIP-5 FIFO scheduling and does not yet provide the complete auction contract below. Its `cip1.auction.activation_height` gate defaults to `u64::MAX`; this is an implementation limitation, not a required deployment mechanism. Remaining work includes the default-GBA median-tip estimator, Python bindings for both fee fields, schedule/fire validation and structured errors, and the tiered physical queue. The current fairness calculation uses the competing due-timer actors rather than a full-network median; simulation must validate that approximation against §8. These gaps must be resolved before claiming conformance.

## 1. Motivation

The Cowboy actor model's reliance on timers requires a scheduler that is both scalable and economically intelligent. Network conditions are dynamic, and a scheduled task's importance can change based on external events. A fixed pre-paid fee for a future transaction is insufficient. This design lets actors make real-time, economically rational decisions about the cost of their own execution, so high-priority tasks can aggressively compete for block space when it matters most.

EIP-1559 pricing is preferred over a first-price-per-cycle auction for three structural reasons:

1. **First-price-per-cycle is unstable in repeated knapsack settings.** Programmatic bidders best-respond by underbidding the prior round's clearing price, leading to oscillation and revenue collapse. Autonomous actors cannot easily run a complex bidding strategy or converge a Nash equilibrium against other agents. EIP-1559 eliminates the strategic-bidding component: the basefee is deterministic from the prior block's utilisation, and the priority tip is a simple price-discovery channel for ordering within the lane.
2. **The default GBA collapses to ~2 lines.** Under EIP-1559 the default bidding strategy reduces to `max_fee = 2 × basefee`, `max_priority_fee = previous_block_p50_tip` — analogous to MetaMask's default estimator on EVM. This removes the centralisation pressure that conservative ad-hoc defaults would otherwise impose on unsophisticated actors.
3. **The invalid-bid attack class disappears structurally.** There is no `bid` field; `max_priority_fee_per_cycle` is bounded by `max_fee_per_cycle − basefee` (lossy clamping is structural), and the per-fire `max_cost` pre-charge (CIP-5 §6.3) already caps the worst-case debit per fire. No drain attack survives.

## 2. Tiered Calendar Queue

The Actor Scheduler state is part of the global consensus state (σ) and is organized into a three-tier structure to manage timers across different time horizons.

### 2.1 Tier 1 — Block Ring Buffer

Imminent timers.

- **Structure:** A fixed-size ring buffer of `RING_BUFFER_SIZE` buckets, one per upcoming block height.
- **Function:** A timer scheduled for block `H` is placed in bucket `H % RING_BUFFER_SIZE`.
- **Performance:** O(1) enqueue and dequeue; the block producer accesses only the single bucket for the current height.

### 2.2 Tier 2 — Epoch Queue

Medium-term timers.

- **Structure:** An array of buckets, one per future epoch (≈ one hour of blocks).
- **Function:** A timer scheduled for a block in epoch `E` is placed in bucket `E`. At each epoch rollover, the protocol redistributes that bucket's timers into the appropriate Ring Buffer slots — an amortized maintenance cost.

### 2.3 Tier 3 — Overflow Sorted Set

Long-horizon timers.

- **Structure:** A Merkleized balanced binary search tree ordered by block height.
- **Function:** Far-future timers are inserted here. Epoch maintenance also walks this tree and migrates any timers now within the Epoch Queue's range.

## 3. Integration with the State Transition Function

Let σ be the global state, B a block, and `H = height(B)`. The per-block sequence (canonical, matching CIP-5 §5.1 and the validator code path):

1. **Header / proposer.** Determined by Simplex consensus and the previous QC.
2. **Epoch maintenance (if applicable).** Redistribute timers from higher tiers into the Block Ring Buffer.
3. **Execute transactions (TX phase).** Process the ordered transaction set Tᵢ. Calls to `schedule_timer` MAY supply `(max_fee_per_cycle, max_priority_fee_per_cycle)`; if omitted, the runtime supplies the default-GBA values (§7.1).
4. **Collect due timers (end-of-block).** Read the timer bucket for `H` and apply lifecycle classification per CIP-5 §5.4:
   - **TTL expired** or **`balance(fee_payer) < max_cost`** → self-destruct under the GC lane (§9).
   - **Otherwise** → enqueue for priority sort.
5. **Compute lane basefee.** The Timer-lane basefee adjusts per block using EIP-1559 dynamics over the prior block's timer-lane utilisation (§5).
6. **Compute effective priority** for each enqueued timer:
   ```
   priority_per_cycle = min(max_priority_fee_per_cycle, max_fee_per_cycle − basefee_lane_timer)
   effective_priority = priority_per_cycle × W(actor)
   ```
   where `W(actor) ∈ [1, 2]` is the per-actor fairness weight (§8).
7. **Sort and select.** Order due timers by `effective_priority` descending; tie-break by `(timer_id, schedule_block)`. Greedily fill `LANE_TIMER_CYCLES`. Each selected timer is gated by `cycles_consumed_so_far + gas_limit_per_fire ≤ LANE_TIMER_CYCLES − cycles_already_used` **and** `gas_limit_per_fire ≤ MAX_CYCLES_PER_FIRE_AUCTION_PHASE` (§10). Timers exceeding the per-timer cap are deferred without an attempt.
8. **Settle.** For each selected timer:
   - Pre-charge `fee_payer` per CIP-5 §6.3 with the priority-tip term added:
     ```
     max_cost = gas_limit_per_fire × (basefee_lane_timer + priority_per_cycle)
              + max_cells_per_fire × basefee_cell
     ```
   - Execute the handler. On normal return, refund unused cycles × `(basefee_lane_timer + priority_per_cycle)`. The tip portion goes to the **block proposer** (consistent with CIP-3 §2.4 tips routing); the basefee portion is **burned**.
   - On insufficient funds at any step → CIP-5 §5.4 path 2 (self-destruct without firing).
9. **Defer.** Timers not selected remain in the bucket; on the next block they fall through the same flow. The 1,000-block fairness window naturally raises `W(actor)` for actors whose timers are repeatedly deferred (§8).
10. **Update fairness counters.** Increment per-actor `recent_executions[actor] += 1` for every timer fired; the 1,000-block rolling decay is applied at the start of the next end-of-block step (§8).
11. **Resolve jobs, adjust basefees, mint rewards.** As elsewhere defined.

## 4. Same-Block Prohibition

Timers created within the current block's transactions MUST NOT execute in the same block. This avoids reentrancy via timer scheduling and contextual ambiguity around the basefees, congestion signals, and balances passed to GBAs. Consensus-critical.

## 5. Timer-Lane EIP-1559 Pricing

The Timer-lane basefee adjusts per block over the prior block's timer-lane utilisation:

```
utilisation_{H} = cycles_consumed_in_lane_{H} / LANE_TIMER_CYCLES        // ∈ [0, 1]
basefee_{H+1}   = basefee_{H} × (1 + clip((utilisation_{H} − 0.5) / 0.5, −0.125, +0.125))
```

- **Target utilisation:** 0.5 (50%) of the 8,800,000-cycle Timer lane.
- **Max basefee adjustment per block:** ±12.5%.
- **100% of the basefee is burned** (consistent with CIP-3 §2.4 and WP §6 "100% basefee burn"). The lane basefee *replaces* the CIP-3 cycle-basefee meter for timer fires — timer cycles are metered against `basefee_lane_timer` only and are **not** double-charged against the global cycle basefee. The two meters are tracked separately (the timer lane runs its own EIP-1559 utilisation curve and its own burn tally) to preserve per-lane burn telemetry.
- **Per-lane fee multiplier** is pinned at `1.0×` at launch (CIP-3 §2.2.3 + WP §6 / §17.9; no subsidy). Tier-0 governance-tunable.

The priority tip is a separate quantity routed to the block proposer (§3 step 8). A timer with `max_priority_fee_per_cycle = 0` still fires when the lane is uncongested, paying only the lane basefee. Under congestion, only timers whose `effective_priority` (§3 step 6) clears the marginal kicked-out timer make the cut; losers carry forward.

## 6. `schedule_timer` API

The PVM host API (`node/execution/src/pvm_host.rs`):

```python
timer_id = pvm_host.schedule_timer(
    height: int,
    payload: bytes,
    fee_payer: bytes = None,                    # CIP-5 §4.1
    gas_limit: int = None,                      # CIP-5 §4.1
    expires_at: int = None,                     # CIP-5 §4.1
    max_fee_per_cycle: int = None,              # defaults to default-GBA value (§7.1)
    max_priority_fee_per_cycle: int = None,     # defaults to default-GBA value (§7.1)
)
```

**Scheduling-time validation:**

- `max_fee_per_cycle ≥ basefee_lane_timer` → else `TimerRejectedBelowBasefee` (immediate failure; nothing escrowed).
- `max_priority_fee_per_cycle ≤ max_fee_per_cycle − basefee_lane_timer` → else clamp at schedule time and emit `TimerPriorityClampedAtSchedule(stated, clamped)` for observability.
- **No escrow at scheduling.** The per-fire `max_cost` (CIP-5 §6.3) is pre-charged at execution time only, not at scheduling.
- **Scheduling cost** remains a small fixed cycle charge paid upfront by `schedule_timer` to occupy the queue slot — independent of execution pricing.

`bid` is not a supported scheduling parameter. A caller supplying it MUST receive `TimerArgDeprecated`; there is no silent-ignore behavior.

## 7. Gas Bidding Agents (GBAs)

A GBA is an actor-owned contract responsible for pricing one or more of the actor's timers. The protocol calls `getGasBid(context)` read-only at fire time. The runtime supplies a **default GBA** when an actor doesn't provide one; custom GBAs remain useful for actors that need dynamic, context-sensitive bidding (DeFi liquidation actors, oracle-pushers, MEV-aware schedulers).

### 7.1 Default GBA (normative)

```python
def getGasBid(context: GBAContext) -> TxFeeParams:
    return TxFeeParams(
        max_fee_per_cycle           = 2 * context.basefee_lane_timer,
        max_priority_fee_per_cycle  = context.previous_block_p50_priority_tip_per_cycle,
        # cell side: defaults to CIP-3 basefee_cell with no priority (timers are cycle-bound)
        max_fee_per_cell            = context.basefee_cell,
        max_priority_fee_per_cell   = 0,
    )
```

- `2 × basefee` headroom absorbs up to ~5 blocks of basefee growth at the ±12.5% per-block clamp (`1.125^5 ≈ 1.80`) before hitting the cap — sufficient for normal congestion swings.
- `p50 priority tip` is the prior-block median priority fee paid by *fired* timers; falls back to `0` if no timers fired in the prior block (avoids an undefined estimator on cold start).

### 7.2 GBA interface and context

Custom GBAs implement:

```
function getGasBid(bytes calldata context) external view returns (TxFeeParams);
```

The protocol-supplied `context` carries the data needed for real-time pricing decisions:

- `trigger_block_height` (u64) — the height the timer was originally scheduled for.
- `current_block_height` (u64) — current height; lets the GBA compute lateness.
- `basefee_cycle` (u128) — current compute-cycle basefee (CIP-3).
- `basefee_cell` (u128) — current storage/byte basefee (CIP-3).
- `basefee_lane_timer` (u128) — current Timer-lane basefee (§5).
- `last_block_cycle_usage` (u64) — total cycles used in the previous block (congestion signal).
- `previous_block_p50_priority_tip_per_cycle` (u128) — prior-block median fired-timer tip.
- `owner_actor_balance` (u128) — current CBY balance of the owning actor.

This admits sophisticated GBAs — e.g., a DeFi liquidation actor whose GBA consults on-chain oracles and submits an extreme tip when market volatility is high.

### 7.3 SDK convenience: `priority_tier_hint`

The `cowboy-py` SDK MAY expose a high-level enum for callers who don't want to construct a GBA:

```python
class PriorityTier(Enum):
    ECONOMY  = "economy"   # multiplier 0.8× on max_priority_fee_per_cycle
    STANDARD = "standard"  # multiplier 1.0× (default)
    FAST     = "fast"      # multiplier 1.5×
    URGENT   = "urgent"    # multiplier 2.5×

cowboy.schedule_timer(..., priority_tier_hint=PriorityTier.FAST)
# expands to: max_priority_fee_per_cycle = 1.5 × p50_priority_tip
```

These multipliers are stored at `0x09` under `system:gov:param:cip1.priority_tier_multipliers.{economy,standard,fast,urgent}` (the `system:gov:param:` prefix is the on-chain `gov_param_key` convention — `node/types/src/constants.rs` `GOV_PARAM_KEY_PREFIX`) and are **Tier-0 governance-tunable** (consistent with the lane fee multipliers in CIP-3 §2.2.3 — both are multiplicative scalars on fee components, neither redirects revenue across recipient classes). CIP-12 §5.1 Tier-0 scope already references "any `governance-tunable` parameter (see genesis defaults and CIP-1/3/5/9/10)"; the priority-tier multipliers are picked up automatically by that clause.

## 8. Per-Actor Fairness Weight `W(actor)`

**Formula:**

```
recent_executions[actor]  = sum of timer fires by `actor` over the most recent 1,000 blocks
network_median            = median of recent_executions across actors with ≥1 fire in window
                            (or 0 if no actor has fired in the window)
ratio[actor]              = recent_executions[actor] / max(1, network_median)
W(actor)                  = clip(2 − ratio[actor], 1, 2)
```

- An actor with no recent fires (`ratio = 0`) gets `W = 2` (maximum boost); an actor at or above the network median (`ratio ≥ 1`) gets `W = 1` (no boost). Linear interpolation for `ratio` in `[0, 1]`.
- Window length `FAIRNESS_WINDOW_BLOCKS = 1_000` (~16.7 min at 1s blocks), Tier-2 governance-tunable. Stored at `0x09` under `system:gov:param:cip1.fairness_window_blocks`.
- `ratio` is **clipped** to `[0, 2]` before subtraction, so a brand-new actor with zero recent fires gets `W = 2` rather than infinity.

The shift from per-*timer* exponential bias to per-*actor* weight is the central anti-monopolisation choice: bias-on-deferred-timer was retrospective and gameable via timer-spam; per-actor weight is preventive and cannot be amplified by re-scheduling.

**State.** Each `actor` carries a 1,000-element ring buffer of fire-counts per block in the Actor Scheduler state. Memory cost: ~8 KiB per actively-firing actor. Eviction: actors with zero fires in the entire 1,000-block window are pruned from the fairness map (next fire re-creates a fresh entry).

**Mutability of formula structure.** Tier-2 (governance changes the inclusion ordering, which affects fee revenue distribution). The numeric `FAIRNESS_WINDOW_BLOCKS` and the `[1, 2]` clip bounds are Tier-0.

**Known limitations (Simulation deferred — §16):**

1. **Fragmentation attack.** A sophisticated developer can deploy 100 actor instances and treat each as a separate "quiet" actor to bypass per-actor weight. Mitigation candidate: per-deployer weight (using actor-creation trace) instead of per-actor — requires deeper actor metadata than CIP-2 currently exposes. Simulation MUST size the attack magnitude before this spec ships; if material, per-deployer weight is added in a follow-up revision.
2. **Median moves under shock.** When a large actor enters/exits and shifts the network median sharply, incumbents are briefly disadvantaged for ~1,000 blocks. EMA-smoothing the median (analogous to the HHI smoothing in CIP-2 §5 amend) is a candidate follow-up addition.

## 9. Lane Budgets and the Three-Path Timer Lifecycle

Per CIP-5 §6.5, the timer subsystem has **two independent per-block budgets**:

| Constant | Purpose | Default |
|---|---|---|
| `LANE_TIMER_CYCLES` | Execution lane — natural-fire timers (path 1) | `8,800,000` (compile-time protocol constant, `node/types/src/constants.rs`; **not** a `TimerConfig` field — changing the timer-lane budget requires a protocol constant change, not a `TimerConfig` update) |
| `TIMER_GC_CYCLES` | Cleanup lane — TTL expiry and insufficient-funds destruction (paths 2 / 3) | `TimerConfig.gc_cycles_per_block = 5,000,000` |

The auction operates only on `LANE_TIMER_CYCLES`. GC draws from `TIMER_GC_CYCLES`, so a resume-after-outage storm of expired timers cannot starve live timer execution.

The **three-path lifecycle** for any due timer (CIP-5 §5.4), plus explicit cancellation:

1. **Natural fire.** `fee_payer` is solvent and TTL has not expired. Pre-charge, execute, refund unused.
2. **TTL expiry.** `expires_at` has passed. Self-destruct under the GC lane.
3. **Insufficient funds.** `balance(fee_payer) < max_cost`. Self-destruct under the GC lane and emit `TimerCancelledInsufficientFunds`.
4. **Explicit cancellation.** Actor-self cancel (host syscall) or validator-set emergency cancel via `SYS_CANCEL_TIMER` (§12).

## 10. Per-Timer Cycle Cap

```
MAX_CYCLES_PER_FIRE_AUCTION_PHASE = 250_000
```

The auction contract caps each timer fire at 250,000 cycles to prevent a single timer from monopolising the lane. The inspected FIFO implementation uses the CIP-5 cap of 550,000 cycles; it does not yet enforce this auction requirement.

Actors with handlers that legitimately need >250k cycles can split the work across multiple timer fires; the same-block prohibition (§4) prevents tail-end re-fires from compounding in a single block.

**Mutability.** Tier-0, key `system:gov:param:cip1.auction.max_cycles_per_fire` (read at `0x09` via `gov_param_key`, using the governance parameter lookup in `node/storage/src/speculative.rs`).

## 11. DoS Mitigation and Congestion Handling

The combination of a bounded lane budget, deterministic basefee growth, and per-actor fairness weight defends against scheduler-targeted DoS:

- **Bounded execution.** `LANE_TIMER_CYCLES` caps timer-driven work per block, so timers cannot crowd out user transactions.
- **Economic prioritisation.** Instead of plain FIFO, the lane is sorted by `effective_priority`: timers whose owners bid higher tips run first. Low-priority spam timers are priced out during congestion as the basefee ratchets up.
- **Best-effort delivery with fairness.** Losers carry over to the next bucket; the per-actor fairness weight `W(actor)` raises priority for actors who have fired less than the network median in the last 1,000 blocks, preserving liveness without amplifying timer-spam.
- **GC isolation.** TTL-expiry storms and insufficient-funds destruction are routed through `TIMER_GC_CYCLES`, an independent budget.
- **Structural invalid-bid resistance.** There is no `bid` field to game; `max_priority_fee_per_cycle` is bounded by `max_fee_per_cycle − basefee_lane_timer`, and the per-fire `max_cost` pre-charge (CIP-5 §6.3) caps the worst-case debit per fire.

## 12. System Instructions

This spec introduces **no new system-instruction opcodes**. The `schedule_timer` extension is a PVM host-API change to existing syscalls (`schedule_timer` / `schedule_timer_ex` / `cancel_timer` / `extend_timer` at `node/execution/src/pvm_host.rs`); it adds optional parameters but allocates no opcodes.

The supporting timer-management system instructions already live in code at the following opcodes (`node/types/src/execution.rs`):

| Symbolic name | Opcode | Sender | Purpose |
|---|---:|---|---|
| `SYS_CANCEL_TIMER { timer_id }` | **48** | `system_deployers` | Validator-set emergency cancel of a misbehaving / abandoned timer |
| `SYS_UPDATE_TIMER_CONFIG { config }` | **49** | `system_deployers` | Governance-tune `TimerConfig` (TTL / per-fire caps / GC budget) |
| `SYS_EXTEND_TIMER { timer_id, new_expires_at }` | **50** | `system_deployers` | Emergency TTL extension when an actor can no longer self-extend |

The canonical opcode allocation table is maintained in CIP-13.

## 13. Funding

Each scheduling actor MUST fund itself or a designated `fee_payer` with at least one `max_cost` reserve per pending timer. Long-running heartbeat actors SHOULD use `schedule_timer_ex` with an explicit `fee_payer` (default `actor_self`) and monitor its balance. Actors and watchtowers SHOULD subscribe to `TimerCancelledInsufficientFunds`. TTL-protected abandoned timers are cleaned up automatically.

## 14. Integration Requirements

- CIP-5 owns timer creation, cancellation, expiry, and the per-fire payer model. This CIP owns auction ordering and fairness; implementations MUST use §§3–11 rather than CIP-5 §9's alternative auction description.
- System instruction opcodes 48, 49, and 50 retain the functions in §12.
- The priority tip extends the per-fire `max_cost` formula while retaining pre-charge and refund accounting.
- Block ordering is transactions before timers. A timer created in a block MUST NOT fire in that block.
- Omitted fee fields select the default GBA estimator. Python bindings MUST expose the explicit fee fields in §6.
- Conformance requires auction ordering, timer-lane basefee dynamics, default-GBA estimation, the §10 cap, and §8 fairness to operate together. Test these through the scheduler and SDK caller paths.

## 15. Governance Parameters

Economic defaults and parameter changes follow their stated governance tiers under CIP-12. The `priority_tier_multipliers` are Tier-0 parameters (§7.3). The scheduler contract does not require a timed conversion of an existing network or a temporary argument-compatibility path.

## 16. Simulation Requirements

- **`FAIRNESS_WINDOW_BLOCKS`** calibration. 1,000 blocks (~16.7 min) is the launch default; longer windows smooth more but lag more on actor identity changes.
- **Per-deployer vs per-actor weight** (fragmentation-attack threat magnitude, §8 limitation 1).
- **Median EMA-smoothing** (§8 limitation 2) — evaluate against empirical workload shocks.
- **Priority-tier multipliers** `{0.8, 1.0, 1.5, 2.5}` — empirical tuning against actual congestion patterns.
- **Lane fee multiplier** — pinned at 1.0× at launch (CIP-3 §2.2.3); Simulation MAY recommend a 0.8× Timer-lane subsidy if the lane is structurally under-utilised post-mainnet.
