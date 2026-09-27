---
title: "CIP-23: TEE Execution and Composite Attestation"
description: Hardware-backed TEE execution for off-chain AI and privacy workloads, with composite attestation, confidential and deterministic verification modes, governance-anchored collateral, and independent chip and operator trust roots.
icon: shield-halved
---

<Note>
  **Status:** Draft<br/>
  **Type:** Standards Track<br/>
  **Category:** Core<br/>
  **Updated:** 2026-07-20<br/>
  **Requires:** CIP-2 (off-chain compute), CIP-3 (fee model), CIP-10 (containers)<br/>
  **Related:** CIP-1 (deferred callbacks), CIP-9 (off-chain storage / CBFS), CIP-13 (delegation), CIP-24 (CBSS)
</Note>

---

## 1. Abstract

This proposal specifies **TEE Execution** for Cowboy — a hardware-backed, end-to-end verifiable path for off-chain AI and privacy-sensitive workloads. It standardizes a **Composite Attestation Envelope (CAE)** that binds an Intel TDX (or AMD SEV-SNP / AWS Nitro) CPU quote together with an NVIDIA NCC GPU report and a service signature into one on-chain-verifiable receipt, and upgrades the **TEE Verifier** system actor at `0x05` into a real attestation pipeline.

Three properties define this specification:

- **Two verification philosophies, kept apart (§3.4).** `TeeAttested` is for **confidential** outputs: a single runner, where the attestation *is* the verification (no cross-runner reproduction). `Deterministic` is for **non-confidential, reproducible** outputs: a committee re-executes and byte-matches the *result*. A confidential output is not reproducible byte-for-byte across runners.
- **On-chain verification is a SNARK-compressed DCAP proof (§3.8).** The chain runs `verify(proof, public_inputs)`; the circuit performs quote parsing, the vendor X.509 chain, ECDSA, and **TCB-level evaluation**. This makes verification deterministic (time-dependent collateral becomes a circuit input anchored to a governance snapshot) and cheap (≈ 1 order of magnitude gas reduction vs a hand-rolled on-chain chain walk).
- **Multi-root trust (§3.6).** After **TEE.Fail (2025-10)** demonstrated physical extraction of attestation keys (and forged TDX attestations on a live chain), a single hardware root is insufficient. This CIP requires `chip_root ∧ operator_root`, where the operator root is a Proof-of-Cloud / deployment endorsement.

This CIP does **not** require every runner to run in a TEE; non-TEE runners continue to serve `None`, `EconomicBond`, `MajorityVote`, `StructuredMatch`, and `SemanticSimilarity` jobs unchanged.

---

## 2. Motivation

CIP-2 defines a `tee_required` flag, a `Deterministic` verification mode, and the TEE Verifier at `0x05`. These require cryptographic evidence of the executing environment; self-declared capabilities cannot provide it. A large body of Cowboy design — PythonVM × TEE secure-kernel patterns, the whitepaper §5.5 "TEE option", CIP-10 §12.3 `BillingAttestation` — presumes a real attestation pipeline. This CIP defines it.

The 2026-Q2 landscape dictates concrete choices: Intel IAS/EPID reached EOL (2025-04-02), shifting the ecosystem to DCAP / Intel Trust Authority and to VM-level confidential computing (TDX, SEV-SNP); NVIDIA Confidential Compute (NCC) on H100/H200/Blackwell is the de facto GPU TEE for AI; Confidential Containers (CoCo) + Trustee are the deployable platform layer.

Two security and implementation considerations inform the design (§3.4, §3.6, §3.8):

1. **TEE.Fail (2025-10)** — a < $1,000 DDR5 physical interposer extracts signing/attestation keys from Intel TDX/SGX and AMD SEV-SNP (deterministic AES-XTS, no replay protection), and was used to **forge TDX attestations on Ethereum BuilderNet**. A forged quote *can* chain to a vendor root. The single-hardware-root assumption is therefore unsafe → **multi-root trust** (§3.6).
2. **On-chain DCAP matured** — production SNARK-compressed DCAP verifiers (e.g. Automata) verify SGX/TDX attestations on EVM at ~1/10th the gas, and resolve the determinism problem by moving time-dependent checks into the circuit → **SNARK verification** (§3.8).

---

## 3. Specification

### 3.1 Terminology

- **CVM** — Confidential Virtual Machine (TDX Trust Domain, SEV-SNP VM, Nitro Enclave).
- **NCC** — NVIDIA Confidential Compute; per-GPU attestation signed by an NVIDIA-rooted key plus an EAT token from the NVIDIA Remote Attestation Service (NRAS).
- **Quote** — a signed, hardware-generated attestation bound to a measurement and caller-supplied `user_data`.
- **Measurement** — a hash of the TEE's initial state (TDX `MRTD`, SNP `LAUNCH_DIGEST`, Nitro `PCR0`; further Nitro PCRs ride in `pcr_extensions`).
- **CAE** — Composite Attestation Envelope (§3.5).
- **CollateralSnapshot** — a governance-published bundle of vendor collateral (root certs, TCBInfo, QEIdentity, CRLs, NRAS root) selected by block height; the *only* source of truth for time-dependent validity on-chain (§3.8.4).
- **Measurement Binding** — on-chain Registry record tying a runner to CPU/GPU measurements, a service pubkey, and an **operator-root reference**, established at registration (§3.9).
- **Chip root / Operator root** — the two trust roots of §3.6.
- **Freshness Anchor** — `(nonce, deadline, generated_at)` in a CAE (§3.5).

### 3.2 System Actor Map (authoritative)

| Address | Actor | Role |
|---------|-------|------|
| `0x01` | Runner Registry | Stake, capabilities, **measurement binding** (+ operator-root ref) |
| `0x02` | Job Dispatcher | VRF selection, candidate filter (TEE filter, §3.10) |
| `0x03` | Result Verifier | Commit-reveal, mode-aware verification dispatch (§3.11) |
| `0x04` | Secrets Manager / CBSS | TEE-gated secret release (wired to `0x05`) |
| `0x05` | **TEE Verifier** | CAE verification pipeline (§3.8) |

This table matches `runner/src/system_actors.rs` (`TEE_VERIFIER = 0x05`, `SECRETS_MANAGER`/CBSS `= 0x04`, …); **that code is authoritative** and wins over any conflicting `CLAUDE.md` / README listing.

### 3.3 Hardware Stack

| Tier | Choice | Role |
|------|--------|------|
| **Primary CPU** | Intel TDX | Confidential VM |
| **Primary GPU** | NVIDIA NCC (H100 / H200 / Blackwell) | Confidential GPU for AI |
| **Secondary CPU** | AMD SEV-SNP, AWS Nitro Enclaves | Alternate backends, same CAE |
| **Restricted** | Intel SGX | `EconomicBond` only — **rejected** for `TeeAttested` / `Deterministic` (IAS/EPID EOL; no live root) |
| **Enablement** | Confidential Containers (CoCo) + Trustee / KBS | Attestation agent, key broker |
| **Attestation** | Intel DCAP / Trust Authority; NVIDIA NRAS; AMD VCEK; AWS Nitro root | Quote collateral |

Canonical TEE-type strings: **`{"tdx","sev","nitro"}`** are eligible for `TeeAttested`/`Deterministic`; **`"sgx"`** supports `EconomicBond` only. Struct enum variants and `registry.rs::CANONICAL_TEE_TYPES` MUST use these same lowercase strings.

