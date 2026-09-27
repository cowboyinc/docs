"""
Example actor that creates and manages a CIP-20 token from inside the PVM.

This actor demonstrates using runtime.token_* host functions to interact
with the native Token Registry from actor code. Deploy this actor and
call its handlers to create tokens, check balances, and transfer.

Deploy:
  cowboy actor deploy token

Execute:
  cowboy actor execute --actor <ADDR> --handler create_token --payload-json '{}'
  cowboy actor execute --actor <ADDR> --handler check_balances --payload-json '{}'
"""

import json
from cowboy_sdk import runtime, actor, public


@actor
class TokenManager:
    """Actor that creates and manages a CIP-20 token."""

    # token_* host functions are system instructions the validator applies
    # synchronously, not deferred sends, so these handlers need no @deferred.
    @public(decode="cbor")
    def create_token(self, payload):
        """Create a new token and store its ID."""
        token_id = runtime.token_create(
            b"Demo Token",     # name
            b"DEMO",           # symbol
            6,                 # decimals (like USDC)
            1_000_000_000_000, # initial_supply: 1M tokens (6 decimals)
            None,              # max_supply
            None,              # transfer_hook
            None,              # metadata_uri
        )
        token_id = bytes(token_id)

        # Store the token_id so other handlers can use it
        self.storage['token_id'] = token_id.hex()

        result = {
            "status": "created",
            "token_id": token_id.hex(),
            "initial_supply": 1_000_000,
            "decimals": 6,
        }
        runtime.emit_event("token_created", result)
        return json.dumps(result).encode()

    @public(decode="cbor")
    def check_balances(self, payload):
        """Check the creator's token balance."""
        sender = self.sender

        token_id_hex = self.storage.get('token_id')
        if not token_id_hex:
            return json.dumps({"error": "no token created yet"}).encode()

        balance = int(runtime.token_balance_of(token_id_hex, sender))
        supply = int(runtime.token_total_supply(token_id_hex))

        result = {
            "sender_balance": balance,
            "total_supply": supply,
            "token_id": token_id_hex,
        }
        return json.dumps(result).encode()

    @public(decode="cbor")
    def transfer(self, payload):
        """Transfer tokens. Payload: {"to": "0x...", "amount": 1000}"""
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload

        token_id_hex = self.storage.get('token_id')
        if not token_id_hex:
            return json.dumps({"error": "no token created yet"}).encode()

        amount = int(data["amount"])

        runtime.token_transfer(token_id_hex, data["to"], amount)

        result = {"status": "transferred", "to": data["to"], "amount": amount}
        runtime.emit_event("token_transferred", result)
        return json.dumps(result).encode()


def _get_actor():
    return TokenManager()


def create_token(payload):
    return _get_actor().create_token(payload)


def check_balances(payload):
    return _get_actor().check_balances(payload)


def transfer(payload):
    return _get_actor().transfer(payload)
