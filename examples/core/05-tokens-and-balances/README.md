# Tokens And Balances

CIP-20 is Cowboy's native fungible token standard.

> Standard interface for fungible tokens on Cowboy (like ERC-20 for Ethereum)

## Overview

CIP-20 tokens are **first-class chain primitives** on Cowboy. Unlike Ethereum where tokens are Solidity contracts, Cowboy tokens are native system instructions — token operations are fast, cheap, and atomically enforced by the validator.

```
                         ┌───────────────────────────────────┐
   cowboy token create   │        Token Registry             │
   ─────────────────────►│                                   │
                         │  mint:  {token_id} → metadata     │
   cowboy token transfer │  bal:   {owner}{token_id} → u64   │
   ─────────────────────►│  alw:   {owner}{spender}{id} → u64│
                         │  frz:   {token_id}{account} → flag│
   GET /token/{id}/      │                                   │
       balance/{addr}    │  Enforced by validator:            │
   ─────────────────────►│  - overflow/underflow checks      │
                         │  - frozen account enforcement      │
                         │  - allowance tracking              │
                         └───────────────────────────────────┘
```

## Quick Start

### Prerequisites

Build the CLI (one-time):

```bash
cargo build -p cowboy-cli --manifest-path node/Cargo.toml --release
```

### Run the Full Demo

```bash
./demo.sh
```

This creates wallets, funds them via faucet, creates WETH and USDC tokens, transfers, approves, mints, and burns — all against the mesa devnet.

### Step-by-Step with CLI

```bash
# 1. Initialize a project (creates wallet, funds via faucet)
cowboy init dev --rpc-url https://rpc.mesa.cowboylabs.net

# 2. Create a token
cowboy token create \
    --name "Wrapped Ether" \
    --symbol WETH \
    --decimals 18 \
    --initial-supply 10000000000000000000  # 10 WETH

# 3. List tokens to get the token_id
cowboy token list

# 4. Check your balance
cowboy token balance --token-id <TOKEN_ID> --address <YOUR_ADDRESS>

# 5. Transfer tokens
cowboy token transfer --token-id <TOKEN_ID> --to <RECIPIENT> --amount 1000000000000000000

# 6. Approve a spender (for DEX-style interactions)
cowboy token approve --token-id <TOKEN_ID> --spender <DEX_ADDRESS> --amount 5000000000000000000

# 7. Mint more tokens (only token owner)
cowboy token mint --token-id <TOKEN_ID> --to <ADDRESS> --amount 1000000000000000000

# 8. Burn tokens
cowboy token burn --token-id <TOKEN_ID> --amount 500000000000000000

# 9. Query token info
cowboy token info --token-id <TOKEN_ID>
```

### From Actor Code (PVM)

Actors running on-chain can interact with tokens using `runtime.token_*` host functions:

```python
from cowboy_sdk import runtime, actor

@actor
class MyDeFi:
    def swap(self, payload):
        sender = self.sender

        # Pull tokens from user (requires prior approval)
        runtime.token_transfer_from(usdc_token_id, sender, self.address, amount_in)

        # Send tokens back to user
        runtime.token_transfer(weth_token_id, sender, amount_out)
```

See `actors/token/main.py` (the `token` target) for a complete actor example.

## Token Operations

| Operation | CLI Command | Runtime Function |
|-----------|-------------|------------------|
| Create | `cowboy token create` | `runtime.token_create(...)` |
| Transfer | `cowboy token transfer` | `runtime.token_transfer(token_id, to, amount)` |
| Approve | `cowboy token approve` | `runtime.token_approve(token_id, spender, amount)` |
| Transfer From | — | `runtime.token_transfer_from(token_id, from, to, amount)` |
| Mint | `cowboy token mint` | `runtime.token_mint(token_id, to, amount)` |
| Burn | `cowboy token burn` | `runtime.token_burn(token_id, amount)` |
| Freeze | `cowboy token freeze` | — |
| Unfreeze | `cowboy token unfreeze` | — |
| Query Balance | `cowboy token balance` | `runtime.token_balance_of(token_id, account)` |
| Query Supply | `cowboy token info` | `runtime.token_total_supply(token_id)` |
| Query Allowance | — | `runtime.token_allowance(token_id, owner, spender)` |
| List Tokens | `cowboy token list` | — |