Base CVM image: `cowboy/runner-tee-base:v1` (extends CIP-10 §5.5), reproducibly built so its measurement is publicly verifiable.

### 3.4 Verification modes — `TeeAttested` vs `Deterministic`

A confidential output (encrypted per-recipient) is **never byte-identical across runners**, so it cannot be committee-matched. This CIP defines two distinct modes:

| Mode | Output | Committee | What verifies the result |
|------|--------|-----------|--------------------------|
| **`TeeAttested`** | **confidential** (enclave-only / encrypted to requester) | **1** | The CAE *is* the verification — no reproduction, no byte-match |
| **`Deterministic`** | **non-confidential** (public result or published `result_hash`) | ≥ 1 | Committee re-executes and **byte-matches the result** (CIP-2 commit-reveal, on the canonical form — §3.5.2); each result also carries a CAE |

Rules:
- A `tee_required` job **defaults to `TeeAttested`**. Selecting `Deterministic` requires the submitter to declare the output non-confidential.
- For both modes every TEE result MUST carry a `CompositeAttestation` that passes §3.8.
- `tee_type` eligibility: `tdx` / `sev` / `nitro` → both modes; `sgx` → neither (`EconomicBond` only).

> **Wire encoding (normative — matches `cowboy-protocol-codec::job_spec::VerificationConfig`).** `TeeAttested` and `Deterministic` name verification **philosophies**, not `VerificationMode` enum tags. There is **no `TeeAttested` tag** in the on-chain `VerificationMode` enum; its wire tags are `None=0, EconomicBond=1, MajorityVote=2, StructuredMatch=3, Deterministic=4, SemanticSimilarity=5` (single `u8`). TEE attestation is carried by the sibling **`tee_required: bool`** field of `VerificationConfig` (plus **`required_tee_type: Option<String>`**, ≤128 B). The two philosophies map to the wire form as: **`TeeAttested`** = `tee_required = true` with a **confidential single-runner** `VerificationMode` (`None` / `EconomicBond`); **`Deterministic`** = `VerificationMode::Deterministic` (tag 4) with `tee_required == true` (the node enforces that `Deterministic` requires `tee_required`).

### 3.5 Composite Attestation Envelope (CAE)

Encoded as D-CBOR (same determinism guarantees as CIP-3).

```rust
pub struct CompositeAttestation {
    pub version:     u16,                         // = 1
    pub cpu:         CpuAttestation,
    pub gpu:         Option<GpuAttestation>,      // required for AI / GPU jobs
    pub service_sig: ServiceSignature,
    pub freshness:   FreshnessAnchor,
    pub extra:       BTreeMap<String, Vec<u8>>,   // reserved
}

pub struct CpuAttestation {
    pub tee_type:       CpuTeeType,               // Tdx | Sev | Nitro  (Sgx not eligible, §3.3)
    pub quote:          Vec<u8>,                  // raw quote bytes (verified inside the SNARK, §3.8)
    pub cert_chain_cid: Cid,                      // CIP-9 CID for the full cert chain
    pub measurement:    [u8; 48],                 // MRTD / LAUNCH_DIGEST / Nitro PCR0 (vendor-native width)
    pub pcr_extensions: Vec<[u8; 48]>,            // runtime/boot measts: TDX RTMR0..3 / Nitro PCR1..N (empty if none)
}

pub struct GpuAttestation {
    pub tee_type:          GpuTeeType,            // NvidiaNcc
    pub nras_token:        Vec<u8>,               // NRAS-signed EAT (JWT)
    pub gpu_measurement:   [u8; 48],
    pub bound_cpu_pubkey:  [u8; 32],              // CPU↔GPU binding
}

pub struct ServiceSignature {
    pub scheme:         SigScheme,                // Ed25519 only (see note); EcdsaP256 reserved
    pub service_pubkey: [u8; 32],                 // 32-byte Ed25519 key (see "Service-key scheme")
    pub sig:            Vec<u8>,                  // Sign(sk, task_id ‖ req_hash ‖ result_hash ‖ attest_digest)
}

pub struct FreshnessAnchor {
    pub nonce:        [u8; 32],
    pub deadline:     u64,                        // block height
    pub generated_at: u64,                        // block height at quote request
}
```

**`attest_digest`.** The digest MUST be computed over the CAE with the signature field cleared, so it does not contain the signature that signs it:

```
attest_digest := keccak256( D-CBOR(CompositeAttestation { service_sig: ∅, .. }) )
```

All CAE consumers (storage §3.7, secrets §3.12, billing) use this definition.

**`nonce`.** The nonce MUST incorporate an on-chain unpredictable beacon, not be derivable solely from public data:

```
nonce := keccak256( task_id ‖ req_hash ‖ submission_block_hash ‖ beacon )
```

**`beacon` is the CIP-11 §9.2 consensus threshold seed** (a BLS12-381 threshold signature over the round — grinding-resistant, unpredictable until the round is certified) for round `r_submit + SELECTION_SEED_DELAY_VIEWS` (currently 3). It MUST be read from the **committed, execution-readable `ConsumedSeedsV1`** (the same source the CIP-2 dispatcher uses for committee selection), **not** from the node's local, prunable `SeedStore` — only the committed path is deterministic under replay. The chosen round is in the future at submission time, so neither a key-extraction adversary nor a colluding proposer can precompute or grind the nonce; the runner generates the quote once that seed is certified (~`SELECTION_SEED_DELAY_VIEWS` views later), within the remaining `MAX_QUOTE_AGE` window.

A nonce derived solely from publicly-known/predictable values (e.g. block hash alone) is non-conformant.

