import json

import pytest

from cowboy_sdk import codec
from cowboy_sdk.errors import PermissionDeniedError
from cowboy_sdk.testing import SimulatedChain

from actors.ranchhand.main import KEY_OWNER, RanchhandActor

OWNER = b"\x11" * 20
HIRER = b"\x77" * 20
PROXY = "ab" * 20


def test_init_records_owner_and_config() -> None:
    actor = RanchhandActor()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, json.dumps({"model": "local"}).encode()) == b"ok: initialized"
    assert actor.storage[KEY_OWNER] == "0x" + OWNER.hex()
    assert actor.storage["model"] == "local"
    assert chain.events()[0][0] == "ranchhand.init"


def test_health_returns_ok_envelope() -> None:
    actor = RanchhandActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    resp = codec.decode(chain.call(actor.health, b""))
    assert resp["status"] == 200
    assert resp["body"] == b"ok"


def test_set_declaration_is_owner_only() -> None:
    actor = RanchhandActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    payload = codec.encode({"declaration_id": "0xabc", "version": 1, "artifact_id": 0})
    chain.set_sender(b"\x99" * 20)
    with pytest.raises(PermissionDeniedError):
        chain.call(actor.set_declaration, payload)
    chain.set_sender(OWNER)
    assert chain.call(actor.set_declaration, payload) == b"ok"


def test_connect_workspace_binds_team_once() -> None:
    actor = RanchhandActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, json.dumps({"mount_volumes": False}).encode())
    chain.set_sender(HIRER)
    payload = codec.encode({"team_id": "T123", "hire_id": 1, "proxy": PROXY})
    assert chain.call(actor.connect_workspace, payload) == b"ok: workspace connected"
    chain.set_sender(OWNER)  # get_tenants is owner-only
    tenants = codec.decode(chain.call(actor.get_tenants, codec.encode({})))
    assert tenants["T123"]["proxy"] == "0x" + PROXY
    assert tenants["T123"]["status"] == "active"
    # another account cannot rebind the same team_id
    chain.set_sender(b"\x88" * 20)
    with pytest.raises(ValueError, match="already bound"):
        chain.call(actor.connect_workspace, payload)
