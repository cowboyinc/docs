# Timers And Automation

This timer example is a timer-only counter with no LLM and no Runner dependency.

## What It Covers

- initialize interval
- `runtime.schedule_after(delay_blocks, "on_timer", payload)` — arm one fire,
  `delay_blocks` from now, routed to a named handler
- `@timer` — declare `on_timer` as the fire handler: deferred mode, so it can
  re-arm, and no `@public`, so only a fire reaches it
- `get_state` returns the current count and next timer metadata

## Flow

1. `init` stores `interval_blocks`.
2. `start` schedules the first timer with `runtime.schedule_after(...)`.
3. `on_timer` increments the count and schedules the next tick.
4. `get_state` exposes the count and next timer state.

## Why This Example Exists

- The existing alerts example mixes timers with AI analysis.
- This is the minimal timer copy-paste reference.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/ticker/main.py` - the actor
- `tests/test_ticker.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, timers and block height, no node.
`chain.advance_block(n)` fires due timers into `on_timer`, so the re-arm loop
runs to `max_ticks` without a network.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_state({})` on a fresh actor and expects
the CBOR-encoded defaults (`interval_blocks` 10, `max_ticks` 1, count 0):

```bash
cowboy test --target ticker --handler get_state \
  --payload 0xa0 \
  --expect 0xa565636f756e74006773746172746564f4696d61785f7469636b73016a6e6578745f74696d6572a06f696e74657276616c5f626c6f636b730a
```

`cowboy actor check ticker` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy ticker`; the deploy
runs `init`. A project deploy checks the node's actor runtime profile, so use
the CLI release that matches the network you target.

## Caveats

- Scheduling needs the `timer.schedule` entitlement — without it `start` fails rather than silently running without a clock.
- `cowboy actor deploy` invokes `init` in the deploy transaction, so `init` is `@public` and returns `already_initialized` on any later call rather than zeroing a count the loop may already have advanced. `start` is the handler that re-reads `interval_blocks`.
- Timers are one-shot. The cadence comes from `on_timer` arming the next fire, which is why the re-arm is visible in the handler instead of hidden behind a "recurring" flag.
- `fee_payer` may only be the actor or the transaction sender. `start` bills the first fire to its caller; every re-arm runs with the actor as sender, so the rest are actor-funded. Keep the actor topped up or the loop stops.
- The payload a fire delivers is not the payload that was scheduled: the node re-serializes the routing envelope's inner bytes as a JSON list of byte values. `_timer_payload` decodes both that form and the CBOR a direct call brings; the SDK has no decoder for it yet.
- This example is about the timer pattern, not business logic.