> **On-chain enforceability (normative status).** This nonce-derivation recipe is a **producer-side MUST**. The chain **cannot** verify the derivation — the verifier treats `nonce` as opaque, checking only its inclusion in `REPORTDATA` (§3.8.5) and non-replay. Beacon-binding is a well-formedness obligation on honest runners, not a chain-checked invariant; an adversary who can already extract attestation keys is out of scope for this control (that is the operator-root's job, §3.6).

**Binding rule (MANDATORY).** The CPU quote's `REPORTDATA` (SNP `REPORT_DATA`, Nitro `user_data`) MUST equal:

```
user_data = keccak256(nonce ‖ service_pubkey ‖ gpu_measurement_if_any)
```

This welds the CPU attestation, service key, GPU report, and task into one tamper-evident receipt.

**Service-key scheme (MANDATORY).** `service_pubkey` is a **32-byte field**, which fits an Ed25519 public key exactly but **cannot** encode a NIST P-256 (secp256r1) point — SEC1 compressed is 33 bytes (1 prefix + 32-byte x) and uncompressed is 65; an x-only 32-byte form loses the y parity and is not a recoverable public key. The field is not merely cosmetic: `service_pubkey` is hashed into `REPORTDATA` (binding rule above) **and** is the HPKE recipient key for secret delivery (§3.12), so both the quote-binding preimage and secret-wrapping require the *actual* key bytes. Therefore, **Ed25519 is the only conformant service-key scheme**; `SigScheme::EcdsaP256` is **reserved and MUST NOT appear in a conformant CAE**. Verifiers MUST reject `EcdsaP256`. Supporting P-256 requires (a) widening `service_pubkey` to a SEC1-encoded point and (b) re-pinning the `REPORTDATA` / HPKE preimages over the new encoding; it is out of scope for this CIP.

#### 3.5.1 Per-path preimages (normative)

A CAE is produced on four paths; each pins its own `nonce` / `REPORTDATA` / `result_hash`. All follow the rules above (`attest_digest` excludes `service_sig`; `nonce` mixes the beacon `b` — the CIP-11 §9.2 committed threshold seed defined above; `REPORTDATA = keccak256(nonce ‖ service_pubkey ‖ gpu_measurement_if_any)`). `service_sig` always signs `scope_id ‖ req_hash ‖ result_hash ‖ attest_digest`.

**`req_hash` recipe (normative).** `req_hash` is computed once, by the Job Dispatcher (`0x02`) at job submission, over the job's `JobType` ONLY (not the whole `JobSpec`):

```
req_hash := keccak256( DOMAIN_TAG ‖ [JOB_SPEC_VERSION] ‖ JobType::encode() )
DOMAIN_TAG = b"cowboy/jobspec-req-digest/v1"
```

where `JobType::encode()` is the canonical commonware-codec bytes (defined by `cowboy-protocol-codec::job_spec`; the first byte is the CIP-11 §9.4 `job_kind` tag) and `JOB_SPEC_VERSION` is the JobSpec wire-format version byte — the single crate-owned constant shared with the `job_spec_hash` recipe (CIP-11 §12.6). It is stored verbatim in `JobSpec.req_hash`; every **job-bearing** path below consumes the stored value and MUST NOT recompute it (the no-request paths pin the `keccak(b"")` constant instead, as the table and prose below state).

| Path (§) | `scope_id` | `req_hash` | `nonce` preimage | `result_hash` |
|---|---|---|---|---|
| Job — TeeAttested/Deterministic (§3.11) | `task_id` | request digest | `keccak(task_id ‖ req_hash ‖ submission_block_hash ‖ b)` | `keccak(result_payload)` |
| Registration (§3.9) | `runner_addr` | `keccak(b"")` | `keccak(runner_addr ‖ registration_block_hash ‖ b)` | `keccak(b"")` |
| Secret release / CBSS (§3.12) | `job_id` | `keccak(b"")` | `keccak(job_id ‖ runner_addr ‖ release_request_block_hash ‖ b)` | `keccak(b"")` |
| Billing — CIP-10 §12.3 | `billing_id` | `keccak(b"")` | `keccak(billing_fields_excl_sig ‖ submission_block_hash ‖ b)` | `keccak(billing_fields_rlp)` |

Paths with no request (registration, secret release, billing) pin **`req_hash = keccak(b"")`** so the `service_sig` preimage `scope_id ‖ req_hash ‖ result_hash ‖ attest_digest` stays byte-exact on every path. For the billing path the GPU term in `REPORTDATA` is replaced by the billing body digest: `REPORTDATA = keccak256(nonce ‖ service_pubkey ‖ keccak(billing_fields_rlp))`. The billing CAE is generated **fresh per event** (the cert chain MAY be cached; the quote MUST be fresh within `MAX_QUOTE_AGE`). These definitions are normatively load-bearing and MUST be byte-exact across all producers and verifiers.

#### 3.5.1a `result_payload` canonicalization

The Job path's `result_hash := keccak256(result_payload)` requires a canonical preimage. Every producer and verifier MUST use:

```
result_payload := [CANONICAL_RESULT_VERSION] ‖ canonical_result_cbor(result.data)
```

- **Encoding.** `canonical_result_cbor` applies the CIP-11 §12.6 CBOR-float64 profile to JSON: recursively sorted text keys for maps; ordered arrays; shortest CBOR integers, including negative integers, for integral in-range numbers; otherwise IEEE-754 binary64. Numeric boundaries are defined by the single-source codec and its fixtures. NaN, infinities, and values exceeding the codec's nesting-depth or encoded-size limits are INVALID.
- **Single source.** `CANONICAL_RESULT_VERSION` is the `u8` wire-format prefix owned by `cowboy-protocol-codec`. Node and runner MUST use the same codec revision and frozen `result.data → result_payload` fixtures, including numeric and depth/size boundary cases. Caps are codec constants, not independently configurable governance parameters. Changes to bytes or the accepted domain require a format-version change and updated cross-stack fixtures.
- **Single-runner failure.** If a `tee_required` result has no canonical preimage, the result is rejected and the job fails verification. There is no cross-runner slash set. `EconomicBond` applies its failed-result bond disposition; `None` has no economic consequence.

**Specification dependency (COW-2703).** The CBOR-float64 profile permits non-integral binary64 in a CAE preimage, while the whitepaper's canonical-encoding rule forbids floats. This must be resolved before CIP-23 is complete: either the whitepaper explicitly permits deterministic binary64 serialization for a single-runner-bound result, or this preimage also excludes non-integral numbers. The `Deterministic` cross-runner rule below excludes them regardless. This CIP does not silently resolve that policy conflict.

**Implementation gap.** The shared codec and cross-stack fixtures exist, but node `execution/src/runner/verifier.rs` and runner `crates/runner-node/src/node.rs` still select JSON serialization under their default configuration. Both callers must use the canonical preimage unconditionally. The shared codec alone does not establish end-to-end conformance.

#### 3.5.2 `Deterministic` cross-runner byte-match

CIP-2 §9 is the primary home of the `VerificationMode::Deterministic` full-result equality rule. Comparison MUST use bare `canonical_result_cbor(result.data)` bytes, without the CAE version prefix, and MUST reject non-integral binary64 from the comparison domain. Canonical serialization does not make floating-point computation reproducible; deterministic outputs MUST use scaled integers or fixed-point values (`Nano`).

An INVALID result has no comparison key and cannot join a cohort or serve as its reference. Select the first valid result in the verifier's deterministic result order as the reference, and count results with exactly the same canonical bytes. Disposition is threshold-gated:

- If the reference cohort reaches the job's stored `threshold`, results outside it, including INVALID results, receive the CIP-2 §6.1 invalid-reveal disposition (`0 + slash`).
- If the reference cohort is below `threshold`, verification fails with no slash.
- If every result is INVALID, verification fails with no slash.

For example, if two runners return `{"rate":3.333}` (INVALID) and one returns `{"rate":3}` (valid), a threshold of two is not met and no runner is slashed. A single surviving INVALID result also fails without a slash. Neither `result_schema` nor the free-form `data_format` selects slash eligibility; the codec and the stored threshold define the decision.

**Implementation gap.** The node has the canonical comparison path, but default execution still uses JSON string equality. The canonical path also contains per-result height conditions on slash eligibility. Those conditions must be removed so every result uses the rules above.

**Separate verification work.** `MajorityVote` and `StructuredMatch` are field-scoped or tolerance-based under CIP-2 §9/§9.1. The default `MajorityVote` path fingerprints full-result JSON. `StructuredMatch` selects configured `match_fields` before fingerprinting, using the full result only when that list is empty; its slash list is schema-based. Default serialization remains non-canonical, while canonical comparison paths are gated. Canonicalization, tolerance semantics, and caller-path coverage must be checked against each mode’s own contract; applying the Deterministic full-result rule to them would not resolve those gaps.

### 3.6 Trust roots — multi-root (`chip_root ∧ operator_root`)

TEE.Fail shows a chip-vendor root alone can be defeated by an adversary with **physical access + root**. This CIP requires two independent roots:

- **`chip_root`** — the vendor attestation, verified via the SNARK pipeline (§3.8).
- **`operator_root`** — a **Proof-of-Cloud / deployment endorsement** (cf. Flashbots Proof-of-Cloud, Intel POE) proving the CVM runs in an attested operational environment, not a bench rig under interposition. One conformant implementation is the shipped governance-trusted-key model (§3.8.2).

Acceptance of a `TeeAttested` or `Deterministic + tee_required` result requires **both roots to pass**. `MeasurementBinding` (§3.9) carries `operator_root_ref`.

**Implementation gap.** The default node build does not enforce both roots for TDX/SEV. `verify_chip_root_snark` is feature-gated, and `handle_verify_cae` can register a governance-endorsed, self-asserted measurement binding when chip verification is unavailable. Such a binding does not satisfy this CIP. Completion requires verified chip-root registration and unconditional binding checks at dispatch, result acceptance, and secret release; failed or unavailable chip verification MUST reject the attested path.

**Threat model (explicit).** The chip root does **not** defend against physical access + root (TEE.Fail class); vendors classify this out of scope. The operator root is the mitigation. Remote/software adversaries without physical access are covered by the chip root alone.

### 3.7 On-chain Storage Strategy

| Field | Where | Rationale |
|-------|-------|-----------|
| `attest_digest = keccak(cae_cbor_without_sig)` | `task/result/<task_id>.cae_digest` | Audit handle |
| `keccak256(measurement)` (32B), `gpu_measurement` hash, `service_pubkey` | Runner Registry `measurement_binding` | On-chain index key; 48-byte native value lives in the CAE |
| Full CAE (30–100 KiB) | CIP-9 Relay Nodes / IPFS; chain stores CID only | Avoid state bloat |
| `seen_nonces[task_id]` | TEE Verifier state, GC'd after `DISPUTE_WINDOW_BLOCKS` | Replay protection |
| `CollateralSnapshot` (root certs, TCBInfo, QEIdentity, CRL, NRAS root) | TEE Verifier state, versioned by `effective_at` | Deterministic time anchor (§3.8.4) |

### 3.8 TEE Verifier Actor (`0x05`)

#### 3.8.1 State

```rust
pub struct TeeVerifierState {
    pub collateral: BTreeMap<u64, CollateralSnapshot>,   // governance-published, keyed by effective_at
    pub seen_nonces: BTreeMap<JobId, BTreeSet<[u8; 32]>>,
    pub last_gc_block: u64,
}
```

Per-runner measurement whitelists live in the Runner Registry `measurement_binding` (§3.9), not here.

#### 3.8.2 Implementation status

The trusted-key path in `node/execution/src/cbss.rs` provides the governance endorsement used by the operator root:

- Instructions (**opcodes 60–63**): `RegisterTeeTrustedKey` / `RevokeTeeTrustedKey` / `SubmitTeeAttestation` / `RevokeTeeAttestation`. Governance registers a trusted quote-signing key per `(tee_type, measurement_hash)`; a runner submits a per-job ECDSA-signed attestation record; the chain verifies the **signature against the governance-registered key** (not a vendor cert chain). CBSS secret release consumes these records through `validate_runner_tee_for_release`.
- This model does **not** verify a hardware cert chain; its trust root is "governance vouches for this measurement→key".

The trusted-key endorsement supplies the `operator_root` (§3.6); it does not substitute for the `chip_root`. The node also contains CAE registration, light verification, and a feature-gated SNARK verifier, but its default self-asserted registration path leaves the two-root requirement incomplete (§3.6).

#### 3.8.3 Instructions

| Opcode | Instruction | Caller | Behavior |
|:------:|-------------|--------|----------|
| 60–63 | `RegisterTeeTrustedKey` / `RevokeTeeTrustedKey` / `SubmitTeeAttestation` / `RevokeTeeAttestation` | governance / runner | Governance endorsement for the operator root (§3.8.2) |
| 124 | `DeprecateBinding { service_pubkey, reason }` | Governance `0x09` | **Emergency force-deprecate** — set a `measurement_binding` to `Deprecated`/`Revoked` **immediately**, before its renewal, on mid-epoch key revocation / CVE (§3.8.8 mid-epoch revocation). Takes effect at the next block. |
| 125 | `VerifyCae { cae, job_id, req_hash, result_hash }` | Result Verifier `0x03` / Registry | Target chip-root SNARK pipeline (§3.8.5) |
| 126 | `UpdateCollateral { snapshot, effective_at }` | Governance `0x09` | Publish a `CollateralSnapshot` (root certs **+ TCBInfo + QEIdentity + CRL + NRAS root**); ≥ `ROOT_UPDATE_DELAY` before `effective_at` |
| 127 | `GcNonces { upto_block }` | Permissionless | GC `seen_nonces` older than `upto_block` |

Opcodes 124–127 are allocated in code; `contract.sys_opcode_uniqueness` guards uniqueness.

> Collateral (TCBInfo / QEIdentity / CRL) is published as **one snapshot** via `UpdateCollateral`. This is what lets governance blacklist a vulnerable microcode/TCB level (§6) — by publishing a snapshot whose TCBInfo marks that level unacceptable.

#### 3.8.4 Determinism via governance-anchored collateral

All time-dependent validity (cert `notAfter`, CRL `nextUpdate`, TCBInfo dates) is evaluated against the `CollateralSnapshot` selected by `block_height` — **never wall-clock**. The snapshot is published by governance with a ≥ 7-day delay (`ROOT_UPDATE_DELAY`). Every validator replaying a transaction selects the same snapshot for the same block, so verification is deterministic.

#### 3.8.5 Verification pipeline (`verify_cae`)

This is the **full** verification used at **registration / renewal** (§3.8.8); steps 3–4 (the SNARK) are skipped on the **per-call light path**, which instead verifies the quote signature against the binding's `bound_quote_key`. The chain verifies a **SNARK proof** whose circuit performs steps 3–4 internally; the remaining steps are cheap on-chain checks. Conceptually:

`verify_cae` runs in one of two **modes** (§3.8.8). `Full` (registration / renewal, low frequency) runs the chip-root SNARK (steps 3–4) and captures the binding. `Light` (per-call, the hot path on every job result) **skips steps 3–4** and instead verifies the per-job quote signature against the binding's `bound_quote_key` — the key the SNARK already verified at registration. The mode is selected by the presence of `proof`: `Full` carries one; `Light` does not.

```text
enum Mode { Full { proof }, Light }   // Full ⇔ proof present; Light ⇔ no proof

fn verify_cae(mode, cae, job_id, req_hash, result_hash, registry, snapshot) -> Result<(), TeeVerifyError>:
    1. Freshness:   now ≤ deadline AND (now − generated_at) ≤ MAX_QUOTE_AGE (= 75 blocks)
    2. Replay:      cae.freshness.nonce ∉ seen_nonces[job_id]

    match mode:
      Full { proof }:                 # registration / renewal — §3.8.8
        3. SNARK/chain: verify(proof) — circuit checks quote parse + vendor X.509 chain
                        (TDX→PCS, SNP→VCEK) + ECDSA, against snapshot roots;
                        (Nitro is the exception — verified directly on-chain, no SNARK, §3.8.9)
                        journal ⇒ ChipRootPublicInputs (measurement, tcb_status, bound_quote_key, …)
        4. TCB level:   tcb_status ∈ governance-allowed set (default {UpToDate})   → else TcbOutOfDate
        4b. Capture:    persist Active binding(service) ← { measurement(s), bound_quote_key := AK
                        from the verified quote, operator_root, tcb_epoch }                 # §3.9

      Light:                          # per-call hot path — NO SNARK, NO per-call CRL/TCB — §3.8.8
        3'. Binding:    b := registry.binding(service); b.status == Active AND now ≤ b.expires_at
                        → else RegistryMismatch
        4'. Quote sig:  verify(b.bound_quote_key, quote.header ‖ quote.report_body, quote.signature)
                        — a single ECDSA against the AK captured at registration  → else SigInvalid

    # common tail (both modes)
    5. Measurement: keccak(cae.cpu.measurement) ∈ registry.binding(service).allowed_cpu
                AND cae.gpu?.gpu_measurement ∈ registry.binding(service).allowed_gpu
    6. Binding:     quote.REPORTDATA == keccak(nonce ‖ service_pubkey ‖ gpu_measurement_if_any)
    7. Service sig: verify(service_pubkey, task_id ‖ req_hash ‖ result_hash ‖ attest_digest, sig)
    8. NRAS (GPU):  verify EAT(JWT) against snapshot.nras_root AND token.measurement == gpu_measurement
    9. Operator root (Full only): operator_root endorsement valid (§3.6, §3.8.2)  → else OperatorRootFail
   10. Accept → insert nonce into seen_nonces[job_id]; else → TeeVerifyError::<kind>
```

An implementer MUST NOT run step 3 (SNARK) on the per-call `Light` path: per-call SNARK verification is infeasible within the freshness window (§3.8.8) and is the explicit motivation for the split.

#### 3.8.6 Error codes

```rust
pub enum TeeVerifyError {
    AttestFail,         // SNARK/cert-chain failure
    TcbOutOfDate,       // TCB level not accepted
    RegistryMismatch,   // measurement not whitelisted
    SigInvalid,         // service signature invalid
    TaskExpired,        // deadline or MAX_QUOTE_AGE exceeded
    TaskReplayed,       // nonce already seen
    InvalidResultHash,  // result_hash mismatch
    UnsupportedTeeType, // no root for this TEE type (e.g. SGX)
    NonceBindingFail,   // REPORTDATA mismatch
    OperatorRootFail,   // operator/Proof-of-Cloud root failed (multi-root)
    TeeUnavailable,     // runner couldn't obtain a quote
}
```

#### 3.8.7 Gas

| Operation | Cycles | Cells |
|-----------|:------:|:-----:|
| `verify_cae` **full** (registration/renewal — SNARK proof verify + steps 1–2,5–9) | ~40,000–120,000 | 64 |
| `verify_cae` **light** (per-call — ECDSA vs `bound_quote_key` + REPORTDATA + lookups; §3.8.8) | ~6,000–12,000 | 32 |
| `UpdateCollateral` | 8,000 | ~1,000 |
| `GcNonces` (per 1,000 entries) | 10,000 | 0 |

Against the CIP-3 lane budgets — with a **code↔whitepaper divergence that must be reconciled before launch, stated here rather than hidden (tracked: COW-3130)**: the deployed `tx_lane` classifier routes attestation instructions (`SubmitTeeAttestation` / `RevokeTeeAttestation`, `verify_cae`, `UpdateCollateral`, `GcNonces`) as `SystemInstruction` → the **System lane (`LANE_SYSTEM_CYCLES = 40,000,000`)**, while the **whitepaper's three lane tables — the `### Dedicated Lanes` table, §6's consensus lane table, and §17.9 (Reserved Capacity — Execution Lanes) — assign attestations wholesale to the Runner lane (`LANE_RUNNER_CYCLES = 8,800,000`)** with no System carve-out (WP §4, fees/basefee, carries no lane table). The divergence is not attestation-specific: `tx_lane`'s only Runner arms are the four `Job*` instructions, its Timer arm (`origin_tx_hash == [0u8;32]`) tests before everything, and **every other system instruction in the corpus falls through the same `_ → System` arm** — attestations are simply the instance this CIP must size against. The corpus cannot claim both; reconciliation is a **whitepaper amendment** (a `SystemInstruction` carve-out in the three tables above) or a `tx_lane` change — COW-3130, out of scope for this CIP. **This CIP sizes headroom conservatively against the smaller, WP-assigned Runner budget so it cannot overstate: ~73–733 CAEs per block by TEE bracket** (8.8M ÷ `verify_cae_full_cycles = 120,000` ≈ 73 for SNARK-full; 8.8M ÷ `verify_cae_light_cycles = 12,000` ≈ 733 for the light/Nitro-order bracket); if the System-lane routing is ratified, the effective budget is ~4.5× larger. Two caveats on that arithmetic, so it is not mistaken for deployed behavior: the deployed schedule holds a **single** `verify_cae_full_cycles = 120,000` (no ~120k…40k range — the range is this CIP's proposed differentiation), and default execution charges only the generic base. Unconditional differentiated metering remains implementation work; no headroom figure here describes current metering. The per-call *light* attestation check normally rides `JobResultSubmit` (Runner-lane under both readings), but it is also reachable as a standalone `VerifyCae { proof: ⟨empty⟩ }` instruction — `proof.is_empty()` selects the light path — which is System-lane in deployed code, so "light ⇒ Runner-lane" does not hold unconditionally. The proof contains the ECDSA/X.509 verification work; the chain pays proof-verification cost.

