"""Structural guards for the access-metered timer and push-model paths."""

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTOR_FILES = [
    ROOT / "hl_research.py",
    ROOT / "hl_live_exec.py",
    ROOT / "hl_paper_exec.py",
    ROOT / "hl_notify.py",
]


@pytest.mark.parametrize("path", ACTOR_FILES, ids=lambda path: path.name)
def test_recurring_ticks_reject_external_callers(path):
    # A public tick can fork an actor-funded loop even when its work is pending.
    tree = ast.parse(path.read_text(), filename=str(path))
    ticks = [
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name.endswith("_tick")
    ]
    assert ticks
    for tick in ticks:
        permissions = [
            decorator
            for decorator in tick.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id in {"public", "callable_by"}
        ]
        assert len(permissions) == 1, tick.name
        permission = permissions[0]
        assert permission.func.id == "callable_by", tick.name
        assert [arg.id for arg in permission.args if isinstance(arg, ast.Name)] == [
            "SELF"
        ], tick.name


def test_notifier_enqueue_is_owner_only():
    tree = ast.parse((ROOT / "hl_notify.py").read_text(), filename="hl_notify.py")
    enqueue = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "enqueue"
    )
    permission = next(
        decorator
        for decorator in enqueue.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id in {"public", "callable_by"}
    )
    assert permission.func.id == "callable_by"
    assert [arg.id for arg in permission.args if isinstance(arg, ast.Name)] == ["OWNER"]


def test_notifier_async_entrypoints_are_deferred():
    path = ROOT / "hl_notify.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    methods = {
        node.name: node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in (
        "set_telegram",
        "set_sources",
        "enqueue",
        "clear_guidance",
        "handle_webhook",
        "handle_message",
        "webhook_drain_tick",
    ):
        decorators = methods[name].decorator_list
        assert any(
            isinstance(item, ast.Name) and item.id == "deferred" for item in decorators
        ), name


@pytest.mark.parametrize(
    ("filename", "names"),
    [
        (
            "hl_research.py",
            {
                "start",
                "ingest_tick",
                "synth_tick",
                "validate_tick",
                "discover_tick",
                "distill_tick",
                "brief_tick",
            },
        ),
        (
            "hl_live_exec.py",
            {
                "start",
                "rebalance_tick",
                "mark_tick",
                "universe_tick",
                "rebalance_apply_tick",
                "apply_prices_tick",
                "apply_stocks_tick",
                "apply_order_tick",
                "apply_followup_tick",
                "mark_exits_tick",
                "command_snapshot_tick",
            },
        ),
        ("hl_paper_exec.py", {"start", "rebalance_tick", "mark_tick"}),
    ],
)
def test_timer_entrypoints_with_async_side_effects_are_deferred(filename, names):
    path = ROOT / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    methods = {
        node.name: node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in names:
        decorators = methods[name].decorator_list
        assert any(
            isinstance(item, ast.Name) and item.id == "deferred" for item in decorators
        ), "%s:%s" % (filename, name)


def test_notifier_has_no_polling_surface():
    path = ROOT / "hl_notify.py"
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert names.isdisjoint({"start", "stop", "tg_tick", "tg_poll_tick", "_tg_poll"})
    for forbidden in ("getUpdates", "tg_running", "tg_offset", "TG_POLL_BLOCKS"):
        assert forbidden not in source


def test_notifier_telegram_token_is_cbss_only():
    path = ROOT / "hl_notify.py"
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    assert "${TELEGRAM_BOT_TOKEN}" in source
    assert 'storage["bot_token"]' not in source
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "http"
    ]
    assert calls
    for call in calls:
        secrets = [kw.value for kw in call.keywords if kw.arg == "secrets"]
        assert len(secrets) == 1
        assert isinstance(secrets[0], ast.Attribute)
        assert isinstance(secrets[0].value, ast.Name)
        assert (secrets[0].value.id, secrets[0].attr) == ("ctx", "secrets")

    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_secret_refs"
    )
    assert "TG_SECRET_KEY" in ast.unparse(helper)

    manifest = json.loads((ROOT / "tools" / "notify_manifest.json").read_text())
    entitlements = {item["id"]: item for item in manifest["entitlements"]}
    keys = entitlements["secrets.read"]["params"]["keys"]
    assert len(keys) == 1 and keys[0].endswith(":TELEGRAM_BOT_TOKEN")


@pytest.mark.parametrize("path", ACTOR_FILES, ids=lambda path: path.name)
def test_continuation_timeouts_are_integer_literals(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    decorators = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "continuation"
    ]
    for decorator in decorators:
        for keyword in decorator.keywords:
            if keyword.arg == "timeout_blocks":
                assert isinstance(keyword.value, ast.Constant)
                assert isinstance(keyword.value.value, int)


@pytest.mark.parametrize(
    ("filename", "method", "clears"),
    [
        ("hl_research.py", "_synthesize", 3),
        ("hl_live_exec.py", "_mark", 2),
    ],
)
def test_large_price_responses_are_discarded_between_awaits(filename, method, clears):
    """Raw allMids payloads cannot survive into the next callback state.

    Canyon gives continuation callbacks a fixed 5M cell budget.  Persisting a
    raw core response in ``ctx`` while scheduling the xyz leg has exhausted
    that budget in production, so keep this serialization constraint explicit.
    """
    path = ROOT / filename
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.AsyncFunctionDef) and item.name == method
    )
    body = ast.get_source_segment(source, node)
    assert body.count("ctx.raw_prices = None") == clears
    assert "ctx.p_coins" not in body
    assert "ctx.p_stocks" not in body


@pytest.mark.parametrize("path", ACTOR_FILES, ids=lambda path: path.name)
def test_timer_actor_cross_actor_calls_stay_bounded(path):
    # Keep a file-wide guard so helpers reached by timers cannot quietly add
    # nested access frames.
    tree = ast.parse(path.read_text(), filename=str(path))
    calls = [
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "call_actor"
        for node in ast.walk(tree)
    ]
    if path.name == "hl_live_exec.py":
        # Canyon cannot deliver the full research snapshot through its fixed
        # 100k message lane and runners cannot hairpin into the RPC. Two bounded
        # pure reads run in separate callbacks; a third bounded call commits the
        # compact snapshot directly to the notifier.
        assert sum(calls) == 3
    else:
        assert not any(calls)


@pytest.mark.parametrize("path", ACTOR_FILES, ids=lambda path: path.name)
def test_child_producing_timer_schedules_request_enough_access(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert constants["TIMER_ACCESS_LIMIT"] == 160 + 162
    if path.name == "hl_live_exec.py":
        assert constants["COMMAND_SNAPSHOT_ACCESS_LIMIT"] == 375
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "schedule_timer_ex"
    ]
    assert calls, "expected the actor's extended timer schedule"
    for call in calls:
        limits = [kw.value for kw in call.keywords if kw.arg == "access_limit"]
        assert len(limits) == 1
        assert isinstance(limits[0], ast.Name)
        assert limits[0].id in {
            "TIMER_ACCESS_LIMIT",
            "COMMAND_SNAPSHOT_ACCESS_LIMIT",
        }
