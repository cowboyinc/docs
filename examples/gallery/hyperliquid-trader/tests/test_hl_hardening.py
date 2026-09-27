"""Safety and recovery tests for the live Hyperliquid Actor."""

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda fn: fn


class _Runner:
    def continuation(self, fn=None, **kwargs):
        return (lambda value: value) if fn is None else fn


class _AddressError(Exception):
    pass


def _to_address_bytes(value):
    if not isinstance(value, str):
        raise _AddressError("address must be hex text")
    raw = value[2:] if value.startswith(("0x", "0X")) else value
    if len(raw) != 40:
        raise _AddressError("address must have 40 hex characters")
    try:
        decoded = bytes.fromhex(raw)
    except ValueError as exc:
        raise _AddressError("address must be hex") from exc
    if len(decoded) != 20:
        raise _AddressError("address must be 20 bytes")
    return decoded


def _load_modules():
    fake = types.ModuleType("cowboy_sdk")
    fake.actor = fake.public = fake.pure = fake.deferred = fake.callable_by = _identity
    fake.OWNER = object()
    fake.SELF = object()
    fake.runner = _Runner()
    fake.capture = lambda: types.SimpleNamespace()
    fake.runtime = types.SimpleNamespace()
    fake.AddressError = _AddressError
    fake.to_address_bytes = _to_address_bytes
    saved = {name: sys.modules.get(name) for name in ("cowboy_sdk", "hl_common")}
    try:
        sys.modules["cowboy_sdk"] = fake
        common_spec = importlib.util.spec_from_file_location(
            "hl_hardening_common", ROOT / "hl_common.py"
        )
        common = importlib.util.module_from_spec(common_spec)
        common_spec.loader.exec_module(common)
        sys.modules["hl_common"] = common
        exec_spec = importlib.util.spec_from_file_location(
            "hl_hardening_exec", ROOT / "hl_live_exec.py"
        )
        live = importlib.util.module_from_spec(exec_spec)
        exec_spec.loader.exec_module(live)
        return common, live
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


C, X = _load_modules()


def test_keccak_resolver_caches_a_working_candidate(monkeypatch):
    calls = []

    def candidate(value):
        calls.append(value)
        return C._KECCAK_EMPTY

    monkeypatch.setattr(C, "_candidates", lambda: iter([candidate]))
    C._resolve_keccak.cache_clear()
    try:
        assert C._resolve_keccak() is candidate
        assert C._resolve_keccak() is candidate
        assert calls == [b""]
    finally:
        C._resolve_keccak.cache_clear()


def test_keccak_resolver_retries_after_failure(monkeypatch):
    attempts = []

    def candidates():
        attempts.append(1)

        def broken(_value):
            raise RuntimeError("unavailable")

        yield broken

    monkeypatch.setattr(C, "_candidates", candidates)
    C._resolve_keccak.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="no working keccak256"):
            C._resolve_keccak()
        with pytest.raises(RuntimeError, match="no working keccak256"):
            C._resolve_keccak()
        assert len(attempts) == 2
    finally:
        C._resolve_keccak.cache_clear()


def test_base64_decode_does_not_require_runtime_imports():
    import base64

    assert C._b64_text("PD94bWwgdmVyc2lvbj0iMS4wIj8+") == '<?xml version="1.0"?>'
    assert C._b64_text("aGVs\nbG8=") == "hello"
    assert C._b64_text("not base64!") == "not base64!"
    large = "<item>" + ("x" * 10_000) + "</item>"
    assert C._b64_text(base64.b64encode(large.encode()).decode()) == large


class Runtime:
    def __init__(self):
        self.height = 100
        self.timestamp = 1_700_000_000_000
        self.events = []
        self.timers = []
        self.sender = b"\x11" * 20

    def assume_ownership_if_unowned(self):
        return None

    def emit_event(self, name, value):
        self.events.append((name, value))

    def schedule_timer_ex(self, height, payload, *, fee_payer=None, access_limit=None):
        self.timers.append((height, payload, fee_payer, access_limit))

    def get_block_height(self):
        return self.height

    def get_timestamp_ms(self):
        return self.timestamp

    def get_sender(self):
        return self.sender

    def _host(self):
        return types.SimpleNamespace()


@pytest.fixture
def runtime(monkeypatch):
    value = Runtime()
    monkeypatch.setattr(X, "runtime", value)
    return value


@pytest.fixture
def actor(runtime):
    value = X.LiveExecution()
    value.storage = {
        "cfg": {
            "account_addr": "0x" + "12" * 20,
            "price_url": X.TESTNET_INFO,
            "budget": 1000,
            "max_positions": 3,
            "min_confidence": 60,
            "slip_bps": 10,
            "slip_bps_major": 5,
            "min_hold_blocks": 0,
            "flip_margin": 0,
            "rebalance_interval_blocks": 10,
            "max_poll_wait_blocks": 7000,
            "max_poll_wait_ms": 1_800_000,
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.06,
            "max_cumulative_loss_pct": 0.10,
            "max_exit_price_retries": 2,
        },
        "portfolio": [],
        "running": True,
        "apply_pending": False,
        "post_reconcile_pending": False,
        "risk_halted": False,
        "risk_halt_reason": "",
        "cumulative_loss_baseline_pnl": 0.0,
        "mark_price_retry_attempts": 0,
        "pending_apply": {},
        "apply_generation": 0,
        "rebalances": 0,
    }
    return value


def test_sender_matching_uses_strict_normalized_expected_address(runtime):
    expected = "0x" + runtime.sender.hex()
    assert X._sender_matches(expected)
    assert X._sender_matches("  0X" + runtime.sender.hex().upper() + "  ")
    assert not X._sender_matches("")
    assert not X._sender_matches("0x" + "  " + "11" * 19)
    assert not X._sender_matches(b"short")


def test_zero_decimal_size_formatting_preserves_integer_trailing_zeros():
    assert X.fmt_size(50, 0) == "50"
    assert X.fmt_size(100, 0) == "100"
    assert X.fmt_size(50.9, 0) == "50"
    assert X.fmt_close_size(4900, 0) == "4900"
    assert X.fmt_close_size(50.1, 0) == "51"


def test_decimal_formatting_preserves_fractional_rounding_contracts():
    assert X.fmt_size(1.2349, 3) == "1.234"
    assert X.fmt_close_size(1.2341, 3) == "1.235"
    assert X.fmt_price(100.25, True, 2, slip_bps=0) == "100.25"
    assert X.fmt_price(100, True, 2, slip_bps=0) == "100"
    assert float(X.fmt_price(100, True, 2, slip_bps=100)) >= 100
    assert float(X.fmt_price(100, False, 2, slip_bps=100)) <= 100


