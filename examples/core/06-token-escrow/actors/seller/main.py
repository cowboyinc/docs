from cowboy_sdk import Address, actor, call, codec, public, runtime


@actor
class SellerActor:
    """Seller-side helper actor that confirms delivery with escrow."""

    def init(self, payload):
        self.storage["__name__"] = "Escrow Workflow Seller"
        self.storage["__description__"] = "Seller-side helper for CIP-20 escrow"
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
        if not escrow_address:
            return {"error": "INVALID_CONFIGURATION"}
        if not self.storage.get("owner"):
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        elif not self._require_owner():
            return {"error": "OWNER_ONLY"}
        self.storage["escrow_address"] = escrow_address
        return {"status": "configured", "escrow_address": escrow_address}

    @public(decode="cbor")
    def lock(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        escrow_address = self.storage.get("escrow_address", "")
        if not escrow_address:
            return {"error": "ESCROW_NOT_CONFIGURED"}

        trade_id = str(data.get("trade_id", ""))
        result = call(
            escrow_address,
            "lock",
            {
                "trade_id": trade_id,
                "seller": self.address.to_hex(checksum=False) or self.sender.to_hex(checksum=False),
            },
        )
        return {
            "status": "lock_requested",
            "trade_id": trade_id,
            "escrow_result": result,
        }

    @public(decode="cbor")
    def confirm_delivery(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        escrow_address = self.storage.get("escrow_address", "")
        if not escrow_address:
            return {"error": "ESCROW_NOT_CONFIGURED"}

        trade_id = str(data.get("trade_id", ""))
        result = call(
            escrow_address,
            "release",
            {
                "trade_id": trade_id,
                "seller": self.address.to_hex(checksum=False) or self.sender.to_hex(checksum=False),
            },
        )
        return {
            "status": "release_requested",
            "trade_id": trade_id,
            "escrow_result": result,
        }


def _get_actor():
    return SellerActor()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def lock(payload):
    return _get_actor().lock(payload)


def confirm_delivery(payload):
    return _get_actor().confirm_delivery(payload)
