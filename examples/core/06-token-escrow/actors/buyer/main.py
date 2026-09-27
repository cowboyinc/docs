from cowboy_sdk import Address, TokenId, actor, call, codec, public, runtime


@actor
class BuyerActor:
    """Buyer-side helper actor that approves and funds escrow deposits."""

    def init(self, payload):
        self.storage["__name__"] = "Escrow Workflow Buyer"
        self.storage["__description__"] = "Buyer-side helper for CIP-20 escrow"
        self.storage["__version__"] = "1.0.0"
        return codec.encode({"status": "initialized"})

    def _owner(self):
        return self.storage.get("owner", "")

    def _require_owner(self):
        return self.sender.to_hex(checksum=False) == self._owner()

    @public(decode="cbor")
    def configure(self, payload):
        data = payload or {}
        raw_escrow = str(data.get("escrow_address", "")).strip()
        escrow_address = Address(raw_escrow).to_hex(checksum=False) if raw_escrow else ""
        raw_token = str(data.get("token_id", "")).strip()
        token_id = TokenId(raw_token).to_hex() if raw_token else ""
        if not escrow_address or not token_id:
            return {"error": "INVALID_CONFIGURATION"}
        if not self.storage.get("owner"):
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        elif not self._require_owner():
            return {"error": "OWNER_ONLY"}
        self.storage["escrow_address"] = escrow_address
        self.storage["token_id"] = token_id
        return {"status": "configured", "escrow_address": escrow_address, "token_id": token_id}

    @public(decode="cbor")
    def approve_escrow(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        escrow_address = self.storage.get("escrow_address", "")
        token_id = self.storage.get("token_id", "")
        amount = int(data.get("amount", 0) or 0)
        if not escrow_address or not token_id:
            return {"error": "ESCROW_NOT_CONFIGURED"}
        if amount <= 0:
            return {"error": "INVALID_AMOUNT"}
        runtime.token_approve(token_id, escrow_address, amount)
        return {"status": "approved", "amount": amount, "escrow_address": escrow_address}

    @public(decode="cbor")
    def deposit(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        escrow_address = self.storage.get("escrow_address", "")
        if not escrow_address:
            return {"error": "ESCROW_NOT_CONFIGURED"}

        trade_id = str(data.get("trade_id", ""))
        raw_seller = str(data.get("seller", "")).strip()
        seller = Address(raw_seller).to_hex(checksum=False) if raw_seller else ""
        amount = int(data.get("amount", 0) or 0)
        result = call(
            escrow_address,
            "deposit",
            {
                "trade_id": trade_id,
                "seller": seller,
                "amount": amount,
            },
        )
        return {
            "status": "deposit_requested",
            "trade_id": trade_id,
            "amount": amount,
            "escrow_result": result,
        }

    @public(decode="cbor")
    def cancel(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        escrow_address = self.storage.get("escrow_address", "")
        if not escrow_address:
            return {"error": "ESCROW_NOT_CONFIGURED"}

        trade_id = str(data.get("trade_id", ""))
        result = call(
            escrow_address,
            "cancel",
            {
                "trade_id": trade_id,
            },
        )
        return {
            "status": "cancel_requested",
            "trade_id": trade_id,
            "escrow_result": result,
        }


def _get_actor():
    return BuyerActor()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def deposit(payload):
    return _get_actor().deposit(payload)


def cancel(payload):
    return _get_actor().cancel(payload)


def approve_escrow(payload):
    return _get_actor().approve_escrow(payload)
