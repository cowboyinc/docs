---
title: "CIP-24: Cowboy Secret Service (CBSS)"
description: 'A threshold-IBE secret release service for actors, using BLS12-381 with a drand-style "tlock" construction. Secrets are account-level; owners encrypt to a per-account master public key (MPK) whose master secret (MSK) is never reified — only Shamir-shared across a committee of staked CBSS proxies via threshold BLS DKG (vetted libraries: blstrs, Commonware BLS DKG, tlock). At job time, t of n proxies return BLS partial signatures on a per-version identity; the runner Lagrange-combines them into the IBE decryption key. The dispatcher is not in the trust path and TEE is not the trust root. Defines the on-chain registry, the new SecretsProxy operator role, the threshold cryptography stack, the runner pull flow with per-proxy receipts, the liveness-challenge protocol, and the SDK surface.'
---

<Note>
  **Status:** Landed (regenesis-only)
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-04-29
  **Updated:** 2026-09-22
  **Requires:** CIP-2 (off-chain compute), CIP-6 (SDK + entitlements), CIP-9 (CBFS-backed runner storage), CIP-12 (governance)
  **Related:** [Secrets Whitepaper, revision v0.1.2](../whitepaper/cowboy-secrets-whitepaper.md), [Technical Whitepaper §9](../whitepaper/cowboy-technical-whitepaper.md) (system-actor registry), CIP-23 (TEE Execution — optional additional gate, not the trust root)
</Note>

## 1. Abstract

CIP-24 specifies the **Cowboy Secret Service (CBSS)**: how an account stores third-party API credentials such that

1. plaintext is never visible on chain or to validators,
2. secrets can be deleted forever (no archival ciphertext that becomes decryptable as cryptography ages),
3. only the specific keys an actor names at the call site are released to the runner,
4. release is just-in-time and the dispatcher is not in the trust path,
5. cross-account sharing is supported, and
6. the trust root is **t-of-n proxy non-collusion**, not a hardware enclave.

The on-chain surface is the system actor at `0x0000…0004`. Off chain, CBSS introduces a new staked operator role — the **CBSS Proxy** (`SecretsProxy`) — running the `cbssd` daemon. Secrets are owned by **accounts**, not actors; an account's actors are *consumers* gated by per-secret ACL. Each per-account release keypair `(MPK ∈ G2, MSK ∈ scalar field)` is established by a single threshold BLS DKG ceremony among `n` proxies — once per account, regardless of how many actors or secrets that account ever has. `MSK` is never reified, only Shamir-shared. At release time, any `t` proxies return BLS partial signatures on the per-version identity; the runner Lagrange-combines them into the IBE decryption key and unwraps the DEK. Defaults: `n = 5`, `t = 4`, BLS12-381. The threshold follows the vetted Commonware `N3f1` DKG quorum for the five-proxy default.

For high-value secrets where the per-account blast radius is unacceptable, an owner MAY specify a **per-secret committee override** at `SetSecret` time. The override triggers a one-off DKG for that specific secret-version with its own `(MPK, MSK, committee, t, n)`, isolated from the account default. This is the escape hatch for owners who want a unique committee for `STRIPE_LIVE_KEY` while letting `SLACK_API_KEY` ride the account default.

CBSS rejects TEE as the trust root for secret release. CIP-23 TEE attestation remains available as an *additional* gate per secret (the `tee_required` flag), but the underlying release is unconditionally non-TEE. Rationale in §10.

**Crypto stack:** CBSS uses **BLS12-381** with a **threshold identity-based-encryption (IBE)** construction, equivalent to drand's "tlock" scheme. Vetted libraries are required — `blstrs` for curve ops, the pinned Commonware BLS DKG state machine (or an equivalent vetted threshold-BLS DKG), and `tlock` (drand) for the IBE construction. BLS12-381 is a new curve dependency for Cowboy but does not replace secp256k1 (account keys remain secp256k1); it lives alongside it as a CBSS-specific primitive. See §3.4.

The whole design rests on one structural decision: **the cryptographic principal is a stable on-chain identity (account by default, secret on override) whose private key is never reified — only Shamir-shared across the proxy committee.** This is the load-bearing change that makes pull-without-dispatcher and pull-without-TEE both work. The owner is offline at job time so cannot be the principal; the runner is VRF-selected at job time so per-recipient material cannot be pre-issued; therefore the principal is an on-chain identity whose key materializes only as a t-of-n threshold operation.

## 2. Motivation

### 2.1 The problem

Actors call third-party APIs (Slack, OpenAI, GitHub, Stripe, etc.) that require credentials. Naive solutions — hard-coding into actor source, reading from a CBFS plaintext file, decrypting in actor PVM Python — all violate at least one of:

- **Validator privacy:** validators replay actor execution; anything readable in the PVM is readable on chain.
- **Eventual decryptability:** anything written to chain history is decryptable forever as cryptography ages. Secrets leak retroactively.
- **Runtime isolation:** an actor that can read `OPENAI_API_KEY` should not also be able to read `STRIPE_LIVE_KEY` just because the same account owns both.
- **Dispatcher trust:** a dispatcher that can unwrap credentials or volume DEKs becomes a plaintext custodian. CBSS keeps the dispatcher off the key path for both.

### 2.2 Design philosophy

The protocol provides the **minimal release surface**: encrypted at-rest in CBFS, threshold-released through a registered proxy network, and delivered into an in-memory runner bundle for a single runner-op (with explicit subprocess env maps only when required). Every other concern — rotation policies, audit pipelines, HSM-backed wrapping, DAO-controlled secrets — is built by application-layer actors on top of this primitive. Following the precedent set by CIP-13 §2.1: protocol provides hooks, ecosystem builds products.

### 2.3 Why not TEE-rooted release

CIP-23 ships TEE-attested *execution*. That solves a different problem: bounding the runner host's introspection of in-flight actor state. Using the same anchor for *secret release* would mean every secret on the network becomes retroactively exfiltratable on a single CPU-vendor microarchitectural break (Foreshadow / Plundervolt / SGAxe / CipherLeaks have all happened to deployed TEEs). Secret ciphertext is durable; an enclave compromise is forever for the secrets it ever wrapped.

CBSS instead anchors trust in **t-of-n proxy non-collusion plus economic slashing**. The failure mode (≥ t simultaneous proxy compromises) is independent of any single hardware vendor and is hardenable through operator diversity policy. §9 quantifies the trade.

---

## 3. Specification

**Normative conventions.** The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** in this document are to be interpreted as described in RFC 2119. Parameters marked **governance-tunable** can be changed by on-chain governance (§4).

### 3.1 New data structures

#### SecretId

```
SecretId {
    account:   Address
    key_hash:  Bytes32             // == keccak256(account || key_name); chain never sees plaintext key_name
}
```

`KeyName` (plaintext, regex `[A-Z][A-Z0-9_]{0,127}`, case-sensitive ASCII) appears in CLI / SDK input, in actor manifest entitlements, and in the runner sandbox during a runner-op. **Chain instructions (§3.5) take `key_hash` directly**, never plaintext, so the mempool, tx args, storage paths, and event fields don't observe literal key names. The CLI computes `keccak256(account || key_name)` locally before submitting any tx. Plaintext names are still visible in deployed manifests (validator-readable) and dictionary-guessable for common names (e.g., `OPENAI_API_KEY`); see §3.2 ("H(key) honesty") for the realistic privacy boundary and COW-2442 for the tracked stronger-privacy follow-up.

Keys are namespaced per owning account via the `account` field. Cross-account access is granted at the policy layer (§3.1.5), not the namespace layer.

#### SecretKeyRef

The wire-level secret reference carried inside a job spec — `secret_refs` (the `secrets=` read set, §5.1/§5.2) and `JobPrecondition.secret_ref` (§5.5) — as opposed to `SecretId`, which keys CBSS's own on-chain storage. Same logical `{account, key_hash}` pair as `SecretId`, kept as a distinct named type because it is defined and versioned on the job-spec wire — its canonical bytes are owned by `cowboy-protocol-codec::job_spec`, the canonical `JobSpec` encoding (CIP-11 §12.6) — rather than in CBSS storage, but it MUST use the byte-identical `key_hash` derivation so a `SecretKeyRef` submitted in a job resolves to the matching `SecretId` in CBSS storage:

```
SecretKeyRef {
    account:   Address (20 bytes)
    key_hash:  [u8; 32]
}

key_hash = keccak256( account || key_name )   // byte-identical to SecretId's key_hash recipe above — same inputs, same order, no separator
```

`SecretKeyRef` is a plain struct on the wire (positional `account` then `key_hash`, no enum tag). It carries **no plaintext key name**, satisfying the "chain instructions take `key_hash` directly" rule above. On the SDK surface (§5.2) a caller writes either a bare `"KEY_NAME"` or the explicit cross-account form `{"account": ..., "key": ...}`; a bare name resolves to **the owning account of the actor issuing the runner-op** — the actor's manifest `secrets.*` entitlements are what authorize the release (§5.1), so the actor's owning account is the only sound default, and implementations MUST NOT resolve a bare name to any other account (e.g. the transaction submitter). In both forms the client library fills `account` and computes `key_hash` locally before the job spec is submitted — the job-spec-layer counterpart of the CLI's own local `SecretId` hashing described above — so the wire shape is always the explicit pair: `account` is required on every `SecretKeyRef`, and there is no implicit-account variant on the wire.

#### SecretMetadata

The on-chain record for a secret. Stored at `0x04` keyed by `SecretId`.

```
SecretMetadata {
    id:                SecretId
    latest_version:    u64
    active_versions:   Vec<u64>                    // index of currently-active version numbers (NOT inlined SecretVersion records); each version's full state lives at secret_version:{account}:{H_key}:{version_be8}. Pruned per MAX_VERSIONS_PER_SECRET.
    created_at:        BlockHeight
    last_updated_at:   BlockHeight
}
```

#### SecretVersionHighWaterMark

The durable, monotonic floor for version allocation. A `u64` stored at `0x04` keyed by `SecretId` (byte layout in §3.2). Written by every `SetSecret` to the version it just allocated; only ever increased, and **never lowered or deleted** — it is retained across `DeleteSecretVersion`, `DeleteSecret`, `RotateCommittee` `force_rekey`, and expired override DKGs. It is a first-class committed record, **not** derived state: an implementer working only from the instruction Effects sections MUST NOT garbage-collect it. The allocation rule and the non-reuse rationale are in §3.2, "Secret version allocation and non-reuse".

#### SecretVersion

Immutable per `(SecretId, version)`. Created by `SetSecret`; deletable by `DeleteSecretVersion`.

```
SecretVersion {
    version:         u64
    pending:         bool                 // true between SetSecret(override-path) and FinalizeSecretVersion; false otherwise. Release attempts on pending versions fail with SecretPending.
    recipient:       ReleaseKeyRef        // always set; points to AccountReleaseKey (default) or per-secret SecretReleaseKey (override). For override path, the SecretReleaseKey may not exist yet (DkgPending) until RotateCommittee commits it.
    policy:          SecretPolicy         // always set
    // The following three fields are populated when pending=false. On the override path they are None at SetSecret time and filled in by FinalizeSecretVersion. Default-path SetSecret populates them immediately.
    wrap_epoch:      Option<u64>          // committee_epoch at the time this version was wrapped; IMMUTABLE once set
    cbfs_pointer:    Option<CbfsPointer>  // CIP-9 envelope; private volume. The CIP-9 envelope carries the outer DEK→plaintext encryption's nonce/AAD bytes (CBSS does not duplicate them here), but their value is fixed by this CIP: the outer AAD MUST equal the extended U-bound aad of §3.4.3 step 4 (see step 5).
    wrapped_dek:     Option<WrappedDek>   // single envelope (DEK encrypted under IBE-derived K); WrappedDek carries its own nonce, ephemeral_u, and aad (see §3.5). aad is canonically derivable from (account, key_hash, version, wrap_epoch, ephemeral_u) so SecretVersion does not store it separately.
    created_at:      BlockHeight
    creator_sig:     Bytes                // tx-level sender signature is the source of truth; the chain records the signing block and signer in event metadata. NO separate finalizer_sig — FinalizeSecretVersion is itself a tx and its sender signature serves the same role.
}

// Invariant: pending == true ⇔ wrap_epoch.is_none() ⇔ cbfs_pointer.is_none() ⇔ wrapped_dek.is_none()
// On override-path SetSecret: pending = true and ALL three Option fields are None until FinalizeSecretVersion lands. wrap_epoch is set THEN, not at SetSecret time.

enum ReleaseKeyRef {
    Account(Address),                     // wraps under AccountReleaseKey[Address].mpk (default path)
    SecretSpecific(SecretId, u64),        // wraps under SecretReleaseKey[(SecretId, version)].mpk (override path)
}
```

A single `wrapped_dek` regardless of ACL size: ACL is purely a runtime gate, not a cryptographic recipient list. Adding or removing an actor from the ACL is a metadata-only update with no chain re-wrap. Storage cost per version is `O(1)`.

#### SecretPolicy

```
SecretPolicy {
    actors:        Option<Vec<ActorAddress>>     // None = all of owner's actors at release time
    tee_required:  bool                          // additional gate; layered on top of threshold release
}
```

`actors == None` resolves at release time to "any actor whose owning account == this secret's owning account." `actors == Some([...])` is an explicit allowlist and MAY include actors owned by other accounts (cross-account grant; unilateral, no acknowledgment from the recipient account required).

