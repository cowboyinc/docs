# Async Cache Refresh

This cache example shows the pattern where external data is fetched asynchronously once, normalized, cached on-chain, and served synchronously afterward.

## What It Covers

- `request_refresh`
- async fetch via generated continuation resume
- deterministic normalize step
- `get_cached_record`
- `get_status`

## Flow

1. `request_refresh` marks the cache as pending.
2. The generated continuation resume stores a normalized record.
3. Later reads use `get_cached_record` and do not call the Runner again.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/cache/main.py` - the actor
- `tests/test_cache.py` - `SimulatedChain` tests, run with `uv run pytest`
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
bytes, hex-encoded; this call is `get_status({})` on a fresh actor and expects the
CBOR-encoded reply:

```bash
cowboy test --target cache --handler get_status \
  --payload 0xa0 \
  --expect 0xa2667374617475736469646c656d726566726573685f636f756e7400
```

`cowboy actor check cache` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy cache`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Why This Example Exists

- It is a common real-world pattern.
- It differs from chat and agent demos by emphasizing cache reuse and cheap reads.

## Caveats

- The normalized record is intentionally small and stable.
- This is an async cache refresh example, not a streaming oracle.
