# Liquidation Oracle

> Monitor SEC filings and social signals for collateral health

## Use Case

DeFi lending protocols need accurate collateral valuations. Traditional oracles only track price, but real risk comes from:
- SEC enforcement actions
- Insider selling (Form 4 filings)
- Social sentiment collapse
- News events and PR crises

With Cowboy, you can build a comprehensive liquidation oracle that:
- Monitors SEC filings (8-K, 10-Q, Form 4)
- Tracks social sentiment in real-time
- Detects corporate events that affect collateral
- Provides early warning signals before price impact

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                    Liquidation Oracle                             │
│                                                                   │
│  Continuous monitoring:                                           │
│  1. Scrape SEC EDGAR for new filings                             │
│  2. Monitor social media for sentiment shifts                    │
│  3. Track news for significant events                            │
│                                                                   │
│  On detection:                                                    │
│  4. Analyze impact using AI                                      │
│  5. Calculate risk score                                         │
│  6. Alert lending protocols if risk elevated                     │
│  7. Recommend collateral adjustments                             │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **SEC monitoring**: Real-time EDGAR filing scraping
- **Insider activity**: Track Form 4 (insider buys/sells)
- **Sentiment integration**: Social media mood tracking
- **Risk scoring**: Combined risk assessment (0-100)
- **Protocol integration**: Alert lending protocols

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/oracle/main.py` - the actor
- `tests/test_oracle.py` - `SimulatedChain` tests, run with `uv run pytest`
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
cowboy test --target oracle --handler get_stats \
  --payload 0xa0 \
  --expect 0xa26a636173655f636f756e74006b6d61726b65745f6e616d6560
```

`cowboy actor check oracle` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy oracle`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not scrape EDGAR in real time, monitor market feeds,
execute liquidations, or provide production risk decisions unless that behavior
is explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Caveats

- **Not real-time**: SEC filings have delays
- **Sentiment noise**: Social media can be misleading
- **False positives**: AI may overreact to news
- **Compliance**: Ensure proper use of SEC data
