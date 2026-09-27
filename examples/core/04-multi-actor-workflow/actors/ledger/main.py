import json
from cowboy_sdk import Address, actor, deferred, pure, public, send, codec


@actor
class SettlementLedger:
    """Terminal actor that records approved and rejected submissions."""

    def _ensure_state(self):
        if self.storage.get("settlements") is None:
            self.storage["settlements"] = {}
        if self.storage.get("history") is None:
            self.storage["history"] = []
        if self.storage.get("owner") is None:
            self.storage["owner"] = self.sender.to_hex(checksum=False)

    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Settlement Ledger"
        self.storage["__description__"] = "Multi-actor workflow: settlement ledger"
        self.storage["__version__"] = "1.0.0"
        data = json.loads(payload) if payload else {}
        sender = self.sender.to_hex(checksum=False)
        self.storage["owner"] = Address(data.get("owner") or sender).to_hex(checksum=False)
        self.storage["board_address"] = str(data.get("board_address", ""))
        self.storage["settlements"] = {}
        self.storage["history"] = []
        return codec.encode({"status": "initialized"})

    @pure
    @public(decode="cbor")
    def configure(self, payload):
        self._ensure_state()
        data = payload or {}
        owner = self.storage.get("owner", "")
        sender = self.sender.to_hex(checksum=False)
        if owner and owner != sender:
            return {"error": "UNAUTHORIZED"}
        if not owner:
            self.storage["owner"] = sender
        self.storage["board_address"] = str(data.get("board_address", ""))
        return {"status": "configured", "board_address": self.storage.get("board_address", "")}

    def _settle(self, data):
        self._ensure_state()
        submission_id = int(data.get("submission_id", 0))
        approved = bool(data.get("approved", False))
        settlement = {
            "submission_id": submission_id,
            "job_id": int(data.get("job_id", 0)),
            "state": "SETTLED_APPROVED" if approved else "SETTLED_REJECTED",
            "reason": str(data.get("reason", "")),
            "worker": str(data.get("worker", "")),
            "reward_units": int(data.get("reward_units", 0)),
        }

        settlements = dict(self.storage.get("settlements") or {})
        settlements[submission_id] = settlement
        self.storage["settlements"] = settlements

        history = list(self.storage.get("history") or [])
        history.append(settlement)
        self.storage["history"] = history

        board_address = self.storage.get("board_address", "")
        if board_address:
            send(
                board_address,
                codec.encode(
                    {
                        "submission_id": submission_id,
                        "result": settlement["state"],
                    }
                ),
                raw=True,
            )

        return {"status": "settled", **settlement}

    @deferred
    @public(decode="cbor")
    def settle_submission(self, payload):
        return self._settle(payload or {})

    @pure
    @public(decode="cbor")
    def get_settlement(self, payload):
        self._ensure_state()
        data = payload or {}
        submission_id = int(data.get("submission_id", 0))
        settlements = dict(self.storage.get("settlements") or {})
        return settlements.get(submission_id, {"error": "SETTLEMENT_NOT_FOUND"})

    @pure
    @public(decode="cbor")
    def get_history(self, payload):
        self._ensure_state()
        data = payload or {}
        limit = int(data.get("limit", 10))
        history = list(self.storage.get("history") or [])
        return {"history": history[-limit:]}

    @deferred
    @public(decode="cbor")
    def handle_message(self, payload):
        return self._settle(payload or {})


def _get_actor():
    return SettlementLedger()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def settle_submission(payload):
    return _get_actor().settle_submission(payload)


def get_settlement(payload):
    return _get_actor().get_settlement(payload)


def get_history(payload):
    return _get_actor().get_history(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)
