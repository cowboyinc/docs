---
title: "CIP-25: Cross-Chain Architecture"
description: A three-layer model for cross-chain state anchoring, messaging, and applications — with pluggable trust backends
icon: link
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-04-23
  **Updated:** 2026-05-11 (r1.1)
  **Requires:** CIP-2 (Off-chain Compute / Runners), CIP-4 (State & Merkle Proofs), CIP-7 (Watchtower Streams)
</Note>

> **Revision history**
>
> - **r1.1 (2026-05-11)** — §1.4 governance scope clarified: this CIP defines the `IChainAnchor` interface contract; **which** backend is deployed per chain-pair is a WP-v2 §16.2 governance decision. Reconciles apparent tension between WP §16.2's "no protocol validator set" framing and CIP-25's runner-committee backend (see the §1.4 governance note).
> - **r1 (2026-04-23)** — Initial three-layer architecture draft.

## Abstract

CIP-25 defines Cowboy's cross-chain architecture as **three orthogonal layers**, each with a single, focused responsibility:

| Layer | Name | Responsibility |
|-------|------|----------------|
| **L1** | Cross-Chain State Anchoring | Make each chain's block commitments **verifiable** on every other chain, with Merkle-proof primitives. Does NOT transfer value or relay messages. |
| **L2** | Cross-Chain Messaging | A mailbox abstraction built on L1: address-to-address payload delivery between chains, with strong ordering and delivery semantics. |
| **L3** | Cross-Chain Applications | Asset bridges, cross-chain lending, oracle relays, generic contract calls — anything built on L2 messages or directly on L1 proofs. |

The three layers are composed bottom-up and **each layer is replaceable**. L1 supports multiple trust backends (runner-attested committees, ZK light clients, optimistic proofs, native light clients) behind a uniform interface. L2 does not need to know which backend L1 is using.

---

## Motivation

### The cross-chain problem, at the architectural level

Most cross-chain systems ship as **vertical products**: "this bridge moves USDC between chain A and chain B." Each such product re-implements, to varying degrees of care:

1. A way to convince chain B that a specific event happened on chain A.
2. A way to encode the semantics of that event (address, amount, reason).
3. A way to act on it without being exploitable.

Baking these three concerns into a single monolith produces:

- **Trust lock-in.** Switching the proof mechanism (e.g., from multisig committee to ZK) means rewriting the product, not swapping a backend.
- **Integration friction.** Every new application needs its own event-decode and replay-protection logic.
- **Auditing opacity.** Security analysis cannot be factored: the relayer, the encoder, and the application logic are entangled.

### Why three layers

Separating concerns into three layers makes each layer testable, replaceable, and composable:

- **L1 answers "what is true on the other chain, provably?"** Nothing about value, users, or semantics lives here. A single primitive — Merkle-proof-checked block root anchoring — powers every downstream application.
- **L2 answers "how do two actors on different chains exchange a typed payload, exactly once, in order?"** Built on L1, but not tied to any specific application.
- **L3 is where products live.** Asset bridge, lending, oracle relay — each one is a thin application on top of L2 (or, for gas-critical paths, directly on L1).

This is the same layering pattern that gave us TCP/IP: L1 is like packet delivery + cryptographic integrity, L2 is like TCP streams, L3 is like HTTP/SMTP/RTMP.

### Why Cowboy is well-positioned

Cowboy brings native features that reduce the friction at each layer:

- **Runners (CIP-2)** provide a ready-made signing committee for L1 attestation, gated by stake and VRF selection.
- **Native Merkle proofs (CIP-4)** mean Cowboy state is already commit-verifiable — destination chains do not need to re-compute anything, only recognize the commitment.
- **Timers (CIP-5)** allow per-block L1 anchoring without keeper-bot dependencies.
- **Watchtower (CIP-7)** already solves intra-chain streaming; extending it across chains via L2 gives us **cross-chain streaming data/AI inference** as a first-class capability — something no general-purpose bridge offers.

### What this CIP is NOT

- It is **not a single-bridge specification.** Asset-bridge logic is an L3 application, specified separately.
- It is **not a trust-backend specification.** L1 is an interface; the runner-attestation backend is one valid implementation, cited here as a reference topology, but ZK, optimistic, and native-light-client backends are equally legitimate; all four are enumerated in §1.4.
- It is **not a consensus change.** Nothing in this architecture modifies Cowboy's L0 (block production, finality, or state commitment). It only standardizes how one chain exposes and consumes another chain's commitments.

---

## Architecture Overview

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  L3  Applications                                               │
  │      asset bridge · lending · oracle relay · generic call       │
  │      streaming data feeds · cross-chain inference               │
  └──────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼ typed payloads, replay protection
  ┌─────────────────────────────────────────────────────────────────┐
  │  L2  Cross-Chain Messaging                                      │
  │      Mailbox(src, dst, sender, recipient, nonce, payload)       │
  │      ordered · exactly-once · bidirectional · multi-chain       │
  └──────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼ "this root is real, this leaf is in it"
  ┌─────────────────────────────────────────────────────────────────┐
  │  L1  Cross-Chain State Anchoring                                │
  │      anchor(chain, height, roots) → commitment                  │
  │      verify_inclusion(chain, height, proof, leaf) → bool        │
  │      pluggable trust backend                                    │
  └──────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼ pluggable
  ┌─────────────────────────────────────────────────────────────────┐
  │      ├─ runner-attested committee (2-of-3 ECDSA)                │
  │      ├─ ZK light client (future)                                │
  │      ├─ optimistic with challenge window (future)               │
  │      └─ native light client (chain-specific, future)            │
  └─────────────────────────────────────────────────────────────────┘
```

**Key property:** each arrow is a well-typed interface. Swapping the trust backend at L1 does not touch L2. Adding a new L3 application does not touch L1.

---

## Layer 1 — Cross-Chain State Anchoring

### 1.1 Responsibilities

L1 is responsible for **one thing and only one thing**: making the block commitments of a source chain verifiable on a destination chain. Specifically:

1. **Publish** a block's cryptographic commitment (e.g., `(tx_root, receipt_root, state_root)` at height H) from source chain S to destination chain D.
2. **Verify** that a given commitment was authentically published (i.e., not forged by a single malicious party).
3. **Check inclusion**: given `(chain, height, merkle_proof, leaf)`, attest that the leaf was part of the committed root.

### 1.2 Non-Responsibilities

L1 **does not**:

- Know what kinds of things are committed (tx, receipt, state — they are all bytes to L1).
- Decode payloads (no ABI knowledge).
- Transfer value (no account balances, no allowances).
- Maintain user state (no nonces, no replay protection — those are L2/L3 concerns).
- Know which chains are "peered" for messaging — L1 anchors commitments per chain, regardless of who consumes them.

This rigorous scope is what makes L1 reusable: an L3 asset bridge, an L3 oracle relay, and an L3 inference stream all consume the same L1 primitive.

### 1.3 Data Model

The canonical L1 commitment is a tuple per `(source_chain_id, height)`:

```
BlockCommitment {
    source_chain_id: u64,
    height:          u64,
    tx_root:         bytes32,  // keccak256(BMT over canonical tx encodings)
    receipt_root:    bytes32,  // keccak256(BMT over canonical receipt encodings)
    state_root:      bytes32,  // optional, for state-proof use cases
    parent_hash:     bytes32,  // optional, for chained verification
    finalized_at:    u64,      // destination-chain timestamp of finalization
}
```

Which roots are published is a per-L1-deployment choice. The minimal configuration `(tx_root, receipt_root)` suffices for L3 applications whose logic rests on event-inclusion proofs (asset bridges, generic cross-chain calls). L3 applications that consume arbitrary source-chain state (e.g., cross-chain lending reading source-side collateral balances) additionally require `state_root`. `parent_hash` and `finalized_at` are optional and turned on depending on the backend and L3 latency policy.

### 1.4 Trust Backends (pluggable)

L1 presents a single interface to L2; the means of producing the commitment is pluggable:

| Backend | Assumption | Latency | Cost | Implementation notes |
|---------|-----------|---------|------|----------------------|
| **Runner committee** | honest majority of selected Runners (e.g., 2-of-3) | ~2 blocks | ECDSA verify + storage writes | Each runner independently signs a domain-separated ECDSA attestation; the destination contract recovers signers one by one and counts to threshold. Keys are reused from the CIP-2 runner registry. |
| **ZK light client** | soundness of proving system | seconds (proving) + 1 block | verifier call | Requires a deployable verifier contract on the destination plus a prover network producing succinct proofs. Open-source Ethereum header provers exist; the Cowboy-source direction is simpler thanks to deterministic finality. |
| **Optimistic** | 1 honest challenger during challenge window | challenge window (hours–days) | cheap happy path, expensive fraud path | Implemented by delaying `isFinalized` until `finalized_at + window`; challenger bonds and incentive mechanics are specified by a dedicated CIP. |
| **Native light client** | the destination chain can afford to verify source-chain consensus directly | 1 block | varies | Requires source-chain signature/pairing precompiles on the destination (e.g., BLS12-381 pairing for Ethereum sync-committee BLS verification; Solana's `secp256k1_recover` syscall supports committee fallback). |

All four backends produce the **same** `BlockCommitment` shape. L2 code written against L1 is agnostic to which backend is in use.

> **Governance scope (r1.1, 2026-05-11).** This CIP standardizes the **interface contract** any L1 backend MUST satisfy (`IChainAnchor`, §1.5). It does NOT mandate a specific backend protocol-wide. Per **WP-v2 §16.2** ("Bridge selection and integration are determined by governance. The protocol does not implement its own bridge validator set."), which backend is deployed for which `(source_chain, destination_chain)` pair is a governance decision — including the runner-attested committee backend described in §1.4 / §1.6 (which Tony's team's existing outbound Cowboy→Ethereum withdrawal bridge already runs in production). The "no protocol validator set" wording in WP-v2 §16.2 should be read narrowly as "no single mandatory bridge validator set"; the `IChainAnchor` interface explicitly admits both Cowboy-runner-attested backends and pure third-party backends on equal footing. This reconciliation is documented in WP §16.2 (governance framing above) and, for the historical delta rationale, in `analysis/2026-05-28_wp-aligned-deltas-v2.md`.

> **Implementation/specification status (2026-09).** The Cowboy node implements the committee publication path; it is not an unimplemented future backend. The native light-client registry actor, governance pin, trusted-emitter rules, and development-only unadjudicated-committee admission flag still require a separate normative amendment to this section. The implementation notes in §1.6 record current behavior but do not complete that amendment or authorize native light-client activation.

### 1.5 Interfaces

On the **destination** chain, L1 exposes:

```solidity
// Pseudo-interface; concrete form varies per destination chain type.
interface IChainAnchor {
    /// Returns true if the commitment for (chainId, height) is finalized.
    function isFinalized(uint64 chainId, uint64 height) external view returns (bool);

