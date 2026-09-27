"""
CIP-21: Cowboy Liquidity Pool Standard (DRAFT)
================================================

CIP-21 defines the standard interfaces for automated market maker (AMM)
liquidity pools on Cowboy. It covers V2 constant-product pools, V3
concentrated liquidity pools, factory contracts, and router interfaces.

Status:
  - V2 Pool actor:  IMPLEMENTED (see main.py, this package)
  - Platform AMM primitives:  NOT YET BUILT
  - V3 Concentrated liquidity:  PLANNED
  - Factory / Router:  PLANNED

Today, pools are implemented as actors using runtime.token_* host functions.
The platform primitives below will eventually provide optimized native
implementations that actors can call directly.


Implementation roadmap (COW-1097)
---------------------------------
Phased, with each phase gated on the previous. ETAs are relative to the
CIP-21 / CIP-22 / platform-launch timelines, not calendar dates, since the
DEX tracks the broader rollout.

  Phase 0  AMM host primitives        (CIP-21 §AMM; the native Q96/swap-math
                                       building blocks) — UNBLOCKS everything below.
  Phase 1  V2 pools + factory + router (constant-product; the IV2Pool /
                                       IPoolFactory / router interfaces here) —
                                       depends on Phase 0.
  Phase 2  V2 polish                  (fee accrual, LP-token accounting, pool
                                       init hardening) — depends on Phase 1.
  Phase 3  V3 concentrated liquidity  (IV3Pool: ticks, positions, mint/burn/
                                       collect) — depends on Phase 0 + 2; the
                                       Q96 tick math is the risky core.
  Phase 4  Hooks / TWAP / positions   (pool hooks, time-weighted oracles, NFT
                                       positions) — depends on Phase 3.
  Phase 5  Graduation                 (bonding-curve → pool graduation) —
                                       depends on Phase 1; UNBLOCKS the parked
                                       CIP-22 continuous-clearing-auction work.

Dependency summary: Phase 0 is the universal prerequisite; V3 (Phase 3) and
graduation (Phase 5) are the two long poles. The V2 path (Phases 0→1→2) is the
minimum that makes on-chain AMMs usable; CIP-22 unblocks only after Phase 5.


Part 1: Platform Primitives (NOT YET BUILT)
============================================

These will be runtime host functions (like runtime.token_*) that provide
optimized AMM math at the validator level. Until they ship, actors must
implement the math themselves (as main.py does).

    # Quote how much output you'd get for a given input
    runtime.amm_get_amount_out(
        amount_in: int,
        reserve_in: int,
        reserve_out: int,
        fee_bps: int,
    ) -> int

    # Quote how much input is needed for a desired output
    runtime.amm_get_amount_in(
        amount_out: int,
        reserve_in: int,
        reserve_out: int,
        fee_bps: int,
    ) -> int

    # Given a known amount of one asset, quote the equivalent of the other
    runtime.amm_quote(
        amount_a: int,
        reserve_a: int,
        reserve_b: int,
    ) -> int

    # V3 concentrated liquidity math (future)
    runtime.amm_compute_swap_step(
        sqrt_price_current: int,   # Q64.96 fixed-point
        sqrt_price_target: int,
        liquidity: int,
        amount_remaining: int,
        fee_bps: int,
    ) -> (sqrt_price_next: int, amount_in: int, amount_out: int, fee_amount: int)


Part 2: V2 Pool Interface (IV2Pool)
====================================

Standard handler signatures for a V2 constant-product pool actor.
See main.py for the reference implementation.

    class IV2Pool:

        def init_pool(self, payload):
            '''
            Initialize pool with token pair and fee.

            Payload: {
                "token_a": "<hex token ID>",
                "token_b": "<hex token ID>",
                "fee_bps": 30,
                "pool_address": "<hex address of this actor>"
            }

            Emits: pool_initialized {token_a, token_b, lp_token, fee_bps}
            '''

        def add_liquidity(self, payload):
            '''
            Add liquidity. Sender must approve pool for both tokens.

            Payload: {
                "sender": "<hex address>",
                "amount_a": 1000000,
                "amount_b": 2500000
            }

            First deposit:  LP = isqrt(amount_a * amount_b) - 1000
            Subsequent:     LP = min(a * supply / reserve_a, b * supply / reserve_b)

            Emits: liquidity_added {amount_a, amount_b, lp_minted, reserve_a, reserve_b}
            '''

        def remove_liquidity(self, payload):
            '''
            Burn LP tokens and receive proportional share of reserves.

            Payload: {
                "sender": "<hex address>",
                "lp_amount": 5000
            }

            amount_a = lp_amount * reserve_a / total_supply
            amount_b = lp_amount * reserve_b / total_supply

            Emits: liquidity_removed {lp_burned, amount_a, amount_b, reserve_a, reserve_b}
            '''

        def swap(self, payload):
            '''
            Swap one token for the other.

            Payload: {
                "sender": "<hex address>",
                "token_in": "<hex token ID>",
                "amount_in": 1000000,
                "min_amount_out": 900000
            }

            Formula:
              amount_in_with_fee = amount_in * (10000 - fee_bps)
              amount_out = (amount_in_with_fee * reserve_out) //
                           (reserve_in * 10000 + amount_in_with_fee)

            Emits: swap {token_in, amount_in, amount_out, fee_bps, reserve_a, reserve_b}
            '''

        def get_reserves(self, payload):
            '''
            Emit current reserves as an event.

            Emits: reserves {token_a, token_b, reserve_a, reserve_b, lp_total_supply, fee_bps}
            '''


Part 3: V3 Pool Interface (IV3Pool) — PLANNED
===============================================

Concentrated liquidity pools allow LPs to provide liquidity within a
specific price range, improving capital efficiency.

    class IV3Pool:

        def init_pool(self, payload):
            '''
            Payload: {
                "token_a": "<hex token ID>",
                "token_b": "<hex token ID>",
                "fee_bps": 30,
                "tick_spacing": 60,
                "sqrt_price_x96": "<initial sqrt price as Q64.96>"
            }
            '''

        def mint(self, payload):
            '''
            Add concentrated liquidity between two ticks.

            Payload: {
                "sender": "<hex address>",
                "tick_lower": -887220,
                "tick_upper": 887220,
                "amount": 1000000
            }
            '''

        def burn(self, payload):
            '''
            Remove concentrated liquidity from a position.

            Payload: {
                "sender": "<hex address>",
                "tick_lower": -887220,
                "tick_upper": 887220,
                "amount": 500000
            }
            '''

        def swap(self, payload):
            '''
            Swap using concentrated liquidity.

            Payload: {
                "sender": "<hex address>",
                "zero_for_one": true,
                "amount_specified": 1000000,
                "sqrt_price_limit_x96": "<Q64.96 price limit>"
            }
            '''

        def collect(self, payload):
            '''
            Collect accumulated fees for a position.

            Payload: {
                "sender": "<hex address>",
                "tick_lower": -887220,
                "tick_upper": 887220
            }
            '''


Part 4: Factory Interface — PLANNED
====================================

The factory creates and indexes pools so they can be discovered.

    class IPoolFactory:

        def create_v2_pool(self, payload):
            '''
            Deploy a new V2 pool for a token pair.

            Payload: {
                "token_a": "<hex token ID>",
                "token_b": "<hex token ID>",
                "fee_bps": 30
            }

            Returns: pool address
            Constraint: one pool per (token_a, token_b, fee_bps) triple
            '''

        def create_v3_pool(self, payload):
            '''
            Deploy a new V3 pool for a token pair.

            Payload: {
                "token_a": "<hex token ID>",
                "token_b": "<hex token ID>",
                "fee_bps": 30,
                "tick_spacing": 60
            }
            '''

        def get_pool(self, payload):
            '''
            Look up an existing pool by token pair and fee.

            Payload: {
                "token_a": "<hex token ID>",
                "token_b": "<hex token ID>",
                "fee_bps": 30
            }
            '''


Part 5: Router Interface — PLANNED
====================================

The router provides a user-friendly interface for multi-hop swaps
and optimal routing across multiple pools.

    class IRouter:

        def swap_exact_in(self, payload):
            '''
            Swap an exact input amount through one or more pools.

            Payload: {
                "sender": "<hex address>",
                "path": ["<token_a_id>", "<token_b_id>", "<token_c_id>"],
                "fee_tiers": [30, 5],
                "amount_in": 1000000,
                "min_amount_out": 900000,
                "deadline": 1700000000
            }
            '''

        def swap_exact_out(self, payload):
            '''
            Swap to receive an exact output amount.

            Payload: {
                "sender": "<hex address>",
                "path": ["<token_a_id>", "<token_b_id>"],
                "fee_tiers": [30],
                "amount_out": 1000000,
                "max_amount_in": 1100000,
                "deadline": 1700000000
            }
            '''

        def add_liquidity_optimal(self, payload):
            '''
            Add liquidity with automatic ratio balancing.
            If the user provides only one token, the router swaps
            half to the other token, then adds liquidity.

            Payload: {
                "sender": "<hex address>",
                "pool": "<pool address>",
                "token": "<hex token ID>",
                "amount": 1000000
            }
            '''


Part 6: Validation Hooks — PLANNED
====================================

Transfer hooks (CIP-20 feature) that pools can register to enforce
invariants or collect protocol fees.

    def can_swap(pool_address, sender, token_in, amount_in):
        '''Called before a swap. Return False to reject.'''
        # Check blacklists, pause state, etc.
        return True

    def on_swap(pool_address, sender, token_in, amount_in, amount_out):
        '''Called after a swap. Used for protocol fee collection.'''
        protocol_fee = amount_out * PROTOCOL_FEE_BPS // 10000
        # Collect to treasury...

    def on_liquidity_change(pool_address, sender, amount_a, amount_b, is_add):
        '''Called after add/remove liquidity. Useful for LP incentives.'''
        pass


Part 7: Standard Fee Tiers
============================

    FEE_TIERS = {
        # bps    description          typical use case
        1:      "0.01%  —  stablecoins (USDC/USDT, DAI/USDC)",
        5:      "0.05%  —  correlated pairs (WETH/stETH)",
        30:     "0.30%  —  standard pairs (WETH/USDC)",
        100:    "1.00%  —  exotic/volatile pairs",
    }

    # V3 tick spacing by fee tier
    TICK_SPACING = {
        1:   1,
        5:   10,
        30:  60,
        100: 200,
    }


Part 8: Events
===============

Standard events emitted by CIP-21 pools:

    pool_initialized:
        token_a, token_b, lp_token, fee_bps

    liquidity_added:
        sender, amount_a, amount_b, lp_minted, reserve_a, reserve_b

    liquidity_removed:
        sender, lp_burned, amount_a, amount_b, reserve_a, reserve_b

    swap:
        sender, token_in, amount_in, amount_out, fee_bps,
        reserve_a, reserve_b

    reserves:
        token_a, token_b, reserve_a, reserve_b, lp_total_supply, fee_bps

    # V3-specific (planned)
    position_minted:
        sender, tick_lower, tick_upper, amount, liquidity

    position_burned:
        sender, tick_lower, tick_upper, amount

    fees_collected:
        sender, tick_lower, tick_upper, amount_a, amount_b


Implementation Notes
=====================

1. V2 pools use integer-only math. No floating point anywhere in the
   swap formula. The isqrt() function uses Newton's method.

2. MINIMUM_LIQUIDITY (1000 units) is locked on the first deposit to
   prevent the pool from being drained to zero. These LP tokens are
   minted to the pool's own address and can never be redeemed.

3. The pool actor is the owner of its LP token, so it has permission
   to mint and burn LP tokens. Regular users cannot mint/burn LP tokens
   directly — they must go through add_liquidity / remove_liquidity.

4. Token transfers use runtime.token_transfer_from() which requires
   prior approval (runtime.token_approve or `cowboy token approve`).
   This is the same approve+transferFrom pattern as ERC-20/Uniswap.

5. Reserves are tracked internally in actor storage because actors
   cannot read token balances of other actors. This means direct
   token transfers to the pool address will desync reserves.

6. The V2 fee formula applies the fee to the input amount:
     amount_in_with_fee = amount_in * (10000 - fee_bps)
     amount_out = (amount_in_with_fee * reserve_out) //
                  (reserve_in * 10000 + amount_in_with_fee)
   This is equivalent to Uniswap V2's 997/1000 ratio for a 30 bps fee.
"""
