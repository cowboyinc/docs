from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.ring.main import RingNode

NEXT = "ab" * 20


def test_init_and_configure() -> None:
    actor = RingNode()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b"") == b"ok"
    assert chain.call(actor.configure, codec.encode({"next_actor": "0x" + NEXT})) == b"ok"
    name, data = chain.events()[-1]
    assert name == "configured"
    assert codec.decode(data) == {"next": "0x" + NEXT}
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state == {"next_actor": "0x" + NEXT, "hop_count": 0}


def test_start_ring_requires_configuration_then_sends() -> None:
    actor = RingNode()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.start_ring, codec.encode({"msg": "yo"})) == b"not configured"
    chain.call(actor.configure, codec.encode({"next_actor": NEXT}))
    assert chain.call(actor.start_ring, codec.encode({"msg": "yo"})) == b"ok"
    sends = mock_host.get_send_log()
    assert len(sends) == 1
    assert codec.decode(sends[0]["payload"]) == {"msg": "yo", "hop": 0}


def test_handle_message_increments_hop_and_forwards() -> None:
    actor = RingNode()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.configure, codec.encode({"next_actor": NEXT}))
    assert chain.call(actor.handle_message, codec.encode({"msg": "yo", "hop": 3})) == b"ok"
    name, data = chain.events()[-1]
    assert name == "ring_hop"
    assert codec.decode(data) == {"hop": 4, "local_count": 1, "msg": "yo"}
    forwarded = codec.decode(mock_host.get_send_log()[-1]["payload"])
    assert forwarded == {"msg": "yo", "hop": 4}
    assert codec.decode(chain.call(actor.get_state, codec.encode({})))["hop_count"] == 1