**Capacity terms.** CIP-3 defines `BLOCK_CYCLES_TARGET = 20,000,000` as the EIP-1559 target and 80,000,000 as the per-block hard cap (the sum of lane budgets). `LANE_SYSTEM_CYCLES = 40,000,000` is a lane budget. The attestation-lane mismatch above remains tracked by COW-3130.

#### 3.8.8 Where the SNARK runs — registration vs per-call (proving-latency split)

zkVM proving of a DCAP quote is **30 s – minutes** (RISC Zero / SP1 / Bonsai), far above `MAX_QUOTE_AGE`. Verifying a fresh chip-root SNARK **on every job** is therefore infeasible. CIP-23 splits the chip root across two cadences:

- **Registration / renewal (low frequency, ~7 days, §3.9).** The full chip-root SNARK (§3.8.5 steps 3–4: vendor cert chain + TCB level) is generated and verified **once per binding**. On success the binding captures the **ECDSA attestation key (AK)** that signs the quote body (`bound_quote_key`) and the accepted measurements / TCB epoch. Proving latency is irrelevant here.

**`bound_quote_key` (normative — which key, and the stability assumption).** In ECDSA-DCAP the **quote body is signed by the attestation key (AK)**; the AK is certified by the Quoting Enclave, whose report is signed by the **PCK** (PCK does **not** sign quote bodies). Therefore `bound_quote_key` MUST be the **AK** — specifically the `ecdsa_attestation_key` carried in the quote's signature section — and the per-call `Light` check (§3.8.5 step 4′) is a single ECDSA of `quote.header ‖ quote.report_body` against that AK. At registration the SNARK verifies the **full chain AK → QE → PCK → Intel root** and the QE-report binding of the AK; the per-call path trusts the captured AK directly and does **no** per-call AK→QE re-verification. Soundness rests on one stated assumption: **the AK is stable for the whole renewal period.** If the QE rotates the AK mid-period, quotes signed by the new AK fail step 4′ (verify against the stale bound AK) and those results are **rejected** until renewal re-captures the new AK — i.e. AK rotation fails closed, never open. (Implementations capture the AK at registration; per-call they MUST verify against the *bound* AK, not against the AK embedded in the incoming quote.)
- **Per call (high frequency).** Result acceptance does **not** re-verify the SNARK. The runner's per-job quote is checked with the **light path**: the quote signature verifies against the binding's `bound_quote_key` (a single ECDSA), plus REPORTDATA binding (step 6), service signature (step 7), freshness (step 1) and replay (step 2). The cert chain / TCB were already proven at registration and are invariant within the TCB epoch.

