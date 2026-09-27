# Message Ring

> Chain of actors passing a message around a ring via deferred transactions

## Use Case

The simplest demonstration of **actor-to-actor messaging** on Cowboy. A set
of actors are deployed and wired into a ring. When the first actor receives
a "start" signal, it sends a message to the next actor, which forwards it
to the next, and so on — the message circulates indefinitely, one hop per
block.

This exercises the core deferred transaction pipeline:

- `send_message` creates a deferred transaction delivered in the next block
- Each hop is a separate on-chain transaction with its own execution context
- The gas pool from the original transaction is shared across the full chain

## How It Works

```
           start_ring
               │
               ▼
  ┌─────────────────────────┐
  │       Actor 0           │
  │  send_message ──────────┼──► Actor 1
  └─────────────────────────┘       │
               ▲                    ▼
          Actor 4              Actor 2
               ▲                    │
               │                    ▼
          Actor 3 ◄────────────────┘

  One hop per block. Message circulates forever.
```

Each actor:

1. Receives a JSON payload via `handle_message`
2. Increments the `hop` counter
3. Emits a `ring_hop` event (on-chain audit trail)
4. Calls `send_message` to forward the payload to the next actor

## Key Features

- **Zero off-chain infrastructure**: no runners, no timers — pure actor messaging
- **Deferred transaction chaining**: each hop is a new deferred tx in the next block
- **Shared gas pool**: one originating transaction funds the entire chain of hops
- **On-chain audit trail**: every hop emits an event with the hop count and message

## Quick Start

### Automated

```bash
cd examples/gallery/advanced-messaging-ring
./deploy_ring.sh https://rpc.mesa.cowboylabs.net 5
```

The script deploys N actors, wires them into a ring, starts the message,
and checks propagation.

### Manual

```bash
# 1. Init wallet
cowboy init dev --rpc-url "${RPC_URL:-http://localhost:5000}"

# 2. Deploy actors (from this directory; the CLI discovers the `ring`
#    target from cowboy.toml)
cowboy actor deploy ring --salt $(echo -n "ring-0" | xxd -p) \
    --cycles-limit 2000000 --cells-limit 2000000

# 3. Configure each actor with its neighbor's address
cowboy actor execute --actor <ACTOR_0> --handler configure \
    --payload-json '{"next_actor":"<ACTOR_1_ADDR>"}' \
    --cycles-limit 2000000 --cells-limit 2000000

# 4. Start the ring
cowboy actor execute --actor <ACTOR_0> --handler start_ring \
    --payload-json '{"msg":"hello ring"}' \
    --cycles-limit 2000000 --cells-limit 2000000

# 5. Watch it propagate
cowboy actor logs --address <ACTOR_0>
cowboy actor logs --address <ACTOR_1>
```

## Architecture

### Storage Layout

| Key            | Type    | Description                        |
|----------------|---------|------------------------------------|
| `next_actor`   | bytes   | 20-byte address of the next actor  |
| `hop_count`    | u64 BE  | Number of messages this actor has forwarded |

### Handlers

| Handler          | Called by        | Description                         |
|------------------|------------------|-------------------------------------|
| `configure`      | Deployer (tx)    | Set the next actor in the ring      |
| `start_ring`     | Deployer (tx)    | Inject the initial message          |
| `handle_message` | Deferred tx      | Forward message to next actor       |

### Events

| Topic          | Data                                    |
|----------------|-----------------------------------------|
| `configured`   | `{"next": "<address>"}`                 |
| `ring_started` | `{"msg": "<message>"}`                  |
| `ring_hop`     | `{"hop": N, "local_count": N, "msg": "..."}` |

## Caveats

- **Gas pool limits**: the ring will stop circulating when the shared gas pool
  from the originating transaction is exhausted. Use higher `--cycles-limit`
  and `--cells-limit` for longer runs.
- **One hop per block**: deferred transactions are delivered in the next block,
  so the message advances one actor per block (~300ms on devnet).
- **No duplicate suppression**: if `start_ring` is called multiple times,
  multiple messages will circulate simultaneously.

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/ring/main.py` — the ring node actor
- `actors/hello/main.py` — minimal counter class (smallest actor-shaped module)
- `actors/feed-subscriber/main.py` — example consumer for Watchtower-style feeds
- `tests/` — `SimulatedChain` tests, run with `uv run pytest`
- `deploy_ring.sh` — automated deploy and test script

## Simulate Locally

The tests drive the ring actor through `cowboy_sdk.testing.SimulatedChain`,
an in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_state({})` on a fresh actor and expects
the CBOR-encoded unconfigured state:

```bash
cowboy test --target ring --handler get_state \
  --payload 0xa0 \
  --expect 0xa269686f705f636f756e74006a6e6578745f6163746f7260
```

`cowboy actor check ring` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./deploy_ring.sh https://rpc.mesa.cowboylabs.net 5
```

`deploy_ring.sh` deploys N copies of the `ring` target with
`cowboy actor deploy ring`, wires them into a ring, starts the message, and
checks propagation. A project deploy checks the node's actor runtime profile,
so use the CLI release that matches the network you target.
