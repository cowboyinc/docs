"""Unit tests for the paper-execution actor's pure core: idea selection, position
return (long vs short), book marking, and price extraction. The deterministic
part every validator replays. cowboy_sdk is stubbed so the module imports under
CPython; the functions under test touch none of it.
"""

import base64
import json
import sys
import types
from pathlib import Path

_fake = types.ModuleType("cowboy_sdk")


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def _wrap(fn):
        return fn

    return _wrap


class _Runner:
    def continuation(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn

    async def http(self, *a, **k):
        return {}


class _AddressError(Exception):
    pass


def _to_address_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        decoded = bytes(value)
    elif isinstance(value, str):
        raw = value[2:] if value.startswith(("0x", "0X")) else value
        if len(raw) != 40:
            raise _AddressError("address must have 40 hex characters")
        try:
            decoded = bytes.fromhex(raw)
        except ValueError as exc:
            raise _AddressError("address must be hex") from exc
    else:
        raise _AddressError("unsupported address")
    if len(decoded) != 20:
        raise _AddressError("address must be 20 bytes")
    return decoded


_fake.actor = _identity
_fake.public = _identity
_fake.pure = _identity
_fake.deferred = _identity
_fake.callable_by = _identity
_fake.OWNER = object()
_fake.SELF = object()
_fake.runner = _Runner()
_fake.capture = lambda: types.SimpleNamespace()
_fake.runtime = types.SimpleNamespace(get_block_height=lambda: 100)
_fake.AddressError = _AddressError
_fake.to_address_bytes = _to_address_bytes
sys.modules["cowboy_sdk"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hl_paper_exec as X  # noqa: E402

IDEAS = [
    {"asset": "BTC", "side": "long", "confidence": 80, "thesis_fit": "aligns"},
    {"asset": "ETH", "side": "short", "confidence": 65, "thesis_fit": "aligns"},
    {
        "asset": "SOL",
        "side": "long",
        "confidence": 90,
        "thesis_fit": "neutral",
    },  # not aligned
    {
        "asset": "DOGE",
        "side": "long",
        "confidence": 40,
        "thesis_fit": "aligns",
    },  # below min
]


def test_address_normalizer_rejects_malformed_widths():
    assert X._normalized_address("0x" + "  " + "ab" * 19) == ""
    assert X._normalized_address(b"abcde") == ""
    assert X._normalized_address("  0X" + "AB" * 20 + "  ") == "0x" + "ab" * 20


def test_select_filters_and_ranks():
    got = X._select_ideas(IDEAS, top_n=3, min_conf=60)
    assert [i["asset"] for i in got] == [
        "BTC",
        "ETH",
    ]  # SOL/DOGE dropped, ranked by conf


def test_select_top_n_cap():
    ideas = [
        {"asset": c, "side": "long", "confidence": 70 + n, "thesis_fit": "aligns"}
        for n, c in enumerate(["A", "B", "C", "D"])
    ]
    got = X._select_ideas(ideas, top_n=2, min_conf=0)
    assert [i["asset"] for i in got] == ["D", "C"]  # highest two


def test_pos_return_long_and_short():
    assert abs(X._pos_return("long", 100.0, 110.0) - 0.10) < 1e-9  # long +10%
    assert (
        abs(X._pos_return("short", 100.0, 90.0) - 0.10) < 1e-9
    )  # short gains when it falls
    assert (
        abs(X._pos_return("short", 100.0, 110.0) + 0.10) < 1e-9
    )  # short loses when it rises
    assert X._pos_return("long", 0.0, 110.0) == 0.0  # no entry → 0
    assert X._pos_return("long", 100.0, None) == 0.0  # no mark → 0


def test_exit_reason():
    assert (
        X._exit_reason("long", 100.0, 90.0, 0.08, 0.20) == "stop_loss"
    )  # long down 10% > 8% stop
    assert (
        X._exit_reason("long", 100.0, 125.0, 0.08, 0.20) == "take_profit"
    )  # long up 25% > 20% target
    assert (
        X._exit_reason("short", 100.0, 88.0, 0.08, 0.10) == "take_profit"
    )  # short +12% (price fell)
    assert (
        X._exit_reason("short", 100.0, 112.0, 0.08, 0.10) == "stop_loss"
    )  # short -12% (price rose)
    assert X._exit_reason("long", 100.0, 105.0, 0.08, 0.20) is None  # within band
    assert X._exit_reason("long", 100.0, 50.0, 0.0, 0.0) is None  # disabled
    assert X._exit_reason("long", 100.0, None, 0.08, 0.20) is None  # no mark


def test_close_at_mark_realizes_full_paper_position():
    position = {
        "asset": "ETH",
        "side": "short",
        "entry_px": 100.0,
        "notional": 500.0,
    }

    pnl, record = X._close_at_mark(position, 90.0, "take_profit", 123)

    assert pnl == 50.0
    assert record == dict(
        position, exit_px=90.0, pnl=50.0, closed_block=123, exit_reason="take_profit"
    )


def test_plan_book():
    port = [
        {"asset": "BTC", "side": "long", "entry_px": 100.0, "notional": 500.0},
        {"asset": "ETH", "side": "short", "entry_px": 100.0, "notional": 500.0},
    ]
    prices = {"BTC": 110.0, "ETH": 90.0, "SOL": 20.0}
    # BTC still wanted (same side) -> kept untouched; ETH not in target -> closed
    target = {
        "BTC": {"side": "long", "confidence": 80},
        "SOL": {"side": "long", "confidence": 70},
    }
    keep, close = X.plan_book(port, target, prices)
    assert [p["asset"] for p in keep] == ["BTC"]
    assert keep[0]["entry_px"] == 100.0  # entry price preserved (no churn)
    assert [(p["asset"], c) for p, c in close] == [("ETH", 90.0)]

    # side flip forces a close even though the asset is still in target
    keep2, close2 = X.plan_book(
        port, {"BTC": {"side": "short", "confidence": 80}}, prices
    )
    assert [p["asset"] for p in keep2] == []  # BTC long closed on flip
    assert {p["asset"] for p, _c in close2} == {"BTC", "ETH"}

    # unchanged idea set -> everything kept, nothing closed (no churn)
    keep3, close3 = X.plan_book(
        port,
        {
            "BTC": {"side": "long", "confidence": 80},
            "ETH": {"side": "short", "confidence": 60},
        },
        prices,
    )
    assert [p["asset"] for p in keep3] == ["BTC", "ETH"] and close3 == []

    # held name we can't price is kept, not dropped
    keep4, close4 = X.plan_book(
        port, {"SOL": {"side": "long", "confidence": 70}}, {"BTC": 110.0}
    )
    assert any(p["asset"] == "ETH" for p in keep4) and close4 == [(port[0], 110.0)]


def test_mark_book():
    book = [
        {"asset": "BTC", "side": "long", "entry_px": 100.0, "notional": 500.0},
        {"asset": "ETH", "side": "short", "entry_px": 100.0, "notional": 500.0},
    ]
    unreal, marked = X._mark_book(book, {"BTC": 110.0, "ETH": 90.0})
    assert abs(unreal - 100.0) < 1e-9  # +50 long + +50 short
    assert abs(marked[0]["upnl"] - 50.0) < 1e-9


def test_resolve_prices_envelope():
    body = json.dumps({"BTC": "63000.5", "ETH": "1800", "DOGE": "0.1"})
    env = {
        "raw": base64.b64encode(body.encode()).decode()
    }  # runner http base64 envelope
    assert X.resolve_prices(["BTC", "ETH"], [("", env)]) == {
        "BTC": 63000.5,
        "ETH": 1800.0,
    }
    # multi-dex: crypto core (bare keys) + equities dex (xyz:SYMBOL)
    assert X.resolve_prices(
        ["BTC", "NVDA"], [("", {"BTC": "5"}), ("xyz", {"xyz:NVDA": "210.1"})]
    ) == {"BTC": 5.0, "NVDA": 210.1}
    assert X.resolve_prices(["BTC"], [("", {})]) == {}


def test_resolve_prices_unwraps_live_runner_result():
    body = json.dumps({"vntl:SPACEX": "212.5"})
    wrapped = types.SimpleNamespace(
        raw={
            "raw": base64.b64encode(body.encode()).decode(),
            "content_type": "application/json",
        }
    )

    assert X.resolve_prices(["SPACEX"], [("vntl", wrapped)]) == {
        "SPACEX": 212.5,
    }


def test_commit_prices_merges_fresh_values_and_preserves_cache_on_empty():
    actor = X.PaperExecution()
    actor.storage = {"last_prices": {"BTC": 90.0, "ETH": 50.0}}

    assert actor._commit_prices({"BTC": 100.0}) == {
        "BTC": 100.0,
        "ETH": 50.0,
    }
    assert actor._commit_prices({}) == {"BTC": 100.0, "ETH": 50.0}
    assert actor.storage["last_prices"] == {"BTC": 100.0, "ETH": 50.0}


def test_paper_receiver_stores_only_authenticated_research_snapshots():
    events = []
    sender = b"\x11" * 20
    X.runtime.get_sender = lambda: sender
    X.runtime.emit_event = lambda name, body: events.append((name, body))
    actor = X.PaperExecution()
    actor.storage = {"research_addr": "0x" + sender.hex()}
    payload = json.dumps(
        {
            "type": "research_snapshot",
            "ideas": [{"asset": "BTC", "side": "long", "confidence": 80}],
            "stats": {"window": 7},
            "notify_addr": "0x" + "22" * 20,
        }
    ).encode()

    assert actor.handle_message(payload) == b"stored"
    assert actor.storage["latest_ideas"][0]["asset"] == "BTC"
    assert actor.storage["ideas_snapshot_block"] == 100
    assert actor.storage["cached_research_stats"] == {"window": 7}

    X.runtime.get_sender = lambda: b"\x33" * 20
    assert (
        actor.handle_message(
            json.dumps(
                {
                    "type": "research_snapshot",
                    "ideas": [{"asset": "ETH"}],
                }
            ).encode()
        )
        == b"ignored: unknown research"
    )
    assert actor.storage["latest_ideas"][0]["asset"] == "BTC"


def test_paper_rebalance_fails_closed_on_stale_research(monkeypatch):
    events = []
    height = 2_000
    monkeypatch.setattr(X.runtime, "get_block_height", lambda: height, raising=False)
    monkeypatch.setattr(
        X.runtime,
        "emit_event",
        lambda name, body: events.append((name, body)),
        raising=False,
    )
    actor = X.PaperExecution()
    actor.storage = {
        "research_addr": "0x" + "11" * 20,
        "latest_ideas": [{"asset": "BTC", "side": "long", "confidence": 80}],
        "ideas_snapshot_block": height - X.IDEAS_MAX_AGE_BLOCKS - 1,
    }

    assert actor._do_rebalance() == b"stale cached ideas"
    assert any(name == "paper.stale_cached_ideas" for name, _ in events)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
