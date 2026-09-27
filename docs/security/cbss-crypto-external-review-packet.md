# CBSS Crypto External Review Packet

Status: prepared for the June 2026 pre-mainnet review window.

This packet scopes the external cryptographic review required by CIP-24 before
any mainnet `--feature cbss` flag flip. The review is deferred to June 2026 and
is not part of current implementation completion. This packet is not itself a
review sign-off and must not be treated as evidence that the external review has
already happened.

## Review Scope

Primary code:

- `runner/crates/cbss-crypto/src/identity.rs`
- `runner/crates/cbss-crypto/src/ibe.rs`
- `runner/crates/cbss-crypto/src/threshold.rs`
- `runner/crates/cbss-crypto/src/vetted_dkg.rs`
- `runner/crates/cbss-crypto/src/dkg.rs`
- `runner/crates/cbss-crypto/src/reshare.rs`
- `runner/crates/cbss-crypto/src/hpke.rs`
- `runner/crates/cbss-crypto/src/share_storage.rs`
- `runner/crates/cbss-crypto/Cargo.toml`

Integration surfaces needed for context:

- `runner/crates/secrets-manager/src/combiner.rs`
- `runner/crates/secrets-manager/src/threshold_client.rs`
- `runner/crates/cbssd/src/commonware_dkg.rs`
- `runner/crates/cbssd/src/dkg_driver.rs`
- `runner/crates/cbssd/src/orchestrator.rs`
- `runner/crates/cbssd/src/partial_sign.rs`
- `runner/crates/cbssd/src/protocol.rs`
- `runner/crates/cbssd/src/storage.rs`
- `node/execution/src/cbss.rs`
- `node/types/src/cbss.rs`

Reference documents:

- `cowboy/docs/cips/cip-24-secrets-manager.md`
- `cbssd-implementation-plan.md`, especially sections 4, 6.8, 8, 9, 10, 11, and 13
- `cbss-review.md`
- `cbss-coverage-manifest.tsv`

## Required Questions

1. Confirm the IBE derivation:
   `HKDF(serialize(e(identity, MPK)), aad)` on encrypt equals
   `HKDF(serialize(e(sigma, G2_gen)), aad)` on decrypt for
   `sigma = MSK * identity`.
2. Confirm identity binding and domain separation for the account-secret family:
   the 172-byte base AAD
   `u64_be(chain_id) || account || key_hash || u64_le(version) || u64_le(wrap_epoch) || mpk_g2`
   under `cbss/ibe/v1`, hashed to G1 for the IBE identity and extended with
   `compress(U)` to the 268-byte AEAD AAD (CIP-24 §3.1, §3.4.3). In particular
   confirm the two release-integrity bindings folded into that layout: the
   leading `chain_id` (chain separation — a wrap for one chain cannot validate or
   release on another) and the trailing raw `mpk_g2` (release-key binding — a wrap
   under a substituted `mpk'` yields a different identity/AAD and is rejected by
   the chain's byte-equality check, which re-derives `chain_id` and `mpk_g2` from
   committed state). Confirm the sibling families keep their own base AADs and
   DSTs: time-lock (`cbss/tlock/v1`, `tag || u64_le(target_height)`), CIP-9
   volume DEK (`cbss/ibe/cip9-volume-dek/v1`), and CIP-7 wrapped-DEK
   (`cip-7-wd-v1`, which binds `release_key_material_hash`).
3. Confirm partial verification soundness:
   `e(sigma_i, G2_gen) == e(identity, S_i)` where `S_i` is the on-chain VSS
   commitment for that proxy index.
4. Confirm threshold combine correctness, including index handling and
   Lagrange coefficients.
5. Review the production Commonware DKG integration, including the
   `cbss-crypto::vetted_dkg` wrapper, `cbssd::commonware_dkg`, transcript
   routing, qualification behavior, offline dealer handling, and whether the
   final ship path inherits the intended Commonware bias-resistance guarantees.
6. Review PSS reshare correctness and the MPK-preservation invariant.
7. Review HPKE use for recipient-specific DKG/reshare payloads:
   ciphersuite, AAD binding, recipient public-key validation, and failure modes.
8. Review sealed share storage:
   key derivation, AES-GCM nonce handling, AAD coverage, corrupted-share
   detection, and zeroization boundaries.
9. Review replay boundaries between release requests, proxy responses, and
   on-chain receipt deduplication.
10. Review memory hygiene for plaintexts, DEKs, share scalars, and rendered
    secret arguments, noting the explicit stdlib subprocess-env residual.

## Local Evidence Commands

Run from `/Users/chadd/dev/cowboy`:

```bash
./cbss-review.sh
```

Expected current local result before the June 2026 external review:

- `cbss-review.sh` may still fail while implementation DoD items unrelated to
  the external review remain open.
- Once implementation DoD items close, `cbss-review.sh` should not require an
  accepted external report before June 2026.
- The later mainnet flag-flip packet must include an accepted external report at
  `cowboy/docs/security/cbss-crypto-external-review-report.md`.
- The coverage manifest should remain current for all release-path, SDK/CLI,
  e2e, stress, and adversarial scenarios.

Focused crypto tests:

```bash
cd runner
cargo test -p cbss-crypto --lib
cargo test -p secrets-manager --lib combiner
cargo test -p cbssd commonware_dkg
cargo test -p cbssd orchestrator
```

## Bundle Artifact

The reproducible bundle entry point is
`cbss-crypto-review-bundle.sh` at the workspace root. The bundle manifest is
tracked at `cowboy/docs/security/cbss-crypto-external-review-bundle.md`.
Regenerate the tarball with:

```bash
./cbss-crypto-review-bundle.sh
tar -tzf cbss-crypto-external-review-bundle.tar.gz | sed -n '1,80p'
```

The generated tarball is a handoff artifact for the reviewer. It is not the
accepted review report and does not close the pre-mainnet governance gate by
itself.

## Review Output Required

The external reviewer should deliver
`cowboy/docs/security/cbss-crypto-external-review-report.md` with these exact
machine-checked header fields:

```text
Status: accepted
Review date: YYYY-MM-DD
Reviewer: <name / firm>
Reviewed runner commit: <git sha>
Reviewed node commit: <git sha>
Reviewed cowboy-docs commit: <git sha>
Reviewed workspace commit: <git sha>
Blocking findings: 0
Required questions: all passed
```

The report must also contain the sentence
`cbss-crypto is acceptable for the CIP-24 mainnet flag-flip` after describing
scope, methods, and findings. It must not contain unresolved `TBD`, `TODO`,
`PENDING`, `not reviewed`, or `not accepted` markers.

A fill-in template is available at
`cowboy/docs/security/cbss-crypto-external-review-report.template.md`. Keep the
template at that filename until the external reviewer has completed the report;
do not copy placeholders to the gated report path.

The narrative report should include:

- a dated report naming the exact commit hashes reviewed for `runner`, `node`,
  `cowboy`, and this top-level workspace;
- findings grouped as blocking, significant, informational;
- explicit pass/fail answers for the required questions above;
- recommended fixes for every blocking or significant finding;
- a final statement that `cbss-crypto` is acceptable for the CIP-24 mainnet
  flag-flip after any listed blocking findings are resolved.

The current implementation Definition of Done does not require this report
after the June 2026 deferral. The report remains required before any mainnet
flag-flip proposal is submitted.
