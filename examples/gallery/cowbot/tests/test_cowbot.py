"""SimulatedChain smoke tests for the cowbot target (offline, no Slack).

slack_http is a @runner.continuation; its runner awaits are not executed
here — these tests cover init, the read handlers, and the health route.
"""

from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.cowbot.main import CowbotActor


def test_init_stores_volume_config_and_emits_event():
    actor = CowbotActor()
    chain = SimulatedChain(actor)
    out = chain.call(actor.init, b'{"knowledge_volume":"kb","state_volume":"st"}')
    assert out == b"ok: initialized"
    name, data = chain.events()[0]
    assert name == "cowbot.init"
    event = codec.decode(data)
    assert event["knowledge_volume"] == "kb" and event["state_volume"] == "st"


def test_health_route_returns_plain_ok_envelope():
    actor = CowbotActor()
    chain = SimulatedChain(actor)
    envelope = codec.decode(chain.call(actor.health, b""))
    assert envelope["status"] == 200
    assert envelope["body"] == b"ok"
    assert ["content-type", "text/plain; charset=utf-8"] in [
        list(h) for h in envelope["headers"]
    ]


def test_owner_reads_start_empty():
    actor = CowbotActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    assert codec.decode(chain.call(actor.get_pending, codec.encode({}))) == {}
    assert codec.decode(chain.call(actor.get_requests, codec.encode({}))) == []