The guarantee is preserved: a per-job result is accepted only if its quote chains (via `bound_quote_key`) to a binding whose chip-root SNARK **and** operator root passed at registration, the binding is `Active` / unexpired, and the per-job freshness / replay / REPORTDATA checks hold. A platform TCB change (microcode update / revocation) makes the binding fail renewal and drop out — bounding staleness to one renewal period.

**Mid-epoch revocation staleness (explicit governance-accepted risk).** Because the `Light` path does **no** per-call CRL / TCBInfo check, a platform key revoked *after* a binding was captured keeps producing **accepted** results until the binding next fails renewal — a window of up to `BINDING_RENEWAL_PERIOD` (~7 days), versus ~75 s under a hypothetical per-call full check. This is the deliberate cost of moving the SNARK off the hot path (§3.8.8) and is an **accepted risk recorded by governance**, mitigated two ways: (1) shortening `BINDING_RENEWAL_PERIOD` trades proving overhead for a tighter staleness bound; (2) **emergency force-deprecate** — governance MAY call `DeprecateBinding { service_pubkey }` (opcode 124) to set a binding to `Deprecated`/`Revoked` **immediately**, before its renewal, on a mid-epoch revocation or disclosed CVE. After force-deprecation the dispatcher drops the runner at the next block and per-call `Light` verification fails step 3′ (`status == Active`). `UpdateCollateral` (publishing a snapshot whose TCBInfo blacklists the level) only bites at the *next* renewal, so `DeprecateBinding` is the fast path for the up-to-7-day window.

