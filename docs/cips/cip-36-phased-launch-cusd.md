---
title: "CIP-36: Phased Launch — Testnet Credits (CUSD) & Mainnet Airdrop"
description: A minimal, constitution-preserving launch path. Cowboy launches on a dedicated launch **testnet** where all billing is CUSD — a transfer-restricted, fiat-backed USD credit — and gas is paid in a hidden, auto-granted **test-CBY** that has no market value. Mainnet, and every whitepaper tokenomic rule it defines (fixed genesis supply, MIN_BASEFEE, validator/runner staking, distribution schedule), is left **unchanged**. Testnet participation seeds a mainnet CBY airdrop. No new consensus governance op, no on-chain token mint, no whitepaper amendment.
icon: rocket
---

<Note>
  **Status:** Draft (r3 — supersedes the single-network phase-register draft; r3 addresses adversarial-review findings on settlement atomicity, `burn_from` solvency, the BankActor `0x16` address, airdrop wash-resistance, and the §8.3 emission-model governance flag)
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-07-07 · **Revised:** 2026-09-22. The 2026-07-27 agent-binding amendment (historically CIP-28/36 r1.4) is retained in §7; the [CIP-28 interface appendix §1](../references/bankactor-interface#1-citation-and-authority-boundary) maps the older revision/section citations. It is not a new activation scheduled by this rewrite.
  **Requires:** CIP-18 (Payments / PaymentGate), CIP-20 (Fungible Tokens — hook + opt-in `burn_from`), [CIP-28 interface appendix](../references/bankactor-interface) (retained BankActor interfaces)
  **References (unchanged):** CIP-2 (Off-chain Compute), CIP-3 (Dual-Metered Gas), CIP-23 (TEE Execution & verification modes)
  **Proposed amendments (separate CUSD release):** CIP-18 (PaymentGate escrow-hop + multi-leg split), CIP-20 (opt-in `burn_from`), CIP-28 retained BankActor interfaces at `0x16` (not its actor-purchase allowance scope). Changes **no mainnet tokenomic rule and adds no governance op**; does not touch CIP-12. One whitepaper-adjacent item — setting the WP §8.3 Community/Airdrops *emission-model basis* to testnet metrics (supply and bucket size unchanged) — is **flagged for governance ratification** (§11, §13 item 2), not asserted as "unchanged". CIP-21/22 are **out of scope** (not implemented at launch; §11). CIP-13 stays dormant.
</Note>

> **Scope:** This CUSD launch proposal is separate from the native-CBY actor purchasing scope in [CIP-28](./cip-28-cowboy-agent-banking) and the Cloud external-USDC collection flow in [CIP-18](./cip-18-payments). Neither flow requires this proposal. Its fiat issuance, provider settlement, token-gas paymaster and ownership-hook requirements need a separate Cloud/Chain scope review before release; their description here is not evidence of a completed integration.

## 1. Abstract

Cowboy launches as **two networks**, not one network in staged phases:

| | **Launch testnet** (this CIP) | **Mainnet** (whitepaper — unchanged) |
|---|---|---|
| Billing | **CUSD** — 1:1 fiat-backed, transfer-restricted, spend-only credit | CBY / CUSD per whitepaper §8 |
| Gas | **test-CBY** — hidden, auto-granted on funding, no market value | CBY, `MIN_BASEFEE = 10,000`, 100% burn (WP §17) |
| Validators / runners | curated, operator-run (it is a testnet) | open & permissionless, staked (WP) |
| CBY | test-only; **discarded**, never bridged to mainnet | fixed genesis supply, real value |
| Governance | none of CIP-12's on-chain governance ops are added or invoked | CIP-12 as-is |

The full protocol stack — PVM, actors, Simplex BFT, dual-metered gas, the runner marketplace, PaymentGate, CBFS — runs on the testnet from day one. Users pay for inference and services in CUSD; they never see, hold, or manage the test-CBY that pays for gas. **Testnet participation and measured performance seed a mainnet CBY airdrop** (§4.3), which is the only link between the two networks.

The design is deliberately **minimal and constitution-preserving**. It reuses the CIP-20 transfer hook (for CUSD's transfer restriction), CIP-18 PaymentGate (for billing), and CIP-28 banking (for the fiat bridge and cards). It adds **no new consensus governance op, no on-chain token mint, and no whitepaper amendment.** Where the earlier single-network draft introduced a `launch_phase` register, `PhaseTransition`/`EmergencyOverride` governance ops, an on-chain bootstrap mint, and a binary-hash activation gate, this revision removes all of them — each was both a source of consensus/governance risk and unnecessary once the launch runs on a testnet rather than by amending mainnet.

## 2. Motivation

The goal is to retire **product-function and demand risk before any price risk exists** — without touching the constitution:

1. **UX risk** — agents should not hold or convert a volatile asset to pay for inference. On the testnet they pay in USD-denominated CUSD; gas is invisible.
2. **Operational shock** — network running costs should not move with a token price during the product-validation window.
3. **Investor / narrative risk** — a volatile day-one token price should not define the launch. Mainnet CBY (real value) launches on its own schedule, informed by measured testnet demand.
4. **Institutional access** — early users should not need crypto-native token handling; a funded dashboard account is enough.
5. **Reversibility** — a testnet is, by construction, a low-blast-radius environment: parameters, pools, and even genesis can be revised without a constitutional amendment or a live-chain migration.

A single big-bang mainnet TGE re-introduces risks 1–4 on the same day, against live actors and a live constitution. Launching on a testnet and bridging to mainnet **only** via a performance airdrop keeps every mainnet rule intact and every risk staged.

## 3. Scope & non-goals

**In scope (normative here):**
- The **launch-testnet** configuration: CUSD billing, hidden test-CBY gas, funded-account admission for spam control (§4–§7).
- The **CIP-18 / CIP-20 / CIP-28** integration needed for CUSD (§8, §11).
- The **mainnet-genesis airdrop framework** that credits testnet participation (§4.3).

**Explicitly NOT in scope (this CIP changes none of these):**
- **The whitepaper / mainnet tokenomics.** The fixed 1,000,000,000-CBY genesis supply (WP §8.1), the `MIN_BASEFEE = 10,000` / 100%-burn consensus rule (WP §17), runner and validator staking requirements (WP §5.2, Validator Set), and the §8.3 distribution schedule are **unchanged** (the mainnet airdrop draws from §8.3's existing *Community / Airdrops* earmark, §4.3 — not a schedule change). The testnet is a separate network and is **not** the constitutional mainnet, so its zero-cost gas and permissioned operation do not contradict any mainnet rule. See §11.
- **New governance mechanics.** No `launch_phase` register, no `PhaseTransition`/`EmergencyOverride` ops, no launch multisig, no on-chain CBY mint, no node-binary activation hash. CIP-12 is untouched.
- **The runner economic model.** Testnet runners are a curated, operator-run pool using existing CIP-2 / CIP-23 verification modes **unchanged** (§9). Any permanent redesign of runner staking/bonding for mainnet is a **separate future proposal**, not decided here.
- **A permanent CUSD-standard change.** CUSD's transfer restriction is a **testnet transfer-hook policy** (§6.2), not a new rule imposed on CIP-20 tokens generally.
- **CIP-21/22.** Not implemented at launch (application-layer; no dependency beyond CIP-20). CUSD simply never enters a pool or auction because no such flow is allowlisted by its hook (§6.2). This is a mechanical consequence, not a change to CIP-21/22.

## 4. The two networks

### 4.1 Launch testnet

A dedicated Cowboy network with its **own genesis**. Being a testnet, its genesis parameters are set for the launch product and carry no constitutional weight:

- **Gas** is denominated in **test-CBY** (§5) and runs the ordinary CIP-3 metering and lane machinery — no consensus change to the fee model. The testnet genesis gas schedule is the testnet's own; it does not touch, and is not bound by, the mainnet `MIN_BASEFEE` floor.
- **Billing** is **CUSD** (§6) through CIP-18 PaymentGate.
- **Spam control** is **funded-account admission + a per-principal quota** (§7), because free test-CBY means gas is not the economic spam gate.
- **Validators and runners** are curated and operator-run. Because validators are operator-run, admission (§7) is a mempool/RPC policy and need not be a consensus rule.

### 4.2 Mainnet

Governed by the whitepaper and existing CIPs, **unchanged by this document**. Real CBY has a fixed genesis supply and real value; gas, staking, burn, and distribution follow the whitepaper. CIP-36 introduces nothing here except the airdrop allocation below, which is a genesis-distribution decision, not a protocol change.

### 4.3 Graduation: the mainnet airdrop

The two networks are joined by exactly one thing: a **mainnet-genesis CBY airdrop that credits testnet participation.**

- The airdrop is funded from an **existing bucket of the whitepaper's genesis distribution** — WP §8.3's **Community / Airdrops** allocation (2% = 20,000,000 CBY, already earmarked for drops). It is **not** a new issuance and does **not** raise the fixed supply. Note the distinction the CIP holds itself to: the bucket's **size (2% / 20M) is unchanged**, but this CIP sets its **eligibility basis** to testnet-performance metrics rather than the §8.3 row's illustrative "2 drops (TGE + 6 months)" — a mainnet-genesis distribution-parameter decision that MUST be ratified through governance (§11, §13 item 2), not asserted as automatically "unchanged". If a metric-based launch airdrop needs more than the earmarked amount, that too is a distribution-parameter decision made at mainnet genesis, not a supply change — but the default is to draw from the existing earmark.
- Eligibility and weighting are computed from **published, reproducible testnet metrics** (e.g. CUSD spend, verified job volume, uptime) fixed at a snapshot and published before distribution, so the allocation is auditable and not operator-discretionary after the fact.
- **The metrics MUST be sybil- and wash-resistant.** Raw "CUSD spend" is gameable: an attacker can deposit fiat, spend CUSD into PaymentGate to a **self-operated** registered provider, and settle that revenue back to fiat via `burn_from` (§6.3), recovering most of the outlay while inflating its spend metric. Two backstops apply, at different times: (a) on the launch testnet the payout-provider pool is **curated and operator-run** (§9), so an attacker cannot freely self-register a provider to receive the wash — self-dealing is gated at admission, not just measured; (b) the wash surface reopens once **third-party providers** onboard (§13.5), so the metric definition (§13) MUST regardless exclude self-dealing round-trips (spend to a provider under the same compliance/`issuance_principal` identity). A same-principal check alone is insufficient — a **two-identity split** (payer under principal A paying a provider under principal B, both controlled by one actor) defeats it — so the metric MUST additionally weight *verified* job output over gross spend and cap per-principal contribution. Absent (a) and a robust metric, airdrop weight is farmable at ~Stripe-fee-plus-second-KYC cost.
- **test-CBY is never bridged.** It has no market value, does not migrate, and is discarded; only the *metrics* inform the airdrop. This removes any cross-chain supply-conservation surface — there is no lock-and-mint, no double-claim, no bridge dependency at launch.

The airdrop's exact metric definition, snapshot, and allocation size are governance/foundation decisions to be fixed before mainnet genesis (§13); they are out of band for the testnet's operation.

## 5. Test-CBY — the hidden testnet gas token

Test-CBY is the testnet's gas asset. It exists so the protocol's metered-gas machinery runs unmodified, while being invisible and free to users:

- **Hidden & unmanaged.** Clients (the desktop app, CLI, dashboard) never display a test-CBY balance or ask a user to acquire, budget, or approve gas. Gas is an implementation detail of the testnet.
- **Auto-granted on funding.** When an account funds CUSD (deposits USD → CUSD is minted, §6), the BankActor grants that account enough test-CBY to cover ordinary usage. Grants are sized generously; test-CBY is not a scarce resource the user manages.
- **No value, no market, non-transferable.** Test-CBY cannot be transferred peer-to-peer, swapped, redeemed, or bridged. It is not the whitepaper's CBY and confers no claim on mainnet CBY (the airdrop is metric-based, §4.3).
- **Gas is therefore not the spam gate.** Because test-CBY is free UX, an attacker's marginal gas cost is ~0. Spam is bounded instead by the CUSD-funded admission rule and per-principal quota in §7.

Because test-CBY runs the normal CIP-3 gas path on a testnet genesis schedule, **no `launch_mode` / zero-basefee consensus change is introduced** (the earlier draft's launch-mode gas edit, and its attribution of a `min_basefee == 0` rejection to CIP-3, are dropped). Metering telemetry is still produced every block and is exactly what calibrates mainnet gas planning.

## 6. CUSD — Cowboy USD billing credits

### 6.1 Definition

CUSD is a **CIP-20 token instance**, not a new token standard.

- **Peg.** 1 CUSD = $1, backed by fiat collected via Stripe and held by the Cowboy Banking operator (CIP-28 `bank_id = 1`).
- **Mint path.** The CIP-28 fiat bridge only: Stripe settlement → the off-chain Compliance Gateway issues a `FiatMintVoucher` after the chargeback-risk window → `BankActor.MintFromFiatVoucher` mints CUSD to the payer's card. CUSD's CIP-20 `mint_authority` is the **CIP-28 BankActor** (see §6.5 on the actor address). No other mint path exists; mint is 1:1 against settled fiat.
- **Decimals.** 6 (USDC convention).
- **Transfer-restricted, spend-only.** CUSD is a *billing credit*, not a currency: it cannot move peer-to-peer, cannot be swapped to any external asset, and for users/agents is **non-redeemable** for fiat. It leaves an account only by being *spent* into PaymentGate (§6.2). Provider revenue is settled off-chain via a burn (§6.3).

### 6.2 Transfer restriction — a testnet CIP-20 hook policy

CUSD sets `transfer_hook` to an allowlist hook. This is a **per-token hook configuration on the launch testnet**, not a change to the CIP-20 standard, and answers the open question raised in review: the restriction is this network's policy, not a permanent global rule imposed on other tokens.

CIP-20's hook is `ITransferHook.can_transfer(token_id, from, to, amount) -> bool` (four arguments; **no caller/spender identity** — the hook sees only the token, the endpoints, and the amount). Because the hook cannot see *who* invoked the transfer, the allowlist is expressed in `from`/`to` terms, and **PaymentGate is a mandatory escrow hop** so that every billing leg has PaymentGate on exactly one side. (The one flow that needs more than the raw endpoints is `card ↔ owner`, which resolves the card's owner from BankActor state; if that ownership lookup fails, the transfer is **rejected**. This is a single bounded cross-actor read and MUST fit the CIP-20 hook budget — 50,000 Cycles / 50,000 Cells — via the documented cross-actor read path; the exact read mechanism is an implementation item (§13). If it cannot fit the hook budget, the `card ↔ owner` self-custody flow is dropped without loss of function: CUSD simply stays card-resident, since it is minted to the card (§6.1) and spent from the card into PaymentGate.)

```
can_transfer(token_id, from, to, amount) -> bool:
    # token_id is CUSD. PG = PaymentGate (CIP-18); BANK = CIP-28 BankActor.
    # Permitted flows ONLY:
    #   card/account -> PG            # pay-in: the sole way funds reach a payment
    #   PG           -> <any>         # pay-out: once from == PG, `to` is unrestricted
    #                                 #   (PaymentGate's handler decides recipients)
    #   BANK         -> card/account  # mint / top-up
    #   card/account -> BANK          # token-gas paymaster (M3.6): a card pays gas in CUSD
    #                                 #   by moving U to the BankActor, which fronts the CBY
    #                                 #   fee. BANK is a sink (a keyless system actor that never
    #                                 #   sends to an arbitrary P2P recipient), so this cannot
    #                                 #   exfiltrate funds; see the amendment note below.
    #   card <-> its owner account    # self-custody moves within one identity (BankActor read)
    # Everything else — including card/account -> a treasury/provider directly — is rejected,
    # so no payer can bypass PaymentGate's fee split.
    # SECURITY GATES = the authenticated `from` positions (PG, BANK), plus the one sink
    # destination `-> BANK`. The hook cannot see a recipient's *type*, so it enforces "funds
    # leave an account only INTO PaymentGate or the BankActor sink"; which parties PaymentGate
    # then pays is PaymentGate's authenticated concern, not the hook's.
```

> **Amendment (M3.6 token-gas paymaster).** The `card/account -> BANK` destination is added to the permitted flows to support paying gas in CUSD (§6.3 / CIP-28 M3.6): a `Token(U)` card settles gas by moving `covered_u` U to the BankActor `0x16`, which debits its own CBY reserve to fund the actual burn/tip, conserving both supply and the fee split. This is a **narrow, sink-only** exception to the "funds leave an account only INTO PaymentGate" rule: `0x16` is a keyless system actor with no PVM code, so it can never *send* CUSD onward to an arbitrary recipient — the CUSD it accumulates can only be moved by an authenticated `BANK -> card/account` flow or burned, so the exception does not open a payer→treasury/provider bypass of PaymentGate's fee split. Gated behind `BANK_ACTIVATION_HEIGHT` and dependent on the M4 hook admitting `-> 0x16`.

Routing settlement **through PaymentGate** (payer → PG, then PG → each recipient) rather than a direct `transfer_from(payer, recipient)` lets the fee split work under a hook that has no caller context, without extending the CIP-20 signature. A settlement MUST be **all-or-nothing**: the `payer → PG` pay-in and every `PG → recipient` payout leg either all apply or none do, so PaymentGate never strands escrowed funds or double-charges. **Prevalidation is necessary but does not establish atomicity across fallible storage writes:** because the node's speculative execution engine does **not** roll back partial writes when a handler returns `Err` (the known pay-then-fail conservation gap), the PaymentGate handler MUST validate every leg (payer solvency, each recipient, the split sums to the escrowed amount) **before** performing any transfer, and provide a commit/rollback or recovery boundary for failures during mutation. The all-or-nothing requirement needs failure-injection acceptance; it is not established by checking balances first. This escrow-and-split behaviour extends CIP-18 settlement: CIP-18 already routes fixed fee legs (actor / protocol / gateway) to distinct treasuries, but its `PaymentIntent` names a single `recipient` — the normative addition here is **arbitrary multi-party payout** (runner + aggregator + protocol/gateway) escrowed through PaymentGate, specified as such (§11).

### 6.3 Conservation, redemption, and provider settlement

- **Mint conservation.** CUSD is minted 1:1 only against fiat that has **settled past the Stripe chargeback window** (this proposal's off-chain fiat collection policy, §10). The reserve↔supply relationship is a custodial, off-chain-audited invariant (§10) — this is inherent to a fiat-backed credit and is disclosed, not hidden.
- **User redemption.** None. Users/agents cannot burn CUSD for fiat; top-ups are consumption credits only.
- **Provider settlement (burn, not transfer-out).** Registered service providers (runners, storage, gateways with a payout agreement) settle CUSD revenue to fiat **off-chain**. The on-chain step is a **CIP-28 BankActor operation**, `settle_provider(provider, n, settlement_id)`, with replay protection. **Required release behavior:** the consumed marker and burn MUST commit together or leave neither effect. **Inspected implementation at node `44e1eb129`:** the handler prechecks authorization, replay, positive amount and provider solvency, then writes the consumed marker **before** invoking `burn_from`. A failure after that marker can strand the ID; prevalidation alone does not prove atomicity or rollback. This agrees with the [CIP-28 interface appendix §3.3](../references/bankactor-interface#33-operator-fiat-and-provider-operations). The current ordering is:
  1. **Authorization.** The caller MUST be the bootstrap bank's `BankOperator` key (`bank_id = 1`); fiat-mint vouchers use the separate `fiat_mint_signer` role. `settle_provider` is **not** open to arbitrary callers, so a provider cannot self-settle or front-run another provider's `settlement_id`.
  2. **Off-chain `settlement_id` requirement.** `settlement_id` is **gateway-assigned and unique** (bound to the off-chain fiat payable, e.g. `HMAC(gateway_secret, payable_ref)`), not caller-chosen, so the key is neither predictable nor forgeable by the provider.
  3. **Replay + solvency check.** If `settlement_id` is already in the consumed-set → **no-op** (no re-burn). Otherwise require `n > 0` and verify `n ≤ balance(provider)` before writing the marker. Downstream burn checks and writes remain fallible.
  4. **Apply.** Only once (1)–(3) pass: record `settlement_id` as consumed, **then** invoke CIP-20 `burn_from` to burn `n` CUSD from the provider account, carrying the ID in the burn `reason`; emit `ProviderSettled` only after success. An already-consumed retry returns a no-op, so it cannot establish whether the earlier burn completed.

  **Remaining acceptance work:** demonstrate a storage/transaction boundary or recovery mechanism that handles failures between the marker, burn and event without lost settlement or duplicate burning. Until then, a consumed ID alone is not payout evidence; reconcile the actual burn and its amount before off-chain payment. This documentation correction does not implement that atomicity requirement.

  (Idempotency and the consumed-set live in this CIP-28 wrapper, §11; `burn_from` itself is only the low-level burn and carries no replay state.) The off-chain fiat-payable is reconciled against the same `settlement_id`, so the on-chain burn and the off-chain payout are **linked by the idempotency key, not atomic across the trust boundary** — the required invariant is that a given `settlement_id` burns `n` at most once and pays out at most once; marker presence alone does not prove a completed burn. Burned units cannot be re-spent, so a provider's balance is either spent on-platform or burned-on-settlement, never both.

### 6.4 Pricing

All **customer-facing** network services — inference, hosted storage, hosted streams, Gateway ingress, hosted secrets — are **priced in USD and billed in CUSD** through PaymentGate (CIP-18 is denomination-agnostic; CUSD joins its accepted-asset set). Target margins follow the storage model (≈2× commodity cost). The protocol-metered fees beneath those products are a separate CBY layer defined in §6.6: where no hosted product wraps a protocol service yet, the party paying the protocol fee is an operator paying the §6.6 layer directly, not a §6.4 customer, and there is no second USD price. There is no CBY settlement of customer billing and no CBY/USD oracle on the testnet.

### 6.5 Actor address

CUSD's `mint_authority`/`burn_from_authority` is BankActor at **`0x16`**. Its key roles, bootstrap bank `bank_id = 1`, instruction identifiers and implementation status are defined by the [CIP-28 interface appendix](../references/bankactor-interface). At the inspected node revision `44e1eb129`, the address and dispatch exist and `BANK_ACTIVATION_HEIGHT = 10`; the old `0x13` allocation and unimplemented-address-mechanism statements are obsolete. Initialization and the gates for lazy expiry and chain-bound voucher signatures are separate from address allocation. This source evidence does not approve or prove a deployed CUSD release.

### 6.6 Protocol fees versus product pricing

§6.4 governs what a **customer** pays: network services are priced in USD and
billed in CUSD through PaymentGate. That is unchanged.

Protocol-metered fees are a different layer and are denominated in **CBY**:
CBFS storage and transfer rent (CIP-31), CBQS stream rent (CIP-39),
CBSS, and gas. These are internal accounting between an owner, a provider, and
the protocol. A volume or stream owner pays them directly from a native CBY
balance with no PaymentGate hop — when that owner is also a Cowboy Cloud
customer, what they buy through §6.4 is the hosted product above this layer,
and until such a product exists for a given service the owner is acting as an
operator whose cost basis is this layer itself.

The two layers meet at the **administered CBY rate**, defined here and
referenced everywhere else:

```text
CBY_ADMINISTERED_USD = 0.15    // USD per CBY — rate version 1, effective 2026-08-03
```

Cowboy Cloud operates the validator set and accounts are fauceted, so CBY has
no market price; this rate is operational policy set by Cowboy Labs, not a
protocol object. It is deliberately NOT on chain — settlement is pure CBY, so
an on-chain price would add an oracle to consensus for no benefit.

**This section is its only definition.** The value carries a rate version and
an effective date, and every change to it lands as a CIP-36 amendment that
increments the version, states the new effective date, and records the old
value — the amendment history of this section is the publication point. A USD
figure elsewhere in the CIP series is computed at the then-current rate
version, so a rate change obligates the amending party to re-run the CIP-31
§1 band review the rate feeds; sibling figures are advisory conversions, and
the band review — not the prose — is what acts on a change. Any CIP that
quotes a USD figure derived from this value MUST reference it rather than
restate it: a rate duplicated across specs drifts silently, and the specs
would then disagree about what their own bands mean. CIP-31 is the one that
does (amended 2026-08-24: this sentence previously named CIP-39 as the second
such CIP, citing cowboy#290; CIP-39 document version 2 quotes no USD figure at
all, so the obligation is stated generally rather than against a list that has
gone stale). CIP-31 carries the USD target band
and the Tier-0 review that retunes its CBY constant when this rate moves;
that review is unevaluable without a single authoritative number, which is
what this is. CIP-39 quotes no USD figure at all as of its document version 2, whose
flat `cbqs.stream_rate_per_block` is an ordinary governance parameter with no
schedule, no band, and no genesis freeze (amended 2026-08-24 alongside CIP-39
document version 2; the superseded reading described the version-1 base-rate
schedule, which that revision removed). Repricing it is a governance write
reviewed against this same
rate, not a runtime retune.

Reviewed on the CIP-31 §1 Tier-0 cadence — 30 days post-TGE, 90 days at
steady state. If a market price for CBY emerges, Cowboy Labs — the actor that
sets this rate — judges whether it is load-bearing (a venue with sustained
depth rather than an incidental print) and, if so, proposes replacing the
administered rate with it in a CIP-36 amendment; nothing is automatic, and
the CBY constants the rate feeds still move only by Tier-0 proposal under
CIP-12's timelock after the band review is re-run.

Denominating protocol fees in CUSD was considered and rejected for v1: it
would require fee escrow to move to the token ledger, `amount_cby` wire fields
to change, and a conversion path to exist, none of which is true today. A
denomination change cannot fix a calibration problem, and pricing against the
administered rate directly fixes it without inventing a settlement path the
implementation does not have.

The 10% share of the CBFS and CBQS fee splits is **credited to the Platform
Fee Account (`0x18`) rather than burned** — a deliberate reversal of the
earlier burn disposition, decided 2026-08 together with the CBY
denomination, on grounds that do not depend on CUSD: the share is
marketplace revenue (the owner pays, providers do the work, the platform
takes a cut), and burning it would destroy both the revenue and the
on-chain record of it, converting a business take into unaccounted monetary
policy. The credit preserves optionality a burn does not — including
burning later. `0x18` sits in the keyless reserved system range, so its
withdrawal authority is genesis-defined under COW-2915, a mainnet-genesis
blocker resolved before any real value accrues; until then the account is
accrual-only by construction. Slashed relay stake is the deliberate
contrast: a deterrent capital position, not revenue, and its 10% share
still burns (CIP-31 §8).

## 7. Spam control — funded-account admission

With gas free (test-CBY), spam is bounded at admission. A transaction is admitted only if **both** hold:

1. **Funded fee-payer.** The fee-payer resolution (account, owner, or CIP-28 card via `fee_payer_override`) reaches an account whose CUSD balance ≥ `min_funded_balance` (default $0.50), or is system-allowlisted (system actors, validators, operator infra). If resolution *fails* (no resolvable payer, or the override points at a non-existent card), the transaction is **rejected** — never treated as allowlisted.
2. **Under the per-principal block quota.** Each **canonical principal** may occupy at most `max_admissions_per_principal_per_block` slots per block (default small, e.g. 8). Without this, one funded account passes check (1) once and then floods the block at zero marginal cost.

**Canonical principal (the sybil hinge).** The quota is keyed neither by the raw fee-payer address nor by a card's mutable owner — CIP-28 lets one operator mint many cards and re-parent them to fresh EOAs, which would multiply quota buckets without new funding. The key is an **immutable, authenticated `issuance_principal: [u8;32]`** stamped on the card at issuance and never mutated by transfer or re-parenting, and it is **not caller-supplied**: the **consented** issuance path (`BankIssueCardV2`, opcode 212) requires an `IssuancePrincipalVoucher` signed by the compliance-gateway/bank key binding `{ bank_id, issuance_principal, owner_or_agent, agent, nonce, expiry }` (the `agent` field added r1.4 — see the enforcement note after the layout); the BankActor verifies the signature and `nonce`/`expiry` replay protection and writes the signed principal. The **permissionless** path (`IssueCard`, opcode 200) takes **no** voucher and records `issuance_principal = 0` (an *unconsented* card). So `card.issuance_principal != 0` is precisely the on-chain witness that a gateway approved this card's issuance — the property CIP-28 §3.2 (r1.4) relies on to gate the default-card pointer. **The voucher is bound to the card owner** — the BankActor requires `owner_or_agent == the BankIssueCardV2 sender` (= tx.from) and keys replay on `(bank_id, sender, nonce)`, so a broadcast voucher cannot be wrapped by a different owner. The BankActor verifies against the **bank operator key** (not `fiat_mint_signer`), so a non-fiat-bridge bank can still issue consented cards. The signing preimage is a domain-tagged **fixed big-endian** concatenation (the exact bytes the gateway MUST sign; pin a golden vector), keccak256-hashed:
```
keccak256(
  b"CowboyBankIssuancePrincipal\x02"   (28 bytes, domain tag — distinct from the
                                        FiatMintVoucher domain so signatures can't cross-replay.
                                        Bumped \x01 -> \x02 in r1.4: the layout changed (agent
                                        added), so the version byte MUST advance to keep the r1.3
                                        and r1.4 golden vectors from sharing a tag.)
  ‖ bank_id            (4  bytes, big-endian u32)
  ‖ issuance_principal (32 bytes)
  ‖ owner_or_agent     (20 bytes)
  ‖ agent              (20 bytes — the card's agent; r1.4. NOT a voucher wire field: the
                                   verifier reconstructs it from the `BankIssueCardV2` (op-212)
                                   instruction's `agent` parameter — see the enforcement note.)
  ‖ nonce              (8  bytes, big-endian u64)
  ‖ expires_at_block   (8  bytes, big-endian u64)
)
```
As with the FiatMintVoucher (CIP-28 §3.3), the preimage binds bank_id but not chain_id, so the operator signing key MUST NOT be reused across chains; intra-chain replay is blocked by the single-use `(bank_id, owner_or_agent, nonce)` marker. The gateway derives it deterministically per compliance identity — `HMAC(gateway_secret, "cowboy/issuance-principal/v1" ‖ compliance_id)` — so one real-world identity collapses to one bucket while the on-chain value is not dictionary-attackable to the raw Stripe id. For an ordinary EOA with no card, the key is the account address. **Enforcement note (from review):** one-principal-per-identity is enforced *off-chain* by the gateway; consensus/admission only reads the signed on-chain principal. The derivation key MUST be **rotation-stable** (a versioned/HSM-held key or a persistent `compliance_id → issuance_principal` registry) so a routine key rotation does not re-issue a fresh principal (a new bucket) to an existing identity. A compromised gateway signing key is a single point that can mint distinct principals; it is a launch-security control (§10), not a consensus guarantee.

**Agent binding (r1.4 — enforcement).** The `agent` in the preimage is **not** a field of the `IssuancePrincipalVoucher` wire struct; it is the `agent` parameter of the `BankIssueCardV2` (opcode 212) instruction that consumes the voucher. The verifier reconstructs the preimage using `instruction.agent` and recovers the signer, so a gateway signature commits to the *specific* agent the owner will name. Without this, an owner could take one legitimate voucher issued for `agent = self` and submit `BankIssueCardV2{ agent: V }` for an arbitrary victim `V` — producing a card with `issuance_principal != 0` that attests nothing about `V`, which fully re-opens the CIP-28 §3.2 poison-default DoS the amendment exists to close. So the rule is normative: **the BankActor MUST reconstruct the consent preimage with the `IssueCard`/`BankIssueCardV2` instruction's own `agent`**, and issuance fails closed if the recovered signer ≠ the bank operator key. **Cross-repo (consensus):** every node MUST build this preimage identically — `instruction.agent` in the `agent` slot, at the r1.4 tag/layout — or two conformant nodes derive different preimages and disagree on signature validity (a fork). This is option (a) of the review: **no** new codec wire field (no mid-struct insertion in `cowboy-protocol-codec`'s `IssuancePrincipalVoucherFields`; its `Write` order is unchanged), only the preimage builder gains the `instruction.agent` input. **Release evidence:** the inspected node already uses the agent-bound consent preimage and `BANK_ACTIVATION_HEIGHT = 10`; the older claim that a dormant bank height proves no existing cards is withdrawn. Operator/fiat signer chain-binding gates are distinct and still require matching signer behavior. Any CUSD release must verify these contracts; the actor-purchase rewrite neither activates them nor creates a compatibility requirement for disposable devnet.

**Determinism.** The funded and quota checks read **parent-block state** (the state root the block builds on), not sequential intra-block state, so every operator node computes the same admission decision independent of ordering; settlement still debits live state sequentially. On the launch testnet, admission is a **mempool/RPC policy** (validators are operator-run); no consensus validity rule is introduced. Note the funded-balance rule is a **gate, not a meter**: it checks `balance ≥ min_funded_balance` and does not debit, so a single top-up admits `max_admissions_per_principal_per_block` slots every block indefinitely at zero marginal cost (bounded only by CIP-3 lane budgets and the per-principal quota). The metered backstop is the optional `admission_charge` (default 0), which adds a nominal per-tx CUSD cost to price sustained load if the quota alone proves insufficient (§13 item 4).

## 8. Payments & banking

- **CIP-18 PaymentGate** is the sole billing path: CUSD is added to its accepted assets, and settlement escrows through PaymentGate (§6.2). Per-job CUSD splits (runner payout, aggregator fee, protocol/gateway fees) are PaymentGate settlement policy — invisible to consensus.
- **CIP-28 Banking** provides the fiat bridge (`MintFromFiatVoucher`), cards, the `issuance_principal` (§7), and provider payout `burn_from` (§6.3). The retained interfaces are dependencies of this separate CUSD release, not prerequisites for CIP-28 actor purchases.
- **Runner result verification** uses the existing CIP-2 / CIP-23 modes unchanged (§9). The **proof-of-traceability** work (companion, tracked separately) strengthens result provenance and is referenced, not redefined here.

## 9. Runners, actors, and the harness direction

- **Runners (testnet).** A **curated, operator-run pool** (team/foundation), selected and verified with the **existing CIP-2 / CIP-23 machinery unchanged** — stake-weighted selection, committee sizing, and the `TeeAttested` / `StructuredMatch` / `MajorityVote` / `Deterministic` verification modes all as specified today. Because the pool is operator-run on a testnet, no bond, no reputation-tier redesign, and no change to CIP-2/CIP-23 is required or made here. **Any permanent mainnet change to runner staking/bonding is deferred to a separate proposal** — deliberately not settled in this CIP (this is the change that would otherwise conflict with WP §5.2, so it is kept out).
- **CIP-13** (runner delegation) stays **dormant**: with no bond on the testnet, there is nothing to delegate against. No CIP-13 change is made.
- **Actors as harnesses (informative).** The product direction is that Cowboy **actors** are *harnesses* — reusable capability adapters (e.g. an exchange-trading harness, an API-key manager, a GPU-procurement router), while **runners** execute AI work. A user's agent discovers a harness (search → an actor offering the capability) and interacts with it. This motivates the testnet-first launch (bootstrap a harness ecosystem and real demand before token price) but imposes no protocol requirement here; harness samples are a separate product/demo deliverable (§12).

## 10. Security & compliance considerations

- **Spam at zero-cost gas.** Bounded by funded-account admission + per-principal quota (§7); the canonical-principal key collapses one identity's cards into one bucket, so sustained volume requires many distinct, Stripe-traceable fiat top-ups. Lane budgets (CIP-3) still bound per-block resource use.
- **Sybil / gateway trust.** The issuance-principal design forces one bucket per compliance identity, but its integrity rests on the off-chain gateway signing key (§7). Key custody (HSM), rotation-stability, and monitoring for anomalous principal issuance are launch-security controls, not consensus guarantees. Document the failure mode (compromised gateway → multiplied principals → spam) and its detection/response.
- **CUSD peg / reserve.** Custodial by construction: the fiat reserve ↔ on-chain CUSD supply relationship is maintained and audited off-chain, and CUSD is minted only against settled (post-chargeback-window) fiat. A post-settlement chargeback is handled by the off-chain fiat reserve/risk policy of this proposal; residual risk is a disclosed custodial assumption, not a protocol invariant.
- **Regulatory posture.** CUSD is deliberately **not a stablecoin**: transfer-restricted (allowlisted flows only), non-redeemable consumption credit — closer to a prepaid platform credit than a payment instrument. Provider payout is a separate off-chain commercial payable settled by burn, not a redemption feature. Counsel review of this characterization is a launch gate.
- **`burn_from` is a confiscation surface.** As specified (§11), `burn_from(token, account, amount, reason)` lets the BankActor burn CUSD from **any** account, not only provider payout accounts — its motivating use (§6.3) is narrower than its capability. Within CUSD's already-custodial model this is acceptable (the operator can already gate mint and reserve), but it is disclosed here as a power over user CUSD balances, contingent on BankActor key custody (HSM, §7). It exists only for CUSD (opt-in per §11, default `None` for every other token) and cannot be exercised against any non-CUSD balance.
- **Address allocation.** BankActor is allocated at `0x16`; see §6.5 and the CIP-28 interface appendix. Address allocation is not an outstanding activation-mechanism decision.
- **No consensus/governance surface added.** Because there is no launch register, no governance mint, no phase-transition op, and no binary-hash activation, the classes of fork / double-mint / governance-capture risk that a single-network staged-mint design carries do **not** exist here.

## 11. Relationship to existing specs

CIP-36 **proposes amendments to CIP-18, CIP-20, and the retained BankActor interfaces of CIP-28** for its separate CUSD release. Those additions do not enlarge the actor-purchase allowance scope. It changes **no mainnet tokenomic rule and adds no governance op**, and does not touch CIP-12. The one whitepaper-adjacent item that needs governance ratification — setting the WP §8.3 Community/Airdrops *emission-model basis* to testnet-performance metrics (supply and bucket size unchanged) — is **flagged, not asserted away**, in the Whitepaper row below and §13 item 2. Everything else is unchanged.

| Spec | Relationship |
|---|---|
| **Whitepaper (constitution)** | **No tokenomic rule amended.** Mainnet supply (1B genesis), `MIN_BASEFEE = 10,000` / 100% burn, runner & validator staking, and the **size** of the §8.3 distribution buckets are unchanged; the launch network is a **testnet**, not the constitutional mainnet, so its free gas / permissioned operation contradict nothing. **One item needs governance ratification, and is flagged here rather than asserted away:** WP §8.3's Community / Airdrops row lists an *emission model* of "2 drops (TGE + 6 months)". This CIP's §4.3 draws from that same 2% / 20,000,000-CBY bucket (no new issuance, no supply change — that part is genuinely unchanged) but sets the airdrop's **eligibility basis** to reproducible testnet-performance metrics instead of the row's illustrative time-based drops. Setting that basis is a **mainnet-genesis distribution-parameter decision** (§13 item 2); to the extent the §8.3 emission-model cell is read as normative constitutional text, the basis change MUST be ratified through the genesis-parameter / governance process (**not** treated as automatically "unchanged"). CIP-36 does not decide that governance question — it scopes precisely what is untouched (supply, bucket size) and surfaces what is not (the emission-model basis). |
| **CIP-12 (Governance)** | **Unchanged.** No `launch_phase` register, no `PhaseTransition`/`EmergencyOverride` ops, no launch multisig, no on-chain mint. Nothing enlarges `0x09`'s `Payload` enum or its execution logic (which per CIP-12 §7.3 would be a Tier-4 action). |
| **CIP-18 (Payments) — amended** | This separate proposal would add CUSD acceptance; the CIP-18 native release supports CBY only. Asset fields in a codec do not establish multi-asset settlement. **New:** settlement **escrows through PaymentGate** (payer → PG → recipients) with **arbitrary multi-party payout** — CIP-18 already splits fixed fee legs to distinct treasuries, but its native PaymentIntent names the service actor and authorizes a total; the current policy selects the treasury; escrowing through PG and paying out N parties (runner + aggregator + protocol/gateway) is the normative addition, needed so the CUSD hook (no caller context) permits the split without a fee-bypass. All-or-nothing remains a release requirement: prevalidation must be paired with a demonstrated commit/rollback or recovery boundary (§6.2). |
| **CIP-20 (Fungible Tokens) — amended (opt-in)** | The transfer hook is **used, not changed** — CUSD relies on the existing 4-arg `can_transfer(token_id, from, to, amount)` (§6.2). The **normative addition** is a per-token `burn_from_authority: Option<Address>`, **set once at creation, immutable, default `None`** (existing tokens gain no power; no rotation/escalation surface), plus `burn_from(token, account, amount, reason)` callable only by that authority — validates authority, reason length, **and `amount ≤ balance(account)` (which also guarantees `amount ≤ total_supply`) — all before any mutation**, rejecting (no state written) if the account is insolvent, then atomically decrements balance **and** `total_supply`, emitting `Burn{token, authority, account, amount, reason}`. The explicit solvency precondition is required so the burn can never underflow a balance (a mint-from-nothing supply break) nor trap mid-handler under the no-rollback engine (§6.2). CUSD sets `burn_from_authority = BankActor` for provider settlement (§6.3). |
| **CIP-28 retained BankActor interfaces — CUSD release dependency** | BankActor at `0x16` (§6.5 and the CIP-28 interface appendix), `MintFromFiatVoucher`, cards, the immutable `issuance_principal` from a signed `IssuancePrincipalVoucher` (§7), and a **gateway-authorized, replay-protected provider-settlement op** `settle_provider(provider, n, settlement_id)` that keeps a consumed-`settlement_id` set in BankActor state and wraps the CIP-20 `burn_from` primitive, prechecking authorization + solvency, then recording the ID before the burn; post-marker failure handling remains the release gap in §6.3. |
| **CIP-2 / CIP-3 / CIP-23** | **Unchanged.** Testnet runners use existing verification modes; the gas/metering model is unmodified (test-CBY runs it on a testnet schedule). No runner-economics redesign, no fee-model amendment. |
| **CIP-13** | **Dormant** (no bond to delegate against); no change. |
| **CIP-21 / CIP-22** | **Out of scope / not implemented.** Application-layer, no dependency beyond CIP-20. CUSD never enters a pool or auction because no such flow is allowlisted by its hook — a mechanical consequence, not a change to those CIPs. |

## 12. Companion deliverables (informative, not normative)

- **Harness demos.** A small set of harness prototypes/mock-ups (finance/trading harness, API-key manager across providers, GPU-procurement router, search/discovery entry point) illustrating actor-as-harness and agent↔harness interaction (MCP or CLI). Separate document.
- **Proof-of-traceability.** A companion mechanism (initial PR authored separately) strengthening runner result provenance; referenced by §8, specified in its own PR.

## 13. Open questions / launch dependencies

1. **BankActor release readiness** — verify bootstrap bank initialization, configured operator/fiat signers and the separately gated consent/mint contracts in the CIP-28 interface appendix before a CUSD launch. `0x16` allocation and handler existence are already evidenced, not outstanding choices between reserved-band and interception mechanisms.
2. **Airdrop metric, size & emission-model ratification** — the reproducible testnet-performance metric (sybil-/wash-resistant per §4.3), snapshot rule, and the WP §8.3 bucket and amount funding the mainnet airdrop (§4.3), fixed before mainnet genesis. Because this sets the §8.3 Community/Airdrops **eligibility basis** to metrics rather than the row's illustrative "2 drops (TGE + 6 months)", it MUST be ratified through the mainnet-genesis distribution-parameter / governance process (§11 Whitepaper row) — supply and bucket size are unchanged, but the basis is a governance decision, not an automatic "unchanged".
3. **test-CBY grant sizing** — the per-account auto-grant that covers ordinary usage without letting free gas become a spam vector (§5); calibrate against early traffic.
4. **Admission tuning** — initial `min_funded_balance`, `max_admissions_per_principal_per_block`, and whether to enable `admission_charge` (§7).
5. **CUSD provider payout rails** — operator fiat payout vs. bridged-USDC settlement for third-party providers; out of scope here, needs its own spec before third-party providers onboard.
6. **Gateway key custody** — HSM / rotation-stability policy for the issuance-principal derivation key and Compliance-Gateway signing key (§7, §10).
7. **Regulatory characterization** — counsel sign-off on the non-transferable, non-redeemable CUSD posture (§10) as a launch gate.
8. **CUSD hook cross-actor read** — the exact mechanism the transfer hook uses to resolve `card → owner` from BankActor state for the `card ↔ owner` flow (§6.2), confirmed to fit the CIP-20 50,000-Cycle / 50,000-Cell hook budget; if it cannot, the `card ↔ owner` flow is dropped (CUSD stays card-resident).
