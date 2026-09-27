import json

import pytest
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.token.main import TokenManager

TOKEN_ID = b"\x77" * 32
# The handlers are @public(decode="cbor"), so any sender may call them; the
# fake registry below credits this address, so the chain runs calls as it.
SELF = b"\x22" * 20


class FakeRegistry:
    """Stand-in for the validator's Token Registry: the mock host has no
    token_* bindings, so the test installs these on it."""

    def __init__(self):
        self.balances = {}
        self.supply = 0
        self.created = []

    def token_create(self, name, symbol, decimals, initial_supply, max_supply, hook, uri):
        self.created.append((name, symbol, decimals, initial_supply))
        self.supply = initial_supply
        self.balances[SELF] = initial_supply
        return TOKEN_ID

    def token_balance_of(self, token_id, owner):
        assert token_id == TOKEN_ID
        return self.balances.get(bytes(owner), 0)

    def token_total_supply(self, token_id):
        assert token_id == TOKEN_ID
        return self.supply

    def token_transfer(self, token_id, to, amount):
        assert token_id == TOKEN_ID
        self.balances[SELF] -= amount
        self.balances[bytes(to)] = self.balances.get(bytes(to), 0) + amount


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    for name in ("token_create", "token_balance_of", "token_total_supply", "token_transfer"):
        monkeypatch.setattr(mock_host, name, getattr(fake, name), raising=False)
    return fake


def test_create_token_stores_id_and_emits_event(registry) -> None:
    actor = TokenManager()
    chain = SimulatedChain(actor, sender=SELF)
    result = json.loads(chain.call(actor.create_token, b""))
    assert result == {"status": "created", "token_id": TOKEN_ID.hex(), "initial_supply": 1_000_000, "decimals": 6}
    assert registry.created == [(b"Demo Token", b"DEMO", 6, 1_000_000_000_000)]
    name, data = chain.events()[-1]
    assert name == "token_created" and codec.decode(data) == result


def test_check_balances_reads_registry_for_sender(registry) -> None:
    actor = TokenManager()
    chain = SimulatedChain(actor, sender=SELF)
    assert json.loads(chain.call(actor.check_balances, b"")) == {"error": "no token created yet"}
    chain.call(actor.create_token, b"")
    balances = json.loads(chain.call(actor.check_balances, b""))
    assert balances == {"sender_balance": 1_000_000_000_000, "total_supply": 1_000_000_000_000, "token_id": TOKEN_ID.hex()}


def test_transfer_moves_tokens_to_recipient(registry) -> None:
    actor = TokenManager()
    chain = SimulatedChain(actor, sender=SELF)
    chain.call(actor.create_token, b"")
    alice = "0x" + "aa" * 20
    result = json.loads(chain.call(actor.transfer, codec.encode({"to": alice, "amount": 2500})))
    assert result == {"status": "transferred", "to": alice, "amount": 2500}
    assert registry.balances[bytes.fromhex("aa" * 20)] == 2500
    assert registry.balances[SELF] == 1_000_000_000_000 - 2500
    name, data = chain.events()[-1]
    assert name == "token_transferred" and codec.decode(data) == result