def test_held_long_close_is_a_marketable_sell_at_the_planning_call_site():
    plan = X.plan_orders(
        {},
        [
            {
                "asset": "BTC",
                "side": "long",
                "qty": 1.0,
                "opened_block": 0,
            }
        ],
        {"BTC": {"a": 0, "sz": 2, "venue": "core"}},
        {"BTC": 100.0},
        100.0,
        slip_bps=200,
        close_absent=True,
    )

    assert len(plan) == 1
    assert plan[0]["b"] is False
    assert float(plan[0]["p"]) < 100.0


def test_close_from_fill_preserves_remainder_and_realizes_fill_only():
    position = {
        "asset": "BTC",
        "side": "long",
        "entry_px": 100.0,
        "qty": 2.0,
        "notional": 200.0,
    }
    remainder, pnl, record = X._close_from_fill(
        position, {"qty": 0.5, "avg_px": 90.0}, "stop_loss", 123
    )

    assert remainder == dict(position, qty=1.5, notional=150.0)
    assert pnl == -5.0
    assert record == dict(
        position,
        exit_px=90.0,
        pnl=-5.0,
        closed_qty=0.5,
        closed_block=123,
        exit_reason="stop_loss",
    )


def test_commit_prices_merges_fresh_values_and_preserves_cache_on_empty(actor):
    actor.storage["last_prices"] = {"BTC": 90.0, "ETH": 50.0}

    assert actor._commit_prices({"BTC": 100.0}) == {
        "BTC": 100.0,
        "ETH": 50.0,
    }
    assert actor._commit_prices({}) == {"BTC": 100.0, "ETH": 50.0}
    assert actor.storage["last_prices"] == {"BTC": 100.0, "ETH": 50.0}


def test_enforce_exits_keeps_position_until_reduce_only_fill(actor):
    position = {
        "asset": "BTC",
        "side": "long",
        "entry_px": 100.0,
        "qty": 1.0,
        "notional": 100.0,
    }
    assert actor._enforce_exits([position], {"BTC": 90.0}, 0.05, 0.0, {}) == [position]
    assert actor.storage.get("history") is None

    kept = actor._enforce_exits(
        [position], {"BTC": 90.0}, 0.05, 0.0, {"BTC": {"qty": 0.25, "avg_px": 89.0}}
    )
    assert kept == [dict(position, qty=0.75, notional=75.0)]
    assert actor.storage["realized_pnl"] == -2.75
    assert actor.storage["history"][0]["closed_qty"] == 0.25


def test_cumulative_forced_fill_is_booked_for_legacy_zero_stop_config(actor):
    position = {
        "asset": "BTC",
        "side": "long",
        "entry_px": 100.0,
        "qty": 1.0,
        "notional": 100.0,
    }
    actor.storage.update(
        {
            "risk_halted": True,
            "risk_halt_reason": "cumulative_loss",
            "realized_pnl": 0.0,
            "closed_count": 0,
            "win_count": 0,
            "history": [],
        }
    )

    kept = actor._enforce_exits(
        [position],
        {"BTC": 80.0},
        0.0,
        0.0,
        {"BTC": {"qty": 1.0, "avg_px": 80.0}},
    )

    assert kept == []
    assert actor.storage["realized_pnl"] == -20.0
    assert actor.storage["closed_count"] == 1


def test_signed_job_uses_the_action_digest():
    action = X.order_action(
        0, False, "65000", "0.001", reduce_only=True, tif="Ioc"
    )
    assert action["orders"][0] == {
        "a": 0,
        "b": False,
        "p": "65000",
        "s": "0.001",
        "r": True,
        "t": {"limit": {"tif": "Ioc"}},
    }
    url, headers, body = X.signed_job(action, 1_700_000_000_000)
    assert url == X.TESTNET_EXCHANGE
    assert json.loads(body)["action"] == action
    assert bytes.fromhex(headers["X-Cowboy-Sign-Digest"][2:]) == C.hl_digest(
        action, 1_700_000_000_000
    )


def test_exit_slot_preserves_reduce_only_and_ioc_on_the_signed_wire(actor):
    plan = [
        {
            "a": 0,
            "b": False,
            "p": "99",
            "s": "0.001",
            "r": True,
        }
    ]

    _url, _headers, body = actor._slot(plan, 0, X.TESTNET_INFO, X.meta_query())

    wire = json.loads(body)["action"]["orders"][0]
    assert wire["r"] is True
    assert wire["t"] == {"limit": {"tif": "Ioc"}}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, "unknown"),
        ({"status": "err", "response": "rate limit"}, "rejected"),
        (
            {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"error": "cannot fill"},
                        ]
                    }
                },
            },
            "rejected",
        ),
        (
            {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": "0.4", "avgPx": "65000"}},
                        ]
                    }
                },
            },
            "partial_fill",
        ),
        (
            {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": "1.0", "avgPx": "65000"}},
                        ]
                    }
                },
            },
            "filled",
        ),
        (
            {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 1001}},
                        ]
                    }
                },
            },
            "unknown",
        ),
    ],
)
def test_order_outcome_uses_one_response_parser(response, expected):
    filled, _, reason = (
        X.parse_fill(response) if isinstance(response, dict) else (0.0, None, None)
    )
    assert X.order_outcome(filled, reason, 1.0) == expected


def test_init_is_idempotent_and_preserves_state(runtime):
    value = X.LiveExecution()
    value.storage = {}
    payload = json.dumps(
        {
            "account_addr": "0x" + "12" * 20,
        }
    ).encode()
    assert value.init(payload) == b"ok: initialized"
    value.storage["portfolio"] = [{"asset": "BTC", "qty": 1}]
    assert value.init(payload) == b"ok: already initialized"
    assert value.storage["portfolio"] == [{"asset": "BTC", "qty": 1}]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"running":true}', "start handler"),
        (b'{"price_url":"https://example.com/info"}', "fixed Hyperliquid testnet"),
        (b'{"account_addr":"not-an-address"}', "address"),
    ],
)
def test_init_rejects_invalid_deployment_inputs(runtime, payload, message):
    value = X.LiveExecution()
    value.storage = {}
    with pytest.raises(ValueError, match=message):
        value.init(payload)


