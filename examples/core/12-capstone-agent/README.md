# Capstone Agent

This lesson combines the core builder path into one small product-shaped actor:
a proposal review workflow with permissions, stored state, and an async
`runner.llm` review memo.

## What It Covers

- proposal submission and indexed state
- owner/admin-gated approval and rejection
- `runner.llm(...)` inside a continuation
- `capture()` for proposal identity across the await boundary
- reading proposal records and aggregate stats

## Flow

1. Deploy the `reviewer` target; the deploy runs `init`, which records the deployer as owner.
2. Submit a proposal.
3. Request an LLM review memo.
4. Wait for the runner callback.
5. Read the proposal and review status.
6. Approve the proposal as the owner.
7. Read final stats.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/reviewer/main.py` - the actor
- `tests/test_reviewer.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
continuation is exercised up to its dispatch (the job it sends to the runner)
and its result step is called directly with a stubbed result.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `submit_proposal({"title": "Grants"})` on a fresh actor and expects the
CBOR-encoded reply:

```bash
cowboy test --target reviewer --handler submit_proposal \
  --payload 0xa1657469746c65664772616e7473 \
  --expect 0xa266737461747573697375626d69747465646b70726f706f73616c5f696401
```

`cowboy actor check reviewer` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy reviewer`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Why This Example Exists

The earlier lessons teach the parts. This capstone shows how those parts fit
together into something a developer could adapt into a useful agent workflow.
