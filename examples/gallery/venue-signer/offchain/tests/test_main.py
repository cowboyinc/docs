"""Plain pytest over plain Python. Never copied into the image."""

import json
import time
from decimal import Decimal

import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from main import (
    HYPERLIQUID_KEY_ENV,
    HYPERLIQUID_MAINNET_URL,
    HYPERLIQUID_TESTNET_URL,
    SUPPORTED_TIFS,
    HyperliquidOrder,
    round_price,
    round_size,
    sign_hyperliquid_order,
)

KEY = "0x" + "11" * 32
FIXED_NONCE = 1_700_000_000_000

# Frozen r/s/v for `_order(nonce=FIXED_NONCE)` under hyperliquid-python-sdk
# 0.24.0. Every other signature assertion is a sign-then-recover round trip,
# which stays green if the SDK moves signing and recovery together (a new
# EIP-712 domain, `verifyingContract`, phantom-agent source letters, or
# msgpack action hash). These vectors pin the bytes the venue actually
# verifies; an SDK bump that changes them is a change to what gets signed.
FROZEN_SIGNATURES = {
    True: {
        "r": "0xd89fe9e06abec78ebf01ceae1481b83aee9b4c5776042856810deb5457fa5bd5",
        "s": "0x3b5c75352ae447c5d037eba952314aef0c3140cbf01e366db3c9d5e67fc2578a",
        "v": 27,
    },
    False: {
        "r": "0x4ee33ccae417bf6124890b9786e44fef757deea8952cb7b74f36c3090329a383",
        "s": "0x4b15b952538923437928426ac9dee28ca906922976ab9f859084a533cf73c557",
        "v": 28,
    },
}


def _order(**overrides) -> HyperliquidOrder:
    base = dict(asset=0, is_buy=True, size=0.0123456, limit_px=78_583.99, sz_decimals=5,
                is_spot=False, mainnet=True)
    base.update(overrides)
    return HyperliquidOrder(**base)


def _recover(req, mainnet, expires_after=None):
    body = json.loads(req.body)
    return recover_agent_or_user_from_l1_action(
        body["action"], body["signature"], None, body["nonce"], expires_after, mainnet
    ).lower()


def test_signature_recovers_to_the_key_in_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order())
    assert req.method == "POST"
    assert req.url == "https://api.hyperliquid.xyz/exchange"
    assert HYPERLIQUID_MAINNET_URL == "https://api.hyperliquid.xyz"
    assert HYPERLIQUID_TESTNET_URL == "https://api.hyperliquid-testnet.xyz"
    body = json.loads(req.body)
    assert body["nonce"] == req.nonce
    assert body["vaultAddress"] is None and body["expiresAfter"] is None
    assert req.headers == {"Content-Type": "application/json"}
    assert _recover(req, True) == Account.from_key(KEY).address.lower()
    # Wire shape: size snapped DOWN to sz_decimals, buy price snapped DOWN to
    # 5 sig figs, IOC limit — and the request reports what it signed.
    (wire,) = body["action"]["orders"]
    assert wire == {"a": 0, "b": True, "p": "78583", "s": "0.01234", "r": False,
                    "t": {"limit": {"tif": "Ioc"}}}
    # A single plain order: no TP/SL grouping, no builder fee recipient.
    assert body["action"] == {"type": "order", "orders": [wire], "grouping": "na"}
    assert (req.price, req.size) == (78_583.0, 0.01234)
    # The request restates the order's commitments without re-parsing the body.
    assert (req.asset, req.is_buy, req.reduce_only, req.tif) == (0, True, False, "Ioc")
    assert req.expires_after is None
    # The key never appears in what the actor sends.
    assert KEY[2:] not in req.body and KEY[2:] not in json.dumps(req.headers)


def test_testnet_targets_the_testnet_host(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(mainnet=False))
    assert req.url == "https://api.hyperliquid-testnet.xyz/exchange"
    assert _recover(req, False) == Account.from_key(KEY).address.lower()


