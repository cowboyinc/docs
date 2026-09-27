import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import CallMock, SimulatedChain

from actors.bettor.main import AutoBettor

ACTOR_ADDR = b"\x22" * 20  # the mock host's own-actor address; timer fires carry it as sender
SELF_ACTOR = "0x" + "22" * 20
CASINO = "0x" + "cc" * 20
TOKEN_ID = "0x" + "77" * 32


@pytest.fixture
def token_log(monkeypatch):
    """The mock host has no token_* bindings; stub a funded balance and log spends."""
    log = []
    monkeypatch.setattr(mock_host, "token_balance_of", lambda t, o: 1_000, raising=False)
    monkeypatch.setattr(mock_host, "token_transfer", lambda t, to, a: log.append(("transfer", to, a)), raising=False)
    return log


def _configured_bettor():
    actor = AutoBettor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = chain.call(
        actor.configure,
        codec.encode(
            {"self_actor": SELF_ACTOR, "casino_actor": CASINO, "token_id": TOKEN_ID, "min_guess": 10, "max_guess": 100, "bet_interval_blocks": 4, "bet_amount": 100}
        ),
    )
    assert reply == b"ok"
    return actor, chain


def test_configure_validates_and_inspect_reflects_it() -> None:
    actor = AutoBettor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.configure, codec.encode({"self_actor": SELF_ACTOR})) == b"bad_casino"
    actor, chain = _configured_bettor()
    view = codec.decode(chain.call(actor.inspect, codec.encode({})))
    assert view["casino_actor"] == CASINO
    assert view["bet_amount"] == 100
    assert view["enabled"] is False


def test_timer_run_places_one_prepaid_bet(token_log) -> None:
    actor, chain = _configured_bettor()
    casino = CallMock()
    casino.when(target=bytes.fromhex(CASINO[2:]), method="inspect_round").respond(
        codec.encode({"round_id": 1, "state": "open", "close_height": 1_000, "bets": []})
    )
    mock_host.set_call_handler(casino)
    assert chain.call(actor.start, codec.encode({})) == b"ok"
    chain.set_sender(ACTOR_ADDR)
    fired = chain.advance_block(1)
    results = [result for handler, result in fired if handler == "run"]
    assert results == [b"ok"]
    # Once the casino reports the bet, the next tick skips instead of re-betting.
    joined = CallMock()
    joined.when(target=bytes.fromhex(CASINO[2:]), method="inspect_round").respond(
        codec.encode({"round_id": 1, "state": "open", "close_height": 1_000, "bets": [{"bettor": SELF_ACTOR, "guess": 1, "stake": 100, "net_stake": 100}]})
    )
    mock_host.set_call_handler(joined)
    fired = chain.advance_block(1)
    assert [result for handler, result in fired if handler == "run"] == [b"already_joined_round"]
    # The stake moved to the casino up front, then the bet rode an async message.
    assert token_log == [("transfer", bytes.fromhex(CASINO[2:]), 100)]
    sent = mock_host.get_send_log()[-1]
    assert bytes(sent["target"]) == bytes.fromhex(CASINO[2:])
    envelope = codec.decode(sent["payload"])
    assert envelope["cmd"] == "submit_bet"
    assert envelope["stake"] == 100
    assert envelope["prepaid"] is True
    assert 10 <= envelope["guess"] <= 100
    view = codec.decode(chain.call(actor.inspect, codec.encode({})))
    assert view["bet_sequence"] == 1


def test_stop_disables_the_loop(token_log) -> None:
    actor, chain = _configured_bettor()
    assert chain.call(actor.start, codec.encode({})) == b"ok"
    assert chain.call(actor.stop, codec.encode({})) == b"ok"
    chain.set_sender(ACTOR_ADDR)
    fired = chain.advance_block(4)
    assert [result for handler, result in fired if handler == "run"] == [b"disabled"]
    assert token_log == []
