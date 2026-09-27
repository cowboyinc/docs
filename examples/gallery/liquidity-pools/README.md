# CIP-21: Liquidity Pools

> Uniswap V2-style constant-product AMM pools on Cowboy

## Overview

This example implements a V2 constant-product liquidity pool as a Cowboy actor. The pool holds reserves of two CIP-20 tokens, creates its own LP token, and allows permissionless swaps using the `x * y = k` invariant.

Unlike Ethereum where pools are Solidity contracts, this pool is a Python actor running in the PVM that uses native `runtime.token_*` host functions for all token operations (transfers, minting LP tokens, burning LP tokens). Token operations are system instructions — fast, cheap, and enforced by the validator.

## Simulate Locally

The tests drive the pool through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. CIP-20
`token_*` host calls are stubbed with a tiny LP-token ledger, so mint, burn
and custody moves are asserted directly.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_reserves({})` on a fresh pool and
expects the CBOR-encoded empty reserves:

```bash
cowboy test --target pool --handler get_reserves \
  --payload 0xa0 \
  --expect 0xa6676665655f627073181e67746f6b656e5f616067746f6b656e5f626069726573657276655f610069726573657276655f62006f6c705f746f74616c5f737570706c7900
```

`cowboy actor check pool` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

This deploys a WETH/USDC pool with `cowboy actor deploy pool`, adds liquidity, executes a swap, and removes liquidity — all against the mesa devnet. A project deploy checks the node's actor runtime profile, so use the CLI release that matches the network you target.

### Prerequisites

Build the CLI (one-time):

```bash
cargo build -p cowboy-cli --manifest-path node/Cargo.toml --release
```

## How It Works

### Constant Product Formula

```
x * y = k

Where:
  x = reserve of token A (e.g., WETH)
  y = reserve of token B (e.g., USDC)
  k = constant (increases slightly with each swap due to fees)

Swap output (with fee):
  amount_in_with_fee = amount_in * (10000 - fee_bps)
  amount_out = (amount_in_with_fee * reserve_out) /
               (reserve_in * 10000 + amount_in_with_fee)
```

### LP Token Math

First deposit:
```
LP_minted = isqrt(amount_a * amount_b) - MINIMUM_LIQUIDITY
```

The `MINIMUM_LIQUIDITY` (1000 units) is minted to the pool itself and permanently locked. This prevents the pool from being drained to zero.

Subsequent deposits:
```
LP_minted = min(
    amount_a * total_supply / reserve_a,
    amount_b * total_supply / reserve_b
)
```

Withdrawal (burning LP tokens):
```
amount_a = lp_amount * reserve_a / total_supply
amount_b = lp_amount * reserve_b / total_supply
```

### Trade Flow

```
User                         Pool Actor                 Token Registry
  |                              |                            |
  |  cowboy token approve        |                            |
  |  (pool, 1000 USDC)          |                            |
  | ────────────────────────────────────────────────────────> |
  |                              |                            |
  |  cowboy actor execute        |                            |
  |  (pool, "swap", ...)        |                            |
  | ──────────────────────────> |                            |
  |                              |                            |
  |                              |  token_transfer_from       |
  |                              |  (user → pool, USDC)       |
  |                              | ────────────────────────> |
  |                              |                            |
  |                              |  calculate output (x*y=k)  |
  |                              |                            |
  |                              |  token_transfer            |
  |                              |  (pool → user, WETH)       |
  |                              | ────────────────────────> |
  |                              |                            |
  | <─────────────────────────── |  emit swap event           |
```

## Pool Operations

| Handler | Purpose | Key Args |
|---------|---------|----------|
| `init_pool` | Set token pair, fee, create LP token | `token_a`, `token_b`, `fee_bps` |
| `add_liquidity` | Deposit both tokens, receive LP tokens | `amount_a`, `amount_b` |
| `remove_liquidity` | Burn LP tokens, receive both tokens | `lp_amount` |
| `swap` | Trade one token for the other | `token_in`, `amount_in`, `min_amount_out` |
| `get_reserves` | Emit reserves event | — |

## Permission Model

Cowboy actor handlers are deny-by-default: a wrapped handler refuses external callers unless it is annotated with `@public` or `@callable_by(...)`.

The pool claims ownership during atomic deploy:

```python
def init(self, payload):
    runtime.assume_ownership_if_unowned()
    ...