@pytest.mark.parametrize("mainnet", [True, False])
def test_signature_matches_the_frozen_vector(monkeypatch, mainnet) -> None:
    """Pins the exact bytes, not just a round trip through the SDK: the EIP-712
    domain (chainId 1337, zero verifyingContract), the phantom-agent source
    (`a` mainnet, `b` testnet) and the msgpack action hash all feed these."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(mainnet=mainnet, nonce=FIXED_NONCE))
    assert json.loads(req.body)["signature"] == FROZEN_SIGNATURES[mainnet]
    # The two networks sign different bytes for the same order and nonce.
    assert FROZEN_SIGNATURES[True] != FROZEN_SIGNATURES[False]


def test_expires_after_is_signed_into_the_order(monkeypatch) -> None:
    """A deadline the venue enforces: the signature commits to it, so a replay
    that strips it does not recover to the key, and a nonce-or-earlier deadline
    is refused."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    deadline = FIXED_NONCE + 60_000
    req = sign_hyperliquid_order(_order(nonce=FIXED_NONCE, expires_after=deadline))
    body = json.loads(req.body)
    assert body["expiresAfter"] == deadline and req.expires_after == deadline
    assert _recover(req, True, deadline) == Account.from_key(KEY).address.lower()
    assert _recover(req, True, None) != Account.from_key(KEY).address.lower()
    for bad in (FIXED_NONCE, FIXED_NONCE - 1, True, 1.0, "1"):
        with pytest.raises(ValueError, match="expires_after"):
            sign_hyperliquid_order(_order(nonce=FIXED_NONCE, expires_after=bad))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("mainnet", "false"),
        ("mainnet", 1),
        ("is_spot", "false"),
        ("is_spot", 0),
        ("is_buy", "true"),
        ("is_buy", 1),
        ("reduce_only", "no"),
        ("reduce_only", 2),
    ],
)
def test_control_fields_require_real_booleans(monkeypatch, field, bad) -> None:
    """Strings and integers must not cross network or order safety boundaries."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    with pytest.raises(ValueError, match=field):
        sign_hyperliquid_order(_order(**{field: bad}))


def test_network_is_required_and_the_readme_call_shape_targets_testnet(monkeypatch) -> None:
    """The README's call shape names the network; omitting it is a construction
    error, never a silent mainnet order."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    with pytest.raises(TypeError):
        HyperliquidOrder(asset=0, is_buy=True, size=0.0123,  # type: ignore[call-arg]
                         limit_px=78_600.0, sz_decimals=5, is_spot=False, tif="Ioc")
    with pytest.raises(TypeError):
        HyperliquidOrder(asset=0, is_buy=True, size=0.0123,  # type: ignore[call-arg]
                         limit_px=78_600.0, sz_decimals=5, mainnet=False, tif="Ioc")
    req = sign_hyperliquid_order(HyperliquidOrder(
        asset=0, is_buy=True, size=0.0123, limit_px=78_600.0,
        sz_decimals=5, is_spot=False, tif="Ioc", mainnet=False,
    ))
    assert req.url == "https://api.hyperliquid-testnet.xyz/exchange"


