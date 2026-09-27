"""Offline coverage for decoding the raw order-audit storage value."""

import subprocess
import sys
import textwrap
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "order_watch.py"


def test_sdk_codec_round_trips_representative_order_audit():
    script = textwrap.dedent(
        """
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("hl_order_watch", sys.argv[1])
        watcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watcher)
        orders = [
            {
                "sym": "BTC", "height": 42, "why": "rebalance",
                "filled": 0.125, "reduce_only": False, "error": None,
                "raw": b"ok",
            },
            {
                "sym": "ETH", "height": -1, "why": "close",
                "filled": 0.0, "reduce_only": True, "error": "refused",
            },
        ]
        assert watcher.codec.decode(watcher.codec.encode(orders)) == orders
        """
    )

    subprocess.run([sys.executable, "-c", script, str(MODULE)], check=True)
