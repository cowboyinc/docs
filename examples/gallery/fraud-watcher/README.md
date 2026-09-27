# Fraud Watcher

> AML transaction classification and monitoring

## Use Case

Anti-Money Laundering (AML) requires monitoring transaction patterns for:
- Structuring (breaking up transactions to avoid reporting)
- Layering (complex transaction chains to obscure origins)
- Unusual activity patterns
- High-risk transaction flows

With Cowboy, you can build an autonomous fraud watcher that:
- Monitors transaction streams in real-time
- Classifies transactions using AI pattern detection
- Flags suspicious activity
- Generates Suspicious Activity Reports (SARs)

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                      Fraud Watcher                                │
│                                                                   │
│  On each transaction:                                             │
│  1. Extract transaction features                                 │
│  2. Check against known patterns (structuring, etc.)             │
│  3. Calculate risk score using AI                                │
│  4. Flag if risk exceeds threshold                               │
│  5. Record for audit trail                                       │
│                                                                   │
│  Periodic analysis:                                               │
│  - Cluster related transactions                                  │
│  - Detect layering patterns                                      │
│  - Generate SAR recommendations                                  │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Pattern detection**: Structuring, layering, velocity
- **AI classification**: ML-based risk scoring
- **SAR generation**: Automated suspicious activity reports
- **Real-time alerts**: Immediate notification on high-risk
- **Audit trail**: All decisions recorded on-chain

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/watcher/main.py` - the actor
- `tests/test_watcher.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded default stats:

```bash
cowboy test --target watcher --handler get_stats \
  --payload 0xa0 \
  --expect 0xa36a636173655f636f756e74006d666c61676765645f636f756e74006e7269736b5f7468726573686f6c641846
```

`cowboy actor check watcher` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy watcher`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not monitor live transaction streams, deliver real-time
alerts, connect to fraud databases, or enforce production decisions unless that
behavior is explicitly shown in `demo.sh`; treat those capabilities as extension
points.

## Caveats

- **Not legal advice**: Consult AML compliance experts
- **False positives**: High sensitivity means more false flags
- **Jurisdiction**: AML requirements vary by region
- **Privacy**: Balance monitoring with user privacy
