from cowboy_sdk import OWNER, SELF, Address, actor, callable_by, codec, deferred, public, runtime


DEFAULT_MIN_PRICE = 0
DEFAULT_MAX_PRICE = 10000
DEFAULT_AUTO_INTERVAL = 0


def _next_price(min_price, max_price, block_height, update_count):
    span = max_price - min_price + 1
    if span <= 0:
        return min_price
    seed = ((block_height + 1) * 1103515245 + (update_count + 1) * 12345) & 0x7FFFFFFF
    return min_price + (seed % span)


@actor
class MockPriceFeed:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        self.storage["owner"] = self.sender.to_hex(checksum=False)
        self.storage["min_price"] = DEFAULT_MIN_PRICE
        self.storage["max_price"] = DEFAULT_MAX_PRICE
        self.storage["current_price"] = DEFAULT_MIN_PRICE
        self.storage["last_updated_height"] = 0
        self.storage["update_count"] = 0
        self.storage["auto_interval_blocks"] = DEFAULT_AUTO_INTERVAL
        runtime.emit_event("feed.init", {"owner": self.storage["owner"]})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def configure(self, payload):
        data = payload or {}
        min_price = int(data.get("min_price", self.storage.get("min_price", DEFAULT_MIN_PRICE)))
        max_price = int(data.get("max_price", self.storage.get("max_price", DEFAULT_MAX_PRICE)))
        interval_blocks = int(data.get("auto_interval_blocks", self.storage.get("auto_interval_blocks", DEFAULT_AUTO_INTERVAL)))

        if min_price >= max_price:
            runtime.emit_event("error", {"reason": "invalid_bounds"})
            return b"bad_bounds"

        self.storage["min_price"] = min_price
        self.storage["max_price"] = max_price
        self.storage["auto_interval_blocks"] = max(0, interval_blocks)
        runtime.emit_event(
            "feed.configured",
            {
                "min_price": min_price,
                "max_price": max_price,
                "auto_interval_blocks": self.storage["auto_interval_blocks"],
            },
        )
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def set_bounds(self, payload):
        data = payload or {}
        return self.configure(
            codec.encode(
                {
                    "min_price": data.get("min", data.get("min_price", DEFAULT_MIN_PRICE)),
                    "max_price": data.get("max", data.get("max_price", DEFAULT_MAX_PRICE)),
                    "auto_interval_blocks": self.storage.get("auto_interval_blocks", DEFAULT_AUTO_INTERVAL),
                }
            )
        )

    @public(decode="cbor")
    def generate_price(self, payload):
        height = runtime.get_block_height()
        update_count = int(self.storage.get("update_count", 0))
        min_price = int(self.storage.get("min_price", DEFAULT_MIN_PRICE))
        max_price = int(self.storage.get("max_price", DEFAULT_MAX_PRICE))
        price = _next_price(min_price, max_price, height, update_count)

        self.storage["current_price"] = price
        self.storage["last_updated_height"] = height
        self.storage["update_count"] = update_count + 1

        runtime.emit_event(
            "feed.price_generated",
            {
                "price": price,
                "height": height,
                "update_count": self.storage["update_count"],
            },
        )
        return {
            "price": price,
            "height": height,
            "update_count": self.storage["update_count"],
        }

    @public(decode="raw")
    def update_price(self, payload):
        return self.generate_price(payload)

    @public(decode="cbor")
    @deferred
    def handle_message(self, payload):
        data = payload or {}
        cmd = data.get("cmd", "")
        if cmd == "push_price":
            return self.push_price(codec.encode(data))
        runtime.emit_event("error", {"reason": "unknown_message", "cmd": cmd})
        return b"unknown_cmd"

    @public(decode="cbor")
    @deferred
    def push_price(self, payload):
        data = payload or {}
        casino_actor_raw = data.get("casino_actor", "")
        casino_actor = Address(casino_actor_raw).to_hex(checksum=False) if casino_actor_raw else ""
        round_id = int(data.get("round_id", 0))
        if not casino_actor or round_id <= 0:
            runtime.emit_event("error", {"reason": "bad_push_request"})
            return b"bad_request"

        result = codec.decode(self.generate_price(codec.encode({})))
        runtime.send_message(
            Address(casino_actor).to_bytes(),
            codec.encode(
                {
                    "cmd": "settle_round",
                    "round_id": round_id,
                    "price": result["price"],
                    "price_height": result["height"],
                }
            ),
        )
        runtime.emit_event("feed.price_pushed", {"casino_actor": casino_actor, "round_id": round_id, "price": result["price"]})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    @deferred
    def start_auto(self, payload):
        interval_blocks = int((payload or {}).get("interval_blocks", self.storage.get("auto_interval_blocks", 0)))
        if interval_blocks <= 0:
            runtime.emit_event("error", {"reason": "bad_interval"})
            return b"bad_interval"
        self.storage["auto_interval_blocks"] = interval_blocks
        runtime.schedule_after(interval_blocks, "tick")
        runtime.emit_event("feed.auto_started", {"interval_blocks": interval_blocks})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def stop_auto(self, payload):
        self.storage["auto_interval_blocks"] = 0
        runtime.emit_event("feed.auto_stopped", {})
        return b"ok"

    @callable_by(SELF, decode="raw")
    @deferred
    def tick(self, payload):
        if int(self.storage.get("auto_interval_blocks", 0)) <= 0:
            return b"stopped"
        self.generate_price(codec.encode({}))
        runtime.schedule_after(int(self.storage.get("auto_interval_blocks", 0)), "tick")
        return b"ok"

    @public(decode="cbor")
    def get_price(self, payload):
        return {
            "price": int(self.storage.get("current_price", DEFAULT_MIN_PRICE)),
            "min_price": int(self.storage.get("min_price", DEFAULT_MIN_PRICE)),
            "max_price": int(self.storage.get("max_price", DEFAULT_MAX_PRICE)),
            "last_updated_height": int(self.storage.get("last_updated_height", 0)),
            "update_count": int(self.storage.get("update_count", 0)),
            "auto_interval_blocks": int(self.storage.get("auto_interval_blocks", 0)),
        }


def _get_actor():
    return MockPriceFeed()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def set_bounds(payload):
    return _get_actor().set_bounds(payload)


def generate_price(payload):
    return _get_actor().generate_price(payload)


def update_price(payload):
    return _get_actor().update_price(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def push_price(payload):
    return _get_actor().push_price(payload)


def start_auto(payload):
    return _get_actor().start_auto(payload)


def stop_auto(payload):
    return _get_actor().stop_auto(payload)


def tick(payload):
    return _get_actor().tick(payload)


def get_price(payload):
    return _get_actor().get_price(payload)
