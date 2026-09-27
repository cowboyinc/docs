"""Offline checks for exact receipt identity and independent live comparisons."""

import io
import json
import time
import traceback
from types import SimpleNamespace

import cbor2
import pytest

import live_e2b as live

ACTOR = "0x" + "aa" * 20
ARGS = SimpleNamespace(actor=ACTOR, hire_id=42, rpc="https://node.invalid",
                       wallet_key="buyer.key", cowboy="cowboy", wait_seconds=30)
RESULT = {"request_id": 19, "hire_id": 42, "operation": "run", "ok": True,
          "status": "ok", "output": {"ok": True, "operation": "run", "data": {
              "stdout": "42\n", "stderr": "", "results": [], "error": None}}}


def receipt(payload, topic="wrapper.result_chunk", actor=ACTOR):
    return {"status": "Success", "events": [[actor, topic, "0x" + cbor2.dumps(payload).hex()]]}


def test_signed_call_follows_its_receipt_id_and_assembles_chunks(monkeypatch):
    final = dict(RESULT, output={"ok": True, "operation": "run", "data": {"stdout": "x" * 5000}})
    encoded = cbor2.dumps(final)
    calls = []

    def execute(args, handler, payload, topic, deadline):
        assert args.wallet_key == "buyer.key"
        assert payload["hire_id"] == 42
        calls.append((handler, payload))
        if handler == "run":
            return {"ok": True, "status": "pending", "poll_handler": "get_result",
                    "request_id": 19, "hire_id": 42, "operation": "run"}
        assert payload["request_id"] == 19
        offset = payload["receipt_offset"]
        return {"request_id": 19, "hire_id": 42, "offset": offset,
                "total": len(encoded), "data": encoded[offset:offset + 3900]}

    monkeypatch.setattr(live, "execute", execute)
    assert live.invoke(ARGS, {"code": "print(42)"}) == final
    assert [handler for handler, _ in calls] == ["run", "get_result", "get_result"]
    assert [payload["receipt_offset"] for _, payload in calls[1:]] == [0, 3900]


@pytest.mark.parametrize("change", [
    {"request_id": 20}, {"hire_id": 99}, {"offset": 1}, {"data": b""},
    {"total": 38001}, {"request_id": True},
])
def test_result_reader_rejects_wrong_or_malformed_chunks(monkeypatch, change):
    encoded = cbor2.dumps(RESULT)
    chunk = dict(request_id=19, hire_id=42, offset=0, total=len(encoded), data=encoded)
    chunk.update(change)
    monkeypatch.setattr(live, "execute", lambda *args: chunk)
    with pytest.raises(ValueError):
        live.get_result(ARGS, 19, time.monotonic() + 10)


def test_correct_chunk_identity_cannot_hide_another_result(monkeypatch):
    encoded = cbor2.dumps(dict(RESULT, request_id=20))
    monkeypatch.setattr(live, "execute", lambda *args: {
        "request_id": 19, "hire_id": 42, "offset": 0, "total": len(encoded), "data": encoded})
    with pytest.raises(ValueError, match="different request"):
        live.get_result(ARGS, 19, time.monotonic() + 10)


def test_receipt_requires_success_one_actor_event_and_complete_cbor():
    valid = receipt({"request_id": 19})
    assert live.receipt_event(valid, ACTOR, "wrapper.result_chunk") == {"request_id": 19}
    for invalid in (
        dict(valid, status="Failed"),
        dict(valid, events=valid["events"] * 2),
        receipt({"request_id": 19}, actor="0x" + "bb" * 20),
    ):
        with pytest.raises(ValueError):
            live.receipt_event(invalid, ACTOR, "wrapper.result_chunk")
    with pytest.raises(ValueError, match="trailing bytes"):
        live.decode(cbor2.dumps(RESULT) + b"\x00")


def test_execute_fetches_labeled_transaction_receipt(monkeypatch):
    tx = "0x" + "12" * 32
    commands, urls = [], []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["COWBOY_RPC_URL"] == ARGS.rpc
        assert kwargs["check"] is True
        return SimpleNamespace(stdout=f"Actor: {ACTOR}\nTransaction hash: {tx}\n")

    def urlopen(url, **kwargs):
        urls.append(url)
        return io.BytesIO(json.dumps(receipt({"request_id": 19})).encode())

    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live.urllib.request, "urlopen", urlopen)
    assert live.execute(ARGS, "get_result", {}, "wrapper.result_chunk",
                        time.monotonic() + 10) == {"request_id": 19}
    assert urls == [ARGS.rpc + "/transaction/" + tx + "/receipt"]
    assert "--no-wait" in commands[0] and "--private-key" in commands[0]
    assert commands[0][commands[0].index("--payload") + 1] == "0xa0"


def test_duplicate_transaction_hash_is_not_guessed(monkeypatch):
    tx = "0x" + "12" * 32
    monkeypatch.setattr(live.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=f"Transaction hash: {tx}\nTransaction hash: {tx}\n"))
    with pytest.raises(ValueError, match="exactly one"):
        live.execute(ARGS, "run", {}, "wrapper.request_accepted", time.monotonic() + 10)


@pytest.mark.parametrize("timed_out", [False, True])
def test_failed_cli_does_not_print_wallet_arguments(monkeypatch, timed_out):
    secret = "synthetic-wallet-secret"

    def fail(command, **kwargs):
        command = command + [secret]
        if timed_out:
            raise live.subprocess.TimeoutExpired(command, 1)
        raise live.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(live.subprocess, "run", fail)
    try:
        live.execute(ARGS, "run", {}, "wrapper.request_accepted", time.monotonic() + 10)
    except RuntimeError:
        assert secret not in traceback.format_exc()
    else:
        pytest.fail("failed CLI command was accepted")


def test_comparison_requires_independent_success_and_expected_stdout():
    case = next(case for case in live.CASES if case[0] == "arithmetic")
    direct = SimpleNamespace(error=None, logs=SimpleNamespace(stdout=["42\n"], stderr=[]))
    live.compare(case, direct, RESULT)
    direct.logs.stdout = ["different\n"]
    with pytest.raises(ValueError, match="stdout"):
        live.compare(case, direct, RESULT)
    direct.error = SimpleNamespace(name="AuthenticationError", value="bad key")
    with pytest.raises(ValueError, match="expected outcome"):
        live.compare(case, direct, dict(RESULT, status="error", ok=False))


def test_program_error_check_rejects_generic_vendor_failure():
    case = next(case for case in live.CASES if case[0] == "error")
    direct = SimpleNamespace(error=SimpleNamespace(name="ValueError", value="boom"),
                             logs=SimpleNamespace(stdout=[], stderr=[]))
    failed = dict(RESULT, status="error", ok=False,
                  output={"ok": False, "error": "vendor error 42"})
    with pytest.raises(ValueError, match="program's ValueError"):
        live.compare(case, direct, failed)
