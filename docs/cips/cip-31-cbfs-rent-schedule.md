# CIP-31: CBFS Rent Schedule

| Field           | Value                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------- |
| **CIP**         | 31                                                                                             |
| **Title**       | CBFS Rent Schedule, Relay Challenge Bond, and Slashing Curve                                   |
| **Status**      | Draft                                                                                          |
| **Type**        | Standards Track (Economic / Parameter)                                                         |
| **Created**     | 2026-05-14                                                                                     |
| **Author(s)**   | Cowboy Foundation                                                                              |
| **Depends-on**  | CIP-9 (RAS / CBFS), CIP-3 (fee model), CIP-12 (governance)                                     |
| **Required-by** | CIP-9 §10.4 / §14 (replaces TBD parameter values); WP §17.6 (forward reference)                |

## Abstract

This CIP pins concrete CBY values for every parameter that CIP-9 (Runner Storage / CBFS) currently labels `TBD`, adds a new `RELAY_CHALLENGE_BOND` field, makes the existing 10 / 1 / 89 platform-account credit / challenge-pool / Relay revenue split explicit in normative text, and defines the pro-rata weighting formula that distributes the 89% Relay share across active Relay Nodes.

The funded cumulative read mechanism is defined in CIP-9 §10.4.1–§10.4.2; this CIP supplies its governed transfer rate and clarifies its separate principal/gas economics. It does not activate that mechanism or select production channel budgets.

## Motivation

1. Let Relay operators run capacity-planning math before mainnet.
2. Let storage-rate volatility (CBY-denominated rent vs USD/GB/yr reality) be observable, monitored, and Tier-0-adjustable on a documented cadence.
3. Provide the challenger economic incentive that makes Proof-of-Retrievability honest in steady state.
4. Bound the worst-case Relay-side loss in slashing events.

## Specification

### 1. Storage Fee Rate

```
STORAGE_FEE_PER_MIB_PER_EPOCH = 2905  // nano-CBY per MiB per rent epoch (1 day per CIP-4)
```

Volumes are billed on `ceil(volume_size_bytes / 2^20)` MiB. Per 1 GiB of stored data per year: `2,905 nano-CBY × 1,024 MiB × 365 epochs ≈ 1.086 CBY / GiB / year`.

Backblaze B2 lists **$6.95 per decimal TB per month**, which is $7.64 / TiB / month — the units are not interchangeable and conflating them understates the multiple. Against that, the rate is **1.82× B2**, and roughly 40% below S3 Standard (~$24 / TiB / month).

The rate is denominated in **nano-CBY**. USD figures below are the administered CBY rate defined in **CIP-36 §6.6** applied to this constant, not a property of the constant itself; §6.6 is that rate's only definition and this section does not restate it. At the current rate the fee is **$0.163 / GiB / year**. If the rate moves, the Tier-0 review below retunes this constant to keep the fee inside the band — which is what the band exists for.

The rate is denominated per MiB, not per byte: the smallest integer per-byte rate (1 nano-CBY/byte/epoch) is ≈ 391.9 CBY / GiB / year — ≈ $58.78 at the administered rate, roughly 650× commodity object storage — so per-byte granularity cannot express a competitive rate.

**Mutability:** Tier-0 governance, stored at `0x09` under key `cip31.cbfs.storage_fee_per_mib_per_epoch`. The governance value is authoritative wherever it is set; the constant above is the genesis default only, and any implementation that bills from a compiled-in constant rather than the parameter is non-conforming. The Tier-0 review cadence is 30-day post-TGE → 90-day steady state; the target band is `[$0.05, $0.50] / GiB / year`. The rate is nano-CBY and the band is USD, so token-price movement drifts the fee against the band by construction; the administered rate (CIP-36 §6.6) pins the conversion between reviews, and the Tier-0 review retunes the constant when the drift leaves the band.

### 2. Transfer Fee Rate

```
TRANSFER_FEE_PER_MIB = 66_700  // nano-CBY per MiB served on read
```

