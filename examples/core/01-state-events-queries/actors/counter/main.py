import json
from cowboy_sdk import actor, pure, runtime, public


@actor
class CounterActor:
    def init(self, payload):
        data = json.loads(payload) if payload else {}
        start = int(data.get("start", 0))
        self.storage["count"] = start
        self.storage["last_event"] = {"status": "initialized", "count": start}
        runtime.emit_event("counter.init", {"count": start})
        return b"ok: initialized"

    @public(decode="cbor")
    def increment(self, payload):
        data = payload or {}
        amount = int(data.get("amount", 1))
        count = int(self.storage.get("count", 0)) + amount
        event = {"amount": amount, "count": count}
        self.storage["count"] = count
        self.storage["last_event"] = event
        runtime.emit_event("counter.incremented", event)
        return event

    @pure
    @public(decode="cbor")
    def get_state(self, payload):
        return {
            "count": int(self.storage.get("count", 0)),
            "last_event": self.storage.get("last_event", {"status": "idle"}),
        }


def _get_actor():
    return CounterActor()


def init(payload):
    return _get_actor().init(payload)


def increment(payload):
    return _get_actor().increment(payload)


def get_state(payload):
    return _get_actor().get_state(payload)
