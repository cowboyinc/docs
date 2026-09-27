from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import CallMock, SimulatedChain

from actors.seller.main import SellerActor

ESCROW = "0x" + "ee" * 20
SELF = "0x" + "22" * 20  # the mock host's actor_addr


def _configured_seller():
    actor = SellerActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert codec.decode(chain.call(actor.configure, codec.encode({"escrow_address": ESCROW}))) == {"status": "configured", "escrow_address": ESCROW}
    return actor, chain


def test_lock_calls_escrow_with_own_address_as_seller() -> None:
    actor, chain = _configured_seller()
    escrow = CallMock(default=codec.encode({"status": "locked", "trade_id": "trade-1", "state": "LOCKED"}))
    mock_host.set_call_handler(escrow)
    reply = codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1"})))
    assert reply == {"status": "lock_requested", "trade_id": "trade-1", "escrow_result": {"status": "locked", "trade_id": "trade-1", "state": "LOCKED"}}
    target, method, args, _, _ = escrow.calls[-1]
    assert (target, method) == (bytes.fromhex(ESCROW[2:]), "lock")
    assert codec.decode(args) == {"trade_id": "trade-1", "seller": SELF}


def test_confirm_delivery_requests_release() -> None:
    actor, chain = _configured_seller()
    escrow = CallMock(default=codec.encode({"status": "released", "trade_id": "trade-1", "state": "RELEASED"}))
    mock_host.set_call_handler(escrow)
    reply = codec.decode(chain.call(actor.confirm_delivery, codec.encode({"trade_id": "trade-1"})))
    assert reply["status"] == "release_requested"
    assert reply["escrow_result"]["state"] == "RELEASED"
    assert escrow.calls[-1][1] == "release"
    assert codec.decode(escrow.calls[-1][2]) == {"trade_id": "trade-1", "seller": SELF}


def test_unconfigured_and_non_owner_paths() -> None:
    actor = SellerActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert codec.decode(chain.call(actor.configure, codec.encode({}))) == {"error": "INVALID_CONFIGURATION"}
    chain.call(actor.configure, codec.encode({"escrow_address": ESCROW}))
    chain.set_sender("0x" + "cc" * 20)
    assert codec.decode(chain.call(actor.lock, codec.encode({"trade_id": "trade-1"}))) == {"error": "OWNER_ONLY"}
