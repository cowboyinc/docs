"""Regression tests for unified Hyperliquid account reconciliation."""

import json
import sys
import types
from pathlib import Path


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda fn: fn


class _Runner:
    def continuation(self, fn=None, **kwargs):
        return (lambda f: f) if fn is None else fn


fake = types.ModuleType("cowboy_sdk")
fake.actor = fake.public = fake.pure = fake.deferred = fake.callable_by = _identity
fake.OWNER = object()
fake.SELF = object()
fake.runner = _Runner()
fake.capture = lambda: types.SimpleNamespace()
fake.runtime = types.SimpleNamespace()
fake.AddressError = ValueError
fake.to_address_bytes = lambda value: bytes.fromhex(
    str(value).removeprefix("0x").removeprefix("0X")
)
sys.modules["cowboy_sdk"] = fake
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hl_live_exec as X  # noqa: E402


def test_queries_distinguish_spot_core_and_builder_dex():
    address = "0x" + "12" * 20
    assert json.loads(X.spot_state_query(address)) == {
        "type": "spotClearinghouseState",
        "user": address,
    }
    assert json.loads(X.user_state_query(address)) == {
        "type": "clearinghouseState",
        "user": address,
    }
    assert json.loads(X.user_state_query(address, "xyz")) == {
        "type": "clearinghouseState",
        "user": address,
        "dex": "xyz",
    }


def test_spot_usdc_is_authoritative_including_zero():
    assert (
        X.parse_spot_usdc({"balances": [{"coin": "USDC", "total": "858.293449"}]})
        == 858.293449
    )
    assert X.parse_spot_usdc({"balances": []}) == 0.0
    assert X.parse_spot_usdc({"not_balances": []}) is None
    account = X.parse_spot_account(
        {
            "balances": [{"coin": "USDC", "total": "858.293449", "hold": "8.830027"}],
            "tokenToAvailableAfterMaintenance": [[0, "853.878436"]],
        }
    )
    assert account == {"total": 858.293449, "hold": 8.830027, "available": 853.878436}


def test_margin_equity_is_parsed_for_account_observability():
    assert X.parse_equity({"marginSummary": {"accountValue": "123.45"}}) == 123.45
    assert X.parse_equity({"marginSummary": {}}) is None


def test_reconcile_imports_core_and_xyz_positions_without_double_counting():
    previous = [
        {
            "asset": "NVDA",
            "side": "long",
            "entry_px": 1,
            "qty": 1,
            "notional": 1,
            "confidence": 77,
            "opened_block": 123,
        }
    ]
    core = {
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.01",
                    "entryPx": "64000",
                }
            }
        ]
    }
    xyz = {
        "assetPositions": [
            {"position": {"coin": "xyz:NVDA", "szi": "-0.707", "entryPx": "221.745"}},
            {"position": {"coin": "xyz:AMZN", "szi": "0.054", "entryPx": "243"}},
        ]
    }
    got = {
        p["asset"]: p
        for p in X.parse_venue_positions([("core", core), ("xyz", xyz)], previous)
    }

    assert set(got) == {"BTC", "NVDA", "AMZN"}
    assert got["BTC"]["side"] == "long" and got["BTC"]["qty"] == 0.01
    assert got["BTC"]["venue"] == "core"
    assert got["NVDA"]["side"] == "short" and got["NVDA"]["qty"] == 0.707
    assert got["NVDA"]["venue"] == "xyz"
    assert got["NVDA"]["confidence"] == 77 and got["NVDA"]["opened_block"] == 123
    assert got["AMZN"]["confidence"] == 0 and got["AMZN"]["opened_block"] == 0


def test_deployable_enforces_budget_as_a_hard_ceiling():
    trader = X.LiveExecution()
    trader.storage = {"last_equity": 900.0}

    assert trader._deployable({"budget": 50.0, "equity_pct": 0.80}) == 50.0
    trader.storage["last_equity"] = 40.0
    assert trader._deployable({"budget": 50.0, "equity_pct": 0.80}) == 32.0
    trader.storage["last_equity"] = -1.0
    assert trader._deployable({"budget": 50.0, "equity_pct": 0.80}) == 0.0
    trader.storage["last_equity"] = None
    assert trader._deployable({"budget": 50.0, "equity_pct": 0.80}) == 50.0


def test_return_pct_uses_strategy_pnl_not_total_venue_equity():
    trader = X.LiveExecution()
    trader.storage = {
        "cfg": {"budget": 50.0},
        "last_equity": 900.0,
        "last_prices": {},
        "portfolio": [],
        "realized_pnl": 0.0,
    }

    snapshot = trader._pnl_snapshot()

    assert snapshot["equity"] == 900.0
    assert snapshot["equity_source"] == "venue"
    assert snapshot["strategy_equity"] == 50.0
    assert snapshot["return_pct"] == 0.0
    assert snapshot["return_basis"] == "strategy_pnl_over_budget"


def test_live_exit_plan_exists_and_builds_reduce_only_orders():
    trader = X.LiveExecution()
    trader.storage = {
        "cfg": {"stop_loss_pct": 0.08, "take_profit_pct": 0.20, "slip_bps": 100},
        "last_prices": {},
        "portfolio": [
            {"asset": "BTC", "side": "long", "entry_px": 100.0, "qty": 1.25},
            {"asset": "ETH", "side": "short", "entry_px": 100.0, "qty": 2.0},
        ],
    }
    universe = {
        "BTC": {"a": 0, "sz": 3},
        "ETH": {"a": 1, "sz": 3},
    }

    orders = trader._exit_plan({"BTC": 90.0, "ETH": 95.0}, universe)

    assert len(orders) == 1
    assert orders[0]["sym"] == "BTC"
    assert orders[0]["b"] is False
    assert orders[0]["r"] is True
    assert orders[0]["why"] == "stop_loss"
    assert trader._exit_plan({"BTC": 105.0, "ETH": 95.0}, universe) == []

    # Cached marks are useful for display, but may not trigger a live order.
    trader.storage["last_prices"] = {"BTC": 80.0}
    assert trader._exit_plan({}, universe) == []

    trader.storage["cfg"]["stop_loss_pct"] = 0.0
    trader.storage["cfg"]["take_profit_pct"] = 0.0
    assert trader._exit_plan({"BTC": 1.0, "ETH": 1000.0}, universe) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
