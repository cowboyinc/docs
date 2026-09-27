---
title: "BankActor Interface Reference"
description: "Retained BankActor data, instructions and gas-policy contracts; separate from actor purchase allowances."
---

# BankActor Interface Reference

This is the normative interface appendix to [CIP-28 §3](../cips/cip-28-cowboy-agent-banking#3-existing-bankactor-interfaces), not a bank-product roadmap or approval to launch CIP-36. The source baseline is [node devnet `44e1eb129`](https://github.com/cowboyinc/node/tree/44e1eb129/execution/src/bank). Required gas-safety behavior and implementation gaps remain explicit in the parent CIP's §3.4. The appendix defines the retained interfaces; CIP-36 defines the separate CUSD economic, custody and payout proposal. Neither document delegates these interface definitions back to the other.

This reference lives outside `docs/cips/` so CIP-number tools resolve the unique CIP-28 parent. It MUST NOT be indexed as a second CIP-28 proposal.

## 1. Citation and authority boundary

Older node comments use the pre-rewrite CIP-28 numbering. Interpret their BankActor references using this appendix, not the new purchase-allowance sections:

| Older citation | Retained definition |
| --- | --- |
| §2, §2.1, §2.2 | Data, storage and bank authority in §2 below |
| §3.1–§3.4 | Instruction families and authorization in §3 below |
| §3.6 | Event contract in §3.6 below |
| §4.1–§4.4, §4.6 | Gas reserve/settle, timers, edge cases and receipt handling in §4 below |
| §4.5 | [BankErr rejection mapping in §4.5](#45-error-codes) below |
| §5.1–§5.4 | Caps, filters, lifecycle and policy lock in §5 below |
| §6 / compliance perimeter | Authority boundary in this section; CUSD product policy is CIP-36 |
| §7.3 / activation | Initialization and dormant behavior in §6 below |

Historical references to CIP-28 r1.2 identify the address correction to `0x16`; r1.3 identifies the fixed-BE fiat-voucher preimage; r1.4 identifies agent-bound consent and the default-card owner checks. Those labels identify amendments, not extra protocols to implement. Their current contracts are defined here and in the parent §3.2–§3.3.

BankActor is `0x16`; PaymentGate is `0x12`. A bank's `operator` authorizes its freeze/pause, signer administration, and consent vouchers. Its separate optional `fiat_mint_signer` authorizes fiat mint vouchers. An owner controls its card's funding, withdrawal and policy subject to lifecycle/lock rules; the bound agent does not inherit owner authority. The compliance perimeter is the bank operator and its off-chain fiat process: freeze/pause cannot seize ownership or confer arbitrary token-mint authority over other actors. CIP-36 does not define the bank operator key; it consumes this definition.

## 2. Data, storage and codec

BankActor stores bank entries, card entries, dense owner/agent indices, default-card pointers, issuance nonces, voucher replay markers and provider-settlement markers under its own actor state. Balances remain in the native/CIP-20 ledgers. `bank_id = 1` is the bootstrap bank. Issuance nonce scope is `(bank_id, owner, agent)`; defaults are keyed by agent. Fiat replay uses `voucher_id`; provider settlement uses `settlement_id`; consent replay uses `(bank_id, owner_or_agent, nonce)`.

Retained typed records, in codec field order:

```text
BankEntry {
  bank_id: u32, name: Vec<u8>, operator: Address,
  fiat_mint_signer: Option<Address>, status: BankStatus, registered_at: u64
}
CardEntry {
  card_address: Address, bank_id: u32, owner: Address, agent: Address,
  issue_nonce: u64, issuance_principal: bytes32,
  created_at: u64, last_renewed_at: u64, expires_at: Option<u64>,
  status: CardStatus, gas_payment_token: PayCurrency,
  policy: CardPolicy, window: SpendWindow
}
CardPolicy {
  per_hour_cap: Option<u128>, per_day_cap: Option<u128>,
  per_month_cap: Option<u128>, allowed_receivers: Vec<Address>,
  allowed_syscall_kinds: Vec<SyscallKind>, locked_after_transfer: bool
}
SpendWindow {
  hour_period_id: u64, hour_spent: u128,
  day_period_id: u64, day_spent: u128,
  month_period_id: u64, month_spent: u128
}
```

`Address` is 20 bytes; a CIP-20 token ID is **32 bytes**, not an address. `PayCurrency` is `Native` (tag 0) or `Token(bytes32)` (tag 1 plus the token ID). Instruction fields represented as a raw `token_id: bytes32` use all-zero for native CBY; a voucher signing preimage uses that raw token ID, not the PayCurrency enum tag. Fiat mint accepts the whitelisted stablecoin path, not arbitrary native minting.

BankStatus tags are Active=0, Paused=1. CardStatus tags are Active=0, Frozen=1, Closed=2, Expired=3. SyscallKind tags are Send=0, DeployActor=1, PublishLibrary=2, Token=3, CrossChain=4, Session=5, Cbss=6, Custom=7 followed by a BE u16. Custom(0) is not Send.

Integers use fixed-width big-endian encoding; bool and enum tags are validating bytes. Option uses 0 for absent or 1 followed by its value; other tags reject. Variable vectors use u16-BE counts. CardPolicy serializes the fields above in order; its enclosing CardEntry uses a u16 byte-length prefix for the encoded policy. SpendWindow is exactly 72 bytes. Truncation, invalid tags, oversized vectors and trailing bytes reject. Bank names contain 1–32 ASCII bytes; policy encoding is at most 8,192 bytes.

| Bound | Value / meaning |
| --- | --- |
| `MAX_CARDS_PER_OWNER_BANK` | 64 live cards per owner and bank |
| `MAX_CARDS_PER_AGENT_BANK` | 64 live cards per agent and bank |
| `MAX_ALLOWED_RECEIVERS` | 64 addresses |
| `MAX_ALLOWED_SYSCALL_KINDS` | 16 kinds |
| `MAX_CARD_POLICY_BYTES` | 8,192 bytes |
| Freeze/pause reason | At most 256 bytes |
| Fiat reference | At most 64 bytes |

Close removes the card from dense indices and releases its live-card slots; ownership transfer updates owner indices and enforces the new owner's cap. These bounds are the ones referenced by CIP-43's access-cost formulas.

Card address derivation and immutable issuance-principal semantics are in parent §3.1–§3.2. Renewal or ownership transfer cannot rederive the address, change the bound agent or replace the principal.

## 3. Instruction contracts

Names below are the node/codec names used by CIP-43. Amounts are u128 and block heights u64; caller means authenticated transaction authority, never a caller-supplied address. Every operation must preserve ledger conservation and replay protection. Declaring a cost in CIP-43 does not make an unsupported instruction executable.

### 3.1 Owner and funding operations

| Instruction | Fields | Authorization and effect |
| --- | --- | --- |
| `BankIssueCard` (200; `IssueCard`) | bank_id, agent, gas_payment_token, initial_policy, expires_at | Caller becomes owner; require active bank, valid bounded policy and index capacity; increment issuance nonce, derive card, set zero issuance_principal, write indices. The source baseline also accepts whitelisted token gas currencies; that is existing CIP-36 code, not approval of token gas for the new purchase scope. |
| `BankDeposit` (201) | card_address, token, amount | Positive amount from authenticated caller; active bank and non-Closed card; move caller balance to card. Frozen/Expired cards may receive funds. |
| `BankWithdraw` (202) | card_address, token, amount, to | Owner only, positive amount, non-Frozen card, sufficient balance and valid distinct destination; move card funds. Allowed while bank is paused and for residual balances after close. |
| `BankCloseCard` (203) | card_address, refund_to | Owner only; reject Frozen/Closed and current default card; refund native balance, close card, clear window and indices. Token residuals remain recoverable with Withdraw; close is not an unbounded all-token sweep. |
| `BankSetPolicy` (205) | card_address, new_policy | Owner only; Active/Frozen and not a locked self-owned card; decode bounded policy, replace policy without resetting spend history, emit canonical policy hash. |
| `BankTransferOwnership` (215) | card_address, new_owner, set_locked | Current owner; valid new owner, non-Closed card and new-owner capacity; update owner/indices/hook pair, preserve agent/principal/address. `set_locked` can set the lock, not clear it. |

`RenewCard(card_address, new_expires_at)` remains a specified but **unimplemented** interface: owner only, Active/Expired, future height if present; return to Active and update expiry/last_renewed_at without changing principal. No opcode or deployed renewal emitter is claimed.

### 3.2 Default and consent

`BankSetDefaultCard` (204; agent, optional card_address) applies the complete authentication, nonzero-principal first-set and current-owner replacement rules in [parent §3.2](../cips/cip-28-cowboy-agent-banking#32-default-card-consent). Clearing requires agent authority or the current default's owner.

`BankIssueCardV2` (212) adds `voucher, signature` to IssueCard's fields. Require nonzero principal, matching bank and owner/sender, unexpired and unused consent nonce, and signature recovery to `BankEntry.operator`. The signed preimage includes the **instruction's agent**. Its exact preimage/wire split is in [CIP-36 §7](../cips/cip-36-phased-launch-cusd#7-spam-control--funded-account-admission); operator authority and card/default semantics are defined here, not delegated to CIP-36. A valid voucher does not grant spending authority to its relayer.

### 3.3 Operator, fiat and provider operations

| Instruction | Fields | Authorization and effect |
| --- | --- | --- |
| `BankFreeze` (206) | card_address, reason | Owning bank operator, Active card; change to Frozen, emit reason. |
| `BankUnfreeze` (207) | card_address | Owning bank operator, Frozen card; change to Expired if due, otherwise Active. |
| `BankPauseBank` (208) | bank_id, reason | Bank operator; pause issuance, deposits and gas charging; preserve owner withdrawal rights. |
| `BankUnpauseBank` (209) | bank_id | Bank operator; restore Active bank. |
| `BankMintFromFiatVoucher` (210) | voucher, signature | Any relay, signature from bank.fiat_mint_signer; active bank, non-Closed card, positive whitelisted stablecoin amount, unexpired unused voucher; consume replay ID and mint via CIP-20 authority. Exact voucher digest is in parent §3.3. Native/unsanctioned-token minting rejects. |
| `BankSettleProvider` (211) | provider, amount, settlement_id | Bootstrap bank 1's operator; positive amount and solvent provider CUSD balance; consume settlement ID and burn through CIP-20 as BankActor. A previously consumed ID is an Ok no-op, without another burn/event. Bank pause does not block settlement. |

Mint and burn are supply-conserving ledger operations. Provider off-chain payout and on-chain burn share an idempotency key, not an atomic transaction across the fiat boundary. The off-chain fiat process, reserve audits and economic release approval belong to CIP-36; these on-chain interfaces and their authority remain defined here. The source baseline writes replay markers before downstream mint/burn calls after validation; do not infer that a failed call automatically rolls back without checking its execution/savepoint boundary.

### 3.4 Signer administration and registration

| Instruction | Fields | Authorization and effect |
| --- | --- | --- |
| `SetBankOperator` (213) | bank_id, new_operator | Current bank operator; validate new operator and rotate the stored operator key. No arbitrary caller or implicit Foundation authority. |
| `SetBankFiatMintSigner` (214) | `bank_id, new_signer: Option<Address>` | Bank operator; set/rotate signer or remove it, disabling fiat mint. |
| `SubmitRegisterBankProposal` | description_hash: bytes32, voting_blocks: u64, tier: u8, name, operator, fiat_mint_signer | Rejected by the inspected dispatch. Runtime multi-bank registration is not enabled; the CIP-43 price entry reserves no authorization. A future registration design requires an explicit separate decision. |

### 3.6 Events

The existing BankActor topics are CardIssued, CardDeposited, CardWithdrawn, CardClosed, CardDefaultSet, CardPolicySet, CardOwnerTransferred, CardFrozen, CardUnfrozen, BankPaused, BankUnpaused, BankFiatSignerSet, BankOperatorSet, FiatMinted, ProviderSettled and GasCharged. They are attributed to `0x16`; payload bytes enter consensus log/receipt commitments. The specified CardRenewed has no runtime emitter, and bootstrap initialization is not a transaction emitting BankRegistered.

COW-1139 corrects CardIssued to `card(20) ‖ bank_id(4 BE) ‖ owner(20) ‖ agent(20) ‖ optional_expiry`: tag 0 alone (65 bytes total), or tag 1 plus u64-BE expiry (73 bytes). None, Some(0) and Some(u64::MAX) are distinct. The issuance nonce is not an event field. This is the coordinated re-genesis contract in [spec #447](https://github.com/cowboyinc/cowboy/pull/447), [node #1707](https://github.com/cowboyinc/node/pull/1707) and [SDK #163](https://github.com/cowboyinc/python-sdk/pull/163), not a claim that the older source baseline already emits it. Preserve that correction when landing this rewrite. Decoders must surface errors on malformed recognized BankActor payloads; batches must fail or explicitly report incompleteness and the offending event, never silently omit it from successful results. Filter foreign emitters first; topic-only input requires trusted BankActor provenance. Opaque forwarding preserves bytes. Both issuance paths must match persisted expiry, and acceptance covers the production encoder/commitment, receipt attribution, indexer persistence/export and SDK single/batch decoding of real receipts.

**CardIssued commitment vectors.** The topic is the exact 10 ASCII bytes
`CardIssued` (hex `43617264497373756564`); the emitter is the 20-byte BankActor
address `0000000000000000000000000000000000000016`. For a list of events,
`logs_root = Keccak-256(u32BE(count) ‖ concat(emitter(20) ‖ u32BE(topic_byte_length)
‖ topic_bytes ‖ u32BE(payload_byte_length) ‖ payload_bytes))` in event order.
Use Keccak-256, not SHA3-256; no ABI padding, terminators or hex-text hashing.

The following normative examples each contain exactly one event. Common inputs
are `card = 0x11` repeated 20 bytes, `bank_id = 16909060` (hex `01020304`),
`owner = 0x22` repeated 20 bytes and `agent = 0x33` repeated 20 bytes. Each complete
payload is that 64-byte prefix followed by the listed expiry suffix. Lengths
below count bytes; roots are hex-encoded 32-byte hashes.

| expires_at | Expiry suffix (hex) | Payload length | logs_root (hex) |
| --- | --- | --- | --- |
| None | `00` | 65 | `e9e090f709fd5a0e536ad7464e3b6ff34a712a2519ebd29dad0f6f68e53c7716` |
| 72623859790382856 | `010102030405060708` | 73 | `3d6a4ffab37beba91107e22c9e4f9b3e4885293a4f9cf69d67882b815a348faa` |
| 0 | `010000000000000000` | 73 | `8c7a8280f46fce86c2a0b984df02b30a9c78486fe0f9a6ac9ced1b2fe7f61a18` |
| 18446744073709551615 | `01ffffffffffffffff` | 73 | `e7f847d94e6e5438eb208a52aea64cdf6346a9bd0e9c3c3231c637ddfa77d6a7` |

The production encoder MUST reproduce these payloads, and single-event
`compute_logs_root` MUST reproduce these roots. Updating an implementation fixture
does not amend this contract; a changed contract requires an explicit specification
change and coordinated consensus rollout.


GasCharged binds the actual transaction digest: `card(20) ‖ tx_digest(32) ‖ receiver(20) ‖ SyscallKind ‖ reserve_amount(16 BE) ‖ actual_amount(16 BE)`. Policy events contain a hash of canonical policy bytes, not the complete policy; reconstructing full policy contents requires those bytes separately. Other existing payload encodings are unchanged by this rewrite.

## 4. Gas reserve, settlement and failure contracts

Older §4.2 references mean the reserve/settle contract in [parent §3.4](../cips/cip-28-cowboy-agent-banking#34-gas-safety-contract): authenticate the selected card and principal, perform all eligibility checks before an atomic reserve, account for actual charges, and refund only the matching reservation/period once. That section also records the current default-card fallback/caller-policy gaps rather than presenting required behavior as already implemented.

Older §4.3 references mean timers fund at firing, not scheduling. Gas funding follows the eligibility, fallback and rejection rules in §4.5 below. The two native gas fee dimensions and separately attributed co-signer costs retain their parent/CIP-3 definitions. Token gas conversion and its CBY liquidity leg are a separate CIP-36 mechanism; an event or card record does not prove that liquidity exists.

Older §4.4 conversion references use integer floor arithmetic: under peg ratio `num/den`, CBY→token is `floor(cby × num / den)` and token→CBY is `floor(token × den / num)`, with nonzero validated ratio and checked/bounded amounts. Such conversion does not replace the required native liquidity for burn/tip settlement. Older §4.6 receipt references retain the actual transaction digest in GasCharged (§3.6).

### 4.5 Error codes

This retains the prior CIP-28 §4.5 rejection contract, including node comments
that map owner-default consent rejection to `BankPolicyDenied`. These are
**normative BankErr-to-receipt names**, not a claim that the inspected node exposes
this enum or every distinct receipt code: its handlers also return generic
`ExecutionError` values, and completing the mapping remains implementation work.

| BankErr condition | Receipt rejection |
| --- | --- |
| `BankErr::CardNotFound` | `BankCardNotFound` |
| `BankErr::BankPaused` | `BankPaused` |
| `BankErr::CardFrozen` | `BankCardFrozen` |
| `BankErr::CardExpired` | `BankCardExpired` |
| `BankErr::CardClosed` | `BankCardClosed` |
| `BankErr::ReceiverNotInWhitelist` | `BankPolicyDenied` |
| `BankErr::SyscallNotAllowed` | `BankPolicyDenied` |
| `BankErr::CapExceeded { tier }` | `BankCapExceeded` |
| `BankErr::InsufficientCardBalance` | `OutOfFunds` |
| `BankErr::DefaultRequiresConsent` | `BankPolicyDenied` |
| `BankErr::NotCardAgent` | `BankNotCardAgent` |
| `BankErr::TokenGasUnsupported` | `BankTokenGasUnsupported` |
| `BankErr::InvalidFeePayerOverride` | `BankInvalidOverride` |

Explicit card overrides reject on ineligibility. Implicit-default resolution
instead MUST fall through to actor-pays for the closed pre-write eligibility
outcomes: paused bank, non-Active card, reached expiry (even if still Active),
policy or caller denial, cap exceeded, insufficient balance, or agent mismatch after
transfer. Storage faults propagate; partial reservations never fall through.
The parent §3.4 records the remaining COW-3107 implementation gap. The retained
`TokenGasUnsupported` name does not erase the source baseline's separate CIP-36
token-gas path or approve token funding for new actor purchase allowances.

## 5. Card policy and lifecycle

### 5.1 Cap and window semantics

A missing cap is unlimited; Some(0) denies spending in that tier. All three caps must pass. Per-card spending is denominated in its gas currency's atomic units. The existing block periods are `BLOCKS_PER_HOUR = 3_600`, `BLOCKS_PER_DAY = 86_400`, `BLOCKS_PER_MONTH = 2_592_000`; period ID is integer `height / period_blocks`. These are fixed block buckets, not sliding windows or calendar months. A new bucket resets that tier's old spent counter before checking/adding spend. Policy changes do not reset existing history. The new actor purchase allowance must separately specify pending-liability and late-refund behavior; it cannot inherit a gas counter and claim purchase safety.

### 5.2 Receiver and syscall filters

`allowed_receivers` and `allowed_syscall_kinds` are independent: both must admit a gas charge. An empty list is unrestricted for that dimension; a nonempty list requires membership. System destinations use their system actor address. Native transfer/actor execution maps to Send; token operations to Token; deploy to DeployActor; library operations to PublishLibrary; session and secrets operations to Session/Cbss; cross-chain to CrossChain; other opcodes use Custom(u16). These are gas receiver/syscall filters, not the new allowance's merchant policy. They do not justify adding a seller allowlist to the purchase product.

### 5.3 Status transitions

Only the owning bank operator freezes an Active card and unfreezes a Frozen one. A due Frozen card remains Frozen until operator Unfreeze; time alone cannot dissolve a compliance hold. Unfreeze selects Active or Expired according to expiry. Lazy Active→Expired is a separate dormant gate at the inspected revision (§6). Gas requires an Active, non-due card regardless of whether the stored status has been lazily updated. Closed/Expired cards cannot become defaults; Closed cards cannot receive BankDeposit or policy/owner changes. Frozen cards cannot withdraw or close. Owner withdrawal remains available on Expired/Closed cards for residual funds. Renewal is specified in §3.1 but has no current handler.

### 5.4 Self-ownership lock

When `locked_after_transfer` is true and owner equals agent, SetPolicy rejects. Ownership transfer does not itself change the address, agent or issuance principal. This lock constrains existing gas-card policy; it neither supplies an actor signing key nor replaces owner revocation of a purchase allowance.

## 6. Initialization and release status

The retained registry's bootstrap bank is `bank_id = 1`, with operator and optional fiat signer supplied by the BankActor initialization configuration. At the named source revision `BANK_ACTIVATION_HEIGHT = 10`; historical prose saying it is `u64::MAX` or that `0x16` is unallocated is stale. This does not prove deployment, a genesis-seeded bank on every network, or full gas integration.

`CARD_EXPIRY_TRANSITION_ACTIVATION_HEIGHT`, `FIAT_MINT_CHAIN_ID_BINDING_ACTIVATION_HEIGHT` and `ISSUANCE_VOUCHER_CHAIN_ID_BINDING_ACTIVATION_HEIGHT` are separate dormant gates. The latter two select chain-bound signing preimages and require matching signer changes; deleting product roadmap text does not change them. Their launch disposition remains separate work. The CardIssued correction has no new gate and ships with matching consumers at fresh genesis. No historical-state migration or mixed-format decoder is required merely to preserve disposable devnet.