def test_start_requires_an_account(runtime):
    value = X.LiveExecution()
    value.storage = {"cfg": {}, "running": False}
    assert value.start(b"") == b"unconfigured: account_addr required"
    assert value.storage["running"] is False


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        (
            {
                "account_addr": "0x" + "12" * 20,
                "budget": 1000.0,
                "stop_loss_pct": 0.0,
                "max_cumulative_loss_pct": 0.10,
            },
            b"unconfigured: stop_loss_pct must be greater than zero",
        ),
        (
            {
                "account_addr": "0x" + "12" * 20,
                "budget": 1000.0,
                "stop_loss_pct": 0.03,
                "max_cumulative_loss_pct": 0.0,
            },
            b"unconfigured: max_cumulative_loss_pct must be in (0, 1]",
        ),
    ],
)
def test_start_refuses_missing_risk_controls(runtime, cfg, message):
    value = X.LiveExecution()
    value.storage = {"cfg": cfg, "running": False}
    assert value.start(b"") == message
    assert value.storage["running"] is False


@pytest.mark.parametrize("budget", [0.0, -50.0, float("nan"), float("inf")])
def test_start_refuses_non_positive_or_non_finite_budget(runtime, budget):
    value = X.LiveExecution()
    value.storage = {
        "cfg": {
            "account_addr": "0x" + "12" * 20,
            "budget": budget,
            "stop_loss_pct": 0.03,
            "max_cumulative_loss_pct": 0.10,
        },
        "running": False,
    }
    assert value.start(b"") == b"unconfigured: budget must be finite and greater than zero"
    assert value.storage["running"] is False


def test_timer_and_staged_rebalance_respect_risk_halt(actor, runtime, monkeypatch):
    actor.storage.update(
        {
            "risk_halted": True,
            "risk_halt_reason": "cumulative_loss",
            "research_addr": "0x" + "34" * 20,
            "ideas_snapshot_block": runtime.height,
            "latest_ideas": [{"asset": "BTC", "side": "long", "confidence": 90}],
        }
    )
    monkeypatch.setattr(
        actor,
        "_apply",
        lambda *_args: pytest.fail("timer path entered live apply while risk halted"),
    )

    assert actor.rebalance_tick(b"{}") == b"risk halted: cumulative_loss"
    assert actor.storage["apply_pending"] is False

    actor.storage.update(
        {"apply_pending": True, "apply_generation": 7, "pending_apply": {}}
    )
    assert (
        actor.rebalance_apply_tick(b'{"generation":7}')
        == b"risk halted: cumulative_loss"
    )
    assert actor.storage["apply_pending"] is False


@pytest.mark.parametrize("method", ["apply_ideas", "rebalance"])
def test_order_entrypoints_require_running_and_idle(actor, method):
    actor.storage["running"] = False
    assert getattr(actor, method)(b'{"ideas":[]}') == b"stopped: start required"
    actor.storage["running"] = True
    actor.storage["apply_pending"] = True
    assert getattr(actor, method)(b'{"ideas":[]}') == b"busy: order work pending"


def test_timer_path_rejects_an_actor_without_an_account(actor):
    actor.storage["cfg"].pop("account_addr")
    actor.storage["apply_pending"] = True
    assert actor._do_rebalance() == b"unconfigured: account_addr required"
    assert actor.storage["apply_pending"] is False


def test_missing_research_releases_the_apply_guard(actor):
    actor.storage["research_addr"] = ""
    actor.storage["apply_pending"] = True
    assert actor._do_rebalance() == b"no research addr"
    assert actor.storage["apply_pending"] is False


def test_rebalance_prefers_fresh_http_pull_when_configured(actor, monkeypatch):
    actor.storage["cfg"]["research_pull"] = True
    actor.storage["research_addr"] = "0x" + "22" * 20
    actor.storage["apply_pending"] = True
    pulled = []
    monkeypatch.setattr(
        actor, "_pull_research", lambda payload: pulled.append(payload) or b"pulling"
    )

    assert actor._do_rebalance() == b"pulling"
    assert pulled == [b"{}"]
    assert actor.storage["apply_pending"] is True
    assert actor.storage["apply_generation"] == 1


def test_research_pull_stages_snapshot_on_fresh_timer(actor, runtime, monkeypatch):
    actor.storage.update({"apply_pending": True, "apply_generation": 7})
    snapshot = {
        "count": 1,
        "ideas": [
            {
                "asset": "BTC",
                "side": "long",
                "confidence": 80,
            }
        ],
    }
    actor.storage["research_addr"] = "0x" + "22" * 20
    monkeypatch.setattr(
        runtime,
        "call_actor",
        lambda target, handler, payload, cycles: json.dumps(snapshot).encode(),
        raising=False,
    )

    assert actor._on_research_pull() == b"ideas scheduled"
    assert actor.storage["latest_ideas"] == snapshot["ideas"]
    assert actor.storage["ideas_snapshot_block"] == runtime.height
    assert len(runtime.timers) == 2
    assert all(timer[3] == 322 for timer in runtime.timers)
    assert json.loads(runtime.timers[0][1]) == {
        "_handler": "rebalance_apply_tick",
        "_payload": {"generation": 7},
    }
    assert json.loads(runtime.timers[1][1]) == {
        "_handler": "research_stats_tick",
        "_payload": {},
    }


def test_research_stats_pull_stores_and_publishes_snapshot(actor, runtime, monkeypatch):
    stats = {"total_theses": 12, "window": 15}
    actor.storage["research_addr"] = "0x" + "22" * 20
    actor.storage["notify_addr"] = "0x" + "33" * 20
    monkeypatch.setattr(
        runtime,
        "call_actor",
        lambda target, handler, payload, cycles: json.dumps(stats).encode(),
        raising=False,
    )

    assert actor._on_research_stats_pull() == b"research stats stored"
    assert actor.storage["cached_research_stats"] == stats
    assert json.loads(runtime.timers[0][1]) == {
        "_handler": "command_snapshot_tick",
        "_payload": {},
    }
    assert runtime.timers[0][3] == 375


def test_empty_research_pull_fails_closed(actor, runtime, monkeypatch):
    actor.storage["apply_pending"] = True
    actor.storage["research_addr"] = "0x" + "22" * 20
    monkeypatch.setattr(
        runtime, "call_actor", lambda *args: b'{"count":0,"ideas":[]}', raising=False
    )
    assert actor._on_research_pull() == b"no research ideas"
    assert actor.storage["apply_pending"] is False
    assert any(name == "live.research_pull_error" for name, _ in runtime.events)


