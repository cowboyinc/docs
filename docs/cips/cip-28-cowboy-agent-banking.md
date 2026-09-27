# CIP-28: Actor Spending Limits

- **Status:** Draft; the native actor-purchase contract below has companion implementation PRs awaiting merge and end-to-end acceptance. Existing BankActor gas-safety gaps remain separate.
- **Updated:** 2026-09-23
- **Related:** [CIP-18](./cip-18-payments), [CIP-3](./cip-3-fee-model), [CIP-36](./cip-36-phased-launch-cusd), [CIP-40](./cip-40-eip712-signing-walletconnect), [CIP-41](./cip-41-multi-signer-authorization)

The terms MUST, MUST NOT, SHOULD and MAY define conformance requirements. Only disposable devnet exists; this release requires no legacy allowance decoder, historical-state migration or parallel old/new purchase flow. This document does not authorize a reset or claim deployment.

## 0. Background

An owner should be able to fund a Cowboy actor, permit it to buy services, and bound what it can spend. The actor needs purchasing authority with enforceable limits; it does not need a bank, card issuer, or unrestricted owner key.

[CIP-18](./cip-18-payments) defines how a service accepts payment. This CIP governs an actor making purchases. It does not control the wallets of external customers paying that actor.

## 1. Goal and scope

An owner funds an allowance for one actor. The actor can buy services from any supported seller using **native CBY**, subject to a per-purchase maximum and **fixed one-hour, 24-hour and 30-day caps**. Authoritative checks and reservation happen before the actor commits the purchase, including when calls run concurrently on different replicas.

The owner can inspect funds and spending, change limits, and revoke new purchasing authority. There are no automatic top-ups. Gas is separately funded and authorized: exhausting a purchase allowance must not itself disable the actor's ordinary operation.

Included are allowance authorization, funding, concurrent reservation, settlement/recovery, revocation, and a management/status API. Excluded are credit cards, lending, external USDC purchases, CBY-to-USDC conversion, new bank operators or card products, fiat issuance, token gas conversion, and a general policy language. A new dashboard is not required.

Existing BankActor gas and consent contracts remain distinct interfaces (§3). Their presence does not make the purchasing flow complete, nor require the new allowance to adopt their card or operator model.

## 2. Allowance contract

### 2.1 Authority and signing

An allowance MUST bind one authenticated owner and actor to one restricted secp256k1 purchasing authority, native CBY funds, a per-purchase maximum, three period caps and enabled state. The owner signs management transactions; the purchasing authority signs only the purchase domain below. It MUST NOT receive the owner's unrestricted key or authority to withdraw, edit limits, transfer generic funds or replace its own allowance. The configured authority MUST be nonzero and distinct from the owner; the actor MUST exist.

The selected native authorization is `PurchaseAuthorization { terms, signature }`, where `terms` contains exactly:

```text
owner(address20), actor(address20), seller(address20), recipient(address20),
amount(u64), request_hash(bytes32), expires_at_block(u64), purchase_id(bytes32)
```

Its signing digest is `keccak256(ASCII("CowboyActorPurchase") || 0x01 || u64_be(chain_id) || encode(terms))`. `encode` is the fixed-width commonware field order above, with big-endian integers, raw address/hash bytes and no JSON/ABI padding. The signature is recoverable secp256k1, 65 bytes. `purchase_id` and `request_hash` MUST be nonzero; purchase IDs are caller-generated random 32-byte values, scoped to owner/actor and never deliberately reused for another operation. A repeat with identical terms resolves the existing record; different terms reject.

New commitments MUST verify the live configured authority, enabled flag, block expiry, seller's native policy/treasury and charge entitlement, minimum net price, funds and every cap. Each reservation snapshots its authority and exact terms. Later revocation, key rotation or policy tightening MUST block new commitments as applicable, but MUST NOT silently change a committed recipient, amount or authority snapshot. Settlement still requires a valid original signature and unexpired terms. Seller policy updates do not redirect an existing reservation. A quote names the committed recipient, so the seller accepts that collection obligation at reservation.

There is no owner-configured seller allowlist. Any seller supported by native CBY integration is eligible; external-USDC-only acceptance is insufficient. Generic owner actions remain outside delegated purchasing authority. Revoked signing keys SHOULD NOT be reused: an uncommitted signature remains usable only if its key is currently authorized and its signed block expiry has not passed; key rotation is not retroactive invalidation of already committed purchases.

### 2.2 Funding and custody

