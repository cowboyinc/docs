import json

import pytest

from cowboy_sdk import codec
from cowboy_sdk.errors import PermissionDeniedError
from cowboy_sdk.testing import SimulatedChain

from actors.relay.main import KEY_RANCHHAND, SlackRelayActor

OWNER = b"\x11" * 20
RANCHHAND = "cd" * 20


def _connected():
    actor = SlackRelayActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, json.dumps({"ranchhand": RANCHHAND}).encode())
    return actor, chain


def test_init_requires_ranchhand_address() -> None:
    actor = SlackRelayActor()
    chain = SimulatedChain(actor)
    with pytest.raises(ValueError, match="ranchhand actor address required"):
        chain.call(actor.init, b"{}")
    actor2, chain2 = _connected()
    assert actor2.storage[KEY_RANCHHAND] == RANCHHAND
    assert chain2.events()[0][0] == "slackrelay.init"


def test_handle_message_ignores_unknown_kinds() -> None:
    actor, chain = _connected()
    reply = chain.call(actor.handle_message, codec.encode({"kind": "nonsense"}))
    assert reply == b"ok: ignored"
    assert chain.events()[-1][0] == "slackrelay.unknown_message"


def test_get_posts_is_owner_only_and_starts_empty() -> None:
    actor, chain = _connected()
    chain.set_sender(b"\x99" * 20)
    with pytest.raises(PermissionDeniedError):
        chain.call(actor.get_posts, codec.encode({}))
    chain.set_sender(OWNER)
    assert codec.decode(chain.call(actor.get_posts, codec.encode({}))) == []