## HTTP API

Query token state directly via the validator's HTTP API:

```
GET /tokens                                    → list all tokens
GET /token/{token_id}                          → token metadata
GET /token/{token_id}/balance/{address}        → balance query
GET /token/{token_id}/allowance/{owner}/{spender} → allowance query
```

## Decimal Conventions

| Token Type | Decimals | 1 Token = | Max Supply (u64) |
|------------|----------|-----------|-------------------|
| ETH-like | 18 | 10^18 units | ~18.4 tokens |
| USDC-like | 6 | 10^6 units | ~18.4T tokens |
| BTC-like | 8 | 10^8 units | ~184B tokens |

Note: Supply is stored as `u64`, so max representable value is ~1.84 * 10^19. Choose decimals accordingly.

## Architecture

### Why System Instructions?

On Ethereum, every ERC-20 token is a separate smart contract. This means:
- Each token re-implements the same logic (transfer, approve, etc.)
- Bugs in one token contract can drain funds
- Each operation costs gas for contract execution

On Cowboy, CIP-20 tokens are **system instructions** handled by the validator:
- Token logic is implemented once, in the validator (Rust)
- All tokens share the same battle-tested implementation
- Operations are cheaper since there's no actor execution overhead
- Actors can still compose with tokens via `runtime.token_*` host functions

### Approve + TransferFrom Flow

```
User                     Validator                   Pool Actor
  │                         │                            │
  │  token approve          │                            │
  │  (pool, 1000 USDC)     │                            │
  │────────────────────────►│                            │
  │                         │  alw[user][pool] = 1000    │
  │                         │                            │
  │  actor execute          │                            │
  │  (pool, "swap", ...)   │                            │
  │────────────────────────►│──────────────────────────►│
  │                         │                            │
  │                         │  token_transfer_from       │
  │                         │◄───────────────────────────│
  │                         │  bal[user] -= 500          │
  │                         │  bal[pool] += 500          │
  │                         │  alw[user][pool] -= 500    │
  │                         │                            │
  │                         │  token_transfer            │
  │                         │◄───────────────────────────│
  │                         │  bal[pool] -= weth_out     │
  │                         │  bal[user] += weth_out     │
```

## Files

| File | Description |
|------|-------------|
| `cowboy.toml` | Project manifest; targets are discovered from `actors/<name>/main.py` |
| `actors/token/main.py` | The `token` target: an actor that creates and manages tokens from PVM code |
| `actors/token/cip20_token.py` | CIP-20 reference documentation (system instructions, runtime API, HTTP endpoints) |
| `tests/test_token.py` | `SimulatedChain` tests, run with `uv run pytest` |
| `demo.sh` | End-to-end demo script using the CLI against mesa devnet |
| `README.md` | This file |

## Simulate Locally

The tests drive the `token` actor through `cowboy_sdk.testing.SimulatedChain`,
an in-memory host: state, events, sender and block height, no node. The mock
host has no Token Registry, so the test installs a small in-memory stand-in for
the `token_*` host functions and asserts on the balances it records.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `check_balances({})` on a fresh actor (no
token created yet — the simulator has no Token Registry, so `create_token`
itself needs a network) and expects the JSON error reply:

```bash
cowboy test --target token --handler check_balances \
  --payload 0xa0 \
  --expect 0x7b226572726f72223a20226e6f20746f6b656e206372656174656420796574227d
```

`cowboy actor check token` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` exercises the token system instructions through the `cowboy token`
CLI; it does not deploy the `token` target. Deploy it yourself with
`cowboy actor deploy token` from this directory when you want the actor-side
flow on a network.
