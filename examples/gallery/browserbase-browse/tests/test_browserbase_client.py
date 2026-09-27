"""Offline checks of signed result delivery and exact live-request replay."""
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import traceback
from threading import Thread
from types import SimpleNamespace
import urllib.error

import cbor2
import pytest

import browserbase_client as client
import test_browserbase_comparison as comparison
from conftest import ACTOR, BUYER
from cowboy_sdk import codec
from test_browserbase import FETCH_OK, last_job, submit


@pytest.fixture
def args():
    return SimpleNamespace(actor="0x" + ACTOR.hex(), hire_id=42, rpc="https://rpc.example",
                           wallet_key="wallet-file", cowboy="cowboy", wait_seconds=30)


def terminal(request_id=17, **changes):
    return dict({"request_id": request_id, "hire_id": 42, "operation": "fetch", "status": "ok", "ok": True,
                 "output": {"ok": True, "operation": "fetch", "data": dict(FETCH_OK, content="A" * 8_000)}}, **changes)


def test_signed_reader_uses_accepted_id_across_pending_and_full_chunks(actor, host, args, monkeypatch, capsys):
    import actors.browse.main as bb
    from test_browserbase import resume
    # Unrelated work already has ID 1. The client must use its receipt, not a
    # counter read, and each read must call the signed public handler.
    submit(actor, host, "fetch", url="https://unrelated.example")
    pending_job = None
    reads = 0
    calls = []

    def execute(config, handler, payload, event_name, deadline):
        nonlocal pending_job, reads
        calls.append((handler, payload, deadline))
        if handler == "get_result":
            reads += 1
            if reads == 2:
                resume(host, pending_job, dict(FETCH_OK, content="A" * 8_000))
        host.set_context(sender=BUYER, tx_hash=(len(calls) + 10).to_bytes(32, "big"))
        host.events.clear()
        getattr(bb, handler)(codec.encode(payload))
        if handler == "fetch":
            pending_job = last_job(host)
        return next(event for name, event in host.events if name == event_name)

    monkeypatch.setattr(client, "execute", execute)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)
    result = client.invoke(args, "fetch", {"url": "https://example.com"})
    assert result["request_id"] == 2 and result["output"]["data"]["content"] == "A" * 8_000
    assert [handler for handler, _, _ in calls] == ["fetch"] + ["get_result"] * 4
    assert all(payload["request_id"] == 2 for handler, payload, _ in calls if handler == "get_result")
    assert len({deadline for _, _, deadline in calls}) == 1
    assert "Accepted request 2" in capsys.readouterr().err


@pytest.mark.parametrize("change", [
    {"request_id": 18}, {"request_id": True}, {"hire_id": 43}, {"offset": 1},
    {"total": 38_001}, {"total": True}, {"data": b""}, {"data": "text"},
])
def test_reader_rejects_wrong_or_malformed_chunk(args, monkeypatch, change):
    raw = cbor2.dumps(terminal())
    chunk = dict(request_id=17, hire_id=42, offset=0, total=len(raw), data=raw[:3900])
    chunk.update(change)
    monkeypatch.setattr(client, "execute", lambda *unused: chunk)
    with pytest.raises(ValueError):
        client.get_result(args, 17, "fetch")


def test_reader_rejects_result_identity_and_size_change(args, monkeypatch):
    for result in (terminal(request_id=18), terminal(hire_id=43), terminal(operation="search"), terminal(status="error")):
        raw = cbor2.dumps(result)
        monkeypatch.setattr(client, "execute", lambda config, handler, payload, event, deadline: {
            "request_id": 17, "hire_id": 42, "offset": payload["receipt_offset"], "total": len(raw),
            "data": raw[payload["receipt_offset"]:payload["receipt_offset"] + 3900]})
        with pytest.raises(ValueError):
            client.get_result(args, 17, "fetch")
    raw = cbor2.dumps(terminal())
    monkeypatch.setattr(client, "execute", lambda config, handler, payload, event, deadline: {
        "request_id": 17, "hire_id": 42, "offset": payload["receipt_offset"],
        "total": len(raw) + (1 if payload["receipt_offset"] else 0),
        "data": raw[payload["receipt_offset"]:payload["receipt_offset"] + 3900]})
    with pytest.raises(ValueError, match="size changed"):
        client.get_result(args, 17, "fetch")