The selected design is a narrowly scoped funded allowance under PaymentGate `0x12`. A funding operation debits the owner and increases the allowance's `available` liability. It does not leave the same funds spendable in the owner's or actor's ordinary balance. This boundary prevents generic transfers and gas use from bypassing the purchase cap. It reuses native ledger accounting without adding banks, cards, a generalized custody framework or unrestricted actor signing keys.

The committed state is keyed by `ASCII("pga:") || owner || actor`. It contains owner, actor, authority, limits, enabled, available, pending, settled, three bucket IDs and three spent counters. Purchase records use `ASCII("pgp:") || owner || actor || purchase_id` and contain signed terms, committing authority, original bucket IDs and phase (`Reserved`, `Settled`, `Cancelled`, `Refunded`). `settled` is cumulative service debit net of full refunds, not withdrawable cash. All arithmetic MUST be checked, and available + pending + settled MUST remain within u64 to keep later returns representable.

Pending funds MUST NOT be withdrawn, reused for gas or spent by another path. Funding and policy changes MUST NOT erase spending history, pending commitments or purchase records. v1 has no automatic top-up and no destructive on-chain purchase-history collection: cleanup needs a separately proven replay-safe contract before implementation. Capacity exhaustion MUST reject new purchases safely; operators must monitor storage headroom. This is an initial bounded-by-host-limit implementation, not a claim of unlimited lifetime throughput.

### 2.3 Native operations and visibility

The common protocol instruction is `PurchaseAllowance`, opcode **225** in companion protocol #150 (224 belongs to resource policy). Its action tags are Configure=0, Fund=1, Withdraw=2, Reserve=3, Settle=4, Cancel=5, Refund=6. The [pinned protocol codec](https://github.com/cowboyinc/cowboy-protocol/blob/3529c47ce50181d6f23c9bf2c9398febb8891958/crates/cowboy-protocol-codec/src/purchase.rs) fixes each action's bytes; these are not BankActor lifecycle/funding/admin instructions (200–215); opcode 216 separately encodes `SubmitRegisterBankProposal`, whose runtime bank-registration path remains rejected.

| Action | Authority and effect |
| --- | --- |
| `Configure(actor, authority, limits, enabled)` | Authenticated owner; creates/updates its allowance without resetting funds/history; disabled blocks new reservations |
| `Fund(owner, actor, amount)` | Owner only; debits owner and credits available |
| `Withdraw(owner, actor, amount)` | Owner only; available only, returns funds to owner |
| `Reserve(authorization)` | Any submitter carrying a valid purchasing signature; checks current limits and moves available to pending atomically |
| `Settle(authorization)` | Valid original authorization and existing reservation; moves pending to seller/protocol credits and marks settled atomically; identical settled retry is a no-op |
| `Cancel(owner, actor, purchase_id)` | Permissionless only after signed expiry and while Reserved; returns pending to available exactly once |
| `Refund(owner, actor, purchase_id)` | Original recipient only, while Settled; recipient funds the full signed total and returns it to available exactly once |

A full allowance refund includes the original protocol fee; the recipient supplies that difference from its balance. It does not claw back Treasury funds. Partial refunds are outside v1. Availability of this primitive does not prove an automatic service-refund policy or grant Gateway refund authority.

All amounts and caps use u64 native units, **1 CBY = 10^9 units**. SDK/API reads MUST expose available, pending, net settled total, current bucket IDs, spent and headroom for each cap, limits, enabled/authority and purchase phase/terms/result reference. Current buckets/headroom MUST be derived against authoritative chain time, even when no write has yet rolled stored counters. Read APIs cannot reserve funds. SDK/API management operations map to the action table; concrete HTTP route names are implementation interfaces, not additional authorization paths.

## 3. Existing BankActor interfaces

BankActor remains allocated at **`0x16`**, with its existing instruction identifiers and public CIP slug. This section and the normative [BankActor interface appendix](../references/bankactor-interface) retain the contracts referenced by other CIPs. The appendix supplies data types, all 17 CIP-43 instruction entries, bounds, lifecycle rules, and a map for older node section citations. It does not add banks, fiat issuance, or gas sponsorship to the actor-purchase release, and does not authorize deleting handlers or changing wire encodings.

### 3.1 Card and gas identity

A BankActor card is a funded account bound to one agent. Owner and agent are separate roles. Card balances live in the native/CIP-20 ledgers; BankActor holds lifecycle, owner, agent, policy and spend-window metadata. Card identity is derived as:

```text
keccak256("CowboyBankCard\x01" || bank_id_be4 || owner_20 ||
          agent_20 || issue_nonce_be8)[12..32]
```