def test_live_receiver_stores_only_authenticated_research_snapshots(actor, runtime):
    research = "0x" + runtime.sender.hex()
    actor.storage["research_addr"] = research
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
    assert actor.storage["ideas_snapshot_block"] == runtime.height
    assert actor.storage["cached_research_stats"] == {"window": 7}
    assert actor.storage["notify_addr"] == "0x" + "22" * 20

    runtime.sender = b"\x33" * 20
    rejected = json.dumps(
        {
            "type": "research_snapshot",
            "ideas": [{"asset": "ETH"}],
        }
    ).encode()
    assert actor.handle_message(rejected) == b"ignored: unknown research"
    assert actor.storage["latest_ideas"][0]["asset"] == "BTC"


def test_direct_notify_binding_survives_empty_research_snapshot(
    actor, runtime, monkeypatch
):
    notify = "0x" + "22" * 20
    scheduled = []
    monkeypatch.setattr(
        actor,
        "_schedule_command_snapshot",
        lambda: scheduled.append("command_snapshot_tick"),
    )

    assert json.loads(
        actor.set_notify(
            json.dumps(
                {
                    "notify_addr": notify,
                }
            ).encode()
        )
    ) == {"notify_addr": notify}
    assert actor.storage["notify_addr"] == notify
    assert scheduled == ["command_snapshot_tick"]

    actor.storage["research_addr"] = "0x" + runtime.sender.hex()
    assert (
        actor.handle_message(
            json.dumps(
                {
                    "type": "research_snapshot",
                    "ideas": [{"asset": "BTC"}],
                }
            ).encode()
        )
        == b"stored"
    )
    assert actor.storage["notify_addr"] == notify


def test_live_rebalance_fails_closed_on_stale_research(actor, runtime):
    runtime.height = 2_000
    actor.storage.update(
        {
            "research_addr": "0x" + runtime.sender.hex(),
            "latest_ideas": [{"asset": "BTC", "side": "long", "confidence": 80}],
            "ideas_snapshot_block": runtime.height - X.IDEAS_MAX_AGE_BLOCKS - 1,
            "apply_pending": True,
        }
    )

    assert actor._do_rebalance() == b"stale cached ideas"
    assert actor.storage["apply_pending"] is False
    assert any(name == "live.stale_cached_ideas" for name, _ in runtime.events)


@pytest.mark.parametrize(
    ("elapsed_ms", "allowed"),
    [
        (60_000, False),
        (1_800_001, True),
    ],
)
def test_tick_guard_waits_for_live_work_and_releases_lost_work(
    actor, runtime, elapsed_ms, allowed
):
    actor.storage.update(
        {
            "apply_pending": True,
            "post_reconcile_pending": True,
            "apply_started_block": 99,
            "apply_started_ms": runtime.timestamp - elapsed_ms,
        }
    )
    assert (
        actor._tick_ok("rebalance_tick", "rebalance_interval_blocks", "apply")
        is allowed
    )
    if allowed:
        assert actor.storage["post_reconcile_pending"] is False
        assert any(name == "live.continuation_lost" for name, _ in runtime.events)


def test_venue_position_prevents_a_duplicate_open(actor):
    assert (
        actor._record_account_state(
            {"balances": [{"coin": "USDC", "total": "1000"}]},
            {
                "marginSummary": {"accountValue": "987.5"},
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.01",
                            "entryPx": "65000",
                        }
                    }
                ]
            },
            {
                "marginSummary": {"accountValue": "12.5"},
                "assetPositions": [],
            },
        )
        is True
    )
    assert actor.storage["account_state"]["core_equity"] == 987.5
    assert actor.storage["account_state"]["xyz_equity"] == 12.5
    selected = [{"asset": "BTC", "side": "long", "confidence": 80}]
    universe = {"BTC": {"a": 0, "sz": 3, "venue": "core"}}
    assert actor._live_plan(selected, {"BTC": 65000}, universe) == []


def test_existing_gross_exposure_blocks_new_orders_without_forced_close(actor):
    actor.storage["cfg"]["budget"] = 50
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "qty": 1.0,
            "entry_px": 100,
            "opened_block": 0,
            "confidence": 0,
        }
    ]
    selected = [{"asset": "ETH", "side": "long", "confidence": 80}]
    universe = {
        "BTC": {"a": 0, "sz": 3, "venue": "core"},
        "ETH": {"a": 1, "sz": 3, "venue": "core"},
    }

    plan = actor._live_plan(selected, {"BTC": 100, "ETH": 100}, universe)

    assert plan == []


def test_explicit_reversal_still_closes_a_held_position(actor, runtime):
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "qty": 1.0,
            "entry_px": 100,
            "opened_block": 0,
            "confidence": 60,
        }
    ]
    selected = [{"asset": "BTC", "side": "short", "confidence": 80}]
    universe = {"BTC": {"a": 0, "sz": 3, "venue": "core"}}

    plan = actor._live_plan(selected, {"BTC": 100}, universe)

    assert plan[0]["sym"] == "BTC"
    assert plan[0]["why"] == "flip"
    assert plan[0]["r"] is True


def test_absent_position_closes_only_after_maximum_hold(actor, runtime):
    actor.storage["cfg"]["max_hold_blocks"] = 50
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "qty": 1.0,
            "entry_px": 100,
            "opened_block": runtime.height - 50,
            "confidence": 80,
        }
    ]
    universe = {"BTC": {"a": 0, "sz": 3, "venue": "core"}}

    plan = actor._live_plan([], {"BTC": 100}, universe)

    assert plan[0]["sym"] == "BTC"
    assert plan[0]["why"] == "stale"
    assert plan[0]["r"] is True


def test_opening_budget_is_distributed_only_across_missing_positions(actor):
    actor.storage["cfg"]["budget"] = 50
    actor.storage["last_equity"] = 900
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "qty": 0.25,
            "entry_px": 100,
            "opened_block": 0,
            "confidence": 90,
        }
    ]
    selected = [
        {"asset": "BTC", "side": "long", "confidence": 90},
        {"asset": "ETH", "side": "long", "confidence": 88},
        {"asset": "SOL", "side": "long", "confidence": 83},
    ]
    universe = {
        "BTC": {"a": 0, "sz": 3, "venue": "core"},
        "ETH": {"a": 1, "sz": 3, "venue": "core"},
        "SOL": {"a": 2, "sz": 3, "venue": "core"},
    }

    plan = actor._live_plan(selected, {"BTC": 100, "ETH": 100, "SOL": 100}, universe)

    assert [order["sym"] for order in plan] == ["ETH", "SOL"]
    assert all(float(order["s"]) * 100 >= X.MIN_ORDER_NOTIONAL for order in plan)
    assert sum(float(order["s"]) * 100 for order in plan) <= 25


