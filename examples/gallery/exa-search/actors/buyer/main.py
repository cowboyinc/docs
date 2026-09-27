"""Minimal buyer Actor for the Exa Search wrapper's asynchronous contract."""

from cowboy_sdk import Address, OWNER, actor, call, callable_by, codec, public, runtime


def _parse(payload):
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        value = codec.decode(bytes(payload))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _address_hex(value):
    return Address(value).to_hex(checksum=False)


@actor
class ExaSearchBuyer:
    """Starts one search and later reads the same request as this Actor."""

    @public
    def init(self, payload):
        if self.storage.get("wrapper"):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        runtime.assume_ownership_if_unowned()
        self.storage["wrapper"] = _address_hex(cfg.get("wrapper"))
        self.storage["hire_id"] = int(cfg.get("hire_id", 0))
        if self.storage["hire_id"] < 1:
            raise ValueError("hire_id must be positive")
        return codec.encode({"ok": True})

    @callable_by(OWNER)
    def start(self, payload):
        req = _parse(payload)
        request = {
            "hire_id": self.storage["hire_id"],
            "query": str(req.get("query", "")),
        }
        for key in ("num_results", "chars_per_result"):
            if key in req:
                request[key] = req[key]
        response = call(
            self.storage["wrapper"], "search", request, cycles_limit=2_000_000
        )
        if isinstance(response, dict) and response.get("ok") and response.get("request_id"):
            self.storage["request_id"] = int(response["request_id"])
        return codec.encode(response)

    @callable_by(OWNER)
    def poll(self, payload):
        req = _parse(payload)
        request_id = int(req.get("request_id", self.storage.get("request_id", 0)))
        if request_id < 1:
            raise ValueError("request_id is required")
        response = call(
            self.storage["wrapper"],
            "get_result",
            {"hire_id": self.storage["hire_id"], "request_id": request_id},
            cycles_limit=2_000_000,
        )
        self.storage["last_result"] = response
        return codec.encode(response)


def _get_actor():
    return ExaSearchBuyer()


def init(payload):
    return _get_actor().init(payload)


def start(payload):
    return _get_actor().start(payload)


def poll(payload):
    return _get_actor().poll(payload)
