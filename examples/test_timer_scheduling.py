"""Checks for the examples that drive themselves with timers.

Each of these actors re-arms its own loop with
``runtime.schedule_after(delay_blocks, handler, payload)``. What the SDK writes
into the routing envelope and what the node hands back to the named handler are
two sides of one contract, so these tests drive the real SDK through
``cowboy_sdk.testing.SimulatedChain`` — whose router mirrors
``execution/src/execution/utils.rs::extract_timer_handler_from_payload`` — rather
than a local stub that could agree with the examples and disagree with the node.

Needs the sibling `node` checkout for the SDK, the same layout the demos assume.

Run: python3 examples/test_timer_scheduling.py (or under pytest)
"""

import functools
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.normpath(os.path.join(HERE, "..", "..", "node", "pvm", "Lib"))

_pkg = os.path.join(LIB, "cowboy_sdk")
if not os.path.isfile(os.path.join(_pkg, "testing.py")):
    raise SystemExit(
        "skip: this suite needs the sibling `node` checkout for the SDK "
        "(expected at %s). Clone cowboyinc/node alongside this repo." % LIB)


def _sdk_modules():
    return {n: m for n, m in sys.modules.items()
            if n == "cowboy_sdk" or n.startswith("cowboy_sdk.")}


def _swap_sdk(modules):
    """Make ``sys.modules``' cowboy_sdk entries exactly ``modules``, returning
    the set displaced."""
    previous = _sdk_modules()
    for name in previous:
        del sys.modules[name]
    sys.modules.update(modules)
    return previous


# Sibling suites (test_runner_result_migration.py, test_non_object_runner_results.py,
# 31-frontier/test_actors.py) install a stub `cowboy_sdk` into sys.modules and
# leave it there, live during both this module's import and its tests. Hence the
# swap in each direction: inheriting a stub gives actors fake decorators and no
# storage, and leaving ours behind breaks theirs.
_foreign = _swap_sdk({})

# Loaded as a package by path, not via sys.path: node/pvm/Lib ships a
# RustPython stdlib copy that must not shadow CPython's, and an installed pypi
# cowboy_sdk must not win over the repo one.
_spec = importlib.util.spec_from_file_location(
    "cowboy_sdk", os.path.join(_pkg, "__init__.py"), submodule_search_locations=[_pkg])
_sdk = importlib.util.module_from_spec(_spec)
sys.modules["cowboy_sdk"] = _sdk
_spec.loader.exec_module(_sdk)

from cowboy_sdk import codec, mock_host  # noqa: E402
from cowboy_sdk.testing import SimulatedChain, _route_timer_payload  # noqa: E402

_REAL_SDK = _swap_sdk(_foreign)


def with_real_sdk(fn):
    """Run the test body against the real SDK, restoring any stub afterwards."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        displaced = _swap_sdk(_REAL_SDK)
        try:
            return fn(*args, **kwargs)
        finally:
            _swap_sdk(displaced)
    return wrapper


def _load(path):
    spec = importlib.util.spec_from_file_location(
        "ex_" + os.path.basename(path)[:-3], os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fire(chain, actor, n):
    """Advance ``n`` blocks with the sender a timer fire actually carries — the
    actor's own address, which is what ``@callable_by(SELF)`` matches."""
    caller = mock_host.context().get("sender", b"")
    chain.set_sender(bytes(actor.address.as_ref()))
    try:
        return chain.advance_block(n)
    finally:
        chain.set_sender(caller)


def _scheduled(handler):
    """The one pending timer, routed as the node would route it on fire."""
    timers = list(mock_host.get_timers().values())
    assert len(timers) == 1, timers
    routed, inner = _route_timer_payload(timers[0]["payload"])
    assert routed == handler, routed
    return timers[0]["height"], inner