Here `\x01` denotes the single domain byte `0x01`, not four printable characters. Ownership transfer changes the owner, not the address or bound agent. The issuance principal is immutable. Owner-authorized lifecycle and policy operations remain separate from operator freeze/pause and signer administration. Freeze does not transfer ownership or confer seizure authority. When `locked_after_transfer` is true and owner equals agent, every `SetPolicy` call MUST reject, including a proposed tightening. The appendix is the single retained BankActor interface authority.

The outer transaction's `fee_payer_override: Option<Address>` remains owned by CIP-40 and mirrored by CIP-33: commonware tag `0x00`, or `0x01` plus 20 address bytes, included in transaction identity and signing. EIP-712 projects `{has_fee_payer_override, fee_payer_override}`; `None` and `Some(ZERO)` are distinct signed values. The same-named timer field is a separate contract.

CIP-3 and CIP-41 still govern gas fees and co-signer intrinsic charges; co-signer intrinsic charges cannot be shifted onto a sponsored card. None of these gas interfaces grants merchant-purchase authority.

### 3.2 Default-card consent

`SetDefaultCard` authenticates its caller and validates that a selected active card belongs to the specified agent. The agent-self path requires actual authenticated agent authority; a keyless actor does not obtain it by supplying its address.

For an owner setting a default:

- With no current default, the selected card must have nonzero `issuance_principal` from consented issuance.
- With an existing default, the caller must own both the selected card and the current default. It cannot replace another guardian's default.
- Clearing a default requires the agent or the current card's owner.

Permissionless `IssueCard` records a zero principal. Consented `BankIssueCardV2` uses the operator-signed issuance voucher defined by CIP-36. Its verification must bind the issuing instruction's actual `agent`, the owner/sender, bank, principal, nonce, and expiry. Ownership transfer and renewal cannot change the principal. Producer/verifier golden vectors must pin the consent preimage.

Consent to sponsor an actor is not blanket consent to sponsor every external caller. The gas path must check its applicable caller authorization before spending a card's funds (§3.4). This caller-sponsorship boundary is separate from the absence of a merchant allowlist for purchases.

### 3.3 Fiat voucher boundary

CIP-36 references BankActor's fiat voucher and issuance-principal interfaces. They remain separate from this purchasing release. The retained fiat voucher contract is (PayCurrency is Native or Token(bytes32), defined with its encoding in the [interface appendix §2](../references/bankactor-interface#2-data-storage-and-codec)):

```text
FiatMintVoucher {
  bank_id: u32, card_address: Address, token: PayCurrency, amount: u128,
  voucher_id: bytes32, expires_at_block: u64, fiat_reference: bytes[<=64]
}

signing_digest = keccak256(
  "CowboyBankFiatMint\x01" || bank_id_be4 || card_address_20 ||
  token_id_32 || amount_be16 || voucher_id_32 || expires_at_block_be8 ||
  fiat_reference_length_be2 || fiat_reference
)
```

`\x01` is a single domain byte. Native currency uses a zero token ID; a token uses its CIP-20 ID. The secp256k1 signature must recover `bank.fiat_mint_signer`; enforce bank/card eligibility, expiry and single-use `voucher_id` atomically with the authorized credit. The voucher may be relayed by anyone; the relay caller does not become mint authority. This preimage omits chain ID, so a fiat-mint signing key must not be reused across chains.

