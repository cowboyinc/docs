import json

from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.hello.main import HelloWorld


def test_init_assumes_ownership_and_sets_defaults() -> None:
    actor = HelloWorld()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b"") == b"ok"
    assert chain.events()[0][0] == "hello_world.init"
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["greeting"] == "Hello, world!"
    assert stats["hello_count"] == 0
    assert stats["owner"] == "11" * 20  # the simulated sender became owner


def test_hello_route_increments_counter() -> None:
    actor = HelloWorld()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    resp = codec.decode(chain.call(actor.hello, b""))
    assert resp["status"] == 200
    body = json.loads(resp["body"].decode())
    assert body == {"greeting": "Hello, world!", "count": 1}
    name, data = chain.events()[-1]
    assert name == "hello_world.hello"
    assert codec.decode(data) == {"count": 1}
    home = codec.decode(chain.call(actor.home, b""))
    assert home["status"] == 200
    assert b"Said hello 1 time." in home["body"]


def test_set_greeting_owner_updates_page() -> None:
    actor = HelloWorld()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.set_greeting, codec.encode({"greeting": "Howdy!"})) == b"ok"
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["greeting"] == "Howdy!"
    home = codec.decode(chain.call(actor.home, b""))
    assert b"<h1>Howdy!</h1>" in home["body"]
