import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain.cbfs_volume import CbfsError, CbfsVolume
from offchain.grants import GrantAdapter, GrantError
from offchain.journal import Journal, latest_records
from offchain.volume import ConflictError

FAKE = os.path.join(os.path.dirname(__file__), "fakebin", "cbfs")


def vol(tmp_path, **kw):
    return CbfsVolume("hire-vol", str(tmp_path / "state"), binary=FAKE, **kw)


def calls(tmp_path):
    p = tmp_path / "state" / "calls.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_root_list_read_commit_round_trip(tmp_path):
    v = vol(tmp_path, rpc_url="http://127.0.0.1:4000")
    r0 = v.root()
    assert v.list("") == [] and v.read("keys/x.json") is None
    r1 = v.commit(r0, {"keys/a.json": b"{}", "usage/s1/000001.jsonl": b"line\n"})
    assert r1 != r0 and v.root() == r1
    assert v.list("keys/") == ["keys/a.json"] and v.read("keys/a.json") == b"{}"
    r2 = v.commit(r1, {"keys/a.json": None})
    assert v.list("keys/") == [] and r2 != r1
    assert v.commit(r2, {}) == r2  # nothing to do, no process
    assert any(c[:2] == ["--rpc-url", "http://127.0.0.1:4000"] or "--rpc-url" not in c for c in calls(tmp_path))


def test_conflict_maps_to_ConflictError_and_mutate_retries(tmp_path, monkeypatch):
    v = vol(tmp_path)
    r0 = v.root()
    v.commit(r0, {"keys/a.json": b"1"})
    with pytest.raises(ConflictError):
        v.commit(r0, {"keys/b.json": b"2"})
    # Root moves under a mutate(): first attempt conflicts, second lands.
    monkeypatch.setenv("FAKE_CBFS_MOVE_ROOT", "1")
    v.mutate(lambda cur: {"keys/c.json": b"3"}, retries=2)
    assert "keys/c.json" in v.list("keys/")


def test_journal_runs_on_cbfs_volume(tmp_path):
    v = vol(tmp_path)
    j = Journal(v, slot="cloud", hire_id=1, session_id="s1", price_version=None, clock=lambda: 5)
    j.reserve("r1", key_id="k", model="m", target="t", limits={})
    j.finish("r1", "completed", {"known": {"input": 1, "output": 2}})
    j.flush()
    assert latest_records(v)[("s1", "r1")]["outcome"] == "completed"


def test_cli_failures_surface(tmp_path):
    v = CbfsVolume("hire-vol", str(tmp_path / "state"), binary="/nonexistent/cbfs")
    with pytest.raises((CbfsError, FileNotFoundError)):
        v.root()
    v = vol(tmp_path, timeout_s=0.001)
    with pytest.raises(CbfsError):
        v.root()


def test_read_grant_adapter(tmp_path, monkeypatch):
    g = GrantAdapter("hire-vol", "0x" + "ab" * 20, str(tmp_path / "state"), binary=FAKE, extra_args=["--trusted-checkpoint", "/ck.bin"])
    grantee = "0x" + "cd" * 20
    sk, ek = "0x" + "11" * 32, "22" * 32
    grant = g.put_read_grant(grantee, sk, ek)
    assert grant.signing_key == "11" * 32 and grant.encryption_key == "22" * 32 and grant.owner == "0x" + "ab" * 20
    g.remove_read_grant(grantee)
    c = calls(tmp_path)
    assert c[0][:2] == ["--trusted-checkpoint", "/ck.bin"] and c[0][2:5] == ["volume", "allow", "hire-vol"] and "--signing-key" in c[0] and "--mode" in c[0]
    assert c[1][-1] == "--remove"
    for bad in [("nope", sk, ek), (grantee, "short", ek), (grantee, sk, "zz" * 32)]:
        with pytest.raises(GrantError):
            g.put_read_grant(*bad)
    monkeypatch.setenv("FAKE_CBFS_ALLOW_FAIL", "1")
    with pytest.raises(GrantError):
        g.put_read_grant(grantee, sk, ek)


def test_absence_only_from_exit_code_and_output_is_bounded(tmp_path, monkeypatch):
    v = vol(tmp_path)
    v.commit(v.root(), {"keys/a.json": b"{}"})
    assert v.read("keys/missing.json") is None
    monkeypatch.setenv("FAKE_CBFS_NETWORK_FAIL", "1")
    with pytest.raises(CbfsError):  # error text mentions "not found" but exit code is 1
        v.read("keys/missing.json")
    monkeypatch.delenv("FAKE_CBFS_NETWORK_FAIL")
    v.max_output_bytes = 1000
    monkeypatch.setenv("FAKE_CBFS_HUGE", "5000")
    with pytest.raises(CbfsError, match="exceeds"):
        v.read("keys/a.json")
    monkeypatch.setenv("FAKE_CBFS_HUGE", "900")
    assert len(v.read("keys/a.json")) == 900


