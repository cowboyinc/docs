# CIP-33: Actor Hiring & Distribution

| Field           | Value                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------- |
| **CIP**         | 33                                                                                             |
| **Title**       | Actor Hiring & Distribution (Trading Post)                                                     |
| **Status**      | Draft                                                                                          |
| **Type**        | Standards Track (System Actor)                                                                 |
| **Created**     | 2026-05-01                                                                                     |
| **Author(s)**   | Cowboy Foundation                                                                              |
| **Depends-on**  | CIP-2, CIP-3, CIP-5, CIP-8, CIP-9, CIP-12, CIP-13, CIP-20, CIP-24, CIP-26, CIP-27, CIP-29, CIP-40, CIP-41 |
| **CIP-40 boundary** | CIP-40 co-owns the Transaction-v2 decoder (§2.6.4). Its release admits/executes only kind 0; kind 1 is standalone-decoder-only and consensus-invalid until a later CIP-33 activation. CIP-27/CIP-41 are dependencies of that future activation, not the decoder-only arm. |
| **Relates-to**  | CIP-7 (key-delivery precedent)                                                                  |
| **Required-by** | General Store product spec (app-layer; not a CIP)                                              |


> **CIP-7 precedent:** references below to committee-delivered stream keys describe the retired CIP-7 design. Replacement Watchtower uses service-delivered keys and cumulative usage authorization; these historical comparisons do not make the retired SKM/SealRequest interfaces dependencies of this CIP.

## Abstract

This CIP defines the protocol rails for **hiring actors**: how an actor declares
itself hireable, how creators grant **stores** the right to distribute it, how
hire payments execute **declared fee splits**, how **review attestations** bind
reviewer identities to exact code snapshots, and how access to private actor
code is enforced through **CBSS key leases** rather than consensus changes.

The rails are **store-neutral**. Any party may operate a store; no store —
including the one operated by Cowboy Labs — holds protocol privileges. Direct
hiring with no store in the loop is a first-class path. The protocol specifies
the *mechanics* of publishing, hiring, splitting, and attesting; it is silent
on review policy, curation, and commission rates, which are store-level
products.

All hiring state and logic live in one governance-controlled system actor,
the **Trading Post**. The Shared-mode path needs no consensus change;
Forked-mode instance hiring (§2.6.4) is future CIP-33 activation work. It
depends on the CIP-27 `fork` amendment and two mailbox changes — message-origin
exposure and escrow-funded directive gas. None is activated by CIP-40's
decoder-only kind-1 arm (§1.2, §2.6.5).

## 1. Motivation

### 1.1 Store mechanics belong in the protocol; stores do not

An agent marketplace needs five mechanics no single app should own:

1. **Hireability** — a machine-readable statement that an actor can be hired,
   under what terms, by humans and by other actors.
2. **Distribution rights** — creator-controlled authorization for a store to
   sell access to the actor.
3. **Fee splits** — hire revenue divided among creator, store, and referrers
   exactly as declared, atomically with payment.
4. **Attestation** — verifiable review verdicts bound to exact code snapshots,
   so trust survives redistribution.
5. **Enforcement** — for private-code actors, access that can be granted,
   metered, and revoked without trusting the storefront.

Baking a *particular store* into consensus would freeze product policy into
protocol and contradict the permissionless premise: anyone can deploy an
actor, so anyone must be able to distribute one. Conversely, leaving these
mechanics to apps reproduces the Ethereum royalty failure: splits and
revocation become conventions that any marketplace can ignore.

The resolution is rails-in-protocol, stores-as-apps. The differentiating
consequence: **actors hire actors** through the same rails humans use, making
every published actor a capability available to every other actor.

### 1.2 Enforcement rides existing cryptography; Forked hiring is a future protocol change

For private-code actors, the code volume is encrypted (CIP-9). An instance can
only execute if the data encryption key (DEK) reaches the executing runner.
Key delivery already flows through CBSS (CIP-24) under chain-state-conditioned
policy (CIP-7 precedent). Gating share issuance on hire/lease state in the
Trading Post therefore gives cryptographic enforcement of paid access —
a decryption monopoly rather than a distribution monopoly — using machinery
that exists today. Fee capture and IP protection are the same mechanism.

The **Shared-mode** hiring path in this CIP needs no consensus change: it is
all Trading Post system-actor logic over existing rails. **Forked-mode
hiring (§2.6.4) is the exception** and introduces three consensus-level
changes in a later CIP-33 activation, called out here so the "no consensus changes" framing is not
overclaimed: (1) the CIP-27 `fork` amendment (a new host syscall surface),
(2) mailbox delivery preserving `Message.from` into the delivered
frame's sender (§2.6.4, needed for spawn-directive provenance), and
(3) escrow-funded directive gas — the spawn directive's materialized
deferred transaction draws its fees from the hire's gas escrow under a
pinned budget, instead of the current rule that borrows origin gas with no
caps (§2.6.4). These dependencies must be implemented together; the
design principles below (§1.3) describe the Shared baseline.

### 1.3 Design principles

- **Neutrality.** No privileged store. Store registration is open. The
  Trading Post grants every store identical powers, scoped to its own grants.
- **Creator sovereignty.** Creators decide which stores may distribute their
  actors. Grants are non-exclusive by default and revocable.
- **Direct hire is first-class.** A hire with no store involved MUST work; a
  store is a discovery and trust layer, never a tollbooth.
- **Enforce by cryptography and settlement, not consensus.** Leases gate key
  delivery; the Trading Post gates payment flows. Validators are unmodified.
- **Policy is replaceable.** Review standards, curation, pricing of trust —
  all app-layer. The protocol records outcomes (attestations), not opinions.

## 2. Specification

### 2.1 The Trading Post system actor

The Trading Post is a system actor deployed at genesis at the well-known address `0x1E` (`TRADING_POST_SYSTEM_ACTOR`), upgradeable
only through CIP-12. It holds all hiring state and executes all hiring flows
as ordinary actor methods. Codec-level monetary amounts are u128 (CIP-20
token base units use the full width), but **this CIP owns the native
boundary**: the NativeCby ledger and its debit/payout paths are u64 wei
(1 CBY = 10^9 wei), so for `PaymentAsset::NativeCby` every amount —
`price.amount`, `initial_due`, `prepaid`, `reserved`, and each split
leg — MUST fit u64, checked at method intake **before any escrow or
transfer** (the stable first rejection for an over-wide native amount),
and every aggregate uses checked u64 add/sub thereafter — normatively
the `try_from` + `checked_add` boundary current code already implements,
now required rather than incidental. The ledger is not widened.

State objects (canonical encodings follow the conventions of CIP-20 state):

```
HireableDeclaration {
  declaration_id:  Hash,             // caller-supplied: H(creator ‖ actor ‖ salt); §2.1.1
  actor:           Address,          // the hireable actor
  creator:         Address,          // payout + grant authority
  badges:          { human_hireable: bool, agent_hireable: bool },  // discovery metadata ONLY — §2.1.4; never enforced at hire
  latest_version:  u32,
  status:          Active | Retired,
}

DeclarationVersion {                 // append-only under a declaration; never edited in place
  version:           u32,            // 1.. , assigned sequentially
  manifest_root:     Hash,           // CBFS manifest root of the code at this version (CIP-9)
  code_visibility:   Public | Private,
  capability_schema: VolumePath,     // CBFS path to capability document (§2.6)
  instance_offers:   [InstanceOffer],// §2.6.4; 1–2 offers, unique modes, ≤4 terms each, no duplicate terms
  default_split:     SplitVector,    // §2.3, used for direct hire
  // Forked-capable versions only (None on Shared-only versions):
  template_actor:    Option<Address>,       // immutable clean-fork template (§2.6.4)
  // Two commitments, NOT one (§2.6.4): the template runs with fork/spawn grants
  // that a hired child's profile MUST NOT retain — declare_version rejects a
  // Forked instance_profile keeping exec.fork (§2.6.4's rejection rule, this
  // CIP's own, not CIP-27's) — so the template's manifest and the child's
  // effective manifest have distinct canonical bytes by construction; their
  // hashes differ except under a Keccak collision (§2.6.4).
  template_commitment: Option<{ code_hash: Hash, lib_pins_hash: Hash, manifest_hash: Hash }>,
                                     // the template's live tuple, pinned at declare_version;
                                     // hire() checks the template against THIS
  instance_profile:  Option<Bytes>,  // exact CIP-27 §3.7.1 fork-profile manifest codec bytes
                                     //   for hired children; ≤ MAX_FORK_PROFILE_BYTES (CIP-27)
  instance_commitment: Option<{ code_hash: Hash, lib_pins_hash: Hash, manifest_hash: Hash }>,
                                     // what hired children run: code_hash/lib_pins_hash equal the
                                     // template's; manifest_hash = keccak256(instance_profile);
                                     // finalize_spawn checks the child against THIS
}

InstanceOffer {                      // §2.6.4 — terms are mode-qualified by construction
  mode:   Shared | Forked,
  terms:  [PricingTerm],             // §2.4; a hire in `mode` may only select terms here
}

PendingSpawn {                       // §2.6.4 — a Forked hire awaiting its instance
  hire_id:        u64,               // the eventual Hire's key; the pending record's key too
  declaration_id: Hash,              // both required to construct the finalized Hire
  grant_id:       Option<u64>,       //   (direct hire = None)
  hirer:          Address,
  template_actor: Address,
  version:        u32,
  hire_config:    Bytes,             // exact bytes; bounded by MAX_CIP33_HIRE_CONFIG_BYTES (§2.6.4)
  child_profile_hash: Hash,          // keccak256 of the version's instance_profile bytes, pinned
  price:          { token: PaymentAsset, amount: u128, initial_due: u128, prepaid: u128 },
                                     // v1 Forked terms are Free/OneShot only (§2.6.4), so no period fields
  gas_escrow:     { budget: GasBudget, deposit_wei: u64, consumed_wei: u64 },
                                     // native CBY — the native ledger is u64 wei, so these are too (§2.6.4)
  consent_nonce:  u64,               // the ListingConsent nonce pinned at hire (§2.1.3)
  delivery_deadline: BlockHeight,    // inclusive bound on directive delivery (§2.6.4)
  finalize_deadline: BlockHeight,    // inclusive bound on finalize; reclaim from finalize_deadline + 1
  split:          SplitVector,
  artifact_id:    u32,               // the pinned ServingArtifact.artifact_id
  lease_grant:    Option<LeaseRef>,
  term:           PricingTerm,
  upgrade_policy: Frozen,            // v1: always Frozen (§2.6.4)
  status:         Pending | Finalized | Refunded,
}

PaymentAsset = NativeCby | Cip20 { token_id }   // the canonical asset form; no bare Address

SpawnDirective {                     // §2.6.4 — committed authentication for the TradingPostSpawn class
  hire_id:        u64,
  template:       Address,
  tx_digest:      Hash,              // canonical digest of the COMPLETE TradingPostSpawn transaction the
                                     //   hire execution constructs — binds every field (origin, budget,
                                     //   directive body, class discriminant) in one comparison (§2.6.4)
  envelope_hash:  Hash,              // keccak256 of the cip33.env.v2 envelope — the job-side hire-identity key (§2.6.4 quarantine)
  consumed:       bool,              // consume-CAS; single-use; row PERMANENT (never deleted — the
                                     //   quarantine check reads envelope_hash for the child's lifetime)
}

GasBudget { cycles_limit: u64, cells_limit: u64, max_fee_per_cycle: u64, max_fee_per_cell: u64 }
// LeaseRef identifies a CBSS key lease (§2.5): { declaration_id, version, artifact_id, hire_id }.

ListingConsent {                     // §2.1.3 — explicit opt-in by the listed / template actor
  subject_actor:  Address,           // the actor consenting to be listed (declaration) or templated (version)
  declaration_id: Hash,
  version:        u32,               // 0 = declaration-level consent by the listed actor; ≥1 = that version's template
  commitment_hash: Hash,             // version-level: keccak256(template_commitment codec bytes ‖ instance_commitment codec bytes); zero at declaration level
  nonce:          u64,               // strictly increasing PER (subject_actor, declaration_id, version) — one namespace per listing, so unrelated listings cannot race each other's nonces; pinned into hires and spawn directives
  status:         Unconfirmed | Active | Revoked | Superseded,   // Unconfirmed = provisional commit awaiting the §2.1.3 confirm leg (admits nothing); Superseded = replaced by a later-generation propose CAS (terminal, admits nothing)
}

ServingArtifact {                    // a concrete volume carrying a version's code
  artifact_id:    u32,               // 0 = the creator's own volume, created with the version
  declaration_id: Hash,
  version:        u32,
  store_id:       Option<u64>,       // None for artifact 0; the registering store otherwise
  volume_id:      VolumeId,
  claimed_root:   Hash,              // Public Store copy: actual destination root (§2.1.2)
                                     // artifact 0 / Private: version source-root claim (§2.5.1)
  registered_by:  Address,           // creator (artifact 0) or the store's operator
  ibe_ciphertext: bytes,             // existing opaque field; visibility check applies to artifact 0 too
  // Private only — no new type: the artifact's volume carries the standard
  // CIP-24 §9.3 fields on its own StorageCommitment (wrapped_dek in WrappedDek
  // v2 format, cbss_release_key_material_hash, cbss_rewrap_required); this
  // volume DEK material remains on StorageCommitment (§2.5.1)
  status:         Active | Retired,  // Retired = no new hires may pin it; pinned hires unaffected
}

StoreRecord {
  store_id:    u64,
  operator:    Address,              // store's signing/treasury address
  metadata:    VolumePath,           // name, policy URI, branding
  status:      Active | Retired,
}

DistributionGrant {
  grant_id:          u64,
  declaration_id:    Hash,           // grants are declaration-scoped; hires pin versions
  store_id:          u64,
  split:             SplitVector,    // agreed split incl. store commission
  max_referral_bps:  u16,            // ≤ the store's own share; referral entries draw from it (§2.3)
  lease_epoch_blocks:u64,            // §2.5; creator-set, per grant
  status:            Active | Revoked,
}

Hire {
  hire_id:        u64,
  declaration_id: Hash,
  version:        u32,               // pinned at hire time; root swaps never affect live hires
  artifact_id:    u32,               // pinned serving artifact this hire runs (§2.2)
  grant_id:       Option<u64>,       // None = direct hire
  hirer:          Address,           // payer; human account or hiring actor
  hire_block:     BlockHeight,       // anchors lease-epoch numbering (§2.5.1)
  term:           PricingTerm,       // selected at hire time
  split:          SplitVector,       // pinned copy (grant/default split + referral entry, §2.3)
  prepaid:        u128,              // balance held by the Trading Post (§2.4); funds renewals
  reserved:       u128,              // portion of prepaid committed to open PerCall sessions (§2.6.3)
  renewal_on:     bool,              // hirer-controlled via set_renewal; renew() no-ops when false
  paid_through:   BlockHeight,       // subscription horizon; ∞ for one-shot; unused for PerCall
  instance_mode:  Shared | Forked,   // §2.6.4
  serving_actor:  Address,           // canonical serving identity: declaration.actor (Shared) or the instance (Forked)
  instance_address: Option<Address>, // Forked: the finalized child; None for Shared
  status:         Active | Paused | Revoked | Lapsed,
}

Attestation {
  attester:      Address,
  subject:       AttestationSubject, // tagged; see below
  verdict:       Approved | Rejected | Flagged,
  policy_uri:    VolumePath,         // the policy the verdict was issued under
  issued_at:     BlockHeight,
}

// A verdict binds either a bare manifest root (content — Shared/legacy) or
// the full executable tuple of a specific version. A ManifestRootV1 subject
// identifies ONLY content: two versions can share a root while differing in
// code, pins, or manifest, so the protocol cannot map a root to "a Forked
// version" and cannot reject root attestations at attest time. The rule is
// therefore satisfaction, not admission: ManifestRootV1 records are always
// accepted, but they NEVER satisfy any attestation requirement on a
// Forked-capable version — every such requirement (store policy, client
// gating, any future protocol gate) is defined over ExecutableV1 subjects
// exclusively.
AttestationSubject =
  | ManifestRootV1 { manifest_root: Hash }
  | ExecutableV1   { declaration_id: Hash, version: u32 }
  // For ExecutableV1 the protocol looks up (declaration_id, version), reads its
  // commitments + manifest_root, and the canonical subject key is
  //   keccak256(b"cip33.attest.v1" ‖ declaration_id ‖ u32_be(version)
  //             ‖ code_hash ‖ lib_pins_hash
  //             ‖ template_manifest_hash ‖ instance_manifest_hash   // zero-hash each when N/A (Shared-only)
  //             ‖ manifest_root)
  // recomputed on chain — so a verdict provably binds THAT version's exact
  // executables in BOTH roles (what the template runs and what hired children
  // run), and any Forked-version requirement is enforced over ExecutableV1
  // subjects. Hash canonicalization is §2.6.4's — CIP-26 pins in the pinned
  // sort order, manifest codec bytes.
```