≈ $0.01025 / GiB served at the administered rate — within 5% of Backblaze B2's over-allowance egress price ($0.01 per decimal GB = $0.010737 / GiB). An exact match would be ≈ 69,900 nano-CBY; the ~4.6% residual is well inside administered-rate uncertainty, so the round constant stands. A payer acknowledging 100 logical-shard MiB units authorizes `66,700 nano-CBY × 100 = 0.00667 CBY` of transfer principal at this rate, exclusive of channel lifecycle gas. Transfer principal goes entirely to the fixed serving Relay (no burn, no challenge-pool share).

**Mutability:** Tier-0, key `cip31.cbfs.transfer_fee_per_mib`.

### 2.1 Funded-channel charging and budget scope

CIP-9 §10.4.1 defines an independently activated funded cumulative read channel;
this parameter supplies its current execution-time rate `r`. It does not grant
a relay authority to debit an arbitrary reader or volume pool. One direct payer
transaction funds a fixed channel; subsequent payer-signed cumulative ACKs
authorize bounded claims against only that channel's remaining principal.

The unit counter is `sum(ceil(logical_shard_bytes / 2^20))` for complete,
independently verified logical shards, not ceil of a concatenated object's bytes
and not a sum over transport chunks. At each checkpoint:

```
delta = (acknowledged_units - settled_units) * r
r <= fixed_max_fee_per_mib
delta <= signed_cumulative_fee_ceiling - already_paid_atomic
delta <= dedicated_escrow_remaining
```

The settled cursor advances once even at zero rate. Different checkpoint
partitions give the same principal at a fixed rate and equal acknowledged units;
a rate change between claims can change the result within the signed bounds.
100% of the claimed transfer principal goes to the fixed serving relay wallet,
with no storage-rent/platform/challenge-pool split. A change to the relay's
registry owner or the volume does not redirect or revoke previously funded
claims. Strictly after the fixed claim deadline, only the fixed original payer
receives unclaimed principal; the submitter separately pays gas.

Opening/settlement/refund gas is additional to transfer principal and is charged
under the existing Cycle, Cell and Access markets. All three debit the same
native CBY account balance. Implementations retaining CBY/DBY tariff-component
labels MUST add those native charges; these labels do not describe separate
assets or an exchange-rate discount.

A relay/epoch is a scheduling grouping, not an unbounded single transaction.
A page contains 1..32 sorted unique channels with one latest cumulative payer
signature per channel. More than 32 channels requires multiple bounded pages;
fixed overhead may make small tails uneconomic. Before signing, the submitter
must apply explicit worst-case gas, per-page/epoch/lifetime budgets, claim-window
and gas/revenue policies to the actual page. Budgets and nonce ownership survive
restart and are not reset by a new epoch. Unknown submissions retain exact bytes;
a new nonce or repacked page is not recovery from uncertainty.

This CIP does not select deposit tiers, lock/claim windows, minimum page revenue,
gas-share limits, carry-over or delivery-loss budgets for production. It also
does not activate either payment version. These remain explicit rollout choices,
and no historical rent/challenge/reward pool migration is implied.

