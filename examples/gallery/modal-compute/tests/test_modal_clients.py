"""Offline checks for the independent and Canyon clients."""

from types import SimpleNamespace

import pytest

import test_modal_canyon as canyon
import test_modal_comparison as comparison


codec = comparison.codec
modal = comparison.modal_compute
ACTOR = "0x" + "aa" * 20


def receipt_event(topic, payload, actor=ACTOR, status="Success"):
    return {
        "status": status,
        "events": [[actor, topic, "0x" + codec.encode(payload).hex()]],
    }


def test_expected_body_is_independent_and_exact():
    body = comparison.expected_body(comparison.DEFAULT_PAYLOAD)
    assert body == {"n": 21}
    # A copy, never the caller's object, so the wrapper can't be mutated through it.
    assert body is not comparison.DEFAULT_PAYLOAD


def test_expected_body_defaults_empty():
    assert comparison.expected_body(None) == {}
    assert comparison.expected_body({}) == {}


def test_decoded_events_requires_success_and_exact_actor():
    wanted = {"request_id": 7}
    receipt = receipt_event("wrapper.request_accepted", wanted)
    receipt["events"].insert(
        0,
        [
            "0x" + "bb" * 20,
            "wrapper.request_accepted",
            "0x" + codec.encode({"request_id": 99}).hex(),
        ],
    )
    assert canyon.decoded_events(receipt, ACTOR, "wrapper.request_accepted") == [wanted]
    with pytest.raises(AssertionError):
        canyon.decoded_events(
            dict(receipt, status="Failed"), ACTOR, "wrapper.request_accepted"
        )


def test_signed_result_reassembles_actor_filtered_chunks(monkeypatch):
    result = {
        "ok": True,
        "request_id": 7,
        "hire_id": 42,
        "operation": "invoke",
        "status": "ok",
        "output": {
            "ok": True,
            "operation": "invoke",
            "data": {"answer": 42, "echo": {"n": 21}},
        },
        "expires_height": 200,
    }
    encoded = codec.encode(result)
    args = SimpleNamespace(actor=ACTOR, hire_id=42)

    def execute(_args, _handler, payload, wallet_key=None):
        offset = payload["receipt_offset"]
        data = encoded[offset : offset + 17]
        chunk = {
            "request_id": 7,
            "hire_id": 42,
            "offset": offset,
            "total": len(encoded),
            "data": data,
        }
        receipt = receipt_event("wrapper.result_chunk", chunk)
        receipt["events"].insert(
            0,
            [
                "0x" + "bb" * 20,
                "wrapper.result_chunk",
                "0x" + codec.encode(dict(chunk, request_id=99)).hex(),
            ],
        )
        return receipt

    monkeypatch.setattr(canyon, "execute", execute)
    assert canyon.signed_result(args, 7) == result


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": 8},
        {"hire_id": 43},
        {"offset": 1},
        {"total": 99_999},
        {"data": "not-bytes"},
    ],
)
def test_signed_result_rejects_malformed_chunks(monkeypatch, change):
    encoded = codec.encode(
        {
            "ok": True,
            "request_id": 7,
            "hire_id": 42,
            "operation": "invoke",
            "status": "pending",
        }
    )
    args = SimpleNamespace(actor=ACTOR, hire_id=42)

    def execute(_args, _handler, payload, wallet_key=None):
        chunk = {
            "request_id": 7,
            "hire_id": 42,
            "offset": payload["receipt_offset"],
            "total": len(encoded),
            "data": encoded,
        }
        chunk.update(change)
        return receipt_event("wrapper.result_chunk", chunk)

    monkeypatch.setattr(canyon, "execute", execute)
    with pytest.raises((AssertionError, ValueError, TypeError)):
        canyon.signed_result(args, 7)


def test_validate_vendor_data_enforces_bounds():
    canyon.validate_vendor_data({"answer": 42, "echo": {"n": 21}})
    for bad in ({}, "", [1, 2], {"x": "a" * (modal.RETURN_BYTE_BUDGET + 1)}):
        with pytest.raises(AssertionError):
            canyon.validate_vendor_data(bad)


def test_execute_scrubs_wallet_input_on_subprocess_failure(monkeypatch):
    # a subprocess failure must not leak argv/output (the wallet key) into the traceback
    import subprocess as _sp, traceback as _tb
    import pytest as _pt
    from types import SimpleNamespace as _NS
    import test_modal_canyon as _c
    secret = "synthetic-wallet-secret-do-not-print"
    a = _NS(cowboy="cowboy", actor="0xactor", rpc="http://rpc.local", wallet_key=secret)
    def _boom(command, **kwargs):
        raise _sp.CalledProcessError(1, command, output=secret, stderr=secret)
    monkeypatch.setattr(_c.subprocess, "run", _boom)
    with _pt.raises(RuntimeError) as err:
        _c.execute(a, "get_result", {"request_id": 1})
    assert secret not in "".join(_tb.format_exception(err.value))
