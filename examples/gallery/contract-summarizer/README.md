# Contract Summarizer

> Smart contract rug-pull detection and analysis

## Use Case

New smart contracts deploy every minute. Many are scams that:
- Contain hidden mint functions
- Have honeypot mechanics (can't sell)
- Include rug-pull backdoors
- Copy legitimate contracts with malicious additions

With Cowboy, you can build a contract analyzer that:
- Monitors new contract deployments
- Decompiles and analyzes bytecode
- Detects common scam patterns
- Generates human-readable summaries
- Alerts on suspicious contracts

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                   Contract Summarizer                             │
│                                                                   │
│  On new contract deploy:                                          │
│  1. Fetch contract bytecode and verified source (if available)   │
│  2. Decompile/analyze for function signatures                    │
│  3. Check for known scam patterns:                               │
│     - Owner-only mint functions                                  │
│     - Transfer restrictions (honeypot)                           │
│     - Proxy upgrades to malicious code                           │
│     - Hidden fee mechanisms                                       │
│  4. Generate risk assessment using AI                            │
│  5. Produce human-readable summary                               │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Pattern detection**: Known scam signatures
- **AI analysis**: Interpret complex contract logic
- **Risk scoring**: 0-100 rug-pull likelihood
- **Plain English summaries**: Understand contracts easily
- **Real-time monitoring**: Track new deploys

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/summarizer/main.py` - the actor
- `tests/test_summarizer.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`analyze_contract`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded defaults (empty `profile_name`, `analysis_count` 0):

```bash
cowboy test --target summarizer --handler get_stats \
  --payload 0xa0 \
  --expect 0xa26c70726f66696c655f6e616d65606e616e616c797369735f636f756e7400
```

`cowboy actor check summarizer` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor, submits a deterministic sample request, and reads back the stored result.

`demo.sh` deploys the target with `cowboy actor deploy summarizer`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not monitor live deployments, fetch arbitrary verified
source code, execute external auditors, or provide production security review
unless that behavior is explicitly shown in `demo.sh`; treat those capabilities
as extension points.

## Caveats

- **Not foolproof**: Sophisticated scams may evade detection
- **Source availability**: Unverified contracts harder to analyze
- **False positives**: Legitimate contracts may trigger warnings
- **Chain support**: Currently focuses on EVM chains
