"""Offline checks for the actual Canyon acceptance/result client."""

from types import SimpleNamespace

import pytest


def load_client():
    import live_agentmail_canyon as canyon
    return canyon


def args():
    return SimpleNamespace(
        rpc="http://rpc.invalid", actor="0xactor", hire_id=42,
        cowboy="cowboy", wallet_key="wallet",
    )


def receipt(canyon, operation="send", request_id=17):
    accepted = {"ok": True, "operation": operation, "request_id": request_id,
                "hire_id": 42, "status": "pending"}
    return "Status: Success\n  - wrapper.request_accepted: 0x%s\n" % canyon.codec.encode(accepted).hex()


def test_canyon_uses_emitted_request_id_and_matching_result(monkeypatch):
    canyon = load_client()
    monkeypatch.setattr(canyon.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=receipt(canyon), stderr=""))
    seen = []
    def record(_args, request_id):
        seen.append(request_id)
        return {"request_id": request_id, "status": "ok",
                "output": {"ok": True, "operation": "send", "data": {"message_id": "m1"}}}
    monkeypatch.setattr(canyon, "request_record", record)
    # An unrelated call_count cannot affect the event-derived request id.
    monkeypatch.setattr(canyon, "state", lambda _args: {"storage": {"call_count": 999}})
    assert canyon.canyon(args(), "send", {})["data"]["message_id"] == "m1"
    assert seen == [17]


@pytest.mark.parametrize("stdout", ["Status: Success\n", None])
def test_canyon_rejects_missing_or_mismatched_acceptance(monkeypatch, stdout):
    canyon = load_client()
    text = receipt(canyon, operation="reply") if stdout is None else stdout
    monkeypatch.setattr(canyon.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=text, stderr=""))
    with pytest.raises(AssertionError):
        canyon.canyon(args(), "send", {})