**Effective access** = (actor's CIP-6 manifest declares the key under the entitlement matching the release **purpose** — `secrets.read` for the `secrets=[...]` read set, `secrets.verify` for a `verify=` MAC check, §5.5) **AND** (`SecretPolicy` allows the actor) **AND** (if `tee_required`, runner provides a valid CIP-23 attestation). The purpose is derived from the stored `JobSpec`, precondition-first: a key named by `JobPrecondition.secret_ref` releases as the verify key (gated by `secrets.verify`); a key named in `secret_refs` releases as a read-set key (gated by `secrets.read`) — unambiguous because the §5.5 overlap rule forbids the same `{account, key_hash}` in both roles within one job. A key held only under `secrets.verify` is releasable for the MAC check but MUST NOT be substituted via `secrets=[...]`, and vice-versa.

#### AccountReleaseKey (default path)

The threshold-shared per-account keypair. Stored at `0x04` keyed by `Address`. Created by the DKG ceremony (§3.6.1); rotated by reshare (§3.6.2). Reused across all of an account's secrets that don't specify a per-secret override.

```
AccountReleaseKey {
    account:              Address
    mpk:                  G2Point                  // BLS12-381 G2, 96 B compressed (master public key); preserved across reshare
    committee:            Vec<ProxyId>             // current committee, exactly `n` distinct entries; mutable on reshare
    threshold:            u8                       // `t`, with 1 ≤ t ≤ n
    committee_epoch:      u64                      // increments on each successful reshare; tracks share-polynomial version, NOT ciphertext binding
    last_reshare_block:   BlockHeight
    vss_commitments:      Vec<G2Point>             // per-proxy VSS commitments S_i = s_i · G2 under the current committee_epoch; n entries
}
```

`committee_epoch` is bookkeeping for the share polynomial. It does **not** appear in the IBE identity or AEAD AAD — the identity is bound to a per-version `wrap_epoch`, and the AEAD AAD is that same canonical base plus the per-wrap `ephemeral_u` (see §3.4.3). PSS preserves `MSK` across reshare, so a proxy's *current* share validly partial-signs any identity ever wrapped under `MPK`, regardless of the wrap_epoch on the original SecretVersion.

#### SecretReleaseKey (override path)

Same shape as `AccountReleaseKey` but scoped to a single `(SecretId, version)`. Stored at `0x04` keyed by that pair. Created lazily when an owner submits `SetSecret` with `committee_override = Some(...)`. Allows per-secret committee parameters (size, threshold, member preference) for high-value secrets that should not share blast radius with the rest of an account's secrets.

```
SecretReleaseKey {
    secret_id:            SecretId
    version:              u64
    mpk:                  G2Point
    committee:            Vec<ProxyId>             // exactly `n` distinct entries
    threshold:            u8
    committee_epoch:      u64
    last_reshare_block:   BlockHeight
    vss_commitments:      Vec<G2Point>
}
```

A `SecretReleaseKey` is single-use in the sense that it is only consumed by its one owning `(SecretId, version)`. Deleting that secret-version (`DeleteSecretVersion` or `DeleteSecret`) cascades to the deletion of the corresponding `SecretReleaseKey` and emits a `ShareZeroizationRequested` event; share zeroization on committee members is best-effort, not chain-enforced (see `DeleteSecret` Effects in §3.5).

#### CbssProxy

The on-chain record of a registered proxy operator. Stored at `0x04` keyed by `ProxyId`.

```
CbssProxy {
    id:               ProxyId
    operator:         Address
    pubkey:           PublicKey                 // secp256k1; used for runner→proxy authentication
    hpke_pubkey:      HpkePublicKey             // X25519 HPKE key used for recipient-specific DKG/reshare payloads
    tls_spki:         Bytes                     // DER SubjectPublicKeyInfo pin for QUIC TLS verification
    network_addr:     NodeAddr                  // QUIC endpoint
    stake:            U256                      // CBY wei; ≥ MIN_PROXY_STAKE
    registered_at:    BlockHeight
    eligible_after:   BlockHeight               // registered_at + PROXY_SOAK_PERIOD
    health:           ProxyHealth               // updated by liveness pings; affects VRF weight
    suspended:        bool                      // true while a slashing case is open
    unbond_at:        Option<BlockHeight>       // Some iff DeregisterCbssProxy called
}
```

#### WrappedDek (IBE envelope)

The encrypted DEK on chain. AES-256-GCM with the IBE-derived key K (see §3.4.3). No public-key envelope per recipient — the IBE construction means there is exactly one ciphertext regardless of ACL size, and the recipient is implicit in the per-version identity `I = hash_to_G1( u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2, domain = "cbss/ibe/v1" )`. The encrypted wrap still needs the Boneh-Franklin encryption ephemeral `U = r · G2_gen`; `U` is public KEM material, not a recipient key.

```
WrappedDek {
    version:      u8           // format-version tag == 2 (WRAPPED_DEK_FORMAT_VERSION); leading byte/field of every serialization
    ciphertext:   Bytes        // AES-256-GCM(K, DEK, nonce, aad)
    nonce:        [u8; 12]
    ephemeral_u:  [u8; 96]     // = compress(U), U = r · G2_gen ∈ G2; canonical compressed BLS12-381 G2 encoding, length-validated
    aad:          Bytes        // == u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u
}
```

Notation: `U` denotes the G2 group element and `ephemeral_u = compress(U)` denotes its stored 96-byte canonical compressed encoding; formulas over the struct field use `ephemeral_u` directly. In `aad`, `u64_le(version)` is the **secret version number** (`SecretVersion.version`, u64) — not this envelope's 1-byte format-version tag, which is never part of `aad` or the key derivation. `u64_be(chain_id)` is the deploying chain's 8-byte big-endian chain identifier (the same value bound into tx signatures and release requests), and `mpk_g2` is the 96-byte canonical compressed encoding of the resolved release key's master public key `MPK` (i.e. `compress(MPK)` — the exact bytes of the `mpk` field on the `AccountReleaseKey`/`SecretReleaseKey` this version is wrapped to). **Endianness is deliberate and load-bearing: `chain_id` is big-endian, `version` and `wrap_epoch` are little-endian, and `mpk_g2` is raw 96-byte compressed G2.** Owner-side encryption and chain-side validation MUST agree byte-for-byte; any change here is wire-breaking.

The serialized `WrappedDek` format version is `2`, carried as a **leading 1-byte version tag** on every serialization (the first byte of the on-chain codec, the leading field of CBOR/JSON forms). Readers MUST reject any other tag value with a typed error (`UnsupportedWrappedDekVersion`; §5 error table). The tag is wire framing only: it is NEVER part of the AEAD `aad` or the key derivation. `aad` validation is full equality, not suffix equality:

```
aad == u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u
```

Implementations MUST reject malformed AAD with `AadMismatch` when `len(aad) != 172 + 96` (i.e. `!= 268`), when the prefix is not the canonical 172-byte `base_aad` the chain re-derives for the `(chain_id, SecretId, version, wrap_epoch, mpk_g2)` tuple, or when the final 96 bytes do not equal `ephemeral_u`. (The length condition is exact equality — a longer AAD with a valid prefix and valid `ephemeral_u` suffix is still malformed, per the full-equality rule above.) The chain derives `chain_id` (its own) and `mpk_g2` (from the resolved release key's committed `mpk`, **never** from the envelope), so an envelope wrapped under a substituted `mpk'` produces a `base_aad` that does not match the chain's re-derivation and is rejected here with `AadMismatch` — the same fail-closed check that gives the IBE identity its release-key binding (§3.4.3).

### 3.2 Storage layout in `0x04`

**Privacy scope.** Plaintext key names are kept off chain in **storage paths and tx args** — see §3.5 instructions, all of which take `key_hash: Bytes32` directly. Plaintext key names DO appear in:

1. **CLI / SDK input** (the user types `OPENAI_API_KEY` into `cowboy secrets set`).
2. **Actor manifest entitlements** (`secrets.read.keys: [...]`). Manifests are owner-deployed and validated by the chain; their entitlement keys are visible to validators, indexers, and any node that fetches the manifest. Owners who require unguessable key names should use random suffixes there too (see §5.1).
3. **The runner sandbox** during a single runner-op (as the substituted plaintext value's identifier).

The chain's storage paths and tx args use **`key_hash = keccak256(account || key_name)`**. Validators and chain observers see only `key_hash` in storage paths, tx args, and event fields. This is dictionary-guessable for common names — see "H(key) honesty" below — but does prevent passive scraping.

The [Secrets Whitepaper §2](../whitepaper/cowboy-secrets-whitepaper.md#2-storage-shapes) defines the complete physical key inventory and each family's creation/deletion lifecycle, including maintenance and co-located TEE rows. The principal secret and evidence keys are:

```
secret:                         + account[20] + key_hash[32]              → SecretMetadata
secret_version_hwm:             + account[20] + key_hash[32]              → u64 allocation floor
secret_version:                 + account[20] + key_hash[32] + version[8] → SecretVersion
secret_version_tombstone:       + account[20] + key_hash[32] + version[8] → SecretVersionTombstone
account_secret_index:           + account[20]                             → Vec<Bytes32>
account_release_key:            + account[20]                             → AccountReleaseKey
secret_release_key:             + account[20] + key_hash[32] + version[8] → SecretReleaseKey
cbss_release_key_evidence:      + recipient.encode()                      → sorted Vec<u64> epoch index
cbss_release_key_evidence:      + recipient.encode() + ':' + epoch[8]    → ReleaseKeyEvidenceEpoch
```

These values are JSON; versions and epochs in keys are big-endian `u64` bytes. Spaces and `+` above are notation only: literal prefixes concatenate with raw bytes, with the extra literal `:` only in the evidence-epoch key. The high-water mark key is exactly `b"secret_version_hwm:"[19] ‖ account[20] ‖ key_hash[32]` (71 bytes). Prior serving-grace epochs are embedded in the release-key records. A tombstone carries `deleted_at_block`, `was_pending` and optional public `{version, recipient, wrap_epoch}` evidence context; it contains no ciphertext. Evidence history has a per-recipient index and one public value per epoch, with the admission/retention rules in §3.5.

**Secret version allocation and non-reuse.** Every `SetSecret` — on both the default and override paths — allocates the next version as `next_version = max(SecretMetadata.latest_version, secret_version_hwm) + 1`, computed with checked `u64` addition. Allocation fails closed only when the floor `max(SecretMetadata.latest_version, secret_version_hwm)` is already `u64::MAX`, so no successor fits — the instruction is then rejected and no state is written. (Allocating the version number `u64::MAX` itself succeeds, when that floor is `u64::MAX - 1`.) On success the chain writes three entries in the *same* state batch — the new `SecretVersion`, the updated `SecretMetadata`, and the durable high-water mark `secret_version_hwm:{account_hex}:{H(key)_hex}` set to `next_version`.

The high-water mark is **monotonic and irreversible**: it is only ever advanced, never lowered or deleted. It survives either delete instruction, account force-rekey and override DKG expiry. `SecretMetadata.latest_version` selects the greatest surviving entry in `active_versions` for a versionless read; both `DeleteSecretVersion` and override `ExpireDkgPending` recompute it. If no active versions remain, the metadata and account secret-index entry are removed. A selected pending version still fails the pending-release gate. This read-selection rule never frees a version for reuse: the next `SetSecret` allocates strictly above the durable mark.

Producers MUST take the version from the chain and MUST NOT infer `latest + 1` or assume `1` after a "not found". The node exposes a bounded read-only route, `GET /cbss/secret-next-version/{account}/{key_hash}` → `{ "next_version": u64 }`, returning the same `max(latest_version, secret_version_hwm) + 1` result (failing closed only when that floor is already `u64::MAX`, so no successor fits). Every `SetSecret` producer — the `cowboy` CLI, the SDK, and the dashboard — takes `next_version` from this route; on the override path the caller-supplied `SecretSpecific(SecretId, next_version)` MUST equal the chain's allocated value or the instruction is rejected. A node that does not serve this route is a hard error for a producer, which MUST NOT fall back to a locally guessed version.

Checkpoint and state-restore tooling MUST preserve all CBSS state: the `0x04` actor-storage inventory and the `0x21` `CbssPhaseState` families described in the Secrets Whitepaper §2. This includes high-water marks, tombstones/evidence context, evidence indexes and epoch values, sweep indexes, reshare-outstanding markers, DKG pending/ceremony records, the reshare sweep cursor, and reshare outbox metadata/chunk pages. Tooling MUST carry each high-water mark verbatim: expired pending override versions are erased without tombstones, so surviving records cannot reconstruct the allocation floor.

The durable allocation floor prevents a recreated `(account, key_hash)` from reusing an earlier version's release identity or deletion authority (COW-4074). Access rows are maintained in node's [`docs/access/CLIENT-LIMITS.md`](https://github.com/cowboyinc/node/blob/55022cf80e71475bf0d0f7c00041a401ba10fea9/docs/access/CLIENT-LIMITS.md).

For `tee_required` secrets, the logical TEE Verifier is `0x05`, but node stores the attestation and trusted-key rows physically under `0x04`: `tee_attestation:` plus job ID and runner, and `tee_trusted_key_index:` plus type length, type bytes and measurement hash. The Secrets Whitepaper §2 gives the value types and lifecycle. The signed-attestation path verifies the job/runner-bound payload against registered trusted keys; CBSS does not accept a proxy-supplied TEE assertion.

Plaintext key names appear only in: (a) the owner's local CLI, (b) actor manifest entitlements (which are owner-controlled and meant to be public anyway), (c) the runner sandbox during a single runner-op. Chain state never holds them.

**H(key) honesty.** The statement above — "validators and chain observers see only `key_hash`, not the key name" — is a true wire-format claim, but it MUST NOT be read as a privacy guarantee. `key_hash = keccak256(account || key_name)` is **dictionary-recoverable**: `account` is a public on-chain address that appears in the very same storage path/tx/event as `key_hash`, and `key_name` is, by the §3.1 regex, a short, human-chosen, typically low-entropy string (`OPENAI_API_KEY`, `STRIPE_LIVE_KEY`, `SLACK_SIGNING_SECRET`, and similar). There is no salt, per-account or otherwise, in this hash. A validator — or anyone who knows the account address and holds a wordlist of common key names — can recompute `keccak256(account || guess)` for each candidate and test it against the on-chain `key_hash` in negligible time; this is a brute-force dictionary attack against a *known, small* input space, not a preimage attack against `keccak256` itself.

Consequently, key-name privacy under CIP-24 is **best-effort, not absolute**, and is only meaningfully strong for **high-entropy key names** (e.g., an owner-appended random suffix, as suggested in point 2 above). Owners who need confidentiality of *which service a secret is for* SHOULD NOT rely on a common or guessable name being hidden by `key_hash` alone. What `key_hash` reliably provides is protection against **passive, non-targeted scraping** — hiding the literal string from the wire so a chain indexer, log line, or casual observer doesn't see it verbatim — not resistance to an adversary who is deliberately guessing that account's key names.

A salted or otherwise name-concealing `key_hash` construction (a CBSS-keyspace change so `key_hash` is no longer computable from public inputs plus a guess alone) is tracked as follow-up work in **COW-2442** and is out of scope for this CIP.

### 3.3 Master opcode allocation update

The canonical protocol codec assigns opcodes **68–84** to CBSS. The allocation is:

| Opcode | Instruction | Source |
|---:|---|---|
| 68 | `SetSecret` | CIP-24 §3.5 |
| 69 | `UpdateSecretPolicy` | CIP-24 §3.5 |
| 70 | `DeleteSecretVersion` | CIP-24 §3.5 |
| 71 | `DeleteSecret` | CIP-24 §3.5 |
| 72 | `RegisterCbssProxy` | CIP-24 §3.5 |
| 73 | `DeregisterCbssProxy` | CIP-24 §3.5 |
| 74 | `RotateCommittee` | CIP-24 §3.5 |
| 75 | `SlashCbssProxy` | CIP-24 §3.5 |
| 76 | `SubmitReleaseReceipt` | CIP-24 §3.5 (per-proxy submission) |
| 77 | `RequestAccountDkg` | CIP-24 §3.5 |
| 78 | `FinalizeSecretVersion` | CIP-24 §3.5 (supports two-phase override flow) |
| 79 | `SubmitLivenessChallenge` | CIP-24 §3.5 (anchors a runner's liveness challenge against a proxy) |
| 80 | `LivenessChallengeResponse` | CIP-24 §3.5 (proxy's response to a challenge) |
| 81 | `ExpireDkgPending` | CIP-24 §3.5 (permissionless cleanup of an expired DkgPending record + bond settlement) |
| 82 | `ExpireLivenessChallenge` | CIP-24 maintenance (permissionless transition of expired pending challenges) |
| 83 | `RequestReshare` | CIP-24 reshare maintenance (owner-triggered committee refresh for an existing release key) |
| 84 | `ForcedDeregisterCbssProxy` | CIP-24 / CIP-12 governance remediation (governance-mediated proxy removal for persistent faults or disclosed leaks) |
| 152 | `RegisterTlockRelease` | CIP-24 §3.4.6 / §3.5 (time-lock release registration; consumed by CIP-34 sealed auctions) |
| 153 | `SubmitTlockRelease` | CIP-24 §3.4.6 / §3.5 (committee posts a height-gated partial; permissionless read) |
| 158 | `GovRequestSystemDkg` | CIP-24 §3.5 / CIP-34 §Sealed-Bid (governance-authorized DKG bootstrap for a keyless system-actor committee, e.g. `0x14`) |

Opcodes `152`, `153`, and `158` are the canonical assignments. The opcode-uniqueness test guards allocation conflicts.

TEE Verifier support instructions take opcodes **60–63** and serve the logical `0x05` TEE Verifier, whose attestation/trust records are physically stored at `0x04` (§3.2): `RegisterTeeTrustedKey` (60), `RevokeTeeTrustedKey` (61), `SubmitTeeAttestation` (62), and `RevokeTeeAttestation` (63). These are live in `node/types/src/execution.rs` (`SYS_REGISTER_TEE_TRUSTED_KEY=60` etc.) and are **the canonical assignments** for these slots.

### 3.4 Cryptography

#### 3.4.1 Vetted-library mandate

CBSS does not roll its own crypto. The protocol is built as a thin (~few hundred lines) integration layer over published, externally-reviewed primitives:

| Primitive | Library | Vetted-ness |
|---|---|---|
| BLS12-381 curve operations + pairings | `blstrs` (Filecoin-maintained) | Production at Ethereum, Filecoin, Drand mainnet scale |
| Hash-to-curve (G1) | `blstrs` (RFC 9380) | Same |
| Threshold BLS DKG | Commonware BLS DKG or equivalent vetted threshold-BLS DKG | Pinned Commonware `N3f1` state machine in the devnet implementation; external integration review before mainnet |
| Threshold IBE construction | `tlock` (drand reference) | Drand mainnet randomness beacon ships this in production for time-lock encryption |
| Symmetric encryption | `aes-gcm` (RustCrypto) | RustCrypto-reviewed; ubiquitous |
| KDF / hash | `hkdf`, `sha2`, `sha3` (RustCrypto) | Standard |

The integration glue (~few hundred lines: identity construction, ciphertext envelope, validation, share storage layer) lives in `runner/crates/cbss-crypto`. **Proactive secret sharing** (committee resharing) is the one sub-protocol where a vetted Rust library does not yet exist; CBSS implements a thin wrapper following drand's published resharing protocol. External crypto review is required before launch.

#### 3.4.2 Curve choice: BLS12-381

CBSS uses **BLS12-381**, not secp256k1 (which Cowboy account keys use). Reasons:

1. **Vetted threshold-encryption libraries exist on BLS12-381.** Drand, Lit, the Ethereum-adjacent ecosystem, and Filecoin all ship BLS12-381 threshold tooling at production scale. On secp256k1 the equivalent threshold-encryption tooling does not exist in vetted form; a CBSS implementation on secp256k1 would require hand-rolling the DKG, partial decryption, and NIZK glue — exactly the cryptographic engineering anti-pattern this CIP rejects.
2. **Pairing-friendly groups** make threshold IBE possible with the simple Boneh-Franklin construction: encryption to an "identity" plus a threshold BLS signature on that identity is the IBE decryption key. No bespoke key-switching primitive required.
3. **Production scale** — Ethereum's beacon chain, Filecoin proofs, Drand all use BLS12-381 at ≥ million-share scale, so library quality is high and corner cases are well-understood.

The cost: BLS12-381 is a new curve dependency for Cowboy. It does **not** replace secp256k1 — account keys, runner registry keys, and proxy registry keys remain secp256k1. BLS12-381 lives alongside as a CBSS-internal primitive used only for threshold encryption. Pairings are 1–3 ms on modern hardware; well within the 250 ms p50 release latency budget.

#### 3.4.3 Construction: threshold IBE ("tlock" applied to secret release)

The construction is standard Boneh-Franklin-style threshold IBE plus threshold BLS signatures, applied with a CBSS-specific identity binding instead of a future-round binding. As in Boneh-Franklin IBE and drand `tlock`, each owner-side encryption includes a fresh encryption ephemeral `r` and publishes `U = r · G2_gen` in the envelope.

**Pairing orientation (used throughout this CIP):** the BLS12-381 pairing is `e: G1 × G2 → GT`. `I` lives in G1; `MPK`, `S_i`, and `U` live in G2; `σ` and `σ_i` live in G1. All pairing expressions follow this orientation: G1 element first, G2 element second.

**Epoch model:** a SecretVersion is bound at SetSecret time to the **wrap_epoch** = the release key's `committee_epoch` at that moment. `wrap_epoch` is then immutable. Reshare changes the release key's `committee_epoch` and rotates the share polynomial, but PSS preserves `MSK`, so a proxy's *current* share validly partial-signs any identity ever derived under any historical `wrap_epoch` of the same release key. The IBE identity binds `chain_id`, `wrap_epoch`, and `mpk_g2`; the AEAD AAD binds all of those plus the per-wrap `ephemeral_u`; neither binds the live `committee_epoch`.

**Why `mpk_g2` is the right release-key field to bind (soundness of the serve-vs-wrap asymmetry).** The base AAD binds the *raw* `mpk_g2` rather than a committee/threshold/VSS commitment because `mpk` is the **only** reshare-invariant piece of release-key material: a reshare provably preserves `MPK` (the chain rejects a reshare whose `new_mpk != prior_release_key.mpk`; §3.5 RotateCommittee), while committee membership, `threshold`, and `vss_commitments` all rotate under PSS. At release time the identity's `wrap_epoch` field is *historical* (read from the immutable SecretVersion) while the `mpk_g2` fed to the pairing comes from the *current* release-key record — this is sound precisely because reshare preserves `mpk`, so the current `mpk` equals the wrap-time `mpk` for every epoch the chain will still serve. The one case where they diverge is a `force_rekey` re-DKG, which installs a fresh `MPK` **and** simultaneously strands every ciphertext bound to the old one (§3.5) — so the `mpk_g2` binding never fails a release that would otherwise have succeeded. Binding any rotating field instead (e.g. `release_key_material_hash`) would strand a secret the moment its wrap-epoch's material ages out; account secrets have no re-wrap path, so they MUST bind the invariant `mpk`. (CIP-9, which has an epoch-scoped re-wrap model, binds `release_key_material_hash` instead — see §9.3. The retired CIP-7 committee path used that binding too; replacement Watchtower does not use CBSS key delivery. The rule that generates all three: bind the strongest release-key commitment the family's re-wrap model can carry.)

**Setup (per `AccountReleaseKey` or `SecretReleaseKey`):**

1. The n CBSS proxies in the committee run a threshold BLS DKG (the devnet implementation uses the pinned Commonware BLS DKG state machine; an equivalent vetted threshold-BLS DKG may be substituted by governance-reviewed implementation update). Round-2 share payloads are recipient-specific and HPKE-encrypted to each recipient's on-chain `CbssProxy.hpke_pubkey`. If timeout or complaint handling excludes a dealer, the daemon finalizes over an explicit qualified dealer set of at least `t` members. Output: master public key `MPK ∈ G2`, Shamir shares `s_1 … s_n ∈ Fr` of the implicit master secret key `MSK ∈ Fr`. Threshold `t`. `MSK` is never reified.
2. `MPK` and the per-proxy commitments `S_i = s_i · G2 ∈ G2` are published on chain in `AccountReleaseKey` or `SecretReleaseKey`.

**Canonical implementation.** Account-secret AAD and identity construction are single-sourced in `cowboy-protocol-cbss-crypto` (`aad.rs`, `identity.rs`) and checked with shared golden vectors. The account-secret layout below does not apply to time-lock release (`cbss/tlock/v1`, §3.4.6), CIP-9 volume DEKs (`cbss/ibe/cip9-volume-dek/v1`, §9.3), or the retired CIP-7 content-key family (`cip-7-wd-v1`). Each active family uses its own specified identity and AAD; replacement Watchtower key delivery is defined in CIP-7 and does not use CBSS.

**Storage (per secret-version):**

3. Let `wrap_epoch` = the release key's `committee_epoch` at SetSecret/FinalizeSecretVersion time, and `mpk_g2 = compress(MPK)` the 96-byte compressed master public key of that release key (`AccountReleaseKey.mpk` on the default path, `SecretReleaseKey.mpk` on the override path). Define the **172-byte** base AAD:
   `base_aad = u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2`
   where `canonical(SecretId) = account(20) || key_hash(32)` (§3.5), so the fixed-width layout is `chain_id(8) ‖ account(20) ‖ key_hash(32) ‖ version(8) ‖ wrap_epoch(8) ‖ mpk_g2(96) = 172` bytes. The leading `chain_id` is the **chain separation** binding: a secret wrapped for chain A is rejected by chain B's validation and its released σ cannot unwrap it. The trailing `mpk_g2` is the **release-key binding**: a secret wrapped under a substituted `mpk'` hashes to a different identity, so the real committee's σ can never open it. Owner derives the per-version identity using this canonical encoding (§3.5 SubmitReleaseReceipt):
   `I = hash_to_G1( base_aad, domain = "cbss/ibe/v1" )`
   (RFC 9380 hash-to-curve). `I ∈ G1`. The owner, the proxies, and the chain all compute `I` from these same inputs in the same byte encoding. `I` is derived from the 172-byte `base_aad`, not from the extended AEAD AAD below.
4. Owner samples a fresh encryption ephemeral scalar `r ←$ Fr` from a CSPRNG for every wrap, with `r != 0`, and computes `U = r · G2_gen ∈ G2` (96-byte compressed). The owner MUST NOT reuse `r` across secret versions. Define the AEAD AAD:
   `aad = base_aad || compress(U)`.
   Owner computes the IBE encryption key for `I`:
   `K = HKDF-SHA256( serialize(e(I, MPK)^r), "cbss/ibe/v1" || aad )`
   where `e(I, MPK)^r ∈ GT` is equivalently `e(I, r · MPK)` or `e(r · I, MPK)`, and `serialize` is the canonical compressed GT encoding (exactly 288 bytes; `blstrs` implements `Compress for Gt`).
   The notation `HKDF-SHA256( ikm, info )` is normative throughout this CIP and means RFC 5869 HKDF with **salt = the ASCII bytes `"cbss/ibe/v1"`** (the same 11-byte domain string as the `info` prefix — not absent, not empty), `info` as written, and output length **L = 32 bytes**. The salt is load-bearing: a derivation with `salt = None`/empty produces a different `K`, so omitting it makes every wrap undecryptable cross-implementation.
5. Owner generates a fresh DEK (256 bits, CSPRNG). AES-256-GCM-encrypts the plaintext value with DEK + a fresh nonce + the **172-byte `base_aad`** (the value envelope binds `base_aad`, **not** the `U`-extended `aad` — the value envelope's key `DEK` is independent of `U`, so it binds only the chain- and release-key-bound base). Uploads the AES-GCM ciphertext to a private CBFS volume → `cbfs_pointer`. Because the stored value envelope binds `base_aad`, any change to `chain_id`, `canonical(SecretId)`, `version`, `wrap_epoch`, or `mpk_g2` invalidates the ciphertext of the secret value itself — not merely its DEK wrap. Reconstructing a value envelope with different bound fields requires the plaintext; changing only the DEK wrap is insufficient.
6. Owner AES-256-GCM-encrypts DEK with `K` + a separate nonce and the **268-byte extended `aad`** from step 4 (`base_aad || compress(U)`), producing `wrapped_dek`. Only the DEK wrap binds the extended AAD; the chain re-derives and full-equality-checks all 268 bytes at SetSecret/FinalizeSecretVersion (§3.5).
7. Owner publishes `wrapped_dek + cbfs_pointer + wrap_epoch + recipient + policy` on chain via `SetSecret` (default path) or `FinalizeSecretVersion` (override path; see §3.5). `wrapped_dek` includes `ephemeral_u = compress(U)`.

**Release (per job):**

8. Runner signs the `ReleaseRequestBody` directly with its **runner-registry key** (the secp256k1 key registered with `0x01` Runner Registry, the same key it uses for any other authenticated runner→chain interaction). No runner-binding ephemeral keypair is involved — the IBE construction does not require a runner-bound ciphertext, so there is no per-request runner ephemeral. This is distinct from the owner-chosen Boneh-Franklin encryption ephemeral `r` in step 4. Both proxies (at PartialSign serve time) and the chain (at SubmitReleaseReceipt validation) verify `runner_sig` against the runner registry.
9. Runner reads the SecretVersion (carrying `wrap_epoch`) and the referenced ReleaseKey (carrying current committee + `S_i`s) from chain. Runner sends a release request to t proxies. Each proxy validates ACL/manifest/job-assignment/anti-replay (§3.7).
10. Each proxy computes the identity exactly as the owner did, using the SecretVersion's `wrap_epoch` and the canonical encoding from step 3:
    `I = hash_to_G1( u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2, domain = "cbss/ibe/v1" )`,
    then its **partial signature**:
    `σ_i = s_i · I ∈ G1` (using the proxy's *current* share s_i — PSS guarantees this works regardless of the relationship between `wrap_epoch` and the proxy's current share epoch).
11. Each proxy returns `σ_i` to the runner. **Verification is the standard BLS partial-signature check**: validate `e(σ_i, G2_gen) == e(I, S_i)` where `S_i ∈ G2` is the proxy's published VSS commitment. Implementations MUST reject identity/point-at-infinity `S_i` and `σ_i` before the pairing check; otherwise `e(0, G2_gen) == e(I, 0)` passes vacuously. No bespoke NIZK required — the BLS pairing equation is its own proof of correctness.
12. Runner combines t partial signatures via Lagrange interpolation in G1:
    `σ = Σ λ_i · σ_i = MSK · I ∈ G1`
    (the full threshold BLS signature on `I`, equivalently the IBE decryption key for `I`).
13. Runner reads `wrapped_dek.ephemeral_u` (decoding it to `U`; §3.1 notation), reconstructs the same extended `aad = base_aad || ephemeral_u`, and derives `K = HKDF-SHA256( serialize(e(σ, U)), "cbss/ibe/v1" || aad )`. By bilinearity, `e(σ, U) = e(MSK · I, r · G2_gen) = e(I, G2_gen)^(MSK · r) = e(I, MPK)^r` — the same `K` the owner computed at step 4.
14. Runner AES-256-GCM-decrypts `wrapped_dek` with `K` → DEK.
15. Runner fetches the CBFS object via `cbfs_pointer`, AES-256-GCM-decrypts ciphertext with DEK + the **172-byte `base_aad`** (the value envelope's AAD; §3.1) → plaintext.

Implementations MUST reject degenerate pairing inputs before any pairing or key derivation: identity/point-at-infinity `MPK`, `U`, `σ`, `I`, `S_i`, and `σ_i`; non-canonical compressed encodings; or points outside the prime-order subgroup. `blstrs` compressed decoding enforces curve/subgroup membership but accepts the identity, so identity rejection is an additional requirement. Implementations MUST also enforce `MAX_IBE_AAD_BYTES = 4096` and `MAX_IBE_CIPHERTEXT_BYTES = 1 MiB` (§4) for every CBSS IBE envelope.

#### 3.4.4 Security properties

- **t-of-n threshold:** any t honest proxies can complete a release; t-1 cannot reconstruct `σ` and therefore cannot derive `K`. Given public `(I, MPK, U)`, deriving `K` without `σ` requires computing `e(I, MPK)^r = e(I, G2_gen)^(MSK · r)` from `(G2_gen, MPK = MSK · G2_gen, U = r · G2_gen)`, reducing to the co-BDH assumption on BLS12-381.
- **Owner-chosen `U`:** a malformed owner-chosen `r`/`U` weakens or bricks only that owner's own secret. Binding `compress(U)` into the AAD makes post-publish tampering a hard AEAD failure rather than a silent mis-derivation.
- **No new steering surface:** `r` is supplied by the secret owner for that owner's own secret. Proxies and runners treat `U` as opaque KEM material; reshare/PSS properties are unchanged because the committee still releases `σ` for the same identity `I`.
- **Per-`(secret_id, version, wrap_epoch)` σ:** a leaked `σ` decrypts only the wrapped DEK for that exact identity. Compromising a different secret version requires a separate t-of-n collusion to derive its σ. Reshare does NOT invalidate σ values for prior identities; PSS preserves `MSK` and hence preserves σ for any identity I.
- **No bespoke NIZK:** BLS partial-signature verification is a single pairing check, covered by `blstrs` and the vetted threshold-BLS implementation.
- **Anti-replay, ACL, audit:** enforced at the proxy validation layer (§3.7) and via per-proxy receipts (§3.5). Cryptographic envelope intentionally does NOT bind to a runner ephemeral key — see §3.4.5 for why.
- **Reshare:** proactive secret sharing on `MSK` rotates shares without changing `MPK`. Stored secrets remain decryptable. In-flight release requests are NOT invalidated by reshare; a proxy's freshly-resharded share still produces valid partials for any prior-epoch identity.
- **Insider rotation:** the value of reshare is bounding the window during which a specific share-holder's compromised share is useful. After reshare, that specific s_i is replaced; the attacker must re-compromise the new s_i' to keep contributing to t-of-n.

#### 3.4.5 Why threshold IBE, not threshold ElGamal with key-switching

The candidate alternative is threshold ElGamal on secp256k1 with a custom "key-switching to runner ephemeral pubkey" construction. Two problems:

1. **No vetted Rust library exists.** Threshold ElGamal on secp256k1 requires hand-rolling the DKG, partial-decrypt construction, and Chaum-Pedersen NIZK. Standard cryptographic-engineering hygiene says: don't roll your own crypto when a vetted equivalent exists. BLS12-381 + threshold IBE has vetted equivalents (Commonware BLS DKG, `tlock`, `blstrs`); secp256k1 + threshold ElGamal does not.
2. **Key-switching to runner ephemeral pubkey** was solving a problem we don't actually have. The runner is the legitimate recipient of DEK; cryptographically binding the ciphertext to a runner-ephemeral pubkey adds protocol complexity without buying additional security beyond what proxy-side anti-replay/ACL already provide. Threshold IBE produces DEK directly to the runner; the runner zeroizes after use; revocation/replay/audit happen at validation time, not in the envelope.

This does **not** remove the Boneh-Franklin encryption ephemeral. Every owner-side wrap still MUST sample a fresh `r` and publish `U = r · G2_gen` in `WrappedDek`. That ephemeral is independent of runner identity; it is the KEM randomness that makes `K` unrecoverable from public `(I, MPK, U)`.

The threshold-IBE construction is what drand's `tlock` ships in production. CBSS uses the same primitive with a CBSS-specific identity binding.

#### 3.4.6 Time-Lock Release mode (height-gated, public identity)

§3.4.3 applied threshold IBE "with a CBSS-specific identity binding **instead of** a future-round binding." This subsection re-introduces tlock's **native** time-lock binding as a **second release mode** on the *same* committee — for consumers (e.g. CIP-34 sealed-bid auctions) that need a key which **becomes available at a future height, bound to a caller-chosen identity, and released permissionlessly**, independent of any participant. It adds **no new key material, no new DKG, and no new trust assumption**: it reuses the committee's existing `MPK`, shares `s_i`, and PSS guarantees.

**Two modes, disjoint by domain separation.**

| | Secret release (§3.4.3) | Time-lock release (this subsection) |
|---|---|---|
| Identity domain | `"cbss/ibe/v1"` | `"cbss/tlock/v1"` |
| Identity binding | `chain_id ‖ SecretId ‖ version ‖ wrap_epoch ‖ mpk_g2` | caller `tag ‖ target_height` |
| Release trigger | signed `ReleaseRequest`, **ACL-gated** (§3.7) | chain height ≥ `target_height`, **permissionless** |
| Who may obtain `σ` | the authorized runner only | anyone (public) |
| Protects | confidentiality (access) | *time* (until a height) |

The domains are distinct, so a release `σ` for one mode can **never** derive the key for the other — the two identity spaces are disjoint in G1 by RFC 9380 domain separation. **A tlock identity MUST NOT be used to wrap an account secret** (its key is public at `target_height`); the domain tag enforces this structurally, not by policy.

**Identity.** For a caller-chosen `tag: Bytes` (e.g. a CIP-34 `request_id`) and reveal height `target_height: u64`:

```
I_tlock = hash_to_G1( canonical(tag) || u64_le(target_height), domain = "cbss/tlock/v1" )   ∈ G1
```

**Encryption** reuses the §3.4.3 Boneh-Franklin *construction* — the encryptor samples a fresh `r ←$ Fr`, publishes `U = r · G2_gen`, and derives `K = HKDF-SHA256( serialize(e(I_tlock, MPK)^r), "cbss/tlock/v1" || aad )` against the committee's existing `MPK`. (The HKDF salt/info domain is `"cbss/tlock/v1"`, matching the identity domain — load-bearing, as in §3.4.3.)

> **Scope boundary (normative): time-lock release does NOT inherit the §3.4.3 account-secret AAD.** "Identical to §3.4.3" above scopes to the pairing/HKDF *construction*, not to the AAD contents. Time-lock release keeps its **own** base AAD — `base_aad_tlock = tag ‖ u64_le(target_height)` (for CIP-34 sealed bids, `tag = request_id`, so `base_aad_tlock = request_id(32) ‖ u64_le(reveal_height)`; CIP-34 §Sealed-Bid), extended to `aad = base_aad_tlock ‖ compress(U)` for the AEAD. It **does not** gain `chain_id` and **does not** gain `mpk_g2`; the §3.4.3 chain-separation and release-key bindings apply **only** to the account-secret family. Base AADs in this CIP are **per-family and do not inherit** across families (the same rule that keeps CIP-9's volume path on its own base AAD, §9.3). The tlock/CIP-34 key-substitution exposure is therefore **not** closed by an AAD change (a submit-time mismatch would arrive too late, and there is no on-chain envelope validation to attach it to); it is addressed by authenticating the committee `MPK` at the encryptor against the release key's `release_key_material_hash` before sealing, and by an independent committee-side reveal-height check — both tracked as separate workstreams, not as changes to this base AAD. See CIP-34 for the sealed-bid scoping statement.

**Release — the only structural difference.** Once chain height ≥ `target_height`, each committee proxy computes its partial on the tlock identity with its **current** share and posts it on chain:

```
σ_i = s_i · I_tlock   ∈ G1          (SubmitTlockRelease, §3.5)
```

verified by the chain with the standard BLS check `e(σ_i, G2_gen) == e(I_tlock, S_i)` (identity/point-at-infinity rejection as in §3.4.3). The accumulated partials are a **public** chain artifact: **anyone** reads ≥ `t` of them, Lagrange-combines `σ = Σ λ_i · σ_i = MSK · I_tlock`, and derives `K = HKDF-SHA256( serialize(e(σ, U)), "cbss/tlock/v1" || aad )`. There is **no §3.7 validation** (no ACL, no job-assignment, no runner-binding, no anti-replay on the *reader*) — time-lock release is public by construction; the only gate is the height. **Single-epoch quorum (consensus-critical, twin of §9a's `MixedEpochReceipts`):** the partials MUST all be from one `committee_epoch` — the Lagrange x-coordinates are proxy positions in that epoch's committee, so a cross-epoch combine yields the wrong `σ`. The partial set records the `committee_epoch` of its first accepted partial as its `quorum_epoch`; if a reshare advances the committee while the set is **below** threshold, the set is RESET to the new epoch (stale, now-uncombinable partials discarded) so collection can still complete. A combiner MUST combine only same-`quorum_epoch` partials; if the live committee has advanced past a threshold-reached set's `quorum_epoch`, the release is treated as **not-yet-releasable** (deterministic) rather than silently mis-combined — the consumer retries or hits its grace fallback (CIP-34 §Sealed-Bid).

**Registration + bounded committee work.** A tlock release is registered on chain (`RegisterTlockRelease { tag, target_height }`, §3.5) so the committee knows what to sign and when. Registration is fee-bearing and bounded: a per-block cap on new registrations, and a registered request (plus its posted partials) is GC'd after `target_height + TLOCK_RETENTION` (§4). A proxy MUST reject `SubmitTlockRelease` while `head < target_height` (premature reveal) and for an unregistered `(tag, target_height)`.

**Reuse + reshare.** A proxy's current share `s_i` validly partial-signs *any* identity under `MPK`, tlock identities included (PSS preserves `MSK`; §3.4.4). Reshare does not invalidate a registered tlock request — a freshly-resharded share still produces a valid partial for `I_tlock`.

**Liveness is the consumer's to handle.** The committee-liveness assumption is identical to secret release. If fewer than `t` partials are posted by `target_height + GRACE`, the **consumer** owns the fallback — e.g. CIP-34 cancels the auction (`CancelledGraceExpired`) at `reveal_height + AUCTION_GRACE` (CIP-34 §Sealed-Bid); as built it does not re-open a plaintext OPEN round. CBSS itself adds no time-lock fallback; it exposes the height-gated release and the consumer composes the timeout.

**Security delta vs §3.4.3.** Confidentiality holds **only until `target_height`** (by design); before it, the co-BDH argument of §3.4.4 applies unchanged (`t-1` proxies cannot derive `σ`). Because the identity binds `tag`, the release for one `tag` reveals **nothing** about another `tag` at the same height — so one auction's reveal cannot expose another auction's still-sealed bids. The public-at-height property is exactly why the domain separation from account-secret identities is mandatory.

### 3.5 New system instructions

Each instruction's `Sender` column gives who is authorized to submit it. Gas costs in §4.1.

#### SetSecret (opcode 68)

The instruction is **shape-asymmetric across the two recipient types** because in the override path the recipient `MPK` does not exist at SetSecret time — it must be produced by a fresh DKG ceremony first. The default path stays one-shot; the override path is two-phase, with `FinalizeSecretVersion` (opcode 78) attaching the ciphertext after DKG finalizes.

```
SetSecret {
    key_hash:             Bytes32                        // == keccak256(sender || key_name); plaintext key_name never crosses the wire
    recipient:            ReleaseKeyRef                  // Account(sender) for default; SecretSpecific(SecretId, next_version) for override
    policy:               SecretPolicy
    // Default path (recipient = Account(_)) carries ciphertext immediately:
    cbfs_pointer:         Option<CbfsPointer>            // Some on default path; None on override path. CIP-9 envelope; carries the outer plaintext-encryption nonce internally.
    wrap_epoch:           Option<u64>                    // Some on default path (== AccountReleaseKey.committee_epoch); None on override path
    wrapped_dek:          Option<WrappedDek>             // Some on default path (carries inner DEK-wrap nonce + ephemeral_u + aad); None on override path
    // Override path carries committee parameters:
    committee_override:   Option<CommitteeOverride>      // Some on override path; None on default path
}

struct CommitteeOverride {
    n:                 u8                     // 5 ≤ n ≤ MAX_OVERRIDE_N
    t:                 u8                     // MIN_OVERRIDE_T ≤ t ≤ n
    preferred_proxies: Option<Vec<ProxyId>>   // hints; final committee is VRF-selected from this set if Some
}

struct WrappedDek {
    version:      u8                 // format-version tag == 2 (WRAPPED_DEK_FORMAT_VERSION); leading byte/field of every serialization (§3.1)
    ciphertext:   Bytes              // AES-256-GCM ciphertext of the DEK
    nonce:        [u8; 12]
    ephemeral_u:  [u8; 96]           // = compress(U), U = r · G2_gen ∈ G2; canonical compressed BLS12-381 G2 encoding
    aad:          Bytes              // == u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u
}
```

**Sender:** owner of the account.

**Effects:**

- **Default path (`recipient = Account(sender)`):** must carry `cbfs_pointer + wrap_epoch + wrapped_dek` (DEK encrypted under the IBE key derived from the account's existing `MPK`; nonces, `ephemeral_u`, and AAD are inside `WrappedDek` and the CIP-9 CBFS envelope). The instruction is rejected with `MissingAccountReleaseKey` if the sender has no `AccountReleaseKey` on file (the `cowboy` CLI auto-issues `RequestAccountDkg` on first use). Allocates `version = max(SecretMetadata.latest_version, secret_version_hwm) + 1` (checked `u64` add; fails closed only when that floor is already `u64::MAX`), creates `SecretVersion(version)` under `SecretId(sender, key)` with `pending = false`, and advances the durable high-water mark to that version — see "Secret version allocation and non-reuse" in §3.2. Emits `SecretCreated`.

- **Override path (`recipient = SecretSpecific(SecretId, next_version)`):** must NOT carry `cbfs_pointer / wrap_epoch / wrapped_dek`. Instead carries `committee_override`. Effects:
    1. Creates `SecretVersion(version = next_version)` with `pending = true`, `wrap_epoch = None`, `cbfs_pointer = None`, `wrapped_dek = None`, where `next_version` MUST equal the chain's allocation `max(SecretMetadata.latest_version, secret_version_hwm) + 1` (the instruction is rejected otherwise; see "Secret version allocation and non-reuse" in §3.2). Advances the durable high-water mark to `next_version`. (The §3.1 invariant requires all three Option fields are None on a pending version.) `wrap_epoch` is populated by `FinalizeSecretVersion` once `MPK` exists.
    2. Locks `DKG_BOND` from the sender, VRF-selects an n-proxy committee per the override spec, and writes a `DkgPending(SecretSpecific(SecretId, next_version), committee, deadline, bond_amount, requester = sender)` record.
    3. Off-chain DKG runs (§3.6.1). On commit, the committee submits `RotateCommittee { scope: SecretSpecific(...), new_committee_epoch: 1, ... }`, which writes the new `SecretReleaseKey` and exposes `MPK` on chain.
    4. Owner reads `MPK + committee_epoch` from chain, sets `wrap_epoch = committee_epoch` (= 1 here), samples fresh `r != 0`, computes `U = r · G2_gen`, derives `K = HKDF(serialize(e(I, MPK)^r), "cbss/ibe/v1" || aad)` with `aad = u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || compress(U)`, encrypts DEK to `K`, uploads CBFS ciphertext, and submits `FinalizeSecretVersion` (opcode 78) to attach `cbfs_pointer + wrapped_dek + wrap_epoch` and clear `pending`.
    5. Until step 4 lands, release attempts against this version fail closed with `SecretPending`.

**Validation rules** (chain-enforced):

- Default path: `wrapped_dek.version == 2` (§3.1; rejects with `UnsupportedWrappedDekVersion` otherwise); `wrap_epoch == account_release_key.committee_epoch` at SetSecret block; `wrapped_dek.ephemeral_u` is the canonical compressed encoding of a non-identity G2 point in the prime-order subgroup (subgroup membership REQUIRED; §3.4.3); and `wrapped_dek.aad == u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || wrapped_dek.ephemeral_u`, where `chain_id` is this chain's identifier and `mpk_g2 = compress(account_release_key.mpk)` — both taken from committed state loaded one step above (the `AccountReleaseKey` already in hand for the `wrap_epoch` check), **never** from the envelope. Rejects with `WrapEpochMismatch` / `AadMismatch` otherwise. A `wrapped_dek` sealed under a substituted `mpk'` (or for another chain) therefore fails this full-equality check with `AadMismatch` — the chain-side half of the release-key binding (§3.4.3).
- Override path: `committee_override.n` and `t` within `(MIN_OVERRIDE_T, MAX_OVERRIDE_N)`; `preferred_proxies` (if Some) all eligible.
- Both paths: `key_hash` is a well-formed 32-byte value; quotas not exceeded. (Plaintext `KeyName` regex enforcement is **CLI/SDK-side**, since the chain never sees plaintext.)

Emits `SecretCreated { id, version, recipient_kind, pending: bool, ... }`.

#### FinalizeSecretVersion (opcode 78)

Attaches ciphertext to a `pending = true` `SecretVersion` after its DKG ceremony has committed. Used only on the override path.

```
FinalizeSecretVersion {
    key_hash:      Bytes32
    version:       u64
    wrap_epoch:    u64                  // == secret_release_key.committee_epoch at this submission's block
    cbfs_pointer:  CbfsPointer          // CIP-9 envelope; carries outer plaintext-encryption nonce internally
    wrapped_dek:   WrappedDek           // carries inner DEK-wrap nonce + ephemeral_u + aad
}
```

**Sender:** owner of the account.

**Effects:** validates that:

- `SecretVersion` exists, has `pending = true`, and `recipient = SecretSpecific(_, version)`.
- The corresponding `SecretReleaseKey` is committed (exists on chain) and its `committee_epoch ≥ 1`.
- `wrap_epoch == secret_release_key.committee_epoch` at FinalizeSecretVersion block (no resharing in between, OR the owner explicitly accepts re-wrapping under the new epoch).
- `wrapped_dek.version == 2` (§3.1); rejects with `UnsupportedWrappedDekVersion` otherwise.
- `wrapped_dek.ephemeral_u` is the canonical compressed encoding of a non-identity G2 point in the prime-order subgroup (subgroup membership REQUIRED; §3.4.3).
- `wrapped_dek.aad == u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || wrapped_dek.ephemeral_u`, with `chain_id` this chain's identifier and `mpk_g2 = compress(secret_release_key.mpk)` derived from committed state (the override path's `SecretReleaseKey`), never from the envelope; a substituted `mpk'` or wrong-chain wrap fails with `AadMismatch`.

On success, sets `cbfs_pointer`, `wrap_epoch`, `wrapped_dek` on the `SecretVersion` and clears `pending`. From this point release proceeds normally.

Emits `SecretVersionFinalized { id, version }`.

An installed but unfinalized version remains pending and consumes a quota slot until finalized or deleted. If its key has not been installed, `ExpireDkgPending` is the cancellation path (§3.5).

#### UpdateSecretPolicy (opcode 69)

```
UpdateSecretPolicy {
    key_hash:    Bytes32
    version:     u64
    new_policy:  SecretPolicy
}
```

**Sender:** owner of the account.

**Effects:** replaces the policy on the named `SecretVersion`. **No re-wrap is required** — the cryptographic recipient is the account (or per-secret override key), not the individual ACL actors. ACL changes are pure metadata and propagate atomically to all subsequent release attempts. ACL semantics on update: **replace, not merge** (the new policy fully supersedes the old).

This is one of the operational wins of per-account anchoring vs per-actor: granting or revoking an actor's access is a single chain tx with no proxy round-trips, no re-encryption, no chain-storage fan-out.

Emits `SecretPolicyUpdated`.

#### DeleteSecretVersion (opcode 70)

```
DeleteSecretVersion { key_hash: Bytes32, version: u64 }
```

**Sender:** owner.

**Effects:** removes the version, including its policy and actor grants, and writes a permanent `SecretVersionTombstone` with `deleted_at_block`, `was_pending` and public evidence context (§3.2). For an installed override it retains public committee evidence, then removes the dedicated `SecretReleaseKey`, its `rksweep:` entry and its `cbss_reshare_outstanding:` marker. Shared account release keys and their sweep/reshare state survive, as do per-proxy reverse-index entries, retained evidence and the high-water mark. Metadata and the account secret index follow the release-selection rule in §3.2. Removing the version removes its on-chain CBFS pointer; this handler does not issue a CBFS object-drop instruction.

A pending override whose dedicated key is not yet installed is rejected: the owner cancels it through `ExpireDkgPending`. An installed but unfinalized override can be deleted; its tombstone has `was_pending = true` and no evidence context because it was never releasable. Subsequent release authorization rejects a deleted version.

The chain emits `cbss.secret.version_deleted` with `account[20] ‖ key_hash[32] ‖ version u64 BE`. For override keys it also emits `cbss.share.zeroization_requested`: exactly 68 bytes, `account[20] ‖ key_hash[32] ‖ version u64 BE ‖ epoch u64 BE`. Epochs are deduplicated and sorted; whole-secret deletion visits versions in ascending order, giving version-then-epoch ordering. At most 256 zeroization hints are emitted per transaction. Hints are best-effort: the committed tombstone is authoritative even when further hints are omitted. The daemon treats a finalized tombstone as authoritative revocation of that version. Tombstone reconciliation selects local shares for erasure only when their complete public epoch matches the committed observation; a `was_pending` tombstone supplies no erasure epochs, and unmatched material remains untouched. A direct zeroization hint separately targets a local share with the exact secret-version scope and epoch. Revocation therefore does not imply unconditional erasure of pending or unmatched material, and chain execution cannot prove that every operator erased its copies.

#### DeleteSecret (opcode 71)

```
DeleteSecret { key_hash: Bytes32 }
```

**Sender:** owner.

**Effects:** applies the same cascade atomically to every active version, then removes `SecretMetadata` and its account secret-index entry. If any version cannot be deleted, the whole transaction rolls back, including prior version writes and events. Shared account keys and the durable high-water mark survive. The chain emits the bounded zeroization hints above and `cbss.secret.deleted` with `account[20] ‖ key_hash[32] ‖ latest_version u64 BE`, using the metadata value read for the deletion; it does not emit a separate `cbss.secret.version_deleted` for each version.


#### RegisterCbssProxy (opcode 72)

```
RegisterCbssProxy {
    pubkey:        PublicKey
    hpke_pubkey:   HpkePublicKey
    tls_spki:      Bytes                  // DER SubjectPublicKeyInfo pin for QUIC TLS verification
    network_addr:  NodeAddr
    stake_amount:  U256                  // ≥ MIN_PROXY_STAKE; locked from operator's account
}
```

**Sender:** any account with sufficient CBY balance.

**Effects:** locks `stake_amount` CBY in the proxy's escrow, mints a fresh `ProxyId`, writes `CbssProxy` with `eligible_after = current_block + PROXY_SOAK_PERIOD`. The proxy is not VRF-eligible until soak completes.

#### DeregisterCbssProxy (opcode 73)

```
DeregisterCbssProxy { proxy_id: ProxyId }
```

**Sender:** the registered proxy operator.

**Effects:** sets `unbond_at = current_block`. Stake remains locked for `UNBOND_COOLDOWN`. If the proxy is currently a member of any `AccountReleaseKey.committee` or `SecretReleaseKey.committee`, `RotateCommittee` is enqueued for each affected release key. After cooldown elapses with no slashing case, stake is returned and the `CbssProxy` record is purged.

#### RotateCommittee (opcode 74)

```
RotateCommittee {
    scope:               ReleaseKeyScope
    new_committee:       Vec<ProxyId>          // exactly `n` distinct, all eligible
    new_committee_epoch: u64                   // == old + 1; for fresh DKG (initial), == 1
    new_mpk:             G2Point               // 96 B compressed; on initial DKG this is the freshly-generated MPK; on reshare this MUST equal the prior MPK (chain enforces; PSS preserves MSK so MPK is preserved)
    vss_commitments:     Vec<G2Point>          // n entries, S_i = s_i · G2 ∈ G2 (matches §3.4 verification e(σ_i, G2_gen) == e(I, S_i))
    new_committee_sigs:  Vec<Signature>        // ≥ t signatures FROM new_committee MEMBERS attesting to the DKG output. REQUIRED on both initial DKG and reshare. Each sig is over the unified `rotate_hash` (domain "cbss/rotate-committee/v1"; full big-endian preimage in §"Canonical domain-separated signature payloads") under the proxy's registered identity key.
    prior_committee_sigs: Vec<Signature>       // ≥ t signatures from PRIOR committee attesting they zeroized old shares. REQUIRED on reshare; OMITTED on initial DKG (no prior committee exists). Each sig is over the SAME `rotate_hash` as new_committee_sigs (domain "cbss/rotate-committee/v1", not a separate preimage) under the prior proxy's identity key.
}

enum ReleaseKeyScope {
    Account(Address),
    SecretSpecific(SecretId, u64),
}
```

**Sender:** any one member of the new committee, on behalf of the committee. The submitter's authority comes from the threshold signature set in `new_committee_sigs`, not from being the lone submitter.

**Effects:** verifies release-key material and threshold committee signatures. A completed rotation installs the targeted `AccountReleaseKey` or `SecretReleaseKey` with its new committee epoch. On normal reshare, the previous committee and commitments are appended to the release key's embedded `prior_committee_epochs`, with `expires_at = current_block + RESHARE_GRACE_BLOCKS`; entries with `expires_at < current_block` are pruned first. If the vector would exceed `MAX_PRIOR_COMMITTEE_EPOCHS` (4), rotation is rejected until enough prior epochs expire. Account force-rekey clears serving grace; overrides reject force-rekey. Rotated-out public material is separately retained for slashing under the evidence rules below.

For epoch selection, a receipt may use a retained prior committee only when `committee_epoch_at_serve` matches its entry in `prior_committee_epochs` and submission is at or before `expires_at`; an expired or missing epoch is rejected. Old committee members MUST zeroize their old shares before signing the quorum attestation; failure to do so is not directly slashable but the signing under false attestation is future-evidence for a leak case. Existing `SecretVersion` records keep their original `wrap_epoch` (immutable per version); the IBE identity for those versions is unchanged. New `SetSecret` calls after this RotateCommittee bind to the new `committee_epoch` as their `wrap_epoch`.

`mpk` is preserved across reshare (proactive secret sharing); only the share polynomial changes. This is true for both account-scoped and secret-scoped keys.

This instruction is also used to commit the result of an *initial* DKG ceremony triggered by `RequestAccountDkg` or by the override path of `SetSecret`. In that case, `prior_committee_sigs` is empty (there is no prior committee to attest zeroization from), but `new_committee_sigs` is **still required** — at least t signatures from members of the freshly-DKG'd new committee over the canonical DKG-commit payload (see the field comments in the struct above). This is what authenticates the initial DKG output; without it, a single nominated proxy could commit an arbitrary `(MPK, S_i)` set.

#### SlashCbssProxy (opcode 75)

On-chain-provable fault classes are limited to those that admit objective, on-chain evidence. **Liveness is excluded from automatic slashing entirely** — packet-delivery non-receipt is fundamentally not provable on chain (a malicious or broken runner can submit a valid signed request without ever delivering it to the proxy; the proxy cannot prove a network negative). Liveness instead drives a **health score** (§4.4) that affects VRF selection weight, and persistent extreme degradation triggers governance review (CIP-12 Tier 1 `ForcedDeregisterCbssProxy`). The on-chain `SubmitLivenessChallenge` / `LivenessChallengeResponse` remain as the **audit trail** that feeds the health score, but they do not by themselves cause stake loss.

**`PlaintextLeak` is also removed** from protocol-level slashing because its evidence ("here is the recovered DEK plus t-1 partials") would publish the secret on chain. Off-chain disclosure → governance → `ForcedDeregisterCbssProxy` is the channel.

```
SlashCbssProxy {
    proxy_id:    ProxyId
    fault_class: FaultClass
    evidence:    SlashEvidence
}

enum FaultClass {
    InvalidPartialReencrypt,             // invalid blinded partial or authenticated out-of-range serve stamp
    DkgSabotage,                         // VSS commitment / share mismatch in DKG round 2
    Equivocation,                        // Two distinct signed responses (same request_hash, distinct payloads)
}
```

**Sender:** any account that holds the corresponding evidence.

**Effects:** evaluates the evidence per fault class (§4.3). On valid evidence, applies the schedule penalty to `CbssProxy.stake`, sets `suspended = true` pending governance review.

**Invalid-partial evidence admission.** The V4 evidence contains the runner-signed request and proxy-signed blinded response. Let `R = request_block`, `S = served_at_block` and `anchor = clamp(S, R, R + REQUEST_FRESHNESS_BLOCKS)`, with `REQUEST_FRESHNESS_BLOCKS = 384`. Submission is admissible from `R` through `anchor + CBSS_EVIDENCE_WINDOW_BLOCKS` (512 blocks), inclusive. Height addition saturates at `u64::MAX`. The verifier authenticates the request, blinding authorization, proxy response and committee position before accepting a fault. An authenticated stamp outside `[R, R + 384]`, in either direction, is itself a fault even if the partial's pairing is valid. Inside that range only an invalid/malformed partial is slashable; a valid pairing with an in-range stamp is never slashable, regardless of submission time.

For a deleted version, the verifier uses its public tombstone context and requires `R ≤ deleted_at_block` and `was_pending == false`. If the serving epoch is no longer current, the verifier point-reads its retained public `ReleaseKeyEvidenceEpoch`, requires `R ≤ last_served_at`, and also requires submission at or before `last_served_at + 512`. Both deletion and historical-epoch eligibility compare the **runner-signed request block**, never the accused proxy's stamp. Retention ends at the committed epoch cutoff plus 512; clamping the stamp does not extend it.

Rotation and override deletion retain a sorted per-recipient epoch index plus one public value per epoch, at most 64 epochs. Each value contains the MPK, ordered committee, threshold, epoch, VSS commitments and `last_served_at`. History pruning removes expired values and rewrites the index when history is next retained. A live override key has at most 28 indexed historical epochs before deletion because force-rekey is rejected for overrides and serving grace permits only four prior epochs within each inclusive 100-block window. The delete cascade also snapshots the current epoch, so post-delete evidence can contain 29 epochs. Evidence history never authorizes serving. Equivocation verification authenticates the request and two conflicting proxy responses directly and needs no release-key history.

#### SubmitLivenessChallenge (opcode 79)

Anchors on chain that a runner experienced a non-response from a proxy. Feeds the proxy's health score; does NOT by itself trigger slashing.

```
SubmitLivenessChallenge {
    runner_request:             SignedReleaseRequestV4 // the original signed request the runner claims went unanswered
    proxy_id:                   ProxyId               // proxy alleged non-responsive
    challenge_sig:              Signature             // exact byte layout: see "Canonical domain-separated signature payloads"
                                                      // following the request hash definitions earlier in §3.5.
                                                      // Signed by the runner named in the request with the "cbss/liveness-challenge/v4" domain prefix.
                                                      // (No challenged_at in the signed payload — chain assigns it on inclusion.)
}
```

`challenged_at` is **NOT a tx arg** — the chain sets it to the inclusion block height when the instruction is processed. This prevents future-dating or backdating attacks on the response-deadline timing.

**Request signer:** the runner identified by `runner_request.body.context.runner_id`.

**Effects:** authenticates the request and challenge, then validates the accepted receipt snapshot:

1. The request's `chain_id` matches this chain. The V4 request signature and outer `challenge_sig` each recover the runner address named in the request.
2. `request_block + MIN_CHALLENGE_DELAY_BLOCKS ≤ submission_block ≤ request_block + LIVENESS_CHALLENGE_MAX_AGE_BLOCKS`, with saturating height addition. The handler sets `challenged_at = submission_block`.
3. An accepted `release_receipt:{release_id}` set exists for the exact request hash and serve epoch, with a nonzero threshold and at least that many receipts and eligible committee members. The challenged proxy belongs to that stored committee snapshot. Every receipt matches the request hash and epoch, and its proxy index matches its committee position; indices are strictly increasing and proxy IDs are unique. This authenticated attempt proof remains valid across later reshares; admission does not re-evaluate current ACL, manifest, job assignment or `SecretVersion` state.
4. **Per-release liveness dedup.** No `liveness_challenge_index:{release_id}:{proxy_id}` entry exists. While the index exists, another challenge for that pair is rejected. A response to a pending challenge and expiry of a pending challenge delete the index; the retained challenge record alone is not a permanent dedup guard. A challenge resolved at admission keeps its index.
5. If the accepted receipt set already contains a receipt from the challenged proxy, the challenge is immediately `Resolved` and the runner forfeits the bond to that proxy.

On success, locks `LIVENESS_CHALLENGE_BOND` from the runner's account and writes `liveness_challenge_index:{release_id}:{proxy_id} → challenge_id` so duplicate challenges are rejected while that index remains. If a receipt for `(release_id, proxy_id)` already exists, the handler writes `LivenessChallenge { id, runner_request, bond_amount, challenged_at = submission_block, status: Resolved, deadline = submission_block }` and transfers the bond to the challenged proxy. Otherwise it writes `LivenessChallenge { id, runner_request, bond_amount, challenged_at = submission_block, status: Pending, deadline = submission_block + LIVENESS_RESPONSE_BLOCKS }`. The proxy has until `deadline` to dispute.

**Admission-time receipt resolution.** Because `MIN_CHALLENGE_DELAY_BLOCKS` is strictly greater than `REQUEST_FRESHNESS_BLOCKS`, every receipt that can pass receipt freshness validation is already either on chain or impossible to submit by the time a liveness challenge is admissible. Runners are expected to check `release_receipt:{release_id}.entries[proxy_id]` before challenging. If they challenge despite an existing receipt, the chain resolves the challenge immediately and transfers the bond to the proxy. The receipt itself does NOT need to be fresh per `REQUEST_FRESHNESS_BLOCKS` for this purpose — persistent `release_receipt` state is the authoritative record of "did the proxy ever submit a paid receipt for this release_id." `Resolved` is therefore an admission-time state, not a response-time state.

If a proxy served but failed to submit a receipt before the receipt freshness window closed, it cannot later create chain-verifiable receipt evidence. Its only on-chain response path is an `Unverifiable` reason (typically `NeverReceived` or structured local-failure evidence), which is health-neutral but does not produce a positive liveness signal.

After `deadline`, a still-`Pending` challenge transitions to `Unanswered`, refunds the bond to the runner, and applies the standard negative health-score decrement to the proxy. **No automatic slash follows.** The challenge is recorded in the proxy's audit history. Persistent unanswered challenges may justify a governance proposal under CIP-12.

#### LivenessChallengeResponse (opcode 80)

A proxy's structured response to a liveness challenge. Records chain-unverifiable response evidence; does NOT by itself prevent governance action.

```
LivenessChallengeResponse {
    challenge_id:  Bytes32
    proxy_id:      ProxyId
    reason:        ChallengeReason
    evidence:      ChallengeResponseEvidence
    proxy_sig:     Signature                         // see "Signed payload" below
}

enum ChallengeReason {
    // NOT chain-verifiable (logged for governance pattern detection):
    InvalidRequest,           // proxy claims the request failed validation at serve time (ACL, manifest, job assignment, sig). NOT auto-resolving because chain re-derivation at response time would use later state and the admission-time snapshot is not stored. Logged with the proxy's structured reason for governance pattern detection.
    RateLimited,              // proxy claims its local rate-limiter rejected the request. Per-actor rate limits are proxy-local sliding-window state and the chain has no way to verify the proxy's claim.
    NeverReceived,            // proxy claims the network request did not arrive; not provable on chain.
}

// Domain-separated signed payload. Implementations MUST sign exactly these bytes:
proxy_sig := sign(proxy_priv,
              keccak256( "cbss/liveness-response/v1" ||
                         challenge_id ||
                         proxy_id ||
                         u8(reason) ||
                         keccak256(serialize(evidence)) ))
```

**Sender:** the challenged proxy.

**Effects:** validates `proxy_sig` over the exact byte layout above against `CbssProxy.pubkey`, then evaluates evidence per `reason`. The chain transitions the challenge to a terminal state with bond disposition that matches the chain's view of who was right:

- **`Resolved`** — not produced by `LivenessChallengeResponse`; produced only by the admission-time receipt check in `SubmitLivenessChallenge`. Chain dereferences `release_receipt:{release_id}` and confirms the entry exists with `proxy_id` matching. Health-score impact on the proxy: small positive (proved liveness via receipt). Bond disposition: forfeited to the challenged proxy.
- **`Unverifiable`** — proxy submits `LivenessChallengeResponse` with `InvalidRequest` / `RateLimited` / `NeverReceived` before deadline. Chain cannot re-derive any of these. **Bond → refunded to the runner** (the runner submitted a good-faith challenge; whether the proxy's response is honest is unknowable on-chain). Health-score impact on the proxy: NEUTRAL — no automatic decrement. Pattern detection lives in off-chain governance.
- **`Unanswered`** — proxy submits no response by `deadline`. The runner was right — the proxy is genuinely silent. **Bond → refunded to the runner**. Health-score impact on the proxy: standard negative decrement. Persistent unanswered patterns flag the proxy for governance review per §4.4.

The runner loses money only when the chain can prove the runner was wrong (`Resolved` via an existing receipt); in all other cases (proxy went silent or proxy gave an unverifiable response) the runner gets the bond back.

**Anti-spam friction.** A naive runner could still try to challenge proxies before they've had time to submit their receipts. To prevent this, `SubmitLivenessChallenge` enforces `body.request_block + MIN_CHALLENGE_DELAY_BLOCKS ≤ submission_block` (see §4.2). Challenges submitted too soon after the original request are rejected with `ChallengePremature`. The default delay is `REQUEST_FRESHNESS_BLOCKS + 32` (~12 min), giving honest proxies a generous window to either serve + submit a receipt (which then resolves any later challenge at admission) or fail loudly.

Only the admission-time receipt check is chain-verifiable and resolves to `Resolved`. `Unverifiable` captures "the proxy responded with chain-unverifiable evidence" without penalizing either party. Repeated `Unverifiable` patterns by the same proxy escalate via off-chain governance, not protocol-level slashing or health-score automation.

#### SubmitReleaseReceipt (opcode 76)

**Per-proxy submission.** The V4 receipt carries the runner's signed blinded request and the proxy's signed blinded response. The chain authenticates both signatures, verifies the blinding authorization and checks `e(tau, G2_gen) == e(blinded_identity, S_i)` against the public share derived from the committed VSS polynomial and the proxy's committee index. The receipt does not publish the unblinded partial `sigma_i`.

```
SubmitReleaseReceiptArgsV4 {
    runner_request: SignedReleaseRequestV4
    response:       PartialSignResponseV4
}

SignedReleaseRequestV4 {
    body:             ReleaseRequestBodyV4
    runner_signature: EthSignature
}

ReleaseRequestBodyV4 {
    context:          ReleaseRequestContextV4
    blinded_identity: G1Bytes
    proof:            BlindingAuthorizationProofV1  // r: G1Bytes, z: [u8; 32]
}

ReleaseRequestContextV4 {
    chain_id:      u64
    secret_id:     SecretId
    version:       u64
    actor:         Address
    runner_id:     Address
    recipient:     ReleaseKeyRef
    job_id:        JobId
    release_nonce: Bytes32
    request_block: BlockHeight
    serve_epoch:   u64
    cip33:         Option<Cip33ReleaseContext>
}

PartialSignResponseV4 {
    request_hash:             Bytes32
    proxy_id:                 ProxyId
    proxy_index:              u32       // one-based position in the ordered committee
    committee_epoch_at_serve: u64
    served_at_block:          BlockHeight
    tau:                      G1Bytes   // blinded partial, 48-byte compressed G1
    proxy_signature:          EthSignature
}

body_v4      := codec(ReleaseRequestBodyV4)
request_hash := keccak256("cbss/release-request/v4" || u32_be(len(body_v4)) || body_v4)

release_id   := keccak256("cbss-release-id/v4" || u64_be(context.chain_id) ||
                         canonical(context.secret_id) || u64_le(context.version) ||
                         context.actor || context.runner_id || context.job_id ||
                         context.release_nonce || u64_le(context.serve_epoch))

canonical(SecretId) := secret_id.account || secret_id.key_hash  // 20 + 32 bytes
```

The codec carries V4 tags and the blinding-proof suite tag; `tee_attested` is not a receipt argument. `release_id` omits `request_block`, while `request_hash` binds the complete encoded V4 body.

**Canonical domain-separated signature payloads.** All sigs use a domain-string prefix to prevent cross-protocol replay; implementations MUST sign exactly these byte layouts:

```
runner_signature (in SignedReleaseRequestV4):
    sign(runner_priv, request_hash)

proxy_signature (in PartialSignResponseV4):
    response_core := codec(PartialSignResponseV4 without proxy_signature)
                     // 133 bytes, including the leading V4 tag and proxy_index
    sign(proxy_identity_priv,
         keccak256("cbss/blinded-partial-response/v4" || response_core))

challenge_sig (in SubmitLivenessChallenge):
    body_v4 := codec(ReleaseRequestBodyV4)
    request_hash_v4 := keccak256("cbss/release-request/v4" || u32_be(len(body_v4)) || body_v4)
    sign(runner_priv,
         keccak256("cbss/liveness-challenge/v4" ||
                   request_hash_v4 ||
                   proxy_id))

response proxy_sig (in LivenessChallengeResponse):
    sign(proxy_identity_priv,
         keccak256("cbss/liveness-response/v1" ||
                   challenge_id ||
                   proxy_id ||
                   u8(reason) ||
                   keccak256(serialize(evidence))))

new_committee_sig / prior_committee_sig (in RotateCommittee):
    BOTH the new committee AND the prior committee sign the SAME rotate_hash,
    domain-separated under the single tag "cbss/rotate-committee/v1"
    (node/execution/src/cbss.rs::rotate_committee_hash). Exact preimage
    (all integers big-endian, no length prefix on the domain):
    rotate_hash = keccak256(
        "cbss/rotate-committee/v1" ||
        scope.encode() ||
        u8(force_rekey) ||
        u32(new_committee.len()) || new_committee[i] (each 32-byte proxy_id, in order) ||
        u64(new_committee_epoch) ||
        new_mpk (96-byte G2) ||
        u32(vss_commitments.len()) || vss_commitments[i] (each 96-byte G2, in order))
```

**Identity binding (used by IBE encryption AND receipt pairing-check, must agree byte-for-byte):**

`base_aad := u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2`

`I := hash_to_G1( base_aad,  domain = "cbss/ibe/v1" )`

The owner (at SetSecret/FinalizeSecretVersion), the proxy (at PartialSign), and the chain (at SubmitReleaseReceipt pairing-check) all compute `I` from the same 172-byte `base_aad` in the same encoding. Here `chain_id` is the chain's own identifier and `mpk_g2 = compress(MPK)` is taken from the release key the chain resolves for the pairing check (§3.6.2 reshare-aware lookup) — not from any client input — so the identity all parties hash is bound to the exact release key. AAD on the AES-GCM `wrapped_dek` envelope extends the same canonical encoding with the Boneh-Franklin encryption ephemeral: `aad = base_aad || wrapped_dek.ephemeral_u`. The chain and runner MUST reject the envelope with `AadMismatch` unless this full equality holds; `len(aad) != 172 + 96` (i.e. `!= 268`) is malformed AAD.

**Response signer:** the operator of `response.proxy_id`; the response signature must recover that registered operator address.

**Chain validation:**

1. Authenticate `runner_request.runner_signature` against the runner address in the V4 context; recompute `request_hash` and `release_id` as above.
2. Check the existing `(release_id, response.proxy_id)` receipt. An exact replay of an accepted receipt returns success, including after freshness expires; a conflicting receipt is rejected.
3. For a new receipt, require `request_block ≤ submission_block ≤ request_block + REQUEST_FRESHNESS_BLOCKS`, using saturating addition, and `request_block ≤ served_at_block ≤ submission_block`. The response hash must match the request, and `committee_epoch_at_serve` must equal the runner's `serve_epoch`.
4. Evaluate current release authorization, including the version, policy, manifest and job/session authority. Authenticate the proxy response and resolve the serving epoch's release key and committee. A retained prior epoch is usable only through its inclusive `expires_at` boundary (§3.5).
5. Require the response's nonzero `proxy_index` to equal the proxy's one-based position in the resolved committee. Verify the runner's blinding authorization against the committed `wrap_epoch` and MPK, derive `S_i` from the VSS polynomial at that index, and check the blinded partial's pairing. Malformed points and failed proofs are rejected.
6. Require every receipt in the set to share the same request hash, epoch, threshold and ordered eligible committee snapshot. A new receipt is rejected once the threshold has already been reached.
7. **TEE eligibility is chain-derived.** The release evaluator checks the required TEE authority in the logical TEE Verifier's records, physically stored at `0x04` (§3.2). It checks a runner's measurement binding and, where the signed-attestation path applies, `tee_attestation:` + `job_id || runner_id`. The proxy cannot supply a `tee_attested` override.

On success, initialize or extend the `ReleaseReceiptSet` with its request/committee snapshot and sort entries by proxy index. Charge the release receipt to the actor and credit the submitting proxy under the payment rules below.

**Overdraft / delinquency.** If `actor.account.balance < RELEASE_FEE_PER_RECEIPT`, the receipt is **still recorded and the proxy is still credited**, with the actor's release-payment balance going negative (capped at `MAX_RELEASE_OVERDRAFT`, governance-tunable, default 16 × RELEASE_FEE_PER_RECEIPT). Implementations whose base account codec cannot represent negative balances MAY store the negative portion in a separate release-debt ledger; the Cowboy node currently stores this as CBSS `ActorReleaseDebt`. The actor account transitions to `delinquent` status, which suspends new job dispatching to that actor until the negative balance is repaid. On Cowboy, `JobSubmit` rejects outstanding `ActorReleaseDebt`; `FundActor` repays that debt before any newly funded balance becomes spendable and emits `cbss.actor.debt_repaid` when repayment is applied. If overdraft would exceed `MAX_RELEASE_OVERDRAFT`, the receipt is rejected with `ActorAccountInsufficient` and no payment occurs — proxies that observe this on chain SHOULD stop accepting `PartialSign` requests from this actor until repayment is observed. This separates "honest proxy was paid for honest work" (always succeeds within overdraft cap) from "actor pays its bills" (enforced by delinquency suspension + cap).

**Each receipt** emits one event:

```
event ReleaseReceiptRecorded {
    release_id:    Bytes32             // stable across t receipts for the same logical release
    request_hash:  Bytes32             // identifies the specific signed request this receipt fulfilled
    account:       Address
    actor:         ActorAddress
    runner:        RunnerAddress
    key_hash:      Bytes32             // == secret_id.key_hash; same caveat as §3.2 (dictionary-guessable)
    version:       u64
    proxy_id:      ProxyId             // single proxy per event
    tee_attested:  bool                 // chain-derived from TEE Verifier state at submission
    block:         BlockHeight
}
```

**`SecretReleased` (quorum event)** is emitted when the t-th receipt for a given `release_id` lands, carrying the actual `proxy_set: [ProxyId; t]` accumulated in `release_receipt:{release_id}`:

```
event SecretReleased {
    release_id:    Bytes32
    account:       Address
    actor:         ActorAddress
    runner:        RunnerAddress
    key_hash:      Bytes32
    version:       u64
    tee_attested:  bool                 // chain-derived; aggregates true iff every contributing receipt observed valid TEE attestation
    proxy_set:     Vec<ProxyId>         // exactly the t proxies whose receipts crossed the quorum threshold
    block:         BlockHeight
}
```

This separates "proxy was paid for honest work" (per-receipt event, one per receipt) from "the secret was successfully released" (quorum event, one per release). The chain knows the threshold `t` from the referenced release key, so the threshold-cross detection is deterministic.

#### RequestAccountDkg (opcode 77)

```
RequestAccountDkg {}                           // sender = the account itself
```

**Sender:** the account owner. Triggered explicitly by an owner or implicitly by the cowboy CLI on first `SetSecret` if no `AccountReleaseKey` exists yet.

**Effects:** Locks `DKG_BOND` from the requesting account. VRF-selects `n = DEFAULT_N` proxies from the eligible set, weighted by `stake × health_score` (see §4.4). Writes a `DkgPending { scope, force_rekey, committee, threshold, deadline = current_block + DKG_FINALIZE_BLOCKS, bond_amount, requester }` record at `dkg_pending:{scope_serialized}` (the record persists `force_rekey` and `threshold` so the off-chain ceremony and any later `RotateCommittee` see the quorum size and re-key intent). Off-chain, the selected committee runs the DKG ceremony (§3.6.1) and posts the result via `RotateCommittee` with `scope = Account(sender)` and `new_committee_epoch = 1`. On successful `RotateCommittee`, the chain refunds `DKG_BOND` in full and clears the `DkgPending` record.

If the DKG ceremony fails to commit by `deadline`, the `DkgPending` record stays on chain until expired by **`ExpireDkgPending`** (see below).

Both `RequestAccountDkg` and the governance path below emit a `cbss.dkg.requested` event with payload `DkgRequestedEventData { scope, epoch, force_rekey, threshold, committee_metadata }` — `epoch` is the ceremony epoch, `committee_metadata` carries the selected proxies' connection details (id / operator / network address / HPKE + DKG public keys), and `force_rekey` signals a re-key vs first bootstrap. Note the event does **not** carry `deadline` (a consumer needing the ceremony deadline reads the `DkgPending` record, not the event). The off-chain committee daemon consumes this event to run the ceremony (§3.6.1).

#### GovRequestSystemDkg (opcode 158, provisional)

```
GovRequestSystemDkg {
    scope: ReleaseKeyScope    // the release-key scope to bootstrap, e.g. Account(0x14)
    force_rekey: bool         // false = first-time bootstrap; true = re-DKG an existing key
}
```

**Sender:** `GOVERNANCE_SYSTEM_ACTOR` (`0x09`) **only** — any other `tx.from` is rejected with `Unauthorized`.

**Invocation (how a `0x09`-origin tx arises).** `0x09` is itself keyless, so — like every `0x09`-authorized instruction (`UpdateSettlementConfig`, `SetGovParam`) — a `GovRequestSystemDkg` tx is **not** externally signed. It is produced by **CIP-12 governance-proposal enactment**: a passed proposal's payload is applied deterministically by `0x09` with `tx.from = 0x09` (CIP-12 §"payloads are executed deterministically by 0x09"). This rides the CIP-12 `ProposalPayloadKind` (`SystemDkgBootstrap { scope, force_rekey }`) that `ExecuteProposal` maps to this instruction.

**Motivation.** `RequestAccountDkg` derives its scope from `sender` (`scope = Account(sender)`) and is authorized by the account's own signature. A **keyless system actor** — one whose address holds native ledger/registry state but has no private key and therefore can never be `tx.from` — cannot use it. The concrete instance is the CIP-34 sealed-bid **`INTENT_SETTLEMENT` committee (`Account(0x14)`)**: sealed-bid reveal (CIP-34 §Sealed-Bid) decrypts every bid against `0x14`'s CBSS committee, so that committee's `AccountReleaseKey` (committee + `MPK` + shares) MUST exist before any sealed auction can clear — but `0x14` cannot self-issue `RequestAccountDkg`. `GovRequestSystemDkg` is the only path that establishes a committee for such a scope, authorized by governance rather than by an owner signature.

**Effects.** Mirrors `RequestAccountDkg` exactly, with three differences: (i) `tx.from` MUST equal `GOVERNANCE_SYSTEM_ACTOR`; (ii) the scope is taken from `args.scope` rather than from `sender`; (iii) **no `DKG_BOND` is locked** (`bond_amount = 0`) — governance has no user account to debit, and the permissionless-cleanup incentive is unchanged because `ExpireDkgPending` on a zero-bond pending simply refunds/slashes nothing. Concretely, on a valid request the chain: VRF-selects `n = DEFAULT_N` proxies weighted by `stake × health_score`; writes `DkgPending { scope, force_rekey, committee, threshold = DEFAULT_T, deadline = current_block + DKG_FINALIZE_BLOCKS, bond_amount = 0, requester }` at `dkg_pending:{scope_serialized}` (`requester` is the scope's account, used only for the — here zero — bond accounting); persists the COW-1057 ceremony record so a sabotaging dealer stays slashable after the pending clears; and emits `cbss.dkg.requested`. Off-chain, the selected committee runs the DKG (§3.6.1) and commits via `RotateCommittee` with `new_committee_epoch = 1` and empty `prior_committee_sigs` — the same initial-commit path `RequestAccountDkg` uses. `ExpireDkgPending` (opcode 81) applies unchanged if the ceremony misses `deadline`.

**Validation.** The request is rejected with `InvalidData` if (a) a `DkgPending` already exists for `scope` (dedup — one ceremony in flight per scope); (b) a release key already exists for `scope` **and** `force_rekey == false`; or (c) **no** release key exists for `scope` **and** `force_rekey == true`. In other words `force_rekey` MUST be set iff the scope already has a live committee — first-time bootstrap uses `force_rekey = false`, re-establishment uses `force_rekey = true`.

**Scope MUST be a keyless system actor (least-privilege).** `scope` MUST resolve to a **keyless system-actor address** — i.e. an address in the reserved system-actor range (`< 0x20`, per WP §9.1) that has no private key, such as `Account(0x14)`. The handler MUST reject any other scope (e.g. `Account(<user>)`, or a `SecretSpecific` under a user account) with `Unauthorized`. Rationale: without this bound, a passed proposal could `GovRequestSystemDkg { scope: Account(<user>), force_rekey: true }` and **re-DKG a normal user's account committee — installing a fresh `MPK` that permanently strands every secret that user ever wrapped**. Keyed accounts never need this path (they self-issue `RequestAccountDkg`), so restricting `scope` to keyless system actors costs nothing and removes an unnecessary governance power over user secrets. (This is a validation the chain *can* check locally, unlike the `force_rekey`-during-open-auction hazard below.)

**`force_rekey` is destructive — a safety MUST.** A `force_rekey` re-DKG produces a **fresh, independent `MPK`** (via the `RotateCommittee` initial-commit path, `new_committee_epoch = 1`), **not** a reshare of the existing `MSK`. Every ciphertext bound to the *old* `MPK` becomes permanently undecryptable — see §10.7 (`MPK` is preserved only on **reshare**, never on a fresh DKG). For the `0x14` sealed-auction committee this means any **in-flight sealed bid** (a `WrappedDek` encrypted to the old `MPK` under `identity = request_id`) can no longer be revealed, and its auction cancels with **no funds moved** (both cancel terminals delete state only). The exact terminal depends on the new committee: if it posts partials for the pre-existing tlock request, `reveal_auction` combines σ under the *new* `MPK`, every old-`MPK` bid fails AEAD, and the auction ends `CancelledNoValidBid` at `reveal_height`; if it posts nothing, the reveal stays `TlockNotReleased` and cancels `CancelledGraceExpired` after `reveal_height + AUCTION_GRACE` (CIP-34 §Sealed-Bid). Either way the bids are stranded. Therefore governance **MUST NOT** `force_rekey` the `0x14` committee while any sealed auction is open (i.e. any `OpenAuction` whose `reveal_height + AUCTION_GRACE` has not passed); doing so silently cancels those auctions. Routine committee refresh that must preserve decryptability MUST use **`RequestReshare` (opcode 83)**, which rotates the share polynomial while holding `MPK` fixed (§10.7); `force_rekey` is reserved for deliberate key replacement (e.g. a disclosed-`MSK` incident) where stranding old ciphertext is the intent. The chain cannot itself observe cross-subsystem auction state, so this is a governance-procedure invariant, not an on-chain check.

#### ExpireDkgPending (opcode 81)

Permissionless cleanup of an expired `DkgPending` record. Settles the bond and emits a public failure event so the requester can retry.

```
ExpireDkgPending {
    scope: ReleaseKeyScope    // identifies the DkgPending entry to expire
}
```

**Sender:** any account.

**Effects:** validates that:

1. `dkg_pending:{scope_serialized}` exists.
2. `current_block >= DkgPending.deadline`, or the sender is the requester cancelling before the deadline.
3. No `RotateCommittee` for this scope has landed (i.e., the DKG didn't complete in some other path).

On success, settles the DKG bond and deletes `dkg_pending:` for the scope. An override's pending `SecretVersion` is erased without a tombstone; metadata and the account secret index follow §3.2's selection rule, while the high-water mark is retained. The ceremony record survives for sabotage evidence. An outstanding-reshare marker is removed only if its fingerprint binds it to this exact pending ceremony; unrelated reshare state survives. The chain emits `cbss.dkg.expired` with the canonical binary `ReleaseKeyScope`.

The requester may retry by submitting a fresh `RequestAccountDkg` (default path) or `SetSecret` with `committee_override` (override path). VRF re-selects a committee — likely overlapping but possibly different given health-score and stake updates.

Per-secret DKG (override path) is initiated by `SetSecret` itself with `committee_override`; there is no separate instruction for it. The same `DkgPending` record is created (keyed by `(SecretId, version)` instead of account) and the same off-chain ceremony commits via `RotateCommittee` with `scope = SecretSpecific(...)`.

#### ExpireLivenessChallenge (opcode 82)

```
ExpireLivenessChallenge { challenge_id: Bytes32 }
```

**Auth:** permissionless (anyone may crank an expired challenge; there is no sender check). **Effects** (`node/execution/src/cbss.rs::handle_cbss_expire_liveness_challenge`):
1. Charge `CBSS_GAS_EXPIRE_LIVENESS_CHALLENGE = 5_000`.
2. Load the challenge; require `status == Pending` AND `current_block > challenge.deadline`, else `InvalidData`.
3. Transition `status → Unanswered` and clear the response fields.
4. `proxy.health.unanswered_challenges += 1`; apply the capped liveness-attrition penalty (COW-1058: `CBSS_LIVENESS_HEALTH_PENALTY_BPS = 100` per unanswered, bounded by `CBSS_MAX_HEALTH_LOSS_PER_EPOCH_BPS = 1000`) to `health.score_bps`.
5. If the penalty crosses below `MIN_HEALTH_FLOOR_BPS` (or sustained sub-threshold ≥ `GOVERNANCE_REVIEW_BLOCKS`), write a `ProxyGovernanceReviewFlag` and emit `cbss.proxy.governance_review_required`.
6. Refund the challenge bond to `runner_request.body.runner_id`.
7. Delete the liveness dedup-index entry; emit `cbss.liveness.expired { challenge_id }`.

No stake is slashed — a missed liveness response is a health-score signal only (§4.4).

#### RequestReshare (opcode 83)

```
RequestReshare { scope: ReleaseKeyScope }
```

**Auth:** release-key **owner** only. **Effects** (`handle_cbss_request_reshare`):
1. Charge `CBSS_GAS_REQUEST_RESHARE = 30_000`; `ensure_cbss_actor_exists`.
2. Load the release-key material for `scope`; `InvalidData` if absent.
3. Require `tx.from == material.owner`, else `Unauthorized`.
4. Push a `reshare_requested` event into the CBSS actor event log (which `cbssd` polls — COW-2338: this is the third reshare trigger alongside the churn and deregister paths). No bond is locked and the committee state is not mutated on-chain; the event signals `cbssd` to orchestrate the off-chain reshare, which lands via a subsequent `RotateCommittee`.

#### ForcedDeregisterCbssProxy (opcode 84)

```
ForcedDeregisterCbssProxy { proxy_id: ProxyId }
```

**Auth:** governance only (`tx.from == GOVERNANCE_SYSTEM_ACTOR = 0x09`). This is the CIP-12 Tier-1 remediation path for persistent faults or disclosed leaks (§4.3, §5.2). **Effects** (`handle_cbss_forced_deregister_proxy`):
1. Charge `CBSS_GAS_DEREGISTER_PROXY = 8_000`.
2. Require `tx.from == GOVERNANCE_SYSTEM_ACTOR`, else `Unauthorized`.
3. Load the proxy (`InvalidData` if absent); set `proxy.suspended = true` and `proxy.unbond_at = Some(current_block)` — **immediate unbond, no delay** (contrast the voluntary `DeregisterCbssProxy` path, which still respects the cooldown). If the proxy sits on any live committee, `RotateCommittee` is enqueued for each affected release key (as in voluntary deregister).
4. Emit `cbss.proxy.forced_deregistered { proxy_id }`.

#### RegisterTlockRelease (opcode 152, provisional)

Registers a time-lock release request (§3.4.6) so the committee knows what identity to sign and when. Permissionless (any account), fee-bearing, rate-limited per block.

```
RegisterTlockRelease {
    scope:          ReleaseKeyRef    // which committee's MPK this tlock binds to (Account or SecretSpecific)
    tag:            Bytes            // caller identity, ≤ MAX_TLOCK_TAG_BYTES (e.g. a CIP-34 request_id)
    target_height:  u64              // reveal height; MUST be > head + MIN_TLOCK_LEAD_BLOCKS
}
```

The chain records `TlockRequest { scope, tag, target_height, registered_at }` keyed by `(scope, tag, target_height)`; a duplicate key is rejected. The record (and any partials posted against it, below) is GC'd at `target_height + TLOCK_RETENTION` (§4). `MIN_TLOCK_LEAD_BLOCKS` ensures the committee has time to observe the request before the reveal height.

#### SubmitTlockRelease (opcode 153, provisional)

A committee proxy posts its partial signature on a registered tlock identity, **only once chain head ≥ `target_height`**. Permissionless to *read* the result; only committee members may submit.

```
SubmitTlockRelease {
    scope:          ReleaseKeyRef
    tag:            Bytes
    target_height:  u64
    proxy_id:       ProxyId
    partial:        [u8; 48]         // σ_i = s_i · I_tlock ∈ G1, canonical compressed
}
```

The chain validates: (a) a `TlockRequest` exists for `(scope, tag, target_height)`; (b) `head >= target_height` (reject `TlockNotYetReleasable` otherwise); (c) `proxy_id` is a current member of the referenced release key's committee; (d) the BLS partial check `e(partial, G2_gen) == e(I_tlock, S_proxy)` with identity/point-at-infinity rejection, where `I_tlock = hash_to_G1(canonical(tag) || u64_le(target_height), "cbss/tlock/v1")`; (e) `(scope, tag, target_height, proxy_id)` not already submitted (per-proxy dedup). Accumulated partials are public; once ≥ `t` are posted, **any** reader Lagrange-combines them into `σ = MSK · I_tlock` (§3.4.6) — no further authorization. No per-proxy receipt/audit record is written (unlike `SubmitReleaseReceipt`): time-lock release is public, so there is no access to audit.

### 3.6 Threshold ceremonies

#### 3.6.1 DKG (initial keypair establishment)

Triggered by `RequestAccountDkg` (account-scoped) or by `SetSecret` with `committee_override` (secret-scoped). Off-chain protocol:

The protocol is the standard threshold BLS DKG (FROST-derived for BLS sigs) as exposed by the chosen vetted library. The devnet implementation uses the pinned Commonware BLS DKG state machine. Sketch:

1. **Round 1.** Each proxy `i` samples a random degree-`t-1` polynomial `f_i(x)` over the BLS12-381 scalar field and broadcasts Feldman/Pedersen commitments to its coefficients (peer-to-peer over the committee mesh; no on-chain anchor).
2. **Round 2.** Each proxy `i` sends `f_i(j)` to each peer `j`, encrypted under `j`'s registry pubkey. Recipients verify the share against the broadcast commitments using a single G2 multi-scalar multiplication.
3. **Aggregation.** Each proxy computes its share `s_i = Σ_j f_j(i)` (a scalar), summing only the qualified dealers when timeout/complaint handling excluded a dealer. The group `MPK = Σ_j C_{j,0}` ∈ G2 (sum of each qualified polynomial's constant-term commitment in G2). Each proxy's published VSS commitment `S_i = s_i · G2` is computable from the aggregated polynomial commitments at index `i`.
4. **On-chain commit.** Once each proxy has its share `s_i` and the group `MPK`, the committee assembles `new_committee_sigs` — t-of-n signatures over the canonical DKG-commit payload (see RotateCommittee). One proxy posts `RotateCommittee(scope, new_committee_epoch = 1, new_mpk, vss_commitments = [S_1..S_n], new_committee_sigs)`. The chain verifies: (a) each `S_i` is a valid G2 point, (b) `new_committee_sigs.len() >= threshold` and all sigs are from members of `new_committee` over the canonical payload, (c) on a reshare, `new_mpk == prior_release_key.mpk`. **The chain does NOT verify Shamir consistency on chain** — that's the committee's responsibility off-chain via VSS during DKG, attested to by the threshold signatures. (A naive "sum(S_i) == MPK" check would be mathematically wrong since S_i are polynomial evaluations, not coefficient commitments.)

If any proxy is detected sending malformed shares (verifiable: VSS commitment mismatch), it can be slashed for `DkgSabotage` (§4.3) by any party submitting the equivocation evidence.

Implementation note: `cbss-crypto` exposes qualified-set finalization so an offline or excluded dealer no longer forces unanimous abort. This does not weaken the ship requirement that the final DKG be vetted and bias-free; the qualified-set hook is ceremony liveness plumbing, not a replacement for the audited DKG protocol.

#### 3.6.2 Reshare (proactive secret sharing)

Triggered by:
- **Scheduled rotation:** every `RESHARE_INTERVAL_BLOCKS` (default ~6 months) for both `AccountReleaseKey` and `SecretReleaseKey` records.
- **Committee churn:** when the count of healthy committee members drops to `t + RESHARE_SAFETY_MARGIN`.
- **Owner request:** the owner of the account (or of the secret in the override case) pays for an immediate reshare.

Protocol: standard proactive secret sharing on the existing `MPK`. Output is a fresh share polynomial of the same secret, distributed to a possibly-different committee of `n` proxies. `MPK` is preserved; `committee_epoch` increments. **Existing SecretVersions retain their `wrap_epoch`** — reshare does not invalidate stored ciphertext or in-flight release requests; PSS guarantees the new shares produce valid partials for any historical identity under the same MPK. The prior epoch's release-key material is retained for a grace window, so CIP-9 volume wraps keep serving across a reshare for that window; a volume's epoch-pinned material hash means it must re-wrap to a current epoch before the window closes (contrast the immediate re-wrap forced by a re-key, §9.3.1).

#### 3.6.3 Threshold partial signature (release)

The runtime release operation, per the threshold-IBE construction in §3.4.3.

Inputs at the proxy:
- The runner's release request (signed; carries `recipient`, `job_id`, `request_block`, `serve_epoch`, and request-binding fields). `serve_epoch` pins which share-polynomial epoch the runner expects partials from; proxies reject with `EpochMismatch` if their current release-key `committee_epoch` differs. The IBE identity still comes from the SecretVersion's immutable `wrap_epoch`, not from `serve_epoch`.
- The proxy's local Shamir share `s_i ∈ scalar field of BLS12-381` of the implicit `MSK`.

Each proxy:

1. Validates the request (§3.7), looking up the relevant `AccountReleaseKey` or `SecretReleaseKey` per the request's `recipient` field and checking `body.serve_epoch == release_key.committee_epoch` at current head.
2. Reads `wrap_epoch` from the SecretVersion on chain and `mpk_g2 = compress(MPK)` from the release key looked up in step 1. Computes the identity using the canonical encoding:
   `I = hash_to_G1( u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2, domain = "cbss/ibe/v1" )`.
3. Computes its partial BLS signature on the identity:
   `σ_i = s_i · I` (a point in G1).
4. Returns the canonical `PartialSignResponse` wire shape:
   ```
   PartialSignResponse {
       sigma_i:                 G1Bytes
       proxy_id:                ProxyId
       committee_epoch_at_serve: u64                   // current release_key.committee_epoch at the time of partial-sign
       served_at_block:         BlockHeight
       proxy_sig:               Signature              // exact byte layout: see "Canonical domain-separated signature payloads" earlier in §3.5; signed under the proxy identity key with the "cbss/partial-sign-response/v1" domain prefix. SAME bytes as the receipt's proxy_sig — proxy can hand the response into the receipt verbatim.
   }
   ```
   The proxy does NOT include its own VSS commitment in the response — the chain looks up `S_i` from the release key's `vss_commitments` (current or retained-prior epoch) keyed by `proxy_id` + `committee_epoch_at_serve`. This prevents a malicious proxy from substituting a different commitment.

No NIZK construction is required — verification (at receipt-submission time) is the standard BLS partial-signature pairing check `e(σ_i, G2_gen) == e(I, S_i)` against the on-chain VSS commitment `S_i ∈ G2`.

The runner rejects identity/point-at-infinity `σ_i` partials before combining. It combines `t` partials via Lagrange interpolation in G1 to obtain the threshold signature `σ = MSK · I ∈ G1`, reads `wrapped_dek.ephemeral_u` (decoding it to `U`), reconstructs `aad = u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u`, then derives the IBE key `K = HKDF(serialize(e(σ, U)), "cbss/ibe/v1" || aad)` and AES-GCM-decrypts `wrapped_dek` with `K`.

### 3.7 Proxy-side validation of release requests

**Release authorization (D11).** The node exposes two coexisting routes with distinct contracts; they are not unified:

| Route | Authority and request identity | Denial |
|---|---|---|
| `GET /cbss/release-authorize` | Serve-time evaluator over one committed state view. The verifying proxy supplies the request's chain, secret/version, actor, runner, recipient, job, serve epoch and optional hire identity; the node derives purpose and checks release eligibility. | HTTP 200 with `allowed:false` and a typed reason |
| `GET /cbss/release-authorize/{job_id}` | Finalized job/session authority for the supplied actor, runner, secret and version. Purpose comes from the stored job or finalized session binding. Ordinary jobs return `authority=legacy`; native sessions return their session authority. | HTTP 403 for an authorization denial |

Every V4 request that needs a new partial goes through `cbssd`'s serve-time check first and, if it allows with a valid context, the finalized-authority check next, including for legacy jobs. Both must allow before a share is used. The daemon matches chain, job, actor, runner, secret, version, derived purpose and finalized height across the responses/request; mismatches fail closed. The serve-time route accepts verifier-supplied identity, not a caller-selected purpose or a substitute for authenticating the signed V4 request. Exact retries can return an already-recorded response after local deletion/readiness checks; they do not use the share again.

When creating a **new partial**, each proxy applies the following validation against chain state at the **current head**. An exact retry may instead return the already-recorded response after the local deletion/readiness checks described above; it does not repeat the new-partial checklist or use the share again.

| Check | Source of truth |
|---|---|
| `request_block ∈ [head - REQUEST_FRESHNESS_BLOCKS, head]` | proxy local clock + chain head |
| Runner registry membership and active status | `0x01` (Runner Registry) at current head |
| Runner currently assigned to `job_id` | `0x02` (Job Dispatcher) at current head |
| Actor's CIP-6 manifest declares the requested `key` under the entitlement matching the release purpose (`secrets.read` for substitution, `secrets.verify` for a `verify=` MAC check; §3.1, §5.5) | actor's deployed manifest at current head |
| `SecretPolicy.actors` permits this actor (or `None` and same-account) | `SecretVersion.policy` at current head |
| If `tee_required`, the node authorizer confirms current TEE eligibility from a measurement binding or the supported signed-attestation path | TEE Verifier records physically stored at `0x04` (§3.2) |
| Anti-replay: a new partial requires a fresh `(release_id, proxy_id)` reservation; an exact retry can return its recorded response after deletion/readiness checks | proxy served-request state and durable receipt outbox; `request_hash` distinguishes an exact retry from a variant of the same logical release |
| Per-actor rate limit: ≤ `MAX_RELEASES_PER_ACTOR_PER_HOUR` | proxy local |
| Proxy is a member of the referenced release key's `committee` at current head | `0x04` |
| `body.serve_epoch == release_key.committee_epoch` at current head (proxy rejects with `EpochMismatch` if reshare landed between request_block and serve time) | `0x04` |
| `SecretVersion.pending == false` | `0x04` |

**Revocation checks.** Each successful V4 authorization requires both node routes in the order above. The daemon also checks local deletion state and reconciles finalized deletion authority; a stale queued request cannot authorize a deleted version.

Failures are reported with structured error codes (`StaleRequestBlock`, `SecretAccessDenied`, `SecretPending`, `SecretCommitteeUnavailable`, etc.); the runner retries with corrected inputs.

### 3.8 Runner pull flow

```
1. Actor task invokes runner.http(..., secrets=["SLACK_API_KEY"]).
2. Runner R looks up the secret-owning account (the actor's owning account by default,
   or the explicit cross-account owner if the entitlement uses {account, key} form).
   The runner-side SDK computes key_hash = keccak256(account || "SLACK_API_KEY") locally;
   plaintext key never leaves the runner sandbox.
3. Runner R syscalls SYS_FETCH_SECRET_METADATA(secret_id = SecretId{account, key_hash},
   version_or_latest) → SecretMetadata + the referenced ReleaseKey snapshot
   (Account or SecretSpecific). The syscall takes SecretId, not plaintext.
4. Runner R picks t of n committee proxies (prefers non-suspended, healthier, low-latency proxies; fall-through to remaining n-t on failure).
5. Runner R generates a fresh release_nonce (CSPRNG, 32 B), reads the release key's
   current committee_epoch as serve_epoch, assembles ReleaseRequestBody
   with request_block = current chain head and serve_epoch = committee_epoch,
   signs it with its runner-registry key
   (NOT an ephemeral key — see §3.4.5 / §3.5 for the registry-key requirement).
   Sends SignedReleaseRequest = { body, runner_sig } to t of n committee proxies in parallel
   (preferring non-suspended, healthier, low-latency proxies; fall-through to remaining n-t on failure).
6. Each proxy validates (§3.7), decrypts its local sealed share with AAD bound to `(release-key scope, committee_epoch)`, verifies the scalar against the VSS commitment polynomial, and returns its BLS partial signature σ_i on the identity
   (PartialSignResponse with sigma_i, committee_epoch_at_serve, served_at_block, proxy_sig).
7. Runner R verifies each σ_i with a single pairing check against the proxy's VSS commitment
   (looked up on chain by proxy_id + committee_epoch_at_serve);
   rejects duplicate proxy indexes, and Lagrange-combines σ_i to produce σ = MSK · I.
8. Runner R reads `wrapped_dek.ephemeral_u` (decoding it to `U`), reconstructs `aad = u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u`, derives K = HKDF(serialize(e(σ, U)), "cbss/ibe/v1" || aad), and AES-GCM-decrypts wrapped_dek → DEK.
9. Runner R fetches CBFS object, AES-GCM-decrypts with DEK + AAD verification → plaintext.
10. Runner R passes plaintext to the in-memory ${SLACK_API_KEY} substitutor at request-construction
    in runner-http / runner-mcp. NO process-wide env var injection on the default path
    (see §3.9).
11. Runner R executes the templated request.
12. Runner R zeroizes plaintext, DEK, σ partials.
13. Each of the t participating proxies independently submits SubmitReleaseReceipt
    (handing the SignedReleaseRequest verbatim into the receipt). Runner does NOT submit
    any audit/payment tx; payment flows directly proxy ← actor account, capped at quorum t.
```

Latency target: < 250 ms p50 for the threshold round (5 proxies, 3 same-continent, parallel) + pairing-verify + AES-GCM-decrypt + CBFS fetch. Bottleneck: the threshold round-trip and CBFS fetch (parallelizable with the threshold round).

### 3.9 Runtime sandbox: in-memory secret bundle (default), env vars only for spawned subprocesses

**Default path: in-memory template substitution, no process env vars.** Process-wide environment is leaky — other threads, libraries, signal handlers, `/proc/$pid/environ` reads, and crash dumps can all observe it; serialization mutexes only help cooperating code. The default release path therefore:

- Holds plaintext in a `Zeroizing<Vec<u8>>` `SecretBundle` keyed by KeyName.
- Performs `${KEY}` substitution **at request-construction time** inside `runner-http` / `runner-mcp` (against headers, URL, body, JSON tool args) — the substitution layer reads the bundle directly without touching the process environment.
- Drops the bundle (zeroizes) at the end of the runner-op.
- Never sets `os.environ[KEY]` for HTTP / MCP / LLM tool calls.

**Subprocess path: explicit env map, no process env mutation.** Some integrations (notably MCP stdio servers spawned by the runner) require a child process that reads its credentials from the environment. For these:

- Build an explicit `env: HashMap<String, Zeroizing<String>>` for the child.
- Pass via `Command::env_clear().envs(env)` — the child inherits ONLY the named entries, not the parent's environment.
- The parent's process environment is never mutated.
- Drop the `Command` builder immediately after spawn and zeroize the source env map.
- Residual exposure: Rust's `std::process::Command` copies env values into its own platform-specific storage before spawning, and the standard library does not guarantee that copy is zeroized. Treat subprocess env delivery as an explicit compatibility exception, not the default secret path.

**PVM Python guardrails:**

- The actor's PVM Python code observes only `${KEY}` placeholders. The PVM does not have a `SecretBundle` reference; substitution happens at the runner layer, after the syscall boundary.
- Direct `os.environ[KEY]` reads from PVM Python are blocked by the sandbox for any key declared in the actor's `secrets.read` entitlement (deny-list installed at sandbox init).
- Logging adapters scrub strings matching active bundle values from spans and trace events on a best-effort basis. The primary defense is preventing PVM Python from ever holding the value.

Process-wide env injection is unsafe by default for the reasons listed above. The bundle-and-substitute pattern keeps plaintext bounded to the request-construction code path.

---

## 4. Parameters

All parameters governance-tunable via CIP-12 Tier 0 proposals.

### 4.1 Gas

Cycle costs below do not determine an access limit. Access rows, including deletion and evidence-history terms, are maintained in node's [`docs/access/CLIENT-LIMITS.md`](https://github.com/cowboyinc/node/blob/55022cf80e71475bf0d0f7c00041a401ba10fea9/docs/access/CLIENT-LIMITS.md). The daemon estimates `RotateCommittee` against committed state before signing each attempt, including retained history and any account force-rekey volume page; it uses a bounded history-aware fallback if estimation is unavailable or unusable. An args-only formula is insufficient.

| Instruction | Default cost | Notes |
|---|---:|---|
| `SetSecret` (default path) | `12_000` | Single wrapped_dek, no DKG. |
| `SetSecret` (override path, intent only) | `8_000 + 30_000 + DKG_BOND` | No ciphertext yet; just enqueues DKG. |
| `FinalizeSecretVersion` | `8_000` | Attaches ciphertext post-DKG; cheap. |
| `UpdateSecretPolicy` | `6_000` | Pure metadata; no re-wrap. |
| `DeleteSecretVersion` | `5_000` | Override key/sweep/outstanding cascade; access cost includes retained history. |
| `DeleteSecret` | `5_000 × num_versions` | Linear in versions; bounded by `MAX_VERSIONS_PER_SECRET`. |
| `RegisterCbssProxy` | `15_000` | One-time. |
| `DeregisterCbssProxy` | `8_000` | Plus reshare costs incurred by `RotateCommittee`. |
| `RotateCommittee` | `25_000 + 1_000 × n` | Cycles for VSS verification; access limit depends on committed history (see above). |
| `SlashCbssProxy` | `12_000 + evidence-verification cost` | Verification varies per `FaultClass`. |
| `SubmitReleaseReceipt` | `8_500` | Two pairing checks (BLS partial-sig verify) + runner sig verify + proxy sig verify per receipt. Submitted independently by each of t proxies. |
| `RequestAccountDkg` | `30_000 + DKG_BOND` | DKG bond returned on success, partially slashed on timeout. |
| `GovRequestSystemDkg` | `30_000` | Same DKG cost as `RequestAccountDkg`, but **no `DKG_BOND`** — governance (`0x09`) has no user account to bond; the fee is paid from the governance actor. |
| `SubmitLivenessChallenge` | `5_000` | Anchors a challenge; deducted from runner. |
| `LivenessChallengeResponse` | `4_000` | Proxy's response. |

### 4.2 Stake / committee

> **Block-time basis (normative):** this chain runs at **1 second per block** (`BLOCKS_PER_YEAR = 31_536_000`, `1 day = 86_400 blocks`, `node/types/src/constants.rs`). Every duration-in-blocks below is computed against 1 s/block.
>
> **⚠️ Deployed-value deltas (2026-07):** a few defaults below are governance-tunable and the **deployed** value differs; the deployed value is authoritative until governance sets otherwise. `PROXY_SOAK_PERIOD` — deployed **100** (`execution/src/cbss.rs`, a devnet/launch-short soak), vs the ~30-day mainnet target. `RESHARE_INTERVAL_BLOCKS` — deployed **1_296_000** (~15 days, a deliberate demo-scale cadence), vs the ~6-month production default. `MIN_PROXY_STAKE` — deployed **1_000** CBY (`CBSS_MIN_PROXY_STAKE`), not 10,000; open decision [cowboy#238](https://github.com/cowboyinc/cowboy/issues/238). `GOVERNANCE_REVIEW_BLOCKS` — corrected to **2_592_000** in code (see cowboy#243). These economic/launch values are governance decisions, not spec-edited here.

| Parameter | Default | Description |
|---|---:|---|
| `MIN_PROXY_STAKE` | `10,000 CBY × 10⁹ wei` | Minimum operator stake. |
| `PROXY_SOAK_PERIOD` | **deployed `100`** (devnet/launch-short); mainnet target ~30 days = `2_592_000` at 1 s/block (see §4.2 note) | Before VRF-eligibility. |
| `UNBOND_COOLDOWN` | `100,800 blocks` (~14 days) | Stake locked during slashing window. |
| `MIN_COMMITTEE_SIZE_FLOOR` | `5` | If eligible-set < 5, new DKGs are queued; existing keys continue to function. |
| `DEFAULT_N` | `5` | Default committee size for account-scoped DKG. |
| `DEFAULT_T` | `4` | Default release threshold for the five-proxy account DKG. This matches the vetted Commonware `N3f1` quorum for `DEFAULT_N = 5`; override path admits up to `MAX_OVERRIDE_N`. |
| `MIN_OVERRIDE_T` | `3` | Floor on per-secret override threshold. |
| `MAX_OVERRIDE_N` | `15` | Ceiling on per-secret override committee size. Bounds DKG cost. Spec-only — not currently a hard on-chain check (no `MAX_OVERRIDE_N` / `MAX_COMMITTEE_N` constant in code; `n` is bounded by DKG / `RotateCommittee` args). The former secrets-WP §13 `MAX_COMMITTEE_N = 16` divergence was resolved when that table was aligned to deployed code (#261). |
| `RESHARE_INTERVAL_BLOCKS` | prod target ~6 months (`15_552_000` at 1 s/block); **deployed `1_296_000`** (~15 days, demo-scale — see §4.2 note) | Scheduled reshare cadence (per release key, account or secret-scoped). Governance-tunable. |
| `RESHARE_SAFETY_MARGIN` | `1` | Trigger reshare when healthy < `t + margin`. |
| `DKG_FINALIZE_BLOCKS` | `7_200` (~2 hours at 1 s/block) | DKG ceremony deadline. |
| `LIVENESS_RESPONSE_BLOCKS` | `64` (~64 s) | Window for a proxy to respond to a `SubmitLivenessChallenge` before the challenge transitions to `Unanswered`. Note: NO automatic slash follows — it's a health-score signal only. |
| `REQUEST_FRESHNESS_BLOCKS` | `384` (~6.4 min at 1 s/block) | Maximum age of `request_block` accepted by proxies and chain receipts. Bounds revocation propagation latency. |
| `CBSS_EVIDENCE_WINDOW_BLOCKS` | `512` | Consensus code constant: evidence-admission window and retained-epoch cutoff (§3.5). |
| `CBSS_MAX_EVIDENCE_EPOCHS` | `64` | Consensus code constant: maximum retained evidence epochs per recipient. After expired epochs are pruned, exceeding the cap rejects rotation; unexpired evidence is not discarded to fit. |
| `RESHARE_GRACE_BLOCKS` | `100` (~20 min) | Window after a `RotateCommittee` during which receipts under that prior epoch are still accepted (paired against retained prior commitments). After expiry, the entry is GC'd and stale-epoch receipts are rejected. |
| `MAX_PRIOR_COMMITTEE_EPOCHS` | `4` | Maximum simultaneously-retained prior committee epochs per release key. Allows up to 4 reshares within one `RESHARE_GRACE_BLOCKS` window without losing prior-epoch receipts. Exceeding the cap causes `RotateCommittee` to fail with `ReshareGraceCapacity` until at least one prior epoch's grace window expires. |
| `RELEASE_FEE_PER_RECEIPT` | `governance-tunable` | Fee debited from actor account on each `SubmitReleaseReceipt`. Goes to the submitting proxy. |
| `MAX_RELEASE_OVERDRAFT` | `16 × RELEASE_FEE_PER_RECEIPT` | Maximum negative release-payment balance an actor may incur from receipts before further receipts are rejected with `ActorAccountInsufficient`. Below this cap, the proxy is paid even on insufficient balance and the actor is marked `delinquent`. Implementations may represent this with negative account balance or a separate release-debt ledger. |
| `LIVENESS_CHALLENGE_BOND` | `governance-tunable` (lean: 10× standard tx fee) | Bond locked from the runner at `SubmitLivenessChallenge`. On `Resolved`, forfeited to the challenged proxy. Refunded to the runner on `Unverifiable` and `Unanswered`. |
| `MIN_CHALLENGE_DELAY_BLOCKS` | `REQUEST_FRESHNESS_BLOCKS + 32` = `416` (~7 min) | Minimum elapsed blocks between a `body.request_block` and a `SubmitLivenessChallenge` referencing it. MUST be greater than `REQUEST_FRESHNESS_BLOCKS`, so valid receipts are either already on chain or impossible to submit by the time a challenge is admissible. Premature challenges rejected with `ChallengePremature`. |
| `LIVENESS_CHALLENGE_MAX_AGE_BLOCKS` | `MIN_CHALLENGE_DELAY_BLOCKS + LIVENESS_RESPONSE_BLOCKS` = `480` (~8 min) | Maximum elapsed blocks between `body.request_block` and challenge submission. Must be meaningfully greater than `MIN_CHALLENGE_DELAY_BLOCKS`; older requests can no longer be challenged. |
| `DKG_BOND` | `governance-tunable` | Refundable bond locked at `RequestAccountDkg` / override-`SetSecret` time. Refunded on successful DKG commit; reduced by `DKG_TIMEOUT_SLASH` on `ExpireDkgPending`. |
| `DKG_TIMEOUT_SLASH` | `5%` of `DKG_BOND` | Portion of the DKG bond forfeited to the slashing pool when `ExpireDkgPending` settles an expired `DkgPending` record. Caller of `ExpireDkgPending` receives a small fixed cleanup reward from this pool. |
| `MIN_HEALTH_FLOOR` | `0.05` | Floor on the per-proxy health-score multiplier applied to VRF weight. Prevents complete exclusion that would block reshare unanimity. |
| `GOVERNANCE_REVIEW_BLOCKS` | `2,592,000` (~30 days at 1 s/block) | Window of sustained `health_score < 0.1` required to flag a proxy for an automatic CIP-12 Tier 1 deregistration proposal. |
| `MAX_LEAK_REIMBURSEMENT` | `100,000 CBY × 10⁹ wei` | Cap on the remediation-pool payout per disclosed plaintext-leak incident (governance-mediated; see §4.3). |
| `MAX_IBE_AAD_BYTES` | `4096` | Hard cap on the AEAD AAD length accepted by the IBE kernel for any CBSS envelope (§3.4.3). CIP-24 secret wraps use exactly 268 bytes (172-byte `base_aad` + 96-byte `compress(U)`) and CIP-9 volume wraps exactly 128 (§9.3); the cap bounds the shared-kernel surface (the retired CIP-7 committee format had variable-length AADs). |
| `MAX_IBE_CIPHERTEXT_BYTES` | `1 MiB` (`1,048,576`) | Hard cap on IBE-envelope ciphertext length (§3.4.3). |

### 4.3 Slashing schedule

Provable on-chain only. `PlaintextLeak` and liveness non-receipt are NOT auto-slash:

- **PlaintextLeak**: evidence requires publishing the secret. Handled via off-chain disclosure → CIP-12 Tier 1 `ForcedDeregisterCbssProxy`.
- **Liveness non-receipt**: not provable on chain (packet delivery is not chain-witnessable). Handled via the §4.4 health score → governance review.

| Fault | Penalty | Detection |
|---|---|---|
| `InvalidPartialReencrypt` | 5% | Authenticated invalid/malformed blinded partial, or an authenticated serve stamp outside the request freshness range; §3.5 admission rules apply. |
| `DkgSabotage` | 5% | VSS commitment / share mismatch in DKG round 2 |
| `Equivocation` | 50% | Two distinct signed responses to the same `request_hash` |

PlaintextLeak handling: a disclosed leak triggers a CIP-12 Tier 1 proposal that, if passed, calls `ForcedDeregisterCbssProxy(proxy_id)` and routes the entire stake to a remediation pool. The remediation-pool payout itself is governance-mediated (lean: capped at `MAX_LEAK_REIMBURSEMENT = 100,000 CBY` per incident).

Liveness handling: see §4.4 health score. No automatic stake loss on liveness; persistent degradation drives governance review.

### 4.4 Health score

Each proxy carries a health score in `[0.0, 1.0]`, decayed and updated based on observable behavior. Used as a multiplier on VRF selection weight; persistent low scores escalate to governance review.

| Signal | Effect |
|---|---|
| `SubmitReleaseReceipt` (positive completion) | small positive |
| `SubmitLivenessChallenge → Resolved` via admission-time receipt check (chain-verifiable; proved liveness via receipt) | small positive on the proxy; bond forfeited to the challenged proxy. |
| `SubmitLivenessChallenge → Unverifiable` via `LivenessChallengeResponse` with `InvalidRequest`, `RateLimited`, or `NeverReceived` (chain-unverifiable) | **NEUTRAL on proxy** — no automatic decrement. Bond refunded to runner. Pattern detection in off-chain governance. |
| `SubmitLivenessChallenge → Unanswered` (proxy submits no response by `deadline`) | negative on proxy; bond refunded to runner. |
| Slashing event (any FaultClass) | large negative |
| Sustained healthy uptime (per epoch) | small positive |

Concrete coefficients are governance parameters (CIP-12). The protocol enforces only that VRF weight = `stake × max(MIN_HEALTH_FLOOR, health_score)`, where `MIN_HEALTH_FLOOR` (default 0.05) prevents complete exclusion that would block reshare unanimity.

Persistent extreme degradation (health < 0.1 for ≥ `GOVERNANCE_REVIEW_BLOCKS` ≈ 30 days) flags the proxy for an automatic CIP-12 Tier 1 deregistration proposal. Governance vote follows.

### 4.5 Quotas / limits

Carrying forward from the design notes §5.4 with no changes:

| Parameter | Default | Description |
|---|---:|---|
| `MAX_SECRETS_PER_ACCOUNT` | 256 | Total distinct keys per owner. |
| `MAX_VERSIONS_PER_SECRET` | 32 | Active versions retained. |
| `MAX_SECRET_VALUE_BYTES` | 65,536 | Per-version plaintext cap. |
| `MAX_ACL_ACTORS` | 64 | Distinct actor addresses per ACL. |
| `MAX_SECRETS_PER_RUNNER_OP` | 8 | `secrets=[...]` length. |
| `MAX_RELEASES_PER_ACTOR_PER_HOUR` | 1,000 | Per-proxy-enforced rate limit. |
| `MAX_TLOCK_TAG_BYTES` | 64 | Time-lock `tag` length (§3.4.6); a CIP-34 `request_id` is 32 B. |
| `MIN_TLOCK_LEAD_BLOCKS` | 16 | Min lead between `RegisterTlockRelease` and `target_height` (committee observe window). |
| `MAX_TLOCK_REGISTRATIONS_PER_BLOCK` | 256 | Per-block cap on new tlock registrations (anti-grief / bounded committee work). |
| `TLOCK_RETENTION` | 256 blocks | A `TlockRequest` + its posted partials are GC'd at `target_height + TLOCK_RETENTION`. |

---

## 5. SDK surface (CIP-6 extension)

### 5.1 New entitlement

```json
{ "id": "secrets.read",
  "params": {
    "keys": [
      "SLACK_API_KEY",
      "OPENAI_API_KEY",
      { "account": "0xacct1...", "key": "SHARED_API_KEY" }
    ]
  }
}
```

Subset upgrade rule (CIP-6 §12.1) applies: an actor upgrade may remove keys from this list but never add them. Cross-account form `{"account", "key"}` is the canonical wire shape.

`secrets.verify` (§5.5) grants **verify-only** use of a key as an inbound-MAC key. Same `keys` shape and subset-upgrade rule. A key listed under `secrets.verify` is releasable for a `verify=` MAC check but is **not** readable via `secrets=[...]` (no `${VAR}` substitution) unless also listed under `secrets.read` — this is what stops an actor from exfiltrating a signing secret it can only verify with. A key MAY hold both grants; within a single job the §5.5 overlap rule still forbids the same key serving both roles.

```json
{ "id": "secrets.verify",
  "params": { "keys": [ "SLACK_SIGNING_SECRET" ] } }
```

The read and verify grants are separate entitlements (CIP-6 §14.3):

| ID | Gates | Params |
|---|---|---|
| `secrets.read` | Release for secret substitution | `keys` |
| `secrets.verify` | Release for an inbound-MAC check | `keys` |

### 5.2 Runner-op kwarg

```python
await runner.http(
    "https://hooks.slack.com/services/...",
    headers={"Authorization": "Bearer ${SLACK_API_KEY}"},
    secrets=["SLACK_API_KEY"],
)

await runner.mcp(
    server="github",
    tool="create_issue",
    args={"token": "${GITHUB_TOKEN}", "title": "..."},
    secrets=["GITHUB_TOKEN"],
)

# Pin a specific version:
await runner.http(..., secrets=[("SLACK_API_KEY", {"version": 3})])

# Cross-account:
await runner.http(..., secrets=[("SHARED_API_KEY", {"account": "0xacct1..."})])
```

`runner.llm` accepts `secrets=[...]` only when the LLM op orchestrates server-side tool calls that need them (e.g., MCP tool plumb-through). Plain text-completion calls reject `secrets=[...]` with `SecretsNotApplicableHere`.

### 5.3 Errors

Canonical error names — implementations MUST use these to avoid divergent vocabularies. SDK / runner / proxy / chain handlers all map their failure modes to this set.

```
// Lookup / state failures
SecretNotFound                 // no SecretMetadata for (account, key_hash)
SecretVersionDeleted           // version was deleted by owner
SecretPending                  // override-path version awaiting FinalizeSecretVersion
SecretAccessDenied             // ACL or manifest entitlement does not permit (actor, runner) — including a
                               // purpose mismatch (§3.1, §5.5): a verify= key the actor does not hold under
                               // secrets.verify, or a secrets=[...] reference to a key held only under secrets.verify
SecretRequiresTEE              // policy.tee_required = true and runner has no valid CIP-23 attestation
SecretQuotaExceeded            // hit MAX_SECRETS_PER_ACCOUNT / MAX_VERSIONS_PER_SECRET / MAX_ACL_ACTORS / etc.
SecretCommitteeUnavailable     // < t proxies responsive at fetch time
SecretDkgInProgress            // initial ceremony in flight (account or secret-scoped override)
SecretsNotApplicableHere       // runner.llm with secrets=[...] but no server-side tool plumbing

// Setup-time failures
MissingAccountReleaseKey       // SetSecret-time failure on default path with no AccountReleaseKey on file
WrapEpochMismatch              // SetSecret/FinalizeSecretVersion: claimed wrap_epoch ≠ release_key.committee_epoch at submission
AadMismatch                    // wrapped_dek full-AAD mismatch (incl. wrong chain_id or substituted mpk_g2) or malformed len != 172 + 96 (268); expected u64_be(chain_id) || canonical(SecretId) || u64_le(version) || u64_le(wrap_epoch) || mpk_g2 || ephemeral_u, chain_id + mpk_g2 re-derived from committed state
UnsupportedWrappedDekVersion   // wrapped_dek.version != 2 — canonical typed error for unsupported envelope tags; implementations map local decode/validation failures to this taxonomy; §3.1
RecipientMismatch              // runner_request.body.recipient ≠ SecretVersion.recipient (anti-committee-substitution)

// Freshness / ordering
StaleRequestBlock              // body.request_block outside REQUEST_FRESHNESS_BLOCKS window
StaleChallenge                 // SubmitLivenessChallenge submitted after LIVENESS_CHALLENGE_MAX_AGE_BLOCKS from body.request_block
StaleCommitteeEpoch            // committee_epoch_at_serve neither current nor in retained prior_committee_epochs
EpochMismatch                  // proxy's current release_key.committee_epoch differs from body.serve_epoch (runner pinned a different epoch than the proxy's current view); runner must re-issue with fresh release_nonce + updated serve_epoch
ReshareGraceCapacity           // RotateCommittee would exceed MAX_PRIOR_COMMITTEE_EPOCHS; wait for grace window to expire

// Signature / proof failures
InvalidRunnerSignature         // runner_request.runner_sig fails verification against runner registry
InvalidProxySignature          // SubmitReleaseReceipt.proxy_sig or PartialSignResponse.proxy_sig fails verification
InvalidPartialReencrypt        // authenticated invalid/malformed blinded partial, or authenticated served_at_block outside [request_block, request_block + 384], even with a valid pairing; admission and authentication rules in §3.5 / §4.3

// Receipt / dedup failures
ReceiptAlreadyRecorded         // (release_id, proxy_id) already exists in release_receipt
ReceiptOrderingInvalid         // served_at_block < request_block, or > submission_block
QuorumAlreadyReached           // release_receipt:{release_id} already has t entries; additional receipts rejected (caps payment at quorum)
MixedEpochReceipts             // committee_epoch_at_serve ≠ body.serve_epoch / quorum_epoch initialized from the runner-pinned serve_epoch; reject (Lagrange combine requires single-polynomial partials)
ActorAccountInsufficient       // SubmitReleaseReceipt would push actor release debt past −MAX_RELEASE_OVERDRAFT; receipt rejected, proxy not paid for this attempt
LivenessChallengeAlreadyOpen   // (release_id, proxy_id) already has an open challenge (per-release dedup)
ChallengePremature             // SubmitLivenessChallenge submitted before MIN_CHALLENGE_DELAY_BLOCKS elapsed since body.request_block

// Time-lock release failures (§3.4.6)
TlockTagTooLong                // RegisterTlockRelease tag exceeds MAX_TLOCK_TAG_BYTES
TlockLeadTooShort              // RegisterTlockRelease target_height not > head + MIN_TLOCK_LEAD_BLOCKS
TlockAlreadyRegistered         // a TlockRequest for (scope, tag, target_height) already exists
TlockRegistrationCapExceeded   // MAX_TLOCK_REGISTRATIONS_PER_BLOCK reached for this block
TlockNotRegistered             // SubmitTlockRelease for an unregistered (scope, tag, target_height)
TlockNotYetReleasable          // SubmitTlockRelease while head < target_height (premature reveal)
TlockPartialAlreadySubmitted   // (scope, tag, target_height, proxy_id) already submitted (per-proxy dedup); the BLS pairing failure reuses InvalidPartialReencrypt

// Decryption failures (runner-side / SDK)
CiphertextCorrupted            // AES-GCM auth tag mismatch on wrapped_dek or CBFS object

// Inbound verification (§5.5 verify= / JobPrecondition)
JobPreconditionFailed             // verify= MAC mismatch on the runner; fail-closed and terminal (§5.5) —
                                  // no executor work, no read-set release, no retry/reassignment
JobPreconditionUnsupportedAlgo    // verify.algo not in the closed enum; admission-time reject
                                  // (node admission maps its `unsupported_algo` validation code here)
JobPreconditionMessageTooLarge    // verify.message exceeds the 64 KiB cap; admission-time reject before
                                  // any CBSS release (node: `message_too_large`)
```

### 5.4 CLI

```
cowboy secrets set <key> <value> [--actors=A,B] [--account=...] [--tee-required] [--committee-override] [--threshold=T/N]
cowboy secrets list [--account=...]
cowboy secrets versions <key>
cowboy secrets policy <key> [--add-actor=...] [--remove-actor=...] [--tee-required=true|false]
cowboy secrets delete <key> [--version=N]
cowboy proxy register --stake 10000CBY  # operator-side; publishes network_addr + HPKE public key
cowboy proxy status
cowboy proxy reshare-status
```

### 5.5 Inbound-verification precondition (`verify=`)

Some runner-ops receive an inbound, externally-signed request (e.g., a Slack slash-command callback) and need to check the caller's signature before proceeding — without pulling the underlying secret's plaintext into the actor's own code, only into the runner sandbox that already holds it for the op. CIP-24 adds an optional **inbound-verification precondition** to a job, exposed to callers as a `verify=` kwarg alongside `secrets=` (§5.2):

```python
await runner.http(
    "https://internal.example.com/slack/handler",
    ...,
    verify=VerifyInput(
        secret=("SLACK_SIGNING_SECRET", {"account": "0xacct1..."}),
        message=raw_request_body,
        signature=x_slack_signature_header,
        # Provider-NEUTRAL crypto: Slack = HMAC-SHA256, hex, "v0=" prefix.
        # "Slack" lives in the actor, never on the wire or in consensus.
        algo=VerifyAlgo.Hmac(hash=Sha256, encoding=Hex, prefix=b"v0="),
    ),
)
```

`verify=` normalizes to an optional `JobPrecondition` on the job spec:

```
JobPrecondition {
    secret_ref:  SecretKeyRef   // §3.1 — {account, key_hash}; not a plaintext key name
    message:     Bytes          // exact bytes the signature was computed over
    signature:   String         // algo-specific encoding
    algo:        VerifyAlgo     // closed enum
}
```

`VerifyAlgo` carries **provider-neutral crypto primitives only** — never a provider name. This is the two-layer split: the provider-specific parts (the `message` preimage construction and the `prefix` choice) live in the actor / the connector's integration definition at the control plane; the wire and consensus carry only neutral crypto. The single scheme today is `Hmac`:

```
VerifyAlgo = Hmac { hash: MacHash, encoding: SigEncoding, prefix: Option<Bytes> }
MacHash     = Sha1 | Sha256          // closed enum, tags 0/1
SigEncoding = Base64 | Hex           // closed enum, tags 0/1
```

The runner computes `[prefix ‖] encoding(HMAC-<hash>(secret, message))` and compares it **constant-time** to `signature`. `prefix` is a small scheme-version tag (≤ 16 bytes), `None` for a bare encoded MAC. Provider profiles are just field values:

| Provider | `hash` | `encoding` | `prefix` |
|---|---|---|---|
| Twilio (`X-Twilio-Signature`) | `Sha1` | `Base64` | none |
| Slack (`v0=`) | `Sha256` | `Hex` | `"v0="` |
| GitHub (`sha256=`), Stripe, generic HMAC | `Sha256` | `Hex` | scheme prefix / none |

Adding an HMAC-based webhook provider requires **no protocol change** — it is a new set of field values chosen by the actor. Implementations MUST reject any unknown `MacHash`/`SigEncoding`/`VerifyAlgo` tag (fail-closed, same discipline as `HttpMethod`) and an over-cap `prefix`. A genuinely new crypto scheme (e.g. ed25519 / RSA webhook signatures) — not a new provider — would add a new `VerifyAlgo` variant (tag 1+) via a CIP-24 amendment.

**Admission checks.** Both of the following are pure (no chain/registry state) and MUST be enforced in the same admission gate as the job spec's other structural checks, before any secret lookup or release is attempted:

1. **Message length cap.** `len(precondition.message)` MUST NOT exceed **64 KiB** (65536 bytes). A `verify=` request whose `message` exceeds the cap is rejected at admission, before verification is attempted.
2. **Secret-ref-overlap rule (CIP-24 #37).** `precondition.secret_ref` MUST NOT overlap — i.e., MUST NOT be the same `{account, key_hash}` pair — with any entry in the job's `secret_refs` (the `secrets=` read set, §5.1/§5.2). A single job may verify a signature keyed by a given secret, or read that secret's plaintext into the sandbox, but not both for the same `{account, key_hash}` in the same job.

**Motivation.** An actor that ingests an authenticated inbound webhook — e.g. a Slack Events request, HMAC-signed with a shared signing secret — cannot verify the signature in its on-chain handler: validators cannot threshold-decrypt CBSS secrets (§3.9), and storing the signing secret in plaintext actor state both leaks it (validator- and indexer-readable) and defeats the threshold model. Verification must therefore happen on a runner — the only tier that can release the secret — but without an extra round-trip and without trusting the gateway/ingress. `verify=` folds verification into the front of the runner op the actor was already going to dispatch.

**Why a distinct `secrets.verify` entitlement (not `secrets.read`).** The signing secret is not an outbound credential. If it were grantable via `secrets.read`, an actor could list it in `secrets=[...]` and exfiltrate it by reflecting `${VAR}` into a request header or body. `secrets.verify` grants only the right to use a key as a `verify=` MAC key (§5.1): the runner refuses to place a verify-scoped key into the substitution bundle, and the release authorizer denies any reference whose purpose does not match the key's grant (§3.1, `SecretAccessDenied`). A key MAY hold both grants; the overlap rule above still forbids both roles for the same key within one job.

**Fail-closed ordering (normative).** Within a runner op carrying `verify=`, the runner MUST, in order: (1) release `verify.secret_ref` under the `secrets.verify` purpose; (2) compute the MAC over `message` and compare it **constant-time** to `signature`; (3) on mismatch, abort with `JobPreconditionFailed` and invoke no executor — no HTTP request, no agent loop, no MCP call, no outbound side effect; (4) only on match, release the `secrets=[...]` read set, substitute, and execute. The runner MAY prefetch read-set secret *metadata* and plan committee fanout before the MAC check, but MUST NOT release or substitute any read-set **plaintext** until the MAC passes — a forged webhook never triggers a read-set release. The verify plaintext is confined to a verify-only ephemeral: it MUST be zeroized before the executor runs, and it never enters the `${VAR}` SecretBundle (§3.9). `JobPreconditionFailed` is **terminal**: the assignment MUST NOT be retried or reassigned on MAC failure.

**Message canonicalization.** `message` is opaque **bytes** on the wire (base64 in the SDK JSON surface; the runner decodes to exact bytes and never parses or re-serializes them). The runner is **preimage-agnostic** — it MACs the bytes it is handed — so the actor MUST construct `message` as the provider's exact canonical preimage over the **verbatim** received body. For the Slack profile (`Hmac{Sha256, Hex, "v0="}`), the actor constructs `message` as exactly `b"v0:" + ascii(timestamp) + b":" + raw_body` and the runner accepts iff `signature == "v0=" + lowerhex(HMAC_SHA256(secret, message))`. For the Twilio profile (`Hmac{Sha1, Base64, none}`), `message` is the URL followed by each POST param name+value in sorted order, and the runner accepts iff `signature == base64(HMAC_SHA1(secret, message))`. Any JSON re-encode, unicode round-trip, or whitespace normalization changes the bytes and MUST fail; a conformance test SHOULD sign a body with duplicate keys, trailing whitespace, and non-ASCII to ensure a reparsing implementation cannot pass by accident. New HMAC-based providers (GitHub `sha256=`, Stripe, …) are new `Hmac` field values chosen by the actor, never new protocol variants.

**Threat model / DoS bound (normative, honest).** The precondition authenticates the *payload*, not the *dispatch*: the actor still chooses to dispatch on an unverified inbound request, so a forged webhook costs one chain dispatch plus at most one verify-release attempt per assigned runner before the MAC fails and the op aborts terminally — and nothing more: no read-set release, no executor work, no outbound side effect, no plaintext leak. Because adaptive committee sizing MAY assign M runners to an op, verify-precondition ops SHOULD execute **single-runner** (redundancy buys nothing for a fail-closed gate), and `JobPreconditionFailed` being terminal prevents reassignment from multiplying attempts. Actors SHOULD further bound the residual with on-chain, secret-free prefilters applied *before* dispatch: a message-size cap, a timestamp-freshness window, exact-replay suppression keyed on `(ts, signature, body_hash)` — the only fields fixed before the MAC; it drops byte-identical resends but is **not** forgery prevention — and, the real ceiling, actor-level per-interval rate and spend caps. Per-channel / per-user limits are best-effort only: the attacker controls the body before the MAC passes, so sender identity cannot be authenticated pre-verification. The guarantee `verify=` provides is that no forged request ever produces an authenticated side effect or a read-set release; the cost it does not eliminate is the bounded dispatch + verify-release attempt(s), which the spend cap ceilings.

---

## 6. Implementation notes

### 6.1 Crate layout

| Crate | Status | Purpose |
|---|---|---|
| `runner/crates/secrets-manager` | implemented for devnet | Hosts the runner-side `SecretsClient`. Owner-side store, update, and delete operations belong to the CLI and chain client. Current implementation has the threshold combiner/decrypt helper, a trait-based `ThresholdSecretsClient` core with bounded concurrent fanout/fallback and per-proxy timeouts across committee members, `HttpChainSecretMetadataResolver` for the node `GET /cbss/secret/{account}/{key_hash}?version=` metadata endpoint, `Secp256k1ReleaseRequestSigner` for chain-compatible runner request signatures, and encrypted ciphertext fetchers for the CBSS/CBFS blob path. `ProxyEndpoint` carries TLS SPKI pins plus chain health/suspension metadata, snapshots without pins fail closed, and suspended proxies are skipped while healthier active proxies are preferred before fanout. `cbssd` supplies the QUIC `PartialSign` proxy adapter with endpoint-derived SPKI-pinned client config, and `runner-node` can opt into this threshold path with `CBSS_THRESHOLD_ENABLED`. Local latency EWMA, adaptive retry tuning, and broader receipt-observation ergonomics remain operator-hardening work. |
| `runner/crates/cbss-crypto` | implemented for devnet | Thin wrapper over vetted BLS / IBE libraries and the Commonware BLS DKG integration target (`blstrs`, Commonware, `tlock`, `aes-gcm`, `hkdf`). Identity construction, ciphertext envelope, validation, HPKE recipient-specific DKG/reshare payloads, and share storage are in place. Proactive secret sharing and the Commonware adapter are included in the external crypto review scope. |
| `runner/crates/cbssd` | implemented for devnet | Operator daemon. Hosts Commonware-backed DKG ceremony orchestration, reshare orchestration, threshold-partial-sig service over QUIC+bincode, share storage at rest, audit log, slashing tx submission, and cbssd-native chain broadcasting. Current implementation has the transport-independent `PartialSignService` core, frame dispatcher, generic Tokio stream I/O helpers, `quinn` server/client adapters, `cbssd serve` health binding, persisted daemon TLS and secp256k1 operator identities, chain-compatible proxy-response signing, SPKI-pinned client verification, chain proxy TLS-SPKI metadata, endpoint-derived SPKI-pinned proxy-client construction, a runner-side QUIC `PartialSign` proxy adapter, a receipt submitter, and a current-head HTTP chain release authorizer that verifies runner signatures, freshness, ACL policy, job/runner assignment, actor manifest entitlements, TEE-required fail-closed state, and local committee membership before signing. |
| `node/execution/src/cbss.rs` | implemented for devnet | On-chain handlers for §3.5 instructions: secret registry, proxy registration/deregistration/slashing, release receipts, DKG/reshare commit, liveness challenge/response/expiry, TEE-attestation reads, and gas/receipt validation. |
| `node/rpc/src/handlers/cbss.rs` | implemented for devnet | CBSS RPC surfaces for proxy metadata, account/secret release keys, secret metadata resolution, and TEE-attestation lookup. |
| `node/cli/src/commands.rs` | implemented for devnet | `cowboy secrets *`, `cowboy proxy *`, and TEE-attestation submission surfaces. |
| `cbfs` `/v1/cbss/blob` route | implemented for devnet | HTTP route used by the owner upload / runner ciphertext-fetch path for encrypted CBSS blobs. |

Companion specifications: [CIP-2](cip-2-offchain-compute.md) describes off-chain
compute and system actors; [CIP-6 §14.3](cip-6-sdk.md#143-entitlement-registry-informative-summary)
lists the SDK entitlements; the [Technical Whitepaper §9](../whitepaper/cowboy-technical-whitepaper.md)
defines system-actor addresses. CBSS opcode assignments are listed in §3.3.

### 6.2 Runner-side `SecretsClient` trait

```rust
#[async_trait]
pub trait SecretsClient: Send + Sync {
    async fn fetch_for_op(
        &self,
        ctx: &JobContext,
        actor: ActorAddress,
        keys: &[SecretRef],            // (account, key, optional version)
    ) -> Result<SecretBundle, SecretsError>;
}

pub struct SecretBundle {
    entries: Vec<(SecretRef, Zeroizing<Vec<u8>>)>,
}

impl Drop for SecretBundle {
    fn drop(&mut self) { /* explicit zeroize per entry */ }
}
```

Implementation: chain client metadata fetch → t parallel `PartialSign` RPCs over QUIC+bincode → BLS partial-signature pairing-verify → Lagrange combine → IBE decrypt of `wrapped_dek` (`K = HKDF(serialize(e(σ, U)), "cbss/ibe/v1" || aad)`) → fetch CBFS object and AES-GCM-decrypt with DEK → return `Zeroizing<Vec<u8>>` per key. The bundle drops at end of runner-op. **No env-var injection on the default path** — `runner-http` / `runner-mcp` perform `${KEY}` substitution from the bundle directly at request-construction time (see §3.9). Only spawned subprocesses receive env vars, via an explicit env map (`Command::env_clear().envs(...)`).

### 6.3 No persistent owner-side DEK cache

The per-account anchoring + IBE construction means owners do not need a local DEK cache. ACL changes (`UpdateSecretPolicy`) are pure metadata and require no re-wrapping. The `cowboy secrets set` CLI holds a DEK in process memory only for the duration of one set/finalize operation, and zeroizes on exit. There is no on-disk cache to manage, prune, or seal.

### 6.4 RPC additions

In `rpc/`:

```
secrets_getMetadata(secret_id: SecretId, version_or_latest) → SecretMetadata
                  // SecretId = { account, key_hash }; raw RPC takes key_hash, not plaintext.
                  // SDK / CLI wrappers convert plaintext key_name → key_hash before calling.
secrets_getAccountReleaseKey(account) → AccountReleaseKey
secrets_getSecretReleaseKey(secret_id, version) → Option<SecretReleaseKey>   // Some iff override path
secrets_listProxies(filter) → Vec<CbssProxy>
secrets_getReleaseHistory(secret_id: SecretId, range) → Vec<SecretReleased>
proxy_health(proxy_id) → ProxyHealth
proxy_committees(proxy_id) → Vec<ActorAddress>     // which actors this proxy serves
```

---

## 7. Implementation tracking

Implementation deliverables and test scenarios are tracked in `cbssd-implementation-plan.md`: chain handlers, cryptography, DKG/reshare, release paths, SDK, CLI, and documentation. External crypto review is required before launch.

The CIP-9 volume-DEK integration (§9.3) includes the `cbssd` volume seal service, release-key material-hash binding, re-wrap-after-re-key flow, share finality, and the node's hold-until-key-delivery gate. It uses the `WrappedDek` format version 2 specified here. Governance enactment provides the system-scope DKG bootstrap (§3.5).

Implementation review uses `cbss-coverage-manifest.tsv` as the B12
coverage index. That manifest maps the release-path, SDK/CLI/liveness, e2e,
stress, and adversarial scenarios from the implementation plan to concrete
source/test anchors, and `cbss-review.sh` validates those anchors on each run.
Remaining implementation and validation work is tracked outside this CIP text: full Intel
DCAP/TDX and AMD SEV-SNP/VLEK vendor-collateral validation, restoration of the
full spawned §5.9 DKG proof matrix in the current cargo harness, high-rate
stress against a spawned validator, a fake-cbssd Byzantine binary over QUIC,
and broader real-validator negative/recovery scenario coverage. External crypto
review remains a launch requirement.
The reviewer packet is `cowboy/docs/security/cbss-crypto-external-review-packet.md`.

---

## 8. Security considerations

### 8.1 The t-collusion question

For the default `(n, t) = (5, 4)`, an attacker needs to compromise 4 of 5 specific operators simultaneously. With per-account anchoring (§10.2), a successful t-collusion exposes **all of the account's default-path secrets** for the duration of the compromise window, retroactively decrypting any wrapped DEKs the colluding proxies could observe at release time. Hardening levers:

- **Operator diversity** (governance): jurisdictional, software-version, hardware-source, organizational. Governance MAY refuse to admit registrations that concentrate the proxy set.
- **Governance teeth on disclosed leaks:** PlaintextLeak is NOT auto-slashable on chain (§4.3) because objective on-chain evidence would publish the secret. A disclosed leak triggers a CIP-12 Tier 1 governance proposal that, if passed, calls `ForcedDeregisterCbssProxy(proxy_id)` and routes the offending proxy's stake to a remediation pool (default cap `MAX_LEAK_REIMBURSEMENT`). With `MIN_PROXY_STAKE = 10,000 CBY` and default `t = 4`, t-collusion still risks at least `t × MIN_PROXY_STAKE` plus permanent ban — but the slash is governance-mediated, not protocol-automatic.
- **VRF committee selection:** attacker cannot pre-position; committee membership is determined per-account at DKG time.
- **Proactive reshare (insider rotation, NOT cryptographic invalidation):** every `RESHARE_INTERVAL_BLOCKS`, the share polynomial rotates. PSS preserves `MSK`, so a retained old share can still partial-sign any historical-`wrap_epoch` identity off-chain — there is no cryptographic invalidation of old share material. What reshare DOES do: (a) replaces the share at-rest in cbssd's storage (an honest proxy zeroizes per `DeleteSecretVersion`/reshare protocol), (b) rotates committee membership so previously-compromised proxies must be re-compromised under the new committee_epoch to keep contributing to t-of-n, (c) makes the prior committee no longer accepted by the chain for receipts past `RESHARE_GRACE_BLOCKS`. The honest-share-zeroization assumption is operational, not cryptographic. An exfiltrated share retains its contribution to offline threshold operations over historical identities. `DeleteSecret` removes the on-chain version rows and pointer and revokes subsequent chain-authorized release; it does not cryptographically invalidate copied shares, wrapped DEKs or ciphertext.
- **Per-secret committee override:** for secrets where the per-account blast radius is unacceptable, the `committee_override` path triggers a one-off DKG with its own committee, isolating that secret's compromise surface from the rest of the account. Owners SHOULD use override for `STRIPE_LIVE_KEY`-class secrets and let routine credentials ride the account default.

### 8.2 Owner key compromise

If the owner's account key is compromised, the attacker can issue `UpdateSecretPolicy` to add an attacker-controlled actor to the ACL, then fetch any of that owner's secrets. CBSS does not defend against this and does not attempt to: account-key compromise is the limit of the model and is recovered from out-of-band (account-recovery flows are out of scope for this CIP).

### 8.3 Compromised runner

A compromised runner can exfiltrate the plaintext for the secrets it is *currently authorized to fetch*. It cannot:
- Fetch secrets retroactively (release is anti-replayed per `(job_id, secret_id, runner)`).
- Impersonate other runners (signatures over runner registry key).
- Fetch secrets for jobs it isn't currently assigned to (proxy validates dispatcher's on-chain assignment).

This is consistent with the trust model of CIP-2: the runner is in the trust path for the duration of the job, period. CBSS does not change that.

### 8.4 Validator / dispatcher / chain-observer privacy

| Party | Sees |
|---|---|
| Validator | `wrapped_dek` (AES-GCM under IBE-derived K, including public `ephemeral_u`; unrecoverable without σ under the co-BDH assumption), CBFS pointers, ACL, `key_hash` in storage/tx/events (dictionary-guessable for common names), audit events. Plaintext `KeyName` from deployed actor manifests (the entitlement params). NOT plaintext secret values, NOT DEKs, NOT shares. |
| Dispatcher | Job assignments only — no DEK material, no CBFS plaintext |
| Chain observer | Same as validator. `key_hash = keccak256(account || key_name)` hides literal key names from passive scraping but is **dictionary-guessable** for common names (`OPENAI_API_KEY`, `STRIPE_LIVE_KEY`, etc.) — an observer with the public account address can trivially reproduce the hash for any guessed name. Owners who require unguessability use random suffixes. |

### 8.5 Anti-replay and freshness

- Per release: `(release_id, proxy_id)` is the dedup key. `release_id` is defined canonically in §3.5 next to `ReleaseRequestBody`; it is stable across `request_block` re-pinning and includes `release_nonce`, so legitimate second pulls within a job get a distinct release_id. Proxies maintain a 24-hour bloom keyed on `(release_id, proxy_id)`; chain `release_receipt:{release_id} → { quorum_epoch, entries: Map<ProxyId, ReleaseReceipt> }` provides eventual-finality dedup and single-epoch quorum enforcement. `request_hash` (which includes `request_block`) is used only for sig verification on the specific request, not for dedup.
- Pinned-version pulls fail closed if the version was deleted (`SecretVersionDeleted`).
- `wrap_epoch` is read from the SecretVersion on chain (immutable per version); reshare does NOT invalidate in-flight requests since PSS preserves MSK and the proxy's current share validly partial-signs any historical identity.

### 8.6 DKG correctness and liveness

- **Correctness:** Pedersen VSS commitments allow any party to verify share validity. A proxy submitting a malformed share is detectable and slashable for `DkgSabotage`.
- **Liveness:** if the ceremony fails to finalize within `DKG_FINALIZE_BLOCKS`, the requesting tx's DKG bond is partially slashed and refunded; the owner may retry. Lean: VRF-select `n + 2` proxies and require `n` to complete (over-sample for ceremony liveness).

### 8.7 Future-shock: cryptographic break

A break of BLS12-381 (pairing-based co-BDH, the assumption IBE wrap confidentiality rests on per §3.4.4, or co-CDH for BLS signature unforgeability), AES-256-GCM, or secp256k1 ECDLP is a full-platform compromise — none unique to CBSS, all shared with the broader Ethereum/Filecoin/Cowboy ecosystems. As in §8.1, `DeleteSecret` revokes chain-authorized release; deletion does not cryptographically invalidate copied ciphertext or key material. Curve agility (e.g., upgrading the IBE construction to BLS12-461 or a post-quantum threshold scheme) is a future CIP, out of scope here.

### 8.8 Comparison vs TEE-based release

| Property | TEE-rooted release | CBSS (this design) |
|---|---|---|
| Trust root | CPU-vendor attestation | t-of-n proxy non-collusion + slashing |
| Failure mode | TEE break → all secrets exfiltratable retroactively for the lifetime of the wrap | t-collusion → secrets observed by colluders during their committee tenure |
| Hardware requirement | TEE-capable runner | Standard hardware |
| Latency | Single-hop unwrap on TEE | t-of-n threshold round (~250 ms target) |
| Operational footprint | TEE provisioning per runner | New operator role; DKG ceremonies |
| Ecosystem | TEE vendors (Intel, AMD) | CBY-staked operators |
| Auditability | TEE attestation logs | On-chain `SecretReleased` events + per-proxy receipts carrying the runner-signed request |

CIP-24 picks the right column. CIP-23 keeps the left column available for *execution* (and as an additional gate via `tee_required`); the two layer cleanly.

---

## 9. Interaction with other CIPs

### 9.1 CIP-2 (Off-chain compute)

CBSS state belongs to system actor `0x0000…0004` (CIP-2 §Specification; storage keys and lifecycle in §3.2).

### 9.2 CIP-6 (SDK + entitlements)

`secrets.read` is registered in the entitlement registry. The subset-on-upgrade invariant (CIP-6 §12.1) applies: an actor cannot expand the keys it can request without redeploying.

### 9.3 CIP-9 (Runner storage)

CIP-24 is the key-delivery layer for CIP-9 **private volumes**: a volume's data-encryption key (DEK) is a CBSS-released secret. The runner obtains the DEK through a committee seal; the Dispatcher is not on the key path.

**Volume DEK as a committee secret.** At volume creation the DEK is IBE-wrapped to the owner account's CBSS master public key (MPK), like any `SecretVersion` (§3.4). The wrap is stored on the volume's `StorageCommitment` as `wrapped_dek`, bound to the account's `cbss_committee_epoch`. `StorageCommitment.wrapped_dek` uses the same `WrappedDek` format version 2: it carries `ephemeral_u`, uses `K = HKDF(serialize(e(I, MPK)^r), "cbss/ibe/v1" || aad)` owner-side, and validates/decrypts with `K = HKDF(serialize(e(σ, U)), "cbss/ibe/v1" || aad)` runner-side.

The volume path keeps CIP-9's own identity and base AAD — **not** the 172-byte secret `base_aad` of §3.4.3. The IBE identity is `I_vol = hash_to_G1( volume_dek_identity, domain = "cbss/ibe/cip9-volume-dek/v1" )`, and the base AAD is `base_aad = cip9_hash( "cowboy.cip-9.volume-dek-aad.v1", volume_dek_identity )` — a 32-byte value, where `volume_dek_identity` is CIP-9's 32-byte volume-DEK identity hash ("Release at mount" below). The full AEAD AAD is `base_aad || compress(U)` (32 + 96 = 128 bytes), validated by full equality exactly as in §3.1 (`len(aad) != 32 + 96` is malformed and MUST be rejected with `AadMismatch`). The HKDF salt and info prefix remain `"cbss/ibe/v1"` (§3.4.3) — only the hash-to-curve domain and base AAD differ on the volume path.

**Release at mount.** When a storage-attached job is dispatched, the node builds a `SealRequest` for the volume (carrying `owner`, `volume_id`, `dek_version`, `committee_epoch`, and the material hash below) rather than unwrapping anything itself. The committee serves threshold partials per §3.6.3 against the volume-DEK identity `volume_dek_identity(chain_id, owner, volume_id, dek_version, committee_epoch, release_key_material_hash, committee_override_hash)`; the runner Lagrange-combines `t` partials to recover the DEK. **Hold-until-key-delivery:** the node holds the job — it is not exposed to a runner — until the release finalizes (idempotent on duplicate delivery), so a runner receives a storage job only once it can actually decrypt the volume.

**Trading-Post-conditioned volumes (CIP-33).** For a volume registered as a CIP-33 serving artifact, this release rule gains one additional condition: the `SealRequest` is honored only when the CIP-33 §2.5.1 issuance predicate (hire, term, and lifecycle state) also passes. The hire/artifact identities are **not request fields** — `SealRequest` is unchanged, byte for byte: `cbssd` recovers them deterministically from the authenticated `JobSpec` the request's `job_id` resolves to (job → `(hire_id, artifact)` binding is consensus state written at dispatch, per CIP-33 §2.5.1), and evaluates the predicate against chain state at the current block. A request whose job carries no hire binding for a Trading-Post-registered volume is refused. Replay tests: a stale `SealRequest` re-presented after hire revocation or lease-epoch advance is refused by the same state read — the request asserts nothing the state does not confirm. Identity derivation, DST, AAD, material-hash binding, and rewrap semantics are unchanged.

#### 9.3.1 Release-key material-hash binding (re-key safety)

A volume wrap is pinned to the *exact committee key material* it was created against, via a 32-byte **release-key material hash**: `cip9_hash("cowboy.cip-9.release-key-material.v1", chain_id ‖ account ‖ committee_epoch ‖ threshold ‖ committee ‖ MPK ‖ VSS_commitments)`. The volume's `StorageCommitment` records `cbss_release_key_material_hash` (the material the DEK is wrapped against) and `cbss_rewrap_required`.

At mount, a private attachment requires:
1. `cbss_release_key_material_hash` is set (a zero hash is rejected);
2. `cbss_rewrap_required == false`;
3. the recorded hash equals the release-key material **retained for the volume's `committee_epoch`** (`release_key_view.material_hash`).

Otherwise the mount is refused.

**Reshare vs. re-key.** A **reshare** (§3.6.2) preserves `MPK` and increments `committee_epoch`. A volume's wrap is pinned to a *specific* epoch's material hash — unlike a general `SecretVersion`, whose `wrap_epoch`-bound identity makes reshare fully transparent (§10.7) — so reshare is transparent to a volume **only while the prior epoch's material is retained, during the grace window**: the volume keeps serving against its recorded epoch for that window, after which its wrap is stale and must be re-wrapped to a current epoch. An **account re-key** (recovery: the release-key material fundamentally changes) is the harder case — existing wraps can no longer be served, so affected volumes are flagged `cbss_rewrap_required = true` **immediately** and mounts are blocked until the owner re-wraps. Either path resolves the same way: re-wrapping the DEK to the current material updates `cbss_release_key_material_hash` and clears `cbss_rewrap_required` (the CBFS CLI ships the flow).

**Share finality.** Proxies finalize their shares before serving partials (bounded pending share promotions), so a release never combines partials against a half-promoted share across a ceremony boundary.

### 9.4 Protocol opcode allocation and system-actor registry

The canonical protocol codec assigns opcodes 68–84 to CBSS and 60–63 to the trusted-key TEE attestation instructions. See §3.3 for the instruction list and the Technical Whitepaper §9 for the system-actor registry.

### 9.5 CIP-23 (TEE Execution)

CIP-23 attestation is *optional* per secret via the `tee_required` flag. When set, the runner MUST present a CIP-23 attestation valid for the active `job_id`; the chain validates TEE eligibility at receipt submission using the logical TEE Verifier records physically stored at `0x04` (§3.2), and proxy serving must fail closed unless the node authorizer path can confirm a live attestation. The current devnet TEE Verifier validates registered P-256/P-384 ECDSA attestation keys over canonical CBSS attestation bytes. It does **not** yet validate Intel DCAP/TDX or AMD SEV-SNP/VLEK vendor collateral; that remains TEE verification work. TEE here is defense-in-depth on top of threshold release, not the trust root.

### 9.6 CIP-34 (Intent Settlement)

CIP-34's **sealed-bid auctions** consume the **time-lock release mode** (§3.4.6): a bid is IBE-encrypted to identity `request_id` with `target_height = reveal_height`, and the committee posts the height-gated release at `reveal_height` so the native `reveal` handler (and any observer) can decrypt all bids at once — "the key becomes available independent of any bidder." This supplies the caller-chosen auction identity and automatic height-based release required by CIP-34 §Sealed-Bid. No new committee, key, or trust is introduced — sealed auctions ride the same staked CBSS committee. CIP-34 owns the liveness fallback (`AUCTION_GRACE` → cancel the auction, `CancelledGraceExpired`; no OPEN re-open, no escrow held) if the committee fails to release by `reveal_height + AUCTION_GRACE`.

**Bootstrapping the `0x14` committee.** The sealed-bid path presumes `Account(0x14)` already has a live CBSS committee / `MPK`, but `0x14` is keyless and cannot self-issue `RequestAccountDkg`. **`GovRequestSystemDkg` (opcode 158, §3.5)** closes this: governance (`0x09`) issues one `GovRequestSystemDkg { scope: Account(0x14), force_rekey: false }` to establish the committee via the ordinary DKG → `RotateCommittee` path (no bond, no new trust). This MUST happen before the first `OpenAuction`. Governance MUST NOT `force_rekey` the `0x14` committee while auctions are in flight (see §3.5 — a re-DKG installs a new `MPK` and strands in-flight bids); routine refresh uses `RequestReshare` (§10.7).

---

## 10. Rationale

### 10.1 Why threshold IBE on BLS12-381

See §3.4.5 for the full argument. Short version: vetted libraries exist for threshold BLS signatures and IBE on BLS12-381 (`blstrs`, Commonware BLS DKG, `tlock`); they do not exist for threshold ElGamal on secp256k1. Using vetted cryptographic implementations takes priority over reusing the account-signature curve.

The IBE construction does not use kfrags or proxy re-encryption; it uses BLS signature shares as IBE decryption keys. The recipient-targeting problem that kfrag-based PRE designs address is solved here by binding the IBE identity to per-version metadata that all parties can compute deterministically.

### 10.2 Why per-account by default, with per-secret override

The default is a per-account committee, with per-secret overrides for stronger isolation.

- **Per-secret committees** scale `O(num_secrets × n)` system-wide with a fresh DKG every time someone stores a credential. Bootstrap-prohibitive.
- **Per-actor committees** isolate an actor's secrets from the rest of an account's secrets, but the isolation only materializes when ACLs are narrow. Once a secret's ACL fans out across multiple actors, an attacker just attacks the *weakest* of those committees — per-actor is no better than per-account in the wide-ACL case. Per-actor also pays a DKG every time you deploy a new actor, and forces a chain re-wrap on every ACL edit (since each ACL actor needs its own wrapped DEK). Operationally heavy with limited security gain in realistic usage patterns.
- **Per-account committees** match the practical sharing patterns of production secret management (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault are all account/project-scoped). Bootstrap is one DKG amortized over the account's lifetime. ACL edits are pure metadata. Storage is `O(1)` wrapped DEK per version regardless of ACL size.

The trade with per-account is **bigger blast radius per t-collusion**: an attacker who compromises ≥ t proxies in an account's committee can decrypt every secret that account ever wrapped under its `MPK`. Mitigations:

1. **Tunable `(n, t)` per account** — high-value accounts can raise `n` and `t` through governance-supported account parameters once larger vetted DKG quorums are enabled, instead of using the default `(5, 4)`.
2. **Per-secret committee override** — a single high-value secret (`STRIPE_LIVE_KEY`) can opt into its own committee at `SetSecret` time, isolating its blast radius from the rest of the account's secrets. This is the escape hatch.
3. **Proactive resharing** — every `RESHARE_INTERVAL_BLOCKS`, the share polynomial rotates. An attacker has a finite window.
4. **Operator diversity governance** — committee admission policy can require jurisdictional / vendor / org diversity.

Per-account-with-override is the right default: cheap and simple in the common case, with explicit isolation available for the cases that need it.

### 10.3 Why a new operator role, not extending Relay Nodes

Different failure semantics, slashing math, and operator profile (§3.1). A node can register as both Relay and Proxy; the protocol roles stay distinct.

### 10.4 Why in-memory bundle by default (and explicit subprocess env when required)

Limits blast radius. Plaintext exists only in the runner-op execution context, only as the substituted final-form request. Never enters PVM Python state; cannot be accidentally logged, persisted, or echoed in error messages.

### 10.5 Why two-layer access control (manifest ∩ ACL)

Manifest is the actor's *advertised need* (caught at deploy-time review). ACL is the owner's *consent* (caught at the runtime gate). Both layers answer different threats: a malicious actor lying about its needs is caught at deploy time; a compromised owner key that grants too broadly is at least bounded by the manifest of each individual recipient actor.

### 10.7 Why `MPK` is preserved on reshare

Owners encrypt to `MPK`. If reshare changed `MPK`, every existing wrapped DEK on chain would become un-decryptable, forcing owner re-encryption en masse. Preserving `MPK` across reshare (proactive secret sharing on the same `MSK`) means reshare is invisible to owners — only the share polynomial rotates.

Note: the per-version IBE identity is bound to the SecretVersion's immutable `wrap_epoch`, not to the live `committee_epoch`. Reshare changes `committee_epoch` but does NOT change any existing version's `wrap_epoch`. New `SetSecret` calls after a reshare bind to the then-current `committee_epoch` as their `wrap_epoch`. The threshold-sig math works for any historical identity because PSS preserves MSK across reshare.

### 10.8 Why per-proxy receipts, not a single aggregate `RecordSecretRelease`

A single aggregate audit/payment instruction submitted "by the runner or any participating proxy" would leave payment dependent on either runner cooperation (which the runner has no incentive to provide once it has the plaintext) or implicit proxy coordination (unspecified, and prone to gas-bidding wars or drop-on-the-floor).

`SubmitReleaseReceipt` (opcode 76) instead makes each proxy submit its own receipt independently. Each accepted receipt debits `RELEASE_FEE_PER_RECEIPT` from the actor's account (subject to overdraft cap) and credits the submitting proxy. **Payment is capped at quorum**: only the first t receipts per `release_id` are paid; receipts t+1..n (which a runner may have triggered by over-fanning out) are rejected with `QuorumAlreadyReached`. Properties:

- **Runner is removed from the payment path.** Cannot grief proxies by withholding submission.
- **No coordination required.** Each proxy decides independently when to submit; the **first t** proxies to land on chain are paid; later submissions for the same release_id (whether from the n-t over-fanout or from the same proxy on a re-pin) are rejected. Proxies SHOULD watch `release_receipt:{release_id}.entries` and stop attempting once `entries.len() == t`.
- **Auditable.** `release_receipt:{release_id} → { quorum_epoch, entries: Map<ProxyId, ReleaseReceipt> }` accumulates the full t-of-n proof of release on chain, each entry carrying the verbatim runner-signed request and the verified `σ_i`. The `SecretReleased` event fires when the t-th entry lands, carrying the actual `proxy_set` of t contributors.
- **Cost:** t txs per release instead of one. Acceptable for a system whose default release rate is O(1 per actor-job); for very-high-frequency cases, an aggregator pattern is possible later without breaking the on-chain interface.