The consent voucher is a different domain and is verified against `BankEntry.operator`, whose authority is defined in the [interface appendix §1](../references/bankactor-interface#1-citation-and-authority-boundary). CIP-36 §7 specifies the agent-bound consent preimage. A fiat receipt is not actor consent.

**CIP-36 disposition:** CUSD economic/custody policy, provider fiat payouts, token-gas paymaster behavior and the associated ownership hooks belong to CIP-36's separate proposal. The existing on-chain mint, burn-settlement, bank-key and card interfaces remain defined in the [interface appendix §3](../references/bankactor-interface#3-instruction-contracts); CIP-36 consumes those definitions rather than redefining their authority. They are not prerequisites for CBY actor purchases or Cloud external-USDC collection. Before any of those mechanisms is released, CIP-36 must be reconciled with the Cloud/Chain funds flow and independently validated. This CIP neither approves that release nor claims its existing code has been removed.

### 3.4 Gas safety contract

Gas-card enforcement is separate from purchase caps. Retain these requirements for callers relying on BankActor:

1. An explicit transaction override must name a valid card and authenticate the principal as its bound agent. An arbitrary EOA override cannot debit that EOA without authorization. CIP-40 determines which transaction classes may carry an override; actor-origin and timer-materialized outer transactions do not acquire a new override permission here.
2. An implicit default is looked up for the **callee actor**, not the external signer. Actor-origin use must authenticate that actor. External callers need explicit sponsorship permission; an unset caller policy does not authorize public use. Closed, bounded caller list, and explicit public sponsorship must remain distinguishable.
3. For an implicit default, deterministic pre-write ineligibility—paused bank, inactive/expired card, policy or caller denial, exhausted cap, insufficient balance, or agent mismatch—falls through to the ordinary CIP-3 funding path, subject to that path's own authorization and funds. It must not make a keyless actor permanently uncallable. Explicit overrides reject on failure. Storage faults propagate; they never silently become fallback.
4. Preflight performs no partial writes. Reserve only the sponsor-eligible worst-case native gas charge with checked arithmetic: `(cycles_limit - sig_cycles) × max_fee_per_cycle + cells_limit × max_fee_per_cell`, where `sig_cycles` is the co-signer intrinsic charge assigned to its own payer by CIP-3/CIP-41. Validate the subtraction; do not reserve those excluded cycles against card funds or caps. Commit the debit and cap reservation together only after every check succeeds.
5. Settlement uses frozen fee inputs and actual usage, follows CIP-3's native burn/tip split, and refunds unused reservation at most once. Refunds affect only the period buckets to which the reservation belongs; a delayed refund must not subtract unrelated new-period spending.
6. Timers reserve and settle at firing, not scheduling. Sponsorship withdrawal may stop funding a timer; it cannot silently grant another payer's authority.
7. The native gas contract requires native CBY funding. Token balances alone cannot satisfy native burn/tip obligations; any token-gas paymaster requires CIP-36's separate liquidity and settlement design.

These are required behaviors, **not a claim that current gas integration satisfies them**. The inspected implementation still rejects calls on default-card cap/policy failures and lacks the specified caller-policy dimension. The remaining fallback/caller-policy work is tracked by [COW-3107](https://linear.app/cowboy-labs/issue/COW-3107). That gap must remain visible to gas-card users; completing an unrelated purchase allowance does not fix it.

## 4. Purchase flow

1. The owner funds and enables an allowance for the actor.
2. The actor selects a supported CBY service and obtains a quote binding recipient, amount, operation and expiry.
3. The authoritative spending mechanism authenticates actor authority, checks revocation, per-purchase maximum, funds and all period caps, then atomically reserves the amount against the purchase identity.
4. Submit only the payment authorized by that reservation. Confirm settlement before the paid operation executes, as required by CIP-18.
5. Convert the reservation to settled spending without counting both. Associate the seller's execution/result or recovery obligation with the purchase.
6. Reconcile interrupted work. A proven terminal unpaid outcome releases the reservation; a possibly successful settlement keeps its liability pending.

The purchase maximum bounds the full authorized service debit, including fees charged as part of that purchase. Transaction gas has its own authorization and funding; it cannot be silently added to the service debit or taken from an exhausted purchasing allowance. Amount increases or a different recipient MUST require a new authorization; a mutable quote must not increase an existing commitment.

The mechanism MUST enforce the same limits across every replica and concurrent call. Process-local counters, a committed-balance precheck, and separate read-then-write operations are insufficient.

## 5. Limit and revocation semantics

Before a new commitment, require enough uncommitted funds and headroom under the per-purchase maximum and **each** period cap. Each outstanding commitment consumes headroom until its disposition is authoritative. Lowering a cap below existing liabilities blocks additional commitments; it does not erase or cancel them.

Revocation takes effect at the authoritative ordering point and blocks later commitments. A commitment ordered before revocation can still settle within its authorized terms. Settlement callbacks, retries, and refunds must refer to that commitment and cannot create fresh purchasing authority.

Period changes cannot drop pending liabilities. Attribute reservations, settlements and releases to explicit period buckets, and retain the information needed to reconcile late outcomes. Do not subtract an old reservation's refund from new-period spending. A payment that may still settle MUST NOT regain spendable balance or cap headroom merely because its HTTP request timed out or a period ended.

The selected allowance clock is the consensus execution timestamp in milliseconds. Windows are fixed at Unix timestamp zero: `bucket = floor(timestamp_ms / period_ms)` for periods `[3_600_000, 86_400_000, 2_592_000_000]`. The codec field `month` means **30 fixed days**, never a calendar month or rolling window. Backward bucket movement MUST reject. BankActor's block-height gas counters remain a different contract.

For each period, admission MUST require `spent_in_current_bucket + all_pending + new_amount <= cap`. A reservation stores its original bucket IDs. On rollover only that period's settled-spent counter resets; all pending funds still reduce current headroom. Settlement removes pending and adds spent only for periods whose current bucket still equals the reservation's original bucket. Cancel returns pending funds after expiry. Refund reduces spent only in matching original buckets; it MUST NOT subtract unrelated new-period spending. Period assignment is by commitment, not settlement date; a late settlement may therefore release current headroom while remaining attributable to an older period.

Example for the one-hour cap 100 (other caps assumed nonbinding): reserve 60 just before an hour boundary. Immediately after rollover, spent=0 and pending=60, so only 40 remains available under the hourly cap. If that original purchase settles after rollover, pending becomes 0 and current-hour spent remains 0; it belonged to the prior hour. A new current-hour purchase of 70 may now settle, setting current-hour spent=70. Refunding the old 60 restores funded balance but leaves current-hour spent=70. The API MUST display this fixed-window behavior rather than call it a rolling limit.

A paid service failure does not automatically reverse spending. Reconcile any refund with the original purchase, returned funds, and period accounting exactly once. Refund eligibility follows CIP-18's paid-failure policy; a client error or an unknown result is not sufficient proof to release funds.

## 6. Cloud and Chain

Both environments MUST enforce bounded actor authority. Initial actor purchases use native CBY; an external customer's USDC payment under CIP-18 is a separate inbound flow.

Cloud's proposed **$0.15/CBY** price, purchased-credit restrictions, earned-revenue accounting and Stripe owner payouts are commercial rules in CIP-18. They do not define Chain's CBY market price or restrict owner-held Chain assets.

Future Chain USDC and bridge transfers need a separate supported asset path. A bridge is not a spending-limit mechanism. Actor-delegated credentials cannot use it to escape limits, while an owner's authorized withdrawal remains distinct from actor purchasing authority. Credit cards are entirely outside this CIP.

## 7. Implementation status

The earlier inspection at node `44e1eb129` documented the absence of this flow. On 2026-09-23 the companion work is:

| Component | Reviewed implementation reference | Release status |
| --- | --- | --- |
| Purchase codec, signature domain, fixed windows | [protocol #150, `3529c47c`](https://github.com/cowboyinc/cowboy-protocol/pull/150) | Implemented in open PR; not a merge/deployment claim |
| Custody, reserve/settle/cancel/refund and transaction rollback | [node #1715, `40f05626`](https://github.com/cowboyinc/node/pull/1715) | Implemented in open PR; live actor-to-service acceptance remains required |
| Allowance credential and confirmed service dispatch | [Gateway #106](https://github.com/cowboyinc/gateway/pull/106), built on [#104](https://github.com/cowboyinc/gateway/pull/104) | Companion implementation; review/merge and coordinated acceptance remain separate |
| SDK construction and management | [Python SDK #164](https://github.com/cowboyinc/python-sdk/pull/164) | Companion implementation; durable protocol pin and release checks remain separate |
| BankActor caller/default-card gas safety | §3.4 / COW-3107 | Separate existing gap; purchase completion does not close it |

Code tests, code presence, merge, activation and live-network acceptance are separate evidence. No row claims deployment or end-to-end release completion. The registered purchasing authority can buy services within the configured cap; compromise can exhaust that cap, so the owner must fund/limit it accordingly.

## 8. Release decisions and proof

| Work | Decision or evidence required |
| --- | --- |
| Actor authority and custody | Validate the selected funded PaymentGate allowance and restricted purchasing signature in §2. Prove that its credential cannot spend around the allowance, alter policy, or access the owner's other funds. |
| Reservation accounting | Validate the records/actions in §2, transaction rollback, withdrawal restrictions and CIP-18 restart recovery. |
| Period accounting | Validate the fixed-window algorithm and cross-boundary example in §5, policy tightening and late settlement/refunds. |
| Management API | Expose funding, limits, revocation, uncommitted withdrawals and accurate status through SDK/API calls. |
| Payment integration | Exercise the real actor → authorization → CBY settlement → service/result path with CIP-18 confirmation and paid-failure handling. |
| BankActor callers | Inventory gas, SDK and CIP-36 dependencies before retiring any interface; track gas safety gaps separately from purchase completion. |

Required proof covers the purchase maximum and each period cap, concurrent requests from different replicas, replay, restart, revocation races, policy tightening, period rollover with pending payments, late settlement, duplicate refunds, withdrawal races and attempted generic-transfer bypass. Test that an exhausted purchase allowance leaves independently funded actor execution usable.

Stop when one owner-funded actor can buy a supported CBY service with enforced limits and recover correctly from the bounded failure cases. Banks, card products, additional assets, a new dashboard and a general policy engine are not acceptance criteria.