    /// Returns the committed roots for (chainId, height), or revert.
    function commitment(uint64 chainId, uint64 height)
        external view returns (bytes32 txRoot, bytes32 receiptRoot, bytes32 stateRoot);

    /// Verify that `leaf` is a member of the Merkle tree committed as
    /// `root` for the given chain/height. Returns true on valid proof.
    function verifyInclusion(
        uint64 chainId, uint64 height, bytes32 root,
        bytes32 leaf, bytes32[] calldata siblings, uint32 index
    ) external view returns (bool);
}
```

On the **source** chain side, the publish path depends on the backend. For a runner-committee backend, the source side selects N runners via VRF and each signs `keccak256("Anchor.v1" || src_chain_id || dest_anchor_addr || height || tx_root || receipt_root)` with a registered ECDSA key, then submits the signature on the destination chain. The `"Anchor.v1"` domain prefix prevents signature reuse across protocols or versions.

> **Normative (2026-07): the signing preimage MUST bind every root the deployment exposes.** The preimage above is the minimal `(tx_root, receipt_root)` form. A deployment that publishes any additional optional root from `BlockCommitment` — `state_root` (which `IChainAnchor.commitment()` returns, §1.5) and/or `parent_hash` — **MUST** extend the `Anchor.v1` preimage to include those exact fields, in the fixed order `… || height || tx_root || receipt_root || state_root || parent_hash`, appending only the fields that are actually published. Otherwise the committee attestation does **not** authenticate the extra root: the destination `IChainAnchor` would return a `stateRoot` (line 182) that no runner ever signed, letting a single malicious relay/committee member present a valid `(tx_root, receipt_root)` signature while substituting an arbitrary `state_root` — forging any source-chain state consumed by L3 apps (§1.3, "additionally require `state_root`"). Implementers of the committee backend and reviewers of that PR MUST verify this binding before production use; it is load-bearing for every `state_root`-consuming deployment.

### 1.6 Failure Modes and Solutions

Known failure modes of the runner-attested backend, each paired with a concrete solution. Every row is load-bearing and must have a complete implementation before production use.

| Failure mode | Solution |
|---|---|
| **Runner majority colludes to forge a commitment** | Four defenses composed:<br>**(1) Economic floor**: each runner locks `stake ≥ k × max_attestable_value` at registration (suggested k=10); the attack's economic cost lower bound is `threshold × stake`.<br>**(2) Automatic slashing**: the committee-attestation consensus verifier buckets each batch of runner submissions by `(tx_root, receipt_root)`; runners whose submission disagrees with the majority enter the **dissenters list**, which the dispatcher feeds directly to the stake module to burn bonded stake — no human intervention.<br>**(3) Fraud-proof window**: after an L1 commitment lands on the destination, open a 7-day challenge window; anyone may submit "signed commitment disagrees with real source block" evidence (source Merkle proof + signature replica) to slash the signer; during the window L3 apps do NOT release funds. *Implementation note (2026-09, node `SubmitFraudProof`):* the evidence supplier is named as the slash **submitter**, so the challenger's share is the `submitter_bps` field of CIP-2 §11's `SlashDistribution` (genesis 0, "inactive at launch"); changing the genesis distribution requires the Tier-3 governance proposal specified by CIP-2 §11, with a matching amendment to WP §8.4 C7. The dedicated incentives CIP can use that existing distribution mechanism, subject to those approvals.<br>**(4) Dual-backend defense-in-depth** (see §B.6): high-TVL apps require both the committee backend AND the ZK backend to agree on the commitment; on mismatch, revert and slash the entire committee. |
| **Runner offline causing finalization delay** | **(1) Liveness timeout**: if dispatcher does not see threshold signatures within N blocks (default 100) after assignment, trigger re-election; replacement runner is VRF-selected with the original runner excluded.<br>**(2) Reputation ledger**: each runner persists `(assignments, completions, timeouts)`; VRF election weight = `(completions / assignments) × stake`, so unreliable runners win selection less often.<br>**(3) Probation state**: after M consecutive timeouts (default 5) a runner enters 24h probation, no new jobs assigned; repeated probation triggers stake penalty or forced deregistration.<br>**(4) Timeout incentive**: the replacement runner earns a "rescue bonus" deducted from the original runner's stake, creating a stable mutual-coverage market. |
| **Commitment published for a re-orged source block** | **(1) Cowboy source (deterministic finality)**: runners only sign blocks where `H ≤ finalized_height` (the runner reads Cowboy's finalized height from the node); Cowboy BFT finality is irreversible, so this check is sufficient.<br>**(2) Ethereum / probabilistic-finality source**: backend parameter `min_confirmations` (default 32 slots, up to 64 for high-TVL deployments); runners wait until `current - H ≥ min_confirmations` before signing. Parameter is configured at backend deploy time per security budget.<br>**(3) Revocation primitive**: if a source-chain reorg is detected deeper than `min_confirmations` (shouldn't happen but exists as a safety net), a threshold of runners may co-sign a `commitment_revoked(chain, height)` message submitted to the destination, added to a revocation set; downstream L2 checks this set in `deliver` and reverts on hit.<br>**(4) Runner-side monitoring**: runner daemon runs local reorg detection; any `reorg_depth > alerted_threshold` (default 3 blocks) triggers off-chain alerts and the backend pauses new attestations.<br>*Implementation note (2026-09):* for committee publication to a **Cowboy destination** with an independent fraud verifier (§1.6 defense 3), the destination admits a committee job only once the verifier's finalized head has retained a checkpoint at or above the height, so committee commitments exist only for source blocks that are already finalized and `min_confirmations` is a floor the finality gate dominates. A later proved disagreement with finalized truth is therefore attributable to the signer without the deep-reorg exemption a confirmation-depth-only rule would need. The reference producer waits for execution-client finality (`eth_getBlockByNumber("finalized")`) when that RPC succeeds; on a provider error it warns and falls back to confirmation-depth waiting. The on-chain admission gate remains authoritative. Other destination kinds do not pass through this node finality, registry-pin or verifier admission gate; confirmation depth remains the source-side defense described in (2). In the Cowboy-destination path, each commitment is also bound at publication to the source entry's kind, verifier fingerprint and registry, the registry refuses in-place edits of those fields, and a committee source with no verifier is admissible only on a chain governance has declared unadjudicated (`cip25.bridge.unadjudicated_committee`), never on a production chain. Configuration changes are timelocked rather than edited: a governance re-pin of `cip25.bridge.native_lc_registry` leaves commitments published under the former registry adjudicable for the window plus one day of grace, and a registry entry that has admitted a committee job admits no new window once retired and can be removed only after that same eight-day delay. The registry and governance parameters named here describe the implementation; their normative definition remains part of the pending §1.4 amendment described above. |
| **Stale commitment consumed by a downstream application** | **(1) `finalized_at` exposed**: L1's `BlockCommitment.finalized_at` records the destination-chain timestamp, readable by L2/L3.<br>**(2) Application-enforced TTL**: L3 (or L2 `deliver`) checks `block.timestamp - finalized_at ≤ APP_TTL`; over-age messages revert. TTL is parameterized with recommended defaults (matching §2.6's `expiry`): asset bridge 24h, oracle push 5 min, lending 15 min, one-shot RPC call 2 min.<br>**(3) Timeout-and-cancel pattern** (§B.6 pattern 2): after TTL, the source-side sender may invoke `reclaim()` to retrieve escrow; the destination contract, on confirming the original message was never consumed, permits revocation submission. User funds are never permanently stuck.<br>**(4) Protocol-level GC**: a batched cleanup tx prunes commitments older than `finalized_at + MAX_TTL` (default 1 week), reclaiming destination storage and shrinking the attack surface. |
| **L3 application cannot produce Merkle proof** | Three co-existing proof paths; applications choose:<br>**(1) Client self-service**: Cowboy RPC already exposes `/proof/tx/<hash>` and `/proof/receipt/<hash>` endpoints returning `(siblings, index, leaf)`; suitable for low-frequency apps and user-triggered claims.<br>**(2) Runner proof service**: new job type `generate_inclusion_proof { chain, height, leaf_key }`; apps submit via `submit_job`, runners compute from local index and return; cost paid in CBY, distributed to runners via protocol fee split. Suitable for automated applications that cannot require end-users to fetch proofs themselves.<br>**(3) Third-party indexer**: extra-protocol but encouraged ecosystem role — independent indexers maintain a secondary index on `tx_root` for all blocks, exposing a high-availability REST API; L3 apps configure the indexer endpoint and get low-latency queries.<br>Contract-side verification logic is identical across all three paths — no contract-side support needed. |
| **Multiple destination chains consuming the same commitment** | Three-phase cost reduction:<br>**(1) Stage 1 (baseline)**: runners send a separate attestation tx per destination chain; cost `O(runners × dst_chains)`. Adequate for ≤3 destination chains.<br>**(2) Stage 2 — BLS aggregation**: runners produce a single BLS12-381 signature; any destination chain with pairing precompile support (Ethereum post-EIP-2537, Arbitrum/Optimism, future L2s) can verify the same signature; cost drops to `O(dst_chains)` (txs still sent per chain, but signature work amortizes).<br>**(3) Stage 3 — cross-chain broadcast**: a single relayer tx carries the aggregated signature plus a destination-chain list, forwarded to a `BroadcastRelay` meta-contract that fans out to all listed chains; cost becomes `O(1)` signatures + `O(dst_chains)` storage.<br>**(4) Non-pairing-chain fallback**: for chains without BLS precompile (Solana et al.), use threshold ECDSA (`t-of-n` aggregated into one secp256k1 signature); Solana programs verify via the `secp256k1_recover` syscall; cost comparable to BLS. Stages 2–3 depend on the cryptographic construction of signature aggregation and destination-chain precompile support; they can be delivered incrementally. |

---

## Layer 2 — Cross-Chain Messaging

### 2.1 The Mailbox Primitive

L2 defines a typed mailbox:

```rust
struct CrossChainMessage {
    src_chain:    u64,
    dst_chain:    u64,
    dst_mailbox:  Address,   // destination Mailbox contract this message may be delivered to
                             // (L2-Deployment-Isolation, §B.2); bound into hash(msg)
    sender:       Address,   // on src_chain
    recipient:    Address,   // on dst_chain
    nonce:        u64,       // per (sender, dst_chain), strictly monotonic
    payload:      bytes,     // arbitrary application bytes
    gas_limit:    u64,       // suggested delivery gas (optional)
}
```

Producing a message on the source chain emits a canonical event; delivering it on the destination chain requires an L1 inclusion proof of that event plus a replay-protection check against the tuple `(src_chain, sender, dst_chain, nonce)`.

### 2.2 Delivery Semantics

L2 provides:

- **Exactly-once delivery.** A `(src_chain, sender, dst_chain, nonce)` triple is consumed on first valid delivery; subsequent attempts revert.
- **Per-(sender, dst_chain) ordering.** Messages from a given sender to a given destination chain are delivered in the order they were emitted. This is enforced by requiring consecutive nonces. (Per-sender-pair, not global.)
- **Best-effort liveness.** Anyone — typically Runners or third-party relayers, economically incentivized — can submit the proof that delivers a message. L2 does not guarantee latency, only eventual delivery.
- **No payload interpretation.** L2 is opaque; recipient decodes.

### 2.3 Delivery Mechanism

Delivery on the destination chain takes the form:

```solidity
function deliver(
    CrossChainMessage calldata msg,
    bytes32[] calldata merkleProof,
    uint32   merkleIndex
) external {
    require(anchor.isFinalized(msg.src_chain, blockOf(msg)));
    // §1.6 re-org defense (3): reject commitments revoked by the committee.
    require(!anchor.isRevoked(msg.src_chain, blockOf(msg)));
    // L2-Deployment-Isolation (§B.2): this mailbox only accepts messages addressed to it.
    // dst_mailbox is part of hash(msg), so inclusion + this check bind delivery end-to-end.
    require(msg.dst_mailbox == address(this));
    require(anchor.verifyInclusion(msg.src_chain, blockOf(msg),
        anchor.commitment(msg.src_chain, blockOf(msg)).txRoot,  // or event root
        hash(msg), merkleProof, merkleIndex));
    require(!consumed[msg.src_chain][msg.sender][msg.dst_chain][msg.nonce]);
    consumed[msg.src_chain][msg.sender][msg.dst_chain][msg.nonce] = true;
    IRecipient(msg.recipient).onCrossChainMessage(msg.src_chain, msg.sender, msg.payload);
}
```

### 2.4 Bidirectionality and Multi-Chain

L2 is symmetric. The same protocol defines:

- Cowboy → Ethereum messaging (minimal case: payload is a single withdrawal record, but still goes through the full `send`/`deliver` path).
- Ethereum → Cowboy messaging (requires an L1 backend that anchors Ethereum commitments on Cowboy; see §1.4).
- Cowboy ↔ Solana messaging (L1 backend for Solana commitments on Cowboy, and vice versa).
- Third-party pair messaging (Ethereum ↔ Solana routed via Cowboy) is **out of scope** — L2 is point-to-point, not a router.

### 2.5 Extension: Streaming

Watchtower (CIP-7) already provides ordered, replayable, per-stream-monotonic messaging within a single chain. Extending it across chains is a natural composition:

- A Cowboy `StreamActor` emits Watchtower messages at sequence `k`.
- Each message is also a valid L2 send to a mirror `StreamActor` on the destination chain.
- The destination `StreamActor` delivers to its subscribers locally.

This yields **cross-chain streaming feeds** without modifying the L2 mailbox primitive. Concrete instances:

- **Cross-chain price feeds.** A Cowboy oracle StreamActor drives a consumer contract on Ethereum.
- **Cross-chain AI inference.** A Cowboy inference StreamActor produces results available to Solana applications.
- **Cross-chain live data.** Video-metadata streams, alerts, game events — anything Watchtower supports intra-chain becomes available cross-chain.

Cross-chain streaming needs total ordering across senders per stream, which is stronger than the default per-sender nonce. §2.6's `send_stream` primitive is designed for exactly this — it maintains a monotonic `seq` per `stream_id` across all senders, and destinations enforce contiguous delivery. L1 requires no additional work for streaming and carries standard commitments only.

### 2.6 Design Extensions and Solutions

Three classes of L2 extensions beyond the current mailbox primitive, each with a concrete mechanism.

| Requirement | Solution |
|---|---|
| **Gas-sponsorship model** (who pays destination-chain `deliver` gas) | Source-side prepayment + split + fallback:<br>**(1) Source-side prepayment**: `Mailbox_C.send(..., fee)` parameter (CBY); the source Mailbox actor debits `fee` from the sender and escrows it into `pending[msg_hash].fee`.<br>**(2) Protocol split**: `fee` divides into `protocol_cut` (suggested 20%) to treasury and `relayer_bounty` (80%) reserved for the deliverer.<br>**(3) Default path — runner-managed**: the runner committee watches `Mailbox_C.MessageSent` events and submits `deliver()` from each runner's own destination account; on success it posts `claim_bounty(msg_hash, delivery_proof)` to the source chain and claims the reserved bounty. `delivery_proof` is a reverse L1 commitment: "destination block at height X contains the `Delivered(msg_hash)` event."<br>**(4) Third-party arbitrage fallback**: anyone may call `deliver()` and submit `claim_bounty` on first-come-first-served basis. If `fee` is too low for any relayer to take, sender can call `bump_fee(msg_hash, extra)` to raise it.<br>**(5) Refund**: if no delivery occurs within `MAX_TIMEOUT` (default 7 days), the sender may call `reclaim_fee(msg_hash)` to recover the unused bounty. |
| **Timeout / cancellation** (how the sender recovers escrow when a message is never delivered) | Hard cutoff + grace period + reverse confirmation:<br>**(1) Parameterized expiry**: `send(..., expiry: u64)`, where `expiry` is a destination-chain timestamp (default 24h ≈ 86,400 Cowboy blocks at 1s per WP §6.1 ≈ 7,200 ETH blocks at 12s); recorded in source `pending[msg_hash]`.<br>**(2) Destination hard cutoff**: `Mailbox_E.deliver(msg, proof)` enforces `block.timestamp ≤ msg.expiry`; post-expiry delivery reverts. Guarantees no delivery can succeed after expiry.<br>**(3) Grace window**: source-side `reclaim(msg_hash)` is unlocked only after `now ≥ expiry + GRACE` (default GRACE = 1h), leaving headroom for network delay and runner anomalies.<br>**(4) Concurrency safety**: a reverse L1 commitment can be queried to confirm whether a `Delivered` event exists on destination for `msg_hash`. Because the destination enforces hard cutoff, no valid delivery can occur after grace — reclaim is therefore safe.<br>**(5) L3 callback**: after a successful reclaim, source Mailbox invokes the sender's `on_timeout(msg_hash)` hook so the L3 application can unwind its source-side state (e.g., the asset bridge refunds CBY, lending unfreezes collateral).<br>**(6) Recommended `expiry` values**: asset bridge 24h, oracle push 5 min, lending 15 min, one-shot RPC call 2 min. |
| **Cross-sender total ordering** (single recipient consumes from multiple senders in arrival order) | StreamID primitive + serializer-chain delegation:<br>**(1) New primitive `send_stream(stream_id, payload, gas_limit) → seq`**: source Mailbox maintains `next_stream_seq[stream_id]` atomically; any sender calling `send_stream` increments the per-stream counter and writes `seq` into the message.<br>**(2) Destination contiguity**: `Mailbox_E.deliver_stream(msg, proof)` accepts only messages where `msg.seq == expected_next_seq[stream_id]`; out-of-order delivery reverts. Downstream recipients always observe an ordered stream.<br>**(3) `stream_id` convention**: `keccak256(dst_chain ‖ recipient ‖ topic_bytes)`, globally unique.<br>**(4) Single-source case**: works directly — Cowboy's deterministic execution guarantees atomic sequence assignment.<br>**(5) Multi-source case** (senders spread across chains): handled as an L3 pattern — designate a serializer chain (default Cowboy); senders from other source chains first deliver to the serializer via L2, which re-emits them via `send_stream`. Cowboy's consensus timeline anchors the total order.<br>**(6) Watchtower (CIP-7) alignment**: Watchtower streams are already monotonic; `send_stream` becomes Watchtower's cross-chain egress — an explicit application submitter verifies a Watchtower message under CIP-7 and then requests `send_stream`; ANCHORED appends do not automatically invoke consensus. Hard anti-equivocation settlement requires CONSENSUS in CIP-7 v1. |

---

## Layer 3 — Applications

Applications are built on L2 messaging (or, for gas-critical paths, directly on L1 proofs).

> **Scope note.** This section gives the typology and payload skeleton for four classes of L3 application; it is **not** a complete specification of any of them. Each application's full mechanism (governance parameters, economic assumptions, liquidation rules, rate limits, etc.) belongs to its own CIP. The payload schemas below are sufficient for L2 Mailbox implementers to fix the interface contract without binding to specific application semantics.

### 3.1 Asset Bridge

**Two canonical designs:**

**Lock-mint.** User deposits native asset on chain A into an escrow actor; escrow sends a cross-chain message; receiver on chain B mints a pegged representation.

**Burn-release.** User burns a pegged asset on chain A; burn event is proven on chain B; chain B's escrow releases the native asset.

**Payload schema:**
```
abi.encode(
    recipient:  Address,  // destination-chain recipient
    token_id:   bytes32,  // asset identifier (native = 0x00; ERC-20 = token contract addr; NFT = contract ‖ tokenId)
    amount:     uint256   // quantity (unit determined by token_id semantics)
)
```

**Invariant reference:** §B.2 L3-AssetBridge-Conservation (total minted ≤ total locked).

**Failure modes:** see §B.4.5 "Liquidity drain," §1.6 "Stale commitment consumed" for application-level TTL enforcement, and §B.6 pattern 3 for rate-limited release.

**Additional L3 modes worth noting:**
- Wrapped asset on each side (e.g., canonical bridged-ETH on Cowboy).
- Swap-and-bridge (LP-based, CIP-21 composable).

### 3.2 Cross-Chain Lending / Collateral

Collateral on chain A, borrowing on chain B. **Requires bidirectional L2.**

**Payload schema (one for each direction):**
```
// A → B (deposit / liquidation notify)
abi.encode(
    action:      uint8,    // 1 = deposit, 2 = liquidate_notify
    borrower:    Address,  // cross-chain user identity
    collateral_token: bytes32,
    amount:      uint256
)

