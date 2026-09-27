"""Hire-gated Reducto document-parse actor.

Async parse via runner continuation; the hirer reads the result through
`get_result`. The Reducto key stays in CBSS, riding jobs only as `${...}`.
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


VENDOR = "reducto"
ACTOR_VERSION = "1.0.0"
PARSE_ENDPOINT = "https://platform.reducto.ai/parse"
SECRET_KEY = "REDUCTO_API_KEY"
AUTH_HEADER = "Authorization"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

ALLOWED_SCHEMES = ("https://", "http://", "reducto://", "jobid://")

MAX_URL_CHARS = 2_048
MAX_RESULT_URL_CHARS = 4_096
MAX_ID_CHARS = 200
MAX_ERROR_CHARS = 500
MAX_REQUEST_BYTES = 8_000

SUPPORTED_INPUT_FIELDS = ("hire_id", "document_url")

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
KEY_LAST_STATUS = "last_status"
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
            return {"raw_text": ""}
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except Exception:
            return {"raw_text": result}
        if isinstance(value, (dict, list)):
            return _decode_result(value, depth + 1)
        return {"value": value}
    if isinstance(result, list):
        return {"raw": result}
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
    # Refuse caller "${...}": a substituted secret could leak via request/error.
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


def _document_url(value):
    if not isinstance(value, str):
        raise ValueError("document_url must be a string")
    value = value.strip()
    if not value:
        raise ValueError("document_url is required")
    if len(value) > MAX_URL_CHARS:
        raise ValueError("document_url must be at most %d characters" % MAX_URL_CHARS)
    for char in value:
        if ord(char) < 0x20 or char.isspace():
            raise ValueError("document_url must not contain whitespace or control characters")
    lowered = value.lower()
    if not any(lowered.startswith(scheme) for scheme in ALLOWED_SCHEMES):
        raise ValueError(
            "document_url must start with one of: %s" % ", ".join(ALLOWED_SCHEMES)
        )
    return value


def _parse_body(req):
    unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS)
    if unsupported:
        raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
    body = {
        "input": _document_url(req.get("document_url")),
        # force_url_result keeps the reply within the runner transport cap.
        "settings": {"force_url_result": True},
    }
    if len(_json_bytes(body)) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds %d bytes" % MAX_REQUEST_BYTES)
    return body


def _detail_message(detail):
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                msg = item.get("msg") or item.get("message")
                if isinstance(msg, str) and msg:
                    parts.append(msg)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "; ".join(parts)
    if isinstance(detail, dict):
        for inner in ("message", "msg", "error", "detail"):
            text = detail.get(inner)
            if isinstance(text, str) and text:
                return text
    return ""


def _error_text(decoded):
    if not isinstance(decoded, dict):
        return "reducto returned a non-JSON response"
    message = _detail_message(decoded.get("detail"))
    if message:
        return _truncate_text_bytes(message, MAX_ERROR_CHARS)
    for key in ("error", "message", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return _truncate_text_bytes(value, MAX_ERROR_CHARS)
        if isinstance(value, dict):
            text = _detail_message(value)
            if text:
                return _truncate_text_bytes(text, MAX_ERROR_CHARS)
    if "raw" in decoded:
        return "reducto returned a non-JSON response"
    return "unexpected parse response from reducto"


def _has_error_marker(decoded):
    if not isinstance(decoded, dict):
        return True
    return any(key in decoded for key in ("detail", "error", "raw", "raw_text"))


# result_url must be a public presigned URL (no API key); reject key-auth hosts and non-presigned URLs.
_KEY_AUTH_RESULT_HOSTS = ("reducto.ai", "cloud.llamaindex.ai")
_PRESIGN_MARKERS = ("x-amz-signature=", "x-amz-credential=", "x-goog-signature=")


def _is_public_presigned_url(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = url[len("https://"):].split("/", 1)[0].split("?", 1)[0].rsplit("@", 1)[-1].lower()
    if any(host == h or host.endswith("." + h) for h in _KEY_AUTH_RESULT_HOSTS):
        return False
    lowered = url.lower()
    return any(marker in lowered for marker in _PRESIGN_MARKERS)


def _valid_parse_response(decoded):
    if not isinstance(decoded, dict):
        return False
    result = decoded.get("result")
    if not isinstance(result, dict):
        return False
    # force_url_result means only a "url"-type reply is valid.
    if result.get("type") != "url":
        return False
    url = result.get("url")
    if not isinstance(url, str) or not url or len(url) > MAX_RESULT_URL_CHARS:
        return False
    if not _is_public_presigned_url(url):
        return False
    job_id = decoded.get("job_id")
    if (
        not isinstance(job_id, str)
        or not job_id.strip()
        or len(job_id.encode("utf-8")) > MAX_ID_CHARS
    ):
        return False
    result_id = result.get("result_id")
    if result_id is not None and (
        not isinstance(result_id, str)
        or not result_id.strip()
        or len(result_id.encode("utf-8")) > MAX_ID_CHARS
    ):
        return False
    return True


def _clip_str(value, limit):
    if not isinstance(value, str):
        return None
    return _truncate_text_bytes(value, limit)


def _clip_int(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < low or value > high:
        return None
    return value


def _parse_data(decoded):
    result = decoded["result"]
    data = {
        "result_type": "url",
        "result_url": _clip_str(result.get("url"), MAX_RESULT_URL_CHARS),
    }
    result_id = result.get("result_id")
    if result_id:
        data["result_id"] = result_id
    data["job_id"] = decoded["job_id"]
    usage = decoded.get("usage")
    if isinstance(usage, dict):
        pages = _clip_int(usage.get("num_pages"), 0, 2**32 - 1)
        if pages is not None:
            data["num_pages"] = pages
        credits = usage.get("credits")
        if isinstance(credits, (int, float)) and not isinstance(credits, bool):
            data["credits"] = round(float(credits), 4)
    duration = decoded.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        data["duration"] = round(float(duration), 3)
    return data


@actor
class ReductoParseWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Reducto Parse"
        self.storage["__description__"] = (
            "Bounded Reducto document parse for registered Store Hires. "
            "Vendor key held in CBSS; returns a presigned result URL."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        runtime.emit_event(
            "wrapper.init",
            {"vendor": VENDOR, "owner": owner, "store_registrar": registrar},
        )
        return codec.encode({"ok": True, "vendor": VENDOR})

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
            return hire_id, hire, self._refuse(
                operation, "request must be valid JSON data"
            )
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
        # M2: KEY_LAST_OUTPUT is public+permanent; metadata only, never a result URL.
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    @public
    @deferred
    def parse(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "parse")
        if refusal is not None:
            return refusal
        try:
            body = _parse_body(req)
        except ValueError as exc:
            return self._refuse("parse", str(exc))
        return self._accept(hire_id, hire, body)

    def _accept(self, hire_id, hire, body):
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
            int(self.storage.get(KEY_CALLS, 0)) + 1,
            "request_id",
            1,
            2**64 - 1,
        )
        now = int(runtime.get_block_height())
        record = {
            "request_id": request_id,
            "hire_id": hire_id,
            "buyer": buyer,
            "operation": "parse",
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: "Bearer ${%s}" % SECRET_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        job = {
            "url": PARSE_ENDPOINT,
            "method": "POST",
            "headers": headers,
            "raw_body": _json_bytes(body),
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True,
            "request_id": request_id,
            "hire_id": hire_id,
            "operation": "parse",
            "status": "pending",
            "poll_handler": "get_result",
        }
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        # ctx is public on-chain state: carries only the request id, never the key.
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
                "operation": "parse",
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            if _has_error_marker(decoded) or not _valid_parse_response(decoded):
                output = {
                    "ok": False,
                    "operation": "parse",
                    "error": _error_text(decoded),
                }
            else:
                output = {
                    "ok": True,
                    "operation": "parse",
                    "data": _parse_data(decoded),
                }
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
                "operation": "parse",
                "error": "result exceeds the encoded result byte budget",
            }
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # M2: metadata only into never-expiring public state (no result URL/data).
        self.storage[KEY_LAST_OUTPUT] = {"ok": output["ok"], "operation": output["operation"]}
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "operation": "parse",
                "status": record["status"],
            },
        )
        return codec.encode(response)

    def _response(self, record):
        response = {
            key: record[key]
            for key in ("request_id", "hire_id", "operation", "status")
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
    def capability(self, payload):
        return codec.encode(
            {
                "vendor": VENDOR,
                "slot": "document-parse",
                "handlers": ["parse"],
                "poll_handler": "get_result",
                "returns": "reducto-parse-async-result",
                "mutating": True,
                "idempotency": "none; Reducto /parse has no idempotency key, so a runner retry could re-parse and double-bill",
                "privacy": "all inputs and the returned presigned result URL are public chain/result data; anyone holding the URL can fetch until expiry without the API key",
                "input": {
                    "hire_id": "int",
                    "document_url": "str (https/http/reducto/jobid URL to the document)",
                },
                "output": "acceptance record; get_result returns {result_url,result_id,job_id,num_pages,credits,duration} under output.data",
                "result_url_note": "presigned bearer access, short-lived, and publicly fetchable without the key until expiry; fetch it promptly",
                "hire_activation": {"protocol": "store-admin-v1"},
                "max_url_chars": MAX_URL_CHARS,
                "max_result_url_chars": MAX_RESULT_URL_CHARS,
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
                "endpoint": PARSE_ENDPOINT,
                "response_byte_limit": RETURN_BYTE_BUDGET,
                "call_count": self.storage.get(KEY_CALLS, 0),
                "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                "registered_hires": self.storage.get(KEY_HIRE_COUNT, 0),
                "active_requests": active,
                "retained_requests": len(ids),
                "last_status": self.storage.get(KEY_LAST_STATUS),
            }
        )


def _get_actor():
    return ReductoParseWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def parse(payload):
    return _get_actor().parse(payload)


def get_result(payload):
    return _get_actor().get_result(payload)


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
