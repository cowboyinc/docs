# DAO Copilot

> AI-powered Snapshot proposal analyzer and voting assistant

## Use Case

DAO governance is overwhelming. Active participants need to:
- Read dozens of proposals per week
- Understand technical implications
- Assess community sentiment
- Vote across multiple DAOs

With Cowboy, you can build a DAO copilot that:
- Monitors Snapshot spaces for new proposals
- Analyzes proposals using AI
- Summarizes key points and risks
- Provides voting recommendations
- Optionally auto-votes based on principles

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                       DAO Copilot                                 │
│                                                                   │
│  On new proposal:                                                 │
│  1. Fetch proposal from Snapshot                                 │
│  2. Analyze with AI for:                                         │
│     - Summary of changes                                         │
│     - Financial implications                                     │
│     - Security considerations                                    │
│     - Alignment with DAO values                                  │
│  3. Generate voting recommendation                               │
│  4. (Optional) Execute vote                                      │
│  5. Record analysis on-chain                                     │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Multi-DAO**: Monitor multiple Snapshot spaces
- **AI analysis**: Comprehensive proposal breakdown
- **Risk assessment**: Security and financial implications
- **Principle alignment**: Check against your stated values
- **Auto-voting**: Optional delegated voting

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/copilot/main.py` - the actor
- `tests/test_copilot.py` - `SimulatedChain` tests, run with `uv run pytest`
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
cowboy test --target copilot --handler get_stats \
  --payload 0xa0 \
  --expect 0xa26864616f5f6e616d65606e70726f706f73616c5f636f756e7400
```

`cowboy actor check copilot` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy copilot`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not perform live governance indexing, wallet execution,
multi-runner consensus, or production enforcement unless that behavior is
explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Caveats

- **AI limitations**: May miss nuances in complex proposals
- **Not financial advice**: Do your own research
- **Auto-voting risk**: Automated votes can be wrong
- **API rate limits**: Snapshot has usage limits