# ─── the loop actors fire their handlers and re-arm ───────────────────────────
@with_real_sdk
def test_timer_counter_ticks_and_rearms():
    m = _load("core/07-timers-and-automation/timer_counter.py")
    actor = m.TimerCounter()
    chain = SimulatedChain(actor)
    chain.call("init", b'{"interval_blocks": 3, "max_ticks": 2}')
    chain.call("start", codec.encode({}))

    height, inner = _scheduled("on_timer")
    assert height == chain.block_height + 3, (height, chain.block_height)
    assert m._timer_payload(inner) == {"tag": "tick"}, inner

    fired = _fire(chain, actor, 3)
    assert [h for h, _ in fired] == ["on_timer"], fired
    tick = codec.decode(fired[0][1])
    assert tick["status"] == "ticked" and tick["tag"] == "tick", tick
    assert tick["count"] == 1, tick
    # Re-armed for the second tick.
    _scheduled("on_timer")

    fired = _fire(chain, actor, 3)
    assert [h for h, _ in fired] == ["on_timer"], fired
    done = codec.decode(fired[0][1])
    assert done["status"] == "completed" and done["count"] == 2, done
    assert mock_host.get_timers() == {}, "the loop kept scheduling past max_ticks"
    print("ok test_timer_counter_ticks_and_rearms")


@with_real_sdk
def test_alert_scheduler_carries_the_observed_value():
    m = _load("gallery/alerts-and-schedulers/alert_scheduler.py")
    actor = m.AlertScheduler()
    chain = SimulatedChain(actor)
    chain.call("init", b'{"check_name":"latency","threshold":42,"interval_blocks":5}')
    chain.call("schedule_next_check", codec.encode(
        {"delay_blocks": 5, "observed_value": 38}))

    height, inner = _scheduled("run_check")
    assert height == chain.block_height + 5, (height, chain.block_height)
    assert m._timer_payload(inner) == {"observed_value": 38}, inner

    _fire(chain, actor, 5)
    assert chain.state(b"check_count") == codec.encode(1), chain.state(b"check_count")
    print("ok test_alert_scheduler_carries_the_observed_value")


@with_real_sdk
def test_feed_auto_tick_regenerates_and_rearms():
    m = _load("gallery/casino-rounds/feed.py")
    actor = m.MockPriceFeed()
    chain = SimulatedChain(actor)
    chain.call("init", b"")
    chain.call("start_auto", codec.encode({"interval_blocks": 4}))
    _scheduled("tick")

    fired = _fire(chain, actor, 4)
    assert [h for h, _ in fired] == ["tick"], fired
    assert codec.decode(chain.state(b"update_count")) == 1, chain.state(b"update_count")
    _scheduled("tick")  # the tick re-armed the next one
    print("ok test_feed_auto_tick_regenerates_and_rearms")


@with_real_sdk
def test_bettor_start_arms_its_run_loop():
    m = _load("gallery/casino-rounds/bettor.py")
    chain = SimulatedChain(m.AutoBettor())
    chain.call("init", b"")
    chain.call("configure", codec.encode({
        "self_actor": "0x" + "11" * 20,
        "casino_actor": "0x" + "22" * 20,
        "token_id": "0x" + "33" * 32,
        "bet_interval_blocks": 6,
    }))
    chain.call("start", codec.encode({}))

    height, inner = _scheduled("run")
    # start() aims one block out, then run() paces itself from bet_interval_blocks.
    assert height == chain.block_height + 1, (height, chain.block_height)
    assert inner == b"[]", inner  # run() takes no per-fire data
    print("ok test_bettor_start_arms_its_run_loop")