def test_max_positions_caps_actual_held_symbols_not_only_selected_ideas():
    skips = []
    held = [
        {"asset": symbol, "side": "long", "qty": 1.0, "opened_block": 0}
        for symbol in ("BTC", "ETH", "SOL")
    ]
    plan = X.plan_orders(
        {"DOGE": {"side": "long", "confidence": 90}},
        held,
        {"DOGE": {"a": 3, "sz": 0, "venue": "core"}},
        {"DOGE": 1.0},
        100.0,
        skips=skips,
        max_positions=3,
    )
    assert plan == []
    assert skips[-1] == {
        "sym": "DOGE",
        "stage": "open",
        "reason": "max_positions",
    }


def test_max_positions_counts_multiple_new_openings_and_planned_closes():
    universe = {
        symbol: {"a": index, "sz": 2, "venue": "core"}
        for index, symbol in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE"))
    }
    prices = {symbol: 100.0 for symbol in universe}

    # One held slot plus a cap of three permits exactly two new symbols.
    plan = X.plan_orders(
        {
            "CCC": {"side": "long", "confidence": 90},
            "DDD": {"side": "long", "confidence": 80},
            "EEE": {"side": "long", "confidence": 70},
        },
        [{"asset": "AAA", "side": "long", "qty": 1.0, "opened_block": 0}],
        universe,
        prices,
        300.0,
        max_positions=3,
    )
    assert [order["sym"] for order in plan if not order["r"]] == ["CCC", "DDD"]

    # Confirmed closes are ordered first and release slots for the replacement
    # target; dispatch still verifies those closes before sending the open.
    replacement = X.plan_orders(
        {"CCC": {"side": "long", "confidence": 90}},
        [
            {"asset": "AAA", "side": "long", "qty": 1.0, "opened_block": 0},
            {"asset": "BBB", "side": "long", "qty": 1.0, "opened_block": 0},
        ],
        universe,
        prices,
        100.0,
        close_absent=True,
        max_positions=2,
    )
    assert [order["sym"] for order in replacement] == ["AAA", "BBB", "CCC"]
    assert [order["r"] for order in replacement] == [True, True, False]


def test_flip_books_close_and_opens_replacement_position(actor):
    actor.storage.update(
        {
            "portfolio": [
                {
                    "asset": "BTC",
                    "side": "long",
                    "entry_px": 100.0,
                    "qty": 1.0,
                    "notional": 100.0,
                    "confidence": 70,
                    "opened_block": 1,
                }
            ],
            "history": [],
            "realized_pnl": 0.0,
            "closed_count": 0,
            "win_count": 0,
            "apply_pending": True,
        }
    )
    selected = [
        {
            "asset": "BTC",
            "side": "short",
            "confidence": 90,
            "thesis_fit": "aligns",
        }
    ]
    plan = [
        {"sym": "BTC", "a": 0, "b": False, "p": "110", "s": "1", "r": True, "why": "flip"},
        {"sym": "BTC", "a": 0, "b": False, "p": "110", "s": "1", "r": False, "why": "open"},
    ]

    def fill(size, price):
        return {
            "status": "ok",
            "response": {
                "data": {"statuses": [{"filled": {"totalSz": str(size), "avgPx": str(price)}}]}
            },
        }

    actor._on_apply(selected, {"BTC": 110.0}, plan, fill(1, 110), fill(1, 110))

    assert actor.storage["realized_pnl"] == 10.0
    assert actor.storage["closed_count"] == 1
    assert len(actor.storage["history"]) == 1
    assert len(actor.storage["portfolio"]) == 1
    assert actor.storage["portfolio"][0]["side"] == "short"
    assert actor.storage["portfolio"][0]["entry_px"] == 110.0


def test_open_waits_for_planned_close_to_confirm(actor, runtime, monkeypatch):
    actor.storage["cfg"]["max_positions"] = 2
    actor.storage.update(
        {
            "portfolio": [
                {"asset": symbol, "side": "long", "entry_px": 100.0, "qty": 1.0, "notional": 100.0}
                for symbol in ("AAA", "BBB")
            ],
            "apply_pending": True,
            "apply_generation": 7,
            "pending_apply": {
                "generation": 7,
                "stage": "orders",
                "selected": [{"asset": "CCC", "side": "long", "confidence": 90}],
                "prices": {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0},
                "plan": [
                    {
                        "sym": "AAA",
                        "a": 0,
                        "b": False,
                        "p": "99",
                        "s": "1",
                        "r": True,
                        "why": "close",
                        "_position_qty": 1.0,
                    },
                    {"sym": "CCC", "a": 2, "b": True, "p": "101", "s": "1", "r": False, "why": "open"},
                ],
                "results": [{}],
            },
        }
    )
    monkeypatch.setattr(
        actor,
        "_apply_order",
        lambda *_args: pytest.fail("opening order sent before close confirmation"),
    )

    actor.apply_order_tick(b'{"generation":7,"index":1}')

    assert any(
        name == "live.order_skipped"
        and value["reason"] == "max_positions_waiting_for_close"
        for name, value in runtime.events
    )


def test_minimum_order_notional_is_a_fixed_safety_floor():
    skips = []
    assert X.MIN_ORDER_NOTIONAL == 10.0
    plan = X.plan_orders(
        {"BTC": {"side": "long", "confidence": 90}},
        [],
        {"BTC": {"a": 0, "sz": 3, "venue": "core"}},
        {"BTC": 100.0},
        9.99,
        skips=skips,
    )
    assert plan == []
    assert skips[-1]["reason"] == "below_min_notional"


