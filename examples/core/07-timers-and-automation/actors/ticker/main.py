import json

from cowboy_sdk import actor, deferred, pure, runtime, public, codec, timer


def _timer_payload(payload):
    """Decode a payload that may arrive from a timer fire or from a direct call.

    A timer routed by `schedule_after` delivers the envelope's `_payload` as a
    JSON list of byte values — the leading `[` marks that form, and the bytes it
    holds are the CBOR that was scheduled. A direct call brings CBOR, or the raw
    JSON the CLI sends. The SDK has no decoder for the delivered form, so this
    stays hand-written until one lands.
    """
    if not payload:
        return {}
    if payload[:1] == b"[":
        payload = bytes(json.loads(payload))
    try:
        return codec.decode(payload) or {}
    except Exception:
        return json.loads(payload) or {}


@actor
class TimerCounter:
    """Timer-only counter example with explicit rescheduling metadata."""

    # Deploy runs init in the deploy transaction and the demo calls it again, so
    # init is @public and returns early rather than zeroing a counter the timer
    # loop may already have advanced.
    @public(decode="raw")
    def init(self, payload):
        if runtime.is_initialized():
            return codec.encode({
                "status": "already_initialized",
                "interval_blocks": int(self.storage.get("interval_blocks", 10)),
            })
        self.storage["__name__"] = "Timer Counter"
        self.storage["__description__"] = "Timer-based scheduled actor"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["owner"] = obj.get("owner", "")
        self.storage["interval_blocks"] = int(obj.get("interval_blocks", 10))
        self.storage["max_ticks"] = int(obj.get("max_ticks", 1))
        self.storage["count"] = 0
        self.storage["started"] = False
        self.storage["next_timer"] = {}
        return codec.encode({
            "status": "initialized",
            "interval_blocks": int(self.storage["interval_blocks"]),
        })

    def _schedule_next(self, delay_blocks):
        # fee_payer may only be the actor or the tx sender, so this bills the
        # first fire to whoever called start. A re-arm from inside on_timer has
        # sender == the actor, so recurring fires are actor-funded: keep the
        # actor topped up or the loop stops when it runs dry.
        timer_id = runtime.schedule_after(
            int(delay_blocks),
            "on_timer",
            codec.encode({"tag": "tick"}),
            fee_payer=runtime.get_sender() or None,
        )
        next_timer = {
            "delay_blocks": int(delay_blocks),
            "handler": "on_timer",
            "tag": "tick",
            "timer_id": timer_id.hex(),
        }
        self.storage["next_timer"] = next_timer
        return next_timer

    # CIP-6 §8: scheduling a timer is an async side effect, so the handler that
    # arms the first fire declares @deferred. on_timer gets the mode from @timer.
    @public(decode="cbor")
    @deferred
    def start(self, payload):
        obj = payload or {}
        if "interval_blocks" in obj:
            self.storage["interval_blocks"] = int(obj.get("interval_blocks", self.storage.get("interval_blocks", 10)))
        self.storage["max_ticks"] = int(obj.get("max_ticks", self.storage.get("max_ticks", 1)))
        self.storage["count"] = 0
        self.storage["started"] = True
        next_timer = self._schedule_next(int(self.storage.get("interval_blocks", 10)))
        runtime.emit_event("timer_counter.started", next_timer)
        return {"status": "started", "next_timer": next_timer}

    # @timer gives the fire handler the deferred mode a re-arm needs, and is
    # deliberately not @public: a fire carries the actor's own address as sender,
    # which deny-by-default admits, so outside callers cannot tick the counter.
    @timer
    def on_timer(self, payload):
        obj = _timer_payload(payload)
        count = int(self.storage.get("count", 0)) + 1
        self.storage["count"] = count
        max_ticks = int(self.storage.get("max_ticks", 1))
        if count >= max_ticks:
            self.storage["started"] = False
            self.storage["next_timer"] = {}
            next_timer = {}
        else:
            next_timer = self._schedule_next(int(self.storage.get("interval_blocks", 10)))
        result = {
            "status": "completed" if count >= max_ticks else "ticked",
            "tag": str(obj.get("tag", "tick")),
            "count": count,
            "max_ticks": max_ticks,
            "next_timer": next_timer,
        }
        runtime.emit_event("timer_counter.tick", result)
        return codec.encode(result)

    @pure
    @public(decode="cbor")
    def get_state(self, payload):
        response = {
            "started": bool(self.storage.get("started", False)),
            "count": int(self.storage.get("count", 0)),
            "max_ticks": int(self.storage.get("max_ticks", 1)),
            "interval_blocks": int(self.storage.get("interval_blocks", 10)),
            "next_timer": self.storage.get("next_timer", {}),
        }
        runtime.emit_event("get_state", response)
        return response


def _get_actor():
    return TimerCounter()


def init(payload):
    return _get_actor().init(payload)


def start(payload):
    return _get_actor().start(payload)


def on_timer(payload):
    return _get_actor().on_timer(payload)


def get_state(payload):
    return _get_actor().get_state(payload)
