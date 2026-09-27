# Minimal Runner Continuation Example

This continuation example shows the smallest async Runner pattern that is still deployable as a teaching reference.

## What It Covers

- `init`
- one public trigger: `refresh`
- one continuation helper: `_run_refresh_job`
- one generated callback entrypoint: `_run_refresh_job__resume`
- one query method: `get_status`

## Flow

1. `refresh` creates a pending request.
2. `_run_refresh_job` builds the task definition and callback metadata.
3. Runner calls the generated `_run_refresh_job__resume` handler to resume the continuation.
4. `get_status` returns the last request and callback state.

## Resume Handler Note

- `@runner.continuation` generates the actor-class `__resume` method automatically.
- You put your resume logic in the async method after the `await`, or in helpers it calls.
- The module-level `_run_refresh_job__resume(payload)` function is only a PVM entrypoint shim that forwards to the generated actor method.

## Why This Example Exists

- The older async examples are domain-heavy.
- Users need a tiny continuation and callback example they can copy safely.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/continuation/main.py` - the actor
- `tests/test_continuation.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous halves:
`refresh` records the pending request and dispatches the `http` job (visible
in `mock_host.get_send_log()`), and `_finalize_refresh` is called directly
with a `RunnerResult` to check the done record.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `init({"source_url":"https://example.com/a"})`
on a fresh actor and expects the CBOR-encoded `initialized` reply:

```bash
cowboy test --target continuation --handler init \
  --payload 0x7b22736f757263655f75726c223a2268747470733a2f2f6578616d706c652e636f6d2f61227d \
  --expect 0xa2667374617475736b696e697469616c697a65646a736f757263655f75726c7568747470733a2f2f6578616d706c652e636f6d2f61
```

`cowboy actor check continuation` validates the target offline. `cowboy test`
and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy continuation`; the
deploy runs `init`. A project deploy checks the node's actor runtime profile,
so use the CLI release that matches the network you target.

## Caveats

- The example uses a minimal HTTP-shaped task so the continuation mechanics stay obvious.
- It intentionally avoids LLM prompt handling; use [09-runner-llm](../09-runner-llm/) for the dedicated `runner.llm(...)` lesson.
