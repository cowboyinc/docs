from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.feed.main import MockPriceFeed


def _configured_feed(min_price=10, max_price=100):
    actor = MockPriceFeed()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.configure, codec.encode({"min_price": min_price, "max_price": max_price})) == b"ok"
    return actor, chain


def test_configure_rejects_inverted_bounds() -> None:
    actor = MockPriceFeed()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.configure, codec.encode({"min_price": 100, "max_price": 10})) == b"bad_bounds"
    assert chain.call(actor.configure, codec.encode({"min_price": 10, "max_price": 100})) == b"ok"
    name, data = chain.events()[-1]
    assert name == "feed.configured"
    assert codec.decode(data) == {"min_price": 10, "max_price": 100, "auto_interval_blocks": 0}


def test_generate_price_stays_in_bounds_and_advances_the_count() -> None:
    actor, chain = _configured_feed()
    first = codec.decode(chain.call(actor.generate_price, codec.encode({})))
    assert 10 <= first["price"] <= 100
    assert first["update_count"] == 1
    second = codec.decode(chain.call(actor.generate_price, codec.encode({})))
    assert second["update_count"] == 2
    view = codec.decode(chain.call(actor.get_price, codec.encode({})))
    assert view["price"] == second["price"]
    assert view["update_count"] == 2


def test_generate_price_is_deterministic_per_height_and_count() -> None:
    actor_a, chain_a = _configured_feed()
    price_a = codec.decode(chain_a.call(actor_a.generate_price, codec.encode({})))["price"]
    actor_b, chain_b = _configured_feed()
    price_b = codec.decode(chain_b.call(actor_b.generate_price, codec.encode({})))["price"]
    assert price_a == price_b
