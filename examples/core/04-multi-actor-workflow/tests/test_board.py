from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.board.main import JobBoard

VALIDATOR = "0x" + "aa" * 20
LEDGER = "0x" + "bb" * 20


def _configured_board():
    actor = JobBoard()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.configure, codec.encode({"validator_address": VALIDATOR, "settlement_address": LEDGER}))
    return actor, chain


def test_init_stores_addresses_from_payload() -> None:
    actor = JobBoard()
    chain = SimulatedChain(actor)
    reply = codec.decode(chain.call(actor.init, b'{"validator_address":"%s","settlement_address":"%s"}' % (VALIDATOR.encode(), LEDGER.encode())))
    assert reply == {"status": "initialized", "validator_address": VALIDATOR, "settlement_address": LEDGER}


def test_create_job_assigns_incrementing_ids() -> None:
    actor, chain = _configured_board()
    first = codec.decode(chain.call(actor.create_job, codec.encode({"title": "Summarize validator log", "required_keyword": "Ship", "reward_units": 2})))
    second = codec.decode(chain.call(actor.create_job, codec.encode({"title": "Second"})))
    assert first == {"status": "job_created", "job_id": 1, "title": "Summarize validator log"}
    assert second["job_id"] == 2
    job = codec.decode(chain.call(actor.get_job, codec.encode({"job_id": 1})))
    assert job["required_keyword"] == "ship"
    assert job["status"] == "OPEN"


def test_submit_work_forwards_submission_to_validator() -> None:
    actor, chain = _configured_board()
    chain.call(actor.create_job, codec.encode({"title": "Summarize validator log", "required_keyword": "ship", "reward_units": 2}))
    reply = codec.decode(chain.call(actor.submit_work, codec.encode({"job_id": 1, "work_text": "Ready to ship after validation."})))
    assert reply == {"status": "submission_queued", "submission_id": 1, "job_id": 1}
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == bytes.fromhex(VALIDATOR[2:])
    forwarded = codec.decode(sent[0]["payload"])
    assert forwarded["submission_id"] == 1
    assert forwarded["job"]["job_id"] == 1
    assert forwarded["submission"]["status"] == "PENDING_VALIDATION"
    assert forwarded["settlement_address"] == LEDGER


def test_record_settlement_updates_submission_status() -> None:
    actor, chain = _configured_board()
    chain.call(actor.create_job, codec.encode({"title": "Job", "required_keyword": "ship"}))
    chain.call(actor.submit_work, codec.encode({"job_id": 1, "work_text": "ship it"}))
    reply = codec.decode(chain.call(actor.record_settlement, codec.encode({"submission_id": 1, "result": "SETTLED_APPROVED"})))
    assert reply == {"status": "submission_updated", "submission_id": 1, "result": "SETTLED_APPROVED"}
    submission = codec.decode(chain.call(actor.get_submission, codec.encode({"submission_id": 1})))
    assert submission["status"] == "SETTLED_APPROVED"
    missing = codec.decode(chain.call(actor.record_settlement, codec.encode({"submission_id": 9, "result": "X"})))
    assert missing == {"error": "SUBMISSION_NOT_FOUND"}
