---
title: "CIP-34: Cross-Chain Intent Settlement"
description: Intent-based settlement on Cowboy — a Verifier system actor, runner-solvers, and CBSS-sealed bids — with pluggable cross-chain backends (native CIP-25, ERC-7683/OIF, NEAR Intents)
icon: right-left
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-06-17
  **Requires:** CIP-2 (Off-chain Compute / Runners), CIP-20 (Fungible Tokens), CIP-24 (CBSS Secrets Manager — v1 SEALED mode uses the §3.4.6 Time-Lock Release amendment, now merged; see §Sealed-Bid), CIP-25 (Cross-Chain Architecture)
</Note>

> **Revision history**
>
> - **r1 (2026-06-17)** — Initial draft. Defines the intent-settlement primitive (Verifier system actor + runner-solvers + CBSS sealing) and pluggable cross-chain backends (native CIP-25, ERC-7683/OIF, NEAR Intents) as CIP-25 L3 applications.
> - **r2 (2026-06-29)** — Audit fixups (Marshal review of #195): pin the account-path CBY deposit to a direct native-CBY **account** debit (the tx model has no attached-value field); state the v1 SEALED-mode **CIP-24 dependency** precisely (a caller-chosen auction identity + height-triggered release require a CIP-24 amendment, feasible on its `tlock` construction); specify **checked-`i128`** conservation arithmetic (CIP-20 does not bound supply below `2^127`); correct the **opcode-allocation** references (the Technical WP has a §9.1 address table but **no** single §9.2 opcode table); specify the **sealed-solver dispute** mapping; fix the `EventListener` citation (WP §16.3) and note that `Transaction` / `Instruction` / `SystemInstruction` now live in `cowboy-protocol-codec`.
> - **r6 (2026-06-29)** — Governance bundle (post-merge): WP §9.1 **allocates `0x14` to `INTENT_SETTLEMENT`** (this change amends the table) and reassigns the deferred `EventListener` to `0x15`; **reserve opcodes `146`–`151`** (next free above codec max `145`) for the six `Intent*` instructions; **fill the Parameters table with concrete defaults** (`MAX_SETTLE_INTENTS=64`, `MAX_DIFFS_PER_INTENT=16`, `GC_GRACE=256`, `AUCTION_GRACE=50`, `RECLAIM_GRACE=256`, `WITHDRAW_TTL=86_400` blocks, `SETTLEMENT_DOMAIN_VERSION=1`; inbound caps per-corridor + mandatory kill switch). Resolves the standing `0x14` tie-break and the parameter/opcode under-specification (round-4 items).
> - **r5 (2026-06-29)** — Audit fixups round 4 (Marshal run 381): constrain a Withdraw intent to **exactly one outbound diff** (unambiguous escrow target); enumerate the signing-digest `method/kind` as a fixed **`IntentKind`** enum (`SETTLE` | `WITHDRAW`) so a signature for one kind cannot be replayed as another. (Round 4 otherwise found the spec **converged** — these are the two remaining LOW nitpicks.)
> - **r4 (2026-06-29)** — Audit fixups round 3 (Marshal run 380): scope `TokenDiff` "price improvement" to the solver (a non-solver signer is credited exactly their `MinReceive` floor; surplus is the solver's fee — resolves the floor-vs-surplus ambiguity); add a consolidated **Parameters** table + flag-day note and **disambiguate `GRACE`** into `GC_GRACE` / `AUCTION_GRACE` / `RECLAIM_GRACE`; pin `min_finality` (dest-chain blocks) + a `finality_level()` backend method + `begin()`/`prove_delivery` enforcement; fix `SETTLEMENT_DOMAIN_VERSION` (a stable `u16`, not the doc revision); define the `open_auction` **AuctionGrant** delegation; note CBY's `Ledger-Backing` analog + the **pegged-token mint_authority bootstrap**. (`credit_deposit`'s CIP-20 mint was verified to invoke no transfer hook — no reentrancy surface — so no change there.)
> - **r3 (2026-06-29)** — Audit fixups round 2 (Marshal run 379): back inbound `credit_deposit` of a CIP-20 token with a **checked CIP-20 mint** (Settlement = pegged-token `mint_authority`) + new **`Ledger-Backing`** invariant (no unbacked balance); **bound bundle work** (`MAX_SETTLE_INTENTS` / `MAX_DIFFS_PER_INTENT`) so one `settle` cannot monopolize the shared system lane; **deadline-bound** the consumed-`(signer,nonce)` set + broadcast records (GC to the active-deadline window) so consensus state stays finite; require **compute-then-commit buffering** in `settle` (the engine does not roll back partial handler writes); reserve `CBY_SENTINEL` outside the token_id space; clarify `max_value` denominates the single outbound token; state `request_withdraw`/`credit_deposit` reentrancy posture.
> - **r7 (2026-07-03)** — **As-built alignment (v1 SEALED implemented + merged).** SEALED mode is now built and merged (node devnet + cbss `main`), so §Sealed-Bid is rewritten to the implementation of record. The material corrections vs the r1–r6 design: **(1) reveal is a permissionless `RevealAuction` transaction (opcode `156`), NOT a CIP-5 timer callback** — CIP-5 timers only fire PVM actor handlers, and a native reveal must be crank-able by anyone after `reveal_height`; **(2)** the three sealed instructions get concrete opcodes — `OpenAuction=154`, `SubmitSealedBid=155`, `RevealAuction=156` (next free above the `146`–`153` block; `152`/`153` are CIP-24 §3.4.6 `RegisterTlockRelease`/`SubmitTlockRelease`, the tlock backend this consumes); **(3)** the bid ciphertext is a versioned `WrappedDek` (the CIP-24 §3.4.3 IBE envelope, tlock HKDF domain `cbss/tlock/v1`) with the on-chain-recomputed base-AAD contract `request_id ‖ u64_le(reveal_height)`; reveal does the threshold-IBE decrypt **natively on chain** (Lagrange-combine ≥ *t* committee partials → σ = MSK·I → AES-256-GCM); **(4)** the `AuctionGrant` originator is bound to the intent's true owner via the broadcast resting record (not a self-asserted field), and the grant digest is chain-bound to the node's own `chain_id`; **(5)** winner selection is **deterministic first-valid in bidder-list order** among bids that both decode to a conserving `SettlementBundle` **and** actually fill `request_id`, applied via a per-attempt-gas, compute-then-commit try-settle loop; **(6)** the liveness fallback **cancels** (`CancelledGraceExpired`) — no escrow is held in v1, so nothing is refunded; and a released-but-no-valid-bid reveal likewise **cancels** (`CancelledNoValidBid`, distinct: the key WAS released but no decrypted bid was a valid conserving fill) — neither "degrades to OPEN"; **(7)** concrete Parameters (`MAX_SEALED_BIDS_PER_AUCTION=256`, `MAX_AUCTION_REVEAL_HORIZON=1_000_000`, `MAX_SEALED_BID_BYTES=8192`, reveal gas) and error codes (`1746`–`1754`). Note: the **solver-mode dispute** (r2/§Solver Model) remains a design item — not yet implemented. Reveal was multi-round independently audited (0 code defects); the node↔cbss crypto envelope is cross-anchored by shared golden vectors verified on both mains. A multi-round review of this alignment then swept the whole document (and CIP-24's cross-references) for surviving old-design claims and fixed them: the "degrade to OPEN" fallback (risk table + CIP-24 §3.4.6/§9.6) → cancel-only; "refund escrow" → no escrow held in v1 (cancel moves no funds); the `open_auction` check list reordered to the code's actual precedence (error codes are consensus-visible); leftover "timer-driven reveal" / "best solver wins" in the `AuctionMode` comment, Worked Example B, and the Reference Implementation → permissionless `RevealAuction` / first-valid; the reveal-gas lane (system, not CIP-2); the solver-mode dispute framed honestly as unbuilt; and the present-tense "needs a CIP-24 amendment" → satisfied. A second hardening pass then caught the residuals the first sweep introduced/missed (the `AuctionMode.OPEN` "best-price wins" line; this bullet's own "refunds") and added the consensus MUSTs a spec-of-record needs: the **reshare epoch-pin** (reveal combines only same-`quorum_epoch` partials, else not-releasable — twin of CIP-24 §9a `MixedEpochReceipts`, now also stated in CIP-24 §3.4.6) and the **1-based committee-index Lagrange** convention.
> - **r8 (2026-07-03)** — **`0x14` committee bootstrap named.** §Sealed-Bid presumed `Account(0x14)`'s CBSS committee/`MPK` already existed but never said how it comes to be — and `0x14` is keyless, so it cannot self-issue the owner-keyed `RequestAccountDkg`. Adds the back-reference to **`GovRequestSystemDkg` (CIP-24 §3.5, opcode 158)**, the one governance-authorized (`0x09`) DKG-bootstrap path for a keyless system-actor scope, which MUST run once before the first `OpenAuction`. Documents the `force_rekey` hazard (a re-DKG installs a fresh `MPK` and strands in-flight sealed bids → their auctions cancel; routine refresh uses `RequestReshare`, which preserves `MPK`). Spec-only; the CIP-24 side (opcode 158, §3.5 + §9.6) is the primary home. Reference implementation: codec `cowboyinc/cowboy-protocol#19`, node `cowboyinc/node#909`.

## Abstract

CIP-34 defines **intent-based settlement** for Cowboy and its **pluggable cross-chain backends**.

An **account** — a user, agent, or solver holding a secp256k1 key — signs a declarative **intent** — "I give up exactly X of token A; I want at least Y of token B" — expressed as a signed **token diff**, signed with the account's own secp256k1 key. (PVM actors hold no account key; they participate directly via the in-actor translator, authorized by caller provenance — see §Intent Authorization.) Off-chain **solvers** (CIP-2 runners) compete to fill it. An on-chain **Settlement system actor** (the Verifier — a native module at a reserved address, like CBSS/RAS) holds deposited balances as an internal ledger and applies a matched set of intents **atomically**, only if the bundle conserves value (no token is created). Solver competition can be run as a **CBSS-sealed auction** (CIP-24 time-lock) to eliminate last-look and bid-sniping — a property NEAR's open solver market does not provide.

Cross-chain legs go through a uniform `ICrossChainSettlement` interface with **pluggable backends**, governance-selected per corridor: the native **CIP-25** bridge (first-party, trust-minimized), an **ERC-7683 / Open Intents Framework** solver-facing adapter (EVM corridors, Across-led), and **NEAR Intents** (1Click + Chain Signatures, for BTC / non-EVM reach). One honesty runs through the CIP: **no standard is a liquidity source.** External liquidity comes from solvers who front capital plus a settlement rail they trust — ERC-7683 standardizes only how a solver *evaluates* an order, and NEAR's reach is gated by their chain onboarding. The native settlement core depends on none of them.

This CIP specifies the on-chain settlement core in full and the external backends at the interface level; which backend is deployed per corridor is a governance decision per **CIP-25 §1.4 / WP-v2 §16.2**.

---

## Motivation

### Why intents, not an order book or an AMM

Cowboy already has continuous-clearing auctions for launches (CIP-22) and liquidity pools (CIP-21). Neither solves *general-purpose, cross-asset, cross-chain settlement* for the agent economy this protocol is built for:

- **AMMs** leak value to LPs and MEV, and price degrades with pool depth. Poor fit for large or long-tail pairs.
- **Order books** need continuous liquidity and on-chain matching cost.
- **Intents** let a third party (a solver) do the hard work — sourcing liquidity, routing, crossing chains — while the chain only checks that the *outcome* the signer asked for was delivered and that no value was conjured. This is the natural settlement model for **agent-to-agent commerce** (CIP-28 banking), **inference billing** (CIP-2 outputs priced in CIP-20 tokens), and **agent swarms** that must exchange value without a human in the loop — accounts authorizing by signature, in-actor logic by caller provenance.

### Why Cowboy is unusually well-suited

The intent-settlement pattern needs exactly the tiers Cowboy already ships:

| Intents role | Cowboy primitive | Notes |
|---|---|---|
| **Solver** (off-chain pricing, routing, signing, inventory) | **Runner (CIP-2)** | Already the off-chain decision + signing tier, with stake, VRF selection, and a dispute window. A solver is a runner with inventory. |
| **Verifier** (internal ledger, signed diffs, atomic conservation-checked settlement) | **System actor (native)** | A native module at a reserved address (like RAS/CBSS) — verifies the account's own secp256k1 signature directly, no PVM cycle ceiling on the hot path. |
| **Sealed order flow / bid privacy** | **CBSS (CIP-24)** | Threshold-IBE (`tlock`) encrypts bids until a reveal height — native sealed-bid auctions; the differentiator over NEAR's open market. v1 uses the CIP-24 §3.4.6 caller-chosen-identity, height-triggered release (now merged; §Sealed-Bid). |
| **Cross-chain transport** | **CIP-25** | Native L2 messaging + L3 asset bridge, *or* an external adapter/backend (ERC-7683/OIF or NEAR Intents) — governance-selected per corridor. |
| **Auction clock / reveal deadline** | **`reveal_height` + permissionless `RevealAuction`** | A fixed reveal height; once the CBSS committee releases the key, anyone may crank `RevealAuction` (no keeper, no timer — CIP-5 timers only fire PVM actor handlers). |

### Why external backends — and what they do *not* give you

The cross-chain problem splits into *transport* (prove a fact across chains), *settlement* (escrow + reimburse a filler), and *liquidity* (a counterparty willing to front capital). CIP-25 solves transport natively; the native settlement core (below) handles escrow/reimbursement. Neither produces **liquidity** — that comes only from solvers willing to inventory a corridor.

External backends are how Cowboy reaches *existing* solver liquidity instead of bootstrapping all of it:

- **NEAR Intents** — a production solver network with BTC / non-EVM reach (1Click API + Chain Signatures). Onboarding Cowboy as a supported chain is curated by NEAR.
- **ERC-7683 / OIF** — the EVM-world *order-evaluation* standard (Across-led, broad EVM ecosystem). It lets external solvers evaluate a Cowboy order without a bespoke integration — but it standardizes *only* the solver-facing resolution, **not** escrow, settlement, pricing, or liquidity (§Where liquidity comes from).

Both are reach plays layered on the native core; neither is required, and neither is free.

### What this CIP is NOT

- **Not an L0 / consensus-mechanism change.** It adds a *system actor* — a new WP §9.1 address plus new `SystemInstruction` opcodes (allocated in the enum + uniqueness test; there is no central WP §9.2 opcode table — see §Protocol Allocations) — but does not modify block production, finality, or the token model. Settled assets are existing CIP-20 tokens and CBY.
- **Not a mandate to use any one backend.** NEAR Intents, the **ERC-7683/OIF adapter** (an `IResolver` order surface over a CIP-25 reimbursement rail — 7683 alone is *not* a settlement backend), and the native CIP-25 bridge are interchangeable `ICrossChainSettlement` backends; selection is per-corridor governance (CIP-25 §1.4).
- **Not a re-specification of CIP-25.** Transport, anchoring, and the lock-mint/burn-release bridge mechanics live in CIP-25. This CIP consumes them.

---

## Specification

### Overview

```
                 signed intent (token diff)
  account/agent ───────────────────────────────►  ┌──────────────────────┐
        ▲                                          │  SETTLEMENT ACTOR    │
        │ credited balances / events               │  (native sys actor)  │
        │                                           │  - balances[acct,tok]│
        ▼                                           │  - verify sig+nonce  │
   ┌──────────────┐     bids / fills (signed diffs) │  - require Σdiff ≤ 0  │
   │ SOLVER       │ ◄────────────────────────────►  │  - apply atomically  │
   │ RUNNERS      │                                  └───────────┬──────────┘
   │ (CIP-2)      │                                              │ Withdraw intent
   └──────┬───────┘                                              ▼
          │ sealed bid (CIP-24 tlock)                 ┌─────────────────────────┐
          │                                            │ ICrossChainSettlement   │
          ▼                                            │  ├─ A native (CIP-25)   │
   ┌──────────────┐                                    │  ├─ B 7683/OIF adapter  │
   │ CBSS         │  reveal key @ height H              │  └─ C near (1Click/CSig)│
   │ COMMITTEE    │                                     └─────────────────────────┘
   └──────────────┘
```

The on-chain core (Settlement actor) is self-contained and usable for **purely domestic** settlement (agent-to-agent, inference billing) with no cross-chain dependency. The cross-chain backend is engaged only by `Withdraw`/`Deposit` intents.

### Core Data Structures

```python
@dataclass
class TokenDiff:
    """A signed balance delta. Negative = signer gives (debit) exactly |amount|;
    positive = signer must receive (credit) AT LEAST amount — a FLOOR enforced by
    Settlement-MinReceive, never less. The per-token surplus over all debits accrues to
    the bundle's SOLVER (Settlement-Conservation), not to individual signers: a non-solver
    signer is credited exactly their floor, and "price improvement" is realized only by the
    solver (or by a signer who is themselves the filling party)."""
    token_id: bytes32           # CIP-20 token id, or CBY_SENTINEL for native CBY (NOT a CIP-20 token).
                                # CBY_SENTINEL is a fixed reserved value outside the CIP-20 token_id space (§Custody).
    amount: i128                # signed token amount; CIP-20 amounts are u128 and CIP-20 does NOT cap
                                # supply below 2^127, so settle uses checked i128 (see Settlement-Conservation)

@dataclass
class Intent:
    signer: address             # 20-byte account OR actor address whose ledger balance moves
    nonce: u64                  # unique per signer (consumed-set, NOT monotonic — permits
                                # out-of-order/concurrent settlement); replay protection. The consumed
                                # set is GC-bounded: a pair need only be retained until `deadline`
                                # (after which the deadline check alone blocks replay) — §Intent Authorization.
    deadline: u64               # block height after which the intent is void
    diffs: list[TokenDiff]      # the signed outcome
    # optional cross-chain leg (present only for Withdraw-style intents)
    dest_chain: u64 | None      # external chain id (CIP-25 ChainId space)
    dest_recipient: bytes | None # recipient on dest_chain (chain-native encoding)
    # optional user trust constraints (Withdraw-style; signed into the digest — §Cross-Chain Trust Constraints)
    allowed_backends: list[u8] | None   # only these ICrossChainSettlement backends may fill
    max_value: u128 | None              # per-intent cap on the single cross-chain OUTBOUND token (Withdraw); effective = min(max_value, corridor cap)
    min_finality: u64 | None            # minimum destination-chain finality, in DEST-CHAIN BLOCKS; enforced at begin() vs the backend's finality_level (§Cross-Chain Trust Constraints)
    min_received: u128 | None           # signed delivery floor on dest_chain (enforced at prove_delivery)
    signature: bytes            # secp256k1 EthSignature over the typed digest (account intents);
                                # EMPTY for actor-origin intents — authorized by caller provenance,
                                # signer runtime-stamped = tx.from, matched by intent_hash (§Intent Authorization)

@dataclass
class SettlementBundle:
    """A solver-assembled set of intents that clears together."""
    intents: list[Intent]       # users' intents + solver's own filling intent(s)
    solver: address             # the runner submitting (must be CIP-2 registered for sealed mode)

@enum
class AuctionMode:
    OPEN     = 0   # solvers submit bundles directly; the first valid CONSERVING bundle to be included settles (no on-chain price ranking — solvers compete off-chain, the chain only conservation-checks)
    SEALED   = 1   # solvers submit CBSS-encrypted bids; revealed by a permissionless RevealAuction tx after reveal_height

@enum
class IntentKind:           # the 'method/kind' tag bound into the signing digest + intent_hash (replay separation)
    SETTLE   = 0            # domestic settlement intent (no dest_chain)
    WITHDRAW = 1            # cross-chain outbound intent (has dest_chain)
```

### Intent Authorization Model

The Settlement actor is a **system actor** — a native module at a reserved low-byte address, the pattern Cowboy already uses for RAS, CBSS, the runner registry, entitlements, basefee, etc. (`node/execution/src`).

**Account intents are signed by the account's own secp256k1 key** (actor intents are authorized by caller provenance instead — see "How callers invoke Settlement" below).

1. The user signs an EIP-712-style typed digest of the intent with the same secp256k1 key that signs transactions.
2. The Settlement system actor recovers `signer` from the `EthSignature` via native `recover_address`, checks it matches the intent's declared signer, then enforces nonce + deadline.

**Signing domain (replay boundary).** Because the account's own secp256k1 key signs intents directly, the typed digest MUST bind a domain so a signature cannot be replayed across chains, deployments, or methods: `{ chain/network id, INTENT_SETTLEMENT address, SETTLEMENT_DOMAIN_VERSION (a fixed u16 bumped ONLY on consensus-critical interface/invariant changes, never on doc revisions), method/kind (the `IntentKind` enum — `SETTLE` | `WITHDRAW` — so a signature for one kind cannot be replayed as another), signer, nonce, deadline, every (token_id, amount) diff, dest_chain, dest_recipient, and any trust constraints (allowed_backends / max_value / min_finality / min_received) }`. This borrows EIP-712's field-binding discipline but is **not** EIP-712 wire-format (the preimage is a flat concatenation `INTENT_SIGNING_DOMAIN ‖ u16_be(SETTLEMENT_DOMAIN_VERSION) ‖ INTENT_SETTLEMENT ‖ u64_be(chain_id) ‖ codec(intent_without_signature)` hashed with a single `keccak256` — no `0x1901` prefix, no `hashStruct`/`typeHash`; wallets sign the raw 32-byte digest). Omitting any field widens the replay surface — cross-chain especially, given the NEAR / Chain-Signatures legs.

This gives the defining intent-system property — *the user signs once off-chain, a solver relays and settles* — with the account's own key and **no account-abstraction ceremony**. It also runs the O(intents × tokens) `settle` loop natively in the system lane (`LANE_SYSTEM_CYCLES`, exactly 2× the block-cycle target — defined in `node/types/src/constants.rs` (`LANE_SYSTEM_CYCLES`), relation asserted in `basefee.rs` SPEC-MG-1) — because Settlement executes as an `Instruction::System` opcode, not PVM user-lane bytecode — removing the *per-instruction* PVM cycle ceiling a bytecode actor would hit. But `LANE_SYSTEM_CYCLES` is a **per-block budget shared by all system instructions** — once exhausted, further system txs in that block are rejected — so a bundle is **explicitly bounded** to keep one `settle` from monopolizing the lane and starving basefee/RAS/CBSS/runner-registry/timer ops: `settle` enforces `MAX_SETTLE_INTENTS` and `MAX_DIFFS_PER_INTENT` (a bounded `intents × tokens`), sized so a single bundle's worst-case cycles stay well below `LANE_SYSTEM_CYCLES`, and rejects an over-cap bundle before any work.

**How callers invoke Settlement.** Two paths:

- **Account (signature).** A user/agent/solver/relayer holding a secp256k1 key submits a top-level `Instruction::System` tx carrying any `Intent*` instruction; authorized by signature, relayable by a solver. This is the **only** path for `IntentSettle` — the heavy O(intents×tokens) loop runs top-level with explicit gas.
- **PVM actor (provenance).** An actor sends a message to `INTENT_SETTLEMENT`; v0 adds an explicit system-target branch to `actor_instruction.rs` that — for the **self-service** instructions `IntentBroadcast` / `IntentDeposit` / `IntentWithdraw` **only** — decodes/validates the payload and creates a deferred `Instruction::System(...)` with `sender = message.from` (as the `job_dispatcher` branch does, `actor_instruction.rs` ~`:1200-1210`). It does **not** enqueue a message to the system address, and **not** translate `IntentSettle` (next-block-deferred execution is capped at `DEFERRED_CYCLES_LIMIT = 100k` per `constants.rs`, too small for the settle loop) nor the cross-chain instructions.

**Provenance authorization (fail-closed).** For an actor-origin instruction the runtime **sets `signer = tx.from`** (it never trusts a payload signer) and requires all of: empty `signature`; `signer` is a live actor (`get_actor(signer).is_some()`); and the actor manifest grants an `econ.intent` entitlement with token/amount bounds — mirroring how system `Transfer` gates actor senders via `econ.transfer` (the `Transfer` arm's `require_manifest("econ.transfer")` gate in `system_instruction.rs`). Account-origin requires a **non-empty, valid** signature. The two modes are mutually exclusive — an empty-signature account intent is rejected, never silently treated as actor-mode.

**Resting intents + matching.** An actor cannot relay provenance, so it authorizes via `IntentBroadcast`, which records a resting intent bound to its **canonical `intent_hash`** over `{signer, nonce, deadline, diffs, dest_chain, dest_recipient, method}`. The record activates at the **broadcast-execution block** (next block, not PVM-emit time), and `deadline` is checked there and again at settle. A second active broadcast for the same `(signer, nonce)` is **rejected** (no silent replace). A solver later includes the intent in a top-level `IntentSettle` bundle; `settle()` accepts an unsigned/actor intent **only if** it matches an active record by exact `intent_hash`, consuming the record and `(signer, nonce)` atomically. An inline unsigned intent with no matching record is rejected — a solver cannot fabricate actor intents.

**State bounds (consumed set + records).** A non-monotonic consumed set and the resting broadcast records are kept **finite by `deadline`, not grown without bound**: because every intent carries a `deadline` that `settle`/`broadcast` re-check, a consumed `(signer, nonce)` pair or an unsettled record need only be retained until `deadline + GC_GRACE` — past that, the deadline check alone rejects any replay, so both are garbage-collected down to the **active-deadline window**. (A monotonic-nonce O(1) counter is unavailable by design, since out-of-order/concurrent settlement is a goal; this deadline-bounded GC is what keeps the consensus state of an unbounded-throughput settlement actor finite.)

Alternatives considered and rejected:

| Option | What | Why not |
|---|---|---|
| **(a) ed25519 intent keys** | A separate ed25519 "intent key" per account, verified via the existing `ed25519_verify` host call | Only needed to dodge a protocol change in a *PVM* actor. With a system actor verifying secp256k1 natively, it adds a second key and a registration ceremony for no benefit. |
| **(b) secp256k1 `ecrecover` host call** | Add a general `ecrecover` to the PVM so a *normal* actor could verify account-key signatures | Not required here — the system actor recovers natively. Still worth doing **independently** as a general ecosystem capability (any actor doing meta-tx / permit / signature-gated logic), so it is proposed separately, **not** as a CIP-34 dependency. |
| **(c) Sender-authorized intents** | Authorization = the tx sender; no off-chain signature | The signer must submit their own settle tx — **defeats solver-relay**. Usable only for self-service swaps. |

### Protocol Allocations

Cowboy's executor dispatches top-level transactions by **`Instruction` type**, not by target address: `Instruction::System(SystemInstruction)` routes to `execute_system_instruction`, `Instruction::Actor` to PVM execution, and `Instruction::Custom` is **unsupported** (`node/execution/src/execution/transaction.rs`). A native system actor is therefore exposed as **explicit `SystemInstruction` variants**, each an opcode with a match arm — exactly like `RunnerRegister`, `TokenTransfer`, `EntitlementGrant`. (Since COW-2360 / node#846 the canonical `Transaction` / `Instruction` / `SystemInstruction` types live in the `cowboy-protocol-codec` crate — `github.com/cowboyinc/cowboy-protocol` — re-exported by `node/types/src/execution.rs`; the file pointers below are those re-exports.) Settlement must touch the **WP §9.1 address table**, the **system-instruction opcode allocation**, and — for the actor-authorization path — the **entitlement registry**:

- **WP §9.1 (system-actor address space).** `INTENT_SETTLEMENT` is **allocated `0x14`** in the WP §9.1 table (this governance bundle amends it), per the §9.1.3 next-free rule (`0x10` `RECEIPT_REGISTRY`, `0x11` `VALIDATOR_SET` (CIP-11, code-deployed), `0x12` PaymentGate, `0x13` `CONTAINER_REGISTRY` (CIP-10, code-deployed) were already reserved; note BankActor/CIP-28 was formerly mis-penciled at `0x13` and must relocate — see WP §9.1 reconciliation). The deferred `EventListener` bridge oracle, formerly penciled for `0x14` in WP §16.3, is **reassigned to `0x15`**: §16.3 deferred its address to allocation time (`0x14` there was an *as-of-revision* snapshot, not a binding §9.1-table reservation), so CIP-34 is the first specification to **bind** this slot in the §9.1 table per the §9.1.3 rules, and the §16.3 snapshot moves to the next free `0x15`. The implementation MUST add the `INTENT_SETTLEMENT = 0x14` constant and extend the uniqueness/low-byte reservation test (note the non-dense reservations `EVENT_SUBSCRIPTION_SYSTEM_ACTOR` `0x1D` and `TradingPost` `0x1E` in `node/types/src/constants.rs`).
- **System-instruction opcodes.** Settlement introduces new `SystemInstruction` variants — `IntentDeposit`, `IntentWithdraw`, `IntentSettle`, `IntentBroadcast`, `IntentRequestWithdraw`, `IntentCreditDeposit` — each claiming the next free opcode and adding an arm in `execute_system_instruction`. **There is no single WP §9.2 opcode table to amend:** the Technical WP normatively fixes only the §9.1 *address* table; opcodes are allocated **in the `SystemInstruction` enum + the opcode-uniqueness test** (`cowboy-protocol-codec`) and documented per-subsystem in the relevant sub-whitepaper ranges (e.g. timers `48–50`, CBSS WP §3 opcodes `68–84`). The PR MUST therefore (i) claim the **reserved contiguous block `146`–`151`** — the next free opcodes above the current max `145` in `cowboy-protocol-codec`'s `SystemInstruction::sub_type()` (as of codec rev `c36a17b`): `IntentDeposit=146`, `IntentWithdraw=147`, `IntentSettle=148`, `IntentBroadcast=149`, `IntentRequestWithdraw=150`, `IntentCreditDeposit=151` — re-pinned at implementation time against the then-current max by (ii) extending the opcode-uniqueness test, and (iii) record the block where the existing subsystem opcodes are documented (no central normative opcode registry exists today). **As built** (codec rev `dadd9330`), the block extends contiguously through the SEALED-mode instructions: `RegisterTlockRelease=152` / `SubmitTlockRelease=153` (the CIP-24 §3.4.6 tlock backend this consumes), and Settlement's own `OpenAuction=154` / `SubmitSealedBid=155` / `RevealAuction=156`. `IntentRequestWithdraw`/`IntentCreditDeposit` (150/151) decode+dispatch but return `UnsupportedInstruction` pending the v2 cross-chain path. **Naming:** the `Intent*` prefix and `INTENT_SETTLEMENT` address deliberately avoid the existing, unrelated `UpdateSettlementConfig` / `SettlementConfig` (staking-reward settlement, in `cowboy-protocol-codec`'s `SystemInstruction`). Option (b)'s general `ecrecover` host call would be an *additional*, separate opcode — out of scope here.
- **Entitlement registry (`econ.intent`).** The actor-authorization path (§Intent Authorization) gates actor-origin intents on an `econ.intent` manifest entitlement that **does not yet exist** in `node/types/src/registry.rs`. The PR MUST add `econ.intent` to the entitlement registry and to manifest validation (`node/types/src/manifest_validate.rs`).

### Settlement Invariants

```
invariant Settlement-Conservation (no minting):
    for every applied SettlementBundle B and every token t,
        Σ over i in B.intents of diff(i, t)  ≤  0
    i.e. total credits never exceed total debits. The non-negative remainder
        surplus(t) = −Σ diff(i, t) ≥ 0
    is credited to the bundle's solver (the fee account), bringing the per-token
    net to exactly 0 — value is moved, never created.

invariant Settlement-Atomicity (internal-ledger scope):
    a SettlementBundle is applied in full or not at all, within a single
    Settlement-actor execution over its internal ledger. No partial
    application is observable. SCOPE: atomicity covers ledger diffs ONLY.
    Cross-actor calls and cross-chain legs (Withdraw/Deposit) are NOT
    atomic with settlement; they use escrow + timeout (see §Timeout).
    MECHANISM: the execution engine does NOT roll back partial writes when a
    handler errors mid-execution, so settle computes/validates the FULL
    write-set in memory and commits only after the whole bundle passes
    (compute-then-commit) — never write-as-you-go-then-revert, which would
    leave a partially-applied bundle.

invariant Settlement-MinReceive:
    for every intent i and every TokenDiff d in i with d.amount > 0,
        the net credit to i.signer for d.token_id  ≥  d.amount.
    (Signer never receives less than signed-for.)

invariant Settlement-ExactlyOnce:
    an (signer, nonce) pair is consumed on first inclusion in an applied
    bundle; any later bundle referencing it reverts.

invariant Ledger-Backing (no unbacked balance):
    for every CIP-20 token t,
        Σ over accounts a of balances[a][t]  ==  Settlement's token-core balance of t.
    Every inbound credit_deposit MINTS t (checked CIP-20 mint; Settlement is the
    pegged token's mint_authority) BEFORE crediting the ledger; every withdraw_local
    transfers out of that token-core balance. The internal ledger never credits more
    of t than the actor verifiably holds or minted — so withdraw is always funded.
    CBY (CBY_SENTINEL) is NOT a CIP-20 token: it is backed analogously by the actor's
    native CBY account balance, and its only inbound mint source is credit_deposit,
    bounded by the INBOUND inflation caps.
```

`Settlement-Conservation` is the heart of the model: the actor never needs to trust the solver, because a bundle that would create value simply fails the check. A non-solver signer therefore receives **exactly** their `Settlement-MinReceive` floor; the per-token surplus is the **solver's fee**, not a price improvement paid to ordinary signers (see `TokenDiff`).

**Arithmetic safety.** The per-token accumulator is **checked `i128`**. CIP-20 declares `max_supply: u256 | None` and does **not** bound a token's supply below `2^127`, so a `TokenDiff` may legitimately carry `|amount| ≥ 2^127`; any single diff or running per-token sum that would overflow signed 128-bit makes `Settlement-Conservation` unverifiable, and the bundle **reverts** (the arithmetic never wraps). Equivalently: the actor rejects any bundle it cannot conservation-check in checked `i128` rather than settling on a wrapped total. (If a token's supply is itself `≥ 2^127`, its diffs cannot be represented as a signed `i128` delta and such a token is not settleable until CIP-20 caps supply or the ledger widens — called out so the bound is enforced, not assumed.)

**Pegged-token setup (bootstrap).** Before the first inbound `credit_deposit` for a corridor asset, governance creates the pegged CIP-20 representation with `mint_authority = INTENT_SETTLEMENT` (`token_create` with `owner = governance`, `mint_authority = Settlement`), and records the token_id in the corridor metadata. A `credit_deposit` for a token whose `mint_authority` is not Settlement **reverts** (it cannot mint → the credit would be unbacked), so this setup is a hard precondition of `Ledger-Backing`, not an assumption.

### Settlement Actor Interface

The signatures below are the conceptual interface; on the wire each maps to a `SystemInstruction` variant (§Protocol Allocations) — `deposit → IntentDeposit`, `withdraw_local → IntentWithdraw`, `settle → IntentSettle`, `broadcast_intent → IntentBroadcast`, `request_withdraw → IntentRequestWithdraw`, `credit_deposit → IntentCreditDeposit`.

```python
class ISettlement:
    # ── Custody (internal ledger) ──────────────────────────────
    def deposit(self, token_id: bytes32, amount: u128) -> None:
        """Move `amount` of `token_id` from caller into the ledger; credit balances[caller][token_id].
        Two callers: an ACCOUNT via its deposit tx (secp256k1), or an ACTOR via the deferred translator
        (`caller = tx.from`, gated by the actor's `econ.intent` manifest grant). Token mechanics
        (see §Custody & Reentrancy):
          - CIP-20 token: caller MUST `token_approve(token_id, INTENT_SETTLEMENT, amount)` first; the
            native handler then pulls via token core with **spender = INTENT_SETTLEMENT** — NOT the
            public `TokenTransferFrom` (which uses spender = tx sender, so the allowance key would be
            caller→caller, not caller→Settlement). See §Custody & Reentrancy.
          - Native CBY (token_id == CBY_SENTINEL): CBY is the protocol-native currency, NOT a CIP-20
            token. The Cowboy tx model has NO attached-value field (Transaction/Instruction carry none;
            native CBY moves only via an explicit Transfer), so BOTH paths debit the caller's CBY
            *account* balance directly. ACCOUNT path: IntentDeposit(CBY_SENTINEL, amount) debits the
            signer's CBY account under the tx signature, exactly as Transfer debits tx.from. ACTOR path:
            debit the actor's CBY account under the `econ.intent` grant (a `send_message` carries
            value=0). Both credit balances[caller][CBY_SENTINEL]; withdraw is symmetric.
        No intent-key registration (§Intent Authorization)."""

    def withdraw_local(self, token_id: bytes32, amount: u128) -> None:
        """Debit caller's ledger balance and move the asset back out, same-chain. `caller` = an account
        (tx) or an actor (deferred translator, `econ.intent`-gated). CIP-20: `token_transfer` from the
        Settlement balance to caller; native CBY: native transfer / credit to the caller's CBY account.
        CEI + reentrancy guard."""

    def balance_of(self, account: address, token_id: bytes32) -> u128: ...

    # ── Settlement ─────────────────────────────────────────────
    def settle(self, bundle: SettlementBundle) -> None:
        """OPEN mode, top-level only (explicit gas; never actor-deferred). REJECT a bundle exceeding
        MAX_SETTLE_INTENTS / MAX_DIFFS_PER_INTENT before any work (bounded `intents × tokens`, so one
        settle cannot monopolize the shared system lane — §Intent Authorization). Authorize every intent —
        account intents by recovering `signer` from the secp256k1 EthSignature (native ecrecover);
        actor intents ONLY if they match an active provenance-checked broadcast record by exact
        `intent_hash` (reject any unsigned intent with no match — solvers cannot fabricate). Check
        nonce + deadline, enforce the invariants over a BUFFERED write-set (compute all diffs in memory;
        the engine does not roll back partial writes on a mid-loop error), then commit in full —
        consuming `(signer,nonce)` AND the broadcast record atomically, crediting surplus (−Σ per token)
        to the solver, emitting Settled. Reverts (commits nothing) on any failure."""

    def broadcast_intent(self, intent: Intent) -> None:
        """Publish an open intent on-chain (emits IntentBroadcast). For ACCOUNT intents this is
        optional discovery (the signature already authorizes; off-chain relay is the default).
        For ACTOR intents it is the authorization channel: via the actor translator the runtime sets
        signer = tx.from, requires empty signature + a live actor + an `econ.intent` manifest grant,
        and records a resting intent bound to its canonical `intent_hash`. Active at the (next)
        broadcast block; a second active broadcast for the same `(signer,nonce)` is rejected. Moves no funds."""

    # ── Cross-chain (engages ICrossChainSettlement) ────────────
    def request_withdraw(self, intent: Intent) -> bytes32:
        """A Withdraw intent (has dest_chain). The intent MUST carry EXACTLY ONE negative
        (outbound) diff — the token escrowed and sent cross-chain — so the escrow target is
        unambiguous; a Withdraw with zero or multiple outbound diffs is rejected. (The positive
        side is the off-ledger delivery floor `min_received` on dest_chain, NOT a ledger diff.)
        Escrow that outbound token in the ledger, emit WithdrawRequested(request_id, ...). A solver
        completes the external leg and proves delivery to release escrow. See §Cross-Chain."""

    def credit_deposit(self, proof: CrossChainProof) -> None:
        """Inbound. Backs the credit BEFORE it lands in the ledger — no unbacked balance (Ledger-Backing):
          - CIP-20 token: the credited token MUST be a CIP-25-pegged representation whose mint_authority
            IS the Settlement actor; credit_deposit performs a CHECKED CIP-20 mint into Settlement's
            token-core balance (enforcing total_supply + amount <= max_supply per CIP-20) and ONLY THEN
            credits balances[recipient][token_id] — so a later withdraw_local token_transfer is funded.
            It MUST NOT credit a token it cannot mint (that would be unbacked).
          - Native CBY: credits the recipient's CBY account (bounded protocol inflation) under the caps.
        Credits against an attestation INDEPENDENT of any
        filling solver (§prove_delivery rule), domain-separated and replay-bound as above. Consumes a
        unique external deposit id EXACTLY ONCE and requires proof of lock/burn/final settlement on the
        external side (else one external event mints twice). Inbound is the higher-risk direction — it
        MINTS Cowboy-side value off an external attestation — so the credit is bounded by
            min(proof amount, per-tx inbound cap, rolling-window budget, outstanding-exposure budget)
        on INBOUND governance params (per corridor/backend/asset), distinct from the outbound caps, with
        a pause/kill switch. A compromised attester is bounded by these — not by a per-tx cap alone."""
```

### Custody & Reentrancy

The v0 Settlement actor is a **system actor** (native module at a reserved address; §Intent Authorization). It custodies two asset classes in one internal ledger, keyed by `token_id`:

- **CIP-20 tokens** — the caller first `token_approve(token_id, INTENT_SETTLEMENT, amount)`. The native `IntentDeposit` handler then invokes **token core directly with spender = INTENT_SETTLEMENT** (allowance key `(user, INTENT_SETTLEMENT, token_id)`, `node/execution/src/.../token/core.rs`). It MUST NOT route through the public `SystemInstruction::TokenTransferFrom`, which has no spender field and uses `caller = tx.from` (`node/execution/src/.../system_instruction.rs`) — that would check a user→user allowance and fail. Withdraw is symmetric: the native `IntentWithdraw` handler invokes token core with **from = INTENT_SETTLEMENT, recipient = caller** — likewise NOT the public `SystemInstruction::TokenTransfer`, which debits `sender = tx.from` (`system_instruction.rs`, `token/core.rs`) and would debit the user instead of the Settlement balance. Prefer `increase_allowance`/`decrease_allowance` over bare `approve` (CIP-20 approve race). Amounts are **u128** (CIP-20 type).
- **Native CBY** — CBY is the protocol-native currency and is **NOT a CIP-20 token** (CIP-20 §"CBY Relationship"); it occupies a reserved `CBY_SENTINEL` token_id in the ledger. The Cowboy tx model has **no attached-value field** (`Transaction` / `Instruction` carry none; native CBY moves only via an explicit `Transfer`), so both deposit paths debit the caller's CBY **account** balance directly — there is no "attached value" to pin. **Account path:** `IntentDeposit(CBY_SENTINEL, amount)` debits `signer`'s CBY account under the tx signature, exactly as `SystemInstruction::Transfer` debits `tx.from`. **Actor path:** the deferred `IntentDeposit` debits the actor's CBY account under the `econ.intent` grant (a `send_message` carries `value = 0`, `actor_instruction.rs` ~`:1131-1145`). Both credit `balances[caller][CBY_SENTINEL]`; withdraw is symmetric (debit ledger, credit the CBY account). `CBY_SENTINEL` is a **fixed reserved `bytes32`** outside the CIP-20 token_id space (token_id = `keccak256(creator ‖ symbol ‖ nonce)`); `token_create` rejects it, so no CIP-20 token can collide with the native-CBY ledger key.

Two reentrancy boundaries:

- **CIP-20 transfer hooks.** CIP-20 tokens may carry transfer hooks. `deposit`/`withdraw_local` are the *only* paths that call CIP-20 transfer; they MUST follow checks-effects-interactions (mutate the ledger, then transfer) and hold a reentrancy guard, so a hostile token hook cannot re-enter `settle`/`withdraw_local` mid-update. The hot path — `settle` — moves value **only within the internal ledger** with no external token calls, so it has no hook-reentrancy surface. `request_withdraw` likewise makes **no external token call** — it escrows *already-deposited* internal-ledger balance (no hook surface); `credit_deposit`'s only external call is the CIP-20 mint into Settlement's own token-core balance (CEI + the reentrancy guard, exactly like `deposit`).
- **Surplus accounting.** The conserved surplus (−Σ diff per token, ≥ 0) is credited inside the same execution; nothing is transferred out during `settle`.

### Sealed-Bid Auctions (CBSS / CIP-24)

OPEN mode is the minimum viable settlement path. SEALED mode is the differentiated one and is built on CIP-24's threshold-IBE time-lock. **Auctions are always caller-driven, never protocol-driven** — the Settlement actor never auto-opens an auction; sealed mode is opt-in per `request_id`.

**CIP-24 dependency (v1).** CIP-24's IBE *construction* is `tlock` (drand), so a height-based time-lock is feasible — but its current *interface* binds each ciphertext to a **fixed per-version identity** `hash(SecretId ‖ version ‖ wrap_epoch)` and releases the key on a signed **`ReleaseRequest`** at job time. Sealed solving instead needs (a) a **caller-chosen auction identity** (`identity = request_id`) and (b) **automatic release at a height/round** (`reveal_height`) with no per-secret request — so that a non-revealing bidder cannot block clearing. v1 therefore **requires a CIP-24 amendment** exposing this "auction-identity, height-triggered" release profile (feasible on the existing `tlock` construction). **This dependency is now satisfied by CIP-24 §3.4.6 (Time-Lock Release mode)** — a domain-separated (`cbss/tlock/v1`), height-gated, permissionless release on the same CBSS committee: a bid encrypts to `identity = request_id` with `target_height = reveal_height`, and the committee posts the release at the height (`RegisterTlockRelease` / `SubmitTlockRelease`). v0 (OPEN) and Backend A carry no such dependency — only SEALED mode does.

Three native `SystemInstruction`s implement it — `OpenAuction` (opcode **154**), `SubmitSealedBid` (**155**), `RevealAuction` (**156**). Reveal is a **permissionless transaction, not a timer** (as originally sketched): CIP-5 timers only fire PVM *actor* handlers, and a native reveal must be crankable by anyone once the key exists, so any account may submit `RevealAuction(request_id)` after `reveal_height`.

```python
class ISealedAuction:
    # opcode 154
    def open_auction(self, reveal_height: u64, grant: AuctionGrant, grant_sig: EthSignature) -> None:
        """Caller-initiated (never protocol-driven). AuctionGrant = originator-signed
        {originator, coordinator, request_id, expires, nonce}; the digest is domain-separated
        (`cowboy/cip-34/auction-grant/v1`) and bound to the node's OWN chain_id (never tx.chain_id).
        Checks, in the code's exact order (the first failing check picks the error code, and error
        codes fold into receipt_root, so order is consensus-visible): (1) block <= grant.expires
        (else AuctionGrantExpired); (2) grant_sig recovers to grant.originator (else InvalidSignature);
        (3) tx.from == grant.coordinator (else Unauthorized); (4) reveal_height in
        (block, block + MAX_AUCTION_REVEAL_HORIZON] (else InvalidData); (5) the anti-grief binding —
        the true owner of request_id (the signer in the broadcast RESTING record for
        intent_hash == request_id) equals grant.originator (else Unauthorized / InvalidData if no
        resting intent), so a third party cannot open an auction on someone else's intent;
        (6) the (originator, nonce) grant nonce is unused (else AuctionGrantNonceUsed); (7) the auction
        is not already open (else AuctionAlreadyOpen). Then commit: consume the nonce ONCE and write
        the auction record {reveal_height, coordinator} keyed by request_id;
        emit cip34.AuctionOpened(request_id, reveal_height)."""

    # opcode 155
    def submit_sealed_bid(self, request_id: bytes32, ciphertext: bytes) -> None:
        """Requires: auction open, block < reveal_height, and the solver is a CIP-2 registered runner
        (sealed-mode bidding is permissioned so undecryptable/invalid bids WILL be slashable once the
        solver-mode dispute lands — see §Solver Model; v1 enforces only the registration gate, not the
        penalty). `ciphertext`
        is a WrappedDek (see below), ≤ MAX_SEALED_BID_BYTES. The solver is appended to a per-auction
        bidder list (≤ MAX_SEALED_BIDS_PER_AUCTION distinct solvers); a solver may overwrite its own bid.
        Emits cip34.SealedBidSubmitted(request_id, solver).

        Checks, in the code's exact order (first failing check picks the error code; codes fold
        into receipt_root, so order is consensus-visible):
          (1) an auction is open for request_id       else AuctionNotOpen        (E1749)
          (2) block_height < reveal_height             else AuctionClosed        (E1750)
          (3) bidder is a CIP-2 registered runner      else SolverNotRegistered  (E1751)
          (4) for a NEW bidder, the bidder list holds < MAX_SEALED_BIDS_PER_AUCTION distinct
              solvers                                  else AuctionBidCapReached (E1752)
        An existing bidder overwriting its own bid skips check (4)."""

    # opcode 156 — PERMISSIONLESS (any account may crank it)
    def reveal_auction(self, request_id: bytes32) -> None:
        """Native on-chain threshold-IBE reveal (no PVM decryptor, no timer). Gating:
          block < reveal_height                              -> AuctionNotYetRevealable (retry later)
          < t partials, block < reveal_height+AUCTION_GRACE  -> TlockNotReleased (retry later)
          < t partials, block >= reveal_height+AUCTION_GRACE -> CancelledGraceExpired (clears state; no funds move)
          partials' quorum_epoch != current committee epoch  -> TlockNotReleased (before grace) /
                                                                CancelledGraceExpired (after) — a committee
                                                                reshare after threshold is never mis-combined
        Otherwise: Lagrange-combine >= t committee partials for identity = request_id into
        sigma = MSK*I (see §Bid envelope), decrypt every bid, then pick the winner DETERMINISTICALLY
        as the first bid IN BIDDER-LIST ORDER that (a) decodes to a conserving SettlementBundle and
        (b) actually fills request_id (some intent's intent_hash == request_id). Apply it via the
        settle() path in a per-attempt-gas, compute-then-commit try-settle loop (a failed candidate
        leaves no state, so the next is tried). On success -> Settled{winner}, emit
        cip34.AuctionRevealed(request_id, winner); if no decrypted bid is valid -> CancelledNoValidBid,
        emit cip34.AuctionCancelled(request_id). Either way clear_auction deletes the record, bidder
        list, and all stored ciphertexts."""
```

**Bid envelope (WrappedDek).** A sealed bid is a versioned `WrappedDek` (v2) — the SAME envelope CIP-24 §3.4.3 uses for IBE, differing only in the HKDF domain (`cbss/tlock/v1`): `{version, ciphertext, nonce(12), ephemeral_u(96 = compressed G2 `U = r·G2`), aad}`. The AEAD `aad` MUST equal `base_aad ‖ compress(U)` where the **CIP-34 base-AAD is `request_id(32) ‖ u64_le(reveal_height)`** — reveal reconstructs this on chain and full-equality-checks it, binding each bid to its auction + height. The key is `K = HKDF-SHA256(ikm = compress(e(σ, U)) [288 B], salt = "cbss/tlock/v1", info = "cbss/tlock/v1" ‖ aad)`, and `e(σ, U) = e(MSK·I, r·G2) = e(I, MPK)^r` matches the encryptor. The tlock identity is `I = hash_to_G1(request_id ‖ u64_le(reveal_height), "cbss/tlock/v1")`; the release committee is the `INTENT_SETTLEMENT` (`0x14`) account's CBSS committee — bootstrapped once by governance via **`GovRequestSystemDkg` (CIP-24 §3.5, opcode 158)** before the first `OpenAuction`, since `0x14` is keyless and cannot self-issue `RequestAccountDkg` (CIP-24 §9.6). This envelope + decrypt is implemented identically in node (`execution/src/tlock_ibe.rs`) and cbss-crypto (`tlock_ibe_encrypt`/`_decrypt`), cross-anchored by shared golden vectors. Reveal combines the posted partials **only if their `quorum_epoch` equals the current committee epoch** (reshare epoch-pin): the Lagrange x-coordinates are committee positions in the epoch the partials were signed under, so a committee reshuffle after the partial set reached threshold makes the release not-combinable — reveal returns not-releasable (`TlockNotReleased`, → `CancelledGraceExpired` after grace) rather than silently producing the wrong σ. The interpolation is evaluated at `x = 0` with each partial's x-coordinate set to its **1-based index** in the committee ordering (its position in the release key's committee list, `+1`); index 0 and duplicate indices are rejected. This ordering is exactly cbss-crypto's, so node and CBSS agree byte-for-byte (pinned by the shared `combine_partials` golden vector).

> **Scope (normative): sealed bids keep their own base AAD — they are OUT of the CIP-24 account-secret AAD binding.** The sealed-bid base AAD is `request_id(32) ‖ u64_le(reveal_height)` under the time-lock domain `cbss/tlock/v1`, and it is deliberately **not** the account-secret §3.4.3 layout. It does **not** gain the `chain_id` prefix or the trailing `mpk_g2` that CIP-24's account-secret `base_aad` binds (`chain_id ‖ account ‖ key_hash ‖ version ‖ wrap_epoch ‖ mpk_g2`, 172 bytes; CIP-24 §3.4.3 / History). Base AADs in the CBSS IBE families are **per-family and do not inherit** — the account-secret chain/release-key binding applies only to account secrets, exactly as CIP-24 §3.4.6 states for time-lock release. A change to the account-secret AAD does **not** alter this envelope, its golden vectors, or the `request_id ‖ reveal_height` contract. The residual key-substitution concern for sealed bids (a `RpcCommitteeMpk` that serves a substituted committee `mpk'`) is **not** remedied by an AAD change here — `submit_sealed_bid` stores `ciphertext` verbatim with no envelope validation, and a submit-time rejection would in any case arrive after the substituting party already holds both the key and the bid. It is instead addressed by authenticating the committee `MPK` at the encryptor against the release key's `release_key_material_hash` (which the release-key RPC already serves), and by an independent committee-side reveal-height check on the tlock signer — both tracked as separate CBSS workstreams, not as changes to this base AAD.

Properties this buys, that an open market cannot:

- **No last-look.** A solver cannot observe competitors' bids and re-quote; all bids are committed before the key exists.
- **No bid-sniping.** The reveal height is fixed; there is no end-of-auction timing game (cf. CIP-22's anti-sniping rationale, achieved here cryptographically rather than by gradual release).
- **Optional private order flow.** The *intent* itself may be IBE-encrypted to the auction identity, so front-runners cannot see the order until reveal.

**Liveness fallback.** If the CIP-24 committee fails to release the key by `reveal_height + AUCTION_GRACE` (default 50 blocks), a permissionless `RevealAuction` **cancels the auction** (`CancelledGraceExpired`) and `clear_auction` deletes the record, bidder list, and all stored ciphertexts. (Earlier drafts spoke of "degrading to OPEN mode"; the implementation simply cancels — it does not re-open a plaintext round.) **No escrow is held at open/bid time in v1** — a bid stores only its ciphertext; balances move only inside `settle_bundle` at a successful reveal — so a cancelled auction moves no funds and traps nothing (nothing to refund).

**No PVM secret path.** PVM actors are **not** trusted decryptors or auction adjudicators in v0 or v1: sealing, key release, and bid evaluation are CBSS-committee + native only. An actor never receives a decryption key or adjudicates sealed bids.

### Solver Model (CIP-2 runners)

- Solvers are CIP-2 runners. Open-mode solving is permissionless (anyone may submit a valid `settle`); sealed-mode bidding requires CIP-2 registration so misbehavior (e.g., submitting undecryptable ciphertext, or a revealed bundle that fails verification) is slashable. **Dispute mapping.** These offences are **on-chain-detectable at `reveal`/settle** (the native handler sees an undecryptable ciphertext, or a revealed bundle that fails `settle` verification), so they are adjudicated by a **solver-mode dispute** that reuses CIP-2's stake + slashing economics but is **triggered by these settlement-time failures** within a dispute window — *not* the JobSpec commit-reveal job-dispute lifecycle, which solvers do not use (the solver loop is a distinct runner mode; see below). **Status:** the solver-mode dispute is **not yet implemented** — v1 SEALED shipped with the registration gate only (an unregistered solver is rejected), and the slashing adjudicator is deferred to a follow-on. Until it lands, an undecryptable/invalid sealed bid is simply skipped at reveal (it never wins), but its submitter is not penalized.
- **Mechanism: an external solver loop, not the Job Dispatcher.** A solver is an off-chain program that watches `IntentBroadcast` events and submits settlement transactions. It runs on CIP-2 runner infrastructure (for stake, identity, slashing) but is a **distinct, new runner mode** — *not* the current JobSpec/Job-Dispatcher request/response path (LLM/HTTP/MCP jobs with a submitter address, `node/runner/src/types.rs`). This CIP introduces the solver loop as that new mode.
- A solver's "fill" is just one or more `Intent`s it signs with its own secp256k1 account key, included in the bundle, whose diffs oppose the users'. The solver profits from the surplus (−Σ diff per token, ≥ 0) credited to its account.
- Solvers discover open intents via `IntentBroadcast` events (and an optional off-chain mempool service). LLM-assisted routing (the local-inference path) is an off-chain solver concern; the chain only sees the resulting signed bundle.

### Cross-Chain Backends

The Settlement actor calls a uniform `ICrossChainSettlement` interface for any leg that leaves Cowboy. Two concerns must stay separate:

- **Order surface** — how an external solver *sees and evaluates* a Cowboy intent.
- **Settlement rail** — how value crosses and how the filler is *reimbursed and trusts that reimbursement*.

A backend pairs the two. Three are defined; all are governance-selected per corridor (CIP-25 §1.4). None of them manufactures liquidity — see §Where liquidity comes from.

```python
class ICrossChainSettlement:
    def quote(self, src_token: bytes32, dst_chain: u64, dst_token: bytes,
              amount: u128, dst_recipient: bytes) -> Quote:
        """Returns (expected_out, fee, deposit_target, quote_deadline)."""

    def finality_level(self, dst_chain: u64) -> u64:
        """The backend's settlement finality for dst_chain, in DEST-CHAIN BLOCKS. begin() reverts if
        this is below the intent's min_finality (§Cross-Chain Trust Constraints)."""

    def begin(self, request_id: bytes32, quote: Quote) -> None:
        """Commit to fulfilling a Withdraw; the Settlement actor already holds escrow."""

    def prove_delivery(self, request_id: bytes32, proof: bytes) -> bool:
        """Releases the filler's escrow + fee ONLY against an attestation whose validity is INDEPENDENT
        of the filling solver: an objective L1 inclusion proof of the destination `Delivered` event
        where available; else a governance-selected attester set independent of the solver set/
        operators/affiliates, or a threshold oracle the solver set cannot quorum. Domain-separated and
        replay-bound to {request_id, backend_id, src/dst chain ids, token, amount, recipient, finality
        block/tx, attester set/version}; verified against the backend/proof policy persisted at begin().
        A solver MAY submit the proof but cannot self-attest."""
```

**Backend A — Native CIP-25 (first-party / bootstrap).** Order surface and settlement rail are both Cowboy's own: `begin` emits a CIP-25 L2 message, an L3 asset bridge (lock-mint or burn-release, CIP-25 §3.1) moves value, and `prove_delivery` is a CIP-25 L1 inclusion proof of the destination `Delivered` event. Trust = the CIP-25 backend for that pair (runner committee / ZK / optimistic); no third party beyond Cowboy's runners. Liquidity is your own first-party solver. **This is the bootstrap that is never blocked.**

**Backend B — ERC-7683 / OIF adapter (EVM corridors, Across-led).**

- *Order surface:* a solver-facing **`IResolver`** that surfaces a Cowboy intent as a 7683 **`ResolvedOrder`** (`steps` / `variables` / `payments` / `Assumption[]`). The current EIP-7683 is resolver-centric and standardizes *only* this solver-facing resolution — it "does not require protocols to use a common escrow, settlement contract, or fill function" (the old `open`/`fill` settler model is its deprecated "Previous Draft"). Because Cowboy is non-EVM execution, the resolver is an **EVM-RPC-shaped gateway** an external solver can `eth_call`; without it, EVM solver tooling cannot read a native Cowboy actor. `Assumption[]` carries the proof backend, finality/challenge window, TTL/reclaim, and value caps a filler must accept.
- *Settlement rail:* the native actor escrows and reimburses; a filler is repaid against a **CIP-25 settlement proof they have opted into**. **7683 does not make a filler trust Cowboy** — they must have onboarded Cowboy's CIP-25 backend and accepted its finality/challenge economics. Targeting Across specifically is an **Across-over-7683 adapter** (Across may expose its own surface), not generic-7683 compliance.

**Backend C — NEAR Intents (BTC / non-EVM reach).** The filling solver acts as a NEAR Intents / 1Click client:

```
1. Settlement actor escrows the user's outbound asset in its ledger (e.g., 100 CBY).
2. Solver runner calls 1Click `quote` for (src, dst_chain, dst_token, amount).
3. Solver fronts its OWN origin-chain inventory to the returned depositAddress. The
   user's escrow stays locked in the Settlement actor — it is the solver's
   reimbursement, NOT the funds sent to 1Click.
4. NEAR solver network fills the intent; funds land at dst_recipient on dst_chain.
5. Escrow releases ONLY against a delivery attestation whose validity is INDEPENDENT of the
   filling solver — an objective CIP-25 L1 inclusion proof of the destination `Delivered` event
   where Cowboy anchors that chain; else a governance-selected attester set independent of the
   solver set/operators, or a threshold oracle the solver set cannot quorum. The 1Click status is
   advisory — it may feed the oracle but is never the authoritative trigger; the solver may submit
   the proof but cannot self-attest. If no valid proof by WITHDRAW_TTL, the user reclaims the escrow.
```

Backend C is a **consciously trusted, capped** backend — **not trust-minimized settlement**, and its attestation is **NOT equivalent to a CIP-25 inclusion proof**. Where the destination chain has no objective proof path and the only attester is NEAR/1Click-correlated, it is allowed only under a **hard low corridor/backend cap + rate limit**, labeled *trusted reach*. Cowboy-account control uses Chain Signatures (next); onboarding Cowboy as a NEAR-supported chain is NEAR-gated (§Where liquidity comes from).

### Cross-Chain Trust Constraints (user-signed)

A Withdraw intent MAY narrow how it is filled, via fields **bound into the signed digest** (a relayer cannot strip them):

- `allowed_backends` — only these `ICrossChainSettlement` backends may fill (e.g. native CIP-25 only, no NEAR).
- `max_value` — per-intent cap **on the single cross-chain outbound token** of the Withdraw (not a multi-token aggregate — there is no cross-token denomination); effective cap = `min(max_value, governance corridor cap)`.
- `min_finality` — minimum destination-chain finality the user accepts, in **dest-chain blocks**.
- `min_received` — signed delivery floor on the destination leg, enforced at `prove_delivery`.

**Enforcement ordering.** `request_withdraw` validates and stores canonical `RequestConstraints` under `request_id`. `begin()` MUST select a backend satisfying **both** the stored user constraints **and** current governance corridor policy, then persist the chosen backend + proof policy; `prove_delivery` verifies against that stored choice. Governance may **tighten** after the request (lower a cap, pause a backend) but MUST NEVER loosen a user-signed constraint. `min_finality` is enforced at `begin()`: the actor reverts if the selected backend's `finality_level(dst_chain)` is below it, and `prove_delivery` re-checks the delivered event's finality depth against the persisted policy.

### Chain Signatures (secp256k1, domain 0)

Cowboy accounts are secp256k1/EVM-style (20-byte address; `EthSignature` recovered over a keccak256 digest). NEAR's Chain Signatures MPC signs **secp256k1 (domain 0)** and ed25519 (domain 1); secp256k1 domain 0 is the *same* path NEAR already uses to control Ethereum and Bitcoin accounts. So a NEAR identity can control a Cowboy account **exactly as it controls an Ethereum account** — the signing primitive is already in production, not new work.

The crypto is therefore not the hard part. **The hard part is transaction construction.** The MPC signs an arbitrary 32-byte digest; it does not know how to build a *valid Cowboy transaction* (the canonical fields, encoding, and keccak digest the node recovers against). Off-node hand-rolling of Cowboy's tx format has already caused signature-validity failures elsewhere in the stack. This CIP therefore **requires** that the Chain-Signatures relayer construct Cowboy transactions through the canonical node transaction builder — the exact encoding the node validates — never a reimplementation. A divergent encoder is a correctness bug, not a convenience.

What this unlocks, once the relayer exists:

- **Inbound settlement into Cowboy.** NEAR can settle an intent *into* a Cowboy account by having its MPC sign a Cowboy transaction that calls `credit_deposit` or transfers CBY — no wrapped representation required.
- **NEAR custody of native CBY** for listing native Cowboy assets on NEAR Intents.

This is the **Path D spike**: a secp256k1 Cowboy-tx-construction-and-relay spike, *not* an ed25519 compatibility spike.

### Where liquidity comes from

The load-bearing honesty of this CIP: **no standard or backend is a liquidity source.** Liquidity is solvers willing to front capital on a corridor, plus a settlement rail they trust to reimburse them.

- **ERC-7683 is a compatibility layer for solver *evaluation*, not liquidity and not a settlement guarantee.** Solver inventory stays fragmented per protocol — a solver that fills Across orders is not automatically able to fill UniswapX orders, even though both are 7683. Each network onboards Cowboy, inventories the corridor, and accepts a settlement path independently.
- **NEAR reach is gated by NEAR** onboarding Cowboy as a supported chain.
- **The only liquidity Cowboy fully controls is its own first-party solver over Backend A** (native CIP-25). Everything external is BD + a settlement rail fillers accept + (for non-EVM Cowboy) an adapter.

| | A — Native CIP-25 | B — ERC-7683 / OIF | C — NEAR Intents |
|---|---|---|---|
| **Liquidity source** | Cowboy's own solver(s) | EVM solver market (Across primary) | NEAR solver network |
| **Order / solver interface** | native (Cowboy events) | `IResolver` → `ResolvedOrder` (EVM-RPC gateway) | 1Click API |
| **Settlement verification** | CIP-25 L1 proof | CIP-25 proof the filler opts into | independent attestation (L1 proof, or governance oracle ≠ solver); 1Click advisory |
| **Cowboy adapter required** | none | EVM-RPC resolver gateway | 1Click client (solver-side) |
| **Trust base** | Cowboy runners / chosen CIP-25 backend | filler-accepted CIP-25 backend | NEAR solvers + 1Click custody + chosen independent attester/oracle |
| **Best corridors** | Cowboy ↔ a peered chain | EVM L2s | BTC / non-EVM |
| **Onboarding owner** | Cowboy (none external) | each solver network (per-corridor BD) | NEAR (curated listing) |

So v0 (domestic), v1 (sealed), and **Backend A** — including a trust-minimized inbound path — need no external cooperation. Backends B and C extend *reach* but each requires solver onboarding + a settlement rail fillers accept; neither is a faucet.

### Timeout, Cancellation, Refund

Follows the CIP-25 §2.6 timeout-and-reclaim pattern, with settlement-actor-specific grace sizing (`RECLAIM_GRACE = 256` blocks, sized against `DISPUTE_WINDOW_BLOCKS = 75`, is a shorter, `deadline`-relative grace than CIP-25 §2.6's `expiry + GRACE` where `GRACE ≈ 1h ≈ 3,600` blocks — the settlement `deadline` already bounds validity, so the extra margin need only cover finality lag):

- Every `Intent.deadline` and every cross-chain `Quote.quote_deadline` is enforced; expired intents/quotes revert.
- A Withdraw that is not proven delivered within `WITHDRAW_TTL` (default 24h, matching CIP-25 asset-bridge TTL) unlocks `reclaim(request_id)`: the user recovers escrow after `deadline + RECLAIM_GRACE`.
- The Settlement actor invokes the originating actor's `on_settlement_timeout(request_id)` hook so application state can unwind. The hook is **best-effort**: a reverting or out-of-gas hook MUST NOT block the reclaim/refund — it can never trap the user's escrow.

### Events

```python
Settled(bundle_hash: bytes32, solver: address, n_intents: u32, surplus: list[TokenDiff])
IntentBroadcast(request_id: bytes32, intent_hash: bytes32, sealed: bool)
AuctionOpened(request_id: bytes32, reveal_height: u64)
SealedBidSubmitted(request_id: bytes32, solver: address)
AuctionRevealed(request_id: bytes32, winner: address)     # emitted on RevealOutcome::Settled
AuctionCancelled(request_id: bytes32)                     # CancelledNoValidBid or CancelledGraceExpired
WithdrawRequested(request_id: bytes32, signer: address, dst_chain: u64, amount: u128, backend: u8)
WithdrawDelivered(request_id: bytes32, proof_id: bytes32)
DepositCredited(proof_id: bytes32, recipient: address, token_id: bytes32, amount: u128)
SettlementReclaimed(request_id: bytes32, signer: address)
```

### Parameters

All are consensus-affecting — changing one changes which txs/bundles succeed → `receipt_root` → a coordinated **flag-day** activation. Governance-set unless a default is given.

| Parameter | Role | Default / notes |
|---|---|---|
| `MAX_SETTLE_INTENTS` | max intents per `settle` bundle | **64** (initial). MUST be validated at implementation against the *measured* per-bundle cycle cost so a single bundle's worst case stays ≤ 25% of `LANE_SYSTEM_CYCLES` (≤ 10M of 40M); governance-tunable |
| `MAX_DIFFS_PER_INTENT` | max `TokenDiff`s per intent | **16** (→ ≤ 1024 token-diffs/bundle with the above); governance-tunable |
| `GC_GRACE` | retain a consumed `(signer,nonce)` / broadcast record until `deadline + GC_GRACE`, then GC | **256 blocks** (margin past `deadline` covering finality lag; comfortably ≥ `DISPUTE_WINDOW_BLOCKS = 75`); governance-tunable |
| `AUCTION_GRACE` | sealed-auction key-release liveness fallback (`reveal_height + AUCTION_GRACE`) | **50 blocks** |
| `MAX_SEALED_BIDS_PER_AUCTION` | distinct solvers per sealed auction (bounds reveal decrypt work) | **256** |
| `MAX_AUCTION_REVEAL_HORIZON` | max `reveal_height − block` at `open_auction` (bounds a coordinator's reveal-delay grief) | **1_000_000 blocks** |
| `MAX_SEALED_BID_BYTES` | max `WrappedDek` ciphertext size (codec, fail-closed) | **8192 bytes** |
| reveal gas | `RevealAuction` charges per decrypted bid + per settle-attempt intent, over the system lane (`LANE_SYSTEM_CYCLES`) | `REVEAL_DECRYPT_PER_BID_CYCLES=9_000`, `REVEAL_SETTLE_ATTEMPT_PER_INTENT_CYCLES=5_000` |
| `RECLAIM_GRACE` | escrow reclaim unlocks at `deadline + RECLAIM_GRACE` | **256 blocks** (≥ destination-chain finality buffer); governance-tunable |
| `WITHDRAW_TTL` | cross-chain delivery window before `reclaim` | **86_400 blocks** (≈ 24h at 1 s/block; matches CIP-25 asset-bridge TTL) |
| inbound caps | `per-tx inbound cap`, `rolling-window budget`, `outstanding-exposure budget` (+ pause/kill switch) | **No global default** — set per corridor/backend/asset at enablement; START conservative (a "trusted reach" backend gets a hard-low cap); the **pause/kill switch is mandatory** |
| `SETTLEMENT_DOMAIN_VERSION` | EIP-712 signing-domain version | **1** (fixed `u16`; bump only on a consensus-critical interface/invariant change) |

---

## Worked Examples

### A. Domestic agent-to-agent settlement (no cross-chain)

```
Two agents (each an account) hold ledger balances. Agent X wants wood for gold; Agent Y the reverse.
1. X signs Intent{ diffs: [GOLD -10, WOOD +5], nonce, deadline }   (X's account secp256k1 key)
2. Y signs Intent{ diffs: [WOOD -5, GOLD +10], nonce, deadline }
3. A solver runner (or either agent) submits SettlementBundle{[X, Y]}.
4. Settlement actor checks: GOLD net = 0, WOOD net = 0  ✓ (conserved)
   min-receive: X gets ≥5 WOOD ✓, Y gets ≥10 GOLD ✓
5. Applies atomically; emits Settled. No solver inventory needed — a pure match.
```

### B. Cross-chain swap via Backend C / NEAR Intents (CBY → USDC on Ethereum)

```
1. user signs Intent{ diffs: [CBY -1000], dest_chain: ETH, dest_recipient: 0xUser }
   with a quoted delivery floor X_min USDC (enforced at prove_delivery, NOT a ledger diff).
2. request_withdraw escrows 1000 CBY in the Settlement actor.
3. Sealed auction (optional): solvers submit CBSS-encrypted bids for this request.
4. Reveal @ height H: the first bid in bidder-list order that decodes to a conserving bundle filling
   this request wins (deterministic first-valid — v1 is not surplus-ranked); it commits via
   ICrossChainSettlement.begin (Backend C / NEAR).
5. Solver fronts its OWN USDC inventory and routes via 1Click; ≥ X_min USDC lands at
   0xUser on Ethereum. The 1000 CBY escrow stays locked as the solver's reimbursement.
6. Solver calls prove_delivery; actor releases the 1000 CBY (+fee) to the solver.
   If no delivery by WITHDRAW_TTL, user reclaims the 1000 CBY.
```

---

## Security Considerations

| Risk | Mitigation |
|---|---|
| **Solver creates value (forged fill)** | `Settlement-Conservation` is checked on-chain for every token in every bundle; a non-conserving bundle reverts. The actor never trusts solver-supplied amounts. |
| **User receives less than signed-for** | `Settlement-MinReceive` enforced per positive diff. Price improvement allowed; shortfall reverts. |
| **Replay / double-spend of an intent** | `(signer, nonce)` consumed on first inclusion; `deadline` height bound. |
| **CIP-20 transfer-hook reentrancy** | Only `deposit`/`withdraw_local` call CIP-20 transfer; they use checks-effects-interactions + a reentrancy guard. `settle` touches the internal ledger only and has no external-call surface (see §Custody & Reentrancy). |
| **Settlement is consensus-critical (system actor)** | The core is native, so a bug is protocol-level. Mitigated by scoping to the stable ledger + conservation + verify core (solvers/sealing/bridges stay off the consensus path) and the same review/audit bar as RAS/CBSS. No separate intent key exists to compromise — intents use the account's own secp256k1 key. |
| **Actor forging an intent (provenance auth)** | Runtime sets `signer = tx.from` (payload signer ignored), requires empty signature + a live actor + an `econ.intent` manifest grant. `settle` accepts an unsigned actor intent ONLY on exact-`intent_hash` match to an active broadcast record, consumed atomically with `(signer,nonce)`. A solver cannot fabricate actor intents, and an actor can only move its own balance. |
| **PVM as a trusted secret-holder** | Out of scope by construction: PVM actors are never decryptors or auction adjudicators (v0/v1). Sealing/reveal is CBSS-committee + native only (see §Sealed-Bid). |
| **Front-running / MEV on the open path** | SEALED mode (CIP-24 tlock) hides bids and optionally the intent until a fixed reveal height; no last-look, no end-time sniping. |
| **CBSS committee fails to release reveal key** | Liveness fallback: a permissionless `RevealAuction` after `reveal_height + AUCTION_GRACE` **cancels** the auction (`CancelledGraceExpired`) and clears all state — it does **not** degrade to a plaintext OPEN round. No escrow is held at open/bid time, so nothing is trapped. |
| **Cross-chain leg fails / stalls (NEAR backend)** | Escrow + `WITHDRAW_TTL` + `reclaim`; `on_settlement_timeout` unwinds app state (mirrors CIP-25 §2.6). |
| **NEAR / 1Click custodial-window trust** | Backend C is governance-selected per corridor, value-capped, and never the only option (Backend A is trust-minimized native CIP-25). Document the added trust explicitly per WP-v2 §16.2. |
| **Solver self-attestation (Backend C / inbound)** | Escrow release and `credit_deposit` accept ONLY an attestation independent of the filling solver (objective L1 proof, or a governance attester set / threshold oracle the solver set cannot influence); 1Click status is advisory. A solver cannot self-attest delivery. |
| **Compromised inbound attester mints CBY** | `credit_deposit` is bounded by `min(proof amt, per-tx inbound cap, rolling-window budget, outstanding-exposure budget)` on **inbound** governance params (distinct from outbound) + a pause/kill switch; each external deposit id is consumed once and requires external lock/burn/final-settlement proof — no double-mint. |
| **7683 filler reimbursement trust (Backend B)** | A 7683 filler is only repaid against a CIP-25 settlement proof it has explicitly onboarded; 7683 itself grants no trust. The resolver is the trust point: wrong/incomplete `Assumption[]` makes it unsafe/non-compliant — mitigated by versioned resolver IDs, audited gateway code, explicit backend-ID/finality/caps/TTL, and fail-closed resolution. Fillers still choose whether to trust it. |
| **Chain-Signatures MPC added to trust base** | The MPC set is a bridge-backend trust assumption, gated by the same governance as any CIP-25 backend; per-pair caps bound exposure. |
| **Invalid Cowboy tx from MPC relayer** | Relayer MUST use the canonical node tx builder; a divergent encoder is rejected by the node and treated as a correctness bug (see §Chain Signatures). |
| **Sealed-bid solver griefing (undecryptable / invalid reveal)** | Sealed bidders are CIP-2 registered; invalid ciphertext or a revealed bundle that fails verification is detected at `reveal`/settle and slashable via the **solver-mode dispute** (CIP-2 stake/slashing, settlement-time trigger — §Solver Model). |

### Adversary notes

- **Conservation is the trust anchor.** Unlike a bridge that must attest external truth, domestic settlement requires zero external trust — the worst a malicious solver can do is submit a bundle that reverts.
- **Cross-chain inherits backend trust.** Backend A's adversary model is exactly CIP-25's (committee/ZK/optimistic). Backend B (7683) inherits whatever CIP-25 settlement backend the filler opted into — 7683 adds no trust. Backend C additionally trusts NEAR's solver network and the chosen delivery attestation. Both external backends are the price of liquidity reach and must be conscious, capped, governance choices — not defaults.

---

## Rationale

**Why a token-diff Verifier rather than an order book?** Conservation-checking a signed diff set is O(intents) and stateless beyond the ledger; it needs no resting orders, no matching engine on-chain, and no price oracle. Solvers absorb all complexity off-chain. This is the same insight NEAR's verifier and CowSwap's batch settlement exploit.

**Why a system actor rather than a PVM actor?** (See §Intent Authorization.) Settlement is core economic infrastructure — it custodies all deposits and runs a hot conservation loop — the same category in which CIP-20 made tokens native. Native execution verifies the account's own secp256k1 key (no separate intent key, no PVM `ecrecover` host call) and escapes the per-`settle` PVM cycle ceiling. Pre-mainnet, claiming the protocol surface is free; later it would be a migration. Only the stable core goes native; solvers, sealing, and cross-chain stay off the consensus-critical path.

**Why reuse CIP-2 runners as solvers instead of a new role?** Runners already carry stake, VRF selection, and a dispute window — the exact accountability a sealed-bid auction needs. Inventing a parallel "solver registry" would duplicate CIP-2 and fragment slashing.

**Why CBSS sealing rather than commit-reveal?** Naive commit-reveal lets a solver withhold its reveal to grief the auction. A threshold-IBE time-lock makes the key *become available* at the reveal height **independent of any bidder** (under the CBSS committee-liveness assumption — committee liveness is still required, and §Sealed-Bid specifies the AUCTION_GRACE fallback if it fails), so a non-revealing solver cannot block clearing. This is the property that makes sealed solving robust. CIP-24's `tlock` construction supports it, and the height-triggered, auction-identity release profile is the **CIP-24 §3.4.6 Time-Lock Release amendment** (now merged; §Sealed-Bid).

**Why make external networks backends rather than the mechanism?** CIP-25 §1.4 / WP-v2 §16.2 already establish that bridge selection is governance, not protocol-baked. Treating NEAR Intents and the ERC-7683/OIF adapter as `ICrossChainSettlement` implementations keeps trust-minimized native settlement (Backend A) available and lets governance opt into external reach (Backends B/C) only where the liquidity is worth the added trust and onboarding cost.

**Why not just use NEAR Intents end-to-end and skip the native actor?** That would forfeit Cowboy's differentiators — CBSS-sealed fair auctions, domestic zero-trust agent-to-agent settlement, and inference billing — and put a custodial third party on the critical path for *every* settlement, including ones that never leave Cowboy.

---

## Backwards Compatibility

New standard; no installed base. Settled assets are existing CIP-20 tokens and CBY. The Settlement actor is additive; no existing CIP changes behavior. Consistent with the protocol's no-back-compat-shim policy.

---

## Reference Implementation

- Settlement **system actor** (ledger + `settle` + invariants + native secp256k1 verify): a native module in `node/execution/src` at address 0x14 (WP §9.1), invoked via its `SystemInstruction` opcodes (allocated in the `cowboy-protocol-codec` enum + uniqueness test; no central WP §9.2 table — see §Protocol Allocations). Plus the `actor_instruction.rs` system-target branch for `INTENT_SETTLEMENT` (decode/validate the self-service `IntentBroadcast`/`IntentDeposit`/`IntentWithdraw` only; deferred `Instruction::System` with `sender = message.from`; no message enqueue; distinct source event; tests for sender-stamping, no-enqueue, malformed-payload fail-closed, insufficient-gas, duplicate-nonce, and forged-signer rejection), an OPEN-mode solver-loop example, and a domestic A↔B match (Worked Example A). Target: devnet, no cross-chain dependency.
- `examples/settlement-sealed/` — CIP-24 sealed-bid auction with permissionless `RevealAuction` (opcode 156) reveal (v1; native handler + CBSS committee only, **no PVM secret path**) (Worked Example B, sans external leg).
- ERC-7683 resolver adapter (Backend B): an EVM-RPC gateway exposing `IResolver`/`ResolvedOrder`; the Across-over-7683 settlement path is a follow-on.
- NEAR backend (Backend C) and the Chain-Signatures relayer spike are tracked separately; the relayer MUST build Cowboy txs via the node's canonical builder.

### Phasing

- **v0** — Settlement **system actor** (native core: ledger + conservation + secp256k1 verify) + **PVM-actor self-service** (translator for `IntentBroadcast`/`IntentDeposit`/`IntentWithdraw` → provenance auth + `econ.intent` gate; `IntentSettle` stays top-level) + OPEN mode + one runner-solver; domestic only. Validates on devnet.
- **v1** — SEALED mode (CIP-24) + competitive solvers; the differentiated product.
- **v2** — Cross-chain reach: Backend A (native CIP-25) first; then Backend B (ERC-7683/OIF resolver adapter, Across corridor) and Backend C (NEAR 1Click + Chain-Signatures) as reach extensions, each gated on a settlement rail fillers accept.

---

## Acknowledgments

The token-diff / solver / verifier settlement model follows [NEAR Intents](https://docs.near-intents.org) and its Verifier contract ([introduction](https://docs.near-intents.org/integration/verifier-contract/introduction), [intent types & execution](https://docs.near-intents.org/integration/verifier-contract/intent-types-and-execution)), and the broader intents lineage (CowSwap batch auctions, UniswapX). The ERC-7683 / Open Intents Framework backend follows the current resolver-centric [EIP-7683](https://eips.ethereum.org/EIPS/eip-7683) (`IResolver` / `ResolvedOrder`) and [Across's ERC-7683 settlement model](https://docs.across.to/guides/concepts/erc-7683). NEAR docs corroborate the verifier internal-ledger model, Message Bus atomic execution, the 1Click trusted-swapping-agent custody window, and Chain Signatures domains (0 = secp256k1, 1 = ed25519). Chain Signatures background from [NEAR's chain-abstraction docs](https://docs.near.org/chain-abstraction/chain-signatures). The sealed-auction anti-sniping rationale parallels CIP-22; the cross-chain timeout/refund semantics follow CIP-25 §2.6.
