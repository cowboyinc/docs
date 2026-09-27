import ast
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SDK = pathlib.Path(
    os.environ.get(
        "COWBOY_SDK_PATH", ROOT.parents[4] / "node" / "pvm" / "Lib" / "pypi_build"
    )
)
sys.path.insert(0, str(SDK))
TESTS_ROOT = ROOT.parent / "tests"
sys.path.insert(0, str(TESTS_ROOT))
from sdk_stub import install as install_sdk_stub  # noqa: E402

install_sdk_stub()

SPEC = importlib.util.spec_from_file_location("hl_ui_host_test", ROOT / "main.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_payload_and_numeric_normalization():
    assert MODULE._parse(b'{"research_actor":"0xabc"}')["research_actor"] == "0xabc"
    assert MODULE._parse(b"not json") == {}
    assert MODULE._num("12.5") == 12.5
    assert MODULE._num(None) == 0


def test_read_uses_json_at_the_sdk_host_boundary(monkeypatch):
    calls = []

    def host_call(target, handler, payload, cycles_limit, access_limit=None):
        calls.append((target, handler, json.loads(payload), cycles_limit, access_limit))
        return b'{"ideas":[{"asset":"BTC"}]}'

    monkeypatch.setattr(MODULE.runtime, "call_actor", host_call)
    target = b"\x11" * 20
    actor = MODULE.HlUiHost.__new__(MODULE.HlUiHost)
    assert actor._read("0x" + target.hex(), "get_ideas", {"limit": 3}) == {
        "ideas": [{"asset": "BTC"}],
    }
    assert calls == [(target, "get_ideas", {"limit": 3}, MODULE.CALL_CYCLES, None)]


def test_routes_include_live_api_and_static_assets():
    from cowboy_sdk.routes import collect_routes

    routes = collect_routes(MODULE.HlUiHost)
    method_paths = {spec.path for kind, spec, _handler in routes if kind == "method"}
    expected_paths = {
        "/ui/",
        "/ui/index.html",
        "/ui/styles.css",
        "/ui/app.js",
        "/ui/api/dashboard",
        "/ui/api/research",
        "/ui/api/research/lessons",
        "/ui/api/trader",
        "/ui/api/health",
    }
    assert method_paths == expected_paths
    method_routes = {
        (verb, spec.path)
        for kind, spec, _handler in routes
        if kind == "method"
        for verb in spec.verbs
    }
    assert method_routes == {
        (verb, path) for path in expected_paths for verb in ("GET", "HEAD")
    }


def test_dashboard_endpoints_cap_cross_actor_reads():
    tree = ast.parse((ROOT / "main.py").read_text())
    methods = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    def count_calls(method, name):
        return sum(
            1
            for node in ast.walk(methods[method])
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        )

    assert count_calls("dashboard", "_read") == 0
    assert count_calls("research_dashboard", "read") == 3
    assert count_calls("research_lessons", "_read") == 1
    assert count_calls("trader_dashboard", "read") == 3


def test_unpriced_positions_do_not_fabricate_full_gain_or_loss(monkeypatch):
    monkeypatch.setattr(MODULE.runtime, "charge_gas", lambda *_args: None, raising=False)
    actor = MODULE.HlUiHost.__new__(MODULE.HlUiHost)
    actor.storage = {MODULE.KEY_TRADER: "0x" + "11" * 20}
    replies = {
        "get_portfolio": {
            "positions": [
                {"asset": "BTC", "side": "long", "entry_px": 100, "mark_px": None},
                {"asset": "ETH", "side": "short", "entry_px": 100, "mark_px": None},
            ],
            "last_prices": {},
        },
        "get_pnl": {"open_positions": 2},
        "get_scorecard": {},
    }
    monkeypatch.setattr(actor, "_read", lambda _target, handler: replies[handler])

    envelope = actor.trader_dashboard(b"{}")
    if isinstance(envelope, (bytes, bytearray)):
        envelope = MODULE.codec.decode(envelope)
    body = json.loads(bytes(envelope["body"]).decode("utf-8"))

    assert [(row["mark"], row["returnPct"]) for row in body["positions"]] == [
        (100, 0),
        (100, 0),
    ]


def test_browser_bundle_contains_no_fabricated_preview_portfolio():
    app = (ROOT / "hl-ui" / "app.js").read_text()
    assert "DEMO_DATA" not in app
    assert "116240" not in app
    assert "unavailableData" in app
