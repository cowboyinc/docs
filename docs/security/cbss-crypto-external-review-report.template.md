# CBSS Crypto External Review Report Template

Copy this file to `cbss-crypto-external-review-report.md` only after the
external reviewer has completed the review. Do not create the final report path
with placeholders; `cbss-review.sh` treats that path as the ship-gate artifact.

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

## Scope Reviewed

- `runner/crates/cbss-crypto/src/identity.rs`
- `runner/crates/cbss-crypto/src/ibe.rs`
- `runner/crates/cbss-crypto/src/threshold.rs`
- `runner/crates/cbss-crypto/src/vetted_dkg.rs`
- `runner/crates/cbss-crypto/src/dkg.rs`
- `runner/crates/cbss-crypto/src/reshare.rs`
- `runner/crates/cbss-crypto/src/hpke.rs`
- `runner/crates/cbss-crypto/src/share_storage.rs`
- `runner/crates/cbss-crypto/Cargo.toml`

## Methods

Describe manual review, proofs checked, tests run, dependency review, and any
limitations.

## Required Questions

1. IBE derivation:
2. Identity binding and domain separation:
3. Partial verification soundness:
4. Threshold combine correctness:
5. Commonware DKG integration, bias-resistance inheritance, qualification behavior, and offline dealer handling:
6. PSS reshare correctness and MPK preservation:
7. HPKE recipient-specific payload usage:
8. Sealed share storage:
9. Replay boundaries:
10. Memory hygiene:

## Findings

### Blocking

None.

### Significant

None.

### Informational

None.

## Final Statement

cbss-crypto is acceptable for the CIP-24 mainnet flag-flip after any listed
blocking findings are resolved.