```

Owner-only setup uses `@callable_by(OWNER)`:

- `init_pool`

`init_pool` creates the LP token and fixes the token pair, fee, and pool address. It is owner-only and one-time so a later call cannot replace the pool's assets or LP token after users have deposited.

Public market operations use `@public`:

- `add_liquidity`
- `remove_liquidity`
- `swap`

These handlers are public because AMM participation is permissionless. They do not trust a `sender` field from the payload. The actor derives the participant from `self.sender` — the caller as an `Address`, which is what `runtime.token_transfer_from`, LP minting, swaps, and withdrawals are handed. A legacy payload may include `sender`, but `Address(declared) != self.sender` rejects the call with `sender mismatch` rather than comparing hex strings that could differ only in case or `0x` prefix.

Each of these carries `decode="cbor"` on its permission decorator, so the payload arrives as a decoded dict and a returned dict is encoded on the way out. `init` is the exception: it takes no arguments and returns bytes. The actor imports neither `json` nor `codec` — the codec is declared, not hand-rolled.

Read-only reserve queries use both `@public` and `@pure`:

- `get_reserves`

## Architecture

### Why Actors for Pools?

Cowboy tokens are system-level primitives (CIP-20), but composable DeFi logic — like AMM pools — lives in actors. The pool actor:

- **Creates** an LP token via `runtime.token_create()` (pool is the owner)
- **Pulls** tokens from users via `runtime.token_transfer_from()` (requires approval)
- **Sends** tokens to users via `runtime.token_transfer()`
- **Mints/burns** LP tokens via `runtime.token_mint()` / `runtime.token_burn()`

This is the same approve+transferFrom pattern as Uniswap on Ethereum.

### Token Flow

```
                     ┌─────────────────────────────────────┐
                     │            Token Registry            │
                     │                                     │
                     │  WETH balances    USDC balances      │
                     │  LP token balances                   │
                     └──────────┬──────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
  token_transfer_from     token_transfer          token_mint
  (user → pool)           (pool → user)           token_burn
        │                       │                  (LP tokens)
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                │
                     ┌──────────┴──────────┐
                     │     V2 Pool Actor    │
                     │                     │
                     │  reserve_a (int)     │
                     │  reserve_b (int)     │
                     │  fee_bps            │
                     │  lp_token (id)      │
                     └─────────────────────┘
```

### Storage Layout

| Key | Type | Description |
|-----|------|-------------|
| `token_a` | hex string | Token ID of token A |
| `token_b` | hex string | Token ID of token B |
| `lp_token` | hex string | Token ID of the LP token |
| `fee_bps` | int | Fee in basis points (30 = 0.30%) |
| `reserve_a` | int | Current reserve of token A |
| `reserve_b` | int | Current reserve of token B |
| `pool_address` | hex string | Address of this pool actor |
| `initialized` | bool | True once `init_pool` has run |

Values are stored as native Python types — `self.storage` CBOR-encodes on write and
decodes on read, so there is nothing to stringify. `cowboy actor state --address
<POOL>` prints the decoded map as JSON (`reserve_a` is a number, not `"0"`). Native
integers cap the reserves at `2^64 - 1`; see [Integer Math](#integer-math).

## Caveats

### Impermanent Loss

If the price ratio of the two tokens changes after you provide liquidity, you may get back less value than if you had simply held both tokens. This is inherent to constant-product AMMs.

### Initialization

The ratio `amount_a / amount_b` at first deposit sets the pool's initial price. If this doesn't match the market price, arbitrageurs will immediately profit at the LP's expense. Always initialize at current market prices.

### Integer Math

All math is integer-only. The `isqrt()` function uses Newton's method (no `math.sqrt`).

CIP-20 quantities are `u128`, but this pool tops out a `u64` below that: `2^64 - 1`
(~1.84 × 10^19). The limit is the SDK's canonical CBOR encoder, which emits no
bignum tags — a storage write of `2^64` raises `OverflowError`, an event payload
raises `CodecError`. (It *decodes* bignum tags 2 and 3, so the asymmetry only shows
up on a write.)

Every handler checks the amounts and the resulting reserves against that ceiling and
returns `{"error": "amount exceeds the pool's u64 ceiling"}`. Letting the encode
fail instead would not corrupt anything — an uncaught raise discards the transaction
frame, token writes included — but the caller would get an opaque encode error after
paying for the transfers. A pool over a large-supply token (genesis mints one at
10^21) has to scale its quantities down, or the SDK needs bignum support first.

### Reserve Tracking

Reserves are tracked internally in actor storage. Cowboy actors cannot read token balances of other accounts. Direct token transfers to the pool address (bypassing `add_liquidity`) will desync reserves from actual balances.

### MINIMUM_LIQUIDITY

1000 LP tokens are permanently locked on the first deposit. This prevents the total supply from reaching zero after the initial deposit, which would break the proportional math for subsequent deposits.

## CIP-21 Roadmap

CIP-21 is the full liquidity pool standard. What's implemented today vs planned:

| Component | Status |
|-----------|--------|
| V2 constant-product pool actor | **Implemented** (`actors/pool/main.py`) |
| LP token (mint/burn) | **Implemented** (via CIP-20) |
| Fee tiers (1, 5, 30, 100 bps) | **Implemented** (configurable) |
| Platform AMM primitives (`amm_get_amount_out`, etc.) | Planned |
| V3 concentrated liquidity | Planned |
| Pool factory (one pool per pair+fee) | Planned |
| Router (multi-hop swaps) | Planned |
| Validation hooks (`can_swap`, `on_swap`) | Planned |

See `actors/pool/cip21_reference.py` for the full spec of all planned interfaces.

## Files

| File | Description |
|------|-------------|
| `cowboy.toml` | Project manifest; targets are discovered from `actors/<name>/main.py` |
| `actors/pool/main.py` | V2 constant-product pool actor (the `pool` target) |
| `actors/pool/cip21_reference.py` | CIP-21 reference spec — all interfaces, events, fee tiers |
| `tests/test_pool.py` | `SimulatedChain` tests, run with `uv run pytest` |
| `demo.sh` | End-to-end demo: create tokens, deploy pool, swap, remove liquidity |
| `README.md` | This file |
