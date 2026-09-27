"""Jina Reader + Search Store wrapper with caller-scoped asynchronous results.

Every handler returns an acceptance record with a request_id; the registered
hirer reads the result from get_result. Vendor data is never returned by the
initial dispatch, so the Runner callback that arrives after the buyer's
transaction cannot be lost or overwritten by another caller.
"""
import json
from cowboy_sdk import (
    Address,
    CodecError,
    ContinuationNotFoundError,
    actor,
    capture,
    codec,
    continuation,
    deferred,
    public,
    runner,
    runtime,
)

VENDOR = "jina"
ACTOR_VERSION = "2.0.0"
READ_ENDPOINT = "https://r.jina.ai"
SEARCH_ENDPOINT = "https://s.jina.ai"
EMBEDDINGS_ENDPOINT = "https://api.jina.ai/v1/embeddings"
RERANK_ENDPOINT = "https://api.jina.ai/v1/rerank"
CLASSIFY_ENDPOINT = "https://api.jina.ai/v1/classify"
TRAIN_ENDPOINT = "https://api.jina.ai/v1/train"
SEGMENT_ENDPOINT = "https://api.jina.ai/v1/segment"
DEEPSEARCH_ENDPOINT = "https://deepsearch.jina.ai/v1/chat/completions"
API_ROOT = "https://api.jina.ai"
# Fixed paths prevent SSRF.
CATALOG_OPERATIONS = {
    "list_classifiers": ("GET", "/v1/classifiers", ""),
    "list_classifiers_post": ("POST", "/v1/classifiers", ""),
    "delete_classifier": ("DELETE", "/v1/classifiers/", "classifier_id"),
    "list_models": ("GET", "/v1/models", ""),
    "get_model": ("GET", "/v1/models/", "model_id"),
    "chat_completions": ("POST", "/v1/chat/completions", ""),
    "create_batch_embeddings": ("POST", "/v1/batch/embeddings", ""),
    "get_batch": ("GET", "/v1/batch/", "batch_id"),
    "cancel_batch": ("DELETE", "/v1/batch/", "batch_id"),
    "list_batches": ("GET", "/v1/batches", ""),
    "batch_output": ("GET", "/v1/batch/{batch_id}/output", "batch_id"),
    "batch_errors": ("GET", "/v1/batch/{batch_id}/errors", "batch_id"),
}
OPENAPI_COVERAGE = {
    "POST /v1/embeddings": "embeddings", "POST /v1/rerank": "rerank",
    "POST /v1/classify": "classify", "POST /v1/train": "train_classifier",
    "GET /v1/classifiers": "catalog:list_classifiers",
    "POST /v1/classifiers": "catalog:list_classifiers_post",
    "DELETE /v1/classifiers/{classifier_id}": "catalog:delete_classifier",
    "GET /v1/models": "catalog:list_models",
    "GET /v1/models/{model_id}": "catalog:get_model",
    "POST /v1/chat/completions": "catalog:chat_completions",
    "POST /v1/batch/embeddings": "catalog:create_batch_embeddings",
    "GET /v1/batch/{batch_id}": "catalog:get_batch",
    "DELETE /v1/batch/{batch_id}": "catalog:cancel_batch",
    "GET /v1/batches": "catalog:list_batches",
    "GET /v1/batch/{batch_id}/output": "catalog:batch_output",
    "GET /v1/batch/{batch_id}/errors": "catalog:batch_errors",
}
OPENAPI_EXCLUSIONS = {
    "GET /health": "vendor infrastructure probe", "GET /ready": "vendor infrastructure probe",
    "GET /live": "vendor infrastructure probe",
}
SECRET_KEY = "JINA_API_KEY"
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer "

# Bound the final encoded response, not only the nested vendor data. Actor-side
# limiting cannot shrink a response that never reaches the Runner callback.
RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976
MAX_OUTPUT_CHARS = 28_000
MAX_RESULT_CHUNK_BYTES = 3_900  # Leaves framing space under the 4096-byte event cap.
MAX_REQUEST_BYTES = 28_000
MAX_RETAINED_REQUESTS = 16
MAX_REGISTERED_HIRES = 512
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150
BODY_METHODS = ("POST", "PATCH", "PUT")
SHAPED_OPERATIONS = ("read", "search")

