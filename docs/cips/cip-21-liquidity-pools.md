---
title: "CIP-21: DEX & Liquidity Pools"
description: Hybrid AMM standard with V2/V3 pools and platform primitives
icon: arrow-right-arrow-left
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-01-18
  **Requires:** CIP-20
</Note>

## Abstract

CIP-21 defines Cowboy's standard for decentralized exchanges and liquidity pools. The design is **hybrid**: pools are actors (maximum flexibility) with platform-level primitives for efficiency (math helpers, routing, LP tokens).

Key features:
- **Two pool types**: Constant product (V2-style) and concentrated liquidity (V3-style)
- **Platform LP tokens**: Fungible LP shares for V2 pools (CIP-20 tokens)
- **Actor-managed positions**: Non-fungible liquidity positions for V3 pools
- **Validation hooks**: `can_swap` / `on_swap` for compliance and MEV protection
- **Dual routing**: Actor-based router for flexibility, platform primitive for efficient multi-hop

---

## Motivation

DEXes are critical infrastructure for any blockchain ecosystem. Cowboy's unique features—actors, timers, platform tokens—enable DEX designs not possible on Ethereum:

- **Native TWAP oracles** via timers (no external keeper)
- **On-chain limit orders** via height-timer polling (a state-triggered timer is a future CIP-5 extension; see §Limit Orders)
- **Efficient batch swaps** via platform routing
- **Compliance pools** via validation hooks

This CIP provides a standard interface so wallets, aggregators, and applications can interact with any Cowboy DEX predictably.

---

## Specification

### Overview

