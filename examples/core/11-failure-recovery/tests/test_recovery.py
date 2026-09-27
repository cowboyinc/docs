from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.recovery.main import RecoveryDemo


class _HttpResult:
    def __init__(self, raw, text=""):
        self.raw = raw
        self.text = text


def test_submit_request_records_pending_and_dispatches_http_job() -> None:
    actor = RecoveryDemo()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"source_url":"https://example.com/health"}')
    chain.call(actor.submit_request, codec.encode({"job": "demo"}))
    request = codec.decode(chain.call(actor.get_request, codec.encode({"request_id": 1})))
    assert request["status"] == "pending"
    assert request["attempt"] == 1
    assert request["input"] == {"job": "demo"}
    job = codec.decode(mock_host.get_send_log()[-1]["payload"])
    assert job["job_type"] == "http"
    assert job["payload"]["args"] == ["https://example.com/health"]
    status = codec.decode(chain.call(actor.get_status, codec.encode({})))
    assert status == {"request_count": 1, "next_request_id": 2}


def test_finalize_request_marks_done_or_failed() -> None:
    actor = RecoveryDemo()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.submit_request, codec.encode({}))
    chain.call(actor.submit_request, codec.encode({}))
    done = codec.decode(actor._finalize_request({"request_id": 1}, _HttpResult({"ok": True})))
    assert done["status"] == "done"
    assert done["result"] == {"ok": True}
    failed = codec.decode(actor._finalize_request({"request_id": 2}, _HttpResult("not json", text="not json")))
    assert failed["status"] == "failed"
    assert failed["result"]["error"] == "NON_OBJECT_RESULT"
    assert [name for name, _ in chain.events()] == ["recovery_demo.done", "recovery_demo.failed"]


def test_retry_request_links_a_new_attempt() -> None:
    actor = RecoveryDemo()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.submit_request, codec.encode({"job": "demo"}))
    actor._finalize_request({"request_id": 1}, _HttpResult("boom", text="boom"))
    chain.call(actor.retry_request, codec.encode({"request_id": 1}))
    retry = codec.decode(chain.call(actor.get_request, codec.encode({"request_id": 2})))
    assert retry["status"] == "pending"
    assert retry["attempt"] == 2
    assert retry["previous_request_id"] == 1
    assert retry["recovered"] is True
    assert retry["input"] == {"job": "demo"}
    recovered = codec.decode(actor._finalize_request({"request_id": 2}, _HttpResult({"ok": True})))
    assert recovered["status"] == "recovered"


def test_unknown_request_is_reported() -> None:
    actor = RecoveryDemo()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    missing = codec.decode(chain.call(actor.get_request, codec.encode({"request_id": 9})))
    assert missing == {"error": "REQUEST_NOT_FOUND"}
    assert codec.decode(chain.call(actor.mark_recovered, codec.encode({"request_id": 9}))) == {"error": "REQUEST_NOT_FOUND"}