// B → A (unlock or seize)
abi.encode(
    action:      uint8,    // 3 = unlock, 4 = seize
    borrower:    Address,
    liquidator:  Address,  // only populated for seize
    amount:      uint256
)
```

**Key application-level invariant:** total borrowed ≤ collateral-value × LTV (enforced in the application, depending on a local price oracle; independent of L2).

**Failure modes:** cross-chain liquidation atomicity — L2 does NOT provide cross-chain atomic transactions; applications must rely on "over-collateralization + liquidation delay + rate-limited release" (§B.6 pattern 3) to mitigate.

### 3.3 Oracle / Inference Relay

Cowboy's AI-inference actors (CIP-2) produce deterministic outputs. With cross-chain streaming (§2.5 + §2.6 `send_stream`), those outputs become consumable by any chain.

**Payload schema (single push):**
```
abi.encode(
    feed_id:    bytes32,   // price or inference feed identifier (keccak256 of feed name)
    value:      bytes,     // application-defined structure (price = uint256; inference output = arbitrary bytes)
    source_ts:  uint64,    // source-chain timestamp, used for freshness checks
    round_id:   uint64     // monotonically increasing; consumers distinguish "new vs. old"
)
```

**Recommended: use `send_stream`** instead of plain `send`, to guarantee consumers observe values in `round_id` order. `stream_id = keccak256("oracle:" ‖ feed_id)`.

**Invariants:** consumers MUST reject messages with `round_id ≤ last_round_id` (prevent old-value replay); `block.timestamp - source_ts ≤ feed_ttl` (prevent stale consumption — corresponds to §1.6 "Stale commitment consumed").

**Typical uses:** Weather oracle on Cowboy → parametric-insurance contract on Ethereum; Cowboy LLM inference output → on-chain decision on Solana.

### 3.4 Generic Cross-Chain Call

The most general L3 application. A `callCrossChain(dst_chain, target, calldata, value)` primitive, implemented as an L2 send where the destination-chain recipient is a dispatcher that calls the target with the supplied calldata.

**Payload schema:**
```
abi.encode(
    target:     Address,   // target contract on destination
    value:      uint256,   // attached asset (sender must escrow equivalent wrapped asset on source)
    gas_limit:  uint64,    // upper bound for target call, prevents exhausting relayer gas
    calldata:   bytes      // ABI-encoded function call passed to target
)
```

**Dispatcher contract logic:**
```solidity
function onCrossChainMessage(ChainId src, Address sender, bytes payload) {
    (target, value, gas_limit, data) = abi.decode(payload, (Address, uint256, uint64, bytes));
    (bool ok, bytes memory ret) = target.call{value: value, gas: gas_limit}(data);
    if (!ok) emit CallFailed(src, sender, ret);  // do NOT revert; L2's `consumed` stays set
}
```

**Failure handling:** if `target.call` reverts, the dispatcher **does not roll back the L2 `consumed` mark** (to prevent infinite-retry gas consumption). The application layer must handle idempotency and failure compensation inside the target contract. Value transfer requires coordination with an asset bridge (§3.1) to convert `value` into wrapped assets on the destination.

**Use cases:** cross-chain DApp deployment, cross-chain governance voting, cross-chain user identity (sign once, effective on all chains), etc.

---

## Technical Feasibility

This section demonstrates that the three-layer architecture is not a paper plan. Each subsection attacks one feasibility question with concrete evidence: interface closure, compositional soundness, known-buildable components, and performance within the envelope of existing cross-chain systems.

### A.1 Interface Contract

The three interfaces are complete — every type referenced is defined in this CIP. Swapping any one layer's implementation affects only its own interface surface.

**L1 — destination-chain anchor interface:**

```
type ChainId      = u64
type BlockHeight  = u64
type Root         = bytes32
type MerkleIndex  = u32

