import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain.journal import DuplicateRequest, Journal, last_committed_at, latest_records
from offchain.volume import LocalVolume


def journal(tmp_path):
    vol = LocalVolume(tmp_path / "vol")
    return vol, Journal(vol, slot="cloud", hire_id=7, session_id="s1", price_version="p1", clock=lambda: 42)


def test_reservation_is_durable_before_dispatch(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={"max_tokens": 10})
    latest = latest_records(vol)
    assert latest[("s1", "r1")]["outcome"] == "in_flight" and latest[("s1", "r1")]["usage"] == {"pending": True}


def test_finish_once_and_unknown_is_never_zero(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.finish("r1", "client_aborted", {"unknown": True})
    j.finish("r1", "completed", {"known": {"input": 5, "output": 9}})  # second finish is ignored
    j.flush()
    rec = latest_records(vol)[("s1", "r1")]
    assert rec["outcome"] == "client_aborted" and rec["usage"] == {"unknown": True}
    with pytest.raises(ValueError):
        j.finish("r1", "completed", {"known": {}, "unknown": True})


def test_recover_marks_in_flight_as_process_lost(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.reserve("r2", key_id="k", model="m", target="t", limits={})
    j.finish("r2", "completed", {"known": {"input": 1, "output": 1}})
    j.flush()
    lost = Journal.recover(vol, "s1", clock=lambda: 99)
    assert lost == ["r1"]
    latest = latest_records(vol)
    assert latest[("s1", "r1")]["outcome"] == "process_lost" and latest[("s1", "r1")]["usage"] == {"unknown": True}
    assert latest[("s1", "r2")]["outcome"] == "completed"


def test_segments_are_unique_and_cas_commits(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.reserve("r2", key_id="k", model="m", target="t", limits={})
    paths = vol.list("usage/s1/")
    assert len(paths) == 2 and len(set(paths)) == 2


def test_replayed_request_id_never_reserves_twice_even_after_flush(tmp_path):
    vol, j = journal(tmp_path)
    a = j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.finish("r1", "completed", {"known": {"input": 1, "output": 1}})
    j.flush()
    with pytest.raises(DuplicateRequest) as e:
        j.reserve("r1", key_id="k", model="m", target="t", limits={})
    assert e.value.record is a and a.outcome == "completed"
    assert len(vol.list("usage/s1/")) == 2  # reserve + finish flush; no third segment


def test_restart_appends_after_existing_segments(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.reserve("r2", key_id="k", model="m", target="t", limits={})
    before = vol.list("usage/s1/")
    j2 = Journal(vol, slot="cloud", hire_id=7, session_id="s1", price_version="p1", clock=lambda: 43)
    j2.reserve("r3", key_id="k", model="m", target="t", limits={})
    after = vol.list("usage/s1/")
    assert after[: len(before)] == before and len(after) == len(before) + 1
    assert after[-1] == "usage/s1/000003.jsonl"
    assert vol.read(before[0]) is not None and b"r1" in vol.read(before[0])


def test_records_keyed_by_session_and_request(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j2 = Journal(vol, slot="cloud", hire_id=7, session_id="s2", price_version="p1", clock=lambda: 43)
    j2.reserve("r1", key_id="k", model="m", target="t", limits={})
    latest = latest_records(vol)
    assert set(latest) == {("s1", "r1"), ("s2", "r1")}


def test_segment_header_carries_commit_time(tmp_path):
    vol = LocalVolume(tmp_path / "vol")
    assert last_committed_at(vol) is None
    t = {"now": 10}
    j = Journal(vol, slot="cloud", hire_id=7, session_id="s1", price_version="p1", clock=lambda: t["now"])
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    t["now"] = 25
    j.finish("r1", "completed", {"known": {"input": 1, "output": 1}})
    j.flush()
    assert last_committed_at(vol) == 25
    assert latest_records(vol)[("s1", "r1")]["ts"] == 10
    Journal.recover(vol, "s1", clock=lambda: 30)  # nothing in flight: no new segment
    assert last_committed_at(vol) == 25


def test_resumed_session_dedups_against_durable_records(tmp_path):
    vol, j = journal(tmp_path)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.finish("r1", "completed", {"known": {"input": 1, "output": 1}})
    j.flush()
    j.reserve("r2", key_id="k", model="m", target="t", limits={})  # still in flight
    before = vol.list("usage/s1/")
    j2 = Journal(vol, slot="cloud", hire_id=7, session_id="s1", price_version="p1", clock=lambda: 43)
    with pytest.raises(DuplicateRequest) as e:
        j2.reserve("r1", key_id="k", model="m", target="t", limits={})
    assert e.value.record.outcome == "completed"
    with pytest.raises(DuplicateRequest) as e:
        j2.reserve("r2", key_id="k", model="m", target="t", limits={})
    assert e.value.record.outcome == "in_flight"
    assert vol.list("usage/s1/") == before  # no new reservation was written
