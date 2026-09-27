# Token Escrow

This escrow example shows a practical business flow with real CIP-20-backed custody, not just state transitions.

## Actors

- `buyer` (`actors/buyer/main.py`)
- `seller` (`actors/seller/main.py`)
- `escrow` (`actors/escrow/main.py`)

## Lifecycle

1. Buyer actor receives CIP-20 funds and calls `approve_escrow`.
2. The escrow actor records a `deposit` for that buyer actor.
3. Escrow pulls tokens into its own address with `token_transfer_from`.
4. Escrow locks the trade for the recorded seller actor.
5. Escrow calls `release` to transfer funds to the seller actor.
6. Escrow may call `cancel` before the trade is locked, which refunds the buyer actor.

## What It Teaches

- actor-to-actor escrow coordination with CIP-20 approval and custody
- deposit, lock, release, and cancel transitions backed by actual token balances
- actor-held balances, escrow custody, and release/refund flows
- a single authoritative escrow state machine that is also the on-chain custodian

## Why CIP-20

This example is intentionally **CIP-20 only**. Native `CBY` is not modeled here because the clearest working escrow pattern in the repo is `approve` plus `token_transfer_from` on fungible tokens. The walkthrough therefore teaches explicit token custody instead of pretending native balance semantics work the same way.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/escrow/main.py` - `EscrowActor`, the custodian and state machine
- `actors/buyer/main.py` - `BuyerActor`, approves and deposits into escrow
- `actors/seller/main.py` - `SellerActor`, locks and confirms delivery
- `tests/test_escrow.py`, `tests/test_buyer.py`, `tests/test_seller.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive each actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The mock host
has no Token Registry, so the escrow and buyer tests install small stand-ins
for the `token_*` host functions and assert on the custody moves they record;
the buyer and seller tests answer their `call()` into escrow with a `CallMock`.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire bytes,
hex-encoded; this call is `configure` on a fresh `escrow` actor with a token id
of `0x77...77` and expects the CBOR-encoded `configured` reply:

```bash
cowboy test --target escrow --handler configure \
  --payload 0xa168746f6b656e5f69647842307837373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737 \
  --expect 0xa2667374617475736a636f6e6669677572656468746f6b656e5f69647842307837373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737373737
```

`cowboy actor check <target>` validates a target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` creates a CIP-20 token with the `cowboy token` CLI, then deploys the
targets with `cowboy actor deploy escrow`, `buyer` and `seller`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Caveats

- This is a minimal workflow with one escrow actor and two helper actors.
- The example prioritizes clarity over breadth: one buyer actor, one seller actor, one escrow actor, one token.
- The CLI walkthrough proves balances with `cowboy token balance`, but actor queries still rely on emitted events because execute calls do not print handler return values directly.