struct BlockCommitment {
    tx_root:      Root
    receipt_root: Root
    state_root:   Root     // optional; 0x00..0 if unused by this backend
    parent_hash:  bytes32  // optional; 0x00..0 if unused
    finalized_at: u64      // destination-chain timestamp of finalization
}

interface IChainAnchor {
    // True iff the commitment for (chain, height) has met the backend's
    // finalization rule (e.g., 2-of-3 sigs landed, ZK proof verified).
    is_finalized(chain: ChainId, h: BlockHeight) -> bool

    // Returns the finalized commitment; reverts if !is_finalized.
    commitment(chain: ChainId, h: BlockHeight) -> BlockCommitment

    // True iff `leaf` is a member of the Merkle tree whose root equals
    // `root` at (chain, height). Pure function of (root, leaf, proof).
    verify_inclusion(
        chain:    ChainId,
        h:        BlockHeight,
        root:     Root,
        leaf:     bytes32,
        siblings: bytes32[],
        index:    MerkleIndex
    ) -> bool
}
```

**L2 — mailbox interface (symmetric on both chains):**

```
type Address = bytes20
type Nonce   = u64

struct CrossChainMessage {
    src_chain: ChainId
    dst_chain: ChainId
    sender:    Address
    recipient: Address
    nonce:     Nonce
    payload:   bytes
    gas_limit: u64
}

