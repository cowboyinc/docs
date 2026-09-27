---
title: "CIP-22: Continuous Clearing Auctions"
description: Fair token launches with per-block clearing and automatic LP seeding
icon: gavel
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-01-18
  **Requires:** CIP-1, CIP-5, CIP-20, CIP-21
</Note>

## Abstract

CIP-22 defines **Continuous Clearing Auctions (CCAs)** for Cowboy, a fair price discovery mechanism for token launches that eliminates sniping, timing games, and information asymmetry. Tokens are released gradually over time, with each block settling at a uniform clearing price. Upon auction completion, proceeds automatically seed a CIP-21 liquidity pool.

This design is inspired by [Uniswap's CCA mechanism](https://blog.uniswap.org/continuous-clearing-auctions), adapted for Cowboy's actor model with native timer integration for gas-efficient per-block clearing.

Key features:
- **Gradual price discovery**: Tokens released per-block according to a schedule
- **Uniform pricing**: All bidders in a block pay the same clearing price
- **Anti-sniping**: Early bidders achieve better average prices
- **Automatic liquidity**: Auction proceeds seed a V2 or V3 pool
- **Composable**: Validation hooks for KYC, geographic restrictions, etc.

---

## Motivation

### The Token Launch Problem

Traditional token launches suffer from well-known issues:

| Method | Problem |
|--------|---------|
| **Fixed-price sale** | Underpricing leaves value on table; overpricing fails |
| **Dutch auction** | Sniping at the last moment; poor price discovery |
| **Batch auction** | All-or-nothing; no gradual discovery |
| **AMM launch** | Bots front-run; massive volatility; MEV extraction |

These mechanisms create timing games where sophisticated actors extract value from regular participants.

### How CCAs Solve This

CCAs distribute tokens over time through continuous clearing:

1. **No sniping advantage**: Supply is released every block; last-minute bids get no special treatment
2. **Early bidder advantage**: Bids placed earlier fill across more blocks at (likely) lower average prices
3. **Uniform block pricing**: All bidders in a block pay the same price—no information asymmetry
4. **Gradual convergence**: Market has time to discover fair value

### Why CCAs on Cowboy?

Continuous Clearing Auctions require per-block processing—releasing tokens, sorting bids, calculating clearing prices. On Ethereum, this creates a **keeper dependency**: someone must submit a transaction each block to trigger clearing, paying gas and creating MEV opportunities.

#### The Keeper Problem on Ethereum

| Approach | Problem |
|----------|---------|
| **External keeper/bot** | Someone must pay gas; dependency on altruistic/MEV actors |
| **Lazy clearing on user action** | Clearing delayed until next bid; stale prices |
| **Bundled into user txs** | Users pay extra gas for clearing; unfair cost distribution |
| **Flashbots/MEV searchers** | Works but adds MEV middlemen; centralization risk |

Over a 7-day auction (~100,800 blocks), someone must pay for ~100,800 clearing transactions.

#### Cowboy's Timer Advantage

Cowboy's native timers (CIP-1/CIP-5) eliminate the keeper problem entirely:

| Aspect | Ethereum CCA | Cowboy CCA |
|--------|--------------|------------|
| **Clearing trigger** | External tx required | Native timer, automatic |
| **Cost** | ~50k gas per block (paid by someone) | Timer lane, no extra user cost |
| **Reliability** | Depends on keepers showing up | Guaranteed by protocol |
| **Missed blocks** | Possible if gas too high | Impossible (timer always fires) |
| **MEV surface** | Keeper selection is MEV opportunity | Deterministic execution |
| **Finalization** | Separate tx to seed LP | Atomic via timer callback |

#### Specific Benefits

1. **Zero Keeper Infrastructure**: No bots, no keeper networks, no gas subsidies. The auction actor schedules a timer once at creation; the protocol handles the rest.

2. **Guaranteed Execution**: Cowboy reserves a dedicated timer lane (`LANE_TIMER_CYCLES` = 8,888,890 cycles ≈ 11.1% of the 80,000,000-cycle block cap; CIP-1 / CIP-3) for timers. Even during extreme congestion, clearing executes. No "clearing got delayed because gas was too high."

3. **True Per-Block Clearing**: On Ethereum, if no one calls the clear function, clearing is deferred and bids see stale prices. Cowboy timers guarantee actual per-block clearing for smoother price discovery.

4. **Lower Total Cost**: Users only pay for their bid transactions. Clearing is "free" (uses reserved timer capacity, not user-paid gas).

5. **Atomic Graduation**: When the auction ends, a single timer callback checks the minimum raise, seeds the LP pool, and enables claims—all atomically, no separate finalization transaction.

6. **Reduced MEV Surface**: Timer execution is deterministic and protocol-ordered. No competition over "who gets to call clear," no sandwich opportunities around the clearing transaction.

This makes CCAs a natural fit for Cowboy—the mechanism works as designed without the infrastructure overhead required on keeper-dependent chains.

---

## Specification

### Overview

![CCA Lifecycle](https://storage.googleapis.com/second-petal-295822.appspot.com/elements/elements%3Af88fea5c6010d835c6e24a2e66c7abc47088d34b3677e069d0553869b0dd3aa5.png)

### Core Data Structures

#### Auction Configuration

```python
@dataclass
class AuctionConfig:
    # Token being sold
    token_id: bytes32               # CIP-20 platform token

    # Currency accepted (e.g., USDC, CBY)
    currency_id: bytes32            # CIP-20 platform token

    # Timing
    start_block: u64                # Auction begins
    end_block: u64                  # Auction ends
    claim_block: u64                # When claims open (>= end_block)

    # Pricing
    floor_price: u256               # Minimum price (Q96 format)
    tick_spacing: u24               # Price granularity (basis points)

    # Supply
    total_supply: u256              # Total tokens to sell
    release_schedule: list[ReleaseStep]  # Per-block release rates

    # Graduation
    min_currency_raised: u256       # Minimum to graduate
    target_currency_raised: u256    # Target (for UI)

    # Liquidity seeding
    pool_type: PoolType             # V2 or V3
    pool_fee_bps: u16               # Pool fee tier
    lp_recipient: address           # Who receives LP tokens/position

    # Optional
    validation_hook: address | None # For KYC, geographic restrictions
    metadata_uri: str | None        # Auction details

@dataclass
class ReleaseStep:
    rate_mps: u24                   # Tokens per block in milli-basis points (1e-7)
    duration_blocks: u40            # How many blocks at this rate

@enum
class PoolType:
    V2_FULL_RANGE = 1
    V3_CONCENTRATED = 2
```

#### Bid Structure

```python
@dataclass
class Bid:
    bid_id: u256
    bidder: address

    # Bid parameters
    currency_amount: u256           # Total spend budget
    max_price: u256                 # Maximum acceptable price (Q96)

    # Tracking
    currency_spent: u256            # How much has been used
    tokens_received: u256           # How much has been filled

    # Status
    created_block: u64
    is_withdrawn: bool
```

#### Auction State

```python
@dataclass
class AuctionState:
    # Current state
    current_block: u64
    current_clearing_price: u256    # Q96 format
    tokens_released: u256
    tokens_sold: u256
    currency_raised: u256

    # Active demand (sorted by max_price descending)
    active_bids: SortedMap[u256, list[Bid]]  # max_price -> bids
    total_active_demand: u256       # Sum of remaining bid budgets

    # Checkpoints for pro-rata calculation
    checkpoints: list[Checkpoint]

    # Status
    status: AuctionStatus

@dataclass
class Checkpoint:
    block: u64
    clearing_price: u256
    currency_raised_cumulative: u256
    tokens_sold_cumulative: u256

@enum
class AuctionStatus:
    PENDING = 0
    ACTIVE = 1
    ENDED = 2
    GRADUATED = 3
    FAILED = 4
```

---

### Auction Interface

```python
class ICCA:
    """Continuous Clearing Auction interface"""

    # ─────────────────────────────────────────────────────────
    # Bidding
    # ─────────────────────────────────────────────────────────

    def place_bid(
        self,
        currency_amount: u256,
        max_price: u256
    ) -> u256:
        """
        Place a bid in the auction.

        Args:
            currency_amount: Total spend budget
            max_price: Maximum price willing to pay (Q96 format)

        Returns:
            bid_id

        Requirements:
            - Auction is ACTIVE
            - max_price >= floor_price
            - max_price >= current_clearing_price (or bid is out-of-range)
            - currency_amount > 0

        Behavior:
            - Transfers currency_amount from bidder to auction
            - Bid is automatically spread across remaining blocks
            - If max_price < current_clearing_price, bid is "out of range"
              and can be withdrawn
        """

    def withdraw_bid(self, bid_id: u256) -> u256:
        """
        Withdraw an out-of-range bid.

        Requirements:
            - Caller is bid owner
            - Bid's max_price < current_clearing_price

        Returns:
            currency_amount refunded
        """

    def increase_bid(self, bid_id: u256, additional_amount: u256):
        """
        Add more currency to an existing bid.

        Requirements:
            - Caller is bid owner
            - Auction is ACTIVE
        """

    def update_max_price(self, bid_id: u256, new_max_price: u256):
        """
        Update bid's maximum price.

        Requirements:
            - Caller is bid owner
            - new_max_price >= floor_price
        """

    # ─────────────────────────────────────────────────────────
    # Claiming
    # ─────────────────────────────────────────────────────────

    def claim(self, bid_id: u256) -> (u256, u256):
        """
        Claim tokens from a bid after auction ends.

        Requirements:
            - block.height >= claim_block
            - Auction status is GRADUATED or FAILED

        Returns:
            (tokens_received, currency_refunded)

        Behavior:
            - If GRADUATED: receive tokens, refund unspent currency
            - If FAILED: refund all currency
        """

    def claim_batch(self, bid_ids: list[u256]) -> (u256, u256):
        """Claim multiple bids at once."""

    # ─────────────────────────────────────────────────────────
    # Queries
    # ─────────────────────────────────────────────────────────

    def get_config(self) -> AuctionConfig: ...
    def get_state(self) -> AuctionState: ...
    def get_bid(self, bid_id: u256) -> Bid: ...
    def get_bids_for_user(self, user: address) -> list[Bid]: ...
    def get_clearing_price(self) -> u256: ...
    def get_tokens_available(self) -> u256: ...
    def estimate_fill(self, currency_amount: u256, max_price: u256) -> u256: ...
```

---

### Clearing Mechanism

The core innovation of CCAs is per-block clearing. Each block:

1. **Release tokens** according to schedule
2. **Sort active bids** by max_price (descending)
3. **Fill bids** from highest to lowest until supply exhausted
4. **Set clearing price** to the marginal bid's max_price
5. **Pro-rata fill** any bids at exactly the clearing price

#### Clearing Algorithm

```python
def clear_block(self):
    """Called by timer each block during auction."""

    if block.height < self.config.start_block:
        return
    if block.height > self.config.end_block:
        self._finalize_auction()
        return

    # 1. Calculate tokens to release this block
    tokens_to_release = self._get_release_amount(block.height)
    self.state.tokens_released += tokens_to_release

    # 2. Get active demand at or above floor
    active_bids = self._get_active_bids_sorted()  # Sorted by max_price DESC

    if len(active_bids) == 0:
        # No demand: price stays at floor, tokens accumulate
        self.state.current_clearing_price = self.config.floor_price
        return

    # 3. Calculate clearing price and fills
    tokens_remaining = tokens_to_release
    clearing_price = self.config.floor_price
    fills = []

    for price_tier, bids_at_tier in active_bids.items():
        if tokens_remaining == 0:
            break

        # Calculate demand at this price tier
        demand_at_tier = sum(
            self._remaining_demand(bid, price_tier)
            for bid in bids_at_tier
        )

        # How many tokens can be sold at this tier?
        tokens_at_tier = min(tokens_remaining, demand_at_tier)

        if tokens_at_tier == demand_at_tier:
            # Fully fill all bids at this tier
            for bid in bids_at_tier:
                fill_amount = self._remaining_demand(bid, price_tier)
                fills.append((bid, fill_amount, price_tier))
            tokens_remaining -= tokens_at_tier
        else:
            # Pro-rata fill at this tier (marginal price)
            clearing_price = price_tier
            for bid in bids_at_tier:
                bid_demand = self._remaining_demand(bid, price_tier)
                pro_rata_share = bid_demand * tokens_at_tier // demand_at_tier
                fills.append((bid, pro_rata_share, price_tier))
            tokens_remaining = 0
            break

        clearing_price = price_tier

    # 4. Execute fills
    for (bid, token_amount, price) in fills:
        currency_cost = token_amount * price // Q96
        bid.currency_spent += currency_cost
        bid.tokens_received += token_amount
        self.state.tokens_sold += token_amount
        self.state.currency_raised += currency_cost

    # 5. Update state
    self.state.current_clearing_price = clearing_price
    self.state.current_block = block.height

    # 6. Checkpoint for pro-rata claims
    self.state.checkpoints.append(Checkpoint(
        block=block.height,
        clearing_price=clearing_price,
        currency_raised_cumulative=self.state.currency_raised,
        tokens_sold_cumulative=self.state.tokens_sold
    ))

    emit_event("BlockCleared", {
        "block": block.height,
        "tokens_released": tokens_to_release,
        "tokens_sold": sum(f[1] for f in fills),
        "clearing_price": clearing_price,
        "currency_raised": self.state.currency_raised
    })
```

#### Remaining Demand Calculation

```python
def _remaining_demand(self, bid: Bid, price: u256) -> u256:
    """
    Calculate how many tokens a bid can still purchase at given price.

    The bid's budget is spread across remaining blocks.
    """
    if bid.max_price < price:
        return 0  # Bid is out of range

    remaining_currency = bid.currency_amount - bid.currency_spent
    # Inclusive of the current block: on the final block (height == end_block)
    # this is 1, not 0 — avoiding a division-by-zero and the off-by-one that
    # would otherwise under-count the current block.
    remaining_blocks = self.config.end_block - block.height + 1

    # Currency available this block (spread evenly)
    currency_this_block = remaining_currency // remaining_blocks

    # Convert to token demand at this price
    token_demand = currency_this_block * Q96 // price

    return token_demand
```

---

### Release Schedules

The release schedule controls how tokens are distributed over time. The clearing loop calls `_get_release_amount(height)` to resolve the per-block rate from the active `ReleaseStep`:

```python
def _get_release_amount(self, height: u64) -> u256:
    """Tokens released at `height`, per the active ReleaseStep.
    rate_mps is tokens-per-block in milli-basis-points (1e-7) of total_supply."""
    offset = height - self.config.start_block
    cursor = 0
    for step in self.config.release_schedule:
        if offset < cursor + step.duration_blocks:
            return self.config.total_supply * step.rate_mps // 10_000_000
        cursor += step.duration_blocks
    return 0  # schedule exhausted
```

Common patterns:

#### Constant Release

```python
# Release 1M tokens over 1000 blocks = 1000 tokens/block
release_schedule = [
    ReleaseStep(rate_mps=1_000_000, duration_blocks=1000)
]
```

#### Front-loaded (Faster Start)

```python
# Higher release early, tapering off
release_schedule = [
    ReleaseStep(rate_mps=2_000_000, duration_blocks=250),   # 2x first quarter
    ReleaseStep(rate_mps=1_000_000, duration_blocks=500),   # Normal middle
    ReleaseStep(rate_mps=500_000, duration_blocks=250),     # 0.5x final quarter
]
```

#### Back-loaded (Slower Start)

```python
# Lower release early, accelerating
release_schedule = [
    ReleaseStep(rate_mps=500_000, duration_blocks=250),
    ReleaseStep(rate_mps=1_000_000, duration_blocks=500),
    ReleaseStep(rate_mps=2_000_000, duration_blocks=250),
]
```

---

### Graduation and Liquidity Seeding

When the auction ends successfully:

```python
def _finalize_auction(self):
    """Called after end_block. Idempotent: both the clear_block post-end path
    and any dedicated finalize timer may invoke it in the same block (CIP-5
    fires all due timers FIFO in one block), so guard against double-seeding
    the LP / double-emitting graduation."""

    if self.state.status in (AuctionStatus.GRADUATED, AuctionStatus.FAILED):
        return  # already finalized

    if self.state.currency_raised < self.config.min_currency_raised:
        # Failed: refund everyone
        self.state.status = AuctionStatus.FAILED
        emit_event("AuctionFailed", {
            "currency_raised": self.state.currency_raised,
            "min_required": self.config.min_currency_raised
        })
        return

    # Success: seed liquidity pool
    self.state.status = AuctionStatus.GRADUATED

    final_price = self.state.current_clearing_price
    unsold_tokens = self.state.tokens_released - self.state.tokens_sold
    currency_for_lp = self.state.currency_raised
    tokens_for_lp = unsold_tokens  # Or could be a configured portion

    if self.config.pool_type == PoolType.V2_FULL_RANGE:
        # Seed V2 pool. CIP-21 §Part 4 exposes get_* and create_* separately
        # (no get_or_create_*); look up, create if absent.
        # FLAG (author): CIP-21 get_/create_ return an `address`; the calls
        # below treat `pool` as a handle. Wrap as Pool(pool).<method>(...) once
        # the factory return type is confirmed.
        pool = Factory.get_v2_pool(self.config.token_id, self.config.currency_id,
                                   self.config.pool_fee_bps)
        if pool == address(0):
            pool = Factory.create_v2_pool(self.config.token_id, self.config.currency_id,
                                          self.config.pool_fee_bps)

        Token.approve(self.config.token_id, pool.address, tokens_for_lp)
        Token.approve(self.config.currency_id, pool.address, currency_for_lp)

        # SECURITY: `0, 0` mins are only safe when the pool is freshly created
        # and empty. If the pool can pre-exist (get-then-create above), an
        # attacker can pre-seed a skewed ratio before graduation and siphon LP
        # value. An implementation MUST either assert get_reserves() == (0, 0)
        # here, or derive non-zero amount_*_min from the final clearing price.
        (amount_a, amount_b, lp_tokens) = pool.add_liquidity(
            tokens_for_lp,
            currency_for_lp,
            0, 0,  # see SECURITY note above — must be guarded in production
            self.config.lp_recipient
        )

    elif self.config.pool_type == PoolType.V3_CONCENTRATED:
        # Seed V3 pool with concentrated position around final price.
        # get_v3_pool takes (token_a, token_b, fee_bps); create_v3_pool
        # additionally takes tick_spacing (CIP-21 §Part 4). Same address-vs-
        # handle and empty-pool SECURITY caveats as the V2 branch above apply.
        pool = Factory.get_v3_pool(self.config.token_id, self.config.currency_id,
                                   self.config.pool_fee_bps)
        if pool == address(0):
            pool = Factory.create_v3_pool(self.config.token_id, self.config.currency_id,
                                          self.config.pool_fee_bps, tick_spacing=60)

        # Full range position
        tick_lower = -887220  # MIN_TICK
        tick_upper = 887220   # MAX_TICK

        position_id = pool.mint_position(
            tick_lower,
            tick_upper,
            tokens_for_lp,
            self.config.lp_recipient
        )

    emit_event("AuctionGraduated", {
        "final_price": final_price,
        "currency_raised": self.state.currency_raised,
        "tokens_sold": self.state.tokens_sold,
        "pool": pool.address,
        "lp_recipient": self.config.lp_recipient
    })
```

---

### Validation Hooks

CCAs support validation hooks for compliance:

```python
class ICCAHook:
    """Validation hook interface for CCAs"""

    def can_bid(
        self,
        auction: address,
        bidder: address,
        currency_amount: u256,
        max_price: u256
    ) -> bool:
        """
        Called before accepting a bid.

        Use for: KYC verification, geographic restrictions,
                 accredited investor checks, bid limits.
        """

    def on_bid(
        self,
        auction: address,
        bidder: address,
        bid_id: u256,
        currency_amount: u256,
        max_price: u256
    ) -> None:
        """Called after bid is placed. For analytics."""

    def can_claim(
        self,
        auction: address,
        bidder: address,
        bid_id: u256
    ) -> bool:
        """Called before allowing claim. For vesting, lockups."""
```

#### Example: KYC Hook

```python
class KYCAuctionHook(Actor):
    """Only verified users can bid"""

    def init(self, kyc_registry: address):
        self.kyc_registry = kyc_registry

    def can_bid(self, auction, bidder, currency_amount, max_price) -> bool:
        return KYCRegistry(self.kyc_registry).is_verified(bidder)

    def on_bid(self, auction, bidder, bid_id, currency_amount, max_price):
        emit_event("KYCBidPlaced", {"bidder": bidder, "bid_id": bid_id})

    def can_claim(self, auction, bidder, bid_id) -> bool:
        # Always allow claim (can't trap funds)
        return True
```

#### Example: Geographic Restriction Hook

```python
class GeoRestrictedHook(Actor):
    """Block certain jurisdictions"""

    def init(self, blocked_countries: list[str], attestation_registry: address):
        self.blocked_countries = set(blocked_countries)
        self.attestation_registry = attestation_registry

    def can_bid(self, auction, bidder, currency_amount, max_price) -> bool:
        attestation = AttestationRegistry(self.attestation_registry).get(bidder)
        if attestation is None:
            return False
        return attestation.country not in self.blocked_countries
```

---

### Timer Integration

> **Same-Block Constraint (CIP-5):** Timers created in the current block cannot fire in the same block. Because the first clearing timer is armed for `config.start_block` from within `init` (which runs in an earlier block), the first clearing fires on schedule; a timer armed for the *current* block would be delayed one block.

Cowboy's native timers enable automatic per-block clearing:

```python
class ContinuousClearingAuction(Actor):
    """CCA implementation with timer-based clearing"""

    def init(self, config: AuctionConfig):
        self.config = config
        self.state = AuctionState(...)

        # NOTE: CIP-5 timers are one-shot and height-based — there is no
        # `trigger_type`/`interval`/`start_height`/`end_height`/`due_height`/
        # `handler` kwarg. The real signature is schedule_timer(height, payload)
        # (see CIP-5 §4.1). We arm the first clearing timer here; `clear_block`
        # re-arms itself for the next block until `end_block`, after which its
        # `block.height > end_block` branch runs `_finalize_auction` once (which
        # is idempotent, so a separate finalize timer is unnecessary). Handler
        # routing and args go through the payload JSON convention (CIP-5 §4.2).
        self.schedule_timer(
            config.start_block,
            b'{"_handler": "clear_block", "_payload": ""}'
        )

    def handle_timer(self, payload: bytes):
        """Timer callback (CIP-5 default handler). Dispatches on _handler."""
        # ... clearing logic as above (the `block.height > end_block` branch
        #     calls self._finalize_auction(), which is idempotent) ...

        # Re-arm for the next block while the auction is still live. One-shot
        # timers require the handler to reschedule itself (CIP-5 §4.2 / App B).
        if block.height <= self.config.end_block:
            self.schedule_timer(
                block.height + 1,
                b'{"_handler": "clear_block", "_payload": ""}'
            )
    # NOTE: no separate `finalize` callback/timer is needed — the post-end fire
    # of the re-armed clearing timer invokes the idempotent _finalize_auction().
```

---

### Factory

```python
class ICCAFactory:
    """Factory for creating CCAs"""

    def create_auction(
        self,
        token_id: bytes32,
        currency_id: bytes32,
        total_supply: u256,
        floor_price: u256,
        start_block: u64,
        end_block: u64,
        release_schedule: list[ReleaseStep],
        min_currency_raised: u256,
        pool_type: PoolType,
        pool_fee_bps: u16,
        lp_recipient: address,
        validation_hook: address | None = None,
        metadata_uri: str | None = None
    ) -> address:
        """
        Create a new CCA.

        Requirements:
            - Caller has approval to transfer total_supply of token_id
            - end_block > start_block
            - release_schedule covers full duration

        Returns:
            Auction actor address
        """

    def create_auction_with_token(
        self,
        name: str,
        symbol: str,
        total_supply: u256,
        currency_id: bytes32,
        floor_price: u256,
        start_block: u64,
        end_block: u64,
        release_schedule: list[ReleaseStep],
        min_currency_raised: u256,
        pool_type: PoolType,
        pool_fee_bps: u16,
        lp_recipient: address,
        validation_hook: address | None = None
    ) -> (bytes32, address):
        """
        Create a new token and CCA in one transaction.

        Returns:
            (token_id, auction_address)
        """
```

---

### Events

```python
# Auction lifecycle
AuctionCreated(auction: address, token: bytes32, currency: bytes32, config: AuctionConfig)
AuctionStarted(auction: address, start_block: u64)
AuctionEnded(auction: address, end_block: u64)
AuctionGraduated(auction: address, final_price: u256, currency_raised: u256, pool: address)
AuctionFailed(auction: address, currency_raised: u256, min_required: u256)

# Per-block clearing
BlockCleared(auction: address, block: u64, tokens_released: u256, tokens_sold: u256, clearing_price: u256, currency_raised: u256)

# Bidding
BidPlaced(auction: address, bidder: address, bid_id: u256, currency_amount: u256, max_price: u256)
BidWithdrawn(auction: address, bidder: address, bid_id: u256, currency_refunded: u256)
BidIncreased(auction: address, bid_id: u256, additional_amount: u256)
BidPriceUpdated(auction: address, bid_id: u256, old_price: u256, new_price: u256)

# Claims
TokensClaimed(auction: address, bidder: address, bid_id: u256, tokens: u256, currency_refunded: u256)
```

---

## Security Considerations

### Price Manipulation

- **TWAP resistance**: Clearing price is demand-weighted, not spot-manipulable
- **No flash loans**: Bids are non-withdrawable while in range
- **Gradual discovery**: Manipulation must be sustained across many blocks

### Bid Griefing

- **Out-of-range withdrawal**: Bids below clearing price can always be withdrawn
- **No trapped funds**: Failed auctions refund all currency
- **Gas-efficient claims**: Batch claiming supported

### Hook Security

- **Hooks capped at 50,000 Cycles and 50,000 Cells** (same as CIP-20/CIP-21)
- **can_claim must return true** for funds to be claimable (hooks should not trap funds). A FAILED auction MUST refund all bids **unconditionally, bypassing `can_claim`** (anti-trap requirement); a GRADUATED auction MAY gate token delivery on `can_claim` for vesting, but the implementation SHOULD expose an owner/governance override so a stuck hook can never permanently freeze principal
- **Timelock recommended** for hook updates

### Integer Precision

- **Q96 format** for prices (matches Uniswap V3)
- **Pro-rata rounding**: Rounds down (conservative for users)
- **Overflow checks**: Max supply capped at 1e30 wei

---

## Rationale

### Why Not Dutch Auction?

Dutch auctions suffer from:
- **Sniping**: Everyone waits for the last moment
- **Winner's curse**: Early bidders overpay
- **All-or-nothing**: Single clearing moment

CCAs distribute over time, rewarding early participation.

### Why Not Batch Auction?

Batch auctions are good but:
- **Single clearing**: No gradual discovery
- **Timing games**: Coordinate around deadline
- **Less information**: Market can't observe demand curve

CCAs reveal demand continuously.

### Why Timer-Based Clearing?

Cowboy's native timers enable:
- **No keeper costs**: Clearing is automatic
- **Deterministic execution**: Same result on all nodes
- **Gas efficiency**: Timer execution uses reserved block capacity

---

## Example: Token Launch

```python
from cowboy_sdk import Token, CCAFactory

# 1. Create token
my_token = Token.create(
    name="My Protocol Token",
    symbol="MPT",
    decimals=18,
    initial_supply=100_000_000 * 10**18  # 100M tokens
)

# 2. Configure auction (sell 50% of supply)
auction_supply = 50_000_000 * 10**18

release_schedule = [
    # Constant release over the full 100,800-block auction window (7 days at ~6s blocks)
    ReleaseStep(rate_mps=500_000, duration_blocks=100_800)
]

# 3. Create auction
auction = CCAFactory.create_auction(
    token_id=my_token,
    currency_id=USDC,
    total_supply=auction_supply,
    floor_price=to_q96(0.10),  # $0.10 floor
    start_block=block.height + 100,  # Start in ~10 minutes
    end_block=block.height + 100 + 100_800,  # 7 days
    release_schedule=release_schedule,
    min_currency_raised=1_000_000 * 10**6,  # $1M minimum
    pool_type=PoolType.V2_FULL_RANGE,
    pool_fee_bps=30,  # 0.30% fee
    lp_recipient=TREASURY,
    metadata_uri="ipfs://..."
)

# 4. Transfer tokens to auction
Token.transfer(my_token, auction.address, auction_supply)
```

---

## Backwards Compatibility

This is a new standard. No backwards compatibility concerns.

---

## Reference Implementation

See `examples/cca/` for reference implementation.

---

## Acknowledgments

This design is inspired by [Uniswap's Continuous Clearing Auctions](https://blog.uniswap.org/continuous-clearing-auctions) and their [Liquidity Launchpad](https://docs.uniswap.org/contracts/liquidity-launchpad/Overview).
