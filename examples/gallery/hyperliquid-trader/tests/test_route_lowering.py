"""Offline coverage for canonical CIP-15 route ordering."""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_STUB = ROOT / "tests" / "sdk_stub.py"
UI_LOWERER = ROOT / "ui" / "tools" / "lower_routes.py"
NOTIFY_LOWERER = ROOT / "tools" / "lower_notify_routes.py"


def test_lowerers_use_canonical_order_for_tied_verbs():
    script = textwrap.dedent(
        """
        import importlib.util
        import sys

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        stub = load("hl_sdk_stub", sys.argv[1])
        stub.install()
        ui = load("hl_ui_lower_routes", sys.argv[2])
        notify = load("hl_notify_lower_routes", sys.argv[3])
        unordered = [
            {"verb": "PATCH", "priority": 7, "path": "/same"},
            {"verb": "POST", "priority": 7, "path": "/same"},
            {"verb": "GET", "priority": 7, "path": "/same"},
        ]

        for lowerer in (ui, notify):
            routes = [dict(route) for route in unordered]
            lowerer._sort_routes(routes)
            assert [route["verb"] for route in routes] == ["GET", "POST", "PATCH"]
        """
    )

    subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(SDK_STUB),
            str(UI_LOWERER),
            str(NOTIFY_LOWERER),
        ],
        check=True,
    )
