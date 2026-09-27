"""Offline tests for the CloudWatch event soak watcher."""

import importlib.util
import json
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "event_watch.py"
SPEC = importlib.util.spec_from_file_location("hl_event_watch", MODULE)
W = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(W)


class Result:
    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_poll_uses_one_bounded_paginated_or_query(monkeypatch):
    calls = []
    payload = [{"t": 1_234_567, "m": "live.rebalanced and live.skipped", "i": "e-1"}]

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result(stdout=json.dumps(payload))

    monkeypatch.setattr(W.subprocess, "run", run)
    events, status, error = W.poll("cal-run", 1_000_000, 2_000_000)

    assert status == "ok" and error == ""
    assert len(calls) == 1
    argv = calls[0][0]
    assert argv[argv.index("--start-time") + 1] == "1000000"
    assert argv[argv.index("--end-time") + 1] == "2000000"
    assert argv[argv.index("--filter-pattern") + 1] == W.FILTER_PATTERN
    assert "--max-items" not in argv
    assert [event["pattern"] for event in events] == ["live.skipped", "live.rebalanced"]
    assert all(event["id"] == "e-1" and event["ts_ms"] == 1_234_567 for event in events)


def test_poll_distinguishes_teardown_from_transient_failure(monkeypatch):
    monkeypatch.setattr(
        W.subprocess,
        "run",
        lambda *args, **kwargs: Result(
            returncode=255,
            stderr="ResourceNotFoundException: The specified log group does not exist",
        ),
    )
    assert W.poll("gone", 1, 2)[1] == "gone"

    monkeypatch.setattr(
        W.subprocess,
        "run",
        lambda *args, **kwargs: Result(returncode=255, stderr="ThrottlingException"),
    )
    assert W.poll("slow", 1, 2)[1] == "retry"


def test_timestamp_pruning_preserves_only_ids_that_can_overlap():
    seen = {"old": 1_000, "edge": 2_000, "new": 3_000}
    assert W.prune_seen(seen, 2_000) == {"edge": 2_000, "new": 3_000}

    events = [
        {"id": "edge", "ts_ms": 2_000},
        {"id": "fresh", "ts_ms": 3_100},
        {"id": "fresh", "ts_ms": 3_100},
    ]
    fresh = W.fresh_events(events, seen)
    assert [event["id"] for event in fresh] == ["fresh", "fresh"]
    assert seen["fresh"] == 3_100


def test_main_exits_when_the_log_group_is_gone(monkeypatch, tmp_path):
    output = tmp_path / "events.jsonl"
    monkeypatch.setattr(sys, "argv", ["event_watch.py", "gone", str(output), "0"])
    monkeypatch.setattr(W, "poll", lambda *args: ([], "gone", "missing"))

    W.main()

    assert not output.exists()


def test_quiet_success_advances_the_cursor_by_wall_clock(monkeypatch, tmp_path):
    output = tmp_path / "events.jsonl"
    calls = []
    times = iter([1_000.0, 1_001.0, 1_002.0])

    def poll(env, since_ms, end_ms):
        calls.append((env, since_ms, end_ms))
        return ([], "ok", "") if len(calls) == 1 else ([], "gone", "missing")

    monkeypatch.setattr(sys, "argv", ["event_watch.py", "cal-run", str(output), "0"])
    monkeypatch.setattr(W.time, "time", lambda: next(times))
    monkeypatch.setattr(W.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(W, "poll", poll)

    W.main()

    assert calls == [
        ("cal-run", 400_000, 1_001_000),
        ("cal-run", 941_000, 1_002_000),
    ]
    assert len(output.read_text().splitlines()) == 1


def test_main_stops_after_repeated_transient_failures(monkeypatch, tmp_path):
    output = tmp_path / "events.jsonl"
    calls = []

    def poll(*args):
        calls.append(args)
        return [], "retry", "throttled"

    monkeypatch.setattr(sys, "argv", ["event_watch.py", "slow", str(output), "0"])
    monkeypatch.setattr(W.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(W.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(W, "poll", poll)

    W.main()

    assert len(calls) == W.MAX_CONSECUTIVE_FAILURES
    assert not output.exists()
