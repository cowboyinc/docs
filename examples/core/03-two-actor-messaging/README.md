# Two-Actor Messaging

This lesson is the first actor composition example in the curriculum. It keeps
the topology small: one sender actor accepts a task and sends a deferred message
to one worker actor.

## What It Covers

- configuring one actor with another actor's address
- using `runtime.send_message(...)` for deferred actor-to-actor work
- recording received messages in the target actor
- reading worker state after the deferred message settles

## Flow

1. Deploy the `worker` and `sender` targets; each deploy runs the actor's `init`.
2. Configure the sender with the worker address.
3. Call `send_task` on the sender.
4. Wait for the deferred message.
5. Read `get_tasks` from the worker.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/sender/main.py` - the sender actor
- `actors/worker/main.py` - the worker actor
- `tests/test_sender.py`, `tests/test_worker.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `handle_message({"task_id": 7, "title": "Prepare launch
checklist"})` on a fresh worker and expects the CBOR-encoded reply:

```bash
cowboy test --target worker --handler handle_message \
  --payload 0xa2657469746c65781850726570617265206c61756e636820636865636b6c697374677461736b5f696407 \
  --expect 0xa2667374617475736768616e646c6564677461736b5f696407
```

`cowboy actor check worker` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys both targets with `cowboy actor deploy worker` and
`cowboy actor deploy sender`. The sender's `send_task` is deferred, so the
demo waits before reading the worker. A project deploy checks the node's actor
runtime profile, so use the CLI release that matches the network you target.

## Why This Example Exists

The ring demo is a useful visualization, but it is too abstract as the first
messaging lesson. This example teaches the smallest practical coordination
pattern: actor A asks actor B to do work.