![DEX Architecture](https://storage.googleapis.com/second-petal-295822.appspot.com/elements/elements%3A49ccbd519f0cd0a55eb27d241a3f2e8c8b1e091da7f6b0e66c728ed00d4f26f5.png)

---

## Part 1: Platform Primitives

The runtime provides efficient helpers for common AMM operations.

### 1.1 Math Primitives

```python
# Constant product math
def amm_get_amount_out(
    amount_in: u256,
    reserve_in: u256,
    reserve_out: u256,
    fee_bps: u16
) -> u256:
    """
    Calculate output amount for constant product swap.

    Formula: amount_out = (amount_in * (10000 - fee_bps) * reserve_out)
                         / (reserve_in * 10000 + amount_in * (10000 - fee_bps))

    Cost: 100 Cycles
    """

def amm_get_amount_in(
    amount_out: u256,
    reserve_in: u256,
    reserve_out: u256,
    fee_bps: u16
) -> u256:
    """
    Calculate required input for desired output.

    Cost: 100 Cycles
    """

def amm_quote(
    amount_a: u256,
    reserve_a: u256,
    reserve_b: u256
) -> u256:
    """
    Calculate equivalent amount of token B for given amount of token A.
    Used for adding liquidity at current ratio.

    Cost: 50 Cycles
    """
```

### 1.2 Concentrated Liquidity Math

```python
# Tick math (V3-style)
def amm_tick_to_sqrt_price(tick: i24) -> u160:
    """
    Convert tick index to sqrt(price) in Q64.96 format.

    Formula: sqrt_price = 1.0001^(tick/2) * 2^96

    Cost: 200 Cycles
    """

def amm_sqrt_price_to_tick(sqrt_price: u160) -> i24:
    """
    Convert sqrt(price) to nearest tick index.

    Cost: 200 Cycles
    """

def amm_get_liquidity_for_amounts(
    sqrt_price_current: u160,
    sqrt_price_lower: u160,
    sqrt_price_upper: u160,
    amount_a: u256,
    amount_b: u256
) -> u128:
    """
    Calculate liquidity value for given token amounts in a price range.

    Cost: 300 Cycles
    """

def amm_get_amounts_for_liquidity(
    sqrt_price_current: u160,
    sqrt_price_lower: u160,
    sqrt_price_upper: u160,
    liquidity: u128
) -> (u256, u256):
    """
    Calculate token amounts for given liquidity in a price range.

    Cost: 300 Cycles
    """
```

### 1.3 Platform Routing

```python
def amm_swap_exact_in(
    path: list[tuple[address, bytes32]],  # [(pool, token_out), ...]
    token_in: bytes32,
    amount_in: u256,
    min_amount_out: u256,
    recipient: address
) -> u256:
    """
    Execute multi-hop swap with exact input amount.

    Path format: [(pool_1, token_mid), (pool_2, token_out)]

    Flow:
    1. Transfer token_in from caller to first pool
    2. For each hop: call pool.swap(), forward output to next pool
    3. Transfer final output to recipient
    4. Verify output >= min_amount_out

    Cost: 1000 + (500 * num_hops) Cycles

    Note: Pools must implement ISwappable interface.
    """

def amm_swap_exact_out(
    path: list[tuple[address, bytes32]],
    token_in: bytes32,
    max_amount_in: u256,
    amount_out: u256,
    recipient: address
) -> u256:
    """
    Execute multi-hop swap with exact output amount.

    Returns: actual amount_in used

    Cost: 1000 + (500 * num_hops) Cycles
    """
```

---

## Part 2: V2 Pool Standard (Constant Product)

V2 pools use the classic `x * y = k` formula with fungible LP tokens.

### 2.1 Interface

```python
class IV2Pool:
    """Standard interface for constant product pools"""

    # Immutable properties
    def token_a(self) -> bytes32: ...
    def token_b(self) -> bytes32: ...
    def lp_token(self) -> bytes32: ...  # Platform token ID
    def fee_bps(self) -> u16: ...        # Fee in basis points (e.g., 30 = 0.30%)

    # State queries
    def get_reserves(self) -> (u256, u256): ...
    def get_spot_price(self, token: bytes32) -> u256: ...  # Price in Q128 format

    # Core operations
    def swap(
        self,
        token_in: bytes32,
        amount_in: u256,
        min_amount_out: u256,
        recipient: address
    ) -> u256: ...

    def add_liquidity(
        self,
        amount_a_desired: u256,
        amount_b_desired: u256,
        amount_a_min: u256,
        amount_b_min: u256,
        recipient: address
    ) -> (u256, u256, u256): ...  # (amount_a, amount_b, lp_minted)

    def remove_liquidity(
        self,
        lp_amount: u256,
        amount_a_min: u256,
        amount_b_min: u256,
        recipient: address
    ) -> (u256, u256): ...  # (amount_a, amount_b)

    # For platform router
    def swap_callback(
        self,
        token_in: bytes32,
        amount_in: u256,
        data: bytes
    ) -> u256: ...
```

### 2.2 Validation Hooks

V2 pools MAY specify a validation hook for compliance or MEV protection:

```python
class IV2PoolHook:
    """Validation hook interface for V2 pools"""

    def can_swap(
        self,
        pool: address,
        user: address,
        token_in: bytes32,
        amount_in: u256,
        min_amount_out: u256
    ) -> bool:
        """
        Called before swap execution.

        Return True to allow, False to block.
        Cannot modify amounts.
        """

    def on_swap(
        self,
        pool: address,
        user: address,
        token_in: bytes32,
        amount_in: u256,
        amount_out: u256
    ) -> None:
        """
        Called after successful swap.

        Use for: analytics, fee distribution, MEV rebates.
        Must not revert.
        """

    def can_add_liquidity(
        self,
        pool: address,
        user: address,
        amount_a: u256,
        amount_b: u256
    ) -> bool:
        """Called before adding liquidity."""

    def can_remove_liquidity(
        self,
        pool: address,
        user: address,
        lp_amount: u256
    ) -> bool:
        """Called before removing liquidity."""
```

### 2.3 Reference Implementation

```python
class V2Pool(Actor):
    """Constant product AMM with platform LP token"""

    def init(
        self,
        token_a: bytes32,
        token_b: bytes32,
        fee_bps: u16,
        hook: address | None = None
    ):
        require(token_a != token_b, "identical tokens")

        # Sort tokens for canonical ordering
        if token_a > token_b:
            token_a, token_b = token_b, token_a

        self.token_a = token_a
        self.token_b = token_b
        self.fee_bps = fee_bps
        self.hook = hook

        self.reserve_a = 0
        self.reserve_b = 0

        # Create platform LP token
        self.lp_token = Token.create(
            name=f"Cowswap V2 LP",
            symbol="COW-V2-LP",
            decimals=18,
            initial_supply=0
        )

        # TWAP oracle (updated via timer)
        self.price_cumulative_a = 0
        self.price_cumulative_b = 0
        self.last_block = block.height

        # Schedule the first TWAP update. NOTE: CIP-5 timers are one-shot and
        # height-based — there is no `interval=`/`handler=` kwarg. The real
        # signature is schedule_timer(height, payload); recurrence is achieved
        # by the handler re-arming itself each block (CIP-5 §4.2 / Appendix B),
        # and the target handler is routed via the payload JSON convention.
        self.schedule_timer(block.height + 1,
                            b'{"_handler": "update_oracle", "_payload": ""}')

    def swap(
        self,
        token_in: bytes32,
        amount_in: u256,
        min_amount_out: u256,
        recipient: address
    ) -> u256:
        require(token_in in (self.token_a, self.token_b), "invalid token")
        require(amount_in > 0, "zero input")

        # Hook check
        if self.hook:
            allowed = Hook(self.hook).can_swap(
                self.address, msg.sender, token_in, amount_in, min_amount_out
            )
            require(allowed, "swap blocked by hook")

        # Determine direction
        is_a_to_b = token_in == self.token_a
        reserve_in = self.reserve_a if is_a_to_b else self.reserve_b
        reserve_out = self.reserve_b if is_a_to_b else self.reserve_a

        # Calculate output using platform primitive
        amount_out = amm_get_amount_out(amount_in, reserve_in, reserve_out, self.fee_bps)
        require(amount_out >= min_amount_out, "slippage")

        # Transfer tokens
        Token.transfer_from(token_in, msg.sender, self.address, amount_in)
        token_out = self.token_b if is_a_to_b else self.token_a
        Token.transfer(token_out, recipient, amount_out)

        # Update reserves
        if is_a_to_b:
            self.reserve_a += amount_in
            self.reserve_b -= amount_out
        else:
            self.reserve_b += amount_in
            self.reserve_a -= amount_out

        # Hook notification
        if self.hook:
            Hook(self.hook).on_swap(
                self.address, msg.sender, token_in, amount_in, amount_out
            )

        emit_event("Swap", {
            "sender": msg.sender,
            "token_in": token_in,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "recipient": recipient
        })

        return amount_out

    def add_liquidity(
        self,
        amount_a_desired: u256,
        amount_b_desired: u256,
        amount_a_min: u256,
        amount_b_min: u256,
        recipient: address
    ) -> (u256, u256, u256):
        # Hook check
        if self.hook:
            allowed = Hook(self.hook).can_add_liquidity(
                self.address, msg.sender, amount_a_desired, amount_b_desired
            )
            require(allowed, "add liquidity blocked by hook")

        # Calculate optimal amounts
        if self.reserve_a == 0 and self.reserve_b == 0:
            # First deposit sets the ratio
            amount_a = amount_a_desired
            amount_b = amount_b_desired
        else:
            # Match current ratio
            amount_b_optimal = amm_quote(amount_a_desired, self.reserve_a, self.reserve_b)
            if amount_b_optimal <= amount_b_desired:
                require(amount_b_optimal >= amount_b_min, "slippage B")
                amount_a = amount_a_desired
                amount_b = amount_b_optimal
            else:
                amount_a_optimal = amm_quote(amount_b_desired, self.reserve_b, self.reserve_a)
                require(amount_a_optimal <= amount_a_desired, "slippage A")
                require(amount_a_optimal >= amount_a_min, "slippage A")
                amount_a = amount_a_optimal
                amount_b = amount_b_desired

        # Transfer tokens
        Token.transfer_from(self.token_a, msg.sender, self.address, amount_a)
        Token.transfer_from(self.token_b, msg.sender, self.address, amount_b)

        # Mint LP tokens
        total_supply = Token.total_supply(self.lp_token)
        if total_supply == 0:
            lp_amount = sqrt(amount_a * amount_b) - 1000  # Minimum liquidity lock
            Token.mint(self.lp_token, address(0), 1000)   # Lock minimum
        else:
            lp_amount = min(
                amount_a * total_supply / self.reserve_a,
                amount_b * total_supply / self.reserve_b
            )

        require(lp_amount > 0, "insufficient liquidity minted")
        Token.mint(self.lp_token, recipient, lp_amount)

        # Update reserves
        self.reserve_a += amount_a
        self.reserve_b += amount_b

        emit_event("AddLiquidity", {
            "sender": msg.sender,
            "amount_a": amount_a,
            "amount_b": amount_b,
            "lp_minted": lp_amount,
            "recipient": recipient
        })

        return (amount_a, amount_b, lp_amount)

    def remove_liquidity(
        self,
        lp_amount: u256,
        amount_a_min: u256,
        amount_b_min: u256,
        recipient: address
    ) -> (u256, u256):
        # Hook check
        if self.hook:
            allowed = Hook(self.hook).can_remove_liquidity(
                self.address, msg.sender, lp_amount
            )
            require(allowed, "remove liquidity blocked by hook")

        total_supply = Token.total_supply(self.lp_token)

        # Calculate token amounts
        amount_a = lp_amount * self.reserve_a / total_supply
        amount_b = lp_amount * self.reserve_b / total_supply

        require(amount_a >= amount_a_min, "slippage A")
        require(amount_b >= amount_b_min, "slippage B")

        # Burn LP tokens
        Token.transfer_from(self.lp_token, msg.sender, self.address, lp_amount)
        Token.burn(self.lp_token, lp_amount)

        # Transfer tokens
        Token.transfer(self.token_a, recipient, amount_a)
        Token.transfer(self.token_b, recipient, amount_b)

        # Update reserves
        self.reserve_a -= amount_a
        self.reserve_b -= amount_b

        emit_event("RemoveLiquidity", {
            "sender": msg.sender,
            "lp_burned": lp_amount,
            "amount_a": amount_a,
            "amount_b": amount_b,
            "recipient": recipient
        })

        return (amount_a, amount_b)

    def update_oracle(self):
        """Timer callback: update TWAP oracle"""
        blocks_elapsed = block.height - self.last_block
        if blocks_elapsed > 0 and self.reserve_a > 0 and self.reserve_b > 0:
            # Accumulate price * time
            self.price_cumulative_a += (self.reserve_b << 128) / self.reserve_a * blocks_elapsed
            self.price_cumulative_b += (self.reserve_a << 128) / self.reserve_b * blocks_elapsed
            self.last_block = block.height

    def get_twap(self, token: bytes32, period_blocks: u64) -> u256:
        """Get time-weighted average price over period"""
        # Implementation uses price_cumulative snapshots
        ...
```

### 2.4 Worked Example: CIP-20 token ↔ V2 pool

This walks the full path end to end — **deploy [CIP-20](./cip-20-fungible-tokens.md) tokens → seed a V2 pool → swap → harvest LP fees** — with concrete
addresses, amounts, and expected balances. Every figure below is produced by the
spec formulas in [§1.1](#11-math-primitives) and [§2.3](#23-reference-implementation)
(integer floor division); a runnable snippet that reproduces them is at the end.

**Cast** (illustrative addresses):

| Role | Address | Holds |
|------|---------|-------|
| `ACME` token (CIP-20) | `0xace0…0020` | created in step 1 |
| `USDC` token (CIP-20) | `0x05dc…0020` | created in step 1 |
| V2 pool (Medium, 30 bps) | `0x9001…0021` | — |
| LP token | `0x1b71…0021` | minted by pool |
| Alice (liquidity provider) | `0xa11ce…01` | seeds the pool |
| Bob (trader) | `0xb0b…02` | swaps ACME → USDC |
| Carol (trader) | `0xca401…03` | swaps USDC → ACME (round trip) |

#### Step 1 — Deploy two CIP-20 tokens

```python
# cowboy_sdk — see CIP-20 §"SDK Usage"
acme = Token.create(name="ACME", symbol="ACME", decimals=0, initial_supply=1_000_000)  # -> 0xace0…0020
usdc = Token.create(name="USD Coin", symbol="USDC", decimals=0, initial_supply=1_000_000)  # -> 0x05dc…0020
```

`decimals=0` keeps the arithmetic whole-numbered for readability; production
tokens typically use 6–18 decimals.

#### Step 2 — Seed the V2 pool (first liquidity)

The pool pulls both legs with `Token.transfer_from` (Alice must `approve` the pool
first), then mints LP tokens. For the **first** provider the spec mints
`sqrt(amount_a * amount_b) - 1000` and locks `1000` to `address(0)`:

```python
Token.approve(acme, pool, 100_000)
Token.approve(usdc, pool, 100_000)
pool.add_liquidity(amount_a_desired=100_000, amount_b_desired=100_000,
                   amount_a_min=100_000, amount_b_min=100_000, recipient=alice)
# lp = sqrt(100_000 * 100_000) - 1000 = 100_000 - 1000 = 99_000
```

| | ACME reserve | USDC reserve | k = a·b | LP supply | Alice LP |
|---|---|---|---|---|---|
| after seed | 100_000 | 100_000 | 10_000_000_000 | 100_000 | 99_000 |

Emits `AddLiquidity(alice, 100_000, 100_000, 99_000, alice)` and `Sync(100_000, 100_000)`.
`1_000` LP is permanently locked at `address(0)` (anti-first-depositor-griefing),
so Alice owns `99_000 / 100_000 = 99%` of the pool.

#### Step 3 — Swap (round trip)

Bob swaps `10_000 ACME` for USDC. With `fee_bps = 30`:

```
amount_out = (10_000 * (10000-30) * 100_000) / (100_000 * 10000 + 10_000 * (10000-30))
           = (10_000 * 9970 * 100_000) / (1_000_000_000 + 99_700_000)
           = 9_970_000_000_000 / 1_099_700_000
           = 9_066   (USDC, floored)
```

Carol then swaps those `9_066 USDC` back for ACME, returning the price to ~1:1 so
the harvest in step 4 is **purely accrued fees** (no impermanent-loss noise):

| Swap | In | Out | ACME reserve | USDC reserve | k = a·b |
|------|----|-----|--------------|--------------|---------|
| Bob: ACME→USDC | 10_000 ACME | 9_066 USDC | 110_000 | 90_934 | 10_002_740_000 |
| Carol: USDC→ACME | 9_066 USDC | 9_945 ACME | 100_055 | 100_000 | 10_005_500_000 |

Each swap emits `Swap(sender, token_in, amount_in, amount_out, recipient)` and a
`Sync`. Note **k strictly grows** (10.000B → 10.0055B): the 30 bps fee on every
swap stays in the reserves. That growth *is* the LP fee.

#### Step 4 — Harvest LP fees

V2 has no separate "claim fees" call — fees accrue into the reserves, so each LP
token becomes redeemable for **more** than it was at seed. Measuring redemption
value per LP token isolates the fee cleanly (it is unaffected by the locked 1_000):

| | Pool value (ACME + USDC) | LP supply | Value per LP |
|---|---|---|---|
| at seed | 200_000 | 100_000 | 2.00000 |
| after round trip | 200_055 | 100_000 | 2.00055 |

**+0.0275% per LP token — entirely harvested swap fees.** Alice realizes her share
by burning her LP:

```python
pool.remove_liquidity(lp_amount=99_000, amount_a_min=0, amount_b_min=0, recipient=alice)
# amount_a = 99_000 * 100_055 / 100_000 = 99_054 ACME
# amount_b = 99_000 * 100_000 / 100_000 = 99_000 USDC
```

Alice receives **99_054 ACME + 99_000 USDC** (emits `RemoveLiquidity(alice, 99_000,
99_054, 99_000, alice)`). Her LP redeemed at the *grown* per-LP value (2.00055 vs
2.00000 at seed): that uplift is her cut of the 30 bps fees Bob and Carol paid.
(Her redemption tracks 99% of reserves, not 100%, because of the locked minimum
liquidity.)

#### Reproduce the numbers

```python
import math

def amount_out(amount_in, reserve_in, reserve_out, fee_bps=30):
    return (amount_in * (10000 - fee_bps) * reserve_out) \
        // (reserve_in * 10000 + amount_in * (10000 - fee_bps))

ra, rb = 100_000, 100_000                       # seed reserves (ACME, USDC)
lp = math.isqrt(ra * rb) - 1000                 # 99_000 to Alice
ts = lp + 1000                                  # 100_000 incl. locked minimum
o1 = amount_out(10_000, ra, rb); ra += 10_000; rb -= o1   # Bob:  -> 9_066 USDC
o2 = amount_out(o1, rb, ra);     rb += o1;     ra -= o2    # Carol:-> 9_945 ACME
print(ra, rb, ra * rb)                          # 100055 100000 10005500000
print(lp * ra // ts, lp * rb // ts)             # 99054 99000  (Alice's harvest)
```

---

## Part 3: V3 Pool Standard (Concentrated Liquidity)

V3 pools allow LPs to concentrate liquidity in price ranges for higher capital efficiency.

### 3.1 Core Concepts

**Ticks:** Price space is divided into discrete ticks. Each tick represents a 0.01% price change.

**Positions:** LPs provide liquidity between two ticks (a price range). Positions are non-fungible.

**Liquidity:** A position's liquidity value determines its share of fees when price is in range.

### 3.2 Interface

```python
class IV3Pool:
    """Standard interface for concentrated liquidity pools"""

    # Immutable properties
    def token_a(self) -> bytes32: ...
    def token_b(self) -> bytes32: ...
    def fee_bps(self) -> u16: ...
    def tick_spacing(self) -> i24: ...

    # State queries
    def slot0(self) -> (u160, i24, u16):  # (sqrt_price, tick, protocol_fee)
        ...
    def liquidity(self) -> u128: ...  # Current active liquidity
    def ticks(self, tick: i24) -> TickInfo: ...
    def positions(self, position_id: bytes32) -> PositionInfo: ...

    # Swap
    def swap(
        self,
        token_in: bytes32,
        amount_in: u256,
        sqrt_price_limit: u160,
        recipient: address
    ) -> (u256, u256): ...  # (amount_in_actual, amount_out)

    # Liquidity management
    def mint_position(
        self,
        tick_lower: i24,
        tick_upper: i24,
        amount: u128,
        recipient: address
    ) -> (bytes32, u256, u256): ...  # (position_id, amount_a, amount_b)

    def burn_position(
        self,
        position_id: bytes32,
        amount: u128  # Portion of position to burn
    ) -> (u256, u256): ...  # (amount_a, amount_b)

    def collect_fees(
        self,
        position_id: bytes32,
        recipient: address
    ) -> (u256, u256): ...  # (fees_a, fees_b)
```

### 3.3 Position Data

Positions are stored in actor state (not as NFTs):

```python
@dataclass
class PositionInfo:
    owner: address
    tick_lower: i24
    tick_upper: i24
    liquidity: u128

    # Fee tracking
    fee_growth_inside_a_last: u256
    fee_growth_inside_b_last: u256
    fees_owed_a: u128
    fees_owed_b: u128

@dataclass
class TickInfo:
    liquidity_gross: u128      # Total liquidity referencing this tick
    liquidity_net: i128        # Net liquidity change when crossing
    fee_growth_outside_a: u256
    fee_growth_outside_b: u256
    initialized: bool
```

### 3.4 Validation Hooks

V3 pools follow the same hook *pattern* as V2 (validation-only: return `True`/`False`, cannot modify amounts), but with V3-specific signatures — `can_swap` takes `sqrt_price_limit` in place of V2's `min_amount_out`, and position hooks (`can_mint_position` / `can_burn_position`) replace V2's `can_add_liquidity` / `can_remove_liquidity`:

```python
class IV3PoolHook:
    def can_swap(self, pool, user, token_in, amount_in, sqrt_price_limit) -> bool: ...
    def on_swap(self, pool, user, token_in, amount_in, amount_out) -> None: ...
    def can_mint_position(self, pool, user, tick_lower, tick_upper, amount) -> bool: ...
    def can_burn_position(self, pool, user, position_id, amount) -> bool: ...
```

### 3.5 Position Manager (Optional)

For better UX, a position manager actor can wrap positions as transferable:

```python
class V3PositionManager(Actor):
    """Wraps V3 positions for transferability"""

    def mint(
        self,
        pool: address,
        tick_lower: i24,
        tick_upper: i24,
        amount_a_desired: u256,
        amount_b_desired: u256,
        recipient: address
    ) -> u256:  # position_nft_id
        # Mint position in pool, store mapping
        ...

    def transfer_position(self, position_nft_id: u256, to: address):
        # Update owner mapping
        ...

    def increase_liquidity(self, position_nft_id: u256, amount_a: u256, amount_b: u256):
        ...

    def decrease_liquidity(self, position_nft_id: u256, liquidity: u128):
        ...

    def collect(self, position_nft_id: u256, recipient: address):
        ...
```

---

## Part 4: Factory

The factory creates and indexes pools:

```python
class ICowswapFactory:
    """Factory interface for creating pools"""

    def create_v2_pool(
        self,
        token_a: bytes32,
        token_b: bytes32,
        fee_bps: u16,
        hook: address | None = None
    ) -> address: ...

    def create_v3_pool(
        self,
        token_a: bytes32,
        token_b: bytes32,
        fee_bps: u16,
        tick_spacing: i24,
        hook: address | None = None
    ) -> address: ...

    def get_v2_pool(self, token_a: bytes32, token_b: bytes32, fee_bps: u16) -> address: ...
    def get_v3_pool(self, token_a: bytes32, token_b: bytes32, fee_bps: u16) -> address: ...

    def get_all_pools(self, token_a: bytes32, token_b: bytes32) -> list[address]: ...
```

### Standard Fee Tiers

| Tier | Fee (bps) | V3 Tick Spacing | Use Case |
|------|-----------|-----------------|----------|
| Stable | 1 | 1 | Stablecoin pairs |
| Low | 5 | 10 | Correlated pairs |
| Medium | 30 | 60 | Most pairs |
| High | 100 | 200 | Exotic pairs |

---

## Part 5: Router

### 5.1 Actor Router (Flexible)

```python
class CowswapRouter(Actor):
    """Flexible router with path-finding and complex operations"""

    def swap_exact_in(
        self,
        path: list[tuple[address, bytes32]],  # [(pool, token_out), ...]
        token_in: bytes32,
        amount_in: u256,
        min_amount_out: u256,
        recipient: address,
        deadline: u64
    ) -> u256:
        require(block.timestamp <= deadline, "expired")

        # Execute swaps along path
        current_amount = amount_in
        current_token = token_in

        for (pool, token_out) in path:
            current_amount = Pool(pool).swap(
                current_token,
                current_amount,
                0,  # No slippage check on intermediate
                self.address if not last_hop else recipient
            )
            current_token = token_out

        require(current_amount >= min_amount_out, "slippage")
        return current_amount

    def swap_exact_out(
        self,
        path: list[tuple[address, bytes32]],
        token_in: bytes32,
        max_amount_in: u256,
        amount_out: u256,
        recipient: address,
        deadline: u64
    ) -> u256:
        # Calculate required input, then execute
        ...

    def add_liquidity_v2(self, pool, amount_a, amount_b, min_lp, recipient, deadline):
        ...

    def remove_liquidity_v2(self, pool, lp_amount, min_a, min_b, recipient, deadline):
        ...

    def mint_position_v3(self, pool, tick_lower, tick_upper, amount_a, amount_b, recipient, deadline):
        ...
```

### 5.2 Platform Router (Efficient)

The platform router (`amm_swap_exact_in` / `amm_swap_exact_out`) provides:

- Lower gas cost (no actor call overhead per hop)
- Atomic multi-hop execution
- Standardized interface

Use platform router when:
- Standard path-based swap
- No custom logic needed
- Maximum efficiency required

Use actor router when:
- Complex operations (add liquidity + swap)
- Custom fee handling
- Flash swaps

---

## Part 6: Advanced Features

### 6.1 Native TWAP Oracle

V2 pools include a built-in TWAP oracle updated via timers:

```python
# Get 5-minute TWAP
twap_price = pool.get_twap(token_a, period_blocks=300)  # ~5 min at 1s blocks
```

No external oracle needed. Price manipulation requires sustained capital over the period.

### 6.2 On-Chain Limit Orders

> **⚠️ Note (timer API):** CIP-5 timers are **height-triggered one-shot only** — there is **no** state/condition trigger (`trigger_type="state"`, `watch_address`, `condition`, `handler=`) in the deployed timer API (`schedule_timer(height, payload)` / `schedule_timer_ex`). The example below is *illustrative of a desired pattern*; against the current API a limit order is emulated by re-arming a short-interval height timer each block that polls the pool price (the handler re-schedules itself, CIP-5 §4.2 / Appendix B). A true state-triggered timer would be a future CIP-5 extension.

Using a limit-order pattern layered on CIP-5 timers:

```python
class LimitOrderBook(Actor):
    def place_order(
        self,
        pool: address,
        token_in: bytes32,
        amount_in: u256,
        min_price: u256,  # Trigger price
        expiry: u64
    ) -> u256:  # order_id
        order_id = self._create_order(msg.sender, pool, token_in, amount_in, min_price, expiry)

        # Transfer tokens to escrow
        Token.transfer_from(token_in, msg.sender, self.address, amount_in)

        # Schedule state-triggered timer
        self.schedule_timer(
            trigger_type="state",
            watch_address=pool,
            watch_key="slot0.sqrt_price",
            condition=f">= {min_price}",
            handler="try_fill",
            args={"order_id": order_id}
        )

        return order_id

    def try_fill(self, order_id: u256):
        order = self.orders[order_id]

        # Check price still favorable
        (sqrt_price, _, _) = Pool(order.pool).slot0()
        if sqrt_price >= order.min_price:
            # Execute swap
            amount_out = Pool(order.pool).swap(
                order.token_in,
                order.amount_in,
                0,
                order.owner
            )
            self._close_order(order_id, filled=True)
        # Else: not fillable yet — re-arm a height timer for the next block to
        # re-poll (no state trigger exists; see the note above this example).

    def cancel_order(self, order_id: u256):
        order = self.orders[order_id]
        require(msg.sender == order.owner, "not owner")

        Token.transfer(order.token_in, order.owner, order.amount_in)
        self._close_order(order_id, filled=False)
```

### 6.3 MEV Protection via Hooks

```python
class MEVProtectionHook(Actor):
    """Pool hook that implements MEV rebates"""

    def can_swap(self, pool, user, token_in, amount_in, min_out) -> bool:
        # Allow all swaps
        return True

    def on_swap(self, pool, user, token_in, amount_in, amount_out):
        # Calculate if user was sandwiched
        expected_out = self._calculate_fair_output(pool, token_in, amount_in)

        if amount_out < expected_out * 0.99:  # >1% worse than fair
            # User was likely sandwiched, log for rebate
            emit_event("MEVDetected", {
                "user": user,
                "expected": expected_out,
                "actual": amount_out,
                "loss": expected_out - amount_out
            })
            # Rebate logic could be added here
```

### 6.4 Compliance Pools

```python
class KYCPoolHook(Actor):
    """Pool hook requiring KYC for swaps"""

    def init(self, kyc_registry: address):
        self.kyc_registry = kyc_registry

    def can_swap(self, pool, user, token_in, amount_in, min_out) -> bool:
        # Check KYC status
        return KYCRegistry(self.kyc_registry).is_verified(user)

    def can_add_liquidity(self, pool, user, amount_a, amount_b) -> bool:
        return KYCRegistry(self.kyc_registry).is_verified(user)

    def can_remove_liquidity(self, pool, user, lp_amount) -> bool:
        # Always allow withdrawal (regulatory requirement)
        return True
```

---

## Events

### V2 Pool Events

```python
Swap(sender: address, token_in: bytes32, amount_in: u256, amount_out: u256, recipient: address)
AddLiquidity(sender: address, amount_a: u256, amount_b: u256, lp_minted: u256, recipient: address)
RemoveLiquidity(sender: address, lp_burned: u256, amount_a: u256, amount_b: u256, recipient: address)
Sync(reserve_a: u256, reserve_b: u256)   # pools MUST emit after every reserve change (swap / add / remove); the §2.3 reference snippets elide it for brevity
```

### V3 Pool Events

```python
Swap(sender: address, recipient: address, amount_a: i256, amount_b: i256, sqrt_price: u160, liquidity: u128, tick: i24)
Mint(sender: address, owner: address, tick_lower: i24, tick_upper: i24, amount: u128, amount_a: u256, amount_b: u256)
Burn(owner: address, tick_lower: i24, tick_upper: i24, amount: u128, amount_a: u256, amount_b: u256)
Collect(owner: address, recipient: address, tick_lower: i24, tick_upper: i24, amount_a: u128, amount_b: u128)
```

### Error Conditions

Actor pools are illustrative reference implementations and revert via `require(cond, "<message>")` rather than numeric codes. The canonical revert messages are:

| Message | Condition |
|---------|-----------|
| `identical tokens` | `token_a == token_b` at pool init |
| `invalid token` | `token_in` is not one of the pool's two tokens |
| `zero input` | `amount_in == 0` |
| `slippage` / `slippage A` / `slippage B` | output below `min_amount_out`, or amount below `amount_*_min` |
| `swap blocked by hook` | `can_swap` returned `False` |
| `add liquidity blocked by hook` | `can_add_liquidity` returned `False` |
| `remove liquidity blocked by hook` | `can_remove_liquidity` returned `False` |
| `insufficient liquidity minted` | computed `lp_amount == 0` |

---

## Security Considerations

### Reentrancy

The actor model provides natural reentrancy protection—actors process one message at a time. However, cross-actor calls during swaps should follow checks-effects-interactions pattern.

### Price Manipulation

- TWAP oracles mitigate flash loan attacks
- Concentrated liquidity pools are more sensitive to manipulation at range boundaries
- Hooks can implement additional protections (MEV detection, circuit breakers)

### Hook Security

- Hooks are capped at 50,000 Cycles and 50,000 Cells (same as CIP-20 token hooks)
- Malicious hooks can block all swaps—pool deployers must be trusted
- Consider timelock for hook updates on major pools

### Integer Precision

- Use Q128 format for prices to maintain precision
- Concentrated liquidity math uses Q64.96 (matching Uniswap V3)
- Platform primitives handle precision; actor implementations should use them

---

## Rationale

### Why Hybrid (Actor + Platform)?

Pure actor-based: Maximum flexibility but higher gas costs
Pure platform-based: Maximum efficiency but inflexible

Hybrid gives:
- Flexibility for pool logic (actors)
- Efficiency for common operations (platform primitives)
- Best of both worlds

### Why Both V2 and V3?

V2 (constant product):
- Simpler for LPs
- Fungible LP tokens (composable with DeFi)
- Lower gas cost
- Good for stable pairs

V3 (concentrated):
- Higher capital efficiency
- Better for professional LPs
- Required for competitive pricing on major pairs

### Why Validation-Only Hooks?

Full hooks (modifying amounts) add complexity:
- Unpredictable outputs
- Gas estimation difficulty
- Hidden fees

Validation hooks are simpler:
- Swap succeeds or fails
- Predictable gas
- Covers compliance use cases

---

## Backwards Compatibility

This is a new standard. No backwards compatibility concerns.

---

## Reference Implementation

See `cowboy-core/src/runtime/amm.rs` for platform primitive implementations.

See `examples/cowswap/` for reference V2 and V3 pool implementations.