interface ICrossChainMailbox {
    // Source-side: emit a message event. Returns the assigned nonce
    // (strictly monotonic per (sender, dst_chain)).
    send(dst: ChainId, recipient: Address, payload: bytes, gas_limit: u64)
        -> Nonce

    // Destination-side: verify the L1 proof, enforce replay protection,
    // and dispatch to the recipient. Reverts on any check failure.
    deliver(
        msg:        CrossChainMessage,
        msg_height: BlockHeight,    // source-chain block containing send()
        proof:      bytes32[],
        index:      MerkleIndex
    )
}
```

**L3 recipient interface (contract an application must implement to receive messages):**

```
interface IRecipient {
    // Invoked by Mailbox.deliver after all checks pass.
    // The mailbox has already deduped against (src, sender, nonce);
    // a well-behaved recipient tolerates re-entry defensively but
    // does not need its own replay protection at this layer.
    on_cross_chain_message(
        src:     ChainId,
        sender:  Address,
        payload: bytes
    )
}
```

**Interface closure check.** Every non-primitive type on any arrow — `BlockCommitment`, `CrossChainMessage`, `Root`, `ChainId`, `Address`, `Nonce` — is defined above. No dangling references. L3's only dependency on L1 is via L2, with one exception: gas-critical paths (§3.4) may call `IChainAnchor.verify_inclusion` directly to save the extra deliver-hop overhead; this is an intentional escape hatch and does not break layering.

### A.2 Worked Example — Asset Bridge (Bidirectional)

Establishes the base composition: L1 + L2 + L3, forward and reverse.

**Participants:**
- `AssetVault` (Cowboy actor) — locks CBY, mints IOUs
- `Mailbox_C` (Cowboy) — L2 source-side on Cowboy; `Mailbox_E` (Ethereum) — L2 destination-side on Ethereum; both conform to `ICrossChainMailbox`
- `Anchor_E` (Ethereum) — L1 `IChainAnchor` on Ethereum, attesting Cowboy commitments
- `AssetMint` (Ethereum) — L3 recipient, mints wCBY

**Forward flow (Cowboy → Ethereum):**

```
1. user                   -> AssetVault:  deposit(amount=100, dest=eth_addr)
2. AssetVault                             lock 100 CBY from user
3. AssetVault             -> Mailbox_C:   send(dst=ETH, recipient=AssetMint,
                                               payload=abi.encode(eth_addr, CBY_TOKEN_ID, 100),
                                               gas_limit=200k)
                                          -> returns nonce N
4. Cowboy chain                           finalize block @height H containing (3)
5. Runner(s)              -> Anchor_E:    publish BlockCommitment(COWBOY, H, ...)
                                          (per chosen backend; e.g., 2-of-3 ECDSA)
6. Anchor_E                               is_finalized(COWBOY, H) = true
7. relayer (permissionless) -> Mailbox_E: deliver(msg, H, proof, index)
   7a. Mailbox_E          -> Anchor_E:    verify_inclusion(COWBOY, H,
                                              commitment(COWBOY,H).tx_root,
                                              hash(msg), proof, index) = true
   7b. Mailbox_E                          consumed[(COWBOY, AssetVault, N)] := true
   7c. Mailbox_E          -> AssetMint:   on_cross_chain_message(
                                              COWBOY, AssetVault,
                                              abi.encode(eth_addr, CBY_TOKEN_ID, 100))
8. AssetMint                              decode payload; mint 100 wCBY to eth_addr
```

**Type flow at each boundary:**

| Step | Caller | Callee | Types crossing |
|---|---|---|---|
| 3 | AssetVault (L3) | Mailbox_C (L2) | `bytes` payload — opaque to L2 |
| 5 | Runner (L1 backend) | Anchor_E (L1) | `BlockCommitment` |
| 7a | Mailbox_E (L2) | Anchor_E (L1) | `Root`, `bytes32` leaf, `bytes32[]` siblings, `MerkleIndex` |
| 7c | Mailbox_E (L2) | AssetMint (L3) | `(ChainId, Address, bytes)` — payload unchanged from (3) |

No boundary requires a conversion step. Payload bytes pass through L2 untouched (integrity via keccak in L1 inclusion proof); L1 never sees application semantics.

**Reverse flow (Ethereum → Cowboy):** structurally identical, with `Mailbox_C` as deliver-side and `Anchor_C` (a Cowboy actor anchoring Ethereum commitments) in the L1 role. Requires a runner backend that watches Ethereum (constructible; see A.5).

### A.3 Worked Example — Cross-Chain Push Oracle

Validates the streaming / multi-message pattern.

**Participants:**
- `PriceFeedActor` (Cowboy) — publishes prices via Watchtower (CIP-7) AND via L2
- `Consumer` (Ethereum) — L3 recipient caching the latest price
- `Mailbox_C`, `Mailbox_E`, `Anchor_E` — as in A.2

**Flow (per price update):**

```
1. PriceFeedActor                         compute price @timestamp T
2. PriceFeedActor       -> Watchtower_C:  append(price, T)       // intra-Cowboy subs
3. PriceFeedActor       -> Mailbox_C:     send_stream(
                                              stream_id = keccak256("oracle:"||FEED_ID),
                                              payload   = abi.encode(FEED_ID, price, T, k),
                                              gas_limit = 60k)
                                          -> returns seq k (k-th price, per-stream monotonic)
4. ...anchor + relayer as in A.2 steps 4–7a...
5. Mailbox_E            -> Consumer:      on_cross_chain_message(COWBOY,
                                              PriceFeedActor,
                                              abi.encode(FEED_ID, price_k, T_k, k))
6. Consumer                               if k > last_round_id, record (price_k, T_k);
                                          otherwise revert (prevents old-value replay)
```

**Architecture properties tested:**
- Multi-message throughput: k is strictly increasing; L2 enforces per-sender-monotonic nonce ordering (invariant **L2-Ordering**, §B.2), so out-of-order proof submissions revert and the Consumer sees prices in publish order.
- Watchtower and L2 are orthogonal: intra-chain subscribers get real-time delivery via step 2; cross-chain subscribers get eventual delivery via step 3–5. No shared state, no cross-dependency.
- No backpressure mechanism — if relayers fall behind, prices queue on Ethereum via L2; Consumer can apply its own staleness policy.

### A.4 Worked Example — Cross-Chain Lending

Validates bidirectional L2 + economic invariant spanning both chains.

**Participants:**
- `Collateral` (Ethereum) — holds ETH deposits
- `Debt` (Cowboy) — mints stablecoin debt against attested collateral
- Mailboxes and anchors in both directions

**Flow (deposit, borrow, liquidate):**

```
Deposit:
 1. user -> Collateral:      deposit(1 ETH)
 2. Collateral -> Mailbox_E: send(COWBOY, Debt,
                                  abi.encode(action=1 (deposit), borrower=user,
                                             collateral_token=ETH_TOKEN_ID, amount=1e18))
 3. ...L1 anchor ETH block on Cowboy; relayer deliver...
 4. Mailbox_C -> Debt:       on_cross_chain_message(ETH, Collateral,
                                                    abi.encode(action=1, user, ETH_TOKEN_ID, 1e18))
 5. Debt:                    credit user with 1 ETH of collateral
                             (uses local price oracle for USD value)

Borrow (local to Cowboy):
 6. user -> Debt:            borrow(1000 USDC)        // under CR constraint

Liquidate (cross-chain):
 7. liquidator -> Debt:      liquidate(user)
                             // Debt burns user's borrowed USDC,
                             // marks 1 ETH of collateral for seizure
 8. Debt -> Mailbox_C:       send(ETH, Collateral,
                                  abi.encode(action=4 (seize), borrower=user,
                                             liquidator=liquidator_addr, amount=1e18))
 9. ...L1 anchor Cowboy block on Ethereum; relayer deliver...
10. Mailbox_E -> Collateral: on_cross_chain_message(COWBOY, Debt,
                                                    abi.encode(action=4, user, liquidator, 1e18))
