import json

from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.relay.main import TwilioRelayActor

TWILIO = bytes.fromhex("cd" * 20)


def _connected():
    actor = TwilioRelayActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, json.dumps({"twilio": TWILIO.hex(), "account_sid": "ACx"}).encode())
    return actor, chain


def test_init_binds_the_twilio_actor() -> None:
    actor, chain = _connected()
    assert chain.events()[0][0] == "twiliorelay.init"


def test_relay_event_rejects_unknown_sender() -> None:
    actor, chain = _connected()
    chain.set_sender(b"\x99" * 20)
    reply = codec.decode(chain.call(actor.relay_event, codec.encode({"request_id": 7})))
    assert reply == {"r": "err: unauthorized"}
    assert mock_host.get_timers() == {}


def test_relay_event_from_twilio_schedules_funded_verify_timer() -> None:
    actor, chain = _connected()
    chain.set_sender(TWILIO)
    reply = codec.decode(chain.call(actor.relay_event, codec.encode({
        "request_id": 7,
        "twilio_number": "+15550001111",
        "preimage": b"pre",
        "sig": "sig",
    })))
    assert reply == {"r": "ok: relayed"}
    timers = list(mock_host.get_timers().values())
    assert len(timers) == 1
    assert codec.decode(timers[0]["payload"]) == {"kind": "run_verify", "request_id": 7}
