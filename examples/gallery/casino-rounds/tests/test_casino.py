import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import CallMock, SimulatedChain

from actors.casino.main import PredictionCasino

CASINO = "0x" + "22" * 20  # the mock host's own-actor address
FEED = "0x" + "fe" * 20
TOKEN_ID = "0x" + "77" * 32
BETTOR = "0x" + "aa" * 20
BETTOR_BYTES = bytes.fromhex("aa" * 20)


@pytest.fixture
def token_log(monkeypatch):
    """The mock host has no token_* bindings; stub balance reads and log payouts."""
    log = []
    monkeypatch.setattr(mock_host, "token_balance_of", lambda t, o: 0, raising=False)
    monkeypatch.setattr(mock_host, "token_transfer_from", lambda t, f, to, a: log.append(("transfer_from", f, to, a)), raising=False)
    monkeypatch.setattr(mock_host, "token_transfer", lambda t, to, a: log.append(("transfer", to, a)), raising=False)
    return log


def _configured_casino():
    actor = PredictionCasino()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = chain.call(
        actor.configure,
        codec.encode({"self_actor": CASINO, "feed_actor": FEED, "token_id": TOKEN_ID, "round_duration_blocks": 10, "fee_bps": 0}),
    )
    assert reply == b"ok"
    return actor, chain


def test_configure_validates_and_stores_wiring(token_log) -> None:
    actor = PredictionCasino()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.configure, codec.encode({"self_actor": CASINO})) == b"bad_feed"
    assert chain.call(actor.configure, codec.encode({"self_actor": CASINO, "feed_actor": FEED, "token_id": TOKEN_ID})) == b"ok"
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state["feed_actor"] == FEED
    assert state["token_id"] == TOKEN_ID
    assert state["active_round_id"] == 0


def test_place_bet_escrows_stake_into_the_pot(token_log) -> None:
    actor, chain = _configured_casino()
    assert chain.call(actor.open_round, codec.encode({})) == b"1"
    chain.set_sender(BETTOR)
    assert chain.call(actor.place_bet, codec.encode({"stake": 100, "guess": 42})) == b"ok"
    assert token_log == [("transfer_from", BETTOR_BYTES, bytes.fromhex("22" * 20), 100)]
    assert chain.call(actor.place_bet, codec.encode({"stake": 100, "guess": 43})) == b"duplicate"
    round_view = codec.decode(chain.call(actor.inspect_round, codec.encode({})))
    assert round_view["state"] == "open"
    assert round_view["total_pot"] == 100
    assert round_view["bet_count"] == 1


def test_close_round_settles_with_the_feed_price_and_pays_the_winner(token_log) -> None:
    actor, chain = _configured_casino()
    chain.call(actor.open_round, codec.encode({}))
    chain.set_sender(BETTOR)
    chain.call(actor.place_bet, codec.encode({"stake": 100, "guess": 42}))
    feed = CallMock()
    feed.when(target=bytes.fromhex(FEED[2:]), method="generate_price").respond(codec.encode({"price": 40, "height": 7}))
    mock_host.set_call_handler(feed)
    # Settlement runs through @callable_by(SELF) settle_round, so crank the
    # close from the casino's own address, the way its advance_cycle timer does.
    chain.set_sender(bytes.fromhex("22" * 20))
    assert chain.call(actor.close_round, codec.encode({})) == b"ok"
    assert feed.calls[-1][1] == "generate_price"
    # The sole bettor wins the whole pot back.
    assert token_log[-1] == ("transfer", BETTOR_BYTES, 100)
    resolved = codec.decode(chain.call(actor.inspect_round, codec.encode({"round_id": 1})))
    assert resolved["state"] == "resolved"
    assert resolved["resolved_price"] == 40
    assert resolved["winning_delta"] == 2
    assert resolved["payouts"] == [{"bettor": BETTOR, "amount": 100}]
