---
title: "CIP-41: Multi-Signer Transaction Authorization"
description: Exposes the transaction's consensus-verified co-signer set to actor code, so an actor can enforce its own roster and threshold inside a single atomic transaction. Adds `co_signers` to the PVM host context, `get_co_signers()` to the SDK, and a client-side co-signing flow over the transaction's mode-selected authorization digest.
icon: users
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-08-08
  **Requires:** CIP-3 (fee model — §2.5 multi-signer admission and metering), CIP-6 (SDK — §11.3 deterministic collections), CIP-40 (transaction verification — §7.3 duplicate-primary rule and `signature_mode`)
  **Relates-to:** CIP-12 (Governance — Council `SignerSet` precedent), CIP-28 (agent banking), CIP-37 (Sign in with Cowboy)
</Note>

## 1. Abstract

Cowboy transactions already carry multiple secp256k1 signatures. `Transaction.additional_signers` is bound into the mode-selected authorization digest, canonicalized against reordering and duplication, and ecrecover-verified for every signer at block admission. The one thing missing is visibility: the verified signer set never reaches actor code, so its only consumers are two authorization checks inside the execution engine.

This proposal exposes that set. `HostContext` gains a `co_signers` field, the SDK gains `get_co_signers()`, and the client tooling gains a co-signing flow that assembles a multi-signer transaction from independently produced signatures. With those three pieces, an actor holding its own roster and threshold can authorize an operation from a quorum in **one atomic transaction**, with no in-handler cryptography and no new signature scheme.

It builds on two companion changes to the existing multi-signer path, specified where those rules live: CIP-40 §7.3 rejects a transaction whose primary is repeated in `additional_signers` (what makes a naive quorum count safe), and CIP-3 §2.5 prices and bounds co-signature verification (intrinsic non-sponsorable charge, block budget, ingress caps).

## 2. Background

The transaction layer is already multi-signer end to end.

`Transaction.additional_signers: Vec<(Address, EthSignature)>` holds the co-signers (`cowboy-protocol/crates/cowboy-protocol-codec/src/transaction.rs:106`). The signing preimage is the full canonical encoding with every signature zeroed and every **signer address retained** (`transaction.rs:142`), so each signature commits to the whole transaction *including the roster* — a signer cannot be added, removed, or reordered after the fact without invalidating every signature. `verify()` requires the additional set to be strictly ascending by address, bounded by `MAX_ADDITIONAL_SIGNERS` (16), and ecrecovers each entry against the shared hash (`transaction.rs:405`). `Transaction::sign_multi` produces the canonical form (`transaction.rs:304`). Block admission verifies every transaction, deferred ones included, precisely so downstream authorization can trust the field (`node/chain/src/application.rs:2656`).

Two consumers exist, both inside the execution engine. `council_authorized` counts distinct roster members across `{tx.from} ∪ additional_signers` against a `SignerSet` loaded from governance actor `0x09` (`node/execution/src/execution/council.rs:19`), performing membership and threshold only. `CreateAccount` reads the same set through the existing `Transaction::signers()` helper (`transaction.rs:202`) and requires both `tx.from` and the account being created to appear in it (`node/execution/src/execution/system_instruction.rs:67`) — a dual-signature rule rather than a threshold. Neither re-verifies signatures; both rely on admission having done so.

Actor code cannot reach any of this. `HostContext` carries `sender` and nothing about co-signers (`node/pvm/crates/pvm-host/src/lib.rs:8`), and the PVM exposes `keccak256` and `ed25519_verify` but deliberately no secp256k1 recovery (`pvm-host/src/lib.rs:532`) — an actor therefore cannot verify Cowboy account signatures for itself, by design. There is also no CLI co-signing flow; multi-signer transactions are assembled by test harnesses and external tooling.

The client-side primitives, by contrast, are already shipped. The wasm codec exposes `signing_preimage`, `signing_hash`, `sign`, and `encode_submission` (`cowboy-protocol/crates/cowboy-protocol-wasm/src/lib.rs:71`), and documents exactly the flow this CIP standardizes: each signer signs the shared digest independently, assemble `additional_signers` client-side, submit the envelope.

## 3. Goals

