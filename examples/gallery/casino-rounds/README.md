# Casino Rounds Example

This example ports the `1-codex` casino actors into the main Cowboy examples set as a runnable multi-actor demo with a separate long-running watcher.

## Actors

- `actors/casino/main.py` runs the prediction casino, opens and resolves each round, pays winners from the pooled pot, and prunes old round state into a bounded recent window.
- `actors/feed/main.py` is a deterministic mock feed that generates the settlement price for each round.
- `actors/bettor/main.py` is an automated bettor that watches the active round and submits one prepaid bet per round.

## Round Flow

1. `demo.sh` creates a CIP-20 token and deploys the feed, casino, and bettor actors.
2. The demo funds the casino pot and tops up each bettor with betting tokens.
3. The feed and casino are configured, then the casino starts auto-restarting rounds.
4. Each bettor starts its timer loop and joins open rounds automatically.
5. `tools/watch_rounds.py` can stream round openings, bets, payouts, pruning events, and balances after the demo exits.

## Files

- `cowboy.toml` — project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/casino/main.py`, `actors/feed/main.py`, `actors/bettor/main.py` — the three actor targets
- `tests/` — `SimulatedChain` tests, run with `uv run pytest`
- `tools/watch_rounds.py` — off-chain terminal watcher (not deployed)
- `demo.sh`

## Simulate Locally

The tests drive each actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender, timers and block height, no node. The
feed is mocked with `CallMock` for cross-actor calls and CIP-20 `token_*`
host calls are stubbed as custody moves.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_state({})` on a fresh casino and
expects the CBOR-encoded default configuration:

```bash
cowboy test --target casino --handler get_state \
  --payload 0xa0 \
  --expect 0xaf676665655f6270730068746f6b656e5f6964606a666565645f6163746f72606a73656c665f6163746f72606c6175746f5f656e61626c6564f46d686f7573655f62616c616e6365006d726f756e645f636f756e746572006d7661756c745f62616c616e6365006f6163746976655f726f756e645f696400716d61785f726563656e745f726f756e64731820726163746976655f726f756e645f737461746560736d61785f64657461696c65645f726f756e64730175726f756e645f6475726174696f6e5f626c6f636b7314767072756e65645f6265666f72655f726f756e645f69640078196175746f5f726573746172745f64656c61795f626c6f636b7301
```

`cowboy actor check casino` (and `feed`, `bettor`) validates a target
offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or
at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a
node checkout).

## Run On A Network

```bash
cd cowboy/examples/gallery/casino-rounds
./demo.sh
python3 tools/watch_rounds.py --state-file "${TMPDIR:-/tmp}/cowboy-casino-rounds-deploy-state.env" --show-balances
```

`demo.sh` deploys the targets with `cowboy actor deploy casino` (and `feed`,
`bettor`). A project deploy checks the node's actor runtime profile, so use
the CLI release that matches the network you target.

The demo writes a state file to `${TMPDIR:-/tmp}/cowboy-casino-rounds-deploy-state.env`, which the watcher reads by default so it can reconnect to the deployed casino and bettor actors. Set `STATE_FILE=<path>` for both commands if you want a different location.

The casino retains full detail only for the newest resolved round by default, keeps compact summaries for the newest 32 resolved rounds, and relies on actor logs as the long-term audit trail after older rounds are pruned.

## What It Teaches

- Multi-actor coordination with a feed, a casino, and multiple bettor actors
- Token-backed prepaid betting and automated round settlement
- Timer-driven autonomous behavior for both the casino and each bettor
- A streaming terminal view that combines recent on-chain state with actor event logs for longer-lived monitoring
- A worked example of Cowboy's deny-by-default permission model — see [Permission Model](#permission-model) below

## Permission Model

Cowboy actor handlers are **deny-by-default**: a handler the SDK wraps (any non-underscore method on an `@actor` class) refuses external callers unless you annotate it with `@public` or `@callable_by(...)`. The three actors in this example are wired with the five permission patterns developers reach for most often in dapps — each one is exercised every time `demo.sh` runs.

### 1. Ownable initialization (atomic deploy + `assume_ownership_if_unowned`)

Each actor's `init` calls `runtime.assume_ownership_if_unowned()`:

```python
# actors/casino/main.py, actors/feed/main.py, actors/bettor/main.py — all use this pattern
def init(self, payload):
    runtime.assume_ownership_if_unowned()
    ...
```

This is safe **only because Cowboy runs `init` atomically with the deploy transaction** (`sender = deployer`, no race window). If `init` ran in a separate transaction, a front-running bot could call it first and seize ownership — the same CPIMP-class vulnerability as the OpenZeppelin proxy `initialize()` story. Cowboy's atomic-init closes that window.

After this call, `runtime.get_owner()` returns the deployer's address. Every `@callable_by(OWNER)` handler below resolves against that value.

### 2. Owner-only setup (`@callable_by(OWNER)`)

Setup and lifecycle handlers belong to the deployer:

```python
# actors/casino/main.py
@callable_by(OWNER, decode="raw")
def configure(self, payload): ...

@callable_by(OWNER, decode="raw")
def start_auto(self, payload): ...

@callable_by(OWNER, decode="raw")
def open_round(self, payload): ...
```

