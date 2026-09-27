"""Offline checks for the independent and Canyon clients."""

from types import SimpleNamespace

import pytest

import test_hyperbrowser_canyon as canyon
import test_hyperbrowser_comparison as comparison


codec = comparison.codec
ACTOR = "0x" + "aa" * 20


def receipt_event(topic, payload, actor=ACTOR, status="Success"):
    return {
        "status": status,
        "events": [[actor, topic, "0x" + codec.encode(payload).hex()]],
    }


def test_expected_scrape_body_forces_main_content_and_default_formats():
    assert comparison.expected_scrape_body("https://example.com/a") == {
        "url": "https://example.com/a",
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }


def test_expected_scrape_body_strips_whitespace_and_honors_formats():
    body = comparison.expected_scrape_body("  https://example.com/a  ", ["links"])
    assert body["url"] == "https://example.com/a"
    assert body["scrapeOptions"]["formats"] == ["links"]


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
        "operation": "fetch",
        "status": "ok",
        "output": {
            "ok": True,
            "operation": "fetch",
            "data": {"job_status": "completed", "links": ["https://example.com/a"]},
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
    assert canyon.signed_result(args, 7, "fetch") == result


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
            "operation": "fetch",
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
        canyon.signed_result(args, 7, "fetch")


def test_validate_scrape_and_fetch_data():
    canyon.validate_scrape_data({"job_status": "submitted", "job_id": "job-abc123"})
    canyon.validate_fetch_data({"job_status": "completed", "links": ["https://example.com/a"]})
    canyon.validate_fetch_data(
        {"job_status": "completed", "_cowboy": {"markdown_omitted": True}}
    )
    for bad in ({}, {"job_status": "submitted"}, {"job_status": "submitted", "job_id": "bad id!"}):
        with pytest.raises(AssertionError):
            canyon.validate_scrape_data(bad)
    for bad in ({}, {"job_status": "running"}, {"job_status": "completed"}):
        with pytest.raises(AssertionError):
            canyon.validate_fetch_data(bad)


def test_canyon_retries_running_fetch_with_a_finite_limit(monkeypatch):
    args = SimpleNamespace(hire_id=42)
    request_ids = iter((7, 7, 7))
    statuses = iter(("running", "pending", "completed"))
    monkeypatch.setattr(canyon, "submit", lambda *_args, **_kwargs: next(request_ids))
    monkeypatch.setattr(canyon, "await_terminal", lambda *_args: {"status": "ok"})
    monkeypatch.setattr(
        canyon,
        "signed_result",
        lambda *_args: {
            "output": {"data": {"job_status": next(statuses), "links": ["https://example.com/a"]}}
        },
    )
    monkeypatch.setattr(canyon.time, "sleep", lambda _seconds: None)
    request_id, result = canyon.fetch_until_complete(args, 1, attempts=3)
    assert request_id == 7
    assert result["output"]["data"]["job_status"] == "completed"


def test_execute_scrubs_wallet_input_on_subprocess_failure(monkeypatch):
    # Browserbase #372 client scrub: a subprocess failure must not surface argv
    # (the --private-key value) or captured output into the error/traceback.
    import subprocess as _sp, traceback as _tb
    import pytest as _pt
    from types import SimpleNamespace as _NS
    import test_hyperbrowser_canyon as _c
    secret = "synthetic-wallet-secret-do-not-print"
    a = _NS(cowboy="cowboy", actor="0xactor", rpc="http://rpc.local", wallet_key=secret)
    def _boom(command, **kwargs):
        raise _sp.CalledProcessError(1, command, output=secret, stderr=secret)
    monkeypatch.setattr(_c.subprocess, "run", _boom)
    with _pt.raises(RuntimeError) as err:
        _c.execute(a, "get_result", {"request_id": 1})
    assert secret not in "".join(_tb.format_exception(err.value))