- An actor can require a quorum of accounts for a privileged operation, enforced in one atomic transaction.
- Authorization semantics are explicit: what a co-signature does and does not authorize, in which execution frames it is visible, and how it expires.
- No new cryptography, no new signature scheme, no change to transaction identity.
- Any client that can produce a secp256k1 signature over a 32-byte hash — CLI, wasm SDK, or an EVM hardware wallet under CIP-40 — can be a co-signer.

### 3.1 Non-Goals

- **Actors as co-signers.** Actors hold no keys and cannot produce a signature; an actor address can never appear in `additional_signers`. Actor quorums are expressed by approval accumulation across calls, where the callee observes the calling actor through `sender`, and are specified separately.
- **Threshold or aggregate signatures.** The set is k independent ECDSA signatures, not one aggregated signature. Cost and transaction size scale linearly.
- **Protocol-level multisig accounts.** Authorization policy lives in the actor, not in a system-level account type. The protocol supplies an authenticated signer set; it does not interpret it.
- **Changing `tx.digest()`.** Transaction identity is signature-independent and stays that way.

## 4. Specification

### 4.1 Signer Set Semantics

A co-signature is **authorization only**. It carries no economic or sequencing role:

- `tx.nonce` is checked against `tx.from`'s account nonce alone (`node/execution/src/execution/transaction.rs:143`), and the nonce increment writes only that account (`transaction.rs:886`). Co-signers consume no nonce.
- A co-signer need not hold a balance, and need not exist as a funded account at all. An address that has never transacted is a valid co-signer. The **co-signer** role never causes a debit — `F_sig` is paid by `tx.from` alone — though a co-signing account may separately pay in an ordinary sponsorship role (as the target actor's `UseOwnerBalance` owner, say), which is that role's debit, not the co-signature's.
- The primary signer controls *submission*: it chooses whether, when, and at which nonce to assemble. Once assembled, the transaction is a **bearer artifact** — it carries every signature it needs, so any party holding the bytes can broadcast it. Exclusive control ends at assembly, not at inclusion.

Fees do not follow the signer set. Gas resolves through the existing cascade — actor balance, then a CIP-28 eligible default card, then `UseOwnerBalance`, then the sender (`transaction.rs:660`) — so `tx.from` is the payer of last resort rather than the sole payer, and a co-signed transaction targeting a sponsoring actor is funded by that actor like any other. The one exception is the co-signer intrinsic charge, which is non-sponsorable; see §4.7.

Because the authorization digest — in either signature mode — binds `chain_id`, `nonce`, and the full instruction, a collected signature set authorizes **exactly one transaction, on one network, at one nonce of one account**. Once `tx.from`'s nonce advances past it, the set is permanently dead. Collected approvals therefore expire on their own, without an expiry field.

### 4.2 Host Context

`HostContext` gains two fields:

```rust
pub struct HostContext {
    // ... existing fields ...
    /// Authenticated co-signers of the directly signed transaction that entered
    /// this frame: `Some(canonical ascending set, excluding sender)`.
    /// `None` in any frame not entered by a directly signed transaction.
    pub co_signers: Option<Vec<Bytes>>,
    /// Code hash of the executing actor. See §5.2.
    pub code_hash: [u8; 32],
}
```

The field is `co_signers`, not `signers`, because `Transaction::signers()` already exists and returns the primary *plus* the additional set (`transaction.rs:202`). Two names differing only in whether the sender is a member is a defect generator; the host field is named for what it holds.

The distinction between `None` and `Some([])` is load-bearing and MUST be preserved end to end. `Some([])` means *a signed transaction entered this frame and carried one signer*. `None` means *this frame's authority did not come from a transaction signature at all*. Collapsing both to an empty list makes a timer fire indistinguishable from a single-signer transaction at exactly the point where an actor is deciding whether it was authorized.

`code_hash` is a separate, unconditional field — it describes the executing actor, not the transaction, so it is present in every frame including nested and non-signed ones, and it always reflects the code actually executing: simulation derives it from the executed code itself, overriding any injected value (§7). It exists because §5.2's policy commitment is otherwise unverifiable from actor code: the execution context already carries `actor.code_hash` internally (it is read on every cross-actor call at `node/execution/src/pvm_host.rs:2158`), but no host field or SDK accessor exposes it.

`code_hash` is a **frame-entry snapshot**, immutable for the life of the handler frame. This matters because `upgrade_self` mutates the live context mid-handler — it stages the new bytes and overwrites `ctx.actor.code_hash` while the *old* code continues executing (`pvm_host.rs:4451`). If the field tracked the live value, old code could call `upgrade_self` and then observe the new hash, passing a §5.2 commitment check under code that no longer matches it. Normatively: the value is captured at frame entry; `upgrade_self` changes what *subsequent* invocations observe, never the current frame; a self-call into the new code observes the new hash in its own (nested) frame; and nested frames restore the outer snapshot on both success and error return.

Normative rules:

1. **Excludes the sender.** The set contains `additional_signers` only. `sender` is already available and is `tx.from` when the set is `Some`. The full authorizing set is `{sender} ∪ co_signers`.
2. **Canonical order preserved.** Strictly ascending under byte-lexicographic comparison of the 20-byte big-endian address — the same total order `Address: Ord` provides and that the wire canonicalization is verified against. Actors may rely on the ordering and on the absence of duplicates.
3. **Derived from the verified transaction.** Populated from the `Transaction` that passed `verify()` at admission. Nothing in the execution path may reconstruct it from handler input, instruction payload, or any other caller-supplied source.
4. **Populated by provenance, not by depth alone.** The set is `Some` in exactly one case: the frame was entered by an actor-invoking instruction of a **directly signed, non-deferred** transaction — `ExecuteActor`, or the atomic `init` invocation of a deploy. `init` qualifies because the signed instruction contains the code, the handler, and the payload; the deployer authorized exactly that invocation. Every other frame is `None`. "Outermost frame" is not the predicate — several execution paths create a fresh, outermost PVM context without a signed instruction behind them, and each of them MUST be `None`: deferred transactions, timer fires, mailbox deliveries, asynchronous event fires, token transfer hooks, runner result callbacks, gateway *read* paths, and default simulation contexts — while a **synchronous** event fire is a nested entry (it runs through the same switch/restore machinery as `call_actor`) and is `None` by the nested-frame rule, not by fresh construction (`run_simulation` accepts a caller-injected `HostContext`, `node/pvm/crates/pvm-runtime/src/simulate.rs:501`; the default is `None`, and injected values are test fixtures carrying no authorization claim). A gateway **submit** is not on that list: the gateway builds and signs an ordinary non-deferred `ExecuteActor` transaction with its own key (`gateway/crates/gateway-actor/src/lib.rs:383`), so the actor observes `Some([])` with the gateway as `sender` — signed provenance, no co-signers.
5. **Cleared on every nested entry.** A nested handler invocation sets `None` and restores the caller's value on return. This covers the cross-actor path through `switch_to_callee` / `restore_call_snapshot` (`node/execution/src/pvm_host.rs:2183`) **and** the self-call path, which bypasses both and dispatches straight to `execute_current_handler` (`pvm_host.rs:3154`). A `sender`-based test does not work here: a self-call leaves `sender` untouched, so `sender == tx.from` still holds in the nested frame. See §5.1.

For deferred transactions rule 4 is reinforced by a structural invariant already in place — `verify()` returns true for a deferred transaction only if `additional_signers` is empty, which is the barrier that lets Council authorization trust the field at all (`node/chain/src/application.rs:2661`).

The Python bridge is additive: `host_context_to_dict` gains a `co_signers` key, holding a list of 20-byte values or `None`, and a `code_hash` key holding 32 bytes (`node/pvm/crates/pvm-runtime/src/module.rs:1023`). Actors reading context through the named accessors observe no change. Context is exposed as a dictionary, so an actor that enumerates or serializes its keys does see the new ones; that is the full extent of the compatibility surface.

### 4.3 SDK Surface

```python
def get_co_signers() -> list[bytes] | None:
    """Co-signers of the directly signed transaction that entered this frame,
    ascending, excluding get_sender(). None when this frame was not entered by a
    signed transaction — nested calls, deferred txs, timers, messages, event
    fires, transfer hooks, runner callbacks, gateway READS, default simulation contexts.
    Gateway SUBMITS are gateway-signed transactions and yield [] (see §4.2.4)."""

def get_code_hash() -> bytes:
    """32-byte code hash of the executing actor. Present in every frame;
    the oracle for the §5.2 policy commitment."""
```

Both are exposed from `cowboy_sdk.runtime` and re-exported through `pvm_sys`, mirroring `get_sender()` (`node/pvm/Lib/cowboy_sdk/runtime.py:140`, `pvm_sys.py:22`). `get_co_signers` reads `context().get("co_signers")` — `.get`, not indexing, so a missing key (an older runtime) yields `None` rather than raising. It never returns `[]` for an absent key: an older runtime is a frame whose provenance is unknown, which is exactly the `None` case.

A roster check is then ordinary actor code. It fails closed on a degenerate threshold — `threshold < 1` would authorize everyone, `threshold > len(roster)` is unsatisfiable and should read as a configuration error, not run silently. The `None` branch is a distinct rejection rather than a quorum of one, and the count needs no de-duplication of its own — canonical form (§4.5, CIP-40 §7.3) guarantees the set is ascending, duplicate-free, and disjoint from the sender:

```python
def authorized(roster: ordered_set, threshold: int) -> bool:
    if threshold < 1 or threshold > len(roster):
        return False                      # degenerate policy: fail closed
    signers = get_co_signers()
    if signers is None:
        return False                      # not a signed-transaction frame
    count = 1 if get_sender() in roster else 0
    for addr in signers:                  # canonical: ascending, no dups, excludes sender
        if addr in roster:
            count += 1
    return count >= threshold
```

The roster is an `ordered_set`, not a built-in `set` — CIP-6 §11.3 prohibits `set()` in actor code for determinism, and this CIP requires CIP-6.

### 4.4 Client Co-Signing Flow

Co-signing is a three-phase flow over the transaction's **mode-selected authorization digest**: in signature mode `0` that is `signing_hash()`, in mode `1` the EIP-712 digest of the typed representation (§4.8). The roster and the mode must both be fixed before anyone signs — the roster because it is inside the preimage, the mode because it selects what is signed.

1. **Build.** The initiator constructs the complete transaction — instruction, `chain_id`, `from`, `from`'s next nonce, gas parameters, `signature_mode`, and `additional_signers` populated with every co-signer address in ascending order and `EthSignature::ZERO` in each slot. This object is the co-signing request.
2. **Sign.** Each signer independently computes the mode-selected digest over that object and signs it. The digest is identical for every signer including the primary. No signer needs the others' signatures, and signatures may be produced in any order, on any device, at any time.
3. **Assemble and submit.** The initiator fills each signature into its slot, places the primary in `signature`, and submits. Any single field altered between phases 1 and 3 invalidates every signature.

CLI surface, under the existing `cowboy transaction` subcommand (`node/cli/src/commands.rs:107`):

| Command | Behavior |
| --- | --- |
| `transaction build-unsigned --signers <a,b,c> …` | Emits the phase-1 object as canyon JSON. Sorts and de-duplicates the roster; rejects a roster containing `from`. |
| `transaction cosign --tx <file> [--private-key <path>]` | Branches on the object's `signature_mode`: computes the mode-selected digest (mode 0 `signing_hash`, mode 1 EIP-712), prints it, this key's 65-byte signature, and its derived address. Offline; no RPC. |
| `transaction assemble --tx <file> --signature <addr>:<sig> …` | Fills slots, verifies the assembled transaction locally, emits **bare `Transaction` encoding as hex**. Fails on a signature whose recovered address does not match its slot. |
| `transaction submit --data <hex>` | Unchanged, and consumes `assemble` output byte-for-byte. |

The artifact `assemble` emits is the bare transaction encoding, not a submission envelope: `submit` decodes `Transaction::read` to EOF and constructs `Submission::Transactions` itself (`node/cli/src/commands.rs:1096`). The wasm `encode_submission` path emits the envelope for direct POST instead, and is a separate artifact tested separately — the two must not be conflated in tooling or in tests.

`cosign` MUST render the decoded instruction before signing, not just the digest. The digest is opaque in either mode; a co-signer that cannot see the operation is signing blind. Clients holding a decoder — the CLI, or the wasm codec in a browser — MUST show the decoded transaction; those that cannot MUST say so rather than present the digest alone.

In native signature mode the wasm path needs no new digest primitive — `signing_hash` for phase 2, `sign` for the signature, `encode_submission` for phase 3 (`cowboy-protocol-wasm/src/lib.rs:71`) — though its existing exports gain the shared roster validation of CIP-40 §7.3. Under CIP-40's `eip712` mode phase 2 is a different digest entirely — see §4.8. Landed and pending transactions already expose the roster over the indexer JSON (`node/indexer/src/json.rs:72`), so a UI can show which slots are filled without new endpoints.

### 4.5 Canonicalization

Canonical form — strictly ascending, duplicate-free, bounded, disjoint from `from`, and the matching producer contract — is normative in CIP-40 §7.3. This CIP relies on those guarantees: they are what make the §4.3 helper's naive membership count correct, and what keeps one authorization mapped to exactly one transaction identity.

### 4.6 No Migration

These rules need no activation height, version gate, or migration path: pre-launch networks are reset routinely, so there is no history to carry across the change. `Transaction::verify()` has no height parameter and gains none. The rules in this CIP are one coherent unit — a build enforcing the context semantics without the SDK, CLI, and canonicalization pieces is simply incomplete — but that is release hygiene, not activation machinery.

CIP-40 also revises `Transaction::verify()` (it adds `signature_mode` to the encoding and re-baselines the golden corpus); whichever lands second rebases onto the other.

### 4.7 Metering

Co-signature verification is priced and bounded by CIP-3 §2.5: an intrinsic per-co-signer charge that is **non-sponsorable** (split from the sponsorship cascade at settlement — co-signers are chosen by whoever assembles the envelope, and the sponsor never consented to them), a pre-recovery structural minimum on `cycles_limit`, a per-block co-signer budget checked before any recovery, and ingress caps with a cross-connection peer key and a global work budget. Those mechanisms are normative there; this CIP adds no metering rules of its own.

### 4.8 Signature Modes

Under CIP-40 a transaction carries a `signature_mode` discriminant covering the transaction as a whole: the primary and every additional signer use the same mode. A multi-signer roster is therefore **mode-homogeneous** — every member signs natively, or every member signs EIP-712 typed data.

This binds the flow in §4.4. `build-unsigned` freezes `signature_mode` along with the roster, because the mode determines what each signer signs: in mode `0` that is `signing_hash()`, and in mode `1` it is the EIP-712 digest of the typed representation, which is not the same value. `cosign` branches on the mode recorded in the phase-1 object and produces the corresponding digest and signature; a signature produced under one mode is not valid under the other, in any slot.

The consequence is a roster constraint, and rosters should be assembled with it in mind: a hardware wallet signing over WalletConnect and a CLI signing natively cannot co-sign the same transaction. Making mode per-signer would require the mode selector for each slot to sit inside the signing preimage, so that no signature is verifiable under a mode its signer did not choose; that is a CIP-40 change, not one this proposal makes.

## 5. Security Considerations

### 5.1 Cross-Frame Confused Deputy

The reason `co_signers` is frame-scoped is the confused-deputy case. If a callee could observe the transaction's signer set, then any actor called — at any depth — by a quorum-authorized transaction would appear to have been authorized by that quorum directly. Actor A, holding a legitimate 3-of-5 roster, calls actor B for an unrelated reason; B checks the same roster and concludes it was authorized. The quorum signed an instruction against A, not a downstream call into B.

Clearing `co_signers` on cross-actor entry makes the guarantee precise: **a co-signature authorizes exactly the instruction it signed, in the frame that instruction entered.** An actor that wants to propagate quorum authorization must pass it explicitly as call data, which correctly reduces the question to "does B trust A" — a decision B makes with full knowledge, rather than one the runtime makes on B's behalf.

The same reasoning applies **within** an actor, and this is the case a `sender`-based rule silently misses. `call_actor` dispatches a self-call — target equal to caller — straight to `execute_current_handler` without entering `switch_to_callee` at all (`node/execution/src/pvm_host.rs:3154`). `sender` is left untouched, so in that nested frame `sender` still equals `tx.from` and a sender comparison would rule the quorum visible. An actor whose public handler self-calls a privileged one would carry the quorum into it. The co-signers authorized an instruction naming one handler, not every handler that one can reach.

Hence the provenance rule in §4.2.4 and the clearing rule in §4.2.5. The implementation obligation covers both nested entries: clear and restore around the cross-actor path (`switch_to_callee` / `restore_call_snapshot`, `pvm_host.rs:2183`) and around the self-call dispatch (`pvm_host.rs:3154`).

### 5.2 Roster Changes Between Signing and Inclusion

The transaction binds no actor state, so a signature collected under one authorization policy is still presented after that policy changes. Roster membership is the obvious case — a removed signer's signature keeps counting — but it is not the dangerous one. Lowering a threshold, widening a role, or raising a spend tier makes an *already collected* set newly sufficient for an operation nobody authorized at that strength.

An actor whose authorization policy is mutable MUST therefore bind a **policy commitment** into the signed instruction, covering every mutable input to the decision — roster, threshold, roles, limits — not roster membership alone. The actor rejects on mismatch, so any policy change invalidates every in-flight authorization rather than reinterpreting it. An actor whose policy is immutable after construction has no such obligation.

The commitment MUST cover the actor's code hash, not only its policy state. `upgrade_self` (`node/pvm/crates/pvm-host/src/lib.rs:518`) lets an actor replace its own code, so the same actor, handler, payload, and policy version can execute under changed authorization semantics. A hash commitment binds this structurally; a monotonic version counter binds it only if every upgrade remembers to bump, which is the discipline that fails first.

This is why §4.2 adds `code_hash` to the host context and §4.3 exposes it as `get_code_hash()`. Without an oracle the obligation is unverifiable from inside the actor — the context exposes `actor_addr` but nothing about the code running at it — and an unverifiable requirement is not a requirement. With it, the actor compares the committed hash against `get_code_hash()` and rejects on mismatch, with no reliance on an upgrade path remembering anything.

The protocol cannot enforce this by exposing `co_signers`. It is an obligation on the actor, and a reference multisig actor MUST carry an acceptance test in which an in-flight transaction is rejected after a policy change that would otherwise have made it succeed.

### 5.3 Blind Co-Signing

A co-signer's exposure is bounded by the instruction they signed, but only if they can see it. The authorization digest is opaque in either mode; presenting it alone tells a signer nothing. §4.4 makes decode-before-sign a requirement of the signing surface rather than a UI nicety, and CIP-40's typed-data mode extends the same property to hardware wallets.

### 5.4 Submission Withholding

The primary signer can collect a quorum and never submit, or submit at a chosen moment. This is inherent: someone must pay and sequence. It is bounded by §4.1's nonce binding — the set dies when the primary's nonce advances — and by the actor's own policy-commitment check (§5.2). Actors whose operations are timing-sensitive should bind a deadline height into the signed payload and reject late execution.

## 6. Rationale

**Why expose the set rather than build multisig into the protocol.** A protocol-level multisig account type would fix one policy — k-of-n over a static roster — into consensus. Actors already own policy, and the policies people actually want (spend limits, per-operation thresholds, time locks, role-scoped signers, agent budgets under CIP-28) are actor logic. The protocol's job is to say truthfully which keys authorized this transaction; interpretation belongs above it.

**Why not in-actor signature verification.** Exposing secp256k1 recovery to the PVM would let an actor verify collected signatures itself, at the cost of every actor author reimplementing replay protection, domain separation, and canonicalization. Consensus already does all three correctly for transactions. CIP-37 draws the same line for gateway authentication.

**Why the Council does not migrate.** `council_authorized` operates on the transaction inside the execution engine, where the field is already available, and its roster is system state rather than actor state. It is unaffected. The CIP-40 §7.3 duplicate-primary rule applies to both paths; the Council never depended on it (its `HashSet` accumulation de-duplicates), but actor code counting `{sender} ∪ co_signers` does.

## 7. Acceptance Criteria

Every criterion below is a runnable boundary — a concrete input, an observable output — not a review assertion.

**Client flow** (the duplicate-primary and metering criteria live with their rules, in CIP-40 §7.3 and CIP-3 §2.5)

- The `authorized` helper fails closed on degenerate thresholds: 0, negative, and greater than the roster size each return `False` regardless of the signer set.
- A table test mutates each field of the canonical signing preimage **except signature bytes** in turn, between `build-unsigned` and `assemble`, run once per signature mode; every row fails assembly's local verification, and in mode 1 the CIP-40 §7.3 forbidden fields (`access_list`, the `origin_*` fields) reject structurally. The row set is the preimage schema itself: `chain_id`, `nonce`, every instruction field, each of the six gas parameters, `from`, `access_list`, `signature_mode`, the signer roster, `origin_tx_hash` / `origin_remaining_cycles` / `origin_remaining_cells`, and `metadata`. (Signature slots are exactly the fields assembly is *supposed* to fill.)
- A transaction assembled by `build-unsigned` → `cosign` (×k, independently, offline) → `assemble` verifies locally and executes on a devnet.
- `assemble` output decodes as a bare `Transaction` byte-equal to `encode_transaction` of the same object, and feeds `transaction submit` unchanged; `encode_submission` output decodes as `Submission::Transactions` — one test per artifact, proving the two are not interchangeable.
- `cosign` fails closed: when the phase-1 object cannot be decoded and rendered, no signature is emitted.
- A signature produced under `signature_mode` 0 fails verification when placed in a mode-1 transaction and vice versa — tested in the primary slot and in an additional slot.

**Provenance — `Some` cases**

- An actor called directly by a k-signer `ExecuteActor` observes `Some` of exactly the k−1 additional signers, ascending, excluding the sender.
- A single-signer transaction yields `Some([])`, distinguishable at the SDK boundary from every `None` case below.
- A deploy's atomic `init` invocation observes `Some` of the deploying transaction's co-signers.
- A gateway **submit** — the gateway-signed `ExecuteActor` — yields `Some([])` with the gateway account as `sender`.

**Provenance — `None` cases and restoration**

Every **causally seedable** derived-path test in this group originates from a transaction carrying a non-empty co-signer set (k ≥ 2), so that an implementation which never populates the field cannot pass by accident — the tests must observe `Some` at the origin and `None` on the derived path, not `None` everywhere. Gateway reads and default simulation have no originating transaction and are tested independently.

- Cross-call: the callee observes `None` and the caller's `Some([...])` is restored — on the success path **and** on the error path (callee raises, caller resumes).
- Self-call: the nested frame observes `None` and the outer `Some([...])` is restored, success and error paths both. A handler reachable only by self-call cannot see the quorum.
- `TokenTransferFrom` where `from ≠ tx.from`: both transfer hooks observe `sender = from` and `co_signers = None`.
- Synchronous event fire: the subscriber observes `None`; the emitting frame's `Some([...])` is restored after the fire, on the success path **and** when the subscriber raises.
- Asynchronous event fire: the subscriber observes `None` even though the deferred transaction carries an `origin_tx_hash` naming the co-signed origin.
- Deferred transactions, timer fires, mailbox deliveries, runner result callbacks, and gateway reads each yield `None` — one test per entry path, each seeded from a co-signed origin where the path admits one.
- Default simulation yields `None`: `run_simulation` with no injected context observes `co_signers = None` (`node/pvm/crates/pvm-runtime/src/simulate.rs:501` accepts a caller-supplied `HostContext`, so this is a property of the *default* context — injected `co_signers` values are untrusted test fixtures and carry no authorization claim).
- Simulation `code_hash` is the executed code's, on **every** simulation API: `run_simulation`, and `Simulator::new(code, options)` / `simulate::Network` (which copy `options.context` unchanged today, `node/pvm/crates/pvm-runtime/src/simulate.rs:612`) each override default or injected `code_hash` with `keccak256` of the code actually executed — an injected context cannot make `get_code_hash()` lie about the running code. Runnable tests for both APIs.
- Bridge mapping is exact: `None` → Python `None`; `Some([])` → `[]`; `Some([a, b])` → `[a, b]` in that order; a missing key (older runtime) → `None`.
- `get_code_hash()` returns the executing actor's code hash in every frame — signed, nested, and non-signed.
- `get_code_hash()` is a frame-entry snapshot: within the handler that calls `upgrade_self`, it continues to return the **old** hash after the call; a self-call made after the upgrade observes the **new** hash in its nested frame; and the first subsequent invocation observes the new hash.
- Nested frames restore the outer frame's `code_hash` snapshot on return, on both the success and the error path.

**Existing consumers and release**

- Council authorization (`council_authorized`) and `CreateAccount` dual-signature behavior are unchanged — continuity tests for both.
- A zero-co-signer transaction is byte-identical on the wire to one produced without this CIP, and unchanged in behavior — this CIP adds no field and changes no encoding.
- One release carries every CIP-41 rule together with its CIP-40 §7.3 and CIP-3 §2.5 prerequisites; none is separately togglable.
- The reference multisig actor — `cowboy/examples` fixture accompanying this CIP, using `get_co_signers()` and a policy commitment covering roster, threshold, limits, and `get_code_hash()` — rejects an in-flight authorization after a policy change: a threshold *reduction* that would otherwise have made the collected set sufficient, and an `upgrade_self` that changes authorization semantics with policy state unchanged.

## 8. Implementation Notes

| Change | Location |
| --- | --- |
| `co_signers: Option<Vec<Bytes>>` and `code_hash` on `HostContext` | `node/pvm/crates/pvm-host/src/lib.rs` |
| `co_signers` and `code_hash` keys in the context dict, preserving `None` vs `Some([])` | `node/pvm/crates/pvm-runtime/src/module.rs` |
| Populate on directly signed entry — `ExecuteActor` and the deploy `init` boundary (`actor_instruction.rs`); clear/restore around both nested-frame entries — `switch_to_callee` / `restore_call_snapshot`, and the self-call dispatch at `pvm_host.rs:3154`; snapshot `code_hash` at frame entry so `upgrade_self`'s live mutation (`pvm_host.rs:4451`) is invisible to the current frame | `node/execution/src/pvm_host.rs`, `node/execution/src/execution/actor_instruction.rs` |
| Every fresh-context constructor initializes `co_signers = None` and the executing actor's `code_hash` — token transfer hooks, deferred/timer/mailbox/runner delivery (`actor_instruction.rs`), asynchronous event delivery (`event_fire.rs`), RPC read (`node/rpc/src/handlers/actor.rs`), and every simulation entry point — `run_simulation`, `Simulator::new`, `simulate::Network` (`simulate.rs`) — deriving `code_hash` from the executed code, overriding injected values. Synchronous event fire is NOT here — it is a nested entry covered by the clear/restore row above | fresh-`HostContext` construction sites in `node/execution`, `node/rpc`, `node/pvm/crates/pvm-runtime` |
| `get_co_signers()`, `get_code_hash()` | `node/pvm/Lib/cowboy_sdk/runtime.py`, `pvm_sys.py` |
| Local-dev mirrors of both accessors, with mock state, setters/reset, and tests — its `runtime.py` promises an interface identical to `cowboy_sdk.runtime` and already mirrors `get_sender` | `python-sdk/cowboy/runtime.py` |
| `build-unsigned` / `cosign` / `assemble` | `node/cli/src/commands.rs` |

The canonicalization changes (duplicate-primary rejection in `verify()`, the producer contract across the Rust/Python/CLI/wasm signing surfaces) are implemented under CIP-40 §7.3, and the metering changes (structural minimum, precharge, settlement split, block budget, ingress caps) under CIP-3 §2.5; their implementation maps live there. This CIP's protocol dependency is a `cowboy-protocol` release consumed by `node` — the codec bump lands first, with the node pins and `Cargo.lock`. No field is added to `Transaction`, so the wire format is unchanged.

**Canonical sources and mirrors.** Several touched surfaces exist in more than one place, and the edit lands in the canonical copy with propagation to each mirror in the same release:

- `HostContext` and `host_context_to_dict`: canonical in `node/pvm`; the standalone `pvm/` repo mirrors the PVM crates, and `bronco` syncs its shared crates from `node/pvm` (per its README). The synced crates pick the change up automatically — but sync is not the whole story: both repos also contain **local `HostContext` literals outside the synced crates** (`bronco/crates/cli/src/host.rs`, `bronco/crates/wasm-sim/src/host.rs`, `pvm/examples/*` constructing `HostContext` directly). The release includes a sweep of every `HostContext` construction site across consumers — enforced as a build gate, since a missed literal is a compile error once the fields are non-optional in the struct — hand-updating non-synced consumers to `co_signers = None` and an honest executed-code hash.
- `cowboy_sdk` (`runtime.py`, `pvm_sys.py`): canonical in `node/pvm/Lib/cowboy_sdk`; the PyPI package republishes a **duplicated snapshot** under `node/pvm/Lib/pypi_build`, refreshed by `pypi_build/sync_sdk.py` and guarded by `cowboy_sdk/tests/test_pypi_build_sync.py` — the release runs the sync script before rebuilding, since rebuilding alone ships the stale snapshot.
- The published `python-sdk` package is a separate codebase, not a synced mirror — but it is touched twice: its signer is a CIP-40 §7.3 producer, and its `cowboy/runtime.py` promises a local-dev interface identical to `cowboy_sdk.runtime`, so it implements both accessors with mock state (listed above).

Dependency order within the release: `cowboy-protocol` (codec + wasm) first, then `node` (pins + `Cargo.lock`), then the mirrors by their normal sync.
