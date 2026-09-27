import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import DEFAULT_SENDER, SimulatedChain

from actors.escrow.main import EscrowActor

TOKEN_ID = "0x" + "77" * 32
BUYER = "0x" + "aa" * 20
SELLER = "0x" + "bb" * 20
ESCROW = b"\x22" * 20  # the mock host's actor_addr


@pytest.fixture
def token_log(monkeypatch):
    """The mock host has no token_* bindings; record custody moves instead."""
    log = []
    monkeypatch.setattr(mock_host, "token_transfer_from", lambda t, f, to, a: log.append(("transfer_from", f, to, a)), raising=False)
    monkeypatch.setattr(mock_host, "token_transfer", lambda t, to, a: log.append(("transfer", to, a)), raising=False)
    return log


def _deposited_escrow():
    actor = EscrowActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert codec.decode(chain.call(actor.configure, codec.encode({"token_id": TOKEN_ID}))) == {"status": "configured", "token_id": TOKEN_ID}
    reply = codec.decode(chain.call(actor.deposit, codec.encode({"trade_id": "trade-1", "buyer": BUYER, "seller": SELLER, "amount": 100})))
    assert reply == {"status": "deposited", "trade_id": "trade-1", "state": "DEPOSITED"}
    return actor, chain


def test_deposit_pulls_buyer_funds_into_custody(token_log) -> None:
    actor, chain = _deposited_escrow()
    assert token_log == [("transfer_from", bytes.fromhex(BUYER[2:]), ESCROW, 100)]
    trade = codec.decode(chain.call(actor.get_trade, codec.encode({"trade_id": "trade-1"})))
    assert trade["state"] == "DEPOSITED"
    assert trade["buyer"] == BUYER and trade["seller"] == SELLER
    name, data = chain.events()[-1]
    assert name == "get_trade" and codec.decode(data) == trade
    assert codec.decode(chain.call(actor.deposit, codec.encode({"trade_id": "trade-1", "buyer": BUYER, "seller": SELLER, "amount": 100}))) == {"error": "TRADE_ALREADY_EXISTS"}


def test_seller_lock_then_release_pays_seller(token_log) -> None:
    actor, chain = _deposited_escrow()
    chain.set_sender(SELLER)
    assert codec.decode(chain.call(actor.release, codec.encode({"trade_id": "trade-1", "seller": SELLER}))) == {"error": "INVALID_RELEASE"}
    assert codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1", "seller": SELLER}))) == {"status": "locked", "trade_id": "trade-1", "state": "LOCKED"}
    assert codec.decode(chain.call(actor.release, codec.encode({"trade_id": "trade-1", "seller": SELLER}))) == {"status": "released", "trade_id": "trade-1", "state": "RELEASED"}
    assert token_log[-1] == ("transfer", bytes.fromhex(SELLER[2:]), 100)
    assert codec.decode(chain.call(actor.get_trade, codec.encode({"trade_id": "trade-1"})))["state"] == "RELEASED"


def test_stranger_cannot_lock(token_log) -> None:
    actor, chain = _deposited_escrow()
    chain.set_sender("0x" + "cc" * 20)
    assert codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1", "seller": SELLER}))) == {"error": "UNAUTHORIZED"}
    chain.set_sender(DEFAULT_SENDER)
    assert codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1", "seller": BUYER}))) == {"error": "UNAUTHORIZED"}


def test_owner_cancel_refunds_buyer_before_lock(token_log) -> None:
    actor, chain = _deposited_escrow()
    assert codec.decode(chain.call(actor.cancel, codec.encode({"trade_id": "trade-1"}))) == {"status": "cancelled", "trade_id": "trade-1", "state": "CANCELLED"}
    assert token_log[-1] == ("transfer", bytes.fromhex(BUYER[2:]), 100)
    assert codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1", "seller": SELLER}))) == {"error": "INVALID_LOCK"}
    assert codec.decode(chain.call(actor.cancel, codec.encode({"trade_id": "missing"}))) == {"error": "TRADE_NOT_FOUND"}
