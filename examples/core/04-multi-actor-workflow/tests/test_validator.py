from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.validator.main import JobValidator

LEDGER = "0x" + "bb" * 20
WORKER = "cc" * 20


def _envelope(work_text: str) -> bytes:
    return codec.encode(
        {
            "submission_id": 1,
            "job": {"job_id": 1, "required_keyword": "ship", "reward_units": 2},
            "submission": {"submission_id": 1, "job_id": 1, "worker": WORKER, "work_text": work_text},
            "settlement_address": LEDGER,
        }
    )


def test_init_returns_initialized() -> None:
    actor = JobValidator()
    chain = SimulatedChain(actor)
    assert codec.decode(chain.call(actor.init, b"")) == {"status": "initialized"}


def test_matching_keyword_approves_and_forwards_verdict() -> None:
    actor = JobValidator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(chain.call(actor.validate_submission, _envelope("Ready to ship after validation.")))
    assert reply["status"] == "validated"
    assert reply["approved"] is True
    assert reply["reward_units"] == 2
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == bytes.fromhex(LEDGER[2:])
    verdict = codec.decode(sent[0]["payload"])
    assert verdict["submission_id"] == 1
    assert verdict["approved"] is True
    assert verdict["worker"] == WORKER


def test_missing_keyword_rejects_and_stores_verdict() -> None:
    actor = JobValidator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(chain.call(actor.validate_submission, _envelope("Not done yet.")))
    assert reply["approved"] is False
    assert reply["reason"] == "required keyword missing"
    assert reply["reward_units"] == 0
    stored = codec.decode(chain.call(actor.get_verdict, codec.encode({"submission_id": 1})))
    assert stored["approved"] is False
    assert codec.decode(chain.call(actor.get_verdict, codec.encode({"submission_id": 2}))) == {"error": "VERDICT_NOT_FOUND"}
