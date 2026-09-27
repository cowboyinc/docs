# Runner LLM

This is the canonical small `runner.llm` lesson in the core curriculum. It
shows how an actor submits an LLM job, captures local callback state, resumes
after the runner result, extracts model text, stores the response, and exposes
query handlers for clients.

## What It Covers

- `init`, which claims ownership with `runtime.assume_ownership_if_unowned()`
- one public trigger: `ask_llm`, `@public` outside `@runner.continuation`
- `runner.llm(...)` prompt and system prompt usage
- `capture()` for state that must survive the await boundary
- `result.text` — the completion off the `RunnerResult` the await yields, with no
  hand-written unwrapping
- one generated callback entrypoint: `ask_llm__resume`
- query methods `get_response` / `get_config`, `@public(decode="cbor")` so the
  payload arrives decoded and the return is encoded on the way out

## Flow

1. `ask_llm` increments `request_count`, stores a pending record, and calls `runner.llm(...)`.
2. The runner executes the LLM call off-chain and returns the result.
3. The continuation resumes, extracts the text, and stores it under `llm:{id}:response`.
4. `get_response` returns the latest response or a specific one by `request_id`.
5. `get_config` returns current LLM settings.

## Why This Example Exists

- Demonstrates the full `@runner.continuation` + `capture()` + `runner.llm(...)` pattern.
- Shows indexed storage keys (`llm:{id}:response`, `llm:{id}:status`) for unbounded growing state.
- Shows event emission (`llm.init`, `llm.response`, `llm.read`) for observability.
- Safe for demo: no external dependencies beyond the runner LLM executor.

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/assistant/main.py` — actor code
- `tests/test_assistant.py` — `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` — deploy, ask the LLM, wait for runner callback, read back

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`ask_llm`: the pending `llm:{id}:*` keys, `request_count`, and the `llm` job
dispatched to the runner (visible in `mock_host.get_send_log()`), plus the
read handlers.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_config({})` on a fresh actor and expects
the CBOR-encoded defaults:

```bash
cowboy test --target assistant --handler get_config \
  --payload 0xa0 \
  --expect 0xa46a6d61785f746f6b656e731901006a6d6f64656c5f6e616d65656c6c616d616b74656d7065726174757265fb3fc999999999999a6d726571756573745f636f756e7400
```

`cowboy actor check assistant` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy assistant`; the deploy
runs `init`. A project deploy checks the node's actor runtime profile, so use
the CLI release that matches the network you target. It needs a healthy
runner with LLM capability.

## Caveats

- Requires a healthy runner with LLM capability (`runner.llm` implemented).
- `cowboy actor deploy` invokes `init` in the deploy transaction, so `init` is
  `@public` and returns `ok: already initialized` on any later call rather
  than zeroing `request_count`.
- `ask_llm` decodes its payload by hand: it declares `@public(decode="raw")`,
  the only codec a continuation may take. The compiler generates
  `ask_llm__resume` alongside it and the generated half carries no decode tag,
  so `decode="cbor"` would cover only one side of the pair — the node refuses it
  at class creation.
- Responses are stored forever under indexed keys — no pruning.
