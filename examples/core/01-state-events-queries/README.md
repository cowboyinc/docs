# State, Events, And Queries

This lesson isolates the basic actor loop after the hello example: mutate state,
emit an event, and read state back through a query handler.

## What It Covers

- incrementing stored state
- storing the last operation
- event emission for auditability
- read-only query handlers

## Flow

1. Deploy the `counter` target; the deploy runs `init`, which sets the counter.
2. `increment` adds a caller-provided amount and emits `counter.incremented`.
3. `get_state` returns the current count and last operation.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/counter/main.py` - the actor
- `tests/test_counter.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `increment({"amount": 5})` on a fresh
actor (count starts at 0) and expects the CBOR-encoded reply:

```bash
cowboy test --target counter --handler increment \
  --payload 0xa166616d6f756e7405 \
  --expect 0xa265636f756e740566616d6f756e7405
```

`cowboy actor check counter` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy counter --init-payload
'{"start":2}'`. A project deploy checks the node's actor runtime profile, so
use the CLI release that matches the network you target.