def test_failed_pre_order_reconcile_cannot_build_an_order_plan(actor, monkeypatch):
    actor.storage["universe"] = {"BTC": {"a": 0, "sz": 3, "venue": "core"}}
    responses = iter([{}, {}, {}])

    async def http(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(X.runner, "http", http, raising=False)
    monkeypatch.setattr(actor, "_record_account_state", lambda *args: False)
    monkeypatch.setattr(
        actor,
        "_live_plan",
        lambda *args: pytest.fail("built a plan after failed reconciliation"),
    )
    actor._begin_apply()
    assert (
        asyncio.run(
            actor._apply(b'{"ideas":[{"asset":"BTC","side":"long","confidence":80}]}')
        )
        == b"reconcile failed"
    )
    assert actor.storage["apply_pending"] is False


def test_late_apply_state_cannot_adopt_a_newer_generation(actor, monkeypatch):
    actor.storage.update({"apply_pending": True, "apply_generation": 8})
    monkeypatch.setattr(
        actor,
        "_record_account_state",
        lambda *_args: pytest.fail("stale apply reconciled into newer generation"),
    )

    assert actor._on_apply_state(7, [], "0x" + "12" * 20, {}, {}, {}) == b"superseded"
    assert actor.storage["apply_pending"] is True
    assert actor.storage["apply_generation"] == 8


def _venue_state(asset="BTC"):
    return (
        {"balances": [{"coin": "USDC", "total": "999"}]},
        {
            "assetPositions": [
                {
                    "position": {
                        "coin": asset,
                        "szi": "0.01",
                        "entryPx": "65000",
                    }
                }
            ]
        },
        {"assetPositions": []},
    )


def test_post_order_reconcile_replaces_local_state(actor):
    actor.storage.update(
        {
            "portfolio": [{"asset": "ETH", "side": "long", "qty": 2}],
            "post_reconcile_pending": True,
        }
    )
    result = actor._on_post_order_reconcile(b'{"opened":1}', *_venue_state())
    assert json.loads(result)["venue_reconciled"] is True
    assert [position["asset"] for position in actor.storage["portfolio"]] == ["BTC"]
    assert actor.storage["post_reconcile_pending"] is False


def test_pull_mode_does_not_emit_doomed_research_feedback_message(actor, monkeypatch):
    actor.storage["cfg"]["research_pull"] = True
    actor.storage["post_reconcile_pending"] = True
    monkeypatch.setattr(
        actor,
        "_push_trader_snapshot",
        lambda: pytest.fail("pull mode attempted a 100k-lane snapshot push"),
    )
    result = actor._on_post_order_reconcile(b'{"opened":1}', *_venue_state())
    assert json.loads(result)["venue_reconciled"] is True


def test_mark_defers_command_snapshot_to_an_actor_funded_timer(
    actor, runtime, monkeypatch
):
    actor.storage["notify_addr"] = "0x" + "22" * 20
    runtime.sender = b"\x01" * 20  # callback provenance need not be the owner
    sent = []
    monkeypatch.setattr(
        runtime,
        "call_actor",
        lambda target, handler, payload, cycles: (
            sent.append((target, handler, json.loads(payload), cycles)) or b"stored"
        ),
        raising=False,
    )
    actor._mark_route({"BTC": "100"}, {})
    assert sent == []
    assert len(runtime.timers) == 1
    height, payload, payer, limit = runtime.timers[0]
    assert (height, payer, limit) == (runtime.height + 1, None, 375)
    assert json.loads(payload) == {"_handler": "command_snapshot_tick", "_payload": {}}
    assert actor.command_snapshot_tick(b"{}") == b"sent"
    assert len(sent) == 1
    assert sent[0][0] == "0x" + "22" * 20
    assert sent[0][1] == "handle_message"
    assert sent[0][2]["type"] == "command_snapshot"
    assert sent[0][3] == 2_000_000


def test_missing_fresh_exit_price_retries_then_halts_new_risk(actor, runtime):
    actor.storage["cfg"].update(
        {
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.06,
        }
    )
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "entry_px": 100.0,
            "qty": 1.0,
            "notional": 100.0,
        }
    ]
    actor.storage["universe"] = {"BTC": {"a": 0, "sz": 3}}
    actor.storage["last_prices"] = {"BTC": 90.0}

    actor._dispatch_mark({})

    missing = [
        value for name, value in runtime.events if name == "live.exit_price_missing"
    ]
    assert missing[-1] == {
        "count": 1,
        "symbols": ["BTC"],
        "attempt": 1,
        "max_retries": 2,
        "retry_scheduled": True,
    }
    assert len(runtime.timers) == 1
    height, payload, payer, limit = runtime.timers[0]
    assert (height, payer, limit) == (runtime.height + 1, None, 322)
    assert json.loads(payload)["_handler"] == "mark_price_retry_tick"

    # A second incomplete read gets the final bounded immediate retry.
    runtime.timers.clear()
    actor._dispatch_mark({})
    assert len(runtime.timers) == 1
    assert actor.storage["risk_halted"] is False

    # A third consecutive miss does not spin forever. It loudly blocks new
    # exposure while the regular mark loop remains alive to recover.
    runtime.timers.clear()
    actor._dispatch_mark({})
    assert runtime.timers == []
    final_missing = [
        value for name, value in runtime.events if name == "live.exit_price_missing"
    ][-1]
    assert final_missing["attempt"] == 3
    assert final_missing["retry_scheduled"] is False
    assert actor.storage["risk_halted"] is True
    assert actor.storage["risk_halt_reason"] == "exit_price_unavailable"
    assert any(name == "live.risk_halted" for name, _value in runtime.events)

    # A complete fresh mark clears only the transient price-data halt.
    actor._dispatch_mark({"BTC": 100.0})
    assert actor.storage["risk_halted"] is False
    assert actor.storage["mark_price_retry_attempts"] == 0


def test_cumulative_loss_halts_entries_and_forces_reduce_only_exits(actor, runtime):
    actor.storage["cfg"].update({"budget": 100.0, "max_cumulative_loss_pct": 0.10})
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "entry_px": 100.0,
            "qty": 1.0,
            "notional": 100.0,
        }
    ]
    actor.storage["universe"] = {"BTC": {"a": 0, "sz": 3, "venue": "core"}}
    actor.storage["last_prices"] = {"BTC": 80.0}

    assert actor._dispatch_mark({"BTC": 80.0}) == b"exits scheduled"
    assert actor.storage["risk_halted"] is True
    assert actor.storage["risk_halt_reason"] == "cumulative_loss"
    assert actor.storage["pending_exits"][0]["r"] is True
    assert actor.storage["pending_exits"][0]["why"] == "cumulative_loss"
    assert actor.apply_ideas(b'{"ideas":[]}') == b"risk halted: cumulative_loss"

    assert actor.clear_risk_halt(b"{}") == b"confirmation required"
    assert actor.clear_risk_halt(b'{"confirm":false}') == b"confirmation required"
    assert actor.clear_risk_halt(b'{"confirm":true}') == b"ok: risk halt cleared"
    assert actor.storage["risk_halted"] is False
    assert actor._cumulative_loss({"BTC": 80.0}) == 0.0
    assert actor._cumulative_loss({"BTC": 60.0}) == pytest.approx(0.20)
    assert any(name == "live.risk_halted" for name, _value in runtime.events)


