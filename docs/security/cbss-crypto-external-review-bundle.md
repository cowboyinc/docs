# CBSS Crypto External Review Bundle

Status: prepared for June 2026 reviewer handoff; not an accepted review report.

The reproducible bundle command is tracked at the workspace root:

```bash
./cbss-crypto-review-bundle.sh
```

Default output:

```text
cbss-crypto-external-review-bundle.tar.gz
```

The tarball stages files under `cbss-crypto-external-review/` and includes:

- `runner/crates/cbss-crypto`, including `vetted_dkg.rs`, IBE, threshold,
  reshare, HPKE, and share-storage code.
- Production cbssd DKG and release integration surfaces:
  `commonware_dkg.rs`, `dkg_driver.rs`, `orchestrator.rs`, `partial_sign.rs`,
  `protocol.rs`, `storage.rs`, `chain_watcher.rs`, and `receipt_submitter.rs`.
- Runner threshold client and combiner integration.
- Chain CBSS type and execution handlers.
- CIP-24, implementation plan, coverage manifest, review log, completion audit,
  packet, and report template.
- A generated `README.md` with workspace, runner, node, and docs commit hashes
  plus local `cbss-review.sh` JSON output.

The bundle is a convenience artifact for the external reviewer. The only
accepted pre-mainnet gate artifact is the completed report at:

```text
cowboy/docs/security/cbss-crypto-external-review-report.md
```

Do not create that final report path with placeholders. Keep placeholders in
`cbss-crypto-external-review-report.template.md` until the reviewer delivers an
accepted report.
