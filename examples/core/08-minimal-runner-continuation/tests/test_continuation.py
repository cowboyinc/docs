from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.continuation.main import DEFAULT_SOURCE_URL, RunnerContinuation


def test_init_stores_source_url_once() -> None:
    actor = RunnerContinuation()
    chain = SimulatedChain(actor)
    first = codec.decode(chain.call(actor.init, b'{"source_url":"https://example.com/a"}'))
    assert first == {"status": "initialized", "source_url": "https://example.com/a"}
    again = codec.decode(chain.call(actor.init, b"{}"))
    assert again["status"] == "already_initialized"
    assert again["source_url"] == "https://example.com/a"


def test_refresh_records_pending_request_and_dispatches_runner_job() -> None:
    actor = RunnerContinuation()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.refresh, codec.encode({}))

    status = codec.decode(chain.call(actor.get_status, codec.encode({})))
    assert status["status"] == "pending"
    assert status["last_request_id"] == 1
    assert status["last_result"] == {"request_id": 1, "status": "pending"}

    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["kind"] == "runner_job"
    assert job["job_type"] == "http"
    assert job["payload"]["args"] == [DEFAULT_SOURCE_URL]
    assert job["payload"]["kwargs"] == {"method": "GET", "timeout_ms": 5000}
    assert job["reply_handler"] == "_run_refresh_job__resume"


def test_finalize_refresh_stores_done_record() -> None:
    actor = RunnerContinuation()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.refresh, codec.encode({}))

    record = codec.decode(actor._finalize_refresh(1, RunnerResult({"status": 200, "body": "ok"})))
    assert record == {"request_id": 1, "status": "done", "response": {"status": 200, "body": "ok"}}
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("runner_continuation.result", record)
    status = codec.decode(chain.call(actor.get_status, codec.encode({})))
    assert status["status"] == "done"
    assert status["last_result"] == record
