"""Hire-gated Modal compute-endpoint invocation actor.

Invokes an already-deployed Modal web endpoint via Modal Proxy Auth Tokens.
Async accept/poll: ``invoke`` -> ``runner.http`` continuation -> ``get_result``.
Token pair lives in CBSS, riding the job only as ``${...}`` refs in the
``Modal-Key`` / ``Modal-Secret`` headers.
"""

import base64
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


VENDOR = "modal"
ACTOR_VERSION = "1.0.0"
SECRET_TOKEN_ID = "MODAL_TOKEN_ID"
SECRET_TOKEN_SECRET = "MODAL_TOKEN_SECRET"
KEY_HEADER = "Modal-Key"
SECRET_HEADER = "Modal-Secret"
ENDPOINT_HOST_SUFFIX = ".modal.run"
USER_AGENT = "cowboy-store-wrapper/1.0.0"
IDEMPOTENCY_PREFIX = "cowboy-modal-"
METHODS = ("GET", "POST")

MAX_ENDPOINTS = 16
MAX_LABEL_CHARS = 64
MAX_URL_CHARS = 512
MAX_QUERY_PARAMS = 24
MAX_QUERY_VALUE_CHARS = 512
MAX_QUERYSTRING_CHARS = 2_048
MAX_ERROR_CHARS = 500
MAX_REQUEST_BYTES = 32_000

SUPPORTED_INPUT_FIELDS = ("hire_id", "endpoint", "payload", "query")

RUNNER_RESULT_BYTE_BUDGET = 65_536
RETURN_BYTE_BUDGET = 38_000
MAX_RESULT_CHUNK_BYTES = 3_900
MAX_RETAINED_REQUESTS = 16
MAX_RETAINED_PER_HIRE = 6
MAX_REGISTERED_HIRES = 512
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150

KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_CALLS = "call_count"
KEY_REQUEST_IDS = "request_ids"
KEY_ENDPOINT_LABELS = "endpoint_labels"
KEY_LAST_STATUS = "last_status"
KEY_LAST_OUTPUT = "last_output"

_QUERY_SAFE = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"


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
            return value if isinstance(value, dict) else {"value": value}
        except Exception:
            return {}
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
            return value if isinstance(value, dict) else {"value": value}
        except Exception:
            return {}
    return {}


def _decode_result(result, depth=0):
    if result is None or depth > 4:
        return {}
    if not isinstance(result, (bytes, bytearray, str, dict, list)):
        if hasattr(result, "data"):
            return _decode_result(result.data, depth + 1)
        if hasattr(result, "body"):
            return _decode_result(result.body, depth + 1)
        if hasattr(result, "result"):
            return _decode_result(result.result, depth + 1)
        if hasattr(result, "value"):
            return _decode_result(result.value, depth + 1)
        if hasattr(result, "text"):
            return _decode_result(result.text, depth + 1)
        if hasattr(result, "raw"):
            return _decode_result(result.raw, depth + 1)
        return {"raw_text": str(result)}
    if isinstance(result, (bytes, bytearray)):
        try:
            return _decode_result(bytes(result).decode("utf-8"), depth + 1)
        except Exception:
            return None
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except Exception:
            return None
        if isinstance(value, (dict, list)):
            return _decode_result(value, depth + 1)
        return {"value": value}
    if isinstance(result, list):
        return {"results": result}
    return result


def _json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _truncate_text_bytes(text, byte_limit):
    raw = text.encode("utf-8")
    if len(raw) <= byte_limit:
        return text
    return raw[:max(0, byte_limit)].decode("utf-8", "ignore")