IO_CHILD = os.path.join(os.path.dirname(__file__), "fakebin", "io-child")


def io_vol(tmp_path, **kw):
    return CbfsVolume("v", str(tmp_path / "state"), binary=IO_CHILD, **kw)


def test_blocking_stdin_write_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("IO_CHILD", "sleep-no-read")
    v = io_vol(tmp_path, timeout_s=0.3)
    started = time.monotonic()
    with pytest.raises(CbfsError, match="timed out"):
        v._run("tx", stdin=b"x" * (2 * 1024 * 1024))
    assert time.monotonic() - started < 2


def test_stderr_burst_before_stdout_does_not_wedge(tmp_path, monkeypatch):
    monkeypatch.setenv("IO_CHILD", "stderr-first")
    v = io_vol(tmp_path, timeout_s=5)
    r = v._run("root")
    assert r.stdout == b"{}" and len(r.stderr) == 64 * 1024


def test_stderr_is_capped_while_draining_and_stdin_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("IO_CHILD", "big-stderr")
    v = io_vol(tmp_path, timeout_s=5)
    with pytest.raises(CbfsError) as e:
        v._run("root")
    assert len(str(e.value)) < 700
    monkeypatch.setenv("IO_CHILD", "echo")
    payload = b"y" * (3 * 1024 * 1024)
    assert v._run("tx", stdin=payload).stdout == payload
    monkeypatch.setenv("IO_CHILD", "hang-after-output")
    v.timeout_s = 0.3
    with pytest.raises(CbfsError, match="timed out"):
        v._run("root")


def test_transient_429_on_commit_is_absorbed_and_exhaustion_is_a_busy_conflict(tmp_path, monkeypatch):
    from offchain.cbfs_volume import _is_busy
    v = vol(tmp_path)
    monkeypatch.setenv("FAKE_CBFS_BUSY_ONCE", "1")
    r1 = v.commit(v.root(), {"keys/a.json": b"1"})  # one 429, retried inside the runner
    assert v.list("keys/") == ["keys/a.json"] and r1 == v.root()
    # Classifier for what survives the runner's retries.
    assert _is_busy("cbfs tx failed (1): error: tx: committing: commit error: HTTP status client error (429 Too Many Requests)")
    assert _is_busy("cbfs tx failed (1): error: tx: committing: commit error: manifest commit-v2 failed: ErrConflict")
    assert _is_busy('cbfs tx failed (1): error: tx: committing: commit error: manifest commit-v2 failed: {"detail":"E1213 (1213)","error":"tx_reverted"}')
    assert _is_busy('cbfs tx failed (1): error: tx: reconciling pending commit: commit error: pending commit replay failed: {"detail":"E1213 (1213)","error":"tx_reverted"}')
    assert not _is_busy("cbfs tx failed (1): error: tx: uploading x: node returned error: ErrAuth")
    assert not _is_busy("cbfs root failed (1): error: E1213")


def test_rate_limit_is_retried_inside_the_runner(tmp_path, monkeypatch):
    v = vol(tmp_path)
    monkeypatch.setenv("FAKE_CBFS_429_TIMES", "3")
    assert len(v.root()) == 64  # three 429s, then success
    (tmp_path / "state" / "429-count").unlink()
    monkeypatch.setenv("FAKE_CBFS_429_TIMES", "50")
    with pytest.raises(CbfsError, match="429"):
        v.list("")


def test_post_commit_propagation_lag_is_retried(tmp_path, monkeypatch):
    v = vol(tmp_path)
    monkeypatch.setenv("FAKE_CBFS_429_TIMES", "2")
    monkeypatch.setenv("FAKE_CBFS_TRANSIENT_MSG", "error: opening Cowboy volume: manifest node placement unavailable for root 9d88a0b2cd8546cb")
    assert len(v.root()) == 64


def test_relay_connection_refusal_is_retried(tmp_path, monkeypatch):
    v = vol(tmp_path)
    monkeypatch.setenv("FAKE_CBFS_429_TIMES", "2")
    monkeypatch.setenv("FAKE_CBFS_TRANSIENT_MSG", "error: verifying relay identity for 127.0.0.1:9243: transport error: connection failed: aborted by peer: the server refused to accept a new connection")
    assert v.list("") == []
