import json
import sys
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXAMPLE_DIR / "tools"))

import edgar_poller


def test_fixture_events_normalize_side_and_notional():
    events = edgar_poller.load_fixture_events()
    assert len(events) == 3
    aapl = [event for event in events if event["ticker"] == "AAPL"]
    assert len(aapl) == 2
    assert all(event["side"] == "buy" for event in aapl)
    assert aapl[0]["notional_usd"] == 2261040.0


def test_cluster_buy_signals_are_deterministic():
    signals = edgar_poller.cluster_buy_signals(edgar_poller.load_fixture_events())
    assert signals == [
        {
            "schema": "cowboy.watchtower.cluster.v1",
            "action": "BUY",
            "ticker": "AAPL",
            "reason": "2 insider buys in fixture window",
            "event_count": 2,
            "accessions": ["0000320193-26-000101", "0000320193-26-000102"],
            "notional_usd": 3868815.0,
            "events": [event for event in edgar_poller.load_fixture_events() if event["ticker"] == "AAPL"],
        }
    ]


def test_jsonl_writer_round_trips(tmp_path):
    out = tmp_path / "events.jsonl"
    rows = edgar_poller.load_fixture_events()[:1]
    with out.open("w", encoding="utf-8") as fh:
        edgar_poller.write_jsonl(rows, fh)
    assert json.loads(out.read_text().strip())["accession"] == rows[0]["accession"]
