import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


DEFAULT_SOURCE_URL = "https://example.com/price.json"


@actor
class OracleCache:
    """Refresh-once, read-many cache example using the runner FSM."""

    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Async Cache Refresh"
        self.storage["__description__"] = "Caching pattern for oracle reads"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["owner"] = obj.get("owner", "")
        self.storage["source_url"] = str(obj.get("source_url", DEFAULT_SOURCE_URL))
        self.storage["status"] = "idle"
        self.storage["cache"] = {}
        self.storage["refresh_count"] = 0
        return codec.encode({
            "status": "initialized",
            "source_url": self.storage["source_url"],
        })

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def request_refresh(self, payload):
        refresh_count = int(self.storage.get("refresh_count", 0)) + 1
        self.storage["refresh_count"] = refresh_count
        self.storage["status"] = "pending"
        request = {
            "refresh_count": refresh_count,
            "source_url": self.storage.get("source_url", DEFAULT_SOURCE_URL),
        }
        return self._refresh_cache(codec.encode(request))

    @runner.continuation
    async def _refresh_cache(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.http(
            request.get("source_url", DEFAULT_SOURCE_URL),
            method="GET",
            timeout_ms=5000,
        )
        return self._finalize_refresh(ctx.request, ctx.result)

    def _finalize_refresh(self, payload, result):
        request = payload or {}
        normalized = result.raw
        if not isinstance(normalized, dict):
            raise ValueError("oracle cache: runner.http did not return a JSON object")
        record = {
            "symbol": str(normalized.get("symbol", "UNKNOWN")).upper(),
            "price": normalized.get("price", normalized.get("value", 0)),
            "as_of": normalized.get("as_of", normalized.get("timestamp", "unknown")),
        }
        self.storage["cache"] = record
        self.storage["status"] = "ready"
        response = {
            "status": "ready",
            "refresh_count": int(request.get("refresh_count", 0)),
            "record": record,
        }
        runtime.emit_event("oracle_cache.refreshed", response)
        return codec.encode(response)

    @pure
    @public(decode="cbor")
    def get_cached_record(self, payload):
        cache = self.storage.get("cache", {})
        if not cache:
            response = {"status": "cache_miss"}
            runtime.emit_event("get_cached_record", response)
            return response
        response = {"status": "cache_hit", "record": cache}
        runtime.emit_event("get_cached_record", response)
        return response

    @pure
    @public(decode="cbor")
    def get_status(self, payload):
        response = {
            "status": self.storage.get("status", "idle"),
            "refresh_count": int(self.storage.get("refresh_count", 0)),
        }
        runtime.emit_event("get_status", response)
        return response


def _get_actor():
    return OracleCache()


def init(payload):
    return _get_actor().init(payload)


def request_refresh(payload):
    return _get_actor().request_refresh(payload)


def _refresh_cache__resume(payload):
    return _get_actor()._refresh_cache__resume(payload)


def get_cached_record(payload):
    return _get_actor().get_cached_record(payload)


def get_status(payload):
    return _get_actor().get_status(payload)