def test_cumulative_loss_includes_realized_pnl_and_requires_complete_marks(actor):
    actor.storage["cfg"]["budget"] = 100.0
    actor.storage["realized_pnl"] = -11.0
    assert actor._cumulative_loss({}) == pytest.approx(0.11)

    actor.storage["portfolio"] = [
        {"asset": "BTC", "side": "long", "entry_px": 100.0, "qty": 1.0, "notional": 100.0}
    ]
    assert actor._cumulative_loss({}) is None


def test_missing_prices_cannot_downgrade_or_auto_clear_cumulative_halt(actor):
    actor.storage.update(
        {
            "risk_halted": True,
            "risk_halt_reason": "cumulative_loss",
            "portfolio": [
                {"asset": "BTC", "side": "long", "entry_px": 100.0, "qty": 1.0, "notional": 100.0}
            ],
            "universe": {"BTC": {"a": 0, "sz": 3, "venue": "core"}},
        }
    )

    actor._dispatch_mark({})
    assert actor.storage["risk_halted"] is True
    assert actor.storage["risk_halt_reason"] == "cumulative_loss"

    actor._dispatch_mark({"BTC": 100.0})
    assert actor.storage["risk_halted"] is True
    assert actor.storage["risk_halt_reason"] == "cumulative_loss"


def test_exit_plan_prioritizes_deepest_stop_breaches(actor):
    symbols = ["A", "B", "C", "D", "E", "F"]
    actor.storage["portfolio"] = [
        {"asset": symbol, "side": "long", "entry_px": 100.0, "qty": 1.0}
        for symbol in symbols
    ]
    actor.storage["universe"] = {
        symbol: {"a": index, "sz": 2, "venue": "core"}
        for index, symbol in enumerate(symbols)
    }
    prices = dict(zip(symbols, [91.0, 90.0, 89.0, 40.0, 30.0, 20.0]))

    exits = actor._exit_plan(prices, actor.storage["universe"])

    assert [order["sym"] for order in exits[:3]] == ["F", "E", "D"]
    assert all(order["r"] is True for order in exits)
    assert all(order["b"] is False for order in exits)
    assert all(float(order["p"]) < prices[order["sym"]] for order in exits)


def test_primary_demo_bundles_research_actor_before_deploy():
    script = (ROOT / "demo.sh").read_text()

    assert 'bundle.py" "$DIR/hl_research.py" "$RESEARCH_CODE"' in script
    assert 'actor deploy --code "$RESEARCH_CODE"' in script
    assert 'actor deploy --code "$DIR/hl_research.py"' not in script


def test_command_snapshot_is_bounded_for_canyon_mailbox(actor, runtime, monkeypatch):
    actor.storage["notify_addr"] = "0x" + "22" * 20
    actor.storage["cached_research_stats"] = {
        "total_theses": 12,
        "active_theses": 9,
        "window": 15,
        "mandate": "x" * 2000,
    }
    actor.storage["latest_ideas"] = [
        {
            "asset": "BTC",
            "side": "long",
            "confidence": 80,
            "rationale": "r" * 1000,
            "theme": "t" * 1000,
        }
    ] * 8
    actor.storage["portfolio"] = [
        {
            "asset": "BTC",
            "side": "long",
            "notional": 100.0,
            "upnl": 1.0,
            "entry_px": 90.0,
            "qty": 1.0,
        }
    ] * 10
    sent = []
    monkeypatch.setattr(
        runtime,
        "call_actor",
        lambda target, handler, payload, cycles: (
            sent.append((target, payload)) or b"stored"
        ),
        raising=False,
    )

    assert actor._push_command_snapshot() is True
    payload = json.loads(sent[0][1])
    assert len(sent[0][1]) < 1800
    assert len(payload["ideas"]) == 6
    assert len(payload["ideas"][0]["rationale"]) == 96
    assert len(payload["portfolio"]["positions"]) == 8
    assert "mandate" not in payload["research_stats"]
    assert "account_state" not in payload["portfolio"]


def test_mark_without_notifier_does_not_schedule_a_snapshot(actor, runtime):
    actor._on_mark({"BTC": 100})
    assert runtime.timers == []


@pytest.mark.parametrize("reconcile", [False, True])
def test_apply_followup_runs_on_timer_and_fences_superseded_work(
    actor, runtime, monkeypatch, reconcile
):
    actor.storage["apply_generation"] = 7
    invoked = []
    monkeypatch.setattr(
        actor,
        "_reconcile_after_orders",
        lambda payload: invoked.append(json.loads(payload)) or b"reconciling",
    )
    monkeypatch.setattr(
        actor, "_push_trader_snapshot", lambda: invoked.append("feedback") or True
    )
    plan = [{"sym": "BTC", "why": "open", "p": "100", "s": "1", "r": False}]
    actor._on_apply([], {"BTC": 100}, plan if reconcile else None, {})
    assert invoked == []
    height, payload, payer, limit = runtime.timers[0]
    assert (height, payer, limit) == (runtime.height + 1, None, 322)
    envelope = json.loads(payload)
    assert envelope["_handler"] == "apply_followup_tick"
    inner = json.dumps(envelope["_payload"]).encode()
    if reconcile:
        assert actor.storage["post_reconcile_pending"] is True
        actor.storage["apply_generation"] = 8
        assert actor.apply_followup_tick(inner) == b"superseded"
        assert invoked == []
        actor.storage["apply_generation"] = 7
        assert actor.apply_followup_tick(inner) == b"reconciling"
        assert invoked[0]["apply_generation"] == 7
    else:
        assert actor.storage["apply_pending"] is False
        assert actor.apply_followup_tick(inner) == b"sent"
        assert invoked == ["feedback"]


def test_exit_route_defers_signed_jobs_to_a_timer(actor, runtime, monkeypatch):
    exits = [{"sym": "BTC"}]
    monkeypatch.setattr(actor, "_exit_plan", lambda *args: exits)
    started = []
    monkeypatch.setattr(
        actor, "_mark_exits", lambda payload: started.append(payload) or b"started"
    )
    assert actor._mark_route({"BTC": "90"}, {}) == b"exits scheduled"
    assert started == []
    assert actor.storage["pending_exits"] == exits
    assert json.loads(runtime.timers[0][1])["_handler"] == "mark_exits_tick"
    assert actor.mark_exits_tick(b"{}") == b"started"
    assert started == [b"{}"]


