import _sdk_path  # noqa: F401  (examples/_sdk_path.py, on pythonpath via pyproject.toml)
from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.counter.main import CounterActor


def test_init_stores_start_value() -> None:
    actor = CounterActor()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"start":2}') == b"ok: initialized"
    assert codec.decode(chain.state("count")) == 2
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("counter.init", {"count": 2})


def test_increment_adds_amount_and_emits_event() -> None:
    actor = CounterActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"start":2}')
    result = codec.decode(chain.call(actor.increment, codec.encode({"amount": 5})))
    assert result == {"amount": 5, "count": 7}
    assert codec.decode(chain.state("count")) == 7
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("counter.incremented", {"amount": 5, "count": 7})


def test_get_state_reads_count_and_last_event() -> None:
    actor = CounterActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"start":2}')
    chain.call(actor.increment, codec.encode({"amount": 5}))
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state == {"count": 7, "last_event": {"amount": 5, "count": 7}}
