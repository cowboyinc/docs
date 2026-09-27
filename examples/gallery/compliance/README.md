# Compliance Agent

> Real-time sanctions screening (OFAC, UN, EU)

## Use Case

Financial services must screen transactions against sanctions lists. Compliance requires:
- OFAC SDN list checking
- UN Security Council sanctions
- EU consolidated list
- UK HM Treasury sanctions
- Real-time screening of all transactions

With Cowboy, you can build an autonomous compliance agent that:
- Maintains up-to-date sanctions lists
- Screens addresses and names in real-time
- Flags potential matches for review
- Records all screening decisions on-chain

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                    Compliance Agent                               │
│                                                                   │
│  On screening request:                                            │
│  1. Receive address/name to screen                               │
│  2. Check against local sanctions cache                          │
│  3. If potential match, verify with AI                           │
│  4. Return: clear, flagged, or blocked                           │
│  5. Record decision with reasoning                               │
│                                                                   │
│  Periodic updates:                                                │
│  - Fetch latest OFAC SDN list                                    │
│  - Update EU/UN sanctions                                        │
│  - Refresh blocklist                                             │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Multi-list**: OFAC, UN, EU, UK sanctions
- **Real-time**: Sub-second screening
- **AI verification**: Reduce false positives
- **Audit trail**: All decisions recorded on-chain
- **Auto-updates**: Lists refreshed regularly

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/screener/main.py` - the actor
- `tests/test_screener.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`screen_entity`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion. A query that matches no list entry is decided on-chain and never reaches
the runner, so that path is asserted end to end.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `screen_entity({"query": "0xcafe"})` on a fresh
actor; nothing on the demo lists matches, so it expects the CBOR-encoded
`clear` screen:

```bash
cowboy test --target screener --handler screen_entity \
  --payload 0xa165717565727966307863616665 \
  --expect 0xa4657175657279663078636166656673746174757365636c656172676d617463686573806973637265656e5f696401
```

`cowboy actor check screener` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor, submits a deterministic sample request, and reads back the stored result.

`demo.sh` deploys the target with `cowboy actor deploy screener`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not refresh live sanctions lists, provide legal
compliance coverage, perform real-time monitoring, or enforce production blocks
unless that behavior is explicitly shown in `demo.sh`; treat those capabilities
as extension points.

## Caveats

- **Not legal advice**: Consult compliance counsel
- **List delays**: Sanctions lists update with delay
- **False positives**: Common names may match
- **Jurisdiction**: Different rules apply in different regions