11. Collateral:              transfer 1 ETH to liquidator
```

**Architecture properties tested:**
- Bidirectional L2 — one application uses both directions simultaneously.
- Economic invariant "debt ≤ collateral value" spans both chains, enforced at application layer (step 5 credit before step 6 borrow; step 7 burn-then-seize order). The invariant is **eventually** consistent, not atomic — an L3 concern handled by over-collateralization + liquidation delay, NOT an L2 or L1 concern.
- L2's per-sender-monotonic nonce prevents a liquidator from observing two conflicting seize messages in wrong order.

**What the architecture does NOT provide:**
- Atomic cross-chain transactions. Steps 7–11 are not atomic; between them, the user's collateral is "in limbo." Mitigations are application-level (time-bounded liquidation auction, circuit-breakers).
- Cross-chain reentry guards. The Debt contract must not call Mailbox during a critical section.

Both caveats are documented in the dedicated cross-chain lending CIP, not in CIP-25.

### A.5 Implementation Feasibility

Every component needed for the full architecture has a concrete construction path. No component is blocked by missing primitives or research.

| Component | Construction path |
|---|---|
| L1 committee backend (either direction) | Runners selected by VRF fetch the source block's `(tx_root, receipt_root)`, sign a domain-separated message `keccak256("…v1" ‖ src_chain_id ‖ dst_anchor ‖ H ‖ roots)`, and the destination contract recovers signers one by one until threshold. All primitives are standard cryptography + contract storage; no new research. |
| L1 ZK backend (ETH → Cowboy) | Active research area (Succinct / RISC Zero / Polyhedra Ethereum header proofs). Verifier deploys on Cowboy or Solidity side and plugs into the same `IChainAnchor` interface. |
| L1 ZK backend (Cowboy → ETH) | Requires producing a proof of Cowboy consensus; simpler than Ethereum because Cowboy has deterministic finality. Custom zk-circuit. |
| L1 optimistic backend | Standard optimistic pattern (fraud window, bond, challenge). Fits `IChainAnchor` with a `finalized_at` delay matching the window. Challenger-incentive design is its own CIP. |
| L1 native light client (ETH → Cowboy) | Cowboy requires BLS12-381 pairing precompiles to verify Ethereum's sync-committee signatures natively. Blocking item is outside CIP-25's scope and belongs to Cowboy VM evolution. |
| L2 Mailbox (Cowboy source) | Cowboy actor: `send(dst, recipient, payload, gas)` assigns a nonce and emits a canonical event; internal state `nonce_of[(sender, dst_chain)]`. |
| L2 Mailbox (EVM destination) | Solidity contract: verifies inclusion proof via `IChainAnchor`, writes `consumed[(src, sender, nonce)]`, invokes recipient. Reference construction is structurally similar to LayerZero Endpoint / Axelar Gateway, but follows this CIP's §2.1 interface. |
| L3 asset bridge | Minimal L3 on top of L2 Mailbox: application implements `IRecipient.on_cross_chain_message` and mints/releases per payload's `(recipient, amount)`. Lock-mint or burn-release (see §3.1). |
| L3 cross-chain lending | CIP-20 tokens + L2 Mailbox bidirectional flow. Depends on a local price oracle, orthogonal to L2. Full mechanism belongs to a dedicated L3 CIP. |
| L3 oracle / streaming | CIP-7 Watchtower producer + explicit verified submission to `send_stream` (§2.6); no automatic CBQS-to-consensus trigger. Downstream consumers subscribe by `stream_id`. |
| L3 generic cross-chain call | Dispatcher contract on destination: decodes `(target, calldata, value)` from payload, performs `target.call{value}(calldata)` under a gas limit. ~50 LOC Solidity. |
| Proof generation service | Cowboy Runner job type (symmetric to the committee-attestation job): takes `(chain, height, leaf_key)` and returns `(siblings, index)`. Billed in CBY through protocol-fee revenue share. |
| Committee slashing | The consensus verifier outputs a dissenters list on disagreement; the stake module consumes this list to execute stake burn (see §1.6). |
| BLS / threshold ECDSA signature aggregation | Reduces per-message L1 cost; standard cryptographic construction. Requires pairing precompile (BLS) or `secp256k1_recover` support (threshold ECDSA) on the destination chain. |

**No 🔴 Blocked entries.** Every row has a concrete implementation path not requiring unsolved research.

### A.6 Performance Envelope

Orders of magnitude, not SLAs. Numbers reflect an EVM destination (Anvil / Ethereum L1 / Ethereum L2); Solana numbers differ and are out of scope for v1.

**Latency per message:**

| Component | Latency | Notes |
|---|---|---|
| Source-chain finality (Cowboy) | ~1 block | Deterministic finality |
| Source-chain finality (Ethereum) | 12–32 slots (2.5–6.5 min) | Backend-specific policy |
| L1 attestation (committee, 2-of-3) | ~2 destination blocks | 2 signatures + 1 block to finalize |
| L1 attestation (ZK) | proof time + 1 destination block | Proof time highly variable (seconds to minutes) |
| L2 delivery | 1 destination block | Single proof-check + dispatch tx |
| L3 recipient execution | application-dependent | |
| **Total (committee backend)** | **~3 dst blocks** above source-finality | **~15 s** local Anvil, ~40 s Ethereum L1 |

**Per-message gas cost on EVM destination:**

| Operation | Gas |
|---|---|
| L1 `submitSignature` per runner | ~50–60k |
| L1 finalization (on 2nd sig) | +~20k |
| L2 `deliver` (proof-verify + replay mark + recipient call) | ~120–180k |
| L3 recipient (asset mint, oracle update, etc.) | application-dependent |
| **Total per message (2 signatures + 1 deliver)** | **~200–300k gas** on EVM |

Comparable: LayerZero ~200–300k, Axelar ~250k, Wormhole ~200k. The architecture is not off-profile.

**Throughput:**
- Per (source, destination) pair: bottlenecked by destination block rate and anchor-submission cost. On Ethereum L1 at 12 s blocks + 300k gas/message, a single dedicated anchor occupies ~1% of a block's gas, sustaining ~25 msg/min.
- Aggregation (BLS / threshold ECDSA) amortizes signatures across many messages: ~1 signature per batch of N. Per-message cost drops ~60–70% at N ≥ 10.

**Storage footprint on destination:**
- Per commitment: ~100 bytes (`tx_root + receipt_root + state_root + parent_hash + finalized_at`).
- Per delivered message: 1 storage slot (consumed bit) per `(src_chain, sender, dst_chain, nonce)`. Grows monotonically; no pruning in v1.
- Runner registry: 3 addresses × ~20 bytes = hard-coded at deploy for minimal committee backend; a dynamic registry adds one SSTORE per rotation.

Growth is linear in traffic. No architectural throughput cliff.

---

## Security Model

This section provides a rigorous treatment: per-layer adversary model, layer invariants in semi-formal pre/postcondition form, a composition theorem linking them, a taxonomy of 15 known cross-chain attacks with architectural defenses, and defense-in-depth patterns for high-value deployments.

### B.1 Adversary Model per Layer

**L1 adversary**

- **Capabilities:** can post arbitrary transactions to source and destination chains; can run up to the backend-specific threshold of participants (for committee backend, up to `n - t - 1` of `n` runners; for ZK backend, zero participants influence soundness; for optimistic backend, all provers EXCEPT at least one honest challenger within the challenge window); can observe all public state; can attempt economic bribery.
- **Protected assets:** authenticity of `BlockCommitment` anchored on destination; correctness of `verify_inclusion` (no false positives).
- **Out of scope:** destination-chain consensus compromise; cryptographic primitive breaks (keccak, ECDSA, BLS, Blake3); soundness break of the specific ZK proof system in use (a cryptographic assumption inherited from the backend choice, not an architectural flaw).

**L2 adversary**

- **Capabilities:** L1 adversary plus: submits arbitrary `deliver` calls with valid or invalid proofs; withholds messages (griefing); selects ordering of their own submissions.
- **Protected assets:** exactly-once delivery per `(src_chain, sender, dst_chain, nonce)`; per-sender-monotonic ordering; payload integrity end-to-end; cross-deployment isolation.
- **Out of scope:** liveness SLA (architecture does not guarantee delivery within N blocks); universal censorship (all relayers simultaneously colluding — requires out-of-band solution such as Cowboy Runner committees paying destination gas from a pool).

**L3 adversary**

- **Capabilities:** L2 adversary plus: crafts arbitrary application-level inputs; exploits recipient-contract bugs; manipulates economic parameters within application rules.
- **Protected assets:** application-specific (bridge conservation, lending solvency, oracle correctness).
- **Out of scope:** recipient-contract bugs; misconfigured application parameters (collateral ratios, liquidation incentives) — these belong to per-application CIPs.

### B.2 Layer Invariants

Each layer's guarantees stated as `invariant { ... } assuming { ... }`. Invariants are postconditions the layer exposes to layers above it; assumptions are preconditions the layer needs from the layer below or from cryptographic primitives.

**L1 invariants.**

```
invariant L1-Authenticity:
    for every (chain, height) where IChainAnchor.is_finalized == true,
    IChainAnchor.commitment(chain, height) is byte-identical to the
    canonical block commitment produced by the source chain's consensus
    at that height.

assuming backend = RunnerCommittee(n, t):  at most (t-1) runners collude
assuming backend = ZKLightClient:          proof-system soundness
assuming backend = Optimistic(w):          ≥1 honest challenger within w
assuming backend = NativeLightClient:      source consensus is honest
assuming:                                  keccak256 + ECDSA/BLS soundness
```

```
invariant L1-Inclusion:
    IChainAnchor.verify_inclusion(chain, h, root, leaf, siblings, index)
    returns true iff there exists a sibling sequence producing `root`
    from `leaf` under the canonical Merkle construction (BMT / MPT as
    specified by the source chain).

assuming:  keccak256 collision resistance
           canonical Merkle construction matches the source chain's
```

```
invariant L1-Monotonic-Finality:
    once is_finalized(chain, h) == true, subsequent calls with identical
    (chain, h) also return true. Finalized commitments are immutable.

assuming:  destination chain operators do not roll back their own state
           backend does not expose a "rollback" operation
