from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.hello.main import HelloActor


def test_init_stores_greeting() -> None:
    actor = HelloActor()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"greeting":"howdy"}') == b"ok: initialized"
    assert chain.events()[0][0] == "hello.init"


def test_say_hello_combines_greeting_and_name() -> None:
    actor = HelloActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"greeting":"howdy"}')
    result = codec.decode(chain.call(actor.say_hello, codec.encode({"name": "builder"})))
    assert result == {"greeting": "howdy", "name": "builder", "message": "howdy, builder"}


def test_get_greeting_reads_last_message() -> None:
    actor = HelloActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.say_hello, codec.encode({"name": "builder"}))
    last = codec.decode(chain.call(actor.get_greeting, codec.encode({})))
    assert last["message"] == "hello, builder"
