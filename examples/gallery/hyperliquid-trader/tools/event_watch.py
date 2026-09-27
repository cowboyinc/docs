#!/usr/bin/env python3
"""Pull actor events out of CloudWatch to local disk, continuously.

WHY THIS EXISTS
---------------
Run5 emitted `live.plan_skips` for its entire life — instrumentation added
specifically to answer "why did a rebalance plan zero orders" — and none of it was
ever read. Then `cowboy-env down` deleted the log groups and the data was gone for
good (memory: river-teardown-destroys-logs).

The other two watchers sample `/height`, runner nonces and `get_pnl`. Neither
captures events, so anything event-based is lost by default. That made run5
unfalsifiable for the two changes it was built to test:

  * `evict_stale`  -> emits "evicted stale mempool txs on finalize" (warn)
  * `plan_orders`  -> emits `live.plan_skips` with per-symbol skip reasons

Both are the *only* evidence those changes did anything. Without this, a run can
only ever tell us whether the chain stayed up — not whether the fixes worked.

USAGE
    event_watch.py <env-name> <out.jsonl> [interval_secs]

Writes one JSON object per poll: {ts, window, counts, events[]}. Dedupes by
CloudWatch eventId across polls so restarts do not double-count.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

# Substrings worth capturing. Deliberately narrow: the validator emits tens of
# thousands of lines a minute under load (run5 logged 30,889 tx-received in 44
# minutes), so a broad filter would cost more than it returns.
PATTERNS = [
    "live.plan_skips",  # why symbols were skipped — the unread instrumentation
    "live.tick_failed",  # a guarded tick that raised (loop survived)
    "live.skipped",  # _tick_ok guard refused a fire
    "live.rebalanced",  # the positive control
    "live.unclosable",  # held position with no exit path — alarm on this
    "evicted stale mempool",  # evict_stale actually firing
    "research.tick_failed",
]
FILTER_PATTERN = " ".join('?"' + pattern + '"' for pattern in PATTERNS)
OVERLAP_MS = 60_000
MAX_CONSECUTIVE_FAILURES = 5


def iso(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def poll(env, since_ms, end_ms):
    """Fetch one bounded, fully paginated OR window.

    Returns ``(events, status, error)`` where status is ``ok``, ``retry`` for
    a transient failure, or ``gone`` when the environment's log group no
    longer exists. The caller advances its cursor only after ``ok``.
    """
    try:
        # Do not set --max-items: the AWS CLI automatically follows every
        # nextToken. Advancing a wall-clock cursor after a truncated page would
        # permanently lose the remaining matches in that window.
        raw = subprocess.run(
            [
                "aws",
                "logs",
                "filter-log-events",
                "--region",
                "us-east-1",
                "--log-group-name",
                f"/cowboy/river/{env}/validator-1",
                "--start-time",
                str(since_ms),
                "--end-time",
                str(end_ms),
                "--filter-pattern",
                FILTER_PATTERN,
                "--query",
                "events[].{t:timestamp,m:message,i:eventId}",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return [], "retry", str(exc)[:240]
    if raw.returncode != 0:
        error = str(raw.stderr or raw.stdout or "aws logs failed")[:240]
        if "ResourceNotFoundException" in error or "log group does not exist" in error:
            return [], "gone", error
        return [], "retry", error

    out = []
    try:
        decoded = json.loads(raw.stdout or "[]")
        if not isinstance(decoded, list):
            raise ValueError("filter-log-events did not return an event list")
        for event in decoded:
            message = str(event["m"])
            for pattern in PATTERNS:
                if pattern in message:
                    out.append(
                        {
                            "pattern": pattern,
                            "ts": iso(event["t"]),
                            "ts_ms": int(event["t"]),
                            "id": event["i"],
                            "msg": message[:600],
                        }
                    )
    except Exception as exc:
        return [], "retry", str(exc)[:240]
    return out, "ok", ""


def fresh_events(events, seen):
    """Return events unseen before this poll and remember their timestamps."""
    known = set(seen)
    fresh = [event for event in events if event["id"] not in known]
    for event in fresh:
        event_id = event["id"]
        seen[event_id] = max(seen.get(event_id, 0), int(event["ts_ms"]))
    return fresh


def prune_seen(seen, since_ms):
    """Drop ids older than the earliest timestamp a later poll can return."""
    return {event_id: ts for event_id, ts in seen.items() if ts >= since_ms}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    env, path = sys.argv[1], sys.argv[2]
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 180

    seen = {}
    consecutive_failures = 0
    # Start 10 min back so a watcher started slightly late still catches the
    # beginning of the run.
    since = int(time.time() * 1000) - 600_000

    while True:
        poll_start = int(time.time() * 1000)
        events, status, error = poll(env, since, poll_start)
        if status != "ok":
            consecutive_failures += 1
            print(
                f"event watcher poll failed ({status}): {error}",
                file=sys.stderr,
                flush=True,
            )
            if status == "gone" or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return
            time.sleep(interval)
            continue
        consecutive_failures = 0
        fresh = fresh_events(events, seen)
        counts = {}
        for e in fresh:
            counts[e["pattern"]] = counts.get(e["pattern"], 0) + 1
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "window_start": iso(since),
            "counts": counts,
            "events": [
                {key: value for key, value in event.items() if key != "ts_ms"}
                for event in fresh
            ],
        }
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if counts:
            print(f"{rec['ts']}  {counts}", flush=True)
        # Move by the successfully scanned wall-clock window, even when nothing
        # matched, while retaining overlap for late-arriving log events.
        since = max(since, poll_start - OVERLAP_MS)
        seen = prune_seen(seen, since)
        time.sleep(interval)


if __name__ == "__main__":
    main()