ENGINES = ("", "direct", "cf-browser-rendering")
RESPOND_WITH = ("", "readerlm-v2", "jina-vlm", "no-content")
RETURN_FORMATS = ("", "markdown", "html", "text")
KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_CALLS = "call_count"
KEY_REQUEST_IDS = "request_ids"
KEY_LAST_STATUS = "last_status"
KEY_LAST_RESULT = "last_result"
KEY_LAST_OUTPUT = "last_output"


def _parse(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if not payload:
            return {}
        try:
            value = codec.decode(bytes(payload))
            if isinstance(value, dict):
                return value
        except CodecError:
            pass
        try:
            value = json.loads(bytes(payload).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _decode(value, depth=0):
    if value is None or depth > 4:
        return {}
    if not isinstance(value, (bytes, bytearray, str, dict, list)):
        if hasattr(value, "data"):
            return _decode(value.data, depth + 1)
        if hasattr(value, "body"):
            return _decode(value.body, depth + 1)
        if hasattr(value, "result"):
            return _decode(value.result, depth + 1)
        if hasattr(value, "value"):
            return _decode(value.value, depth + 1)
        if hasattr(value, "text"):
            return _decode(value.text, depth + 1)
        return {"raw_text": str(value)}
    if isinstance(value, (bytes, bytearray)):
        try:
            return _decode(bytes(value).decode("utf-8"), depth + 1)
        except Exception:
            return {"raw_text": ""}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {"raw_text": value}
        return _decode(parsed, depth + 1) if isinstance(parsed, (dict, list)) else {"value": parsed}
    if isinstance(value, list):
        return {"data": value}
    return value


def _error(value):
    if not isinstance(value, dict):
        return "vendor request failed"
    if set(value) == {"raw", "content_type"}:
        return "vendor returned a non-JSON response"
    for key in ("error", "message", "detail", "raw_text"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item[:500]
        if isinstance(item, dict):
            for inner in ("message", "detail", "description"):
                text = item.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    return "vendor request failed"


def _address(value, field):
    try:
        return Address(value).to_hex(checksum=False)
    except Exception as exc:
        raise ValueError("%s must be a 20-byte address" % field) from exc


def _raw_transport(value):
    return isinstance(value, dict) and set(value) == {"raw", "content_type"}


def _integer(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("%s must be an integer" % field)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer" % field) from exc
    if value < low or value > high:
        raise ValueError("%s is out of range" % field)
    return value


def _declaration(value):
    text = str(value or "").lower()
    try:
        raw = bytes.fromhex(text[2:] if text.startswith("0x") else text)
    except ValueError as exc:
        raise ValueError("declaration_id must be 32-byte hex") from exc
    if len(raw) != 32:
        raise ValueError("declaration_id must be 32-byte hex")
    return "0x" + raw.hex()


def _text(value, field, maximum, default=""):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    value = value.strip()
    if len(value) > maximum or "\r" in value or "\n" in value:
        raise ValueError("%s is invalid or too long" % field)
    return value


def _boolean(value, field):
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % field)
    return value


def _enum(value, field, choices):
    value = _text(value, field, 100).lower()
    if value not in choices:
        raise ValueError("%s must be one of %s" % (field, ", ".join(choices)))
    return value


def _encode(value, preserve_url=False):
    safe = (b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?&=+@,;$%"
            if preserve_url else b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    out = []
    for byte in value.encode("utf-8"):
        out.append(chr(byte) if byte in safe else "%%%02X" % byte)
    return "".join(out)


def _target(value):
    value = _text(value, "url", 4_000)
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    authority = value.split("//", 1)[1].split("/", 1)[0]
    if not authority or "@" in authority:
        raise ValueError("url must not contain credentials")
    return value


def _reject_secret_templates(req):
    # Caller-supplied templates are never needed. Refuse them before the request
    # is serialized as raw JSON or sent beside the secret-backed auth header.
    if "${" in json.dumps(req):
        raise ValueError("secret templates are not allowed in caller input")


def _api_body(req, operation):
    body = dict(req)
    body.pop("hire_id", None)
    if operation == "deepsearch":
        body["stream"] = False
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("%s request exceeds %d bytes" % (operation, MAX_REQUEST_BYTES))
    return encoded


def _catalog_body(req, operation):
    body = dict(req)
    for key in ("hire_id", "operation", "classifier_id", "model_id", "batch_id"):
        body.pop(key, None)
    return _api_body(body, operation)


def _api_headers():
    return {"Accept": "application/json", "Content-Type": "application/json",
            AUTH_HEADER: AUTH_SCHEME + "${%s}" % SECRET_KEY}


def _path_component(value, field):
    value = _text(value, field, 300)
    if not value:
        raise ValueError("%s is required" % field)
    return _encode(value)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _truncate_text_bytes(text, byte_limit):
    raw = text.encode("utf-8")
    if len(raw) <= byte_limit:
        return text
    return raw[: max(0, byte_limit)].decode("utf-8", "ignore")


def _limited_response(value, budget):
    """Return exact vendor JSON when it fits the Actor response budget."""
    try:
        out = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return None
    return out if len(_json_bytes(out)) <= budget else None


@actor
class JinaReaderWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Jina Reader"
        self.storage["__description__"] = "Read URLs, search, and extract structured web content."
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        runtime.emit_event("wrapper.init", {"vendor": VENDOR, "owner": owner, "store_registrar": registrar})
        return codec.encode({"ok": True, "vendor": VENDOR})

    @public
    def register_hire(self, payload):
        if _address(runtime.get_sender(), "sender") != self.storage.get(KEY_REGISTRAR):
            raise ValueError("register_hire: caller is not the Store registrar")
        req = _parse(payload)
        hire_id = _integer(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        record = {"hire_id": hire_id, "hirer": _address(req.get("hirer"), "hirer"),
                  "declaration_id": _declaration(req.get("declaration_id")),
                  "version": _integer(req.get("version"), "version", 1, 2**32 - 1),
                  "artifact_id": _integer(req.get("artifact_id"), "artifact_id", 0, 2**32 - 1)}
        key = "hire:" + str(hire_id)
        existing = self.storage.get(key)
        if existing:
            if existing != record:
                raise ValueError("register_hire: hire_id already has different data")
            return codec.encode({"ok": True, "hire_id": hire_id, "hirer": record["hirer"]})
        count = self.storage[KEY_HIRE_COUNT]
        if count >= MAX_REGISTERED_HIRES:
            raise ValueError("register_hire: registered Hire capacity is full")
        self.storage[key] = record
        self.storage[KEY_HIRE_COUNT] = count + 1
        runtime.emit_event("wrapper.hire_registered", {"hire_id": hire_id, "hirer": record["hirer"]})
        return codec.encode({"ok": True, "hire_id": hire_id, "hirer": record["hirer"]})

    def _hire(self, req, operation):
        hire_id = _integer(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hire = self.storage.get("hire:" + str(hire_id)) or {}
        if not hire:
            return hire_id, {}, codec.encode({"ok": False, "error": "hire %d is not activated" % hire_id,
                                              "hint": "Store registrar must call register_hire"})
        if _address(runtime.get_sender(), "sender") != hire["hirer"]:
            raise ValueError("%s: caller is not the registered hirer" % operation)
        return hire_id, hire, None

    def _headers(self, req):
        headers = {"Accept": "application/json", AUTH_HEADER: AUTH_SCHEME + "${%s}" % SECRET_KEY}
        engine = _enum(req.get("engine"), "engine", ENGINES)
        respond = _enum(req.get("respond_with"), "respond_with", RESPOND_WITH)
        return_format = _enum(req.get("return_format"), "return_format", RETURN_FORMATS)
        if engine:
            headers["X-Engine"] = engine
        if respond:
            headers["X-Respond-With"] = respond
        if return_format:
            headers["X-Return-Format"] = return_format
        if req.get("timeout_seconds") is not None:
            headers["X-Timeout"] = str(_integer(req.get("timeout_seconds"), "timeout_seconds", 1, 20))
        if req.get("token_budget") is not None:
            headers["X-Token-Budget"] = str(_integer(req.get("token_budget"), "token_budget", 100, 20_000))
        for field, name, maximum in (
            ("target_selector", "X-Target-Selector", 500), ("remove_selector", "X-Remove-Selector", 500),
            ("wait_for_selector", "X-Wait-For-Selector", 500),
            ("user_agent", "X-User-Agent", 500), ("referer", "X-Referer", 2_000),
            ("locale", "X-Locale", 50), ("markdown_hr", "X-Md-Hr", 20),
            ("markdown_bullet", "X-Md-Bullet-List-Marker", 5),
            ("markdown_heading", "X-Md-Heading-Style", 20),
            ("markdown_emphasis", "X-Md-Em-Delimiter", 5),
            ("markdown_strong", "X-Md-Strong-Delimiter", 5),
            ("markdown_link", "X-Md-Link-Style", 20),
            ("respond_timing", "X-Respond-Timing", 50),
            ("robots_txt", "X-Robots-Txt", 100)):
            value = _text(req.get(field), field, maximum)
            if value:
                headers[name] = value
        for field, name in (("keep_data_images", "X-Keep-Img-Data-Url"),
                            ("do_not_track", "DNT"), ("disable_gfm", "X-No-Gfm"),
                            ("use_final_url", "X-Base"), ("no_cache", "X-No-Cache"),
                            ("caption_images", "X-With-Generated-Alt"),
                            ("openai_citations", "X-Retain-Links"),
                            ("links_summary", "X-With-Links-Summary"),
                            ("images_summary", "X-With-Images-Summary"),
                            ("include_iframes", "X-With-Iframe"),
                            ("include_shadow_dom", "X-With-Shadow-Dom")):
            if _boolean(req.get(field), field):
                headers[name] = "1"
        if _boolean(req.get("remove_images"), "remove_images"):
            headers["X-Retain-Images"] = "none"
        if req.get("cache_tolerance_seconds") is not None:
            headers["X-Cache-Tolerance"] = str(_integer(
                req.get("cache_tolerance_seconds"), "cache_tolerance_seconds", 0, 86_400))
        instruction = _text(req.get("instruction"), "instruction", 2_000)
        schema = req.get("json_schema")
        if instruction or schema is not None:
            headers["X-Respond-With"] = "readerlm-v2"
        if instruction:
            headers["X-Instruction"] = instruction
        if schema is not None:
            if not isinstance(schema, dict):
                raise ValueError("json_schema must be an object")
            serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            if len(serialized) > 8_000:
                raise ValueError("json_schema exceeds 8000 characters")
            headers["X-Json-Schema"] = serialized
        return headers

    def _pins(self, hire_id, hire):
        return {"hire_id": hire_id, "declaration_id": hire["declaration_id"],
                "version": hire["version"], "artifact_id": hire["artifact_id"]}

    # --- request handlers: each builds a job and returns an acceptance record --

    @public
    @deferred
    def read(self, payload):
        req = _parse(payload)
        _reject_secret_templates(req)
        hire_id, hire, refusal = self._hire(req, "read")
        if refusal is not None:
            return refusal
        target = _target(req.get("url"))
        return self._accept(hire_id, hire, "read", READ_ENDPOINT + "/" + _encode(target, True),
                            "GET", self._headers(req), None, source_url=target)

    @public
    @deferred
    def search(self, payload):
        req = _parse(payload)
        _reject_secret_templates(req)
        hire_id, hire, refusal = self._hire(req, "search")
        if refusal is not None:
            return refusal
        query = _text(req.get("query"), "query", 400)
        if not query:
            return codec.encode({"ok": False, "error": "query is required"})
        count = _integer(req.get("count", 5), "count", 1, 20)
        include_content = _boolean(req.get("include_content"), "include_content")
        headers = self._headers(req)
        if include_content:
            count = 1
            headers["X-Max-Tokens"] = str(_integer(
                req.get("max_content_tokens", 2_000), "max_content_tokens", 100, 4_000))
        else:
            headers["X-Respond-With"] = "no-content"
        url = SEARCH_ENDPOINT + "/?q=" + _encode(query) + "&count=" + str(count)
        return self._accept(hire_id, hire, "search", url, "GET", headers, None)

    def _accept_post(self, req, operation, endpoint):
        _reject_secret_templates(req)
        hire_id, hire, refusal = self._hire(req, operation)
        if refusal is not None:
            return refusal
        return self._accept(hire_id, hire, operation, endpoint, "POST", _api_headers(),
                            _api_body(req, operation))

    @public
    @deferred
    def embeddings(self, payload):
        return self._accept_post(_parse(payload), "embeddings", EMBEDDINGS_ENDPOINT)

    @public
    @deferred
    def rerank(self, payload):
        return self._accept_post(_parse(payload), "rerank", RERANK_ENDPOINT)

    @public
    @deferred
    def classify(self, payload):
        return self._accept_post(_parse(payload), "classify", CLASSIFY_ENDPOINT)

    @public
    @deferred
    def train_classifier(self, payload):
        return self._accept_post(_parse(payload), "train_classifier", TRAIN_ENDPOINT)

    @public
    @deferred
    def segment(self, payload):
        return self._accept_post(_parse(payload), "segment", SEGMENT_ENDPOINT)

    @public
    @deferred
    def deepsearch(self, payload):
        return self._accept_post(_parse(payload), "deepsearch", DEEPSEARCH_ENDPOINT)

    @public
    @deferred
    def read_post(self, payload):
        return self._accept_post(_parse(payload), "read_post", READ_ENDPOINT)

    @public
    @deferred
    def search_post(self, payload):
        return self._accept_post(_parse(payload), "search_post", SEARCH_ENDPOINT)

    @public
    @deferred
    def catalog(self, payload):
        req = _parse(payload)
        _reject_secret_templates(req)
        operation = _text(req.get("operation"), "operation", 100)
        spec = CATALOG_OPERATIONS.get(operation)
        if spec is None:
            raise ValueError("catalog operation is not supported")
        hire_id, hire, refusal = self._hire(req, operation)
        if refusal is not None:
            return refusal
        method, path, id_field = spec
        if "{" in path:
            path = path.replace("{" + id_field + "}", _path_component(req.get(id_field), id_field))
        elif id_field:
            path += _path_component(req.get(id_field), id_field)
        if operation == "list_batches" and req.get("limit") is not None:
            path += "?limit=" + str(_integer(req.get("limit"), "limit", 1, 100))
        body = _catalog_body(req, operation) if method == "POST" else None
        return self._accept(hire_id, hire, operation, API_ROOT + path, method, _api_headers(), body)

    def _accept(self, hire_id, hire, operation, url, method, headers, body, source_url=None):
        buyer = _address(runtime.get_sender(), "sender")
        request_bytes = (body or "").encode("utf-8") if isinstance(body, str) else b""
        if len(request_bytes) + len(url.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds %d bytes" % MAX_REQUEST_BYTES)
        self._cleanup()
        ids = self.storage[KEY_REQUEST_IDS]
        cid = continuation.new_cid(self, "_run_request")
        if any(self.storage["request:" + str(i)].get("cid") == cid for i in ids):
            return codec.encode({"ok": False, "error": "transaction already has a pending request"})
        if len(ids) >= MAX_RETAINED_REQUESTS:
            return codec.encode({"ok": False, "error": "request capacity is full; retry after results expire"})
        request_id = _integer(self.storage[KEY_CALLS] + 1, "request_id", 1, 2**64 - 1)
        # No runner.http timeout_ms: the Actor HTTP timeout setting is ignored by
        # the Runner; job selection windows and retries bound the call instead.
        job = {
            "url": url,
            "method": method,
            "headers": dict(headers),
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        if method in BODY_METHODS and body is not None:
            job["headers"]["Content-Type"] = "application/json"
            job["raw_body"] = body
        record = {
            "request_id": request_id, "hire_id": hire_id, "buyer": buyer,
            "operation": operation, "shape": operation in SHAPED_OPERATIONS,
            "status": "pending", "cid": cid,
            "deadline_height": runtime.get_block_height() + REQUEST_LIFETIME_BLOCKS,
        }
        if source_url:
            record["source_url"] = _truncate_text_bytes(source_url, 1024)
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {"ok": True, "request_id": request_id, "hire_id": hire_id,
                    "status": "pending", "poll_handler": "get_result"}
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        ctx = capture()
        ctx.request_id = req["request_id"]
        result = await runner.http(**req["job"])
        return self._finish(ctx.request_id, result)

    def _finish(self, request_id, result):
        key = "request:" + str(request_id)
        record = self.storage.get(key)
        if not record or record["status"] != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        if runtime.get_block_height() >= record["deadline_height"]:
            result = {"error": "request result did not arrive before its deadline"}
        decoded = _decode(result)
        operation = record["operation"]
        if record["shape"]:
            data = decoded.get("data") if isinstance(decoded, dict) else None
            failed = data is None or (isinstance(decoded, dict) and any(
                k in decoded for k in ("error", "detail", "raw_text"))) or _raw_transport(decoded)
            payload_data = self._shape(data) if not failed else None
        else:
            failed = (not isinstance(decoded, dict)) or any(
                k in decoded for k in ("error", "detail", "raw_text")) or _raw_transport(decoded)
            payload_data = decoded if not failed else None
        if failed:
            output = {"ok": False, "operation": operation,
                      "error": _truncate_text_bytes(_error(decoded), 512)}
        else:
            data = _limited_response(payload_data, API_BYTE_BUDGET)
            if data is None:
                output = {"ok": False, "operation": operation,
                          "error": "vendor result exceeds the encoded result byte budget"}
            else:
                output = {"ok": True, "operation": operation, "data": data}
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        record["expires_height"] = runtime.get_block_height() + RESULT_RETENTION_BLOCKS
        if len(codec.encode(self._response(record))) > RETURN_BYTE_BUDGET:
            output = {"ok": False, "operation": operation,
                      "error": "vendor result exceeds the encoded result byte budget"}
            record["status"] = "error"
            record["output"] = output
        record.pop("cid", None)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        self.storage[KEY_LAST_OUTPUT] = {"operation": operation, "ok": output["ok"]}
        first = None
        if output["ok"]:
            data = output.get("data")
            first = data[0] if isinstance(data, list) and data else data
        self.storage[KEY_LAST_RESULT] = {
            "operation": operation,
            "title": _truncate_text_bytes(str(first.get("title", "")), 120) if isinstance(first, dict) else "",
            "url": _truncate_text_bytes(str(first.get("url", record.get("source_url", ""))), 500)
            if isinstance(first, dict) else "",
        }
        runtime.emit_event("wrapper.request_finished",
                           {"request_id": request_id, "hire_id": record["hire_id"],
                            "status": record["status"]})
        return codec.encode(self._response(record))

    def _shape(self, data):
        items = data if isinstance(data, list) else [data]
        output = []
        remaining = MAX_OUTPUT_CHARS
        for item in items[:10]:
            if not isinstance(item, dict):
                item = {"content": str(item)}
            fixed = {"title": str(item.get("title", ""))[:300],
                     "description": str(item.get("description", ""))[:1_000],
                     "url": str(item.get("url", ""))[:2_000],
                     "published": str(item.get("publishedTime", item.get("published", "")))[:100]}
            content = item.get("content", item.get("text", item.get("markdown", "")))
            if isinstance(content, (dict, list)):
                content = json.dumps(content, sort_keys=True, separators=(",", ":"))
            fixed["content"] = str(content)[:max(0, min(8_000, remaining - 3_800))]
            output.append(fixed)
            remaining -= len(json.dumps(fixed, sort_keys=True))
            if remaining <= 500:
                break
        return output if isinstance(data, list) else output[0]

    def _response(self, record):
        response = {k: record[k] for k in ("request_id", "hire_id", "operation", "status")}
        response["ok"] = record["status"] != "error"
        if record["status"] == "pending":
            response["deadline_height"] = record["deadline_height"]
        else:
            response["output"] = record["output"]
            response["expires_height"] = record["expires_height"]
        return response

    def _cleanup(self, only=None):
        now = runtime.get_block_height()
        retained = []
        ids = self.storage.get(KEY_REQUEST_IDS, [])
        for request_id in ids:
            if only is not None and request_id != only:
                retained.append(request_id)
                continue
            key = "request:" + str(request_id)
            record = self.storage[key]
            if record["status"] == "pending" and now >= record["deadline_height"]:
                continuation.delete_cont(record["cid"])
                self._finish(request_id, {"error": "request result did not arrive before its deadline"})
                record = self.storage[key]
            if record["status"] != "pending" and now >= record["expires_height"]:
                del self.storage[key]
            else:
                retained.append(request_id)
        if retained != ids:
            self.storage[KEY_REQUEST_IDS] = retained

    @public
    @deferred
    def get_result(self, payload):
        """Return this caller's result; optional receipt chunks support signed CLI calls."""
        req = _parse(payload)
        request_id = _integer(req.get("request_id"), "request_id", 1, 2**64 - 1)
        hire_id = _integer(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        self._cleanup(request_id)
        record = self.storage.get("request:" + str(request_id))
        if (not record
                or record["buyer"] != _address(runtime.get_sender(), "sender")
                or record["hire_id"] != hire_id):
            return codec.encode({"ok": False, "error": "request not found for caller and hire"})
        encoded = codec.encode(self._response(record))
        if "receipt_offset" in req:
            offset = _integer(req["receipt_offset"], "receipt_offset", 0, len(encoded) - 1)
            runtime.emit_event("wrapper.result_chunk", {
                "request_id": request_id, "hire_id": hire_id, "offset": offset,
                "total": len(encoded), "data": encoded[offset: offset + MAX_RESULT_CHUNK_BYTES]})
        return encoded

    @public
    def capability(self, payload):
        return codec.encode({"vendor": VENDOR, "slot": "scrape/read/search",
            "handlers": ["read", "search", "read_post", "search_post", "embeddings",
                "rerank", "classify", "train_classifier", "segment", "deepsearch", "catalog"],
            "poll_handler": "get_result",
            "returns": "jina-async-result",
            "features": ["browser_render", "direct_fetch",
                "markdown", "image_captioning", "css_selectors", "structured_extraction",
                "wait_selectors", "token_budget", "cache_control", "robots_txt", "iframes",
                "shadow_dom", "citation_format", "link_summaries", "readerlm-v2", "jina-vlm",
                "locale", "privacy", "web_search", "pdf_urls", "raw_html", "embeddings",
                "multimodal_embeddings", "reranking", "multimodal_reranking",
                "zero_shot_classification", "few_shot_classification", "classifier_training",
                "segmentation", "deep_research"],
            "read_input": {"hire_id": "int", "url": "str", "engine": "str?",
                "return_format": "markdown|html|text?", "respond_with": "readerlm-v2|jina-vlm?",
                "target_selector": "str?", "wait_for_selector": "str?", "remove_selector": "str?",
                "instruction": "str?", "json_schema": "object?", "timeout_seconds": "int?",
                "token_budget": "int?", "locale": "str?", "user_agent": "str?", "referer": "str?",
                "caption_images": "bool?", "remove_images": "bool?", "openai_citations": "bool?",
                "links_summary": "bool?", "images_summary": "bool?", "include_iframes": "bool?",
                "include_shadow_dom": "bool?", "no_cache": "bool?", "do_not_track": "bool?"},
            "search_input": {"hire_id": "int", "query": "str", "token_budget": "int?",
                "locale": "str?", "do_not_track": "bool?"},
            "output": "acceptance record; get_result returns the request status and shaped output",
            "api_json_input": "Jina's documented JSON request body plus hire_id; Store metadata is stripped",
            "catalog_operations": sorted(CATALOG_OPERATIONS.keys()),
            "excluded": ["multipart_file_upload", "custom_javascript", "sse_streaming",
                         "cookie_forwarding", "custom_proxy", "browser_viewport"],
            "hire_activation": {"protocol": "store-admin-v1"},
            "result_byte_budget": RETURN_BYTE_BUDGET})

    @public
    def get_last_result(self, payload):
        return codec.encode({"status": self.storage.get(KEY_LAST_STATUS, "none"),
                             "result": self.storage.get(KEY_LAST_RESULT, {})})

    @public
    def get_last_output(self, payload):
        return codec.encode(self.storage.get(KEY_LAST_OUTPUT, {}))

    @public
    def get_config(self, payload):
        return codec.encode({"vendor": VENDOR, "call_count": self.storage.get(KEY_CALLS, 0),
                             "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                             "last_status": self.storage.get(KEY_LAST_STATUS)})


def _get_actor():
    return JinaReaderWrapper()


def init(payload): return _get_actor().init(payload)
def register_hire(payload): return _get_actor().register_hire(payload)
def read(payload): return _get_actor().read(payload)
def search(payload): return _get_actor().search(payload)
def embeddings(payload): return _get_actor().embeddings(payload)
def rerank(payload): return _get_actor().rerank(payload)
def classify(payload): return _get_actor().classify(payload)
def train_classifier(payload): return _get_actor().train_classifier(payload)
def segment(payload): return _get_actor().segment(payload)
def deepsearch(payload): return _get_actor().deepsearch(payload)
def read_post(payload): return _get_actor().read_post(payload)
def search_post(payload): return _get_actor().search_post(payload)
def catalog(payload): return _get_actor().catalog(payload)
def get_result(payload): return _get_actor().get_result(payload)
def capability(payload): return _get_actor().capability(payload)
def get_last_result(payload): return _get_actor().get_last_result(payload)
def get_last_output(payload): return _get_actor().get_last_output(payload)
def get_config(payload): return _get_actor().get_config(payload)


def _run_request__resume(payload):
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