# ─── timers checked at the point they are armed ───────────────────────────────
@with_real_sdk
def test_casino_cycle_timer_carries_the_round_id():
    m = _load("gallery/casino-rounds/casino.py")
    chain = SimulatedChain()
    m._schedule_cycle_timer(chain.block_height + 3, 7)

    height, inner = _scheduled("advance_cycle")
    assert height == chain.block_height + 3, (height, chain.block_height)
    assert m._timer_payload(inner) == {"round_id": 7}, inner

    # A target already in the past still fires, one block out.
    mock_host.reset()
    m._schedule_cycle_timer(chain.block_height - 5, 9)
    height, inner = _scheduled("advance_cycle")
    assert height == chain.block_height + 1, (height, chain.block_height)
    assert m._timer_payload(inner) == {"round_id": 9}, inner
    print("ok test_casino_cycle_timer_carries_the_round_id")


@with_real_sdk
def test_price_watcher_tick_generation_survives_the_envelope():
    m = _load("gallery/price-watcher/price_watcher.py")
    chain = SimulatedChain(m.PriceWatcher())
    chain.call("init", b'{"autostart": true, "interval_blocks": 30}')

    height, inner = _scheduled("daily_tick")
    assert height == chain.block_height + 30, (height, chain.block_height)
    # A tick that reads gen 0 returns "superseded" and the scan chain dies.
    assert m._parse(inner) == {"gen": 1}, inner
    print("ok test_price_watcher_tick_generation_survives_the_envelope")


# ─── the router these tests trust agrees with the node ────────────────────────
@with_real_sdk
def test_router_matches_the_node_known_answer_vectors():
    """Known-answer vectors copied from the node's own Rust tests
    (``execution/src/execution/utils.rs::tests``), which pin the bytes the JSON
    arm of ``extract_timer_handler_from_payload`` hands a routed handler. The
    CBOR arm is dormant (its activation height is the u64::MAX sentinel), so the
    live regime is this one.
    """
    vectors = [
        # extract_timer_handler_json_inner_bytes_are_compact_and_sorted
        (b'{"_handler":"h","_payload":[1, 2, 3]}', "h", b"[1,2,3]"),
        (b'{"_handler":"h","b": 2, "a": 1}', "h", b'{"a":1,"b":2}'),
        # ..._empty_handler_name_falls_back / _non_object_json_falls_back /
        # _invalid_json_falls_back / _default
        (b'{"_handler": ""}', "handle_timer", b'{"_handler": ""}'),
        (b"[1, 2, 3]", "handle_timer", b"[1, 2, 3]"),
        (b"not json at all", "handle_timer", b"not json at all"),
        (b"some arbitrary payload", "handle_timer", b"some arbitrary payload"),
    ]
    for payload, handler, inner in vectors:
        assert _route_timer_payload(payload) == (handler, inner), payload
    # ..._too_long_handler_falls_back: 257 chars is over the node's bound.
    long_name = b'{"_handler": "' + b"a" * 257 + b'"}'
    assert _route_timer_payload(long_name) == ("handle_timer", long_name)
    print("ok test_router_matches_the_node_known_answer_vectors")


# ─── acceptance: the examples no longer reach past the SDK for timers ─────────
@with_real_sdk
def test_no_example_reaches_the_host_to_schedule_a_timer():
    offenders = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if root == HERE and name.startswith("test_"):
                continue  # the cross-example suites carry the needles as subject matter
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if "runtime._host()" in line or "pvm_host.schedule_timer" in line:
                        offenders.append("%s:%d" % (os.path.relpath(path, HERE), lineno))
    assert offenders == [], offenders
    print("ok test_no_example_reaches_the_host_to_schedule_a_timer")


def main():
    tests = [
        test_timer_counter_ticks_and_rearms,
        test_alert_scheduler_carries_the_observed_value,
        test_feed_auto_tick_regenerates_and_rearms,
        test_bettor_start_arms_its_run_loop,
        test_casino_cycle_timer_carries_the_round_id,
        test_price_watcher_tick_generation_survives_the_envelope,
        test_router_matches_the_node_known_answer_vectors,
        test_no_example_reaches_the_host_to_schedule_a_timer,
    ]
    for test in tests:
        test()
    print("\nall %d timer-scheduling tests passed" % len(tests))


if __name__ == "__main__":
    main()