```

**L2 invariants.**

```
invariant L2-Authenticity:
    if Mailbox.deliver(msg, h, proof, index) succeeds, then there exists
    a source-chain transaction at block h that invoked Mailbox.send with
    parameters producing msg.

assuming:  L1-Authenticity  (commitment truly reflects source block)
           L1-Inclusion     (Merkle proof verification is sound)
```

```
invariant L2-Exactly-Once:
    for any (src_chain, sender, dst_chain, nonce), at most one Mailbox.deliver
    invocation returns success.

assuming:  consumed[(src_chain, sender, dst_chain, nonce)] is a write-once flag
           L1-Monotonic-Finality  (finalized commits don't disappear)
```

```
invariant L2-Ordering:
    for a given (sender, dst_chain), the source-side Mailbox assigns
    strictly increasing nonces. Therefore deliver calls that produce
    application state changes progress in nonce order; out-of-order
    delivery is permitted by the protocol but applications observing
    per-sender-monotonic ordering MAY enforce it via their own
    recipient logic.

assuming:  source-side Mailbox nonce counter is atomic under concurrency
```

```
invariant L2-Integrity:
    payload passed to IRecipient.on_cross_chain_message(src, sender, p)
    is byte-identical to payload passed to the corresponding source
    Mailbox.send(..., payload=p, ...).

assuming:  L1-Inclusion
           leaf = keccak256(canonical_encode(msg))
           canonical_encode is deterministic and unambiguous
```

```
invariant L2-Deployment-Isolation:
    a message emitted for (src, dst_mailbox_addr) cannot be delivered
    against a mailbox with a different address on the same dst chain.

assuming:  source-side send() binds dst_mailbox_addr into the message hash,
           OR the destination mailbox re-derives its own address into the
           verification equation (e.g., by including address(this) in the
           signed message)
```

**L3 invariants are application-specific.** Example for an asset bridge:

```
invariant L3-AssetBridge-Conservation:
    sum(wCBY minted on destination) ≤ sum(CBY locked on source).

assuming:  L2-Authenticity  (only real locks trigger mints)
           L2-Exactly-Once  (no double-mint)
           L2-Integrity     (mint amount matches lock amount)
           lock and mint logic are correctly paired in contracts
```

### B.3 Composition Theorem

**Theorem (informal).** Under the assumptions of the chosen L1 backend (B.1) plus standard cryptographic assumptions, L2 provides authenticated, exactly-once, integrity-preserving delivery of typed messages. Therefore any L3 application whose security reduces to a conjunction of L2 invariants is secure under the same assumptions.

**Proof sketch.**

1. L1-Authenticity + L1-Inclusion ⇒ an accepted proof for `leaf = hash(msg)` implies `msg` was emitted on the source chain. This gives **L2-Authenticity**.
2. L2-Authenticity + L1-Monotonic-Finality + write-once `consumed` map ⇒ **L2-Exactly-Once**. Finalized commits don't disappear; the consumed map rejects second attempts.
3. Monotonic source-side nonces + L2-Exactly-Once ⇒ **L2-Ordering** (any sequence of successful deliveries observes per-(sender, dst_chain) monotonic nonces; applications may enforce contiguity).
4. L1-Inclusion + deterministic canonical encoding ⇒ **L2-Integrity** (the payload on the destination is byte-identical to the payload at the source).
5. For L3: any application whose invariant I can be written as

   ```
   I = Conj_k(Pre_k ⇒ L2-Invariant_k)
   ```

   holds by simple implication from the L2 invariants established in (1)–(4).

**Corollary (backend-swap safety).** Every L3 invariant bottoms out in `L1-Authenticity` and `L1-Inclusion`, which are **backend-agnostic predicates**. Swapping the L1 trust backend (committee → ZK, ZK → optimistic, etc.) does NOT invalidate any L2 or L3 proof, provided the new backend satisfies its own `assuming` clauses in B.2.

### B.4 Attack Taxonomy

Fifteen cross-chain attacks, organized into five categories. For each: description, architectural defense, and pointer to where the complete defense mechanism is specified in this CIP.

#### B.4.1 Trust-model attacks

| Attack | Defense | Solution reference |
|---|---|---|
| **Committee collusion** (t runners coordinate to sign a false commitment) | Signed message is domain-separated and bound to destination contract address; collusion requires t+ key compromises AND matching signatures. Economic bonding raises attacker cost. | §1.6 "Runner majority colludes…" (economic floor + automatic slashing + fraud-proof window + dual-backend defense-in-depth) |
| **ZK soundness break** (prover produces a false witness) | Backend-layer assumption; mitigated architecturally only by defense-in-depth. | §B.6 pattern 1 (dual-backend anchor). Dependent on chosen proof system; not addressable at L2/L3. |
| **Optimistic insufficient challenge window** (challenger misses window) | `BlockCommitment.finalized_at` exposes destination-chain timestamp; L2 can enforce `now ≥ finalized_at + window` before acting. | §1.4 (backend definition); challenger-incentive design belongs to its own CIP. |

#### B.4.2 Source-chain state attacks

| Attack | Defense | Solution reference |
|---|---|---|
| **Source chain reorg** (attested block disappears from source) | Cowboy source: deterministic finality. Ethereum source: backend parameter `min_confirmations`. | §1.6 "Commitment published for a re-orged source block" (Cowboy finality check + ETH `min_confirmations` + revocation primitive + runner monitoring) |
| **Stale commitment consumption** (L3 acts on a very old commitment) | L1 exposes `finalized_at`; L2 **does not** enforce TTL (by design). L3 applications MUST enforce their own TTL on `finalized_at`. | §1.6 "Stale commitment consumed" (`finalized_at` exposure + app TTL + timeout-and-cancel + GC) |
| **Delay-of-finality** (adversarial runner withholds signature to stall finalization) | 2-of-3 threshold tolerates one offline runner. VRF selection on a per-height basis rotates the committee. | §1.6 "Runner offline causing finalization delay" (liveness timeout + reputation ledger + probation + rescue bonus) |

#### B.4.3 Messaging-layer attacks

| Attack | Defense | Solution reference |
|---|---|---|
| **Within-deployment replay** (resubmit same (sender, nonce) twice) | Write-once `consumed[(src, sender, nonce)]` map. Second deliver reverts. | §2.1 / §2.3 (mailbox core; L2-native). |
| **Cross-deployment replay** (replay on a sibling L2 mailbox on same destination chain) | Signed commitment binds `address(this)` of destination contract (L2-Deployment-Isolation). | §B.2 L2-Deployment-Isolation invariant (enforced by mailbox implementations). |
| **Signature malleability** (tweak `(r, s, v)` to alter recovered signer) | Use a standard ECDSA library that rejects high-s (EIP-2). | §1.5 interface definition (ECDSA verification semantics of `IChainAnchor`). |
| **Nonce skip** (deliver msg with nonce N+10 to lock N+1..N+9 out) | Per-sender-monotonic nonces are ASSIGNED by source-side mailbox, not chosen by the user. An attacker cannot inject a skipped nonce because no corresponding `send` ever occurred — the L1-inclusion proof would fail. | §2.1 (nonce atomically assigned by mailbox; not an attack surface). |

#### B.4.4 Delivery-layer attacks

| Attack | Defense | Solution reference |
|---|---|---|
| **Delivery griefing** (relayer refuses to submit proof) | Delivery is permissionless; anyone with the proof can submit. Third-party relayers compete for any fee incentive. | §2.6 "Gas-sponsorship model" (source-side prepayment + protocol split + runner-managed default path + third-party arbitrage fallback) |
| **Front-running claim** (MEV searcher beats user to claim) | Recipient address is baked into message at source; front-runner cannot redirect funds, only pay gas to be "first." | §2.1 message structure (recipient bound at source, non-mutable). |
| **Cross-sender ordering race** (relayer chooses which of two pending messages to deliver first) | L2 does NOT enforce cross-sender total ordering by default; applications that need it use the `send_stream` primitive. | §2.6 "Cross-sender total ordering" (StreamID primitive + destination contiguity enforcement + serializer-chain delegation) |
| **Gas grief on delivery** (submit deliver with insufficient gas to succeed recipient call) | Deliver reverts atomically if recipient call fails; `consumed` map is NOT updated on revert, so retry is possible with sufficient gas. | §2.3 (atomic delivery semantics, enforced by mailbox). |

#### B.4.5 Application-layer attacks

| Attack | Defense | Solution reference |
|---|---|---|
| **Oracle / price-feed staleness** (consumer contracts act on old price) | L1 `finalized_at` timestamp available to L3; consumer enforces TTL. | §1.6 "Stale commitment consumed" + §2.6 "Timeout / cancellation" (expiry parameter + hard cutoff + L3 `on_timeout` hook). |
| **Liquidity drain on bridge** (exploit to withdraw more than deposited) | L3 conservation invariant: mint ≤ lock, enforced by pairing send/receive contracts. L2's exactly-once prevents double-spend. | §B.2 L3-AssetBridge-Conservation invariant + §B.6 pattern 3 (rate-limited release). |
| **Stale sender key** (source-chain key rotated; in-flight msg delivered under old identity) | The signed commitment is over the block at submission time; key rotation takes effect at block height N+1. Messages in flight use keys valid at the emission block. | §1.3 BlockCommitment (identity bound per block height); full key-rotation protocol is a follow-up CIP. |

### B.5 Defense Index

Each attack in B.4 has a corresponding mechanism defined in this CIP. The table below maps "attack → mechanism location," confirming no attack is described without a mechanism to back it up.

| Attack class | Mechanism location |
|---|---|
| B.4.1 committee collusion | §1.6 four defenses (economic floor + automatic slashing + fraud-proof window + dual-backend) + §B.6 pattern 1 |
| B.4.1 ZK soundness | §B.6 pattern 1 (dual-backend anchor) — architecture can only offer defense-in-depth; soundness itself is a backend assumption |
| B.4.1 optimistic challenge window | §1.4 (backend definition) + §1.5 (`finalized_at` window check); challenger incentives specified in a dedicated CIP |
| B.4.2 source reorg | §1.6 "Commitment published for a re-orged source block" (Cowboy finality check + ETH `min_confirmations` + revocation primitive + runner monitoring) |
| B.4.2 stale commitment | §1.6 "Stale commitment consumed" + §2.6 "Timeout / cancellation" |
| B.4.2 delay-of-finality | §1.6 "Runner offline causing finalization delay" (liveness timeout + reputation ledger + probation + rescue bonus) |
| B.4.3 within-deployment replay | §2.1 / §2.3 (mailbox core) |
| B.4.3 cross-deployment replay | §B.2 L2-Deployment-Isolation invariant |
| B.4.3 signature malleability | §1.5 (ECDSA verification semantics) |
| B.4.3 nonce skip | §2.1 (atomic nonce assignment by mailbox; not an attack surface) |
| B.4.4 delivery griefing | §2.6 "Gas-sponsorship model" (source-side prepayment + split + runner-managed + third-party fallback) |
| B.4.4 front-running claim | §2.1 (recipient bound into message, non-mutable) |
| B.4.4 cross-sender ordering | §2.6 "Cross-sender total ordering" (StreamID + serializer chain) |
| B.4.4 gas grief on delivery | §2.3 (atomic delivery semantics) |
| B.4.5 oracle staleness | §1.6 "Stale commitment consumed" + §2.6 "Timeout / cancellation" (`expiry` + hard cutoff + `on_timeout` hook) |
| B.4.5 liquidity drain | §B.2 L3-AssetBridge-Conservation invariant + §B.6 pattern 3 (rate-limited release) |
| B.4.5 key rotation | §1.3 (identity bound per block height); full key-rotation protocol is a dedicated CIP |

### B.6 Defense-in-Depth Patterns

High-value applications SHOULD adopt at least one:

**1. Dual-backend anchor.** Deploy two independent `IChainAnchor` instances (e.g., one committee-backed, one ZK-backed). L3 requires both to agree on the commitment before releasing funds. Cost: 2× attestation gas, 2× verify gas. Benefit: attacker must break both backends simultaneously — economically and cryptographically uncorrelated.

**2. Timeout-with-cancel.** Recipient rejects messages whose `finalized_at` exceeds a TTL. Source-side sender can reclaim escrow after the same TTL if delivery never succeeded. Limits damage from delayed delivery and disincentivizes relayer stalling. Requires coordination between source-side and destination-side contracts.

**3. Rate-limited release.** Bridge minting or lending liquidation caps per-epoch volume (e.g., 100 ETH/day per user, 1000 ETH/day globally). Governance adjustable. Caps the loss ceiling under any single exploit to `cap × response_time`. Orthogonal to all L1/L2 mechanisms.

**Recommendation:** applications with TVL above a governance-defined threshold (initial proposal: $1M USD equivalent) MUST adopt at least one pattern. Below the threshold, patterns are RECOMMENDED.

---

## Related CIPs

- **CIP-2** (Off-chain Compute / Runners): source of attestation signers for the runner-committee L1 backend.
- **CIP-4** (State & Merkle Proofs): provides the commitment primitives that L1 publishes cross-chain.
- **CIP-5** (Timers): enables keeper-free L1 anchoring for runner-attested and optimistic backends.
- **CIP-7** (Watchtower): the intra-chain streaming primitive that, extended through L2's `send_stream` (§2.6), yields cross-chain streaming.
- **CIP-20** (Fungible Tokens): the L3 asset-bridge reference implementation will target CIP-20 token semantics.
- **CIP-21** (Liquidity Pools): enables the "swap-and-bridge" L3 pattern.

---

## Appendix A — Worked Example: Single-Slice L3 Bridge Without L2

As an illustration of the architecture's composition flexibility, consider a unidirectional L3 asset bridge that binds directly to a committee-backed L1 and inlines its L2 responsibilities. The diagram below shows the component mapping, and marks where the seam would appear if L2 were later extracted into a reusable mailbox.

```
L3 AssetBridge.claim  (destination contract)         ← application: asset withdrawal
    │
    │  reads tx/receipt proof, decodes event,
    │  checks chain_id + replay + actor identity
    │
    ▼
L1 Anchor.{isFinalized, commitment}                  ← anchor API (per §1.5)
    InclusionVerifier.verify                          ← inclusion proof verifier
    │
    │  commitment produced by 2-of-3 ECDSA
    │  over keccak256("Anchor.v1" || …)
    │
    ▼
Runner committee  (off-chain, selected via VRF)      ← L1 trust backend
```

In this pattern the application binds **directly** to L1; there is no L2 mailbox in the path. The destination-side asset bridge inlines event decoding and replay protection against L1 proofs. When L2 is introduced, the inlined logic is extracted into a reusable `CrossChainMailbox` component, and `AssetBridge` becomes a thinner L3 recipient whose only responsibility is the asset-release semantic — corresponding to §2.1's `IRecipient.on_cross_chain_message` interface.

---

## Appendix B — Notation

**Base types (§1.3 / §A.1):**

- **Chain ID.** A canonical `u64` per chain (Cowboy's own chain id for the Cowboy network; EIP-155 chain id for EVM chains; cluster genesis hash hash-slice for Solana-style chains).
- **Block commitment.** Per §1.3.
- **Merkle proof.** `(root, leaf, siblings[], index)`. The BMT form (`keccak256` with parity-based left/right) is Cowboy's canonical; other hashes may be specified per L1 backend.
- **Finality.** "Cowboy-finalized" means passed consensus on Cowboy (deterministic). "Destination-finalized" means recognized by the anchor contract on the destination chain.

**L1 parameters and primitives (§1.4 / §1.6):**

- **`finalized_at`.** Field on `BlockCommitment`; destination-chain-view timestamp when the commitment was finalized (u64 seconds). L2/L3 use it for freshness checks.
- **`min_confirmations`.** Backend parameter for probabilistic-finality source chains; runners wait until `current - H ≥ min_confirmations` before signing. Ethereum default 32 slots, up to 64 for high-TVL deployments.
- **`commitment_revoked(chain, height)`.** Message co-signed by a threshold of runners on source-chain deep reorg. Destination-side mailbox checks the revocation set in `deliver` and reverts on hit.
- **fraud-proof window.** 7-day challenge window after a commitment lands on destination; anyone may submit inconsistency evidence to trigger slashing.

**L2 parameters and primitives (§2.1 / §2.6):**

- **`send(dst, recipient, payload, gas_limit, expiry, fee) → nonce`.** Source-side Mailbox entry; `expiry` is a destination-chain timestamp; `fee` is CBY prepaid.
- **`deliver(msg, proof)`.** Destination-side Mailbox entry. Enforcement: `block.timestamp ≤ msg.expiry` hard cutoff.
- **`reclaim_fee(msg_hash)` / `bump_fee(msg_hash, extra)`.** Source-side operations to recover or top up the bounty for messages not yet delivered.
- **`reclaim(msg_hash)`.** Source-side retrieval of escrowed assets for messages not delivered by `expiry + GRACE`.
- **`GRACE`.** Source-side reclaim grace period, default 1h.
- **`APP_TTL` / `MAX_TIMEOUT`.** Application-level message freshness (app-defined) and protocol-level bounty-recovery window (default 7 days) respectively.
- **`on_timeout(msg_hash)`.** Callback hook invoked on the L3 application when the sender reclaims, used to unwind local escrow state.
- **`send_stream(stream_id, payload, gas_limit) → seq`.** L2 primitive for cross-sender total ordering. `stream_id = keccak256(dst_chain ‖ recipient ‖ topic)`; source Mailbox maintains atomic `next_stream_seq` per `stream_id`.
- **`deliver_stream(msg, proof)`.** Destination-side contiguity-enforcing delivery by `stream_id`; out-of-order reverts.
- **`Delivered(msg_hash)` event.** Emitted on successful destination `deliver`; consumed as input to reverse L1 commitments for source-side fee settlement and concurrency safety.

**L3 parameters and roles:**

- **`APP_TTL` recommended values (unifies §1.6 and §2.6):** asset bridge 24h, oracle push 5 min, lending 15 min, one-shot RPC call 2 min.
- **Dispatcher contract.** Destination-side recipient for generic cross-chain call (§3.4).
- **Relayer.** Submitter of L2 `deliver`, typically the Runner committee (default path) or third-party arbitrageur (fallback).
