import json

import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import DEFAULT_SENDER, SimulatedChain

from actors.dealer.main import BlackjackDealer
from actors.dealer import engine

TOKEN_ID = "0x" + "77" * 32
TOKEN_BYTES = bytes.fromhex("77" * 32)
TABLE = b"\x22" * 20  # the mock host's own-actor address
ALICE = "0x" + "aa" * 20
ALICE_BYTES = bytes.fromhex("aa" * 20)


@pytest.fixture
def token_log(monkeypatch):
    """The mock host has no token_* bindings; record chip custody moves instead."""
    log = []
    monkeypatch.setattr(mock_host, "token_transfer_from", lambda t, f, to, a: log.append(("transfer_from", f, to, a)), raising=False)
    monkeypatch.setattr(mock_host, "token_transfer", lambda t, to, a: log.append(("transfer", to, a)), raising=False)
    return log


def _table(shoe=None):
    actor = BlackjackDealer()
    chain = SimulatedChain(actor)
    config = {"token_id": TOKEN_ID, "min_bet": 10, "max_bet": 100, "max_players": 2}
    if shoe is not None:
        config["shoe"] = shoe
    reply = codec.decode(chain.call(actor.init, json.dumps(config).encode()))
    assert reply["status"] == "initialized"
    return actor, chain


def test_init_stores_table_config() -> None:
    actor, chain = _table()
    assert codec.decode(chain.state("min_bet")) == 10
    assert codec.decode(chain.state("max_bet")) == 100
    assert codec.decode(chain.state("token_id")) == TOKEN_ID
    name, data = chain.events()[0]
    assert name == "blackjack.init"
    assert codec.decode(data)["max_players"] == 2


def test_place_bet_escrows_chips_and_enforces_limits(token_log) -> None:
    actor, chain = _table()
    chain.set_sender(ALICE)
    joined = codec.decode(chain.call(actor.join_table, codec.encode({})))
    assert joined["status"] == "joined"
    assert codec.decode(chain.call(actor.place_bet, codec.encode({"amount": 5}))) == {"error": "BET_OUT_OF_RANGE"}
    placed = codec.decode(chain.call(actor.place_bet, codec.encode({"amount": 20})))
    assert placed == {"status": "bet_placed", "player": ALICE, "amount": 20}
    assert token_log == [("transfer_from", ALICE_BYTES, TABLE, 20)]
    assert codec.decode(chain.call(actor.place_bet, codec.encode({"amount": 20}))) == {"error": "BET_ALREADY_PLACED"}


def test_natural_blackjack_round_pays_three_to_two(token_log) -> None:
    # Deal order for one player: player, dealer, player, dealer.
    # Alice draws AS + KS (natural 21); the dealer shows 9H + 7D and busts on 6C.
    actor, chain = _table(shoe=["AS", "9H", "KS", "7D", "6C"])
    chain.set_sender(ALICE)
    chain.call(actor.join_table, codec.encode({}))
    chain.call(actor.place_bet, codec.encode({"amount": 20}))
    chain.set_sender(DEFAULT_SENDER)
    started = codec.decode(chain.call(actor.start_round, codec.encode({})))
    assert started["status"] == "round_started"
    assert started["phase"] == "ROUND_COMPLETE"
    last = codec.decode(chain.call(actor.get_last_round, codec.encode({})))
    assert last["payouts"] == {ALICE: 50}  # 20 back + 30 winnings = 3:2 blackjack
    assert last["player_results"][ALICE][0]["result"] == "blackjack"
    assert token_log[-1] == ("transfer", ALICE_BYTES, 50)


def test_engine_helpers_agree_on_settlement_rules() -> None:
    assert engine.hand_value(["AS", "KS"]) == {"total": 21, "soft": True}
    assert engine.is_blackjack(["AS", "KS"]) is True
    assert engine.is_blackjack(["AS", "KS"], from_split=True) is False
    assert engine.settle_hand(20, ["AS", "KS"], ["9H", "7D", "6C"]) == {"outcome": "blackjack", "payout": 50}
    assert engine.settle_hand(20, ["TS", "9H"], ["TS", "9D"]) == {"outcome": "push", "payout": 20}
    assert engine.can_split(["8S", "8H"]) is True
    assert engine.dealer_should_hit(["9H", "7D"]) is True