def _contains_secret_template(value):
    # secret-template guard: refuse "${" anywhere in caller input (token leak risk)
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(
            _contains_secret_template(key) or _contains_secret_template(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_template(child) for child in value)
    return False


def _address_hex(value, field):
    try:
        return Address(value).to_hex(checksum=False)
    except Exception as exc:
        raise ValueError("%s must be a 20-byte address" % field) from exc


def _sender_hex():
    return _address_hex(runtime.get_sender(), "sender")


def _bounded_int(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("%s must be an integer" % field)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer" % field) from exc
    if parsed < low or parsed > high:
        raise ValueError("%s must be between %s and %s" % (field, low, high))
    return parsed


def _declaration_id(value):
    text = str(value or "").strip().lower()
    try:
        raw = bytes.fromhex(text[2:] if text.startswith("0x") else text)
    except ValueError as exc:
        raise ValueError("declaration_id must be a 32-byte hex value") from exc
    if len(raw) != 32:
        raise ValueError("declaration_id must be a 32-byte hex value")
    return "0x" + raw.hex()


def _no_control(value, field):
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError("%s must not contain control characters" % field)
    return value


def _label(value):
    if not isinstance(value, str):
        raise ValueError("endpoint must be a string label")
    value = value.strip()
    if not value:
        raise ValueError("endpoint label is required")
    if len(value) > MAX_LABEL_CHARS:
        raise ValueError("endpoint label must be at most %d characters" % MAX_LABEL_CHARS)
    _no_control(value, "endpoint label")
    for char in value:
        if not (char.isalnum() or char in "-_."):
            raise ValueError("endpoint label may only use letters, digits, - _ .")
    return value


def _validate_endpoint_url(value):
    # tokens ride this URL: host must be *.modal.run, no userinfo/port/query
    # (buyer picks a registered label, never a URL -- anti credential-harvesting)
    if not isinstance(value, str):
        raise ValueError("endpoint url must be a string")
    value = value.strip()
    if not value:
        raise ValueError("endpoint url is required")
    if len(value) > MAX_URL_CHARS:
        raise ValueError("endpoint url must be at most %d characters" % MAX_URL_CHARS)
    _no_control(value, "endpoint url")
    if not value.startswith("https://"):
        raise ValueError("endpoint url must be https")
    if "?" in value or "#" in value:
        raise ValueError("endpoint url must not contain a query string or fragment")
    if "\\" in value:
        raise ValueError("endpoint url must not contain a backslash")
    if "{" in value or "}" in value:
        # no ${...} in the URL either -- render_job_with_secrets would leak the token
        raise ValueError("endpoint url must not contain a secret-template placeholder")
    host = value[len("https://"):].split("/", 1)[0]
    if not host or "@" in host or ":" in host or "%" in host:
        raise ValueError("endpoint url has an ambiguous authority")
    if not host.endswith(ENDPOINT_HOST_SUFFIX) or len(host) <= len(ENDPOINT_HOST_SUFFIX):
        raise ValueError("endpoint host must be a *%s subdomain" % ENDPOINT_HOST_SUFFIX)
    return value


def _method(value):
    method = str(value or "POST").strip().upper()
    if method not in METHODS:
        raise ValueError("endpoint method must be one of %s" % ", ".join(METHODS))
    return method


def _endpoint_record(cfg):
    return {
        "label": _label(cfg.get("label") or cfg.get("endpoint")),
        "url": _validate_endpoint_url(cfg.get("url")),
        "method": _method(cfg.get("method")),
    }


def _percent_encode(text):
    return "".join(
        chr(byte) if byte in _QUERY_SAFE else "%%%02X" % byte
        for byte in text.encode("utf-8")
    )


def _querystring(query):
    if query is None:
        return ""
    if not isinstance(query, dict):
        raise ValueError("query must be an object of scalar values")
    if len(query) > MAX_QUERY_PARAMS:
        raise ValueError("query supports at most %d parameters" % MAX_QUERY_PARAMS)
    parts = []
    for key, value in query.items():
        if not isinstance(key, str) or not key:
            raise ValueError("query keys must be non-empty strings")
        _no_control(key, "query key")
        if isinstance(value, bool):
            item = "true" if value else "false"
        elif isinstance(value, (int, float)):
            item = repr(value) if isinstance(value, float) else str(value)
        elif isinstance(value, str):
            item = value
        else:
            raise ValueError("query values must be strings, numbers, or booleans")
        if len(item) > MAX_QUERY_VALUE_CHARS:
            raise ValueError("query value exceeds %d characters" % MAX_QUERY_VALUE_CHARS)
        _no_control(item, "query value")
        parts.append("%s=%s" % (_percent_encode(key), _percent_encode(item)))
    if not parts:
        return ""
    qs = "?" + "&".join(parts)
    if len(qs) > MAX_QUERYSTRING_CHARS:
        raise ValueError("query string exceeds %d characters" % MAX_QUERYSTRING_CHARS)
    return qs


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "modal request failed"
    for key in ("message", "detail", "error", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return _truncate_text_bytes(value, MAX_ERROR_CHARS)
        if isinstance(value, dict):
            for inner in ("message", "error", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return _truncate_text_bytes(text, MAX_ERROR_CHARS)
    if _is_non_json_transport(decoded):
        snippet = _raw_snippet(decoded)
        if snippet:
            return _truncate_text_bytes(snippet, MAX_ERROR_CHARS)
        return "modal endpoint returned a non-JSON response"
    return "modal request failed"


def _raw_snippet(decoded):
    raw = decoded.get("raw")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        text = base64.b64decode(raw + "===", validate=False).decode("utf-8", "replace")
    except Exception:
        return ""
    return " ".join(text.split())[:MAX_ERROR_CHARS]


def _is_non_json_transport(decoded):
    # The HTTP runner reserves this exact two-field shape for bodies it could
    # not decode. It has no separate discriminator, including when the server
    # claimed application/json.
    if not isinstance(decoded, dict):
        return False
    if set(decoded) != {"raw", "content_type"}:
        return False
    content_type = decoded.get("content_type")
    return (
        isinstance(decoded.get("raw"), str)
        and isinstance(content_type, str)
    )


def _is_vendor_error(decoded):
    # Application JSON is data even when it owns fields such as raw or error.
    return _is_non_json_transport(decoded)


@actor
class ModalComputeWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Modal Compute"
        self.storage["__description__"] = (
            "Bounded invocation of a registered Modal web endpoint for Store "
            "Hires. Modal token pair held in CBSS."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        self.storage[KEY_ENDPOINT_LABELS] = []
        for endpoint in cfg.get("endpoints") or []:
            self._store_endpoint(_endpoint_record(endpoint))
        runtime.emit_event(
            "wrapper.init",
            {"vendor": VENDOR, "owner": owner, "store_registrar": registrar},
        )
        return codec.encode({"ok": True, "vendor": VENDOR})

    def _store_endpoint(self, record):
        labels = list(self.storage.get(KEY_ENDPOINT_LABELS, []))
        key = "endpoint:" + record["label"]
        if record["label"] not in labels:
            if len(labels) >= MAX_ENDPOINTS:
                raise ValueError("registered endpoint capacity is full")
            self.storage[KEY_ENDPOINT_LABELS] = labels + [record["label"]]
        self.storage[key] = record

    @public
    def register_endpoint(self, payload):
        if _sender_hex() != self.storage.get(KEY_REGISTRAR, ""):
            raise ValueError("register_endpoint: Store registrar required")
        record = _endpoint_record(_parse(payload))
        self._store_endpoint(record)
        runtime.emit_event(
            "wrapper.endpoint_registered",
            {"label": record["label"], "method": record["method"], "url": record["url"]},
        )
        return codec.encode({"ok": True, **record})

    @public
    def register_hire(self, payload):
        if _sender_hex() != self.storage.get(KEY_REGISTRAR, ""):
            raise ValueError("register_hire: Store registrar required")
        cfg = _parse(payload)
        hire_id = _bounded_int(cfg.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hirer = _address_hex(cfg.get("hirer"), "hirer")
        record = {
            "hire_id": hire_id,
            "hirer": hirer,
            "declaration_id": _declaration_id(cfg.get("declaration_id")),
            "version": _bounded_int(cfg.get("version"), "version", 1, 2**32 - 1),
            "artifact_id": _bounded_int(
                cfg.get("artifact_id"), "artifact_id", 0, 2**32 - 1
            ),
        }
        key = "hire:" + str(hire_id)
        existing = self.storage.get(key)
        if existing:
            if existing != record:
                raise ValueError(
                    "register_hire: hire_id already registered with different data"
                )
            return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})
        count = int(self.storage.get(KEY_HIRE_COUNT, 0))
        if count >= MAX_REGISTERED_HIRES:
            raise ValueError("register_hire: registered Hire capacity is full")
        self.storage[key] = record
        self.storage[KEY_HIRE_COUNT] = count + 1
        runtime.emit_event(
            "wrapper.hire_registered", {"hire_id": hire_id, "hirer": hirer}
        )
        return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})

    def _hire(self, req, operation):
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hire = self.storage.get("hire:" + str(hire_id)) or {}
        if not hire:
            return hire_id, {}, codec.encode(
                {
                    "ok": False,
                    "error": "hire %d is not activated on this actor" % hire_id,
                    "hint": "the Store registrar must call register_hire for this hire",
                }
            )
        if _sender_hex() != hire.get("hirer"):
            raise ValueError("%s: caller is not the registered hirer" % operation)
        return hire_id, hire, None

    def _authorize(self, req, operation):
        hire_id, hire, refusal = self._hire(req, operation)
        if refusal is not None:
            return hire_id, hire, refusal
        try:
            size = len(_json_bytes(req))
        except Exception:
            return hire_id, hire, self._refuse(operation, "request must be valid JSON data")
        if size > MAX_REQUEST_BYTES:
            return hire_id, hire, self._refuse(
                operation, "request exceeds %d bytes" % MAX_REQUEST_BYTES
            )
        if _contains_secret_template(req):
            return hire_id, hire, self._refuse(
                operation, "request may not contain runner secret templates"
            )
        return hire_id, hire, None

    def _pins(self, hire_id, hire):
        return {
            "hire_id": hire_id,
            "declaration_id": hire["declaration_id"],
            "version": hire["version"],
            "artifact_id": hire["artifact_id"],
        }

    def _refuse(self, operation, error):
        output = {"ok": False, "operation": operation, "error": error}
        self.storage[KEY_LAST_STATUS] = "error"
        # M2: last_output is public+non-expiring; metadata only, never vendor data
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    @public
    @deferred
    def invoke(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "invoke")
        if refusal is not None:
            return refusal
        try:
            unsupported = sorted(k for k in req if k not in SUPPORTED_INPUT_FIELDS)
            if unsupported:
                raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
            label = _label(req.get("endpoint"))
            endpoint = self.storage.get("endpoint:" + label)
            if not endpoint:
                raise ValueError("endpoint %r is not registered" % label)
            method = endpoint["method"]
            url = endpoint["url"]
            raw_body = None
            if method == "POST":
                if req.get("query") is not None:
                    raise ValueError("query is only valid for GET endpoints")
                body = req.get("payload")
                if body is None:
                    body = {}
                if not isinstance(body, dict):
                    raise ValueError("payload must be a JSON object")
                raw_body = _json_bytes(body)
                if len(raw_body) > MAX_REQUEST_BYTES:
                    raise ValueError("payload exceeds %d bytes" % MAX_REQUEST_BYTES)
            else:  # GET
                if req.get("payload") is not None:
                    raise ValueError("payload is only valid for POST endpoints")
                url = url + _querystring(req.get("query"))
        except ValueError as exc:
            return self._refuse("invoke", str(exc))
        return self._accept(hire_id, hire, label, method, url, raw_body)

    def _accept(self, hire_id, hire, label, method, url, raw_body):
        buyer = _sender_hex()
        self._cleanup()
        ids = list(self.storage.get(KEY_REQUEST_IDS, []))
        cid = continuation.new_cid(self, "_run_request")
        for request_id in ids:
            record = self.storage.get("request:" + str(request_id)) or {}
            if record.get("cid") == cid:
                return codec.encode(
                    {"ok": False, "error": "transaction already has a pending request"}
                )
        if len(ids) >= MAX_RETAINED_REQUESTS:
            return codec.encode(
                {"ok": False, "error": "request capacity is full; retry after results expire"}
            )
        retained_for_hire = sum(
            1
            for request_id in ids
            if (self.storage.get("request:" + str(request_id)) or {}).get("hire_id")
            == hire_id
        )
        if retained_for_hire >= MAX_RETAINED_PER_HIRE:
            return codec.encode(
                {"ok": False, "error": "this hire's retained request capacity is full"}
            )
        request_id = _bounded_int(
            int(self.storage.get(KEY_CALLS, 0)) + 1, "request_id", 1, 2**64 - 1
        )
        now = int(runtime.get_block_height())
        record = {
            "request_id": request_id,
            "hire_id": hire_id,
            "buyer": buyer,
            "operation": "invoke",
            "endpoint": label,
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        account = self.storage[KEY_SECRET_ACCOUNT]
        headers = {
            "Accept": "application/json",
            KEY_HEADER: "${%s}" % SECRET_TOKEN_ID,
            SECRET_HEADER: "${%s}" % SECRET_TOKEN_SECRET,
            "Idempotency-Key": IDEMPOTENCY_PREFIX + cid.hex(),
            "User-Agent": USER_AGENT,
        }
        # GET must omit the body kwarg entirely (E1604); POST uses raw_body exact bytes
        job = {
            "url": url,
            "method": method,
            "headers": headers,
            "secrets": [[account, SECRET_TOKEN_ID], [account, SECRET_TOKEN_SECRET]],
            "hire": self._pins(hire_id, hire),
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
            job["raw_body"] = raw_body
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True,
            "request_id": request_id,
            "hire_id": hire_id,
            "operation": "invoke",
            "endpoint": label,
            "status": "pending",
            "poll_handler": "get_result",
        }
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
        if not record or record.get("status") != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        if int(runtime.get_block_height()) >= int(record["deadline_height"]):
            output = {
                "ok": False,
                "operation": "invoke",
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            if decoded is None:
                output = {
                    "ok": False,
                    "operation": "invoke",
                    "error": "modal endpoint returned a non-JSON response",
                }
            elif not isinstance(decoded, dict) or not decoded:
                # no body + runner.http hides status => never a false ok
                output = {
                    "ok": False,
                    "operation": "invoke",
                    "error": "modal endpoint returned an empty result",
                }
            elif _is_vendor_error(decoded):
                output = {"ok": False, "operation": "invoke", "error": _vendor_error(decoded)}
            else:
                output = {"ok": True, "operation": "invoke", "data": decoded}
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        now = int(runtime.get_block_height())
        record["finished_height"] = now
        record["expires_height"] = now + RESULT_RETENTION_BLOCKS
        record.pop("cid", None)
        response = self._response(record)
        if len(codec.encode(response)) > RETURN_BYTE_BUDGET:
            output = {
                "ok": False,
                "operation": "invoke",
                "error": "result exceeds the encoded result byte budget",
            }
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # M2: metadata only into never-expiring public state (no vendor data).
        self.storage[KEY_LAST_OUTPUT] = {"ok": output["ok"], "operation": output["operation"]}
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "operation": "invoke",
                "status": record["status"],
            },
        )
        return codec.encode(response)

    def _response(self, record):
        response = {
            key: record[key]
            for key in ("request_id", "hire_id", "operation", "endpoint", "status")
        }
        response["ok"] = record["status"] != "error"
        if record["status"] == "pending":
            response["deadline_height"] = record["deadline_height"]
        else:
            response["output"] = record["output"]
            response["expires_height"] = record["expires_height"]
        return response

    def _cleanup(self, only=None):
        now = int(runtime.get_block_height())
        retained = []
        ids = list(self.storage.get(KEY_REQUEST_IDS, []))
        for request_id in ids:
            if only is not None and request_id != only:
                retained.append(request_id)
                continue
            key = "request:" + str(request_id)
            record = self.storage.get(key)
            if not record:
                continue
            if record["status"] == "pending" and now >= record["deadline_height"]:
                cid = record.get("cid")
                if cid:
                    continuation.delete_cont(cid)
                self._finish(request_id, None)
                record = self.storage.get(key)
            if record["status"] != "pending" and now >= record["expires_height"]:
                del self.storage[key]
            else:
                retained.append(request_id)
        if retained != ids:
            self.storage[KEY_REQUEST_IDS] = retained

    @public
    @deferred
    def get_result(self, payload):
        req = _parse(payload)
        request_id = _bounded_int(req.get("request_id"), "request_id", 1, 2**64 - 1)
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        self._cleanup(request_id)
        record = self.storage.get("request:" + str(request_id))
        if (
            not record
            or record.get("buyer") != _sender_hex()
            or record.get("hire_id") != hire_id
        ):
            return codec.encode(
                {"ok": False, "error": "request not found for caller and hire"}
            )
        encoded = codec.encode(self._response(record))
        if len(encoded) > RETURN_BYTE_BUDGET:
            raise ValueError("get_result: encoded result exceeds the result byte budget")
        if "receipt_offset" in req:
            offset = _bounded_int(
                req["receipt_offset"], "receipt_offset", 0, len(encoded) - 1
            )
            runtime.emit_event(
                "wrapper.result_chunk",
                {
                    "request_id": request_id,
                    "hire_id": hire_id,
                    "offset": offset,
                    "total": len(encoded),
                    "data": encoded[offset : offset + MAX_RESULT_CHUNK_BYTES],
                },
            )
        return encoded

    @public
    def list_endpoints(self, payload):
        labels = self.storage.get(KEY_ENDPOINT_LABELS, [])
        endpoints = []
        for label in labels:
            record = self.storage.get("endpoint:" + label)
            if record:
                endpoints.append(
                    {"label": record["label"], "method": record["method"], "url": record["url"]}
                )
        return codec.encode({"vendor": VENDOR, "endpoints": endpoints})

    @public
    def capability(self, payload):
        return codec.encode(
            {
                "vendor": VENDOR,
                "slot": "compute-invoke",
                "handlers": ["invoke"],
                "poll_handler": "get_result",
                "returns": "modal-endpoint-async-result",
                "scope": "invocation of a registered, already-deployed Modal web endpoint",
                "unsupported": (
                    "Modal Sandboxes and deploy-then-invoke of arbitrary code are NOT "
                    "wrapped: a create-handle->exec flow would round-trip a live sandbox "
                    "credential through public state and produce unbounded output; it needs "
                    "a runner-side MCP tool that does not exist."
                ),
                "auth": {"headers": [KEY_HEADER, SECRET_HEADER], "scheme": "modal-proxy-auth"},
                "mutating": True,
                "idempotency": (
                    "stable Idempotency-Key per request; Modal's proxy does not dedup, so a "
                    "runner retry dedups only if the deployed function honors the header"
                ),
                "status_visibility": (
                    "runner.http exposes only the response body, not the HTTP status; the "
                    "wrapper flags non-JSON/proxy/edge errors but returns any JSON body verbatim"
                ),
                "privacy": "all inputs are public chain data; never send secrets or credentials",
                "input": {
                    "hire_id": "int",
                    "endpoint": "str (a registered endpoint label)",
                    "payload": "object? (POST endpoints only)",
                    "query": "object of scalars? (GET endpoints only)",
                },
                "output": "acceptance record; get_result returns the endpoint reply under output.data",
                "hire_activation": {"protocol": "store-admin-v1"},
                "endpoint_registration": {"handler": "register_endpoint", "registrar_only": True},
                "endpoint_host_suffix": ENDPOINT_HOST_SUFFIX,
                "methods": list(METHODS),
                "max_endpoints": MAX_ENDPOINTS,
                "max_request_bytes": MAX_REQUEST_BYTES,
                "runner_result_byte_budget": RUNNER_RESULT_BYTE_BUDGET,
                "result_byte_budget": RETURN_BYTE_BUDGET,
                "max_result_chunk_bytes": MAX_RESULT_CHUNK_BYTES,
                "max_retained_requests": MAX_RETAINED_REQUESTS,
                "max_retained_per_hire": MAX_RETAINED_PER_HIRE,
                "max_registered_hires": MAX_REGISTERED_HIRES,
                "request_lifetime_blocks": REQUEST_LIFETIME_BLOCKS,
                "result_retention_blocks": RESULT_RETENTION_BLOCKS,
            }
        )

    @public
    def get_last_output(self, payload):
        last = self.storage.get(KEY_LAST_OUTPUT) or {}
        return codec.encode(
            {
                "operation": last.get("operation"),
                "ok": last.get("ok"),
                "status": self.storage.get(KEY_LAST_STATUS),
            }
        )

    @public
    def get_config(self, payload):
        ids = self.storage.get(KEY_REQUEST_IDS, [])
        active = sum(
            1
            for request_id in ids
            if (self.storage.get("request:" + str(request_id)) or {}).get("status")
            == "pending"
        )
        return codec.encode(
            {
                "vendor": VENDOR,
                "response_byte_limit": RETURN_BYTE_BUDGET,
                "call_count": self.storage.get(KEY_CALLS, 0),
                "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                "registered_hires": self.storage.get(KEY_HIRE_COUNT, 0),
                "registered_endpoints": len(self.storage.get(KEY_ENDPOINT_LABELS, [])),
                "active_requests": active,
                "retained_requests": len(ids),
                "last_status": self.storage.get(KEY_LAST_STATUS),
            }
        )


def _get_actor():
    return ModalComputeWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_endpoint(payload):
    return _get_actor().register_endpoint(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def invoke(payload):
    return _get_actor().invoke(payload)


def get_result(payload):
    return _get_actor().get_result(payload)


def list_endpoints(payload):
    return _get_actor().list_endpoints(payload)


def capability(payload):
    return _get_actor().capability(payload)


def get_last_output(payload):
    return _get_actor().get_last_output(payload)


def get_config(payload):
    return _get_actor().get_config(payload)


def _run_request__resume(payload):
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
