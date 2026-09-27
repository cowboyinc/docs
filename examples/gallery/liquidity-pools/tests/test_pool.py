import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.pool.main import MINIMUM_LIQUIDITY, V2Pool, _isqrt

POOL = "0x" + "22" * 20  # the mock host's own-actor address
POOL_BYTES = bytes.fromhex("22" * 20)
TOKEN_A = "77" * 32
TOKEN_B = "88" * 32
LP_TOKEN = b"\xaa" * 32
LP = "0x" + "aa" * 20
LP_BYTES = bytes.fromhex("aa" * 20)


@pytest.fixture
def token_host(monkeypatch):
    """The mock host has no token_* bindings; fake a minimal ledger that tracks
    the LP token supply and logs every custody move."""
    state = {"lp_supply": 0, "log": []}
    monkeypatch.setattr(mock_host, "token_create", lambda *a: LP_TOKEN, raising=False)

    def total_supply(token_id):
        assert bytes(token_id) == LP_TOKEN
        return state["lp_supply"]

    def mint(token_id, to, amount):
        state["lp_supply"] += amount
        state["log"].append(("mint", bytes(to), amount))

    def burn(token_id, amount):
        state["lp_supply"] -= amount
        state["log"].append(("burn", amount))

    monkeypatch.setattr(mock_host, "token_total_supply", total_supply, raising=False)
    monkeypatch.setattr(mock_host, "token_mint", mint, raising=False)
    monkeypatch.setattr(mock_host, "token_burn", burn, raising=False)
    monkeypatch.setattr(mock_host, "token_transfer_from", lambda t, f, to, a: state["log"].append(("transfer_from", bytes(t), bytes(f), bytes(to), a)), raising=False)
    monkeypatch.setattr(mock_host, "token_transfer", lambda t, to, a: state["log"].append(("transfer", bytes(t), bytes(to), a)), raising=False)
    return state


def _initialized_pool():
    actor = V2Pool()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(chain.call(actor.init_pool, codec.encode({"token_a": TOKEN_A, "token_b": TOKEN_B, "fee_bps": 30, "pool_address": POOL})))
    assert reply["status"] == "initialized"
    assert reply["lp_token"] == LP_TOKEN.hex()
    return actor, chain


def test_init_pool_is_one_time(token_host) -> None:
    actor, chain = _initialized_pool()
    assert codec.decode(chain.state("fee_bps")) == 30
    again = codec.decode(chain.call(actor.init_pool, codec.encode({"token_a": TOKEN_A, "token_b": TOKEN_B})))
    assert again == {"error": "already initialized"}


def test_first_deposit_mints_sqrt_liquidity_and_locks_the_minimum(token_host) -> None:
    actor, chain = _initialized_pool()
    chain.set_sender(LP)
    reply = codec.decode(chain.call(actor.add_liquidity, codec.encode({"amount_a": 10_000, "amount_b": 250_000})))
    expected = _isqrt(10_000 * 250_000) - MINIMUM_LIQUIDITY
    assert reply["status"] == "liquidity_added"
    assert reply["lp_minted"] == expected
    assert reply["reserve_a"] == 10_000 and reply["reserve_b"] == 250_000
    assert ("mint", POOL_BYTES, MINIMUM_LIQUIDITY) in token_host["log"]
    assert ("mint", LP_BYTES, expected) in token_host["log"]
    assert token_host["lp_supply"] == expected + MINIMUM_LIQUIDITY


def test_swap_follows_constant_product_with_fee(token_host) -> None:
    actor, chain = _initialized_pool()
    chain.set_sender(LP)
    chain.call(actor.add_liquidity, codec.encode({"amount_a": 10_000, "amount_b": 250_000}))
    reply = codec.decode(chain.call(actor.swap, codec.encode({"token_in": TOKEN_B, "amount_in": 1_000, "min_amount_out": 0})))
    amount_in_with_fee = 1_000 * (10_000 - 30)
    expected_out = (amount_in_with_fee * 10_000) // (250_000 * 10_000 + amount_in_with_fee)
    assert reply == {
        "status": "swapped",
        "token_in": TOKEN_B,
        "amount_in": 1_000,
        "amount_out": expected_out,
        "reserve_a": 10_000 - expected_out,
        "reserve_b": 251_000,
    }
    assert token_host["log"][-1] == ("transfer", bytes.fromhex(TOKEN_A), LP_BYTES, expected_out)
    slipped = codec.decode(chain.call(actor.swap, codec.encode({"token_in": TOKEN_B, "amount_in": 1_000, "min_amount_out": 10_000})))
    assert slipped == {"error": "slippage"}


def test_remove_liquidity_pays_out_proportionally_and_rejects_spoofed_sender(token_host) -> None:
    actor, chain = _initialized_pool()
    chain.set_sender(LP)
    minted = codec.decode(chain.call(actor.add_liquidity, codec.encode({"amount_a": 10_000, "amount_b": 250_000})))["lp_minted"]
    spoofed = codec.decode(chain.call(actor.remove_liquidity, codec.encode({"lp_amount": minted, "sender": "0x" + "bb" * 20})))
    assert spoofed == {"error": "sender mismatch"}
    reply = codec.decode(chain.call(actor.remove_liquidity, codec.encode({"lp_amount": minted})))
    total = minted + MINIMUM_LIQUIDITY
    assert reply["status"] == "liquidity_removed"
    assert reply["amount_a"] == (minted * 10_000) // total
    assert reply["amount_b"] == (minted * 250_000) // total
    assert ("burn", minted) in token_host["log"]
    reserves = codec.decode(chain.call(actor.get_reserves, codec.encode({})))
    assert reserves["reserve_a"] == 10_000 - reply["amount_a"]
    assert reserves["lp_total_supply"] == MINIMUM_LIQUIDITY
