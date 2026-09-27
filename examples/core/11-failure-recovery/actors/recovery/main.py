import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


DEFAULT_SOURCE_URL = "https://example.com/health"


@actor
class RecoveryDemo:
    """Async request example with explicit pending, failed, retry, and recovered states."""

    def _request_key(self, request_id):
        return str(int(request_id))

    def _requests(self):
        raw = self.storage.get("requests", {})
        requests = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    requests[self._request_key(key)] = value
                except Exception:
                    requests[str(key)] = value
        return requests

    def _store_requests(self, requests):
        self.storage["requests"] = {str(key): value for key, value in requests.items()}

    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Recovery Demo"
        self.storage["__description__"] = "Error handling and recovery patterns"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["owner"] = obj.get("owner", "")
        self.storage["source_url"] = str(obj.get("source_url", DEFAULT_SOURCE_URL))
        self.storage["next_request_id"] = 1
        self.storage["requests"] = {}
        return codec.encode({"status": "initialized"})

    def _create_request(self, payload, previous_request_id=None, recovered=False):
        requests = self._requests()
        request_id = int(self.storage.get("next_request_id", 1))
        self.storage["next_request_id"] = request_id + 1
        attempt = 1
        if previous_request_id is not None and self._request_key(previous_request_id) in requests:
            attempt = int(requests[self._request_key(previous_request_id)].get("attempt", 1)) + 1
        request = {
            "request_id": request_id,
            "input": payload,
            "status": "pending",
            "attempt": attempt,
            "previous_request_id": previous_request_id,
            "recovered": bool(recovered),
        }
        requests[self._request_key(request_id)] = request
        self._store_requests(requests)
        return request

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def submit_request(self, payload):
        obj = payload or {}
        request = self._create_request(obj)
        return self._run_request(codec.encode(request))

    @runner.continuation
    async def _run_request(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        url = request.get("input", {}).get("source_url", self.storage.get("source_url", DEFAULT_SOURCE_URL))
        ctx.result = await runner.http(url, method="GET", timeout_ms=5000)
        return self._finalize_request(ctx.request, ctx.result)

    def _finalize_request(self, payload, result):
        request = payload or {}
        request_id = int(request.get("request_id", 0))
        requests = self._requests()
        request_key = self._request_key(request_id)
        current = requests.get(request_key)
        if not current:
            return codec.encode({"error": "REQUEST_NOT_FOUND"})
        normalized = result.raw
        if isinstance(normalized, dict):
            current["status"] = "recovered" if current.get("recovered") else "done"
            current["result"] = normalized
            event = "recovery_demo.done"
        else:
            # `failed` is the state retry_request() exists to escape, so
            # recording it keeps the request recoverable; a raise dead-ends it.
            current["status"] = "failed"
            current["result"] = {"error": "NON_OBJECT_RESULT", "text": result.text[:200]}
            event = "recovery_demo.failed"
        requests[request_key] = current
        self._store_requests(requests)
        runtime.emit_event(event, current)
        return codec.encode(current)

    @public(decode="cbor")
    @deferred  # re-dispatching the continuation's runner job is an async side effect
    def retry_request(self, payload):
        obj = payload or {}
        previous_request_id = int(obj.get("request_id", 0))
        requests = self._requests()
        previous = requests.get(self._request_key(previous_request_id))
        if not previous:
            return {"error": "REQUEST_NOT_FOUND"}
        retry = self._create_request(previous.get("input", {}), previous_request_id=previous_request_id, recovered=True)
        self._store_requests(requests | {self._request_key(retry["request_id"]): retry})
        return self._run_request(codec.encode(retry))

    @public(decode="cbor")
    def mark_recovered(self, payload):
        obj = payload or {}
        request_id = int(obj.get("request_id", 0))
        requests = self._requests()
        request_key = self._request_key(request_id)
        current = requests.get(request_key)
        if not current:
            return {"error": "REQUEST_NOT_FOUND"}
        current["status"] = "recovered"
        current["result"] = obj.get("result", {})
        requests[request_key] = current
        self._store_requests(requests)
        return current

    @pure
    @public(decode="cbor")
    def get_request(self, payload):
        obj = payload or {}
        request_id = int(obj.get("request_id", 0))
        requests = self._requests()
        response = requests.get(self._request_key(request_id), {"error": "REQUEST_NOT_FOUND"})
        runtime.emit_event("get_request", response)
        return response

    @pure
    @public(decode="cbor")
    def get_status(self, payload):
        response = {
            "request_count": len(self._requests()),
            "next_request_id": int(self.storage.get("next_request_id", 1)),
        }
        runtime.emit_event("get_status", response)
        return response


def _get_actor():
    return RecoveryDemo()


def init(payload):
    return _get_actor().init(payload)


def submit_request(payload):
    return _get_actor().submit_request(payload)


def _run_request__resume(payload):
    return _get_actor()._run_request__resume(payload)


def retry_request(payload):
    return _get_actor().retry_request(payload)


def mark_recovered(payload):
    return _get_actor().mark_recovered(payload)


def get_request(payload):
    return _get_actor().get_request(payload)


def get_status(payload):
    return _get_actor().get_status(payload)
