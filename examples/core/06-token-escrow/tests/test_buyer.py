import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import CallMock, SimulatedChain

from actors.buyer.main import BuyerActor

TOKEN_ID = "0x" + "77" * 32
ESCROW = "0x" + "ee" * 20
SELLER = "0x" + "bb" * 20


def _configured_buyer():
    actor = BuyerActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(chain.call(actor.configure, codec.encode({"escrow_address": ESCROW, "token_id": TOKEN_ID})))
    assert reply == {"status": "configured", "escrow_address": ESCROW, "token_id": TOKEN_ID}
    return actor, chain


def test_approve_escrow_sets_allowance(monkeypatch) -> None:
    approvals = []
    monkeypatch.setattr(mock_host, "token_approve", lambda t, s, a: approvals.append((t, s, a)), raising=False)
    actor, chain = _configured_buyer()
    reply = codec.decode(chain.call(actor.approve_escrow, codec.encode({"amount": 200})))
    assert reply == {"status": "approved", "amount": 200, "escrow_address": ESCROW}
    assert approvals == [(bytes.fromhex(TOKEN_ID[2:]), bytes.fromhex(ESCROW[2:]), 200)]
    assert codec.decode(chain.call(actor.approve_escrow, codec.encode({"amount": 0}))) == {"error": "INVALID_AMOUNT"}


def test_deposit_calls_escrow_and_returns_its_reply() -> None:
    actor, chain = _configured_buyer()
    escrow = CallMock()
    escrow.when(target=bytes.fromhex(ESCROW[2:]), method="deposit").respond(codec.encode({"status": "deposited", "trade_id": "trade-1", "state": "DEPOSITED"}))
    mock_host.set_call_handler(escrow)
    reply = codec.decode(chain.call(actor.deposit, codec.encode({"trade_id": "trade-1", "seller": SELLER, "amount": 100})))
    assert reply["status"] == "deposit_requested"
    assert reply["escrow_result"] == {"status": "deposited", "trade_id": "trade-1", "state": "DEPOSITED"}
    target, method, args, _, _ = escrow.calls[-1]
    assert (target, method) == (bytes.fromhex(ESCROW[2:]), "deposit")
    assert codec.decode(args) == {"trade_id": "trade-1", "seller": SELLER, "amount": 100}


def test_cancel_calls_escrow_cancel() -> None:
    actor, chain = _configured_buyer()
    escrow = CallMock(default=codec.encode({"status": "cancelled", "trade_id": "trade-2", "state": "CANCELLED"}))
    mock_host.set_call_handler(escrow)
    reply = codec.decode(chain.call(actor.cancel, codec.encode({"trade_id": "trade-2"})))
    assert reply == {"status": "cancel_requested", "trade_id": "trade-2", "escrow_result": {"status": "cancelled", "trade_id": "trade-2", "state": "CANCELLED"}}
    assert escrow.calls[-1][1] == "cancel"
    assert codec.decode(escrow.calls[-1][2]) == {"trade_id": "trade-2"}


def test_non_owner_is_rejected() -> None:
    actor, chain = _configured_buyer()
    chain.set_sender("0x" + "cc" * 20)
    assert codec.decode(chain.call(actor.deposit, codec.encode({"trade_id": "trade-1", "seller": SELLER, "amount": 100}))) == {"error": "OWNER_ONLY"}
    assert codec.decode(chain.call(actor.configure, codec.encode({"escrow_address": ESCROW, "token_id": TOKEN_ID}))) == {"error": "OWNER_ONLY"}
