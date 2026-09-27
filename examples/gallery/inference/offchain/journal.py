"""Session usage journal (CIP §3.4, §6).

The journal is the accounting authority: a reservation is durable BEFORE any
provider dispatch, and each request's outcome is recorded once. Usage
completeness is tracked separately from transport outcome; unknown usage is
never written as zero.

Records go to the hire volume as `usage/<session>/<segment>.jsonl` under CAS,
one segment per flush, never per request. On restart, `recover()` rewrites
any `in_flight` record without an outcome as `process_lost` / unknown.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict

from .volume import Volume

OUTCOMES = ("in_flight", "completed", "upstream_error", "client_aborted", "process_lost")


class DuplicateRequest(Exception):
    """reserve() was asked for a request_id this session already holds.
    Carries the existing record; the caller must not dispatch again."""

    def __init__(self, record: "Record"):
        super().__init__(record.request_id)
        self.record = record


@dataclass
class Record:
    request_id: str
    slot: str
    hire_id: int
    session_id: str
    key_id: str
    model: str
    target: str
    ts: int
    limits: dict
    price_version: str | None
    outcome: str = "in_flight"
    usage: dict = field(default_factory=lambda: {"pending": True})
    finished_ts: int | None = None

    def line(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n").encode()


class Journal:
    def __init__(self, volume: Volume, *, slot: str, hire_id: int, session_id: str, price_version: str | None, clock=lambda: int(time.time())):
        self.volume = volume
        self.slot = slot
        self.hire_id = hire_id
        self.session_id = session_id
        self.price_version = price_version
        self.clock = clock
        # Every record this session has seen, seeded from the durable segments
        # so a resumed session still dedups a replayed request_id against what
        # it already committed. Bounded by the session's max_requests.
        self._records: dict[str, Record] = {
            rid: Record(**rec) for (_sid, rid), rec in latest_records(volume, f"usage/{session_id}/").items()
        }
        self._dirty: set[str] = set()
        self._segment = _last_segment(volume, session_id)

    def reserve(self, request_id: str, *, key_id: str, model: str, target: str, limits: dict) -> Record:
        """Durable reservation before dispatch. A repeated request_id raises
        DuplicateRequest with the existing record so no second execution starts,
        in this process or a resumed one."""
        if request_id in self._records:
            raise DuplicateRequest(self._records[request_id])
        rec = Record(request_id, self.slot, self.hire_id, self.session_id, key_id, model, target, self.clock(), dict(limits), self.price_version)
        self._records[request_id] = rec
        self._dirty.add(request_id)
        self.flush()
        return rec

    def finish(self, request_id: str, outcome: str, usage: dict) -> Record:
        """Record the outcome once. `usage` is {"known": {"input": n, "output": n}}
        or {"unknown": True}; a zero is only ever a known zero."""
        if outcome not in OUTCOMES or outcome == "in_flight":
            raise ValueError(f"bad outcome {outcome!r}")
        if not (("known" in usage) ^ ("unknown" in usage)):
            raise ValueError("usage must be exactly one of known/unknown")
        rec = self._records.get(request_id)
        if rec is None:
            raise KeyError(request_id)
        if rec.outcome != "in_flight":
            return rec
        rec.outcome = outcome
        rec.usage = usage
        rec.finished_ts = self.clock()
        self._dirty.add(request_id)
        return rec

    def flush(self) -> str | None:
        """Write every changed record as one new segment (CAS). Segment numbers
        are strictly increasing within the session and never reuse a name that
        already exists in the volume, so a restarted session appends."""
        if not self._dirty:
            return None
        records = b"".join(self._records[rid].line() for rid in sorted(self._dirty))
        chosen: list[str] = []

        def apply(v: Volume) -> dict[str, bytes]:
            self._segment = max(self._segment, _last_segment(v, self.session_id)) + 1
            path = _segment_path(self.session_id, self._segment)
            chosen[:] = [path]
            return {path: segment_header(self.session_id, self._segment, self.clock()) + records}

        self.volume.mutate(apply)
        self._dirty.clear()
        return chosen[0]

    @staticmethod
    def recover(volume: Volume, session_id: str, *, clock=lambda: int(time.time())) -> list[str]:
        """After a crash: any record whose latest state is `in_flight` becomes
        `process_lost` with unknown usage. Returns the affected request ids."""
        latest = latest_records(volume, f"usage/{session_id}/")
        lost = sorted(rid for (_sid, rid), rec in latest.items() if rec["outcome"] == "in_flight")
        if lost:
            now = clock()
            lines = []
            for rid in lost:
                rec = dict(latest[(session_id, rid)], outcome="process_lost", usage={"unknown": True}, finished_ts=now)
                lines.append((json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode())
            records = b"".join(lines)

            def apply(v: Volume) -> dict[str, bytes]:
                n = _last_segment(v, session_id) + 1
                return {_segment_path(session_id, n): segment_header(session_id, n, now) + records}

            volume.mutate(apply)
        return lost


def segment_header(session_id: str, segment: int, committed_at: int) -> bytes:
    """First line of every segment: when the writer committed it. This is the
    writer's clock at commit, distinct from each record's admission time."""
    return (json.dumps({"kind": "segment", "session_id": session_id, "segment": segment, "committed_at": committed_at}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _segment_path(session_id: str, n: int) -> str:
    return f"usage/{session_id}/{n:06d}.jsonl"


def _last_segment(volume: Volume, session_id: str) -> int:
    """Highest segment number present for the session, 0 if none."""
    prefix = f"usage/{session_id}/"
    nums = []
    for path in volume.list(prefix):
        stem = path[len(prefix):].split(".", 1)[0]
        if stem.isdigit():
            nums.append(int(stem))
    return max(nums, default=0)


def latest_records(volume: Volume, prefix: str = "usage/") -> dict[tuple[str, str], dict]:
    """Fold segments (in segment order) into the last-written state per
    (session_id, request_id)."""
    latest: dict[tuple[str, str], dict] = {}
    for path in volume.list(prefix):
        for line in (volume.read(path) or b"").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "segment":
                continue
            latest[(rec["session_id"], rec["request_id"])] = rec
    return latest


def last_committed_at(volume: Volume, prefix: str = "usage/") -> int | None:
    """Latest segment commit time under `prefix`, None if no segment exists."""
    latest = None
    for path in volume.list(prefix):
        first = (volume.read(path) or b"").split(b"\n", 1)[0]
        if not first:
            continue
        head = json.loads(first)
        if head.get("kind") == "segment":
            latest = head["committed_at"] if latest is None else max(latest, head["committed_at"])
    return latest
