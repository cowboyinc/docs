"""Ring node actor using the runtime-supported actor API."""

from cowboy_sdk import Address, actor, deferred, public, pure, runtime, send, codec


@actor
class RingNode:
    def init(self, payload):
        self.storage["__name__"] = "Ring Node"
        self.storage["__description__"] = "Ring network demo node"
        self.storage["__version__"] = "1.0.0"
        self.storage["next_actor"] = ""
        self.storage["hop_count"] = 0
        return b"ok"

    @public(decode="cbor")
    def configure(self, payload):
        data = payload or {}
        raw = data.get("next_actor", "")
        next_actor = Address(raw).to_hex(checksum=False) if raw else ""
        if not next_actor:
            return b"error: missing next actor"
        self.storage["next_actor"] = next_actor
        self.storage["hop_count"] = 0
        runtime.emit_event("configured", {"next": next_actor})
        return b"ok"

    @deferred
    @public(decode="cbor")
    def handle_message(self, payload):
        next_actor_hex = self.storage.get("next_actor", "")
        if not next_actor_hex:
            runtime.emit_event("error", {"reason": "not configured"})
            return b""

        data = payload or {}
        hop = int(data.get("hop", 0)) + 1
        count = int(self.storage.get("hop_count", 0)) + 1
        self.storage["hop_count"] = count

        message = {
            "msg": str(data.get("msg", "")),
            "hop": hop,
        }
        runtime.emit_event(
            "ring_hop",
            {"hop": hop, "local_count": count, "msg": message["msg"]},
        )
        send(next_actor_hex, codec.encode(message), raw=True)
        return b"ok"

    @deferred
    @public(decode="cbor")
    def start_ring(self, payload):
        next_actor_hex = self.storage.get("next_actor", "")
        if not next_actor_hex:
            return b"not configured"
        data = payload or {}
        msg = str(data.get("msg", "hello ring"))
        runtime.emit_event("ring_started", {"msg": msg})
        send(next_actor_hex, codec.encode({"msg": msg, "hop": 0}), raw=True)
        return b"ok"

    @public(decode="cbor")
    @pure
    def get_state(self, payload):
        return {
            "next_actor": self.storage.get("next_actor", ""),
            "hop_count": int(self.storage.get("hop_count", 0)),
        }


def _get_actor():
    return RingNode()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def start_ring(payload):
    return _get_actor().start_ring(payload)


def get_state(payload):
    return _get_actor().get_state(payload)
