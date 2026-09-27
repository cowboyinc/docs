"""Offline checks for the independent live and signed-Canyon clients."""

from types import SimpleNamespace

import pytest

import check_alphavantage_canyon as canyon
import live_alphavantage as comparison


codec = comparison.codec
ACTOR = "0x" + "aa" * 20


def receipt_event(topic, payload, actor=ACTOR, status="Success"):
    return {
        "status": status,
        "events": [[actor, topic, "0x" + codec.encode(payload).hex()]],
    }


def test_expected_urls_come_from_the_independent_fixture():
    assert comparison.expected_url("daily", {"symbol": "IBM"}) == (
        "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
        "&symbol=IBM&outputsize=compact&apikey=${ALPHAVANTAGE_API_KEY}"
    )
    assert comparison.expected_url("quote", {"symbol": " ibm "}) == (
        "https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
        "&symbol=IBM&apikey=${ALPHAVANTAGE_API_KEY}"
    )


def test_selected_vendor_data_returns_the_whole_reply_no_extraction():
    # These handlers do not use the Runner's top-level selector, so every
    # handler delivers the whole vendor reply, bounded client-side;
    # selected_vendor_data returns it unchanged.
    vendor = {"Symbol": "IBM", "Name": "International Business Machines"}
    assert comparison.selected_vendor_data("daily", vendor) is vendor
    assert comparison.selected_vendor_data("overview", vendor) is vendor


def test_canyon_event_reader_filters_actor_and_requires_success():
    wanted = {"request_id": 7}
    receipt = {
        "status": "Success",
        "events": [
            ["0x" + "bb" * 20, "wrapper.request_accepted", "0x" + codec.encode({"request_id": 8}).hex()],
            [ACTOR, "other.topic", "0x" + codec.encode({"request_id": 9}).hex()],
            [ACTOR, "wrapper.request_accepted", "0x" + codec.encode(wanted).hex()],
        ],
    }
    assert canyon.decoded_events(receipt, ACTOR, "wrapper.request_accepted") == [wanted]
    with pytest.raises(AssertionError, match="did not execute"):
        canyon.decoded_events({"status": "Failed", "events": []}, ACTOR, "x")


def test_signed_result_reassembles_and_validates_exact_request(monkeypatch):
    args = SimpleNamespace(actor=ACTOR, hire_id=42)
    result = {
        "ok": True,
        "request_id": 7,
        "hire_id": 42,
        "operation": "quote",
        "status": "ok",
        "output": {
            "ok": True,
            "operation": "quote",
            "data": {"Global Quote": {"01. symbol": "IBM"}},
        },
        "expires_height": 200,
    }
    encoded = codec.encode(result)

    def execute(_args, handler, payload, wallet_key=None):
        assert handler == "get_result" and wallet_key is None
        assert payload == {"hire_id": 42, "request_id": 7, "receipt_offset": 0}
        return receipt_event(
            "wrapper.result_chunk",
            {
                "request_id": 7,
                "hire_id": 42,
                "offset": 0,
                "total": len(encoded),
                "data": encoded,
            },
        )

    monkeypatch.setattr(canyon, "execute", execute)
    assert canyon.signed_result(args, 7) == result


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": 8},
        {"hire_id": 43},
        {"offset": 1},
        {"total": 38_001},
        {"data": b""},
    ],
)
def test_signed_result_rejects_unbound_or_oversized_chunks(monkeypatch, change):
    args = SimpleNamespace(actor=ACTOR, hire_id=42)
    encoded = codec.encode({"request_id": 7})
    chunk = {
        "request_id": 7,
        "hire_id": 42,
        "offset": 0,
        "total": len(encoded),
        "data": encoded,
    }
    chunk.update(change)
    monkeypatch.setattr(
        canyon,
        "execute",
        lambda *_args, **_kwargs: receipt_event("wrapper.result_chunk", chunk),
    )
    with pytest.raises((AssertionError, KeyError, ValueError)):
        canyon.signed_result(args, 7)


def test_execute_scrubs_wallet_input_on_subprocess_failure(monkeypatch):
    # Browserbase #372 client scrub: a subprocess failure must not surface argv
    # (the --private-key value) or captured output into the error/traceback.
    import subprocess as _sp, traceback as _tb
    import pytest as _pt
    from types import SimpleNamespace as _NS
    import check_alphavantage_canyon as _c
    secret = "synthetic-wallet-secret-do-not-print"
    a = _NS(cowboy="cowboy", actor="0xactor", rpc="http://rpc.local", wallet_key=secret)
    def _boom(command, **kwargs):
        raise _sp.CalledProcessError(1, command, output=secret, stderr=secret)
    monkeypatch.setattr(_c.subprocess, "run", _boom)
    with _pt.raises(RuntimeError) as err:
        _c.execute(a, "get_result", {"request_id": 1})
    assert secret not in "".join(_tb.format_exception(err.value))
