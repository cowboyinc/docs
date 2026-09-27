"""Offline tests for the pipeline watcher's display helpers."""

import importlib.util
import os
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "tools" / "watch_pipeline.py"
SPEC = importlib.util.spec_from_file_location("hl_watch_pipeline", MODULE)
W = importlib.util.module_from_spec(SPEC)
with (
    mock.patch.dict(
        os.environ,
        {"RPC": "http://test", "RESEARCH": "0x1", "TRADER": "0x2"},
    ),
    mock.patch("urllib.request.urlopen", side_effect=OSError),
    mock.patch("time.sleep", side_effect=KeyboardInterrupt),
):
    SPEC.loader.exec_module(W)


def test_money_rounds_before_choosing_color_and_normalizes_signed_zero():
    assert W.money(-0.0001) == "0.00"
    assert W.money(0.0001) == "0.00"


def test_money_preserves_color_for_visible_values():
    assert W.money(-0.01) == f"{W.RED}-0.01{W.RESET}"
    assert W.money(0.01) == f"{W.GREEN}+0.01{W.RESET}"
