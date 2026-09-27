# Spam Detection Agent

> AI-powered spam filtering for email, Discord, and social media

## Use Case

Spam detection is a constant arms race. Traditional spam filters use static rules that spammers quickly learn to evade. AI-powered detection adapts to new patterns but requires:
- Cloud infrastructure for ML models
- Training data pipelines
- Monitoring and retraining cycles
- API subscriptions

With Cowboy, you can deploy an autonomous spam detection agent that:
- Analyzes incoming messages using LLMs
- Classifies spam vs legitimate content
- Learns from false positives/negatives
- Runs 24/7 without infrastructure management

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                    Spam Detection Agent                          │
│                                                                  │
│  On new message:                                                 │
│  1. Receive message via actor mailbox                            │
│  2. Submit to AI runners for classification                      │
│  3. Runners return: {is_spam: bool, confidence: 0-100, reason}   │
│  4. Store score, label, recommended action, and reasons          │
│  5. Expose latest case and aggregate stats for review            │
│  6. Emit an auditable classification event                       │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Runner-backed classification**: an LLM runner scores each message
- **Confidence scoring**: High-confidence spam auto-quarantined
- **Audit trail**: All decisions recorded on-chain
- **Feedback loop**: False positives/negatives improve classification
- **Multi-platform**: Email, Discord, Telegram, Twitter

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/detector/main.py` - the actor
- `tests/test_detector.py` - `SimulatedChain` tests, run with `uv run pytest`
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
cowboy test --target detector --handler get_stats \
  --payload 0xa0 \
  --expect 0xa26d6d6573736167655f636f756e74006e7370616d5f7468726573686f6c641846
```

`cowboy actor check detector` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy detector`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not run 24/7 ingestion, auto-quarantine live content,
call production moderation APIs, or verify multi-runner consensus unless that
behavior is explicitly shown in `demo.sh`; treat those capabilities as extension
points.

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `quarantine_threshold` | Auto-quarantine confidence | 80% |
| `flag_threshold` | Flag for review confidence | 50% |
| `num_runners` | AI runners for consensus | 3 |

## Caveats

- **False positives**: Legitimate messages may be flagged
- **Evolving threats**: New spam patterns require prompt updates
- **Privacy**: Message content sent to AI runners
- **Latency**: AI analysis adds ~1-2 seconds per message