@pytest.mark.parametrize("method,reads_before_orders", [("_mark_exits", 0)])
def test_signs_only_the_slot_about_to_be_submitted(
    actor, runtime, monkeypatch, method, reads_before_orders
):
    plan = [{"a": i, "b": True, "p": "100", "s": "1", "r": False} for i in range(3)]
    actor.storage.update({"pending_exits": plan, "pending_exit_prices": {"BTC": 100}})
    monkeypatch.setattr(actor, "_record_account_state", lambda *args: True)
    monkeypatch.setattr(actor, "_live_plan", lambda *args: plan)
    monkeypatch.setattr(actor, "_on_apply", lambda *args: b"done")
    monkeypatch.setattr(actor, "_on_mark", lambda *args: b"done")
    signed = []

    def sign(action, nonce):
        signed.append(nonce)
        return X.TESTNET_EXCHANGE, {}, "{}"

    calls = []

    async def http(*args, **kwargs):
        calls.append(args)
        assert len(signed) == max(0, len(calls) - reads_before_orders)
        runtime.timestamp += 1000
        return {"BTC": "100"}

    monkeypatch.setattr(X, "signed_job", sign)
    monkeypatch.setattr(X.runner, "http", http, raising=False)
    assert asyncio.run(getattr(actor, method)(b"{}")) == b"done"
    assert len(signed) == 3 and signed == sorted(set(signed))


def test_apply_submits_one_signed_order_per_continuation(actor, runtime, monkeypatch):
    plan = [
        {
            "a": i,
            "b": True,
            "p": "100",
            "s": "1",
            "r": False,
            "sym": "ASSET" + str(i),
            "why": "open",
            "venue": "core",
        }
        for i in range(3)
    ]
    actor.storage.update(
        {
            "apply_generation": 7,
            "apply_pending": True,
            "pending_apply": {
                "generation": 7,
                "stage": "orders",
                "selected": [],
                "prices": {"BTC": 100},
                "plan": plan,
                "results": [],
            },
        }
    )
    signed = []
    calls = []

    def sign(action, nonce):
        signed.append(nonce)
        return X.TESTNET_EXCHANGE, {}, "{}"

    async def http(*args, **kwargs):
        calls.append(args)
        assert len(signed) == len(calls)
        runtime.timestamp += 1000
        return {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {"filled": {"totalSz": "1", "avgPx": "100"}},
                    ]
                }
            },
        }

    monkeypatch.setattr(X, "signed_job", sign)
    monkeypatch.setattr(X.runner, "http", http, raising=False)
    monkeypatch.setattr(actor, "_on_apply", lambda *args: b"done")

    for index in range(3):
        result = asyncio.run(
            actor._apply_order(
                json.dumps(
                    {
                        "generation": 7,
                        "index": index,
                    }
                ).encode()
            )
        )
        assert len(signed) == index + 1
        if index < 2:
            assert result == b"next order scheduled"
    assert result == b"done"
    assert signed == sorted(set(signed))


def test_staged_orders_allocate_distinct_nonces_at_timer_dispatch(
    actor, runtime, monkeypatch
):
    plan = [
        {
            "a": i,
            "b": True,
            "p": "100",
            "s": "1",
            "r": False,
            "sym": "ASSET" + str(i),
            "why": "open",
            "venue": "core",
        }
        for i in range(2)
    ]
    actor.storage.update(
        {
            "apply_generation": 7,
            "apply_pending": True,
            "pending_apply": {
                "generation": 7,
                "stage": "stock_prices",
                "selected": [],
                "core_prices": {"BTC": 100},
            },
            "universe": {},
        }
    )
    monkeypatch.setattr(actor, "_live_plan", lambda *args: plan)
    monkeypatch.setattr(actor, "_schedule_followup", lambda *args: None)

    assert actor._on_apply_prices(7, {}) == b"orders scheduled"
    state = actor.storage["pending_apply"]
    assert "nonce_base" not in state

    dispatched = []
    monkeypatch.setattr(
        actor,
        "_apply_order",
        lambda payload: dispatched.append(json.loads(payload)) or b"scheduled",
    )
    assert actor.apply_order_tick(b'{"generation":7,"index":0}') == b"scheduled"
    state["results"] = [{}]
    actor.storage["pending_apply"] = state
    assert actor.apply_order_tick(b'{"generation":7,"index":1}') == b"scheduled"
    assert [item["nonce"] for item in dispatched] == [
        runtime.timestamp,
        runtime.timestamp + 1,
    ]


def test_next_nonce_advances_when_consensus_millisecond_is_unchanged(actor, runtime):
    assert actor._next_nonce() == runtime.timestamp
    assert actor._next_nonce() == runtime.timestamp + 1
    assert actor.storage["nonce_block_ms"] == runtime.timestamp + 1


def test_failed_post_order_reconcile_preserves_local_state(actor):
    original = [{"asset": "ETH", "side": "long", "qty": 2}]
    actor.storage.update(
        {
            "portfolio": list(original),
            "post_reconcile_pending": True,
        }
    )
    spot, _, xyz = _venue_state()
    result = actor._on_post_order_reconcile(
        b'{"opened":1}', spot, {"error": "timeout"}, xyz
    )
    assert json.loads(result)["venue_reconciled"] is False
    assert actor.storage["portfolio"] == original
    assert actor.storage["post_reconcile_pending"] is False


def test_late_reconcile_cannot_replace_newer_state(actor, runtime):
    original = [{"asset": "ETH", "side": "long", "qty": 2}]
    actor.storage.update(
        {
            "portfolio": list(original),
            "apply_generation": 3,
            "apply_pending": True,
            "post_reconcile_pending": True,
        }
    )
    result = actor._on_post_order_reconcile(
        b'{"opened":1,"apply_generation":2}', *_venue_state()
    )
    assert json.loads(result)["venue_reconciled"] is False
    assert actor.storage["portfolio"] == original
    assert actor.storage["apply_pending"] is True
    assert any(name == "live.reconcile_superseded" for name, _ in runtime.events)


@pytest.mark.parametrize(
    "value",
    [
        "not-an-address",
        "0x" + "12 34" + "56" * 18,
        "0x" + "12\n34" + "56" * 18,
        "0x" + "  " + "ab" * 19,
        "0x1234",
    ],
)
def test_address_normalization_rejects_malformed_input(value):
    with pytest.raises(ValueError):
        C.normalize_address(value)


def test_address_normalization_returns_canonical_hex():
    assert C.normalize_address("0X" + "AB" * 20) == "0x" + "ab" * 20