def test_nonce_is_a_millisecond_clock_unless_supplied(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    before = int(time.time() * 1000)
    req = sign_hyperliquid_order(_order())
    after = int(time.time() * 1000)
    assert before <= req.nonce <= after
    explicit = sign_hyperliquid_order(_order(nonce=before + 7))
    assert explicit.nonce == before + 7 and json.loads(explicit.body)["nonce"] == before + 7
    assert _recover(explicit, True) == Account.from_key(KEY).address.lower()
    with pytest.raises(ValueError, match="nonce"):
        sign_hyperliquid_order(_order(nonce=0))
    for bad in (True, 1.0, "1"):
        with pytest.raises(ValueError, match="nonce"):
            sign_hyperliquid_order(_order(nonce=bad))


def test_missing_key_refuses(monkeypatch) -> None:
    monkeypatch.delenv(HYPERLIQUID_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError):
        sign_hyperliquid_order(_order())


def test_non_positive_size_and_price_refuse(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    for overrides in ({"size": 0.0}, {"size": -1.0}, {"limit_px": 0.0}, {"limit_px": -1.0}):
        with pytest.raises(ValueError, match="positive"):
            sign_hyperliquid_order(_order(**overrides))


def test_zero_snap_refuses_before_the_tolerance_is_consulted(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    # The widest tolerance still refuses: this pins the dedicated post-snap
    # guard, which runs before the distance comparison.
    with pytest.raises(ValueError, match="size rounds to zero"):
        sign_hyperliquid_order(_order(size=0.000001, max_rounding_bps=500))


@pytest.mark.parametrize("tif", ["Ioc", "Gtc", "Alo"])
def test_every_supported_tif_reaches_the_wire(monkeypatch, tif) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(tif=tif))
    wire = json.loads(req.body)["action"]["orders"][0]
    assert wire["t"]["limit"]["tif"] == tif
    assert req.tif == tif


@pytest.mark.parametrize("bad", ["Fok", "FrontendMarket", "ioc", "IOC", "", None])
def test_unsupported_tif_refuses(monkeypatch, bad) -> None:
    """The whitelist is exactly the three resting/IOC limit kinds; a venue
    value this signer does not model (FrontendMarket, a Fok alias, a case
    variant) must not reach the wire."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    assert SUPPORTED_TIFS == ("Ioc", "Gtc", "Alo")
    with pytest.raises(ValueError, match="tif"):
        sign_hyperliquid_order(_order(tif=bad))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_size_or_price_never_reaches_the_wire(monkeypatch, bad) -> None:
    """Malformed non-finite inputs must stop at the signing boundary."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    with pytest.raises(ValueError, match="finite"):
        sign_hyperliquid_order(_order(size=bad))
    with pytest.raises(ValueError, match="finite"):
        sign_hyperliquid_order(_order(limit_px=bad))


def test_asset_and_sz_decimals_are_validated(monkeypatch) -> None:
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    for bad in (-1, 9_999_999_999, True):
        with pytest.raises(ValueError, match="asset"):
            sign_hyperliquid_order(_order(asset=bad))
    with pytest.raises(ValueError, match="sz_decimals"):
        sign_hyperliquid_order(_order(sz_decimals=7))          # perps allow 0..6
    with pytest.raises(ValueError, match="sz_decimals"):
        sign_hyperliquid_order(_order(sz_decimals=-1))
    with pytest.raises(ValueError, match="sz_decimals"):
        sign_hyperliquid_order(_order(sz_decimals=True))
    sign_hyperliquid_order(_order(
        asset=10_000, is_spot=True, sz_decimals=8, size=1.5, limit_px=2.0,
    ))


def test_builder_deployed_perp_uses_perp_precision(monkeypatch) -> None:
    """High HIP-3 asset indexes are not spot markets."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(
        asset=110_000,
        is_spot=False,
        sz_decimals=2,
        size=1.25,
        limit_px=0.0012345678,
        max_rounding_bps=500,  # 0.0012345678 -> 0.0012 is about 280 bps
    ))
    wire = json.loads(req.body)["action"]["orders"][0]
    assert wire["p"] == "0.0012"
    with pytest.raises(ValueError, match="sz_decimals"):
        sign_hyperliquid_order(_order(
            asset=110_000, is_spot=False, sz_decimals=7,
        ))


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), -float("inf"), 50.0, True, -1, 501, 10_000],
)
def test_max_rounding_bps_is_a_bounded_integer(monkeypatch, bad) -> None:
    """10,000 bps is not a legal widening: at a 100% tolerance the size guard
    is mathematically inert (a floored size is always less than one requested
    size away), so a reduce-only close could sign for a fraction of the
    position while the desk believes it flattened."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    with pytest.raises(ValueError, match="max_rounding_bps"):
        sign_hyperliquid_order(_order(max_rounding_bps=bad))


def test_tolerance_boundary_is_inclusive_and_zero_means_on_grid_only(monkeypatch) -> None:
    """Pins `> tolerance`, not `>=`: an order exactly at the tolerance signs,
    one bps past it refuses, and `max_rounding_bps=0` signs an on-grid order
    while refusing any snap at all."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    # 3.125 floors to 3 at sz_decimals=0: exactly 0.125/3.125 = 400 bps away.
    at_boundary = sign_hyperliquid_order(_order(size=3.125, sz_decimals=0, max_rounding_bps=400))
    assert at_boundary.size == 3.0
    with pytest.raises(ValueError, match="size .* snaps"):
        sign_hyperliquid_order(_order(size=3.125, sz_decimals=0, max_rounding_bps=399))
    on_grid = sign_hyperliquid_order(_order(size=1.0, limit_px=2.0, sz_decimals=0,
                                            max_rounding_bps=0))
    assert (on_grid.size, on_grid.price) == (1.0, 2.0)
    with pytest.raises(ValueError, match="size .* snaps"):
        sign_hyperliquid_order(_order(size=1.5, limit_px=2.0, sz_decimals=0,
                                      max_rounding_bps=0))
    with pytest.raises(ValueError, match="limit_px .* snaps"):
        sign_hyperliquid_order(_order(size=1.0, limit_px=2.000001, sz_decimals=0,
                                      max_rounding_bps=0))


def test_snapping_is_never_more_aggressive_than_asked(monkeypatch) -> None:
    """Buy price rounds down, sell price rounds up, size rounds down — and a
    reduce-only close can never be signed for more than the position."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    buy = sign_hyperliquid_order(_order(is_buy=True, limit_px=78_583.99))
    sell = sign_hyperliquid_order(_order(is_buy=False, limit_px=78_583.01))
    assert buy.price == 78_583.0 and sell.price == 78_584.0
    assert json.loads(buy.body)["action"]["orders"][0]["b"] is True
    assert json.loads(sell.body)["action"]["orders"][0]["b"] is False
    close = sign_hyperliquid_order(_order(is_buy=False, reduce_only=True, size=1.04,
                                          sz_decimals=0, max_rounding_bps=500))
    assert close.size == 1.0
    close_wire = json.loads(close.body)["action"]["orders"][0]
    assert close_wire["s"] == "1"
    assert close_wire["r"] is True
    assert (close.is_buy, close.reduce_only) == (False, True)


def test_binary_float_noise_does_not_move_an_exact_decimal_tick(monkeypatch) -> None:
    """Decimal(float) would turn the exact sell tick 0.1 into 0.10001."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(
        is_buy=False, size=1.0, limit_px=0.1, sz_decimals=0,
    ))
    wire = json.loads(req.body)["action"]["orders"][0]
    assert wire["p"] == "0.1"


def test_spot_precision_and_asset_index_reach_the_signed_wire(monkeypatch) -> None:
    """The signing path must not silently apply perp precision or market 0."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(
        asset=10_007,
        is_spot=True,
        size=1.0,
        limit_px=0.00012345678,
        sz_decimals=0,
    ))
    wire = json.loads(req.body)["action"]["orders"][0]
    assert wire["a"] == 10_007
    assert wire["p"] == "0.00012345"


