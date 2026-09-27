"""Off-chain interface of the venue signer.

Plain Python. Each exported function takes an order the actor has already
priced and sized from public venue data, reads the venue key from the
environment through the proposed CIP-10 `RuntimeConfig.secret_env` field
(cowboy-protocol#127, specified by cowboy#390), signs the order OFFLINE, and
returns the exact HTTP request to send. The support train must land before
this example is runnable. Nothing here talks to a network: the actor sends the
returned request with `runner.http`, so this image needs no egress policy and
the runner never learns a venue's signing scheme.

One function per venue. Hyperliquid today; Gemini (HMAC-SHA384 over a base64
payload with a nonce) is the next one and follows the same shape.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from eth_account import Account
from hyperliquid.utils.signing import (
    get_timestamp_ms,
    order_wires_to_order_action,
    sign_l1_action,
)

HYPERLIQUID_KEY_ENV = "HYPERLIQUID_PRIVATE_KEY"
HYPERLIQUID_MAINNET_URL = "https://api.hyperliquid.xyz"
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

# Hyperliquid price rule: at most 5 significant figures, and at most
# `MAX_DECIMALS - sz_decimals` decimals (6 for perps, 8 for spot). Integer
# prices are always allowed.
_PERP_MAX_DECIMALS = 6
_SPOT_MAX_DECIMALS = 8
# Widest snapping tolerance a caller may ask for, in basis points. The
# guard is inert at 100% (a floored size is always strictly less than one
# requested size away, so nothing would ever be refused); a few hundred bps
# keeps a reduce-only close from silently signing for a fraction of the
# position.
_MAX_ROUNDING_BPS = 500
SUPPORTED_TIFS = ("Ioc", "Gtc", "Alo")
# Asset indexes are small non-negative integers from `/info` meta; anything
# past this is not an index the venue could have handed out.
_MAX_ASSET = 1_000_000


@dataclass(frozen=True)
class HyperliquidOrder:
    """An order the actor has already decided on.

    `asset`, `sz_decimals`, and `is_spot` come from the venue's `/info`
    metadata (core perps: the coin's index in `universe`; spot: 10000 plus
    its index in `spotMeta`; builder-deployed perps use their dex-specific
    high asset indexes). The actor fetches that metadata unauthenticated with
    `runner.http`. The asset index is the only market identifier the venue
    signs over; `is_spot` is carried solely to select the correct precision
    rule without misclassifying builder-deployed perps. `mainnet` picks the venue host and the
    phantom-agent `source` the EIP-712 signature commits to (`a` mainnet,
    `b` testnet; Hyperliquid's domain chainId is 1337 on both), and has no
    default.

    Price and size are snapped to what the venue accepts in the direction
    that can only make the order LESS aggressive than asked: a buy's price
    rounds down and a sell's up, size always rounds down. If snapping moves
    either by more than `max_rounding_bps` (an integer from 0 through 500,
    default 50) the order is refused instead of signed, so a caller learns its
    inputs were off the venue's grid rather than discovering a fill at a price
    it never asked for.

    `nonce` is what Hyperliquid de-duplicates per key: it must be unique
    among the key's recent orders and within about a day of the venue's
    clock, in milliseconds. `None` uses this box's clock, which is enough
    for one sequential signer per key (the demo desk's `trade` path); two
    signers sharing a key must supply distinct nonces themselves.

    `expires_after` is a venue-time millisecond deadline the signature commits
    to: signing and sending happen in different processes with the blob held
    in actor state in between, so without it a delayed send, a crash retry,
    or a replayed container result fires at a stale price for about a day.
    `None` leaves the order valid for as long as the venue accepts the nonce.
    """

    asset: int
    is_buy: bool
    size: float
    limit_px: float
    sz_decimals: int
    is_spot: bool
    mainnet: bool
    reduce_only: bool = False
    tif: str = "Ioc"
    max_rounding_bps: int = 50
    nonce: int | None = None
    expires_after: int | None = None


@dataclass(frozen=True)
class SignedRequest:
    """The request to send, verbatim, and what it commits to.

    `body` is JSON text. `price` and `size` are the values actually signed
    (after snapping), so the actor records the fill it asked for, not the
    one it hoped for. `asset`, `is_buy`, `reduce_only` and `tif` restate the
    order's commitments so a caller can audit what it is about to send
    without re-parsing the body.
    """

    url: str
    method: str
    headers: dict[str, str]
    body: str
    nonce: int
    price: float
    size: float
    asset: int
    is_buy: bool
    reduce_only: bool
    tif: str
    expires_after: int | None


def sign_hyperliquid_order(order: HyperliquidOrder) -> SignedRequest:
    """Sign one Hyperliquid order with the key in `HYPERLIQUID_PRIVATE_KEY`."""
    key = os.environ.get(HYPERLIQUID_KEY_ENV)
    if not key:
        raise RuntimeError(f"{HYPERLIQUID_KEY_ENV} is not set")
    for field, value in (
        ("mainnet", order.mainnet),
        ("is_spot", order.is_spot),
        ("is_buy", order.is_buy),
        ("reduce_only", order.reduce_only),
    ):
        if type(value) is not bool:
            raise ValueError(f"{field} must be a bool, got {value!r}")
    if order.tif not in SUPPORTED_TIFS:
        raise ValueError(f"tif must be one of {', '.join(SUPPORTED_TIFS)}, got {order.tif!r}")
    if type(order.asset) is not int or not 0 <= order.asset < _MAX_ASSET:
        raise ValueError(f"asset must be an index in 0..{_MAX_ASSET}, got {order.asset!r}")
    spot = order.is_spot
    max_decimals = _SPOT_MAX_DECIMALS if spot else _PERP_MAX_DECIMALS
    if type(order.sz_decimals) is not int or not 0 <= order.sz_decimals <= max_decimals:
        raise ValueError(f"sz_decimals must be 0..{max_decimals}, got {order.sz_decimals!r}")
    # `nan <= 0` and `inf <= 0` are both false, so malformed or non-finite
    # upstream values need an explicit check at the signing boundary.
    if not math.isfinite(order.size) or not math.isfinite(order.limit_px):
        raise ValueError("size and limit_px must be finite")
    if order.size <= 0 or order.limit_px <= 0:
        raise ValueError("size and limit_px must be positive")
    if (
        type(order.max_rounding_bps) is not int
        or not 0 <= order.max_rounding_bps <= _MAX_ROUNDING_BPS
    ):
        raise ValueError(
            f"max_rounding_bps must be an integer in 0..{_MAX_ROUNDING_BPS}, "
            f"got {order.max_rounding_bps!r}"
        )

    requested_size = Decimal(repr(order.size))
    requested_price = Decimal(repr(order.limit_px))
    size_decimal = _round_size_decimal(requested_size, order.sz_decimals)
    if size_decimal <= 0:
        raise ValueError("size rounds to zero at the asset's sz_decimals")
    price_decimal = _round_price_decimal(
        requested_price,
        order.sz_decimals,
        spot,
        order.is_buy,
    )
    if price_decimal <= 0:
        raise ValueError("price rounds to zero at the venue's precision")
    tolerance = Decimal(order.max_rounding_bps) / Decimal(10_000)
    if abs(size_decimal - requested_size) / requested_size > tolerance:
        raise ValueError(
            f"size {order.size} snaps to {size_decimal} at sz_decimals={order.sz_decimals}, "
            f"more than {order.max_rounding_bps} bps away"
        )
    if abs(price_decimal - requested_price) / requested_price > tolerance:
        raise ValueError(
            f"limit_px {order.limit_px} snaps to {price_decimal} at the venue's precision, "
            f"more than {order.max_rounding_bps} bps away"
        )

    # Build the SDK's order-wire shape with canonical decimal strings. Passing
    # these values through float would let binary rounding add off-grid decimal
    # places (or even move a floored size above the requested Decimal value).
    wire = {
        "a": order.asset,
        "b": order.is_buy,
        "p": _decimal_to_wire(price_decimal),
        "s": _decimal_to_wire(size_decimal),
        "r": order.reduce_only,
        "t": {"limit": {"tif": order.tif}},
    }
    action = order_wires_to_order_action([wire])
    nonce = get_timestamp_ms() if order.nonce is None else order.nonce
    if type(nonce) is not int or nonce <= 0:
        raise ValueError("nonce must be a positive integer of milliseconds")
    expires_after = order.expires_after
    if expires_after is not None and (
        type(expires_after) is not int or expires_after <= nonce
    ):
        raise ValueError("expires_after must be an integer of milliseconds after the nonce")
    wallet = Account.from_key(key)
    # `expires_after` enters the action hash, so the signature commits to it.
    signature = sign_l1_action(wallet, action, None, nonce, expires_after, order.mainnet)
    payload = {
        "action": action,
        "nonce": nonce,
        "signature": signature,
        "vaultAddress": None,
        "expiresAfter": expires_after,
    }
    base = HYPERLIQUID_MAINNET_URL if order.mainnet else HYPERLIQUID_TESTNET_URL
    return SignedRequest(
        url=f"{base}/exchange",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")),
        nonce=nonce,
        price=float(price_decimal),
        size=float(size_decimal),
        asset=order.asset,
        is_buy=order.is_buy,
        reduce_only=order.reduce_only,
        tif=order.tif,
        expires_after=expires_after,
    )


def round_size(size: float, sz_decimals: int) -> float:
    """Snap size DOWN to `sz_decimals`: never buy, sell, or close more than asked.

    Non-positive and non-finite sizes snap to 0.0, the same shape `round_price`
    gives a bad price, so pre-trade display code sees one failure value."""
    if size <= 0 or not math.isfinite(size):
        return 0.0
    return float(_round_size_decimal(Decimal(repr(size)), sz_decimals))


def round_price(px: float, sz_decimals: int, spot: bool, is_buy: bool) -> float:
    """Snap a price onto Hyperliquid's grid (5 significant figures, capped
    decimals) in the direction that cannot make the order more aggressive:
    a buy rounds down, a sell rounds up."""
    if px <= 0 or not math.isfinite(px):
        return 0.0
    return float(_round_price_decimal(Decimal(repr(px)), sz_decimals, spot, is_buy))


def _round_size_decimal(size: Decimal, sz_decimals: int) -> Decimal:
    return _quantize(size, sz_decimals, ROUND_FLOOR)


def _round_price_decimal(
    px_decimal: Decimal,
    sz_decimals: int,
    spot: bool,
    is_buy: bool,
) -> Decimal:
    max_decimals = (_SPOT_MAX_DECIMALS if spot else _PERP_MAX_DECIMALS) - sz_decimals
    # Decimal.adjusted() gives the base-10 exponent of the most-significant
    # digit without the boundary errors float log10 has just below powers of
    # ten (for example 99.99999999999999). Keeping four digits after that
    # exponent yields at most five significant figures.
    sig = 4 - px_decimal.adjusted()
    decimals = max(0, min(sig, max_decimals))
    rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
    return _quantize(px_decimal, decimals, rounding)


def _quantize(value: Decimal, decimals: int, rounding: str) -> Decimal:
    """Quantize without the default Decimal context truncating large values."""
    quantum = Decimal(1).scaleb(-decimals)
    integer_digits = max(1, value.adjusted() + 1)
    with localcontext() as context:
        context.prec = max(28, integer_digits + decimals + 2)
        return value.quantize(quantum, rounding=rounding)


def _decimal_to_wire(value: Decimal) -> str:
    """Canonical fixed-point wire text: no exponent and no redundant zeroes."""
    wire = format(value, "f")
    if "." in wire:
        wire = wire.rstrip("0").rstrip(".")
    return "0" if wire == "-0" else wire
