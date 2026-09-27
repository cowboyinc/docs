# Watchtower Form 4

> **Historical implementation:** this example uses the retired on-chain/epoch-key model. It is not a conformance example for the near-chain Watchtower replacement. Do not use its key-delivery, signing, billing or automatic-ingestion behavior as the new protocol contract. Replacement examples must pass [CIP-7 acceptance](../../../docs/cips/cip-7-simple-stream-protocol.md#implementation-and-acceptance) before being advertised as current.

Historical Watchtower example.

Status: active gallery showcase. Watchtower is intentionally outside the
default gallery sweep because the offline smoke requires Python CBOR crypto
dependencies and the live path requires CBSS-capable infrastructure.

Fixture-first CIP-7 demo for an insider-trading signal feed.

The example normalizes Form 4 filings, clusters same-ticker insider buys, publishes
the resulting signal through an OPEN feed, and then exercises a SUBSCRIBER_PAID
feed where the subscriber recovers the epoch content key through CBSS partial
delivery.

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`.
- `actors/stream/main.py` is a local copy of the CIP-7 StreamActor used so this
  example can stay self-contained.
- `actors/subscriber/main.py` is an on-chain consumer that stores BUY signals.
- `tools/edgar_poller.py` loads local Form 4 fixtures by default and can
  optionally read recent SEC submissions with `--live-edgar`.
- `tools/watchtower_crypto.py` builds CIP-7 init and encrypted StreamMessage
  payloads using the same publisher-key and content-key derivations as the CLI.
- `tools/paid_consumer.py` verifies StreamMessage signatures and decrypts paid
  ciphertext once `fetch-key` returns a content key.
- `tests/` — `SimulatedChain` and tool tests, run with `uv run pytest`.
- `demo.sh` is the end-to-end runner.

## Simulate Locally

The actor tests drive both targets through
`cowboy_sdk.testing.SimulatedChain`, an in-memory host: state, events, sender
and block height, no node. The tool tests cover fixture normalization and the
paid-message crypto round trip.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire bytes,
hex-encoded; this call is `get_head()` on a fresh stream actor and expects the
CBOR-encoded zero cursor:

```bash
cowboy test --target stream --handler get_head \
  --payload 0x \
  --expect 0xa26d686561645f73657175656e6365006e666c6f6f725f73657175656e636501
```

`cowboy actor check stream` and `cowboy actor check subscriber` validate the
targets offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on
`PATH` or at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner`
in a node checkout).

## Quick Smoke

```bash
./demo.sh
```

This does not contact a node. It verifies fixture normalization, BUY signal
projection, paid message encryption, signature verification, and decryption.
`./demo.sh --offline` is equivalent and can be used when you want to be explicit.

Python dependencies: `cbor2`, `cryptography`, and `PyNaCl`.

## Run On A Network

```bash
RPC_URL=http://localhost:4000 ./demo.sh --live
```

The live path deploys the `stream` target with `cowboy actor deploy stream`
(a project deploy checks the node's actor runtime profile, so use the CLI
release that matches the network you target). It requires an
already-running local devnet with at least five
registered CBSS proxies and committed account DKG support. It does not build
binaries; set these if the defaults are not already present:

```bash
COWBOY_BIN=/path/to/cowboy \
CBSS_CIP7_PREWRAP_BIN=/path/to/cbss-cip7-prewrap \
CBSS_CIP7_UNSEAL_BIN=/path/to/cbss-cip7-unseal \
RPC_URL=http://localhost:4000 \
./demo.sh --live
```

Successful live output includes:

- `BUY_SIGNAL_OK`
- `FREE_OK`
- `PAID_OK`

`PAID_OK` is only printed after the demo fetches a real CBSS delivery bundle,
unseals it with `cbss-cip7-unseal`, publishes a ciphertext StreamMessage, and
decrypts the signal with the recovered content key.