def test_snapping_beyond_the_tolerance_refuses_instead_of_signing(monkeypatch) -> None:
    """0.5001 at sz_decimals=6 has no valid price except 1 (100% away) and
    1.6 at sz_decimals=0 snaps to 1 (37% away): neither may sign silently."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    # A sell snaps 0.5001 UP to 1 (100% away): refused by the tolerance.
    with pytest.raises(ValueError, match="limit_px .* snaps"):
        sign_hyperliquid_order(_order(is_buy=False, limit_px=0.5001, sz_decimals=6,
                                      size=1000.0))
    # A buy snaps it DOWN to 0: refused before the tolerance is even consulted.
    with pytest.raises(ValueError, match="rounds to zero"):
        sign_hyperliquid_order(_order(is_buy=True, limit_px=0.5001, sz_decimals=6,
                                      size=1000.0))
    with pytest.raises(ValueError, match="size .* snaps"):
        sign_hyperliquid_order(_order(size=1.6, sz_decimals=0))
    # 1.01 -> 1.0 is about 99 bps away: reject at the default 50 bps. This
    # boundary pins the basis-point divisor to 10,000 rather than 1,000.
    with pytest.raises(ValueError, match="size .* snaps"):
        sign_hyperliquid_order(_order(size=1.01, sz_decimals=0))
    # Within tolerance signs; the caller may widen it explicitly, but only up
    # to the cap: 1.6 -> 1 (37.5% of the position) cannot be signed at all.
    sign_hyperliquid_order(_order(size=1.04, sz_decimals=0, max_rounding_bps=500))
    with pytest.raises(ValueError, match="size .* snaps"):
        sign_hyperliquid_order(_order(size=1.6, sz_decimals=0, max_rounding_bps=500))


@pytest.mark.parametrize(
    "requested, sz_decimals, asset, expected_wire",
    [
        (68_584_102.57358684, 2, 0, "68584102.57"),
        (2_586_506_824.906289, 8, 10_000, "2586506824.906289"),
    ],
)
def test_size_wire_is_canonical_and_never_exceeds_the_request(
    monkeypatch, requested, sz_decimals, asset, expected_wire
) -> None:
    """Decimal snapping must survive wire construction without float noise."""
    monkeypatch.setenv(HYPERLIQUID_KEY_ENV, KEY)
    req = sign_hyperliquid_order(_order(
        asset=asset,
        is_spot=asset == 10_000,
        size=requested,
        limit_px=2.0,
        sz_decimals=sz_decimals,
    ))
    wire_size = json.loads(req.body)["action"]["orders"][0]["s"]
    assert wire_size == expected_wire
    assert Decimal(wire_size) <= Decimal(repr(requested))
    assert -Decimal(wire_size).as_tuple().exponent <= sz_decimals
    assert Decimal(repr(req.size)) == Decimal(wire_size)


@pytest.mark.parametrize(
    "px, sz_decimals, spot, is_buy, expected",
    [
        (78_583.99, 5, False, True, 78_583.0),   # buy: 5 sig figs, down
        (78_583.01, 5, False, False, 78_584.0),  # sell: 5 sig figs, up
        (1.234567, 0, False, True, 1.2345),      # 5 sig figs, 6 decimals allowed
        (0.00012345678, 0, False, True, 0.000123),   # decimals capped at 6 for perps
        (0.00012345678, 0, True, False, 0.00012346),  # spot allows 8, sell rounds up
        (123.456, 4, False, True, 123.45),       # 6 - 4 = 2 decimals
        (0.123456, 0, False, True, 0.12345),     # sub-one: sig = 5 < cap 6
        (0.0123456, 0, True, True, 0.012345),    # sub-one: sig = 6 < cap 8
        (0.5, 0, False, True, 0.5),
        (0.5, 0, False, False, 0.5),
    ],
)
def test_round_price_follows_the_venue_rule(px, sz_decimals, spot, is_buy, expected) -> None:
    assert round_price(px, sz_decimals, spot, is_buy) == expected


@pytest.mark.parametrize(
    ("px", "expected"),
    [
        (99.99999999999999, 99.999),
        (999.9999999999999, 999.99),
    ],
)
def test_round_price_just_below_power_of_ten_keeps_five_significant_figures(
    px, expected
) -> None:
    assert round_price(px, 0, False, True) == expected


def test_round_price_floor_never_rounds_to_tens() -> None:
    """`sign_hyperliquid_order` refuses sz_decimals above the venue max, but
    `round_price` itself must not hand `quantize` a negative decimals count."""
    assert round_price(54_321.5, 7, False, True) == 54_321.0
    assert round_price(54_321.5, 7, False, False) == 54_322.0


def test_round_size_floors() -> None:
    assert round_size(0.0123456, 5) == 0.01234
    assert round_size(1.6, 0) == 1.0
    assert round_size(2.0, 0) == 2.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 0.0, -1.0])
def test_round_size_and_round_price_fail_the_same_way(bad) -> None:
    """Both exported helpers hand display code 0.0 for a size or price that
    cannot be snapped, instead of `nan` from one and an exception from the other."""
    assert round_size(bad, 5) == 0.0
    assert round_price(bad, 5, False, True) == 0.0
