from cowboy_sdk import Address, TokenId, actor, codec, public, runtime


@actor
class EscrowActor:
    """Authoritative escrow state machine with CIP-20-backed custody."""

    def init(self, payload):
        self.storage["__name__"] = "Escrow Workflow Custodian"
        self.storage["__description__"] = "CIP-20 escrow state machine"
        self.storage["__version__"] = "1.0.0"
        return codec.encode({"status": "initialized"})

    def _get_trades(self):
        return dict(self.storage.get("trades", {}))

    def _set_trades(self, trades):
        self.storage["trades"] = trades

    def _owner(self):
        return self.storage.get("owner", "")

    def _require_owner(self):
        return self.sender.to_hex(checksum=False) == self._owner()

    def _token_id(self):
        return self.storage.get("token_id", "")

    @public(decode="cbor")
    def configure(self, payload):
        data = payload or {}
        raw_token = str(data.get("token_id", "")).strip()
        token_id = TokenId(raw_token).to_hex() if raw_token else ""
        if not token_id:
            return {"error": "TOKEN_ID_REQUIRED"}
        owner = self._owner()
        if not owner:
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        elif not self._require_owner():
            return {"error": "OWNER_ONLY"}
        self.storage["token_id"] = token_id
        return {"status": "configured", "token_id": token_id}

    @public(decode="cbor")
    def deposit(self, payload):
        data = payload or {}
        trade_id = str(data.get("trade_id", ""))
        raw_buyer = str(data.get("buyer", "")).strip()
        buyer_override = Address(raw_buyer).to_hex(checksum=False) if raw_buyer else ""
        if buyer_override and not self._require_owner():
            return {"error": "OWNER_ONLY"}
        buyer = buyer_override or self.sender.to_hex(checksum=False)
        raw_seller = str(data.get("seller", "")).strip()
        seller = Address(raw_seller).to_hex(checksum=False) if raw_seller else ""
        amount = int(data.get("amount", 0) or 0)
        token_id = self._token_id()

        if not token_id:
            return {"error": "TOKEN_NOT_CONFIGURED"}
        if not trade_id or not buyer or not seller or amount <= 0:
            return {"error": "INVALID_DEPOSIT"}

        trades = self._get_trades()
        if trade_id in trades:
            return {"error": "TRADE_ALREADY_EXISTS"}

        runtime.token_transfer_from(token_id, buyer, runtime.get_actor_address(), amount)
        trades[trade_id] = {
            "trade_id": trade_id,
            "buyer": buyer,
            "seller": seller,
            "amount": amount,
            "token_id": token_id,
            "state": "DEPOSITED",
        }
        self._set_trades(trades)
        return {"status": "deposited", "trade_id": trade_id, "state": "DEPOSITED"}

    @public(decode="cbor")
    def lock(self, payload):
        data = payload or {}
        trade_id = str(data.get("trade_id", ""))
        raw_seller = str(data.get("seller", "")).strip()
        seller = Address(raw_seller).to_hex(checksum=False) if raw_seller else ""

        trades = self._get_trades()
        trade = trades.get(trade_id)
        if not trade:
            return {"error": "TRADE_NOT_FOUND"}
        if self.sender.to_hex(checksum=False) != seller and not self._require_owner():
            return {"error": "UNAUTHORIZED"}
        if seller != trade.get("seller"):
            return {"error": "UNAUTHORIZED"}
        if trade.get("state") != "DEPOSITED":
            return {"error": "INVALID_LOCK"}

        trade["state"] = "LOCKED"
        trades[trade_id] = trade
        self._set_trades(trades)
        return {"status": "locked", "trade_id": trade_id, "state": "LOCKED"}

    @public(decode="cbor")
    def release(self, payload):
        data = payload or {}
        trade_id = str(data.get("trade_id", ""))
        raw_seller = str(data.get("seller", "")).strip()
        seller = Address(raw_seller).to_hex(checksum=False) if raw_seller else ""

        trades = self._get_trades()
        trade = trades.get(trade_id)
        if not trade:
            return {"error": "TRADE_NOT_FOUND"}
        if self.sender.to_hex(checksum=False) != seller and not self._require_owner():
            return {"error": "UNAUTHORIZED"}
        if seller != trade.get("seller") or trade.get("state") != "LOCKED":
            return {"error": "INVALID_RELEASE"}

        runtime.token_transfer(trade.get("token_id", self._token_id()), trade.get("seller", ""), int(trade.get("amount", 0)))
        trade["state"] = "RELEASED"
        trades[trade_id] = trade
        self._set_trades(trades)
        return {"status": "released", "trade_id": trade_id, "state": "RELEASED"}

    @public(decode="cbor")
    def cancel(self, payload):
        data = payload or {}
        trade_id = str(data.get("trade_id", ""))
        buyer = self.sender.to_hex(checksum=False)

        trades = self._get_trades()
        trade = trades.get(trade_id)
        if not trade:
            return {"error": "TRADE_NOT_FOUND"}
        if self._require_owner():
            buyer = trade.get("buyer", "")
        if buyer != trade.get("buyer") or trade.get("state") != "DEPOSITED":
            return {"error": "INVALID_CANCEL"}

        runtime.token_transfer(trade.get("token_id", self._token_id()), trade.get("buyer", ""), int(trade.get("amount", 0)))
        trade["state"] = "CANCELLED"
        trades[trade_id] = trade
        self._set_trades(trades)
        return {"status": "cancelled", "trade_id": trade_id, "state": "CANCELLED"}

    @public(decode="cbor")
    def get_trade(self, payload):
        data = payload or {}
        trade_id = str(data.get("trade_id", ""))
        trade = self._get_trades().get(trade_id)
        if trade is None:
            return {"error": "TRADE_NOT_FOUND"}
        runtime.emit_event("get_trade", trade)
        return trade


def _get_actor():
    return EscrowActor()


def init(payload):
    return _get_actor().init(payload)


def deposit(payload):
    return _get_actor().deposit(payload)


def configure(payload):
    return _get_actor().configure(payload)


def lock(payload):
    return _get_actor().lock(payload)


def release(payload):
    return _get_actor().release(payload)


def cancel(payload):
    return _get_actor().cancel(payload)


def get_trade(payload):
    return _get_actor().get_trade(payload)