#### 2.1.1 Declaration identity

`declaration_id = H(creator ‖ actor ‖ salt)` with a caller-chosen salt
supplied to `declare`, which recomputes and verifies the hash. Ids are
therefore known to the creator **before** the declaration exists, so the
artifact's CIP-24 volume-DEK wrap (§2.5.1) and every id-bearing record can
be produced client-side in one step — no draft/activate round-trip — and
nobody can squat an id bound to another creator.

#### 2.1.2 Methods

Each method emits a CIP-29 event named for the transition.

| Method | Caller | Effect |
|---|---|---|
| `declare(declaration_id, salt, actor, badges, v1: DeclarationVersion fields + escrow)` | creator | verify id (§2.1.1); create declaration with version 1 and its artifact 0; auto-record consent iff caller owns `actor` (§2.1.3) — otherwise unhireable until `consent_listing` |
| `declare_version(declaration_id, fields + escrow)` | creator | append `latest_version + 1` with its artifact 0; existing hires keep their pinned version; Forked-capable versions inactive until template consent (§2.1.3). Forked-capable fields validate at intake: `instance_profile` MUST NOT retain `exec.fork` (rejection, §2.6.4 — the rule that keeps template and instance manifests distinct), and `instance_commitment.manifest_hash` MUST equal `keccak256(instance_profile)` |
| `consent_listing(declaration_id, version, commitment_hash, expected_nonce)` | declaration level (`version = 0`): the actor's owner (tx) or the actor itself; version level: the template actor ONLY, via the §2.1.3 four-phase propose/provisional-commit/ack/confirm handshake | **branch on `version`**: `version = 0` (declaration level — pure Trading Post state, no template guard, no directive leg exists) records `ListingConsent` **`Active` directly**; `version ≥ 1` runs the monotonic CAS on `expected_nonce`, records `Unconfirmed`, enqueues the consent-ack directive, and the template's confirm leg flips it `Active` (§2.1.3) |
| `revoke_consent(declaration_id, version, commitment_hash, nonce)` | same callers/scoping as `consent_listing`; version level is local-first (`REVOKING` before the TP leg) and the TP leg CASes the exact `{subject == sender, declaration_id, version, commitment_hash, nonce, status ∈ {Unconfirmed, Active}}` tuple (§2.1.3) | consent → `Revoked`; new hires stop; queued and gap-window directives fail the template guard, dead-letter, reclaim (§2.1.3); existing hires per §2.4 |
| `retire_declaration(id)` | creator | stop new hires; existing hires honored per §2.4 |
| `register_store(...)` | anyone | open registration; creates `StoreRecord` |
| `grant(declaration, store, split, max_referral_bps, lease_epoch_blocks)` | creator | create `DistributionGrant` |
| `revoke_grant(id)` | creator | stop new hires via this grant; existing hires honored per §2.4 |
| `register_artifact(store_id, declaration, version, volume_id, claimed_root, ibe_ciphertext)` | the store's operator, with an active grant on the declaration | register a serving copy under the root rules below; invariant: at most one `Active` artifact per `(store_id, declaration, version)` — registering another requires retiring the first |
| `retire_artifact(declaration, version, artifact_id)` | the artifact's registering store | stop new hires pinning it; existing hires and their escrow are unaffected; retain the Public volume pin while any pinned hire can still execute |
| `hire(declaration, version?, grant?, instance_mode, hire_config?, term, referral?, payment, gas?)` | hirer (human account or actor) | pin version (default: latest), artifact (§2.2), and split; verify consent Active (§2.1.3); validate referral (§2.3); resolve `term` inside the requested mode's `InstanceOffer` (rejects a cross-mode term). **Shared:** `gas` MUST be absent; execute the first-period split, credit surplus to `prepaid`, create the `Hire` with `serving_actor = declaration.actor` — synchronous, as today. **Shared:** `hire_config` MUST also be absent (Forked-only argument). **Forked:** `gas = {budget: GasBudget, deposit_wei}` REQUIRED and validated (§2.6.4); validate `template_actor`'s live tuple against the version's `template_commitment`, take the **two typed escrows** — the price in its token, the gas deposit in CBY — record a `PendingSpawn` (§2.6.4) pinning the consent nonce and `child_profile_hash`, set `delivery_deadline = hire_height + SPAWN_TTL_BLOCKS` and `finalize_deadline = delivery_deadline + FINALIZE_WINDOW_BLOCKS` (§2.6.4), and enqueue the spawn directive; NO `Hire` yet |
| `finalize_spawn(hire_id, instance_address)` | anyone | Forked only; verify the committed child against the pinned `PendingSpawn` (§2.6.4) fail-closed; CAS `Pending → Finalized`; settle `initial_due` through the pinned split, carry surplus as `prepaid`; return `deposit_wei − consumed_wei` of the gas escrow to the hirer; write the `Hire` with `serving_actor = instance_address`; initialize the lease (§2.5, Private only) to that address |
| `reclaim_spawn(hire_id)` | hirer | Forked only; valid from `finalize_deadline + 1`; one atomic CAS `Pending → Refunded`, with the directive row handled by branch (§2.6.4): consume it if still unconsumed, else verify its terminal delivery settlement — a failed delivery attempt never strands the refund; return the price escrow in full and the gas escrow minus `consumed_wei` (consumed directive gas is never refunded) |
| `deposit(hire, payment)` | anyone | credit the hire's `prepaid` balance |
| `withdraw_unused(hire)` | hirer | refund available balance (`prepaid − reserved`) not consumed by the current period |
| `renew(hire)` | anyone (typically a CIP-5 timer) | deterministic and boundary-gated: no-op (silent, no state change, no event) before the period boundary, when `renewal_on = false`, or when the hire status is `Paused`; errors on a `Revoked` hire; otherwise (eligible only at `current_block > paid_through` — §2.4's inclusive-validity boundary) draws ONE period price from available balance and advances `paid_through := max(paid_through, current_block − 1) + period_blocks` (the gap-absorbing rule — a stale boundary never stacks dead periods) and executes the pinned split; insufficient available balance → status `Lapsed` (re-activatable by `deposit` + `renew` while declaration and grant remain Active) |
| `set_renewal(hire, on)` | hirer | toggle `renewal_on`; turning it off plus `withdraw_unused` is cancellation — current period honored; third-party `deposit`/`renew` cannot reactivate a hire whose hirer turned renewal off |
| `pause(hire)` / `resume(hire)` / `revoke_hire(hire)` | the grant's store, for hires routed through that grant; the creator, for direct hires with `Free` or `PerCall` terms only (§2.4) | trust-and-safety lifecycle control (§2.4, §2.5.3); `revoke_hire` auto-refunds the hire's available balance to the hirer |
| `settle_per_call(hire, session_receipt)` | hired actor or hirer | settle accumulated `PerCall` usage from `reserved` per the CIP-8 receipt (§2.6.3; v1 metered terms are Shared-mode only) |
| `attest(subject, verdict, policy_uri)` | anyone | record `Attestation` keyed by the tagged `AttestationSubject` (§2.7); `ManifestRootV1` records are content attestations and never satisfy a Forked-version requirement (§2.1's subject rule) |

The escrow argument to `declare` and `declare_version` is
[`ArtifactEscrow`](https://github.com/cowboyinc/cowboy-protocol/blob/92695c988611536395a1c8abc1185c88180cbc6d/crates/cowboy-protocol-codec/src/trading_post.rs#L536)
in the existing Cowboy protocol wire types. It supplies artifact 0, including
its existing `ibe_ciphertext`. The same visibility check
applies when artifact 0 is created: Public requires empty bytes and Private
requires nonempty bytes. Store artifact registration supplies the field through
`RegisterArtifactArgs`. Consensus checks only this empty/nonempty condition.

##### Serving roots and the Public volume pin

**Serving roots.** `DeclarationVersion.manifest_root` is the immutable
publisher source root. Artifact 0 MUST record this root. A Private Store
artifact MUST record it as a signed source-content claim (§2.5.1), not as
the actual committed root of its destination volume. A Private copy can have
a different destination commitment because its descriptors and encryption bind
to that volume. At registration, every Store destination `StorageCommitment`
MUST exist, be `Active`, and belong to the Store operator. A Private version
also requires a `Private` destination. Its `claimed_root` MUST equal the
version source root; its destination commitment root need not equal that
claim. The §2.2 ownership-consent rule also applies. Node PR #1708 at
`e89078ff` implements these Private destination checks without requiring
destination-root equality.

Public and Private here refer to the version's `code_visibility`. For a Public
Store artifact, `claimed_root` MUST be the actual committed
manifest root of its destination `volume_id`. It MAY differ from the source
root: CBFS file descriptors bind to a volume, so an equivalent copy in a
different volume can have a different manifest root. Both roots MUST be
nonzero. At registration, the destination `StorageCommitment` MUST also have
`Public` visibility and `manifest_root == claimed_root`. The existing
authority, grant, and artifact uniqueness checks still apply. `ibe_ciphertext` is an existing
field of the `RegisterArtifactArgs` wire type and `ServingArtifact` record.
The registrant supplies these opaque bytes; consensus only checks that they
are empty for Public artifacts and nonempty for Private artifacts. This check
does not validate a Private DEK wrap or prove copy equivalence. The actual
volume DEK material and release rules remain on `StorageCommitment` (§2.5.1).
This CIP assigns the field no content, encoding, or consumer. It is kept for
wire compatibility, and the empty/nonempty rule only tags visibility. It is not
a security control, and no key release reads it.

This destination lookup is an additional consensus point read. The
`TradingPostRegisterArtifact` access reservation is Rk=13, Wk=6 in access-weight
table version 16 at Node PR #1708 commit `13de6a65`. This includes the
Public-volume pin and the permanent §2.2 reverse index. The same table
reserves Rk=3, Wk=4 for `TradingPostDeclare` and Rk=5, Wk=4 for
`TradingPostDeclareVersion` because artifact 0 also enters that index.
Validators MUST activate this validation and pin with the final measured
access table at regenesis (§11 of CIP-43); mixed old and new validation is not
supported.

Conformance tests MUST reject an absent destination commitment (including when
source and destination roots match), a wrong owner, and non-Active status for
either visibility. Public registration MUST reject a Private destination,
destination-root mismatch, and either zero root. Private registration MUST
reject a Public destination or a claimed root different from the version source
root; it MUST accept a distinct Private destination root when all other
conditions hold. A valid Public commitment MUST reject nonempty ciphertext and
accept empty ciphertext when all other registration conditions hold.

For a Public Store artifact, the signed registration binds
`(declaration_id, version)` to the destination
`(volume_id, claimed_root)` and identifies the responsible Store operator.
The immutable version identifies the source root. Before registration, the
Store MUST verify that the pinned source and destination contain equivalent
paths, entry types, file bytes, content types, metadata, and link targets.
This verification is outside consensus. Registration records the Store's
equivalence claim; it does not prove that claim (§4). This amendment adds no
transaction field or attestation type; `ibe_ciphertext` predates it. A
per-volume pin count includes one reference for each Active Public Store
artifact, each non-`Revoked` hire pinned to a Public Store artifact regardless
of whether that artifact is Active or Retired, and each `Pending` Forked spawn
pinned to such an artifact, even after its finalize deadline until its state
becomes terminal. Registration and Shared hire creation each add one reference;
retirement and irrevocable hire revocation
each remove one. A Forked spawn adds its reference when it becomes Pending,
transfers that reference to its Hire at finalization, and removes it on
reclaim. Each update occurs exactly once and atomically with its state
transition. The count is a JSON `u64` in Trading Post actor storage under
`public_volume_artifacts:` followed by the 32-byte `volume_id`; zero removes
that key. It is separate from the RAS `StorageCommitment` and survives its
status changes and rent-driven garbage collection while nonzero. Direct manifest
commits, staged finalization, volume-owner transfers, and owner-initiated
volume deletion MUST reject while this Store-volume pin count is nonzero.
An owner `delete_object` operation that changes the committed manifest follows
the direct-commit guard. `delete_object` is not a separate onchain instruction:
it submits a manifest commit, so it uses the `CommitManifest` or staged-finalize
access row (CIP-43 §5.5).
Direct-hire artifact 0 has no Store registration and does not enter this count.
Retirement stops new hires but MUST NOT
release the pin for existing hires. `Active`, `Paused`, and `Lapsed` hires retain
it: a paused hire can resume, a lapsed hire can renew, and a OneShot hire can
serve after retirement. The pin is released only after the artifact is Retired
and every hire pinned to it is irrevocably `Revoked` (and no pending Forked
spawn can finalize into a hire). The release transition MUST be atomic with
the last such terminal hire or spawn transition. The existing RAS identity
rule rejects `CreateVolume` for any occupied deterministic `volume_id`. A
garbage-collected volume retains its commitment as a terminal ID tombstone;
neither owner deletion nor rent-driven garbage collection permits recreation
under a live hire. This identity rule does not require a new pin read on
creation. Rent settlement and expiry remain unchanged; this pin does not prove
availability. Node PR #1708 at `d60babed` implements the hire-lifetime count
for Shared hires and rejects owner deletion while the count is nonzero. Its
delete row was introduced in access table version 15 and remains in version 16
(§5 of CIP-43). Forked spawning is not implemented there. `reclaim_spawn` is
hirer-only, and an abandoned `Pending`
spawn has no automatic pin release after its deadline. The terminalization
rule remains unresolved and is required before Forked Public Store copies
can activate. The hire and revoke access rows were introduced in table
version 14 and remain in version 16 (§5 of CIP-43).
Node rent settlement can still move a pinned volume to `GarbageCollecting` and
destroy its bytes. A pin prevents owner deletion, root mutation, transfer, and
ID reuse; it does not stop rent-driven garbage collection. The protocol has
not selected a rule for keeping such a volume funded or for ending a perpetual
OneShot serving obligation when its bytes are lost. Until that decision and
its Node implementation are complete, this amendment cannot claim perpetual
availability for Public Store hires. A non-`Revoked` OneShot or Lapsed hire
retains its pin indefinitely under the current lifecycle; revocation is the
only defined terminal hire transition and can strand a OneShot hirer (§2.4).
The pin covers only Public Store artifacts. A Private Store artifact's
destination volume has no consensus pin: the Store operator, which owns it, can recommit, transfer, or
delete that volume while Private hires pinned to the artifact remain live, and
those hires then mount whatever the volume holds. The signed source-root claim
(§2.5.1) is checked only at registration; nothing onchain ties the destination
volume to that claim afterward. Private destination roots are not required to
equal the claim, so the Public root-equality guard cannot apply unchanged. This
is an unresolved gap: until a Private destination rule and its Node
implementation are selected, this CIP does not claim that a Private Store copy
remains stable for its live hires.
Conformance tests MUST reject pinned-volume deletion and any attempt to create
an already occupied `volume_id`, including a garbage-collected tombstone. They
MUST also reject
a pinned staged finalize when its proposed root equals the current root.
Regenesis makes preexisting-hire migration unnecessary (§11 of CIP-43). For Shared/legacy content, a Store
approval MUST apply to the serving `claimed_root` (§2.7), not only to the
source root. Forked-capable versions retain their `ExecutableV1`
attestation requirements, which bind the source version but do not cover a
distinct Public serving root. This CIP does not yet define a complete
attestation rule for Forked hires that use a Public Store copy. That combination
requires a serving-root binding before it can claim attested code.

Read surface (read-only methods, callable in-PVM via `call()` and exposed
over node RPC / the indexer for off-chain readers — the in-PVM path is a
**named change in this release**: host cross-call interception today
reaches only `0x1D` and the stream-key manager, so this release extends
it with a read-only, deterministic bridge to exactly these methods.
Bridge semantics pinned: per-method argument/result codecs with golden
vectors; metered as ordinary cross-call reads at the CIP-3 read
schedule; unknown method / malformed arguments → `InvalidInput`, absent
records → `NotFound` (a `HostError` the caller observes, never a fault);
strictly no state mutation, enforced by construction (the bridge
dispatches only to the read-surface handlers). The §2.6.4 activation
gate depends on this bridge): `get_declaration(id)`,
`get_version(id, v)`, `list_artifacts(id, v)`, `get_grant(id)`,
`get_hire(id)`, `get_pending_spawn(hire_id)`, `get_consent(declaration, version)`, `get_reservation(reservation_id)`, `list_grants(declaration)`, `list_attestations(subject)`, `get_serving(hire_id)` (returns `serving_actor`).

Neutrality invariant: there is no method by which any party other than (a) the
creator, for their own declarations, versions, and grants, (b) a store, for
artifacts it registers under its own active grant and for hires routed through
its own grants, (c) the hirer, for their own hire's funds and renewal
choices, or (d) a listed or template actor (itself or via its owner), for
consent on listings naming it (§2.1.3), can alter hiring state. The Trading Post's governance authority
(CIP-12) can upgrade code but holds no day-one operational powers over
individual listings.

#### 2.1.3 Listing consent — the listed actor must opt in

`declare` and `declare_version` authenticate the **creator** — and, without
more, only the creator. That is a confused-deputy hole: nothing stops an
attacker from declaring somebody else's actor as their own listing — or,
worse, declaring a victim actor as a Forked template with its *copied live
executable commitment*, offering it Free, and hiring it repeatedly. The
Trading Post would then send **genuine `0x1E` spawn directives** at the
victim: the template-side guard of §2.6.4 authenticates the sender as the
real Trading Post — which it is — so a provenance check alone cannot
distinguish an authorized listing from a hostile one, and the victim burns
`fork_count`, `children_count`, and quota on protocol-authentic messages.

The fix is explicit, bound consent, enforced at **both** ends:

- **Declaration level (the listed actor, Shared serving).** A declaration
  is not hireable until an `Active` declaration-level `ListingConsent`
  from the listed `actor` exists, binding `{declaration_id}`. No template
  guard exists at this level (no directives are involved), so this consent
  lives purely in Trading Post state and admits three recording paths
  with identical effect: a transaction whose sender is the actor's
  **owner** (read from committed chain state at execution), an
  actor-originated deferred instruction from the actor itself
  (`trading_post_consent` host function, the §2.6.2 mechanism), or —
  the zero-ceremony common case — auto-recording at `declare` when the
  caller (creator) is the actor's current owner.
- **Version level (the Forked template) — actor-originated ONLY, via a
  four-phase handshake.** The §2.6.4 spawn guard reads the consent tuple
  from the **template's own storage**, which the Trading Post cannot
  write; an owner-signed or auto-recorded consent would flip Trading Post
  state while the guard's source of truth stays empty. And the two legs
  can NOT commit "in one transaction": an actor-originated deferred
  instruction materializes as a **separate later transaction** —
  atomicity across the pair does not exist on this chain and is not
  claimed. The protocol is therefore an honest
  propose / provisional-commit / ack / confirm handshake with an
  actor-chosen nonce and a Trading Post CAS (the propose transaction's
  local write and its reserved enqueue — see revocation below for the
  reservation rule, which applies to every leg here — commit or roll
  back together; cross-transaction ordering is made causal by the
  confirm leg, never assumed from the scheduler):

  1. **Propose (template's transaction).** The template's owner-gated
     handler picks `expected_nonce` = its local consent counter + 1,
     writes the local tuple `{declaration_id, version, commitment_hash,
     nonce: expected_nonce, state: PENDING, reservation_id, attempt}`
     (the id and attempt stored locally so recovery can find the row —
     `get_reservation(reservation_id)` is the only bridge lookup and
     the preimage is not recoverable from the tuple otherwise), and enqueues
     `trading_post_consent(declaration_id, version, commitment_hash,
     expected_nonce)`. The guard rejects directives against a `PENDING`
     tuple, so nothing is spawnable yet. `commitment_hash` binds
     `keccak256(template_commitment ‖ instance_commitment codec bytes)`,
     so consent cannot be replayed onto a substituted commitment,
     profile, or version.
  2. **Provisional commit (Trading Post, at materialization).** The
     deferred consent executes as a **monotonic CAS**: it succeeds iff
     `expected_nonce` equals the subject's last recorded nonce + 1 AND
     the commitment matches the version's current commitments;
     concurrent or replayed proposals lose the CAS deterministically. On
     success the `ListingConsent` is recorded **`Unconfirmed`** — NOT
     `Active`: no hire can be accepted against it yet.
  3. **Ack (mailbox, provenance rails).** The consent execution — success
     or failure — enqueues a **consent-ack directive** from `0x1E` to the
     template carrying `{declaration_id, version, nonce, result}`. The
     template's ack handler flips `PENDING → ACTIVE` on success (and
     deletes the tuple on failure), and in the same handler enqueues a
     **consent-confirm** deferred instruction back to the Trading Post —
     **atomically replacing the tuple's `{reservation_id, attempt}`
     pointer with the confirm reservation's** in that same transaction.
     The pointer invariant is global: the tuple always names the
     currently-outstanding leg's reservation — the pre-commit retry
     writes its fresh `{reservation_id, attempt+1}` when it re-proposes,
     and `ACTIVE → REVOKING` stores the revoke reservation's pointer the
     same way — because recovery's only lookup is
     `get_reservation(pointer)`, and a stale pointer sends it hunting a
     long-terminalized row.
  4. **Confirm (Trading Post).** The confirm instruction flips
     `Unconfirmed → Active`. Only now are hires — and therefore spawn
     directives — possible.

     The confirmation round-trip exists because **no delivery-order
     assumption survives contact with the actual scheduler**: mailbox
     delivery is priority-then-FIFO, and deferred instructions
     materialize from digest-ordered state — "the ack precedes any
     spawn directive" is not a property the chain provides. The confirm makes the
     ordering **causal instead of scheduled**: the template's local
     tuple is `ACTIVE` strictly before the confirm instruction exists,
     and the confirm commits strictly before any hire can create a
     directive — so every spawn directive finds an `ACTIVE` template
     regardless of priorities or interleaving, and no hirer's gas is
     ever consumed against a template still `PENDING`. Split-state
     outcomes remain fail-safe at every stage: `PENDING` or
     `Unconfirmed` states reject/admit-nothing; a lost ack or confirm
     leaves a listing that never activates and a proposal that can be
     retried at the next nonce.

  **Revocation mirrors it, local-first, with liveness made structural
  rather than promised.** The template sets the tuple to `REVOKING`
  (the guard rejects from this instant — safety never depends on the
  Trading Post leg) and enqueues the Trading Post revocation. Two
  requirements prevent a revocation from being lost. The current deferred
  path can silently drop an enqueue: gas
  shortfalls are logged-and-skipped, bounded-queue admission can
  reject, and a failed materialization has no durable retry — under
  those semantics the TP could stay `Active` indefinitely, selling
  hires whose spawns always dead-letter:

  - **Reserved enqueue, atomic with the local write.** The
    `trading_post_consent`/revoke host functions perform a **reserved
    enqueue**: queue admission and materialization gas are reserved at
    enqueue time, and if the reservation fails the *entire* enclosing
    transaction fails — the `REVOKING` (or `PENDING`) local write rolls
    back with it, and the template retries. Local state and an admitted
    instruction exist together or not at all.
  - **Admitted means materialized — or refunded, never dropped.** For
    this instruction class, admission is a consensus obligation with
    exactly two provable outcomes (the same statement as the
    reservation lane's, §2.1.3 — this bullet claims nothing stronger):
    an admitted instruction materializes at ≤ 2× its snapshot rates,
    or terminalizes `Expired` with full remaining refund and the
    defined same-generation retry. What is banned is the silent third
    outcome the current pipeline permits — skipped-for-gas,
    queue-dropped, logged-and-forgotten. A named requirement on the
    deferred pipeline, shipped with the release.

  No "one block later" bound is claimed — the bound is "at the admitted
  instruction's materialization or expiry," both consensus-visible. The residual between local `REVOKING` and TP revocation is
  economic and bounded: hires accepted in that window were not
  *knowingly* dead (revocation was not yet consensus state when they
  paid); their directives fail the guard's **first, cheapest check**
  (minimal consumption by construction — the tuple check precedes all
  other work), dead-letter, and resolve by reclaim — price in full, gas
  minus that minimal consumption. Directives already queued at
  revocation fail the same way.
- **The directive check.** Each `ListingConsent` carries a
  strictly-increasing per-subject `nonce`; the spawn directive embeds
  `{declaration_id, version, consent_nonce}` (§2.6.4) and the child
  envelope embeds `consent_nonce` (§2.6.4), so finalization also binds
  the authorization epoch. The template guard thus verifies not only *who
  sent* the directive (`0x1E`) but *what it is for* — a listing this
  template actually joined and has not revoked.

**Wire and replay rules for the handshake legs** — every leg is a typed,
bound message, not prose:

- **Consent-ack directive** (Trading Post → template, provenance =
  `0x1E` via the typed frame-origin): codec `b"cip33.cack.v1" ‖ declaration_id ‖ u32_be(version) ‖
  u64_be(attempted_nonce) ‖ u64_be(current_nonce) ‖ u8 result`
  (0 = committed-unconfirmed, 1 = CAS-failed; `current_nonce` is the
  TP's recorded nonce after the attempt — equal to `attempted_nonce` on
  success, the authoritative resync value on failure). Applies iff the
  template's local tuple matches `{declaration_id, version, nonce}`
  exactly AND `state == PENDING`; on result 0 → `ACTIVE` + enqueue
  confirm, on result 1 → delete tuple. Any mismatch — older nonce,
  newer nonce, wrong state, unknown listing — is a no-op rejection: a
  stale or replayed ack can neither activate nor delete a newer
  proposal.
- **Consent-confirm instruction** (template → Trading Post, its own
  protocol opcode on the reserved typed path; consensus sender MUST be
  the record's `subject_actor`): args `(declaration_id, version,
  nonce, commitment_hash)`. Applies iff the `ListingConsent` matches
  `{subject_actor == sender, declaration_id, version, nonce,
  commitment_hash}` — `commitment_hash` is carried and matched **here
  too**, not only at propose/revoke: confirm is the leg that actually
  flips the listing to `Active`, so omitting it would let a confirm
  bound to nonce `n` activate a commitment substituted between propose
  and confirm — AND `status == Unconfirmed` → `Active`. Stale/replayed
  confirms (wrong nonce, commitment mismatch, already `Active`,
  `Revoked`) reject without effect.
- **Every leg has an allocated opcode and canonical struct** — allocation is
  against the **full protocol uniqueness table**, not a local range —
  the pinned table's system block runs through 199 with the Bank block
  fixed at 200–216, and mid-range values that look free are not (157 is
  deliberately retired and MUST NOT be reused; 158/159 are system-DKG;
  160/161 are Container Registry). This amendment therefore allocates the first contiguous free
  block at protocol pin 76942600 — 217/218 are also taken
  (SYS_SUBMIT_GRANT_RELEASE_PROPOSAL, SYS_CBQS) —
  **219 = `trading_post_consent` (propose/commit)**,
  **220 = `trading_post_confirm`**, **221 = `trading_post_revoke_consent`**,
  and — because they are new instruction surfaces too —
  **222 = `finalize_spawn`**, **223 = `reclaim_spawn`**,
  each with the positional arg struct exactly as its method row states
  (`(declaration_id, version, commitment_hash, expected_nonce)` for 219 /
  `(declaration_id, version, nonce)` for 220 /
  `(declaration_id, version, commitment_hash, nonce)` for 221), canonical
  codec-bytes encoding, strict EOF. `DeferredReservation.instruction_kind`
  **is** the opcode and its `instruction bytes` are that canonical arg
  encoding — the reservation digest is therefore well-defined. The ack
  handler is the reserved template entrypoint `on_consent_ack`,
  dispatched under the typed `0x1E` frame-origin path exactly like
  `spawn_instance` (not `@public`). Validation order within each leg:
  decode → provenance/sender → tuple match → state CAS. All producer/decoder vectors regenerate with the allocation. Golden vectors
  per opcode; stale-ack, stale-confirm, cross-listing replay, and
  wrong-sender vectors are pinned.
- **One outstanding generation, no nonce gaps.** Per
  `(subject_actor, declaration_id, version)` at most ONE handshake
  generation may be in flight: `consent_listing` proposes are rejected
  by the template's own handler while a local tuple exists in any
  state, and by the Trading Post CAS while a `ListingConsent` sits
  `Unconfirmed`. Nonce namespaces are per-listing (§2.1's scoped nonce), so different-listing legs completing out of reservation order touch disjoint counters; same-listing generations are serialized by the one-outstanding rule — the gap scenario (n+1 materializes first, fails, and the local counter strands ahead of the TP's) cannot arise in any interleaving. Belt-and-braces resynchronization: the failure ack's `current_nonce` field is the Trading Post's
  recorded value, and the template resets its local counter to it when
  deleting the tuple — even a bugged or migrated template cannot stay
  desynchronized past one failed round. `revoke_consent` binds the
  full tuple: it CASes on exact
  `{subject_actor == sender, declaration_id, version, commitment_hash,
  nonce, status ∈ {Unconfirmed, Active}}` — a revoke can never hit a
  later generation than the one it names. Vectors: same-listing
  second-propose rejected at both ends; failure-ack nonce resync;
  revoke naming a stale nonce rejected.

**The reservation is a durable consensus object, not a promise.**
`DeferredReservation { reservation_id, subject_actor: Address,
instruction_kind: u16 (the opcode), instruction_bytes: Bytes (the
canonical arg encoding, PERSISTED — state alone materializes the
instruction), attempt: u32 (initial 0; also inside the id preimage,
and stored plainly because a hash preimage is not invertible),
snapshot_cycle_rate: u64, snapshot_cell_rate: u64 (the pricing
inputs, persisted), gas_reserved: u64, remaining_escrow: u64,
created_height, expiry_height,
status: Reserved | AckPending | Materialized | Expired,
expiry_reason: Option<Backlog | RateExceeded>,   // precedence: RateExceeded iff the entry was rate-blocked at its final budgeted scan, else Backlog
  evidence_consumed: bool,           // terminal-GC gate: recovery's read-and-apply sets it; see the GC rule
                                     // per-leg accounting: `completed` records the commit leg's consumption; the ack leg's is the remainder of consumed escrow
completed: Option<{leg: u8, result: u8, current_nonce: u64}> }`
— the complete encoded record, golden vectors per transition — with
`reservation_id = keccak256(b"cip33.rsv.v1" ‖ subject_actor ‖
u16_be(opcode) ‖ u32_be(len(instruction_bytes)) ‖ instruction_bytes ‖
u64_be(nonce) ‖ u32_be(attempt))` — domain-tagged, covering the opcode
and framed bytes (a propose and a revoke with coincidentally identical
argument tuples cannot collide), and carrying an **attempt counter** so
a same-generation retry mints a fresh row instead of colliding with its
own Expired predecessor. An `Expired` record additionally **persists
the outcome of any leg that completed before expiry** —
`completed: Option<{leg, result, current_nonce}>`:

- **Creation** is atomic with the local tuple write, in the propose /
  revoke transaction's write-set (both-or-neither). `gas_reserved` is
  not caller-chosen: each instruction kind carries a **fixed pinned
  budget** — `CONSENT_LEG_CYCLES = 2_000_000` and
  `CONSENT_LEG_CELLS = 4_096` per leg it funds (the propose
  reservation covers two legs — commit + ack — so reserves twice the
  pair; confirm and revoke reserve one), executed in the **System
  lane** (CIP-3 §2.2.3). Pricing: the reservation persists its
  **per-dimension snapshot rates** (cycle and cell, creation block) and
  reserves `gas_reserved = budget × snapshot_rate ×
  RESERVATION_RATE_MARGIN` (margin = 2, checked u128 → u64), tracking
  `remaining_escrow` in the record as legs consume. Execution always
  charges the **live lane-effective basefee in full** — never below it;
  a discount would execute under the required basefee and leave
  burn/distribution unfunded, which no lane can subsidize (a lane is
  capacity, not money). If the live rate exceeds `2 × snapshot` in a
  block, the reservation simply **waits** (it stays admitted, older
  entries keep priority); if the margin is still exceeded at
  `expiry_height`, it terminalizes `Expired` with reason `RateExceeded`
  and full remaining refund — feeding the same recovery path as any
  expiry. "Admitted ⇒ materialized" is therefore stated precisely:
  admitted ⇒ **materialized at ≤ 2× snapshot rates, or expired with a
  clean refund and a defined same-generation retry** — provable from
  state either way. Debited from the enqueuing actor. The reservation
  consumes one slot of per-actor capacity
  (`MAX_RESERVATIONS_PER_ACTOR = 16`, **released when the reservation
  terminalizes — `Materialized` or `Expired` — not held forever**) and
  **aggregate per-block lane resources — admission is by resource, not
  count** (a count bound proves nothing about capacity and none is
  used): each block schedules pending reservations, oldest first, only
  while their summed **cycle** budgets fit the reservation sub-budget
  of the System lane — `RESERVATION_LANE_CYCLES = 8_000_000`. Admission
  is cycles-only because lanes are a **cycle** partition in consensus
  (`types/src/constants.rs` defines `LANE_*_CYCLES` and no per-lane cell
  budget exists); the legs still consume cells, charged at execution per
  CIP-3, but cells are not a lane-admission denominator (an earlier
  `RESERVATION_LANE_CELLS` figure named a budget the fee model does not
  have — removed). It is a **carve-out of the System lane, not an addition**: one shared
  `LANE_SYSTEM_CYCLES = 40_000_000` counter governs the whole lane;
  each block schedules reservations FIRST in the system section, up to
  their sub-budget caps, and ordinary System traffic fills whatever
  remains (≥ 32M when reservations are saturated; unused reservation
  budget is naturally borrowable because the counter is shared —
  no double-accounting, total lane consumption can never exceed
  CIP-3's capacity; cumulative per-block accounting under a
  SystemState key).
  Creation fails the whole transaction on per-actor-slot, balance, or
  **global-capacity** failure — the ONE combined counter of the
  retention rules below (`reserved + unconsumed_terminal ≤
  RESERVATION_CAPACITY = 4_096`, stored at SystemState key
  `keccak256("cip33:rsv_capacity")` as a u32 little-endian value —
  incremented exactly once at creation, decremented exactly once at
  the FIRST of {evidence consumption, tombstone GC, auto-resolution},
  the once-only guarantee riding the same consumed-flag CAS that
  gates those transitions; increment/decrement transition vectors —
  no separate outstanding cap exists; per-actor 16 alone bounds
  nothing against many actors); lane pressure defers scheduling, never admission.
  **Validator work is bounded by construction**: pending reservations
  live in an explicit FIFO index. **The index MUST be prefix-scannable,
  which forbids a raw >32-byte composite key** (`storage/src/state_key.rs`'s
  `ACTOR_STORAGE_SCANNABLE_MAX = 32` collapses any longer user key to
  `keccak256(key)[..32] ‖ 0xFF` — point reads still work but the row
  becomes invisible to prefix scan and ordering is lost, which would
  silently turn every sweep here into a permanent no-op; this is the
  confirmed COW-2902 failure shape and applies to all three indexes in
  this section). Each index is therefore a **two-level structure under
  its own `StatePrefix`**: an ordering key of `u64_be height` (8 bytes —
  the FIFO/due-height bucket) whose value is an append-ordered list of
  the `reservation_id`s at that height, plus the `DeferredReservation`
  record itself keyed by `reservation_id`. Heights are dense and walked
  in order; ties break by list order (creation order). No scan key
  exceeds 8 bytes. Row written at creation, deleted at terminalization —
  no unindexed scans exist — and each block walks the ordering keys for
  at most `RESERVATION_SCAN_BUDGET = 512` entries (entries
  beyond the budget wait, order preserved), and expiries are processed
  by the same walk — an entry past `expiry_height` terminalizes when
  the budgeted scan first reaches it (a bounded lazy sweep, not a
  second pass). **Precedence, pinned:** an entry that is both
  schedulable and past expiry at the same height **materializes** —
  execution wins over expiry (deterministic; expiry is the fallback,
  never a race). **Head-of-line rule, pinned:** an over-rate FIFO head
  (rate cap below) is **skipped for that block and keeps its queue
  position** — eligible younger entries within the scan budget execute
  ahead; deterministic (the rate check reads consensus state), and
  chosen over stopping, which would let one over-rate entry
  head-of-line-block the lane for up to its whole TTL.
- **Funding scope and leg states.** One propose reservation funds its
  whole causal tail on the Trading Post side: the commit execution AND
  the consent-ack directive's delivery to the template (the ack has no
  other payer — the Trading Post owns no funds). The two legs are
  tracked, not conflated: the commit execution CASes
  `Reserved → AckPending` (no refund yet — the ack is unfunded work
  still owed), and the ack's delivery CASes
  `AckPending → Materialized`, at which point the unconsumed remainder
  of `gas_reserved` refunds to the enqueuing actor. Each CAS consumes
  its leg exactly once — a replayed commit or ack finds the wrong
  state and is invalid. Consumption is charged per leg at
  execution-block rates under the reservation's rate cap (below);
  consumed fees burn/distribute as ordinary CIP-3 fees. The confirm
  leg is funded by its own reservation (created in the template's
  ack-handler transaction, same rules, single-leg: `Reserved →
  Materialized`).
- **Materialization** is deterministic and bounded: pending
  reservations execute in FIFO order by `(created_height,
  reservation_id)` in each block's deferred section, up to the global
  per-block budget; a reservation beyond the budget carries to the
  next block ahead of younger ones. "Admitted ⇒ materialized" is
  provable from state alone. Being consensus state, reservations need
  no node-local queue and behave under restart/reorg exactly as all
  state does (roll back with their block).
- **Expiry** (`expiry_height = created_height +
  RESERVATION_TTL_BLOCKS = 600`) is the only terminal failure, and its
  stall model is exactly the backlog rule above: with next-block
  execution ahead of younger entries, an unexpired unmaterialized
  reservation exists **only** while the global budget has been
  saturated by older entries for every block since creation — 600
  blocks of sustained saturation. At expiry the record flips
  `Expired`, refunds the remaining `gas_reserved`, releases its
  capacity slot, and emits an event; the local tuple remains in its
  safe state (`PENDING` / `REVOKING` — both reject directives).
  **Terminal rows are bounded, not immortal**, by a mechanism that can
  actually find them: terminalization moves the record's index row from
  the scheduling FIFO into a **separate terminal-retention index**
  keyed `(terminal_height, reservation_id)` — retention rows never
  consume scheduling scan budget, and the walk that prunes them is its
  own, with its own cursor and `RETENTION_SCAN_BUDGET = 128` per block
  (nothing is ever findable only by state scan). Consumption rules are
  automatic where recovery is moot: `Materialized` rows set
  `evidence_consumed = true` at terminalization (nothing to recover);
  `Expired` rows are consumed by the recovery transaction as before.
  Pruning: consumed rows GC `RESERVATION_RETENTION_BLOCKS = 600` after
  terminalization; an **unconsumed** row past retention does NOT vanish into a generic
  not-found (that would erase exactly the fact recovery branches on —
  whether a commit happened — making nonce/state/gas depend on GC
  timing, and giving `REVOKING` no safe branch at all): pruning an
  unconsumed row replaces it with a **minimal TAGGED tombstone** —
  codec `u8 leg (0 = propose-commit, 1 = confirm, 2 = revoke; other
  values reject) ‖ u8 has_completed (0/1; other values reject) ‖
  (u8 result (0 = success, 1 = CAS-failed; other values reject) ‖
  u64_be current_nonce iff has_completed = 1)`, 2 or 11 bytes, strict
  EOF (trailing bytes reject), the tag making pre-commit
  (`has_completed = 0`, recovery re-proposes same-generation) and
  post-commit (`= 1`, recovery applies the evidence as the ack)
  decodable interoperably; the RPC returns the same tagged union.
  Recovery consumes a tombstone exactly as it would the full record.
  **Every phase transition reindexes to its next due-height** — the
  forward cursor only ever needs to look ahead, never back, and never
  holds: tombstone **creation** (at retention expiry of an unconsumed
  row) deletes the `(terminal_height, id)` row and writes
  `(resolution_due_height = terminal_height +
  TOMBSTONE_PUBLIC_RESOLUTION_BLOCKS, id)` — without this, an
  absent-template tombstone would sit at a key the cursor has already
  passed and auto-resolution could never reach it, which was precisely
  the hostage scenario; **consumption or auto-resolution** reindexes
  again to `(gc_due_height = that_height + RESERVATION_RETENTION_BLOCKS,
  id)`. Each phase has its **own `StatePrefix`** and the same two-level
  8-byte-height ordering as the scheduling index above (never a
  `phase ‖ height ‖ id` composite scan key — that would exceed the
  32-byte scannable limit); a row is always findable at exactly one
  upcoming height in exactly one phase index. Tests:
  absent-template at 4,096 rows (slot 4,097 admits at exactly
  `terminal_height + 7_200` — the D−1/D boundary vector pins the
  anchor; never before), and cursor monotonic-progress under mixed
  phases. **Capacity is never
  hostage to an absent template**: a tombstone unconsumed by
  `terminal_height + TOMBSTONE_PUBLIC_RESOLUTION_BLOCKS (= 7_200)` —
  **the ONE anchor, everywhere**: the same `terminal_height` the due-key
  formula uses; neither reservation creation nor tombstone-creation
  height ever anchors this window (three candidate heights exist and
  validators disagreeing on the anchor would release the global slot —
  and admit slot 4,097 — at different blocks) — is
  **auto-resolved by the retention walk itself** — deterministic, free,
  no new instruction: the walk marks it consumed, releases its
  capacity slot, and reindexes it as above; the recovery facts remain
  readable until its GC, and a template returning even later
  reconciles by the **complete matrix** (local state × `get_consent`) —
  generic clear-and-re-propose is NOT the rule, because it is wrong for
  revocation (clearing a `REVOKING` tuple against a TP `Active` would
  leave hires accepted forever while every spawn dead-letters):
  `PENDING × missing/Superseded` → clear, optionally re-propose;
  `PENDING × Unconfirmed(nonce match)` → apply the ack path (evidence
  or supersede at next propose); `PENDING × Active` → adopt TP state
  (flip local `ACTIVE`, re-enqueue confirm-idempotent no-op);
  `ACTIVE × Unconfirmed` → re-enqueue confirm; `ACTIVE × Active` →
  done; `ACTIVE × Revoked/Superseded` → clear (delisted);
  `REVOKING × Unconfirmed/Active` → **retry the revoke** (fresh
  reservation, same generation-binding CAS — never re-propose);
  `REVOKING × Revoked` → delete tuple. Every cell is a vector. 4,096 abandoned
  templates can therefore delay capacity by at most the resolution
  window, never hold it forever. `get_reservation` = not-found
  retains ONE meaning — no reservation was ever created — and is
  unreachable from a valid local pointer within the resolution
  window; past it, the reconcile-from-`get_consent` rule governs. The supersede rule remains for the one orphan a
  crashed template could still leave: a propose CAS at `n+1` marks a
  still-standing `Unconfirmed(n)` as `Superseded` and proceeds —
  `Superseded` is a first-class `ListingConsent.status` value (schema
  updated) with exactly one inbound transition (that CAS) and none
  outbound. Global bound:
  unconsumed-per-listing ≤ 1 (the retry consumes its predecessor in the
  same transaction) AND a **combined capacity counter** `reserved + unconsumed_terminal ≤
  RESERVATION_CAPACITY = 4_096`: every propose/confirm/revoke creation
  reserves its terminal-capacity slot up front (a live row is a future
  terminal row — terminalization itself can never fail, so the slot
  must exist before the row does), and the slot releases only at
  evidence consumption / tombstone GC. One counter, one cap, enforced
  at creation for all three legs — bounded against unbounded listings,
  and against the all-4096-expire-at-once shape a separate outstanding
  cap would miss. The
  recovery path is explicit and **branches on what completed**, read
  over the §2.1.2 bridge (`get_reservation`, in the read-surface list):
  - **Pre-commit expiry** (`Reserved`, `completed = None`): nothing
    reached the Trading Post. The handler clears the stale local tuple
    and creates a fresh reservation for the SAME generation with
    `attempt + 1` — one transaction, no window in which the guard
    accepts anything, no nonce consumed.
  - **Post-commit ack loss** (`Expired` from `AckPending`,
    `completed = Some{commit result, current_nonce}`): the Trading Post
    already holds `Unconfirmed(n)` — re-running the proposal would
    correctly fail every guard, and MUST NOT be attempted. The
    persisted `completed` value IS the ack content: the handler applies
    it exactly as the ack directive would (success → local `ACTIVE` +
    enqueue confirm; failure → delete tuple, reset counter to
    `current_nonce`) — ack redelivery by state read, no new message
    class.
  - **Lost confirm**: local `ACTIVE` + TP `Unconfirmed` persists only
    while the confirm reservation is un-terminalized; on its expiry the
    template re-enqueues the confirm at `attempt + 1`
    (`Unconfirmed → Active` CAS idempotent against the same nonce — a
    duplicate is a no-op rejection, not a fault).
  Vectors: pre-commit expiry → same-generation retry succeeds;
  post-commit expiry → state-read ack application activates without
  re-proposal; expired-confirm re-enqueue activates. One stale
  conformance test is corrected alongside: same-listing concurrent
  proposals reject at propose (no CAS loser exists to receive an ack),
  and different-listing proposals both succeed in disjoint nonce
  namespaces — the old "loser's ack deletes its tuple" test described a
  state the protocol no longer produces.

Conformance tests: third-party declaration of an unconsented actor
(declaration created but permanently unhireable; hire rejects), a Forked
version whose template never consented (version inactive; hire rejects),
consent revoked between hire and spawn (directive rejected by the guard,
escrow reclaimable, no `fork_count` consumed), commitment substitution after
consent (`commitment_hash` mismatch — version inactive), version-number
substitution, two declarations naming one template (each needs its own
consent), and quota/counters unchanged on every rejection path.
Handshake machinery tests: directive against a `PENDING` or
`Unconfirmed`-side tuple rejects; hire against an `Unconfirmed` consent
rejects; same-listing concurrent proposals reject at propose (no CAS
loser exists to receive an ack — §the recovery bullet);
failed-commit ack deletes the tuple and resyncs the counter; lost
ack/confirm → listing never activates until the same-generation retry
(reservation-expiry path) completes it; adversarial delivery interleavings (spawn-priority vs ack)
never find a `PENDING` template after `Active` consent (causality, not
scheduling); reserved-enqueue failure rolls back the local write;
revocation gap-window hire dead-letters on the guard's first check and
reclaims (price in full, gas minus minimal consumption).

#### 2.1.4 Hireability badges are discovery metadata

`badges.human_hireable` / `badges.agent_hireable` are **advisory discovery
metadata, normatively unenforced**: `hire` admits any caller kind regardless
of badge state, and no conformant implementation may reject a hire on badge
grounds. This is deliberate, not an omission. Enforcement would require
classifying the payer as human or agent from consensus state at hire
execution, and that classification is a fiction: a human drives an actor
with one deploy, an actor's owner hires through an EOA with one signature —
either badge is a one-hop proxy away from meaningless, while the
classification edge cases (actor-origin deferred hires, contract-less
accounts) would be real consensus surface. Badges tell stores and clients
what the creator *intends* and how to render the listing; filtering on them
is a client decision, like attester trust (§2.7).

### 2.2 Direct hire and artifact pinning

`hire(declaration, grant = None, ...)` settles against the pinned version's
`default_split` (typically 100% creator) and initializes the lease with the
protocol default `lease_epoch_blocks = 1800`. Stores MUST NOT be required for
any flow in this CIP. Direct-hire pause/resume/revoke is creator-controlled
under the restricted rule of §2.4 (`Free`/`PerCall` terms only).

Artifact pinning at hire: a granted hire MUST pin the granting store's
`Active` artifact for the pinned version (unique per §2.1.2's
one-Active-artifact invariant, so selection is deterministic) — if the store
has registered none, the hire is rejected. Falling back to the creator's artifact
0 is **direct-hire only**: a store-routed hire that silently served
unreviewed creator-volume code would carry the store's trust without the
store's review. The pinned artifact is the only volume the hire's instances
run and the only escrow the lease will service (§2.5.1). Registration
also writes a **permanent `volume_id → (registered_owner, declaration,
version, artifact_id)` reverse index** — keyed by the stable
`volume_id` alone, everywhere in this section — and that index is safe
by three pinned rules, because an unauthenticated writable index is itself a weapon (a
store registering someone else's volume would make CIP-24 fail-close
that owner's ordinary unbound jobs): (i) **ownership consent** —
`register_artifact` verifies the volume's `StorageCommitment.owner`
equals the registrant's authority (the creator for artifact 0; the
store's operator account for snapshot artifacts) and rejects
otherwise; (ii) **permanent uniqueness, keyed by `volume_id` alone** — one
volume maps to at most one artifact, ever: a second registration of
the same `volume_id` under ANY owner, artifact, or version rejects (no
overwrite, no set — reclassification is impossible by construction,
including after a CIP-9 ownership transfer), and the row is
tombstoned like every §2.6.4 index; (iii) **exact codec, keyed by the stable identity** — key =
`volume_id` alone (NOT `(owner, volume_id)`: CIP-9 supports ownership
transfer, which mutates `StorageCommitment.owner` — an owner-composite
key would fail open after a transfer, missing the lookup under the new
owner and even permitting a second classification); value =
`registered_owner(20) ‖ declaration_id(32) ‖ u32_be(version) ‖
u32_be(artifact_id)` (the owner at registration, recorded as history,
never used as the lookup key), RPC `get_artifact_by_volume(volume_id)`
returning exactly that tuple or not-found. Global uniqueness is by
`volume_id`, transfer-proof by construction. `cbssd` classifies by one
indexed read — never a scan — which is what lets CIP-24's "refuse
unbound jobs for registered volumes" rule fail closed. Lifecycle
vectors: foreign-volume registration rejected; duplicate-volume
registration rejected across versions and stores; classification
unchanged after artifact retirement, declaration retirement, and —
the transfer cases — a CIP-9 ownership transfer before and after a
running job (classification holds through both; no second
registration becomes possible).

Node PR #1708 at `13de6a65` implements this permanent reverse index,
including artifact 0, and exposes the indexed volume lookup through Node RPC.
The separate `cbssd` classification consumer still needs to use that lookup
before the CIP-24 fail-closed rule is complete.

### 2.3 Split vectors

```
SplitVector = [ (beneficiary: Address, share_bps: u16) ]   // Σ share_bps = 10_000
```

- Validated at `declare`/`declare_version`/`grant` time: shares sum to
  exactly 10,000 bps; max 8 entries; no zero-share entries.
- **Pinned at hire.** The effective vector (grant or default split, plus any
  referral entry) is copied into the `Hire` and every subsequent `renew` and
  `settle_per_call` settles against the pinned copy. Editing a grant's split
  affects only future hires.
- Executed atomically inside `hire`/`renew`/`settle_per_call`: the Trading
  Post receives or draws the payment (CBY or a CIP-20 token named in the
  term) and transfers each beneficiary's share in the same method execution.
  Rounding dust (the remainder after integer division) accrues to the first
  entry (by convention, the creator).
- **Settlement is direct ledger movement.** Trading Post transfers mutate
  balances directly and do NOT invoke CIP-20 transfer hooks — a multi-leg
  split must not be abortable or reenterable by a beneficiary's hook. Hook
  semantics consequently do not fire on hire settlement; tokens whose
  economics depend on transfer hooks should not be offered as pricing
  denominations. Standard CIP-20 transfer events ARE emitted per leg, so
  indexers observe every movement. A settlement fails closed as a whole if
  any participant's token account is frozen.
- **Referral and creator codes are not a mechanism.** At hire time the client
  MAY append at most one referral entry of up to the grant's
  `max_referral_bps`, deducted exclusively from the store's share; the pinned
  vector still sums to 10,000. The protocol has no opinion about what codes
  mean.
- The protocol itself takes **no cut**. Chain revenue from hiring is gas and
  compute, as for any other actor activity.

### 2.4 Pricing terms

```
PricingTerm =
  | OneShot      { price, token }                   // perpetual access; survives retirement (below)
  | Subscription { price, period_blocks, token }    // paid_through advances per renewal
  | PerCall      { price_per_call, token }          // metered via MPP session (§2.6.3)
  | Free
```

`token` is a `PaymentAsset` (§2.1 — `NativeCby` or `Cip20 { token_id }`,
never a bare address); all amounts are u128 base units.

**Prepaid balances fund renewals.** No method ever pulls from a hirer's
wallet or actor balance: money enters a hire only as an explicit payment
attached to `hire` or `deposit`. `renew` draws each period's price from the
hire's available balance at the period boundary — callable by anyone, typically a
CIP-5 timer, since its effect is deterministic and boundary-gated.
Insufficient available balance → `Lapsed` (lease stops; recoverable by
`deposit` + `renew` while declaration and grant remain Active). Available
balance is `prepaid − reserved`; `withdraw_unused` returns it to the hirer at
any time, and cancellation is `set_renewal(false)` + `withdraw_unused` — the
current period stays honored.

Term semantics:

- **OneShot** sets `paid_through = ∞` and **survives retirement**: the
  creator's escrow and serving obligation for already-sold one-shot hires is
  perpetual. Creators who want a sunset path MUST price as `Subscription`.
- **Subscription** advances `paid_through` per successful `renew` by the
  **gap-absorbing rule**: `paid_through := max(paid_through,
  current_block − 1) + period_blocks`. The boundary is pinned against
  the validity rule as written (§2.5.1: valid while
  `current_block ≤ paid_through`, inclusive): `renew` is a silent no-op
  **while `current_block ≤ paid_through`** and becomes eligible only at
  `current_block > paid_through` — so an anyone-callable renew can
  never mutate, and never `Lapse` a hire, inside already-paid time,
  including the final paid block. At seamless first-eligibility
  (`current_block = paid_through + 1`) the formula gives
  `paid_through + period_blocks` — contiguous coverage, exactly
  `period_blocks` newly paid blocks; after a gap it gives
  `current_block − 1 + period_blocks` — coverage starts at the draw
  block, same exact period length. `Lapsed` is set only by an
  *eligible* renew finding insufficient funds; boundary vectors at
  `paid_through`, `paid_through + 1`, and a gapped draw pin all three.
  One draw buys one period of *future* service, always — never a dead
  period. Without the `max`, an
  anyone-callable `renew` on a long-stale hire would advance the old
  boundary by one period per call, each drawing a full period's price
  for time already past: repeated calls drain the entire prepaid balance
  for zero service. With it, the first post-gap renew starts the period
  at the current block and the next renew is a boundary-gated no-op
  until that period ends. Drain vector: stale hire + N rapid `renew`
  calls → exactly one draw.
- **PerCall** ignores `paid_through`; validity is an open, funded MPP session
  (§2.6.3), where "funded" means the session's reservation is covered by
  `reserved`. Reservations move balance from available to `reserved` at
  session open and release at settlement or session close, so
  `withdraw_unused` can never strand an open session.
- **Free** carries no money; validity is simply declaration ∧ grant (if any)
  ∧ hire all `Active`. `Private + Free` is legitimate (e.g., a free private
  beta).

**Consumer protection (creator-side actions).** `retire_declaration` and
`revoke_grant` stop *new* hires but MUST NOT strand paid time — existing
hires remain serviceable (lease shares keep issuing, §2.5) until
`paid_through`, indefinitely for OneShot. A creator cannot rug active
subscribers; they can only decline future renewals.

**Trust-and-safety actions (store-side)** are the deliberate exception:
`pause` and `revoke_hire` MAY stop service inside paid time (§2.5.2–2.5.3).
On `revoke_hire` the Trading Post auto-refunds the hire's available balance
to the hirer. Funds already split for the in-progress period are not clawed
back at the protocol level — per-period escrow of executed splits is
complexity v1 deliberately omits (Open Questions); compensating the
in-progress period is store-level policy. Stated honestly: a store revoking a
`OneShot` hire strands the hirer's perpetual access with no protocol remedy
beyond the (typically zero) available-balance refund — clients SHOULD weight
store trust accordingly, and stores compete on their published compensation
policy.

**Creator-side direct hires get no such exception.** For direct hires, the
creator may `pause`/`revoke_hire` only `Free` and `PerCall` terms (nothing
prepaid is stranded; reservations settle first). Paid `Subscription` and
`OneShot` direct hires are not creator-revocable at all — the creator's exit
is declining renewal (`retire_declaration`); refusing an abusive hirer is the
actor's own application logic, not a protocol power that doubles as a rug.

### 2.5 Key leases (private-code actors)

#### 2.5.1 Mechanism

For `code_visibility = Private`, every `ServingArtifact`'s volume is an
ordinary **CIP-9 private volume**, and its key escrow is the standard
CIP-24 **volume-DEK wrap, unmodified** — the implementation MUST NOT introduce a
bespoke `tradingpost/{…}` IBE identity family, which CIP-24 does not
define (its identities are the account/secret and volume-DEK families,
each with required DST, AAD, epoch, and rewrap semantics this CIP has
no business duplicating). The reserved Trading Post *secret key name* in the
CBSS whitepaper does not define a volume-DEK identity and is not used to wrap
this artifact's volume key. The registrant (creator for artifact 0, store
for snapshot artifacts) wraps the volume's DEK exactly as any CIP-9
private volume's owner does — same identity derivation, same
DST/AAD/epoch fields, same rewrap machinery. Concretely, the volume's own `StorageCommitment` carries
`wrapped_dek` (WrappedDek v2), `cbss_release_key_material_hash`, and
`cbss_rewrap_required`, exactly per CIP-24 §9.3; the artifact record
stores `volume_id` and its existing `ibe_ciphertext` field, but no
volume DEK wrap or new `WrapRef`. What this CIP adds is **authorization only**: for a volume registered
as a serving artifact, the CIP-24 §9.3 "release at mount" flow gains
one additional condition — the `SealRequest` is honored only when the
§2.5.1 issuance predicate (hire/term state) also passes. The reciprocal
one-paragraph CIP-24 amendment naming this condition ships in the same
release (its §9.3 release rule gains "…and, for Trading-Post-registered
volumes, the CIP-33 §2.5.1 predicate"). A new *condition*, not a new
*cryptographic family*: identity derivation, DST, AAD, material-hash
binding, and rewrap semantics apply verbatim. For a Private artifact,
`claimed_root == version.manifest_root` records the registrant's claim that the
copy contains the source content identified by that root. It does not assert
that the destination volume's commitment equals the source commitment. The
Private registration path MUST NOT check destination-root equality; the
destination MUST exist, be `Active` and `Private`, and belong to the Store
operator. Node PR #1708 at `e89078ff` implements these checks. The source-root
claim is accountability, not a consensus proof of copy equivalence (§4).

`cbssd` enforces an **issuance policy** (chain-state-conditioned, as for
CIP-7 stream keys). An issuance request carries `(runner_id, job_id)` — hire and artifact
identities are NOT request fields: `cbssd` derives them from the
authenticated job binding below (matching CIP-24's byte-unchanged
`SealRequest` rule). Shares for the hire's pinned artifact volume are issued iff, per
node RPC at the current block:

```
job(job_id) is assigned to runner_id
  ∧ job(job_id) is bound to (hire_id, pinned artifact)   // job metadata, set at dispatch
  ∧ Hire(hire_id).status == Active
  ∧ term validity:  Subscription | OneShot → current_block ≤ paid_through
                    PerCall              → bound MPP session open ∧ funded (§2.4, §2.6.3)
                    Free                 → declaration ∧ grant (if any) Active (§2.4)
```

Shares are issued at most once per `(hire, runner, lease_epoch)`; the policy
is re-evaluated at every lease-epoch boundary
(`lease_epoch = (block − hire_block) / lease_epoch_blocks`). The node MUST
expose the job-assignment and hire-binding query this predicate reads.

`lease_epoch_blocks` is creator-set per grant (default **1800 blocks ≈ 30
min** at the 1 s minimum block interval; recommended range 900–3600). It is
independent of consensus epochs (none exist; `EPOCH_LENGTH = u64::MAX`) and
of CBFS rent epochs.

#### 2.5.2 Revocation latency

Two layers:

- **Fast path (seconds):** runners MUST subscribe to Trading Post lifecycle
  events (CIP-29 via indexer feed) and stop accepting/continuing jobs for a
  hire upon observing `pause`/`revoke_hire`. This is a runner-policy
  requirement tied to reputation/slashing (CIP-2), not consensus.
- **Cryptographic backstop (≤ one lease epoch):** share issuance stops at the
  next lease-epoch boundary regardless of runner behavior.

#### 2.5.3 Residual risk — cached DEKs

A runner that has lawfully decrypted during epoch *k* may cache the DEK and
compute beyond revocation. This is accepted and bounded: such execution earns
nothing — `cbssd` issues no further shares, the Trading Post refuses
settlement for non-Active hires, and job results for revoked hires are
rejected in the existing runner settlement flow. The penalty surface is the
runner's stake and reputation (CIP-2, CIP-13). Rewrapping on a shorter
cadence does not harden this — the DEK is unchanged, so a cached key opens
the same ciphertext (CIP-9 §9.4.1); only real rotation with re-encryption
would, and it does not exist yet (§6.1, CIP-9 §9.4.2).

Pause powers are correspondingly honest: a store can pause **what it
distributed** — future scheduling and key issuance — not reach into runners'
memory, and not touch the actor itself or hires routed through other stores.

### 2.6 Agent hire

The differentiating flow: a hiring **actor** assembles capabilities at
runtime.

#### 2.6.1 Capability schema

`capability_schema` points to a CBFS-hosted JSON document, content-addressed
by the version's `manifest_root`, describing: methods exposed to hirers
(names, JSON-Schema parameter/result types), supported `PricingTerm`s, runner
requirements (models, mounts), and rate limits. The schema document format is
versioned (`"cip33_schema": 1`); evolution happens by document version, not
protocol change.

#### 2.6.2 Discovery and hire

Hiring actors query listings either off-chain (their runner consults an
indexer during a CIP-2 job and passes candidates into actor logic) or
on-chain (`call()` into the read surface, §2.1.2). The hire itself is an
**deferred system-instruction flow** — a PVM handler cannot call the
Trading Post's Rust mutators, so the hiring actor enqueues a deferred
`TradingPostHire` whose consensus sender and payer is the actor itself
(the same actor-originated deferred-system-instruction mechanism the
Intent path already uses, with `sender_override` to the actor). The hire
executes deterministically in the same pipeline, after the enqueuing
handler. This is a **dedicated host function** (`trading_post_hire(args)`
in the SDK), not the generic deferred mechanism — which produces only
`ExecuteActor`: the host fn enqueues a deferred `TradingPostHire` with the
enqueuing actor as consensus sender and payer, gated by an `econ.hire`
entitlement, args bounded by the `HireArgs` codec caps, gas drawn from the
enqueuing handler's pool, sender overridden to the actor, and the result
surfaced via the hire receipt/event. Identity, balance debit, failure
rollback, and replay behavior carry dedicated tests. `call()` reaches only
the read surface (§2.1.2).

#### 2.6.3 Metered usage

`PerCall` terms meter over an MPP session (CIP-8) bound to `(hire_id,
version)`, with per-call price from the pinned term. Session open moves the
session's reservation from available balance into `reserved` ("funded" in
§2.5.1 means the reservation covers it). Settlement is explicit:
`settle_per_call(hire, session_receipt)` — the receipt is the CIP-8
session-close statement (bilaterally signed call count/amount) — draws the
settled amount from `reserved`, releases the remainder to available, and
executes the hire's pinned split. Disputes follow the CIP-8 session dispute
path and the CIP-2 result-dispute window; the Trading Post accepts only
receipts that validate against the session's binding. Receipt validation is
**fail-closed**: a receipt that cannot be fully verified — including when the
implementation has no signature-verifying validator deployed — MUST be
rejected, never settled. Settlement ordering is likewise fail-closed as a
whole (§2.3): `settle_per_call` MUST validate and debit the settled amount
from `reserved` **before** any beneficiary payout, so an over-settle
(`settled` exceeding the reservation) aborts with no partial transfers.
Subscription terms require no session.

#### 2.6.4 Instances

A listing declares one or both of two peer instance modes; neither is a
degraded form of the other, and a listing offering both MAY price them
differently.

**`instance_mode: Shared`** — the listed actor serves all hires directly,
partitioning state and (where needed) per-hire private volumes keyed by
`hire_id`; per-user volume provisioning is performed by the creator's runner
flow (CIP-2/CIP-9). Shared is the right mode when the creator's actor *is*
the product: accumulated state, curated datasets, live credentials, a
creator-operated service. The creator maintains the code and fixes bugs for
every hirer at once; the hirer holds a `hire_id` claim, not an actor.

**`instance_mode: Forked`** — each hire receives its own actor instance,
spawned by the listed actor itself via CIP-27 clean-mode fork
(`fork(mode="clean", new_owner=hirer, payload=hire_config)`). Forked is the
right mode when the hirer needs sovereignty: hard state isolation, an
instance they own and fund, private volumes bound to their own actor
address, and an enforced executable commitment (below) that proves the
instance runs exactly the reviewed, attested artifact.

No synchronous bridge exists in either direction between the Rust Trading
Post and PVM code — `TradingPostHire` cannot enter a PVM handler, and a PVM
handler cannot call the Trading Post's Rust mutators (host cross-calls
intercept other system actors, not `0x1E`, and PVM writes materialize
through the transaction overlay, invisible to the store the Rust handlers
read). Forked hires are therefore **three-phase, escrow-backed, verified
against committed state**:

1. **`hire(instance_mode: Forked, hire_config)`** (system instruction)
   validates the version's executable commitment against the listed actor's
   live binding (below), escrows the payment, and records a `PendingSpawn`
   pinning the **complete proposed hire**: hirer, version, payment asset and
   amount, the effective split vector, artifact pin, lease grant, term,
   upgrade policy, the exact `hire_config` bytes, `child_profile_hash`,
   `consent_nonce`, and both deadlines. The deadline source is canonical:
   `delivery_deadline = hire_height + SPAWN_TTL_BLOCKS` and
   `finalize_deadline = delivery_deadline + FINALIZE_WINDOW_BLOCKS`, with
   `SPAWN_TTL_BLOCKS = 600` and `FINALIZE_WINDOW_BLOCKS = 600` (~10 min
   each at the 1 s minimum interval) — protocol constants, not
   caller-supplied. Two deadlines, not one, because one is unsatisfiable
   at the boundary: a directive validly delivered at the single deadline
   height D commits a child whose state is first inspectable by a
   finalize transaction at D+1 — exactly when a single-deadline reclaim
   would already be eligible, making the race the protocol's fault.
   Delivery is valid through `delivery_deadline`; finalize through
   `finalize_deadline`; reclaim from `finalize_deadline + 1`; D and D+1
   CAS-ordering vectors at both boundaries are pinned. Pinning the
   full terms is what makes finalization deterministic — it must not race
   intervening declaration or pricing changes, and refund must be exact.
   It also enqueues a **spawn directive** into the template's mailbox — a
   message from `TRADING_POST_SYSTEM_ACTOR` (`0x1E`) with pinned codec
   `b"cip33.spawn.v1" ‖ declaration_id ‖ u32_be(version) ‖
   u64_be(consent_nonce) ‖ u64_be(hire_id) ‖ hirer(20) ‖
   u64_be(delivery_deadline) ‖ u32_be(len(child_profile)) ‖ child_profile
   ‖ u32_be(len(hire_config)) ‖ hire_config` — the listing identity and
   consent nonce included so the template can verify *what the directive
   is for*, not merely who sent it (§2.1.3: a genuine `0x1E` directive can
   exist for a hostile listing if consent were absent), and the
   `instance_profile` bytes included because the template must pass them
   as `child_manifest` in phase 2. The directive IS the
   authorization, which requires one **consensus change this CIP depends
   on**: today mailbox materialization drops `Message.from` — deferred
   delivery names the *target* actor as `tx.from`, so a message-delivered
   handler's sender is the actor itself and no origin is authenticatable.
   The amended rule, with the exact representation pinned: `tx.from` on the
   materialized deferred transaction **stays the target actor** (for the
   *ordinary* deferred class, its account nonce, gas, and digest
   identity are unchanged — no replay or fee consequence; the
   `TradingPostSpawn` class below has its own nonce-less identity
   model), and delivery carries `Message.from` in a new
   **frame-origin** field on the delivered PVM frame, exposed as
   `get_sender()`. This is the CIP-41 frame-scoped-context pattern reused:
   the frame origin is set at delivery, restored/cleared on nested dispatch
   exactly like `co_signers`, and is `None` for ordinary
   transaction-entered frames. `Message.from` is already consensus-written
   (`send_message` stamps the true caller, the Trading Post writes `0x1E`),
   so exposing it makes provenance unforgeable without touching `tx.from`
   semantics. Digest/replay validation is unaffected; clearing/restoration
   carries tests mirroring §the CIP-41 provenance suite.
   Tests: a forged ordinary-actor message presents that actor's address
   (guard rejects); a genuine directive presents exactly `0x1E`; nested
   calls do not inherit it.

   **Spawn gas is a separate native-CBY escrow**, dimensionally distinct from
   the token price (which may be a CIP-20 token). Two escrows, two
   conservation equations, never summed: the price escrow satisfies
   `initial_due(split) + prepaid + price_refund == price.amount` in the
   price token; the gas escrow, in CBY wei, satisfies
   `consumed_wei + returned_wei == deposit_wei`.

   The gas path is fully specified, because no existing mechanism can fund
   it: the current mailbox rule materializes a deferred transaction that
   *borrows the origin transaction's gas* with no caps — unusable here,
   where the origin is a hire instruction and the payer must be the escrow.
   **Execution requirements.** The mechanics below define CIP-33 execution. Under the CIP-40 contract, kind 1 has no producer, lane, digest,
   fee path, delivery, or execution semantics; only its exact standalone decoder arm exists, and
   every external and consensus path rejects it as specified by the reciprocal contract below.
   Execution requires this entire surface, implemented consistently across its consumers.

   Required mechanics:

   - **Input.** `hire`'s `gas` argument (§2.1.2) supplies
     `{budget: GasBudget, deposit_wei: u64}` — deposit u64 like the native
     ledger; there is no ledger widening. Units are pinned end-to-end and
     they are **wei, with no conversion anywhere**:
     `max_fee_per_cycle` / `max_fee_per_cell` are wei per unit — the
     dimension the operative fee path actually uses (basefee, rate
     products, and fee debits are native-ledger wei throughout current
     code; an atto rate here would undercharge this class by 10^9
     relative to every ordinary transaction, and this CIP does not
     propose a network-wide fee-unit migration). Validation at hire:
     `deposit_wei ≥ cycles_limit × max_fee_per_cycle +
     cells_limit × max_fee_per_cell` (products and sum checked in u128,
     result must fit u64); any limit of zero, or an overflowing
     product/sum, rejects. The deposit moves
     from the hirer to Trading Post custody in the hire execution;
     `PendingSpawn.gas_escrow` records `{budget, deposit_wei,
     consumed_wei: 0}`.
   - **The typed transaction class.** This CIP's third consensus change
     (§1.2) is a **new deferred transaction class, `TradingPostSpawn`**,
     not a payer flag on the existing one — the existing deferred class
     borrows origin gas, requires its fee fields zero, and charges the
     executing actor (or owner) only on success with error/OOG outcomes
     paying nothing; none of that composes with an escrow that must be
     debited on success *and* failure. `TradingPostSpawn` is
     consensus-produced only (materialized by the hire execution; it has
     no user-submittable form). Its authentication is a **committed
     record binding the complete transaction**: the hire execution
     constructs the full `TradingPostSpawn` transaction
     deterministically, computes its canonical digest, and writes a
     `SpawnDirective { hire_id, template, tx_digest, envelope_hash,
     consumed: bool }` row in Trading Post state. Delivery validates by
     **one comparison** — the presented transaction's canonical digest
     equals the committed `tx_digest`, which therefore binds origin =
     `0x1E`, budget = exactly the escrowed budget, the class
     discriminant, and the byte-exact directive body in a single check
     (no field is verified by convention, because every field is inside
     the digest) — then a **consume CAS** (`consumed: false → true`); a
     transaction with no matching unconsumed row is invalid outright (no
     replay, no forgery, no second delivery). The template's own
     consumed-mark remains belt-and-braces above it. The `0x1E` origin is a **typed field of the
     transaction class itself**, which delivery loads directly into the
     frame-origin `get_sender()` reads — the durable carrier; the
     generic `Message.from` frame-origin rule above remains for ordinary
     mailbox traffic, but this class does not depend on that plumbing.
     Its identity model is pinned as **nonce-less**: kind-1 carries no
     `chain_id`, no account nonce, and no signature fields at all —
     replay protection is the consume-CAS on the committed
     `SpawnDirective` row (strictly stronger than a nonce: exactly one
     execution ever), and any future activated transaction identity must
     bind CIP-40's exact `ChainIdentity` rather than a genesis-header
     digest or locally inferred value. The template
     is named as `tx.from` for frame-identity only; its **account nonce
     is never read or mutated** by this class — an earlier
     "nonce semantics unchanged" phrasing meant exactly this and is
     replaced by it. **Delivery preconditions**,
     checked before any execution: the digest/CAS match above,
     `PendingSpawn.status == Pending`, and
     `height ≤ delivery_deadline` — a directive presented after reclaim,
     after finalization, or past its deadline is invalid with zero
     consumption, never merely faulted. It executes under the pinned
     `budget` and settles at its **own commit boundary**: on every one
     of the five outcomes, ONE outer commit atomically writes the whole
     fee tail — `SpawnDirective.consumed = true`,
     `gas_escrow.consumed_wei`, the escrow debit, and the fee
     burn/distribution — while the directive's application effects
     commit only on success and roll back on failure inside it. The
     pairing is indivisible by construction: no outcome can consume the
     directive without settling, or settle without consuming, or roll
     back `consumed` while a debit stands (each of those asymmetries is
     a replay or a theft). Mirror-image rule on the other side:
     `reclaim_spawn`'s single atomic authority is the
     `Pending → Refunded` CAS on the pending record, and under it the
     directive row has **two valid branches** — it MUST NOT demand a
     fresh consume-CAS unconditionally, because every attempted
     deterministic delivery (guard fault, OOC, OOCe included)
     terminalizes `consumed = true`, and a reclaim that requires
     `false → true` after a failed attempt would strand both escrows
     forever: (a) `consumed == false` (directive never delivered /
     expired) → consume it now, then refund; (b) `consumed == true`
     from a terminal delivery outcome → verify the terminal settlement
     and refund without a second CAS. Either way no live spawn
     authorization survives a refund. Vectors: guard-fault → reclaim,
     OOC → reclaim, OOCe → reclaim, rate-cap → reclaim (each refunding
     price in full and `deposit − consumed`), undelivered-expired →
     reclaim, and the delivery-vs-reclaim ordering race (whichever
     commits first wins; the loser is invalid, nothing double-spends).

     **Transaction-v2 reciprocal contract.** CIP-40 §7.1 owns the shared transaction envelope:
     `u8 version=2 ‖ u8 kind ‖ body`. Kind 0 uses CIP-40's exact ordinary field
     order, including `signature_mode` followed immediately by CIP-28's
     `fee_payer_override: Option<Address>`, and carries no inner version byte.

     The shared standalone decoder also recognizes kind 1 as the unsigned exact
     byte concatenation

     `u64_be(hire_id) ‖ template[20] ‖ origin[20] ‖ u64_be(cycles_limit) ‖
     u64_be(cells_limit) ‖ u64_be(max_fee_per_cycle) ‖ u64_be(max_fee_per_cell) ‖
     u32_le(directive_len) ‖ directive[directive_len]`.

     `directive_len <= CIP33_SPAWN_DIRECTIVE_MAX_BYTES = 49,254`; length mismatch,
     cap excess, or trailing bytes reject. These five `u64` fields are fixed-width
     big-endian and the directive length is fixed-width little-endian; this body does
     not use kind 0's minimal-`UInt` scalar codec. It carries no signature fields.
     Under the CIP-40 contract, every external RPC/mempool path and
     every consensus block-validation, import, replay, and execution path rejects a
     decoded kind 1 before lane selection, digest use, recovery, state, event, or
     execution; a block containing it is invalid. No kind-1 producer or capability
     is advertised. CIP-33 must define and implement the complete execution policy before
     any lane or execution semantics below become active.

     Kind 0 alone has a signing hash and transaction identity:

     `keccak256(b"cowboy.tx.v2" ‖ u64_be(chain_id) ‖ chain_instance_id(32)
     ‖ full v2 encoding with every signature field zeroed)`.

     `chain_id` and `chain_instance_id` are the indivisible `ChainIdentity`
     returned by CIP-40's closed `/chain-info` response; `genesis_digest` is not
     a transaction context or response member. Kind 1 has no signing preimage,
     signing hash, typed-data object, or signature fixture under the CIP-40 contract. The
     corpus proves kind-0 native/EIP-712 signing, kind-1 standalone decode/re-encode
     followed by rejection at every external and consensus boundary, unknown-kind
     outer rejection, and cross-version/kind mutation failure.
   - **Fee payer.** Fees debit `gas_escrow.deposit_wei` — never the
     template's balance, never origin gas.
   - **Pricing.** Directives execute in the deterministic deferred
     pipeline, not a priced mempool, so there is no priority-fee or tip
     dimension: consumption is charged at the block's effective CIP-3
     rates, capped by `max_fee_per_cycle` / `max_fee_per_cell`. If the
     block's rate exceeds a cap at delivery, the directive is **not
     executed** — a spawn failure with zero consumption; it dead-letters
     and the full deposit is reclaimable. There is no retry in v1: one
     directive, one delivery attempt.
   - **Settlement.** As the directive executes, consumed Cycles/Cells
     price in wei exactly as an ordinary transaction's do (same
     constants, same arithmetic — no unit boundary exists to cross);
     the settlement point writes the total into
     `gas_escrow.consumed_wei` (≤ `deposit_wei` by the budget bound).
     Consumed fees are burned/distributed exactly as ordinary CIP-3
     fees — the escrow changes the payer, not the fee economics. OOG
     against the budget is a spawn failure that keeps `consumed_wei` as
     consumed, leaves the remainder and the whole price escrow
     reclaimable. A parity vector pins that one cycle/cell costs this
     class the same wei as an ordinary transaction at the same block. `finalize_spawn` and `reclaim_spawn` each return
     `deposit_wei − consumed_wei` to the hirer; consumed gas is never
     refunded — the one cost a failed spawn can impose. The consensus outcome
     set is exactly **five** — success, template guard fault,
     `OutOfCycles`, `OutOfCells`, and rate-cap rejection (the only
     zero-consumption case). The boundary between "outcome" and "node
     problem" is **typed, not adjectival**, because the current error
     surface folds both into one `StoreError` that becomes a zero-gas
     failed receipt — exactly the conflation that must not reach this
     class. The fee model distinguishes two classes. **Deterministic resource
     rejections** (`StorageFull`, `MailboxFull`, quota rejections — 
     facts every validator computes identically from state) are
     guard-fault-class outcomes, charged and settled under the one
     outer commit like any fault the directive's own code raises;
     **validator-local faults** (IO errors, corruption — events other
     validators did not experience) are a typed non-outcome: no
     receipt, no charge, no settlement, node-local halt/retry, because
     charging on them is consensus divergence by construction. Every `StoreError` variant MUST map explicitly to one of these
     two classes. Acceptance covers the five, each proving exactly
     one escrow debit, the matching `consumed_wei` CAS update, correct
     fee burn/distribution, and the correct execution-state rollback
     alongside a committed fee settlement.

   An expired directive dead-letters at the deadline. No hire exists yet
   in any of these outcomes.
2. **Spawn** (mailbox delivery of the directive). `spawn_instance` runs as
   the directive's message handler; the template's guard checks, in order:
   `get_sender() == TRADING_POST_SYSTEM_ACTOR` (consensus-grounded
   provenance); the directive's `{declaration_id, version, consent_nonce}`
   against the listing identity recorded in the **template's own storage**
   at consent time (§2.1.3 — provenance alone cannot distinguish an
   authorized listing from a hostile one, since both produce genuine `0x1E`
   directives); the deadline from the directive body; and a
   mark-directive-consumed flag in template storage (mailbox delivery is
   once, the mark is belt and braces against handler-level re-invocation).
   Nothing here trusts caller-supplied fields: every directive field was
   written by the hire instruction. A spawn attempt from any other sender,
   or for a listing the template never consented to, fails before any
   child exists or `fork_count` is consumed. The template then calls
   `fork(mode="clean", new_owner=hirer, child_manifest=child_profile,
   payload=envelope)` — `child_profile` is the version's
   `instance_profile` bytes, delivered in the directive, and passing it is
   **mandatory**: the template's own manifest necessarily carries grants
   (`exec.fork` at minimum) that the version's `instance_profile` cannot
   retain (§2.6.4's `declare_version` rejection rule), so a profile-less
   fork — valid under CIP-27 §3.7.1 only when the verbatim parent
   manifest passes — would produce a child whose manifest hash equals the
   template's, failing the `instance_commitment` check at finalization. The envelope is

   ```
   envelope = b"cip33.env.v2"          # 12 bytes
              ‖ declaration_id         # 32 bytes
              ‖ u32_be(version)        # 4 bytes
              ‖ u64_be(consent_nonce)  # 8 bytes
              ‖ u64_be(hire_id)        # 8 bytes
              ‖ u32_be(len(hire_config)) ‖ hire_config
   ```

   — exact widths, order, and golden vectors pinned. The listing identity
   and authorization epoch are inside the payload the child provably
   received, so a finalized child commits to its declaration, version,
   and consent, not just its hire. **`MAX_CIP33_HIRE_CONFIG_BYTES = 32,768`**.
   The directive's fixed overhead is exactly 102 bytes and
   `MAX_FORK_PROFILE_BYTES = 16,384`, so the 65,536-byte message limit permits at
   most `65,536 − 102 − 16,384 = 49,050` config bytes. The smaller 32,768-byte
   config cap is therefore closed with margin. The exact maximum directive is
   `CIP33_SPAWN_DIRECTIVE_MAX_BYTES = 102 + 16,384 + 32,768 = 49,254` bytes;
   its 84-byte fixed body overhead is `8 + 20 + 20 + 4×8 + 4`, so the maximum
   kind-1 body is 49,338 bytes and the complete version/kind shell is
   49,340 bytes. Exact-max, cap+1, truncated-length, and trailing-byte vectors are
   pinned for the standalone decoder. The CIP-27 fork receipt
   (`__FORK_MODE__`, `__FORK_PAYLOAD_HASH__` — birth-only, host-written
   slots actor code cannot set) binds the child to the keccak of those
   exact delivered bytes.
3. **`finalize_spawn(hire_id, instance_address)`** (system instruction,
   submittable by anyone) verifies the **committed** child state,
   fail-closed: `parent` equals `PendingSpawn.template_actor` — the actor
   that actually forked, which is NOT `declaration.actor` (the Shared
   serving identity; the two coincide only when a listing points both at
   one actor); the child's `code_hash` and CIP-26 library pins equal the
   version's `instance_commitment` (identical to the template's by
   construction), and the child's **effective manifest hash equals
   `instance_commitment.manifest_hash`** — the pinned fork profile, not
   the template's manifest, which necessarily differs (§the executable
   commitment below); `__OWNER__` is the hirer; `__FORK_MODE__` is clean;
   and `__FORK_PAYLOAD_HASH__` equals the keccak of the envelope
   **re-encoded from the pinned record** — `PendingSpawn` stores the
   exact `hire_config` bytes (≤ `MAX_CIP33_HIRE_CONFIG_BYTES`),
   `consent_nonce`, and `child_profile_hash`, so finalize re-encodes
   `b"cip33.env.v2" ‖ declaration_id ‖ u32_be(version) ‖
   u64_be(consent_nonce) ‖ u64_be(hire_id) ‖ u32_be(len) ‖ hire_config`
   and compares byte-identical constructions, never a hash of a hash. This
   binds the child to **this** pending hire of **this** listing:
   declaration, version, and `hire_id` are all inside the envelope the
   callback actually received. Finalization is a compare-and-swap on the pending record's
   state (`Pending → Finalized`, or `Pending → Refunded` on reclaim; any
   other transition rejects), and the Trading Post maintains a uniqueness
   index on `instance_address` so one child can satisfy exactly one hire —
   two identical pending hires need two spawns. Settlement on success follows the
   **existing hire economics, at finalize time**: the pinned
   `{initial_due, prepaid, token}` releases only `initial_due` through the
   split — the OneShot price, or zero for Free, the only v1 Forked terms
   (below); any surplus carries as `Hire.prepaid`. `hire_block` anchors at
   successful **finalization**, not phase-1 escrow (`paid_through = ∞` for
   OneShot; unused for Free), so creator delay burns nothing the hirer
   paid for. Both escrows conserve exactly across finalize, reclaim, and
   failure, each in its own equation (phase 1): the price token as
   split + prepaid + refund, the gas escrow as consumed + returned — with
   boundary and race tests. Only on success is the `Hire` written with
   `instance_address`. A pending spawn that misses `finalize_deadline` lets
   the hirer reclaim the price escrow in full and the gas escrow minus
   consumption — an unresponsive creator costs the hirer time and at most
   the pinned gas budget, never the price.

Phase separation opens a window that must be closed, not merely priced: a
committed, unfinalized child is a **hirer-owned actor with no hire** — and
without a gate the hirer could use it (public handlers, unbound job
submissions) through the pending window, decline to finalize, reclaim the
price, and keep the product free. Two quarantine layers make an
unfinalized child worth nothing, one per service surface:

- **Job-side, protocol-mandated, carrying hire identity.** For a
  submission from an actor whose `parent` is an **ever-registered**
  `template_actor` — the Trading Post's reverse index is a permanent
  set: entries are written at version activation and survive consent
  revocation, version retirement, declaration retirement, and every
  other lifecycle transition forever, exactly like the serving-actor
  tombstones, and no GC path removes them or `SpawnDirective` rows
  (this matters because the "no match → ordinary actor" rule below
  fails **open**: it is only safe while the records it consults can
  never have been cleaned up) —
  dispatch and result acceptance read the child's birth receipt: if
  `__FORK_PAYLOAD_HASH__` equals the `envelope_hash` of a recorded
  `SpawnDirective` (the committed row of §2.6.4's typed class — this IS
  the hire identity: `envelope_hash → hire_id`; rows are permanent, so
  the check holds for the child's whole lifetime), the child is a hire
  instance and is admissible **only** as that hire's finalized
  `serving_actor` under the canonical validity predicate — a pending or
  orphaned child cannot enter the compute layer. A child of a template
  whose receipt matches **no** recorded directive is not a hire instance
  and passes as an ordinary actor: the parentage test alone quarantines
  nothing, so a template's own operational forks and their descendants
  are not overblocked. Lifecycle tests: the quarantine holds unchanged
  after directive consumption, after reclaim, after consent revocation,
  and after declaration/version retirement — an orphan never ages into
  an unbound job actor.
- **Handler-side, commitment-carried.** Templates conforming to this CIP
  gate every application handler on an **activation check that is a
  read, not a message**: the SDK base's `on_fork_clean` records the
  envelope's `hire_id` and a pending mark; on invocation while pending,
  the gate `call()`s the Trading Post read surface (`get_hire(hire_id)`,
  over the §2.1.2 read bridge). What it may cache is **immutable
  identity only** — "a finalized `Hire` with `instance_address` = me
  exists" is a fact that never un-happens. What it MUST NOT cache is
  lifecycle: on **every** handler invocation the gate re-reads the
  hire's mutable state and serves only while `Hire.status` and the
  canonical §2.5.1 per-term predicate pass — a gate that latched a
  one-time positive check would keep serving through `pause` and
  `revoke_hire` (and could even activate on a hire paused before the
  instance's first call), nullifying the very lifecycle controls the
  binding rules mandate. The re-read is one bridge call per
  invocation, priced like any cross-call read. The state reads are precise: before
  finalization **no `Hire` exists** — `get_hire` returns not-found and
  the pending/refunded states live on `get_pending_spawn(hire_id)` — so
  the gate's rule is "activate on a matching finalized `Hire`, stay
  inert on not-found or any `PendingSpawn` state," never a status read
  of a record that isn't there.
  Activation is a consensus-state read of the same fact
  as settlement: `finalize_spawn` writing the `Hire` simultaneously
  makes the hirer's payment effective and the child's gate satisfiable,
  deterministically, on every validator, with no second message leg to
  fail. An unfinalized child reads not-found / a `PendingSpawn` state
  and stays inert forever. This is actor code,
  but it is exactly the code the `instance_commitment` pins and the
  attestation covers: a template without the gate is visible in review,
  and the party it fails is its own creator (the free product is carved
  out of the creator's revenue, nobody else's). Attesters SHOULD treat a
  missing gate as review-relevant.

With both layers, the accepted residue shrinks back to what was claimed:
an unfinalized spawn consumes creator quota and yields a child that can
neither serve jobs nor answer handlers — the creator's cost, never the
hirer's funds and never a free product. Escrow still moves only on
successful finalization or reclaim. Tests: spawn → direct call and job
submission before finalize (both rejected), spawn → no finalize →
reclaim (full price back, child permanently inert), self-activation
succeeding on the first post-finalize invocation and never before
(the gate's read flips exactly when the `Hire` commits), a hire paused
**before** the instance's first call never serving, and `pause` /
`revoke_hire` after activation stopping service at the next invocation
(the per-invocation lifecycle re-read). Templates additionally carry the CIP-27 §3.7.1
fork profile: hired instances drop `exec.fork` — normative, enforced by
§2.6.4's `declare_version` rejection rule, not merely conventional — so
an instance cannot itself spawn further untracked clones.

**Executable commitment.** A `DeclarationVersion` today pins a CBFS
`manifest_root` — content, not code: fork clones the listed actor's *live*
`code_hash` and library pins, so without a code commitment a creator could
upgrade the actor after version v1 was attested and every subsequent "v1"
hire would receive current, unreviewed code under v1's attestations,
pricing, and leases. Forked-mode versions therefore pin **two commitments** at declaration
time, because one cannot serve both phases: the template necessarily runs
with grants (`exec.fork` at minimum) that the hired child's profile MUST
NOT retain. That MUST is **this CIP's own normative rule, enforced at
`declare_version`** — a Forked `instance_profile` that retains
`exec.fork` **rejects at declaration** (conformance vector: an
`instance_profile` byte-identical to the template's manifest rejects) —
and deliberately NOT an appeal to CIP-27 §3.7.1, which does not supply
it: §3.7.1's attenuation rule is *subset*, a subset admits equality, and
CIP-27 explicitly leaves recursive fork as "the template's explicit
choice via the profile." Without the rejection rule the premise below
fails (a profile retaining `exec.fork` and otherwise equal to the
template's manifest would make the two commitment tuples byte-identical,
collapsing the two-tuple design). With it, the template's manifest bytes
(which carry `exec.fork`) and the child's (which cannot) are **distinct
by construction**, and their hashes differ except under a Keccak
collision — a single-tuple design remains unsatisfiable, with `hire()`
and `finalize_spawn` demanding contradictory manifests. Nature
of the two-hash guarantee, for the verifier that relies on it: there is
**no domain separation** between the template and instance preimages —
both are bare keccak256 over canonical manifest bytes (the template's
node-derived manifest hash must remain comparable to `hire()`'s on-chain
read, which is why a CIP-33-local prefix is not used) — so distinctness
is **cryptographic (Keccak collision resistance over preimages the
`declare_version` rule keeps distinct), not structural**. Role
substitution independently fails on the commitment's positional tuple
binding. A future hash-function migration MUST revisit this property; it
does not survive the hash function for free.
`template_commitment` `(code_hash, lib_pins_hash, manifest_hash)` is the
template's own live tuple: `hire()` checks the template against it.
`instance_commitment` shares `code_hash`/`lib_pins_hash` (clean fork
never changes code or pins) but its `manifest_hash` is
`keccak256(instance_profile)` — the version's pinned CIP-27 fork-profile
bytes, the attenuated manifest hired children actually run:
`finalize_spawn` checks the child against it. Hash canonicalization
(nothing here may depend on storage iteration order): `lib_pins_hash` =
`keccak256(b"cip33.pins.v1" ‖ u32_be(pin_count) ‖ pins…)` where each pin's
preimage is `u8(len(name)) ‖ name ‖ code_hash(32)` — `name` is the
`ActorLibPin.name` ASCII bytes (1..=64, the pin table's identity; there
is no other "library id") and `code_hash` its pinned digest — pins sorted
**bytewise ascending by `name`**, duplicate names rejected, `pin_count`
exactly the actor's pin-table size, and the preimage consumed to exact
EOF (trailing bytes reject). Each `manifest_hash` = keccak256 of the
manifest's canonical codec bytes; each commitment's codec bytes are the
three hashes in the order shown. Permutation vectors prove pin/manifest-entry
insertion order cannot change consent, finalize, or attestation identity.
**Attestations (§2.7) on Forked-capable versions bind the full tuple, not
the `manifest_root` alone**: the attestation subject key is the §2.1
canonical form —
`keccak256(b"cip33.attest.v1" ‖ declaration_id ‖ u32_be(version) ‖
code_hash ‖ lib_pins_hash ‖ template_manifest_hash ‖
instance_manifest_hash ‖ manifest_root)`, one formula, defined once. Root-only subjects are
forgeable by construction here — two append-only versions can share a
`manifest_root` while differing in code, pins, or manifest, and a client
indexed by root would inherit an old Approved verdict for unreviewed
executables; per §2.1, root subjects never satisfy a Forked-version
requirement. A consequence worth stating: self-fork
cannot instantiate an *old* version — if the listed actor has upgraded past
a version's commitment, Forked hires of that version fail at `hire()` (the
live-binding precheck) — so per-version templates are a **represented, first-class concept**,
not advice: a Forked-capable `DeclarationVersion` carries an immutable
`template_actor` address whose live executable tuple must equal the
version's `template_commitment` at `hire()` time; `PendingSpawn` pins it, phase 2's
spawn targets it, and `finalize_spawn`'s parent check verifies against it
— `declaration.actor` remains the Shared-mode serving identity only. A
Forked template legitimately cannot `upgrade_self` in place (it must not
carry `sys.upgrade` in an instantiable manifest), which is exactly why
each executable version is its own deployed template. Two simultaneously
live versions — two templates, distinct commitments, hires against both —
carry a dedicated test.

Mode selection criteria, stated rather than implied: who owns the state
(creator → Shared, hirer → Forked); who maintains the code (creator pushes
fixes → Shared; hirer-frozen instances → Forked, with upgrades below); and
what must not propagate — a Forked hire uses **clean-mode** fork,
exclusively, precisely so that the answer is "nothing": the listed actor's
hire ledgers, volume pointers, and credential references never reach an
instance. Full-copy fork is NOT a hire mode: cross-principal full copies
are ruled out by CIP-27's identity-boundary inheritance rules, and a live mutable state
snapshot is unattestable — the commitment pins code, and nothing pins what
the parent's storage contained at spawn. Snapshot-semantics listings (a
pre-trained game world sold as state) would need a pinned, content-addressed
storage-root commitment of their own; that is future work, not a mode this
CIP defines.

**v1 Forked instances are frozen.** This falls out of the capability rules
rather than being bolted on: `sys.upgrade` is registry non-inheritable, and
no fork of any kind may carry a non-inheritable grant across the identity boundary (CIP-27 §3.7.1), so an
instance structurally cannot `upgrade_self` — and ownership alone confers
no upgrade power anyway. Freezing is also what keeps the executable
commitment honest: an instance that could change its code would silently
invalidate the very attestation, lease, and pricing its hire was granted
under. Instance upgrades, when they come, are a **Trading Post version
transition** — a new version with a new commitment, the instance re-verified
against it — not an in-place mutation; that protocol is future work, and
`upgrade_policy` is reserved in the data model for it.

**Data model and state machine.** This mode is carried in normative codec
structs, methods, and an explicit state machine — the canonical §2.1
definitions carry `InstanceOffer`, `PendingSpawn`, the
`DeclarationVersion.{template_actor, template_commitment, instance_profile, instance_commitment}` fields, the
`Hire.{instance_mode, serving_actor, instance_address}` fields, the tagged
`AttestationSubject`, and the `hire` / `finalize_spawn` / `reclaim_spawn`
methods. `instance_offers` is structurally bounded: one or two offers, modes
unique (a duplicate mode rejects at `declare_version`), at most four
terms per offer, no duplicate terms within an offer. Terms are
mode-qualified by construction (a Forked hire cannot
select a Shared-priced term); the deadline boundaries are inclusive (delivery through
`delivery_deadline`, finalize through
`finalize_deadline`, reclaim from `finalize_deadline + 1`);
finalize/reclaim are `Pending → Finalized | Refunded` CAS transitions with
any terminal-state call rejected (duplicate finalization, reclaim after
finalize); a uniqueness index on finalized `instance_address` enforces one
child per hire. `serving_actor` is the single canonical serving identity
every downstream authority (runner admission, CBSS release, MPP,
`settle_per_call`) compares against.

**v1 Forked offers carry `Free` and `OneShot` terms only** — metered
terms (`PerCall`, `Subscription`) are rejected on a Forked `InstanceOffer`.
Metered revenue against a hirer-owned instance is uncollectable in v1: the
hirer may call their own instance directly, bypassing any MPP session.
Deferring metered Forked terms to a future version keeps the protocol from
listing revenue it cannot enforce.

Two binding rules are **mandatory in v1 regardless of term**, because even
Free/OneShot hires support `pause`/`revoke`/`status`. The reverse index is
**scoped to Forked instances** — one instance, one hire, a well-defined
one-to-one map; a Shared `serving_actor` is `declaration.actor` serving
arbitrarily many hires and is deliberately NOT in this index (each Shared
job binds to its `hire_id` directly, as today). Job dispatch and result
acceptance (§the runner sections) require a submission from an indexed
`serving_actor` to carry its exact binding **and** that hire to pass the
**canonical §2.5.1 term-validity predicate** — the same one `cbssd`
evaluates, with its per-term conditions exactly as written there: hire
`Active`, and then Subscription/OneShot → `paid_through` (declaration
retirement and grant revocation deliberately do NOT invalidate paid
time — §2.4's consumer-protection rule, which this predicate must not
quietly override; a OneShot hire outlives its listing by design);
PerCall → open funded session; Free → declaration ∧ grant (if any)
`Active` (Free carries no paid time to protect, so listing state is its
validity). `Hire.status` alone is not the gate, but neither is a blanket
listing-liveness test. An unbound, wrong-binding, or predicate-failing
submission from an indexed address rejects. The index entry is
written at finalization and is **permanent — never deleted, on revoke or
ever**. This is load-bearing, not tidiness: the dispatcher's current
`hire_binding` is optional and validated only when present, so an address
that fell out of the index would re-enter the unindexed path and a
*revoked* public instance would serve again as an ordinary actor — the
revocation gate undone by its own bookkeeping. Revocation flips the hire's
status; the address→hire tombstone outlives every lifecycle state.
Instance ownership transfer leaves `serving_actor`, the index entry, and
the binding unchanged: owning the actor is not owning the hire. Tests:
unbound and wrong-binding submissions while Active, Paused, Revoked, and
after ownership transfer — all rejected; the revoked address never serves
unindexed; predicate transitions test per-term: declaration retirement /
grant revocation invalidate a Free hire at the transition block but
leave a paid-through OneShot/Subscription hire serving (§2.4); lapse and
an unfunded PerCall session invalidate their own terms. When metered Forked terms arrive, MPP binds to
`(hire_id, version, serving_actor)` and `settle_per_call` authority is
`serving_actor` — the identity rails are already in place for it.

Economics: a Forked hire's spawn-time payment flows through the pinned
split vector (§2.3) at finalization like any other hire. Metered terms are
**not offered on Forked hires in v1** (above); when a future version adds
them, the MPP session binds to `(hire_id, version, serving_actor)` — the
serving identity, not a mode-blind `(hire_id, version)`. Whether creators
receive a continuing share of a Forked instance's downstream revenue
remains open (§6).

Acceptance (Forked mode): a spawn attempt from any sender other than
`TRADING_POST_SYSTEM_ACTOR`, a directive whose
`{declaration_id, version, consent_nonce}` does not match the template's
recorded consent (§2.1.3), a re-invocation after the consumed mark, and
a directive past its deadline each rejected **before any child exists**
(no `fork_count` consumed); gas-escrow conservation
(`consumed + returned == deposit`) verified at all four terminals —
finalize, reclaim, rate-cap failure, OOG failure; envelope golden vectors byte-identical between
phase-2 construction and finalize re-encoding; duplicate finalization and reclaim-after-finalize fail against
the terminal CAS state; one child cannot finalize two identical pending
hires (uniqueness index); a Forked `hire` selecting a Shared-offer term
rejects; a child forked without the pinned profile (its effective
manifest hashing to the template's, not `instance_commitment`'s) fails
finalization; attestation subject changes when any element of the
executable tuple — including either manifest hash — changes while
`manifest_root` is held constant; pin/manifest permutation vectors leave
every commitment, consent, and subject identity unchanged.

#### 2.6.5 Transaction Decoding and Execution

CIP-40 defines the shared Transaction-v2 envelope, while CIP-33 defines its kind-1 `TradingPostSpawn` body. Standalone decoding of that body is required for codec conformance; it does not authorize execution. External submission and consensus validation MUST reject kind 1 until the complete forked-hiring execution contract is implemented.

Forked hiring requires the producer, lane classification, escrow fee path, mailbox delivery, digest, execution handler, runner authorization, and lease authorization in §2.6.4. The protocol, node, PVM SDK, runner, CBSS, bindings, CLI, and wallet codecs MUST agree on the same executable identity, fee, and lifecycle contract, with cross-crate conformance tests. A decoder-only implementation is not a complete forked-hiring implementation.

Runner and lease authorization require extension — the current checks do
not know instances exist. Job binding today authorizes the hirer or
`declaration.actor`, not a per-hire instance address; the CBSS lease
release validates hire, version, artifact, and term, but neither the
instance address nor its current code binding. Forked mode adds both,
normatively, through the single canonical field: job authorization admits
`Hire.serving_actor`, and lease release verifies the requesting address
equals `serving_actor` **and** (Forked) the instance's committed code
binding against the version's executable commitment — a lease MUST NOT serve an instance
whose binding no longer matches (defense in depth under the v1 freeze; the
gate that matters if upgrades ever arrive). A clean-forked instance
carries no sealed state, so the lease is granted fresh to the instance
address at finalization.

### 2.7 Attestations

Attestation records bind `(attester, subject, verdict, policy_uri)`, where
`subject` is the tagged `AttestationSubject` (§2.1) — a bare
`manifest_root` (Shared/legacy code) or a version's full executable tuple
(`ExecutableV1`, mandatory for Forked-capable versions). Every index, cap,
and supersession rule below is keyed by the **subject key** (the recomputed
hash for `ExecutableV1`, the root for `ManifestRootV1`), never by
`manifest_root` alone. Properties:

- Open: any address may attest; there is no attester registry to gatekeep.
  Trust is a **client** decision — wallets, dashboards, and stores choose
  which attesters to honor and how to render unattested code (e.g.,
  Android-style friction). The protocol ranks nothing. Spam is bounded, not
  policed, by **one exact rule** (an implementation choice here would be
  consensus divergence): a hard cap of `MAX_ATTESTERS_PER_SUBJECT = 1024`
  distinct attesters per subject key — the cap the current implementation
  already carries (`protocol trading_post.rs`). At the cap, a *new*
  attester's record rejects; supersession by an already-listed attester
  MUST still succeed, so a full index can never block an attester from
  downgrading a prior verdict. Whole-list reads are acceptable under the
  cap; `list_attestations` returns the full subject index in attester-key
  order, no cursor.
- Bound to exact subjects: an attestation never carries to a different
  subject key — a new root, or any change in a version's executable tuple,
  is a new subject and needs its own attestation. This is what closes the
  root-reuse verdict-inheritance defeat (§2.6.4): two versions sharing a
  `manifest_root` have distinct `ExecutableV1` subject keys.
- For a Public Store copy in the Shared/legacy content model, approval of
  the publisher source root does not approve a different serving root.
  Clients that require Store approval MUST check the attestation on the
  selected artifact's `claimed_root`. That attestation approves the copy;
  it does not independently prove equivalence with the source. A verdict
  downgrade on that serving root applies to the copy under the client's
  policy. Root-only approval never satisfies a Forked-capable version's
  attestation requirement.
- Supersession is **overwrite-in-place**: state holds exactly one record
  per `(attester, subject)` key, and a new verdict replaces it — so an
  attester can downgrade a prior `Approved` to `Flagged` or `Rejected`,
  and per-subject state is genuinely bounded at `1024 × one record` (an
  append-only history under the attester cap would let one admitted
  attester grow a subject forever, un-bounding the bound). Verdict
  *history* is not lost; it lives where history belongs — every `attest`
  emits its CIP-29 event, and indexers reconstruct timelines from the
  event log, not from consensus state.
  `Flagged` is advisory — clients SHOULD surface it prominently; it gates
  nothing protocol-side.
- Cowboy Labs' review organization is simply one attester, whose verdicts the
  flagship store and first-party clients choose to require.

### 2.8 Events

Every state transition in §2.1 emits a named CIP-29 event carrying the
affected ids and (for lifecycle transitions) the acting party. Store-initiated
`pause`/`revoke_hire` events are thereby publicly auditable; the
"platform can pause apps" power is exactly as visible as it is real.

## 3. Economics and neutrality

The Trading Post charges nothing beyond gas. Store commission is a market
price set per grant; competing stores may undercut it; creators may multi-home
across stores or sell direct. The chain's revenue from a thriving hire economy
is store-agnostic: every hire's execution buys compute and gas regardless of
which storefront brokered it. This is deliberate — the protocol's take is on
*computation*, the unforkable layer, while distribution remains contestable.

## 4. Security considerations

- **Trading Post as high-value target.** It custodies split execution and
  lease state. Mitigations: CIP-12 upgrade governance, no operational
  superpowers to abuse (§2.1 neutrality invariant), splits executed by simple
  arithmetic over validated vectors, and the actor-model's single-threaded
  method execution (no reentrancy across a split's transfers; token transfers
  are CIP-20 syscalls inside one method execution).
- **Grant/declaration forgery.** All mutating methods authenticate the caller
  against the object's authority field (creator, store operator, or hirer);
  standard actor-call sender authentication, no new signature schemes.
  `declaration_id` preimage verification (§2.1.1) prevents id squatting, and
  §2.1.3 listing consent prevents the inverse forgery — declaring an actor
  you do not control as your listing or template and steering
  protocol-authentic spawn directives at it.
- **Prepaid custody.** The Trading Post holds per-hire balances; only the
  hirer can withdraw, only `renew`/`settle_per_call` can draw, and
  `revoke_hire` refunds automatically. Balance accounting is per-hire — no
  pooled-fund invariants to break.
- **Artifact equivalence is a claim.** A Public Store registration binds
  the immutable source version to the actual serving root after the Store
  verifies the copy (§2.1.2). Consensus checks the destination commitment,
  ownership, visibility, status, and root at registration and pins Public
  volumes against manifest commits, owner transfers, owner deletion, and ID
  reuse while the count remains nonzero. It does not
  compare the source and destination files. A false equivalence claim is
  attributable to the registering operator and can be checked by readers
  of both pinned manifests and their contents. A serving-root attestation
  approves that copy, not the source-to-copy relationship. Private artifacts
  retain the signed root-equality claim in §2.5.1. Readers with decryption
  access can compare their content with the claimed root. Attesters can
  downgrade their verdict (§2.7). A cryptographic equivalence proof remains
  an open question.
- **Lease policy correctness in `cbssd`.** Issuance policy reads chain state;
  `cbssd` hand-rolls chain interaction today and has drifted from node before
  — the policy check MUST be covered by e2e tests against a live devnet node,
  not unit fixtures.
- **Split griefing.** Vector caps (8 entries, no zero shares) bound transfer
  fan-out; beneficiary addresses are not validated as live — a transfer to a
  dead address burns that share, which is the beneficiary's risk, as with any
  CIP-20 transfer.
- **Attestation index growth.** `attest` is open to any address (§2.7), so a
  hostile attester can grow a subject's attestation index without bound. §2.7
  bounds it with the exact `MAX_ATTESTERS_PER_SUBJECT = 1024` cap. The cap
  carries a residual: Sybil attesters can fill a
  subject's index and block *new* attesters from recording verdicts on it —
  accepted, since filling costs gas per record and verdicts from
  unrecognized attesters carry no client trust weight; attesters clients
  rely on SHOULD attest at review time, before an adversary targets the
  root. Spam records themselves carry no trust weight because clients
  filter by attester.
- **Store-side censorship.** Bounded by design: scope-limited pause powers,
  open store registration, direct hire, and public events.
- **Cached-DEK residual** (§2.5.3): accepted, bounded by settlement refusal
  and runner stake; revisit if runner trust assumptions weaken.

## 5. Out of scope

Review policy and curation; commission values; fork attribution economics (CIP-27 follow-up);
creator-code semantics beyond split entries; store UX; fiat display and
funding rails (CIP-18/CIP-28 territory).

## 6. Open questions

1. Cached-DEK hardening for hire volumes. The earlier phrasing here proposed
   "tighter per-epoch DEK rotation … using CIP-24's existing rewrap machinery on
   a shorter cadence", which does not work: rewrapping keeps the DEK, so a
   shorter cadence changes nothing about what a cached key opens (CIP-9 §9.4.1).
   Real rotation requires re-encryption and does not exist yet; its requirements
   and unsolved problems are in CIP-9 §9.4.2–§9.4.3. Until one of those is
   filled, the cached-DEK residual this CIP accepts has no mitigation beyond
   shortening the hire term. Candidate for a CIP-33.r2 once runner-trust data
   exists.
2. `PerCall` rate-limit defaults and MPP session caps for hostile hiring
   actors.
3. Capability-schema registry: whether a curated schema-type catalog (beyond
   free-form JSON Schema) earns its complexity once agent-hire volume exists.
4. Whether `instance_mode: Forked` should become the default once CIP-27
   stabilizes.
5. Per-period escrow of executed splits, enabling protocol-level pro-rata
   clawback on trust-and-safety revocation (v1 refunds only unspent
   `prepaid`, §2.4).
6. Cryptographic plaintext-equivalence proof binding a `ServingArtifact` to
   its version's executable subject (v1: signed claim + social detectability,
   §4).
