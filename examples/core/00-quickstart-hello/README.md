# Quickstart Hello

The smallest deployable Cowboy actor in the curriculum, laid out as a
`cowboy init` project with one target.

## What It Covers

- one actor class
- `init`
- one mutating handler: `say_hello`
- one read handler: `get_greeting`
- module-level PVM entrypoints

## Flow

1. Deploy the `hello` target; the deploy runs `init` with the greeting.
2. Call `say_hello` with a name.
3. Read the stored greeting back with `get_greeting`.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/hello/main.py` - the actor
- `tests/test_hello.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `say_hello({"name": "builder"})` on a fresh
actor and expects the CBOR-encoded reply:

```bash
cowboy test --target hello --handler say_hello \
  --payload 0xa1646e616d65676275696c646572 \
  --expect 0xa3646e616d65676275696c646572676d6573736167656e68656c6c6f2c206275696c646572686772656574696e676568656c6c6f
```

`cowboy actor check hello` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy hello`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.