def test_execute_uses_exact_transaction_receipt_and_emitter(args, monkeypatch):
    tx = "0x" + "12" * 32
    accepted = {"request_id": 17}
    payload = {"request_id": 17, "hire_id": 42}

    def run(command, **kwargs):
        assert "--no-wait" in command
        assert codec.decode(bytes.fromhex(command[command.index("--payload") + 1][2:])) == payload
        assert kwargs["env"]["COWBOY_RPC_URL"] == args.rpc
        return SimpleNamespace(stdout="Actor: 0x" + "ff" * 32 + "\n  Transaction hash: " + tx + "\n")

    def urlopen(url, **kwargs):
        assert url == args.rpc + "/transaction/" + tx + "/receipt"
        event = [args.actor, "wrapper.request_accepted", "0x" + cbor2.dumps(accepted).hex()]
        return io.BytesIO(json.dumps({"status": "Success", "events": [["0x" + "bb" * 20, event[1], event[2]], event]}).encode())

    monkeypatch.setattr(client.subprocess, "run", run)
    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    assert client.execute(args, "fetch", payload, "wrapper.request_accepted", client.time.monotonic() + 30) == accepted


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError, subprocess.TimeoutExpired])
def test_execute_errors_do_not_print_wallet_input(args, monkeypatch, failure):
    args.wallet_key = "synthetic-wallet-secret-do-not-print"

    def run(command, **kwargs):
        if failure is subprocess.CalledProcessError:
            raise failure(1, command, output=args.wallet_key, stderr=args.wallet_key)
        raise failure(command, 1, output=args.wallet_key, stderr=args.wallet_key)

    monkeypatch.setattr(client.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        client.execute(args, "fetch", {}, "wrapper.request_accepted", client.time.monotonic() + 30)
    assert args.wallet_key not in "".join(traceback.format_exception(error.value))


def test_invoke_timeout_keeps_acceptance_id_and_does_not_resubmit(args, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(client, "execute", lambda *call: calls.append(call) or {
        "request_id": 17, "hire_id": 42, "operation": "fetch", "ok": True,
        "status": "pending", "poll_handler": "get_result"})
    monkeypatch.setattr(client, "get_result", lambda *unused, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        client.invoke(args, "fetch", {"url": "https://example.com"})
    assert len(calls) == 1 and "Accepted request 17" in capsys.readouterr().err


def test_replay_preserves_emitted_method_headers_and_fractional_body(actor, host, monkeypatch):
    captured = []
    monkeypatch.setattr(comparison, "http", lambda *args: captured.append(args) or FETCH_OK)
    _, job = submit(actor, host, "fetch", url="https://example.com", format="json", schema={"minimum": 0.5})
    kw = job["payload"]["kwargs"]
    kw["headers"] = {"Wrong-Auth": "${BROWSERBASE_API_KEY}", "Accept": "application/json"}
    comparison._live_reply(job, "test-key")
    assert captured == [(kw["url"], "POST", {"Wrong-Auth": "test-key", "Accept": "application/json"}, kw["raw_body"])]
    assert json.loads(captured[0][3])["schema"]["minimum"] == 0.5
    _, job = submit(actor, host, "browse_result", run_id="a-run")
    comparison._live_reply(job, "test-key")
    assert captured[-1][1:] == ("GET", {"Accept": "application/json", "X-BB-API-Key": "test-key"}, None)
    job["payload"]["kwargs"]["body"] = ""
    with pytest.raises(ValueError, match="unexpected body"):
        comparison._live_reply(job, "test-key")
    assert len(captured) == 2


@pytest.mark.parametrize("status", ["FAILED", "STOPPED", "TIMED_OUT", "RUNNING"])
def test_agent_comparison_requires_completed_matching_run(status):
    with pytest.raises(AssertionError):
        comparison.completed_run({"run_id": "run-1", "status": status, "done": True,
                                  "result": {"heading": "Example Domain"}}, "run-1")
    with pytest.raises(AssertionError):
        comparison.completed_run({"run_id": "other", "status": "COMPLETED", "done": True,
                                  "result": {"heading": "Example Domain"}}, "run-1")


@pytest.mark.parametrize("field,value", [("secrets", None), ("secrets", [["0x" + "99" * 20, "BROWSERBASE_API_KEY"]]),
                                        ("hire", None), ("hire", {"hire_id": 99})])
def test_replay_requires_declared_secret_and_hire(actor, host, monkeypatch, field, value):
    _, job = submit(actor, host, "fetch", url="https://example.com")
    if value is None:
        del job["payload"]["kwargs"][field]
    else:
        job["payload"]["kwargs"][field] = value
    def unexpected_http(*args):
        pytest.fail("malformed job reached HTTP")
    monkeypatch.setattr(comparison, "http", unexpected_http)
    with pytest.raises(ValueError):
        comparison._live_reply(job, "test-key")


def test_vendor_http_does_not_follow_redirects():
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                comparison.http(f"http://127.0.0.1:{server.server_port}/start", "GET", {"X-BB-API-Key": "test-key"})
            assert error.value.code == 302
            assert visited == ["/start"]
        finally:
            server.shutdown()
            thread.join()


def test_demo_checks_decoded_success_not_content_words():
    source = (Path(__file__).resolve().parents[1] / "demo.sh").read_text()
    script = source.split('printf \'%s\' "$RESULT" | python3 -c \'\n', 1)[1].split("' || die", 1)[0]
    error = terminal(status="error", ok=False,
                     output={"ok": False, "operation": "fetch", "error": "content extraction failed"})
    for value, expected in ((terminal(), 0), (error, 1)):
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps(value), capture_output=True, text=True)
        assert (result.returncode != 0) == bool(expected)
