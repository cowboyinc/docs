from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.ledger.main import SettlementLedger

BOARD = "0x" + "dd" * 20
WORKER = "cc" * 20


def _verdict(submission_id: int, approved: bool) -> bytes:
    return codec.encode(
        {
            "submission_id": submission_id,
            "job_id": 1,
            "approved": approved,
            "reason": "keyword matched" if approved else "required keyword missing",
            "worker": WORKER,
            "reward_units": 2 if approved else 0,
        }
    )


def _configured_ledger():
    actor = SettlementLedger()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"board_address":"%s"}' % BOARD.encode())
    return actor, chain


def test_init_stores_board_address() -> None:
    actor, chain = _configured_ledger()
    assert codec.decode(chain.state("board_address")) == BOARD


def test_approved_verdict_settles_and_notifies_board() -> None:
    actor, chain = _configured_ledger()
    reply = codec.decode(chain.call(actor.settle_submission, _verdict(1, True)))
    assert reply["status"] == "settled"
    assert reply["state"] == "SETTLED_APPROVED"
    assert reply["reward_units"] == 2
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == bytes.fromhex(BOARD[2:])
    assert codec.decode(sent[0]["payload"]) == {"submission_id": 1, "result": "SETTLED_APPROVED"}
    settlement = codec.decode(chain.call(actor.get_settlement, codec.encode({"submission_id": 1})))
    assert settlement["state"] == "SETTLED_APPROVED"
    assert settlement["worker"] == WORKER


def test_rejected_verdict_is_recorded_in_history() -> None:
    actor, chain = _configured_ledger()
    chain.call(actor.settle_submission, _verdict(1, True))
    chain.call(actor.settle_submission, _verdict(2, False))
    history = codec.decode(chain.call(actor.get_history, codec.encode({"limit": 10})))["history"]
    assert [entry["state"] for entry in history] == ["SETTLED_APPROVED", "SETTLED_REJECTED"]
    assert codec.decode(chain.call(actor.get_settlement, codec.encode({"submission_id": 3}))) == {"error": "SETTLEMENT_NOT_FOUND"}