`actors/bettor/main.py` uses the same decorator on `configure`, `start`, `stop`, and their `*_auto_betting` aliases. `actors/feed/main.py` uses it on `configure`, `set_bounds`, `start_auto`, `stop_auto`.

This is the precedent equivalent of Solidity's `onlyOwner` (OpenZeppelin `Ownable`). It's the right fit when there's a single administrative principal — the deployer. If you needed multiple roles with finer-grained permissions, you'd reach for explicit allowlists (`@callable_by(addr1, addr2)`) or a custom role table in storage; `@callable_by(OWNER)` is the simple, common case.

### 3. Public participation (`@public`)

Anyone should be able to bet. Anyone should be able to read state. So:

```python
# actors/casino/main.py
@public(decode="raw")
def place_bet(self, payload): ...

@public(decode="raw")
def inspect_round(self, payload): ...

@public(decode="raw")
def get_state(self, payload): ...
```

The market's *state* is the access control here — `place_bet` checks `state == "open"`, `block_height < close_height`, sufficient escrow, no duplicate bet. There's no sender allowlist because there shouldn't be one in a real public market; this matches how Polymarket/Augur expose their bet entry points.

`feed.generate_price` is also `@public` so the casino can call it cross-actor. The casino learned the feed's address at `configure` time and trusts it implicitly.

### 4. Timer self-fires (`@callable_by(SELF)`)

Both the casino and the bettors schedule their own timers via `runtime.schedule_after(delay_blocks, handler, payload)`. When a timer fires, the host invokes the named handler on the same actor with `sender = self.address`. `@callable_by(SELF)` matches exactly this:

```python
# actors/casino/main.py
@callable_by(SELF, decode="raw")
@deferred
def advance_cycle(self, payload): ...      # close + schedule next round

@callable_by(SELF, decode="raw")
@deferred
def open_next_round(self, payload): ...    # auto-restart after settle

# actors/bettor/main.py
@callable_by(SELF, decode="raw")
@deferred
def run(self, payload): ...                # the bettor's main loop tick
```

**Common pitfall:** `@callable_by(SELF)` does **not** allow `self.foo(...)` calls from another handler — `runtime.get_sender()` returns the outermost caller, not the actor's own address. `SELF` only matches genuine timer/host-set sender flows. If you want "callable only from inside this actor's own code," use a `_underscore` helper instead (the SDK skips permission checks on those).

### 5. Permissionless crank (`@public` with state guard)

`close_round` and `finalize_round` (its alias) settle a round once its close height is reached. They're `@public`, but the body guards on round state:

```python
# actors/casino/main.py
@public(decode="raw")
@deferred
def close_round(self, payload):
    ...
    if self.storage.get(_round_key(round_id, "state"), "") != "open":
        runtime.emit_event("error", {"reason": "round_not_open"})
        return b"not_open"
    ...
```

The contract: anyone can pay the gas to crank the market forward, but they can only do so when the market is ready. The casino's own timer (`advance_cycle`) is the normal driver, but if it fails or stalls, any address can step in. This is the dapp-native precedent for resolution functions (e.g. Polymarket settlement, perp liquidations, AMM oracle pokes): make it permissionless, gate it with state.

### Inter-actor message handling

`handle_message` is `@public` on both `actors/casino/main.py` and `actors/feed/main.py`. The runtime delivers async `send_message` payloads by invoking a handler the recipient declared — typically `handle_message`, which dispatches on the `cmd` field:

```python
# actors/casino/main.py
@public(decode="raw")
@deferred
def handle_message(self, payload):
    data = _parse(payload)
    cmd = data.get("cmd", "")
    if cmd == "submit_bet":
        return self.place_bet(json.dumps(data).encode())
    if cmd == "settle_round":
        return self.settle_round(json.dumps(data).encode())
    ...
```

`settle_round` itself is `@callable_by(SELF)`: it's only ever reached through this dispatch or via `close_round` (intra-actor — the call always passes), so external callers don't need a way in.

### Summary

| Pattern | Used by | Precedent |
|---|---|---|
| Atomic init + `assume_ownership_if_unowned` | All three actors | OpenZeppelin `Ownable` + Initializable |
| `@callable_by(OWNER)` for setup | `configure`, `start_auto`, `open_round`, etc. | `onlyOwner` modifier |
| `@public` for participation | `place_bet`, `inspect_round`, `generate_price` | Polymarket/Augur bet entry points |
| `@callable_by(SELF)` for timer self-fires | `advance_cycle`, `open_next_round`, `run` | Gelato/Chainlink keeper callbacks |
| `@public` + state guard for settlement | `close_round`, `finalize_round` | Permissionless crank / oracle pokes |

For the full decorator and runtime helper reference, see [CIP-6 Handler Permissions](../../../docs/cips/cip-6-sdk.md#7-handler-permissions).

## Caveats

- This example is heavier than most demos because it creates a token, funds actors, and deploys several actors.
- It is intended for direct runs, not the default local example sweep.
- The watcher reads raw actor storage through `cowboy actor get` for current state and uses `/actor/<addr>/logs` for longer-term round history.
