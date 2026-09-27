# Failure Recovery Example

This async example intentionally demonstrates pending, failed, retried, and recovered states.

## What It Covers

- `submit_request`
- terminal request state transitions
- `retry_request`
- explicit `recovered` state
- `get_request` and `get_status`

## Flow

1. Submit a request and record `pending`.
2. The Runner continuation resumes and writes the terminal request state.
3. `retry_request` creates a linked retry attempt.
4. A later successful attempt can be marked `recovered`.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/recovery/main.py` - the actor
- `tests/test_recovery.py` - `SimulatedChain` tests, run with `uv run pytest`
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
cowboy test --target recovery --handler get_status \
  --payload 0xa0 \
  --expect 0xa26d726571756573745f636f756e74006f6e6578745f726571756573745f696401
```

`cowboy actor check recovery` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy recovery`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Why This Example Exists

- Users need an operationally safe async reference.
- It shows status transitions explicitly instead of hiding failure paths.

## Caveats

- The example focuses on state transitions, not on a specific external service.
- Retries are new requests linked by `previous_request_id`.
