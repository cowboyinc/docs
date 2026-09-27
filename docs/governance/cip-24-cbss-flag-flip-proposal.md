# CIP-12 Proposal Draft: Enable CIP-24 CBSS Parameters and Feature Flag

Status: Draft, blocked on June 2026 external security review

## Summary

Register the CIP-24 Cowboy Secret Service parameter table and enable the CBSS
feature flag after the implementation passes the Definition of Done in
`cbssd-implementation-plan.md`.

This proposal should not be submitted until:

- the June 2026 external cryptographic review of `runner/crates/cbss-crypto` is delivered and all blocking findings are resolved,
- the checked coverage manifest remains current for the CIP-24 release-path, SDK/CLI/liveness, end-to-end, stress, and adversarial scenarios,
- `cbss-review.sh` exits 0 in full mode,
- the release branch includes the final reviewed CBSS operator runbook.

## Proposed Action

Pass a CIP-12 Tier 0 governance action that writes the CBSS parameter table
below, then flips the chain/runtime CBSS feature gate from disabled to enabled
for production network operation.

The proposal is intentionally a single activation action: parameter registration
and feature enablement land together so the flag cannot be enabled without the
governed values that validators, cbssd operators, runners, and SDK clients use
for admission, gas, liveness, and slashing behavior.

## Parameter Table

All values are initial mainnet defaults. Future changes require a CIP-12 Tier 0
parameter proposal unless a later CIP delegates a narrower parameter to a lower
tier.

| Parameter | Initial value | Purpose |
|---|---:|---|
| `cbss.feature_enabled` | `true` | Enables CIP-24 system-instruction admission. |
| `cbss.default_committee_n` | `5` | Default account-scoped DKG committee size. |
| `cbss.default_threshold_t` | `4` | Default account-scoped release threshold for the five-proxy Commonware `N3f1` DKG. |
| `cbss.min_proxy_stake` | `1` | Minimum stake for proxy registration in current smallest-unit testnet economics. |
| `cbss.dkg_bond` | `1000` | Owner bond for DKG requests. |
| `cbss.dkg_timeout_slash` | `50` | Portion of a timed-out DKG bond not refunded. |
| `cbss.dkg_finalize_blocks` | `7200` | DKG completion deadline. |
| `cbss.proxy_soak_period_blocks` | `100` | Blocks before a registered proxy becomes eligible. |
| `cbss.request_freshness_blocks` | `384` | Release-request freshness bound. |
| `cbss.liveness_min_challenge_delay_blocks` | `416` | Minimum age before a runner may challenge a proxy for non-response. |
| `cbss.liveness_response_blocks` | `64` | Proxy response window after a liveness challenge. |
| `cbss.liveness_challenge_max_age_blocks` | `480` | Latest block at which a liveness challenge may be admitted. |
| `cbss.liveness_challenge_bond` | `100` | Runner bond for liveness challenges. |
| `cbss.release_fee_per_receipt` | `1` | Per-proxy payment for each accepted release receipt. |
| `cbss.max_release_overdraft` | `16` | Maximum actor release debt, equal to `16 * release_fee_per_receipt`. |
| `cbss.max_secrets_per_account` | `256` | Account-level secret count cap. |
| `cbss.max_versions_per_secret` | `32` | Active version cap per secret key. |
| `cbss.max_acl_actors` | `64` | Actor allowlist cap per secret version. |
| `cbss.invalid_partial_slash_bps` | `500` | Stake/health slash for invalid partial re-encryption evidence. |
| `cbss.dkg_sabotage_slash_bps` | `500` | Stake/health slash for signed DKG sabotage evidence. |
| `cbss.equivocation_slash_bps` | `5000` | Stake/health slash for signed equivocation evidence. |
| `cbss.min_health_floor_bps` | `500` | Minimum health weight used during proxy selection. |
| `cbss.reshare_grace_blocks` | `100` | Prior-committee receipt acceptance window after rotation. |
| `cbss.max_prior_committee_epochs` | `4` | Retained prior committee snapshots per release key. |
| `cbss.max_tls_spki_bytes` | `4096` | Maximum stored proxy TLS SPKI DER size. |

## Gas Parameters

| Parameter | Cycles | Cells |
|---|---:|---:|
| `cbss.gas.set_secret_default` | `12000` | base cells |
| `cbss.gas.set_secret_override` | `38000` | base cells |
| `cbss.gas.finalize_secret_version` | `8000` | base cells |
| `cbss.gas.update_secret_policy` | `6000` | base cells |
| `cbss.gas.delete_secret_version` | `5000` | base cells |
| `cbss.gas.delete_secret_per_version` | `5000` | base cells |
| `cbss.gas.register_proxy` | `15000` | base cells |
| `cbss.gas.deregister_proxy` | `8000` | base cells |
| `cbss.gas.request_account_dkg` | `30000` | base cells |
| `cbss.gas.rotate_committee_base` | `25000` | base cells |
| `cbss.gas.rotate_committee_per_proxy` | `1000` | `0` |
| `cbss.gas.expire_dkg_pending` | `5000` | base cells |
| `cbss.gas.submit_release_receipt` | `8500` | base cells |
| `cbss.gas.submit_liveness_challenge` | `5000` | base cells |
| `cbss.gas.liveness_response` | `4000` | base cells |
| `cbss.gas.expire_liveness_challenge` | `5000` | base cells |
| `cbss.gas.slash_base` | `12000` | base cells |
| `cbss.gas.slash_invalid_partial_evidence` | `8500` | `0` |
| `cbss.gas.slash_dkg_sabotage_evidence` | `10000` | `0` |
| `cbss.gas.slash_equivocation_evidence` | `8000` | `0` |

## Preconditions

- `cargo test -p cbss-crypto --lib`
- `cargo test -p cbssd`
- `cargo test -p secrets-manager --lib`
- `cargo test -p tee-verifier --lib`
- `cargo test -p result-verifier --lib`
- `cargo test -p cowboy-execution --lib cbss`
- `cargo test -p cowboy-execution --lib runner::tee_verifier`
- `cargo test -p cowboy-rpc --lib`
- `cargo test -p cowboy-cli`
- `cbss-coverage-manifest.tsv` validates under `cbss-review.sh`
- June 2026 external crypto review report is accepted at `cowboy/docs/security/cbss-crypto-external-review-report.md`

## Rollback

If production monitoring shows a CBSS consensus, release-payment, or secret-release safety issue after activation, submit an emergency CIP-12 proposal to set `cbss.feature_enabled = false`. Existing secrets remain encrypted; new CBSS operations stop at admission while operators preserve audit logs and local shares for forensic review.

## Open Blockers

- June 2026 external crypto review is not yet delivered.
- `cbss-review.sh` still exits non-zero as of the latest local audit.
