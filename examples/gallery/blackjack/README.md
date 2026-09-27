# Blackjack Dealer Example

This example implements a single-table, vegas-style blackjack game with one authoritative dealer actor and CIP-20 chip escrow.

## What It Teaches

- a dealer actor that owns the entire blackjack round state
- CIP-20 token escrow via `token_transfer_from` for bets, doubles, splits, and bankroll funding
- vegas-style blackjack flow with a hole card, insurance, and dealer resolution
- split support, including split aces receiving one card each
- Cowboy's deny-by-default permission model for owner-only dealer operations, public player actions, and public views

## House Rules

- 6-deck shoe
- blackjack pays 3:2
- dealer stands on soft 17
- double allowed on any two-card hand, including after split
- split only on same-rank pairs
- up to 4 hands total
- split aces receive one card each and do not resplit
- insurance available only against an Ace upcard
- no surrender

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/dealer/main.py` — deployed dealer actor
- `actors/dealer/engine.py` — pure blackjack rules helpers for local tests
- `tests/test_dealer.py` — `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` — end-to-end walkthrough with a demo chip token
- `README.md` — this file

## Simulate Locally

The tests drive the dealer through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. CIP-20
`token_*` host calls are stubbed, so bets and payouts are asserted as custody
moves.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_table_state({})` on a fresh dealer and
expects the CBOR-encoded empty table:

```bash
cowboy test --target dealer --handler get_table_state \
  --payload 0xa0 \
  --expect 0xa66570686173656742455454494e4767706c61796572738068726f756e645f69640068746f6b656e5f6964606c70656e64696e675f62657473a06d63757272656e745f726f756e64a0
```

`cowboy actor check dealer` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy dealer`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

The demo script:

1. creates a house wallet and two player wallets
2. creates a 0-decimal CIP-20 chip token
3. deploys the dealer actor with table config in `--init-payload`, so `init` runs atomically in the deploy frame
4. funds the house bankroll
5. approves the dealer to pull player bets
6. runs one split-heavy round and one blackjack payout round
7. reads back table and player state through actor events

## Permission Model

Cowboy actor handlers are deny-by-default: a wrapped handler refuses external callers unless it is annotated with `@public` or `@callable_by(...)`.

The dealer follows the same pattern as the newer examples:

```python
def init(self, payload):
    owner = runtime.assume_ownership_if_unowned()
    ...
```

`demo.sh` passes the table config through `cowboy actor deploy --init-payload ...`, so init happens in the same deploy transaction that claims ownership for the house wallet. The script no longer calls `init` as a post-deploy execute.

The SDK owner is the only owner authority. `init` stores that runtime owner for display, but it does not accept an `owner` override from the payload. That avoids a split-brain state where `@callable_by(OWNER)` checks one address while game logic checks a different storage field.

Owner-only dealer operations use `@callable_by(OWNER)`:

- `fund_bankroll`
- `seed_next_shoe`
- `start_round`

Public player actions use `@public` and rely on game-state checks inside the handler:

- `join_table`
- `leave_table`
- `place_bet`
- `buy_insurance`
- `act`

Read-only views use both `@public` and `@pure`:

- `get_table_state`
- `get_player_state`
- `get_last_round`

## CBY Note

This example is intentionally **CIP-20 only**. Native `CBY` is not modeled as a CIP-20 token, and the current repo examples document native in-actor transfer support as incomplete. The working blackjack demo therefore uses a fungible chip token rather than pretending native CBY escrow works the same way.

## Caveats

- The rules and payout flow are casino-style, but the shoe ordering is deterministic unless you reseed it for a demo.
- The dealer actor is the full source of truth for one table only; there is no matchmaking or multiple concurrent tables.
- The CLI execute path does not print handler return values, so the walkthrough relies on emitted events and query handlers.