`verify_cae` (§3.8.5) thus has two entry modes: **full** (registration / renewal, carries `proof`) and **light** (per-call, no `proof`). The dispatcher (§3.10) and Result Verifier (§3.11) use the light mode; only `RegisterRunner` / renewal uses the full mode.

> *Rationale.* Proving-latency analysis (RISC Zero/SP1 DCAP ≈ 30 s–min) showed a per-call SNARK cannot fit the 75-block freshness window. Anchoring the heavy proof to the ~7-day binding cadence and binding the platform key keeps per-call acceptance cheap and latency-free while the cert chain / TCB are proven once per renewal period.

#### 3.8.9 Nitro (P3) backend verification — COSE_Sign1, direct (no SNARK)

AWS Nitro Enclaves do not produce a DCAP quote. The platform attestation is a **`COSE_Sign1` document** (CBOR) signed **ES384** by a per-enclave leaf key, certified by an X.509 chain to the **AWS Nitro root** (Root-G1). There is **no microcode TCB** to evaluate. Consequently Nitro does **not** use the chip-root SNARK (§3.8.5 steps 3–4); its verification is a handful of signature / certificate checks performed **directly on-chain**. This subsection specializes §3.8.5 for `cae.cpu.tee_type == Nitro`.

**Why no SNARK.** The chip-root SNARK exists to keep a *heavy* verification (DCAP cert chain + QE identity + TCBInfo / CRL evaluation) off the hot path and succinct. Nitro verification is light — one ES384 signature, a 5-certificate ECDSA chain walk, and CBOR field extraction, with no TCB / CRL logic — so proving it would add a Nitro guest + prover + proving latency for no benefit. Direct on-chain verification is cheaper end-to-end and removes a build/maintain surface. (Verified end-to-end on real Nitro hardware: a 4.5 KB COSE doc, ES384 leaf sig + 5-cert chain to the AWS root, full validation under a few ms.)

**Document shape (normative).** `cae.cpu.quote` carries the raw `COSE_Sign1` = `[protected, unprotected, payload, signature]`. `protected` MUST decode to `{1: -35}` (alg = ES384); `signature` is a 96-byte raw `r ‖ s`. `payload` is the CBOR attestation document with at least: `module_id`, `digest = "SHA384"`, `timestamp`, `pcrs` (map `u8 → 48 B`), `certificate` (leaf DER), `cabundle` (DER chain, root-first), and the binding field **`user_data`** carrying the CIP-23 **REPORTDATA** (§3.5 — Nitro has no dedicated REPORTDATA slot; `user_data` is it). The native `nonce` / `public_key` fields MAY additionally mirror the freshness nonce / service key but are not the normative binding.

**Full path (registration / renewal).** For `tee_type == Nitro`, step 3 of §3.8.5 is replaced by:

```text
3-Nitro. Direct COSE verification (no proof; cae.cpu.quote = COSE_Sign1 doc):
   a. Decode COSE_Sign1; require protected.alg == ES384.
   b. Leaf sig:   verify ES384 over Sig_structure
        ["Signature1", protected, b"", payload] with the leaf cert's P-384 key  → else AttestFail
   c. Chain:      walk cabundle ‖ leaf; each cert ECDSA-verified by its issuer;
        the chain root MUST equal a cpu_root in snapshot.cpu_roots              → else AttestFail
        (the AWS Nitro root is governance-published collateral, §3.8.4 — the
         doc's self-carried root is NEVER trusted)
   d. Measurement: doc.pcrs[0] == cae.cpu.measurement   (PCR0 = launch digest)  → else RegistryMismatch
        AND doc.pcrs[i] == cae.cpu.pcr_extensions[k] for the bound PCR set
        (e.g. PCR1, PCR2), each 48 B
   e. Binding:    doc.user_data == REPORTDATA
        == keccak(cae.freshness.nonce ‖ service_pubkey ‖ gpu_measurement?)       → else NonceBindingFail
        (§3.5 / §3.8.5 step 6 — pins the freshness nonce AND the Ed25519
         service key inside the AWS-signed doc; the GPU term covers P2 jobs)
   f. tcb_status := UpToDate   (Nitro exposes no TCB; §3.8.5 step 4 is a no-op)
   g. Capture:    persist Active binding(service) ← { measurement(s),
        bound_quote_key := service_pubkey, operator_root, tcb_epoch }           # §3.9
```

The captured **`bound_quote_key` for Nitro is the Ed25519 `service_pubkey`**, not a platform attestation key. This differs from DCAP (where it is the AK, §3.8.8) and is the crux of the per-call path below. The service key is bound into the AWS-signed document **transitively**, as a term of the `user_data` REPORTDATA preimage (step e).

**Per-call path (light) — service key only.** The Nitro leaf signing key is **ephemeral**: a fresh key per enclave boot with a short-lived certificate. It is therefore unusable as a stable per-call verification key (unlike a DCAP AK, which §3.8.8 assumes stable for the renewal period). Nitro per-call verification MUST NOT re-verify the COSE document. Registration step (e) already bound the **stable Ed25519 service key** into the AWS-signed document (as a REPORTDATA term); per-call acceptance then rests on:

- §3.8.5 step 7 — the **service signature** over `task_id ‖ req_hash ‖ result_hash ‖ attest_digest`, verified against `service_pubkey` (== `binding.bound_quote_key`);
- §3.8.5 step 5 / step 3′ — measurement whitelist; binding `Active` and unexpired.

Step 4′ (quote-sig vs `bound_quote_key`) and step 6 (REPORTDATA) **do not apply** per-call to Nitro: there is no per-call platform quote, and the REPORTDATA (`user_data`) binding was already checked once, at registration (step e), inside the AWS-signed document. The §3.8.8 guarantee holds unchanged: a per-job result is accepted only if it carries a service signature from a key that an AWS-rooted document bound to a whitelisted enclave measurement at registration, the binding is `Active` / unexpired, and freshness / replay hold.