**Measured implementation evidence, not a production quote.** The Node STF
[lifecycle matrix](https://github.com/cowboyinc/node/blob/f0630e07b57e8fde5c5eeff20e962e78e594a569/ras-write-relayer/channel-costs.md)
and its machine-readable companion exercise 1/8/32 channels, 4/256 GiB worth
of logical-shard units per channel, and 1/2/4 checkpoint partitions, including
openings, refunds and one deliberately new-nonce charged duplicate-claim
negative test. Real QMDB writes, execution receipts, roots and account debits
are measured; this is not a transfer of that many payload bytes. At the fixed
66,700 atomic-unit rate, the one-checkpoint 32 × 256 GiB scenario has 559.520154
CBY transfer principal and 104.853345 CBY total measured gas (18.74%); the
1 × 4 GiB scenario has 0.273203 CBY principal and 6.615329 CBY gas (2421.40%).
The comparison demonstrates amortization and the remaining small-tail problem,
not a universal viability threshold. A 20% gas-share policy is not selected by
these measurements, and admission uses signed worst-case caps, not the observed
gas after execution. Locked-capital cost, throughput and real disk/relay latency
remain outside this matrix. Exact unknown-transaction replay is the recovery
rule; the charged negative test does not authorize replacing an unknown nonce.

### 3. Minimum Storage Balance

```
MIN_STORAGE_BALANCE = STORAGE_FEE_PER_MIB_PER_EPOCH × ceil(volume_size_bytes / 2^20) × 1
```

i.e. one epoch of fees at the current rate for the volume's current size. Falls below this triggers `STORAGE_GRACE_EPOCHS` per CIP-9 §10.3.

**Mutability:** formula-derived; the multiplier `1` (one epoch) is Tier-0-tunable via `cip31.cbfs.min_storage_balance_epochs`.

### 4. Fee Distribution Split (`10 / 1 / 89`)

For each epochly storage-fee batch collected from an account:

| Share | Destination                                                | Rationale |
| ----- | ---------------------------------------------------------- | --------- |
| **10%** | **Platform Fee Account** (credited to system actor `0x18`) | Marketplace platform share; disposition below |
| **1%** | **PoR Challenge Pool** (accrued at `0x0B` Relay Registry) | Funds bounded bounties on confirmed third consecutive misses (§7) and challenge-timer costs |
| **89%** | **Relay pro-rata distribution**                          | Active Relays, weighted by `weight_i = (shard_count_i × shard_age_in_epochs_i)` (§5 below) |

Implementation contract:

- `STORAGE_FEE_PLATFORM_BPS = 1000` (10%)
- `STORAGE_FEE_CHALLENGE_POOL_BPS = 100` (1%)
- `STORAGE_FEE_RELAY_BPS = 8900` (89%)
- Invariant: `STORAGE_FEE_PLATFORM_BPS + STORAGE_FEE_CHALLENGE_POOL_BPS + STORAGE_FEE_RELAY_BPS == 10000`

**Mutability:** Tier-0 governance under `cip31.cbfs.fee_split`; the invariant MUST hold after any proposal.

**Disposition of the 10% storage-rent share.** Storage and transfer fees are collected in CBY (§1, §2), but this split applies only to storage rent; transfer principal has no 10% share.

The share is therefore **credited to the Platform Fee Account, system actor `0x18`**, and the parameter is `cip31.cbfs.fee_split.platform_bps`. This is a platform share in the marketplace sense — the owner pays, relays do the work, the platform takes a cut — and it is deliberately NOT called a treasury share: `treasury` elsewhere in Cowboy denotes the protocol treasury, and the two have different owners and different purposes.

It is credited rather than burned because burning is irreversible and crediting is not: a credited share can be burned later from the account, while a burned one cannot be recovered, and the credit preserves an on-chain record of accrued platform revenue that a burn destroys.

The address is fixed rather than governance-set, so no governance write can redirect the share and settlement needs no per-block state read to find it. Withdrawal authority is genesis-defined for the account under COW-2915 — `0x18` sits in the keyless reserved system range, so it needs an explicit genesis-configured owner or multisig rather than a recoverable key, and v1 defines accrual here while COW-2915's resolution defines the exit path before mainnet genesis. Where a CBY sink is wanted, it is a buy-and-burn performed from that account, outside settlement and outside consensus.

This applies only to fees. **Slashed Relay stake (§8) is denominated in CBY and its 10% share is still burned**, which remains coherent: the stake is a token position, not a dollar claim.



### 5. Relay Pro-Rata Weight Formula

```
weight_i  = shard_count_i × shard_age_in_epochs_i
share_i   = weight_i / Σ_j weight_j
payout_i  = share_i × (storage_fees_collected × 0.89)
```

- `shard_count_i` is the number of unique shards Relay `i` currently holds and serves with valid PoR responses in the prior epoch.
- `shard_age_in_epochs_i` is `min(epochs_since_assignment, MAX_SHARD_AGE_FOR_WEIGHTING)` where `MAX_SHARD_AGE_FOR_WEIGHTING = 90` epochs (~3 months at 1-day epochs). The cap prevents permanent first-mover advantage.

**Rationale.** Pure shard-count weighting rewards Relays that load shards fast but never repair; pure age weighting rewards squatters. The product rewards Relays that take on real storage AND keep it healthy over time. The 90-epoch cap is a Tier-0 parameter.

**Mutability:** Tier-2 governance (changes the revenue distribution mechanism). Key: `cip31.cbfs.relay_weight_formula`. Tier-0 may not change the formula structure, only `MAX_SHARD_AGE_FOR_WEIGHTING`.

### 6. Minimum Relay Stake

```
MIN_RELAY_STAKE = 33_000 CBY
```

Required to register a Relay Node via the Relay Registry (`0x0B`). Sized to be meaningful relative to a professional Relay's expected revenue: a Relay holding ~100 TiB of shards backs ~67 TiB of logical data at `K + M = 4 + 2`, earning ~5,450 CBY / month from the 89% pro-rata share — high enough to deter spam, low enough to admit professional operators.

At 33,000 CBY that is **6.06 months** of revenue. The figure was 5,000 CBY when the storage fee was calibrated at CBY = $1; repricing the fee for the administered rate preserved relay revenue in USD but not the stake, which is a fixed CBY quantity, so it was resized with it. `RELAY_CHALLENGE_BOND`, `CHALLENGER_BOUNTY` and the three penalty parameters were scaled by the same 6.6x, preserving their nominal ratios to stake. `POR_CHALLENGE_FEE` is the one exception: 1 -> 7 is 7x, because 6.6 is not an integer and rounding down to 6 would have made the fee cheaper in real terms than it was. The current PoR settlement path charges only the eviction penalty on a confirmed third consecutive miss; it does not charge the single-miss or invalid-proof penalty parameters (§8).

**Mutability:** Tier-0, key `cip31.cbfs.min_relay_stake`.

### 7. Relay Challenge Bond

```
RELAY_CHALLENGE_BOND = 66 CBY
```

The CBY a challenger MUST post when calling `por_challenge(shard_id, byte_offset, byte_length)`. CIP-9 §5.6 defines the challenge lifecycle; this CIP pins its governed bond, fee, and bounty amounts. The bond and per-challenger/relay challenge limits make repeated speculative challenges costly.

**Lifecycle:**

- Bond is escrowed at `0x0B` until a valid response or expiry settlement after `POR_RESPONSE_WINDOW` and the congestion grace.
- A valid response within the window or grace returns the bond less `POR_CHALLENGE_FEE = 7 CBY`, retained in the challenge pool. An invalid response transaction is rejected; it does not resolve the challenge or itself cause a slash or bounty.
- An unanswered challenge after the window and grace emits a miss alarm and returns the bond less `POR_CHALLENGE_FEE`. With slashing disabled, the per-shard consecutive-miss count does not advance. With slashing enabled, a miss advances that count; a valid response resets it. A first or second consecutive miss does not slash stake or pay a bounty.
- On the third consecutive miss with slashing enabled, the Relay is disabled and `RELAY_EVICTION_PENALTY` is slashed up to its available stake (§8). The challenger receives the full bond and may receive `CHALLENGER_BOUNTY = 33 CBY` from the challenge pool. The paid bounty is `min(CHALLENGER_BOUNTY, available_pool, epoch_cap_remaining)`, where `available_pool = max(pool_balance − reserve, 0)` after crediting the slash's pool share and `epoch_cap_remaining = max(POR_BOUNTY_EPOCH_CAP − paid_this_epoch, 0)`. `POR_BOUNTY_EPOCH_CAP` (Tier-0, `cip31.cbfs.por_bounty_epoch_cap`, default unbounded) limits total bounties per rent epoch; `cip31.cbfs.por_pool_reserve` defaults to zero. The bond refund is independent of pool balance.
- If the challenged shard incarnation is removed or replaced, or the volume reaches terminal `GARBAGE_COLLECTING` before an unanswered challenge settles, settlement voids the challenge: the full bond is returned, the open slot is released, and there is no fee, miss alarm, slash, or bounty. Reversible `DELETED` status does not void a challenge.

**Mutability:** Tier-0, key `cip31.cbfs.relay_challenge_bond`. `POR_CHALLENGE_FEE` and `CHALLENGER_BOUNTY` are sub-keys, both Tier-0.

### 8. Slashing Schedule

The three governed penalty values referenced by CIP-9 §14 are:

```
POR_MISS_PENALTY        = 330  CBY     // Governed parameter; not charged by current PoR settlement
POR_FRAUD_PENALTY       = 3300 CBY     // Governed parameter; not charged for a rejected invalid proof
RELAY_EVICTION_PENALTY  = 13200 CBY    // Charged on a third consecutive miss with slashing enabled
```

**Current PoR settlement rule:** A single unanswered challenge is operational noise (Relays restart, NICs flap), so it raises an alarm without a stake slash. With slashing enabled, only a third consecutive miss for the same Relay and shard disables the Relay and charges `RELAY_EVICTION_PENALTY`, capped by its actual stake. A rejected invalid proof leaves the challenge open for a valid response or expiry; it does not charge `POR_FRAUD_PENALTY`. `POR_MISS_PENALTY` and `POR_FRAUD_PENALTY` remain governed values but are not applied by this settlement path. A void under §7 does not advance the miss count.

**Distribution of slashed Relay stake.** Slashed Relay CBY follows the same three-way proportions as storage fees — **10% / 1% challenge pool / 89% pro-rata to the *other* Relays** (the slashed Relay is excluded from the pro-rata distribution that epoch) — but not the same disposition of the 10%. Slashed stake is a CBY position, so its 10% is **burned**, which composes cleanly with CIP-3's deflationary design and recycles deterrent capital into the network rather than wholesale burn. Storage fees are nano-CBY and their 10% is credited to the Platform Fee Account `0x18` (§4), which is deliberately not a treasury share.

**Mutability:** All three penalty rows are Tier-0, keys `cip31.cbfs.por_miss_penalty`, `cip31.cbfs.por_fraud_penalty`, `cip31.cbfs.relay_eviction_penalty`.

### 9. Challenge Resolution Timing

PoR response verification resolves a valid challenge when the response transaction executes. An invalid response transaction is rejected and leaves the challenge open. An unanswered challenge may be settled only after `POR_RESPONSE_WINDOW` plus the congestion grace; the settlement rules are in §7. The 75-block runner-result dispute window does not delay or reverse this PoR path, and no `EvidenceInvalidityAppeal` is implemented for it.

### 10. Parameter Storage at `0x09` Governance

Governed parameters above are stored at the **Governance** system actor (`0x09`) in the CIP-12 centralized governance-parameter store, under logical paths `cip31.cbfs.<name>` (full key `system:gov:param:cip31.cbfs.<name>`). The Storage Manager (`0x0A`, CIP-9 §11.1) reads rent parameters for rent-epoch settlement and the transfer rate at channel checkpoint execution. Payer channel terms and operational gas/lifetime budgets are not silently created as governance defaults by this CIP.

These are governance **parameters**. The challenge-pool balance, escrowed challenge bonds, and per-epoch counters are runtime **state** and live separately at the **Relay Registry** system actor (`0x0B`) under `ras:*` keys (see §4 and §7) — the `0x0B` address in this CIP refers only to that escrow/pool/bond state, never to the parameters.

```
0x09 governance parameters (CIP-31), stored under the CIP-12 `system:gov:param:` prefix:
  cip31.cbfs.storage_fee_per_mib_per_epoch        u64 (nano-CBY)
  cip31.cbfs.transfer_fee_per_mib                 u64 (nano-CBY)
  cip31.cbfs.fee_split.platform_bps                   u16 (default 1000)
  cip31.cbfs.fee_split.challenge_pool_bps         u16 (default 100)
  cip31.cbfs.fee_split.relay_bps                  u16 (default 8900)
  cip31.cbfs.min_storage_balance_epochs           u16 (default 1)
  cip31.cbfs.min_relay_stake                      u64 (CBY)
  cip31.cbfs.relay_challenge_bond                 u64 (CBY)
  cip31.cbfs.por_challenge_fee                    u64 (CBY, default 7)
  cip31.cbfs.challenger_bounty                    u64 (CBY, default 33)
  cip31.cbfs.por_miss_penalty                     u64 (CBY)
  cip31.cbfs.por_fraud_penalty                    u64 (CBY)
  cip31.cbfs.relay_eviction_penalty               u64 (CBY)
  cip31.cbfs.max_shard_age_for_weighting          u16 (default 90)
```

### 11. Genesis-defaults Summary Table

| Parameter                                | Default       | Mutability | Key                                                |
| ---------------------------------------- | ------------- | ---------- | -------------------------------------------------- |
| `STORAGE_FEE_PER_MIB_PER_EPOCH`          | 2,905 nano-CBY | Tier-0     | `cip31.cbfs.storage_fee_per_mib_per_epoch`        |
| `TRANSFER_FEE_PER_MIB`                   | 66,700 nano-CBY | Tier-0   | `cip31.cbfs.transfer_fee_per_mib`                 |
| `STORAGE_FEE_PLATFORM_BPS`                   | 1000 (10%)    | Tier-0     | `cip31.cbfs.fee_split.platform_bps`                   |
| `STORAGE_FEE_CHALLENGE_POOL_BPS`         | 100 (1%)      | Tier-0     | `cip31.cbfs.fee_split.challenge_pool_bps`         |
| `STORAGE_FEE_RELAY_BPS`                  | 8900 (89%)    | Tier-0     | `cip31.cbfs.fee_split.relay_bps`                  |
| `MIN_STORAGE_BALANCE_EPOCHS`             | 1             | Tier-0     | `cip31.cbfs.min_storage_balance_epochs`           |
| `MIN_RELAY_STAKE`                        | 33,000 CBY     | Tier-0     | `cip31.cbfs.min_relay_stake`                      |
| `RELAY_CHALLENGE_BOND`                   | 66 CBY        | Tier-0     | `cip31.cbfs.relay_challenge_bond`                 |
| `POR_CHALLENGE_FEE`                      | 7 CBY         | Tier-0     | `cip31.cbfs.por_challenge_fee`                    |
| `CHALLENGER_BOUNTY`                      | 33 CBY        | Tier-0     | `cip31.cbfs.challenger_bounty`                    |
| `POR_MISS_PENALTY`                       | 330 CBY       | Tier-0     | `cip31.cbfs.por_miss_penalty`                     |
| `POR_FRAUD_PENALTY`                      | 3,300 CBY     | Tier-0     | `cip31.cbfs.por_fraud_penalty`                    |
| `RELAY_EVICTION_PENALTY`                 | 13,200 CBY    | Tier-0     | `cip31.cbfs.relay_eviction_penalty`               |
| `MAX_SHARD_AGE_FOR_WEIGHTING`            | 90 epochs     | Tier-0     | `cip31.cbfs.max_shard_age_for_weighting`          |
| Relay weight formula structure           | (§5)          | Tier-2     | `cip31.cbfs.relay_weight_formula`                 |

## Rationale

**Why a separate CIP rather than amending CIP-9 inline.** CIP-9 owns the **data plane** (shards, manifests, erasure coding, PoR mechanics). CIP-31 owns the **economic plane** (rates, splits, bonds, slashing magnitudes). Splitting them lets governance touch the economic surface (Tier-0 / Tier-2) without re-opening the data-plane spec. This mirrors CIP-3 ↔ WP §13 (mechanism vs parameter values).

**Why these specific values.** The storage rate is anchored to commodity object storage. Backblaze B2 ($6.95 per decimal TB / month = $7.64 / TiB / month ≈ $0.0896 / GiB / yr) is the reference floor — it is what a Relay operator would pay to simply resell hosted storage — and S3 Standard (~$0.28 / GiB / yr) is the ceiling users would otherwise pay for hot, durable storage. A Relay stores 1.5× raw bytes per logical byte (`K + M = 4 + 2` erasure coding) and keeps 89% of fees, so its break-even fee is ≈ 1.7× its underlying storage cost: roughly $35–70 / TiB / yr self-hosted on commodity hardware, ~$154 / TiB / yr reselling B2. The chosen rate (~$167 / TiB / yr, ≈ 1.82× B2) covers a self-hosted Relay 3–5× over, leaves margin even for a Relay backing onto B2, prices the PoR / stake / slashing risk premium that centralized providers do not bear, and still undercuts S3 — read-immediately storage with verifiable retrievability, without SLA-grade replication. The transfer rate matches B2's over-allowance egress price ($0.01 per decimal GB).

**Why `RELAY_CHALLENGE_BOND = 66 CBY`.** The bond locks challenger capital while a response is pending. A valid response or unconfirmed miss costs the challenger 7 CBY; a void returns the full bond. A full bond refund and a pool-bounded bounty require a confirmed third consecutive miss with slashing enabled. Thus a single induced miss does not create the fixed `+26 CBY` profit assumed by the earlier schedule.

## Security Considerations

1. **Challenge griefing.** The 66 CBY bond locks capital for each open challenge, and a valid response or unconfirmed miss retains the 7 CBY fee. A rejected invalid proof creates no bounty. Only a confirmed third consecutive miss with slashing enabled can pay a pool-bounded bounty; per-relay open-challenge and per-challenger epoch limits also bound issuance. A void caused by chain-state changes refunds the full bond without a fee.
2. **Rate cliff.** Storage rate as a Tier-0 parameter means a single proposal could spike rent 10× in one epoch. The 30/90-day review cadence in §1 is documentation only; the protocol-level guardrail is CIP-12's Tier-0 timelock (3 days) plus the per-epoch grace period (`STORAGE_GRACE_EPOCHS` = 1 storage epoch = `STORAGE_EPOCH_BLOCKS` = 86,400 blocks ≈ 24 h) that lets evicted volumes recover.
3. **Pro-rata gaming.** The weight formula `shard_count × shard_age` is hard to game: shard assignment is VRF-controlled (CIP-9 §5.3), shard age accrues only with valid PoR responses, and the 90-epoch cap prevents permanent capture.
4. **Slashed-stake recycling.** The 10/1/89 split for slashed Relay stake (§8) sends 89% to the *other* Relays. This composes with the deflationary signal (10% burn) and provides an economic incentive for healthy Relays to call out misbehaving peers — without creating a perverse incentive to frame innocent peers (since the bounty is in the 1% challenge pool, not in the 89% pro-rata share).

## Backwards Compatibility

Existing rent, split, bond and slashing parameters remain separate from the independently activated funded-channel extension. Old kind-27 read tickets/completions and signatures MUST NOT be reinterpreted as cumulative ACKs; capabilities, domains, records and replay state remain distinct. Neither payment mode is activated by this specification update. The rent/challenge parameters originally named here are either:

- a TBD row in CIP-9 §14 being filled in with a concrete value (10 of 13 rows), or
- a renaming / explicit-bps version of the schema already present (`STORAGE_FEE_BURN_RATE` 10% becomes `STORAGE_FEE_PLATFORM_BPS = 1000`; `POR_CHALLENGE_FEE_SHARE` becomes `STORAGE_FEE_CHALLENGE_POOL_BPS = 100`), or
- a brand-new field (`RELAY_CHALLENGE_BOND`, `POR_CHALLENGE_FEE`, `CHALLENGER_BOUNTY`, `MAX_SHARD_AGE_FOR_WEIGHTING`) that supplements but does not replace CIP-9 mechanism.
