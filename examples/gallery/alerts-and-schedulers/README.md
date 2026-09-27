# Alerts And Schedulers

This gallery demo combines scheduled checks with runner analysis. For the
minimal timer-only pattern, start with
[07-timers-and-automation](../../core/07-timers-and-automation/).

## What It Covers

- check configuration
- timer scheduling with `runtime.schedule_after(...)`
- runner-backed analysis of a metric observation
- alert count and next timer metadata

## Flow

1. `init` stores a check name, threshold, and interval.
2. `run_check` records a check request and invokes an LLM runner continuation.
3. The continuation resumes, normalizes the result, stores the report, and schedules the next check while `max_auto_checks` remains.
4. `get_last_check` and `get_stats` expose the current scheduler state.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/scheduler/main.py` - the actor
- `tests/test_scheduler.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`run_check`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion. `chain.advance_block(n)` fires the timer `schedule_next_check` arms into
`run_check`, so the scheduling loop runs without a network.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded defaults (threshold 42, interval 25, zero counters):

```bash
cowboy test --target scheduler --handler get_stats \
  --payload 0xa0 \
  --expect 0xa7697468726573686f6c64182a6a636865636b5f6e616d65606b616c6572745f636f756e74006b636865636b5f636f756e74006d6e6578745f74696d65725f6964606f696e74657276616c5f626c6f636b7318196f6d61785f6175746f5f636865636b7300
```

`cowboy actor check scheduler` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the scheduler actor, runs one check with deterministic input,
and reads back the latest check plus scheduler stats.

`demo.sh` deploys the target with `cowboy actor deploy scheduler`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Caveats

- This example intentionally mixes timers and runner analysis; it is not the simplest timer reference.
- API keys and external credentials should stay in runner infrastructure, not actor storage.