**Staleness & rotation.** Leaf-cert / enclave-key rotation does not affect the per-call path (it never trusts that key). A re-launched enclave with a different PCR0 yields a different measurement and fails the whitelist until re-registration. Mid-epoch revocation of the AWS root or governance distrust of a measurement uses the same controls as DCAP: `DeprecateBinding` (immediate) and `UpdateCollateral` (next renewal) — §3.8.8.

**Error mapping.** Nitro reuses §3.8.6 codes: COSE decode / ES384 / chain failure → `AttestFail`; `user_data` REPORTDATA mismatch → `NonceBindingFail`; PCR not whitelisted → `RegistryMismatch`; no AWS root in the snapshot → `UnsupportedTeeType`.

**Gas.** Nitro **full** replaces the SNARK-verify term with on-chain COSE + X.509 work: one ES384 verify + a 5-cert ECDSA chain walk + CBOR extraction — on the order of the DCAP **light** path, and bounded likewise. There is **no** per-call Nitro cost beyond §3.8.5's common tail. A full cycle accounting is deferred to implementation.

### 3.9 Runner Registry Extension

```rust
pub struct MeasurementBinding {
    pub cpu_tee_type:             CpuTeeType,
    pub allowed_cpu_measurements: BTreeSet<[u8; 32]>,    // keccak(48-byte measurement)
    pub allowed_gpu_measurements: BTreeSet<[u8; 32]>,    // empty → GPU not required
    pub service_pubkey:           [u8; 32],
    pub bound_quote_key:          Vec<u8>,                // ECDSA attestation key (AK) that signs the quote body; AK→QE→PCK→root chain verified by the SNARK at registration; per-call quote sigs verify against it (§3.8.8)
    pub operator_root_ref:        OperatorRootRef,        // Proof-of-Cloud / deployment endorsement
    //   wire: OperatorRootRef { kind: u8, data: Vec<u8> } (matches cowboy_types::tee). `kind`
    //   discriminates the operator-root scheme (kind=1 = shipped governance-trusted-key model,
    //   §3.8.2); `data` is scheme-specific and opaque in P1 — a deliberate extension point,
    //   its layout defined per-`kind` as each Proof-of-Cloud scheme is specified.
    pub bound_at:                 u64,
    pub expires_at:               u64,                    // default bound_at + BINDING_RENEWAL_PERIOD
    pub status:                   BindingStatus,          // Active | Deprecated | Revoked
}
```

Registration: a runner boots a CVM, derives a service keypair, submits `RegisterRunner(stake, rate_card, initial_cae, service_pubkey, claimed_measurements, operator_root)`; the Registry cross-calls `0x05::VerifyCae` in **full** mode (chip-root SNARK, §3.8.8) and validates `operator_root`; on success it persists an `Active` binding **capturing `bound_quote_key`** (the **attestation key (AK)** whose AK→QE→PCK→root chain the SNARK verified) for the per-call light path. A binding MUST be renewed every `BINDING_RENEWAL_PERIOD` (default 604,800 blocks ≈ 7 days at 1 s); expired → `Deprecated` (leaves the candidate pool; historical results stay verifiable). Renewal re-runs the full SNARK, so a TCB/microcode change is caught within one renewal period.

**Attestation-first registration** closes the swap-binary race: binding measurements at registration **plus** per-job CAE nonce binding means any binary swap invalidates both.

### 3.10 Dispatcher Filter

For `tee_required` jobs, the runner's Registry record MUST have a `measurement_binding` with `status == Active`, `expires_at > submission_block`, and `cpu_tee_type` matching the job's `required_tee_type` (if set). Self-declared `capabilities.tee_support` MUST NOT be used as proof of TEE capability for acceptance.

**Implementation gap:** the dispatcher has a binding-check path, but its default configuration still permits self-declared `tee_support`. It must require verified `measurement_binding` records unconditionally.

### 3.11 Result Verifier

Per-call acceptance uses the **light** `verify_cae` (§3.8.8) — no per-call SNARK; the quote signature is checked against the binding's `bound_quote_key` plus REPORTDATA / service-sig / freshness / replay.

- **`TeeAttested`:** committee = 1; the result MUST carry a CAE; `0x03` runs the light `verify_cae` and accepts on pass. No byte-match (the output is confidential).
- **`Deterministic`:** every result MUST carry a CAE; `0x03` runs the light `verify_cae` per result, then performs the byte-identical match **on the (non-confidential) result payload**, using the canonical, no-floats comparison and threshold-gated failure/slash dispositions in **§3.5.1a / §3.5.2**.
- Other modes MAY attach a CAE; if present it MUST pass the light path, except `sgx` CAEs which are rejected (`UnsupportedTeeType`).

### 3.12 Secrets Manager / CBSS Integration

`Secrets Manager (0x04)` / CBSS gates a secret release on a valid CAE when the secret's policy sets `tee_required`. Secrets are delivered HPKE-wrapped to the runner's `service_pubkey`; cleartext exists only inside the CVM.

CIP-24 release authorization MUST require an Active, unexpired, verified `measurement_binding` and re-check it at release, so `DeprecateBinding` blocks further release after mid-job revocation. The fresh per-release CAE uses §3.5.1's secret-release preimages.

**Implementation gap.** `validate_runner_tee_for_release` currently checks existing bindings but can accept a governance-key attestation record when no binding exists. A binding can also originate from the self-asserted registration path (§3.6). Completion requires verified bindings on every release and fresh per-release CAE verification; the current gate does not establish both requirements.

### 3.13 `tee_call` SDK Helper

No new PVM syscall. `cowboy_sdk.tee.tee_call(...)` is a Python wrapper that emits a `SubmitJob` with `tee_required = true` (mode defaults to `TeeAttested`; pass `deterministic=True` for the reproducible path). PVM determinism is unaffected.

### 3.14 The three-layer chain (defense in depth)

A TEE-required job touches three independent layers, each at a different lifecycle moment:

| Layer | Field / record | When set | Authority |
|---|---|---|---|
| Actor manifest | `sec.tee_required` entitlement | Actor deploy | Actor owner |
| Job spec | `VerificationConfig.tee_required` (+ mode) | `submit_task` | Submitter |
| Runner record | `measurement_binding` (§3.9) | Registration / renewal | Runner |

If the manifest declares `sec.tee_required`, all the actor's jobs MUST set `tee_required = true` (submitter-side validation rejects violations). The dispatcher (§3.10) selects only bound runners; the Result Verifier (§3.11) requires a passing CAE.

### 3.15 CIP-13 (delegation) interaction

TEE eligibility is a **categorical capability check** (`measurement_binding.status`), not stake-thresholded. Delegated stake raises VRF weight but does **not** confer eligibility. Slashing for forged attestation: self-stake uncapped; delegator slash capped at `MAX_DELEGATION_SLASH_PER_EPOCH_BPS = 500` (5%) per CIP-13 §3.6. Delegators have no claim on or visibility into secrets the runner accesses.

### 3.16 Parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| `MAX_QUOTE_AGE` | 75 blocks (≈75 s @ 1 s) | Quote freshness window |
| `BINDING_RENEWAL_PERIOD` | 604,800 blocks (≈7 days @ 1 s) | Re-attestation |
| `ROOT_UPDATE_DELAY` | 1 week | Governance delay for `UpdateCollateral` |
| `SEEN_NONCE_GC_WINDOW` | = `DISPUTE_WINDOW_BLOCKS` (75) | Nonce GC |
| `MAX_CAE_ON_CHAIN_BYTES` | 64 (digest only) | Full CAE off-chain (CIP-9) |
| Cycle **target** `T_c` | 20,000,000 | CIP-3 — the `BLOCK_CYCLES_TARGET` code constant; sustained per-block EIP-1559 target |
| Per-block **hard cap** | 80,000,000 | CIP-3 — sum of lane budgets (`4 × T_c`); whole-block admission ceiling, not single-workload headroom |

