# Sentiment Analysis Agent

> Track public sentiment on assets, brands, or topics in real-time

## Use Case

Market sentiment drives prices. Understanding public opinion helps with:
- Trading decisions (buy the fear, sell the greed)
- Brand monitoring (PR crisis detection)
- Product feedback analysis
- Political sentiment tracking

Traditional sentiment analysis requires:
- NLP models and training data
- Social media API subscriptions
- Real-time streaming infrastructure
- Dashboard and alerting systems

With Cowboy, you can build an autonomous sentiment tracker that:
- Scrapes social media and news sources
- Analyzes sentiment using LLMs
- Tracks sentiment over time
- Alerts on significant changes
- Runs 24/7 without infrastructure

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                  Sentiment Analysis Agent                        │
│                                                                  │
│  Every interval:                                                 │
│  1. Submit scraping task to runners                              │
│  2. Runners fetch: Twitter, Reddit, News, Discord                │
│  3. Return raw text samples                                      │
│                                                                  │
│  For each batch:                                                 │
│  4. Submit to AI runners for sentiment analysis                  │
│  5. AI returns: {sentiment: -1 to 1, confidence, themes}         │
│  6. Update rolling average                                       │
│  7. Check for alert conditions                                   │
│  8. Record for historical analysis                               │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Multi-source**: Twitter, Reddit, Discord, news sites
- **LLM-powered**: Understands context, sarcasm, nuance
- **Trend detection**: Identifies emerging themes
- **Alerting**: Notify on sentiment shifts
- **Historical tracking**: On-chain sentiment history

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/analyzer/main.py` - the actor
- `tests/test_analyzer.py` - `SimulatedChain` tests, run with `uv run pytest`
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
cowboy test --target analyzer --handler get_stats \
  --payload 0xa0 \
  --expect 0xa365746f706963606d726571756573745f636f756e74006f616c6572745f7468726573686f6c643831
```

`cowboy actor check analyzer` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy analyzer`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not scrape social platforms, run 24/7 ingestion, stream
live sentiment, or call production analytics APIs unless that behavior is
explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `topic` | Asset/brand/topic to track | Required |
| `sources` | Social platforms to scrape | ["twitter", "reddit"] |
| `check_interval_blocks` | Blocks between checks | 300 |
| `alert_threshold` | Sentiment change for alert | 0.3 |

## Caveats

- **Bot activity**: Social media has bots that skew sentiment
- **Sarcasm**: AI may misinterpret ironic posts
- **API limits**: Social platforms rate-limit scraping
- **Lag**: Sentiment is backward-looking, not predictive
- **Not financial advice**: Sentiment doesn't guarantee price movement
