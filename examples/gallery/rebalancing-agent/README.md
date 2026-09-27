# Rebalancing Agent

This gallery demo shows a deterministic portfolio drift analyzer. It is an
applied example, not a primitive lesson; use the core curriculum first if you
want the building blocks.

## What It Covers

- target pool configuration
- drift calculation in basis points
- threshold-based rebalance recommendation
- stored decision state and stats queries

## Flow

1. `init` stores the pool address and drift threshold.
2. `check_rebalance` receives current and average ratios in the payload.
3. The actor calculates drift, chooses `hold`, `increase_eth`, or `decrease_eth`, and stores the decision.
4. `get_last_decision` and `get_stats` expose the latest state.

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/rebalancer/main.py` — the full drift analyzer (the target `demo.sh` deploys)
- `actors/simple_rebalancer/main.py` — a minimal variant of the same logic
- `tests/` — `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh`

## Simulate Locally

The tests drive both agents through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_last_decision({})` on a fresh
rebalancer and expects the CBOR-encoded idle decision:

```bash
cowboy test --target rebalancer --handler get_last_decision \
  --payload 0xa0 \
  --expect 0xa1667374617475736469646c65
```

`cowboy actor check rebalancer` (and `simple_rebalancer`) validates a target
offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or
at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a
node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor with `cowboy actor deploy rebalancer` (the
portfolio config rides on the deploy's atomic `init`), submits deterministic
portfolio ratios, and reads back the stored rebalance recommendation. A
project deploy checks the node's actor runtime profile, so use the CLI
release that matches the network you target.

## Caveats

- This runnable version is deterministic and does not fetch prices itself.
- The demo does not execute swaps; it stores a recommendation that another actor or client could act on.