---

## 4. Rationale

- **Two modes, not one.** Confidential output (the whole point of TEE for privacy) and committee byte-match are mutually exclusive. `TeeAttested` makes attestation the verification; `Deterministic` keeps reproduction for public outputs.
- **SNARK over hand-rolled chain.** Moving X.509 + ECDSA + TCB evaluation into a circuit solves two problems at once: the on-chain **non-determinism of time** (collateral becomes a snapshot-anchored circuit input) and **gas** (verify a proof, not a 7-layer walk). It also reuses an industry-proven pattern.
- **SNARK off the hot path (§3.8.8).** zkVM DCAP proving is ~30 s–minutes — it cannot fit the 75-block freshness window per call. Verifying the chip-root SNARK at registration / renewal (~7 days) and binding the platform key (`bound_quote_key`) lets per-call acceptance be a single ECDSA + REPORTDATA / freshness / replay — cheap and latency-free — while the cert chain / TCB are proven once per renewal period.
- **Multi-root.** TEE.Fail proved a forged-but-root-valid quote is achievable with physical access. A second, operationally-rooted endorsement (Proof-of-Cloud) is the pragmatic mitigation.
- **CPU + GPU via REPORTDATA binding.** Without cross-binding, CPU and GPU attestations can be mixed and matched; the `user_data` rule makes the CPU quote worthless without the matching GPU report and service key.
- **On-chain digest, off-chain body.** Anchoring only `attest_digest` keeps state growth independent of attestation size; the full CAE lives on CIP-9.
- **`attest_digest` excludes the signature.** Otherwise the digest contains the signature that signs it — a circular definition that cannot be implemented.
- **Unpredictable nonce.** A publicly-derivable nonce offers no freshness against a key-extraction adversary; an on-chain beacon does.

---

## 5. Integration requirements

- Non-TEE runners serve jobs that do not require attestation and cannot be selected for `tee_required` jobs.
- A governance-trusted key supplies only the operator endorsement. Every attested path requires both roots and fails closed when either is missing or invalid.
- Dispatcher and result verification require verified `measurement_binding` records; self-declared `tee_support` is not attestation evidence.
- CIP-10 `BillingAttestation.tee_signature` is `Option<CompositeAttestation>`, freshly generated per billing event with the byte-exact `nonce`, `REPORTDATA`, and `result_hash` in §3.5.1. The cert chain MAY be cached; the quote MUST be fresh within `MAX_QUOTE_AGE`.

---

## 6. Security Considerations

- **Threat model (§3.6):** chip-vendor root does **not** cover physical access + root (TEE.Fail). Mitigated by the **operator root**; remote/software adversaries are covered by the chip root.
- **TEE.Fail / physical extraction:** a forged quote can chain to a vendor root. Single-root acceptance is therefore disallowed; `chip_root ∧ operator_root` is required.
- **Forged quotes (remote):** rejected by the SNARK chip-root verification.
- **Replay:** `nonce` (with beacon) is bound inside `REPORTDATA` and checked against `seen_nonces[task_id]` for the dispute window.
- **Vulnerable microcode / TCB:** governance publishes a `CollateralSnapshot` whose TCBInfo marks the level unacceptable → step 4 (`TcbOutOfDate`).
- **NRAS centralization:** verified against `snapshot.nras_root` (no live call); a threshold multi-provider fallback is a follow-up.
- **CAE availability:** the on-chain `attest_digest` proves the receipt exists even if the CIP-9 body is unreachable; runners SHOULD pin to ≥ k Relay Nodes.
- **GPU memory residue:** addressed by CIP-10 §14.6.

---

## 7. Implementation Roadmap

| Phase | Deliverable | Exit criteria |
|:-----:|-------------|---------------|
| **P0 (done)** | Governance-trusted-key endorsement (opcodes 60–63), quote collection (`runner-tee`), CBSS `tee_required` gate | Shipped; serves as operator root |
| **P1** | Chip root: SNARK-compressed TDX DCAP + `UpdateCollateral` + TCB step; dispatcher → `measurement_binding`; modes split | TDX `TeeAttested` end-to-end; red-team forged/replayed/expired/TCB all rejected |
| **P2** | CPU+GPU composite (NCC + NRAS) + CoCo/Trustee; multi-root acceptance enforced | Confidential LLM inference with composite CAE; both roots required |
| **P3** | SEV-SNP + Nitro backends; CAE off-chain (CIP-9); governance collateral exercised | ≥ 2 backends; dispute window exercised against a malicious runner |

Net effort to reach the full chip-root + multi-root design is estimated at ≈ 20–30 engineer-weeks, dominated by the SNARK DCAP verifier.

---

## Appendix A: Reference Mapping (PythonVM × TEE prior art ↔ Cowboy)

| Prior-art term | Cowboy equivalent |
|----------------|-------------------|
| `tee_call` syscall | `cowboy_sdk.tee.tee_call()` → `SubmitJob` |
| CAE | `cowboy_types::tee::CompositeAttestation` |
| Service Registry | CIP-2 Runner Registry + `measurement_binding` |
| verify / replay path | Cowboy speculative execution + cached batch apply |

## Appendix B: TEE landscape (snapshot, informative)

**CPU/GPU backends.** Intel **TDX** (primary, Azure/Google/Alibaba), **NVIDIA NCC** (primary GPU; H100/H200/Blackwell; ~2–5% LLM overhead), **AMD SEV-SNP** and **AWS Nitro** (secondary), **Intel SGX** (IAS/EPID EOL 2025-04-02, `EconomicBond` only), ARM CCA (tracked, immature). Enablement: **Confidential Containers (CoCo) + Trustee/KBS** (primary platform layer, reused from CIP-10). Not used: Gramine/Occlum/Open Enclave (SGX LibOS/SDK — superseded by VM-level CC).

**2026-06 update (informs §3.4/§3.6/§3.8).**
- **TEE.Fail (2025-10):** < $1,000 DDR5 interposer extracts keys from TDX/SGX/SEV-SNP (deterministic AES-XTS); forged TDX attestations on Ethereum BuilderNet. Vendors deem physical attacks out-of-scope → drives **multi-root** (§3.6).
- **On-chain SNARK DCAP (Automata et al.):** production SGX/TDX attestation on EVM with SNARK compression (~1/10 gas) and decentralized collateral caching → drives **§3.8 SNARK pipeline**.
- **Proof-of-Cloud (Flashbots) / Intel POE:** chip-root + operator-root composition → the `operator_root` of §3.6.
- **NVIDIA Blackwell CC:** B200/GB200 confidential mode (encrypted PCIe 5.0 / NVLink); CC deployment guide v7.1 (2026-04).

**Sources:** TEE.Fail — bleepingcomputer.com/news/security/teefail-attack-breaks-confidential-computing-on-intel-amd-nvidia-cpus/ ; Proof-of-Cloud — writings.flashbots.net/mind-the-gap-tee-poc ; on-chain SNARK DCAP — medium.com/@oraclethaacat/beyond-dcap-... ; NVIDIA CC — docs.nvidia.com/cc-deployment-guide-tdx.pdf
