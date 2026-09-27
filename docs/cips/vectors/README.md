# Watchtower interoperability fixtures

`cip7-watchtower-v1.json` pins the replacement CIP-7 message and usage signing bytes/signatures, a three-leaf unpadded Merkle tree with all inclusion paths, HKDF and fixed-point guard cases. The distinct publisher and usage signing seeds are public test data. A usage receipt MUST verify under the usage key and fail under the publisher key.

Run `python3 scripts/check_watchtower_vectors.py` from the repository root with `cbor2==5.6.5` and `cryptography==44.0.3` installed. The Script Tests CI workflow runs this check on script or vector changes. Fixture regeneration requires explicit `--write` and review of the resulting bytes. This helper independently consumes the three proof shapes after reconstruction; it is not a production implementation or proof of service conformance.

Rust, external Python and PVM consumers must use the same fixture. Encrypted-body interoperability and negative acceptance cases in the CIP remain required implementation tests; this fixture does not claim those runtimes have been updated.
