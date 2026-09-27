import json

import pytest

from cowboy_sdk import codec
from cowboy_sdk.errors import PermissionDeniedError
from cowboy_sdk.testing import SimulatedChain

from actors.twilio.main import KEY_DECLARATION, KEY_OWNER, TwilioActor

OWNER = b"\x11" * 20


def test_init_records_owner_and_config() -> None:
    actor = TwilioActor()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, json.dumps({"model": "local"}).encode()) == b"ok: initialized"
    assert actor.storage[KEY_OWNER] == "0x" + OWNER.hex()
    assert actor.storage["model"] == "local"
    assert chain.events()[0][0] == "twilio.init"


def test_health_returns_ok_envelope() -> None:
    actor = TwilioActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    resp = codec.decode(chain.call(actor.health, b""))
    assert resp["status"] == 200
    assert resp["body"] == b"ok"


def test_set_declaration_is_owner_only() -> None:
    actor = TwilioActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    payload = codec.encode({"declaration_id": "0xabc", "version": 1, "artifact_id": 0})
    chain.set_sender(b"\x99" * 20)
    with pytest.raises(PermissionDeniedError):
        chain.call(actor.set_declaration, payload)
    chain.set_sender(OWNER)
    assert chain.call(actor.set_declaration, payload) == b"ok"
    assert actor.storage[KEY_DECLARATION]["declaration_id"] == "0xabc"
