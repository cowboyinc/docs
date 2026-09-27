"""Hire-gated Tavily Search actor."""

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


VENDOR = "tavily"
ACTOR_VERSION = "1.0.0"
SEARCH_ENDPOINT = "https://api.tavily.com/search"
SECRET_KEY = "TAVILY_API_KEY"
AUTH_HEADER = "Authorization"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

SEARCH_DEPTHS = ("advanced", "basic", "fast", "ultra-fast")
TOPICS = ("general", "news", "finance")
TIME_RANGES = ("day", "week", "month", "year", "d", "w", "m", "y")
ANSWER_MODES = ("basic", "advanced")
RAW_CONTENT_MODES = ("markdown", "text")
INCLUDE_DOMAIN_MODES = ("filter", "boost")
MAX_QUERY_CHARS = 400
MAX_DOMAIN_COUNT = 20
MAX_DOMAIN_CHARS = 253
MAX_COUNTRY_CHARS = 64
MAX_LANGUAGE_CHARS = 64
MAX_TRACKING_ID_CHARS = 128
MIN_MAX_RESULTS = 0
MAX_MAX_RESULTS = 10
DEFAULT_MAX_RESULTS = 5
MAX_REQUEST_BYTES = 16_384
SUPPORTED_INPUT_FIELDS = (
    "hire_id",
    "query",
    "search_depth",
    "chunks_per_source",
    "max_results",
    "topic",
    "time_range",
    "start_date",
    "end_date",
    "include_published_date",
    "filter_by_published_date",
    "include_answer",
    "include_raw_content",
    "include_images",
    "include_image_descriptions",
    "include_favicon",
    "include_domains",
    "exclude_domains",
    "include_domains_mode",
    "country",
    "language",
    "filter_by_language",
    "auto_parameters",
    "exact_match",
    "include_usage",
    "safe_search",
    "project_id",
    "session_id",
    "human_id",
)

RUNNER_RESULT_BYTE_BUDGET = 65_536
RUNNER_DATA_BYTE_BUDGET = 60_000
RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976
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


def _limited_response(value, budget):
    """Shorten prose fields without changing identifiers, URLs, or result rows."""
    try:
        out = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return {"error": "non-serializable response"}
    original = len(codec.encode(out))
    if original <= budget:
        return out
    if not isinstance(out, dict):
        return {"error": "non-serializable response"}
    marker = {"truncated": True, "original_bytes": original, "limit_bytes": budget}
    out["_cowboy"] = marker
    fields = []
    if isinstance(out.get("answer"), str) and out["answer"]:
        fields.append((out, "answer"))
    for item in out.get("results", []):
        if not isinstance(item, dict):
            continue
        for key in ("content", "raw_content"):
            if isinstance(item.get(key), str) and item[key]:
                fields.append((item, key))
        for image in item.get("images") or []:
            if isinstance(image, dict) and isinstance(image.get("description"), str):
                fields.append((image, "description"))
    for image in out.get("images", []):
        if isinstance(image, dict) and isinstance(image.get("description"), str):
            fields.append((image, "description"))
    while len(codec.encode(out)) > budget:
        largest_parent = None
        largest_key = ""
        largest_size = 0
        for parent, key in fields:
            size = len(parent[key].encode("utf-8"))
            if size > largest_size:
                largest_parent = parent
                largest_key = key
                largest_size = size
        if largest_parent is None or largest_size == 0:
            return {"_cowboy": marker}
        excess = len(codec.encode(out)) - budget
        keep = max(0, largest_size - excess - 128)
        largest_parent[largest_key] = _truncate_text_bytes(
            largest_parent[largest_key], keep
        )
    return out


def _contains_secret_template(value):
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
    # Aligned to the firecrawl reference: reject a non-(int|str) type (any
    # float, incl. nan/inf) BEFORE int(), so a float request id can never
    # raise an uncaught OverflowError (int(float('inf'))) reachable pre-auth.
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


def _text(value, field, max_chars, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    value = value.strip()
    if required and not value:
        raise ValueError("%s is required" % field)
    if len(value) > max_chars:
        raise ValueError("%s must be at most %d characters" % (field, max_chars))
    return value


def _boolean(value, field):
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % field)
    return value


def _choice(value, field, choices):
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    value = value.strip().lower()
    if value not in choices:
        raise ValueError("%s must be one of: %s" % (field, ", ".join(choices)))
    return value


def _select(value, field, choices):
    if isinstance(value, bool):
        return value
    return _choice(value, field, choices)


def _date(value, field):
    return _text(value, field, 32, required=True)


def _domains(value, field):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % field)
    if not value:
        raise ValueError("%s must not be empty" % field)
    if len(value) > MAX_DOMAIN_COUNT:
        raise ValueError("%s supports at most %d domains" % (field, MAX_DOMAIN_COUNT))
    result = []
    for item in value:
        domain = _text(item, field + " item", MAX_DOMAIN_CHARS, required=True).lower()
        if "://" in domain or "/" in domain or any(ch.isspace() for ch in domain):
            raise ValueError("%s items must be bare domains" % field)
        result.append(domain)
    return result


def _tracking_headers(req):
    mapping = (
        ("project_id", "X-Project-ID"),
        ("session_id", "X-Session-Id"),
        ("human_id", "X-Human-Id"),
    )
    headers = {}
    for field, header in mapping:
        value = _text(req.get(field), field, MAX_TRACKING_ID_CHARS)
        if value:
            # these become HTTP header values — refuse CR/LF/control chars (header injection)
            if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
                raise ValueError("%s must not contain control characters" % field)
            headers[header] = value
    return headers


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "tavily request failed"
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
        if isinstance(value, dict):
            for inner in ("error", "message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    if "raw" in decoded:
        return "tavily returned a non-JSON response"
    return "tavily request failed"


def _is_vendor_error(decoded):
    return (
        not isinstance(decoded, dict)
        or decoded.get("type") == "error"
        or any(key in decoded for key in ("error", "detail", "raw", "raw_text"))
    )


def _valid_search_response(decoded):
    if not isinstance(decoded, dict):
        return False
    if not isinstance(decoded.get("query"), str) or not decoded.get("query"):
        return False
    results = decoded.get("results")
    if not isinstance(results, list):
        return False
    if len(results) > MAX_MAX_RESULTS:
        return False
    for item in results:
        if not isinstance(item, dict):
            return False
        for field in ("title", "url"):
            # Tavily's contract requires these present as strings, not non-empty:
            # a result with an empty title (a page with no <title>) is still a
            # valid hit and must not turn a paid response into an error.
            if not isinstance(item.get(field), str):
                return False
        if not isinstance(item.get("content"), str):  # allow "" — Tavily can return empty content
            return False
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False
        for field in ("raw_content", "published_date", "favicon"):
            if item.get(field) is not None and not isinstance(item.get(field), str):
                return False
        if item.get("images") is not None and not isinstance(item.get("images"), list):
            return False
    if "images" not in decoded or not isinstance(decoded.get("images"), list):
        return False
    if "answer" not in decoded or (
        decoded.get("answer") is not None and not isinstance(decoded.get("answer"), str)
    ):
        return False
    response_time = decoded.get("response_time")
    if isinstance(response_time, bool) or not isinstance(
        response_time, (int, float, str)
    ):
        return False
    if decoded.get("request_id") is not None and not isinstance(
        decoded.get("request_id"), str
    ):
        return False
    return True


def _search_body(req):
    unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS)
    if unsupported:
        raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
    body = {
        "query": _text(req.get("query"), "query", MAX_QUERY_CHARS, required=True),
        "max_results": _bounded_int(
            req.get("max_results", DEFAULT_MAX_RESULTS),
            "max_results",
            MIN_MAX_RESULTS,
            MAX_MAX_RESULTS,
        ),
    }
    if "search_depth" in req:
        body["search_depth"] = _choice(
            req["search_depth"], "search_depth", SEARCH_DEPTHS
        )
    if "chunks_per_source" in req:
        body["chunks_per_source"] = _bounded_int(
            req["chunks_per_source"], "chunks_per_source", 1, 3
        )
    if "topic" in req:
        body["topic"] = _choice(req["topic"], "topic", TOPICS)
    if "time_range" in req:
        body["time_range"] = _choice(req["time_range"], "time_range", TIME_RANGES)
    for field in ("start_date", "end_date"):
        if field in req:
            body[field] = _date(req[field], field)
    bool_fields = (
        "include_published_date",
        "filter_by_published_date",
        "include_images",
        "include_image_descriptions",
        "include_favicon",
        "filter_by_language",
        "auto_parameters",
        "exact_match",
        "include_usage",
        "safe_search",
    )
    for field in bool_fields:
        if field in req:
            body[field] = _boolean(req[field], field)
    if "include_answer" in req:
        body["include_answer"] = _select(
            req["include_answer"], "include_answer", ANSWER_MODES
        )
    if "include_raw_content" in req:
        body["include_raw_content"] = _select(
            req["include_raw_content"], "include_raw_content", RAW_CONTENT_MODES
        )
    included = _domains(req.get("include_domains"), "include_domains")
    excluded = _domains(req.get("exclude_domains"), "exclude_domains")
    if included:
        body["include_domains"] = included
    if excluded:
        body["exclude_domains"] = excluded
    if "include_domains_mode" in req:
        body["include_domains_mode"] = _choice(
            req["include_domains_mode"],
            "include_domains_mode",
            INCLUDE_DOMAIN_MODES,
        )
    for field, limit in (
        ("country", MAX_COUNTRY_CHARS),
        ("language", MAX_LANGUAGE_CHARS),
    ):
        if field in req:
            body[field] = _text(req[field], field, limit, required=True).lower()
    depth = body.get("search_depth", "basic")
    if "chunks_per_source" in body and depth == "ultra-fast":
        raise ValueError("chunks_per_source is not supported with ultra-fast search")
    if body.get("include_image_descriptions") and not body.get("include_images"):
        raise ValueError("include_image_descriptions requires include_images=true")
    if "include_domains_mode" in body and "include_domains" not in body:
        raise ValueError("include_domains_mode requires include_domains")
    if "country" in body and body.get("topic", "general") != "general":
        raise ValueError("country is only supported for topic=general")
    if body.get("filter_by_language") and "language" not in body:
        raise ValueError("filter_by_language requires language")
    if body.get("safe_search") and depth in ("fast", "ultra-fast"):
        raise ValueError("safe_search is not supported with fast search depths")
    if len(_json_bytes(body)) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds %d bytes" % MAX_REQUEST_BYTES)
    return body


@actor
class TavilySearchWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Tavily Search"
        self.storage["__description__"] = (
            "Bounded Tavily web search for registered Store Hires. "
            "Vendor key held in CBSS."
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
        # metadata only — KEY_LAST_OUTPUT never expires (M2)
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    @public
    @deferred
    def search(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "search")
        if refusal is not None:
            return refusal
        try:
            body = _search_body(req)
            headers = _tracking_headers(req)
        except ValueError as exc:
            return self._refuse("search", str(exc))
        return self._accept(hire_id, hire, body, headers)

    def _accept(self, hire_id, hire, body, tracking_headers):
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
            "operation": "search",
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
        headers.update(tracking_headers)
        job = {
            "url": SEARCH_ENDPOINT,
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
            "operation": "search",
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
                "operation": "search",
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            if _is_vendor_error(decoded):
                output = {
                    "ok": False,
                    "operation": "search",
                    "error": _vendor_error(decoded),
                }
            elif not _valid_search_response(decoded):
                output = {
                    "ok": False,
                    "operation": "search",
                    "error": "unexpected search response from tavily",
                }
            else:
                data = _limited_response(decoded, API_BYTE_BUDGET)
                marker_only = (
                    isinstance(data, dict)
                    and len(data) == 1
                    and "_cowboy" in data
                )
                if (
                    not isinstance(data, dict)
                    or data.get("error") == "non-serializable response"
                    or marker_only
                ):
                    output = {
                        "ok": False,
                        "operation": "search",
                        "error": "result could not be bounded under the byte budget",
                    }
                else:
                    output = {"ok": True, "operation": "search", "data": data}
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
                "operation": "search",
                "error": "result exceeds the encoded result byte budget",
            }
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # metadata only — never persist vendor data in KEY_LAST_OUTPUT (M2)
        self.storage[KEY_LAST_OUTPUT] = {"ok": output["ok"], "operation": output["operation"]}
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "operation": "search",
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
                "slot": "search",
                "handlers": ["search"],
                "poll_handler": "get_result",
                "returns": "tavily-search-async-result",
                "input": {
                    "hire_id": "int",
                    "query": "str",
                    "search_depth": "advanced|basic|fast|ultra-fast?",
                    "chunks_per_source": "int?",
                    "max_results": "int?",
                    "topic": "general|news|finance?",
                    "time_range": "day|week|month|year|d|w|m|y?",
                    "start_date": "YYYY-MM-DD?",
                    "end_date": "YYYY-MM-DD?",
                    "include_answer": "bool|basic|advanced?",
                    "include_raw_content": "bool|markdown|text?",
                    "include_images": "bool?",
                    "include_image_descriptions": "bool?",
                    "include_favicon": "bool?",
                    "include_domains": "str[]?",
                    "exclude_domains": "str[]?",
                    "include_domains_mode": "filter|boost?",
                    "country": "str?",
                    "language": "str?",
                    "include_published_date": "bool?",
                    "filter_by_published_date": "bool?",
                    "filter_by_language": "bool?",
                    "auto_parameters": "bool?",
                    "exact_match": "bool?",
                    "include_usage": "bool?",
                    "safe_search": "bool?",
                    "project_id": "str?",
                    "session_id": "str?",
                    "human_id": "str?",
                },
                "output": "acceptance record; get_result returns vendor data under output.data",
                "hire_activation": {"protocol": "store-admin-v1"},
                "default_search_depth": "basic",
                "default_max_results": DEFAULT_MAX_RESULTS,
                "max_results": MAX_MAX_RESULTS,
                "max_query_chars": MAX_QUERY_CHARS,
                "max_domains": MAX_DOMAIN_COUNT,
                "runner_result_byte_budget": RUNNER_RESULT_BYTE_BUDGET,
                "runner_data_byte_budget": RUNNER_DATA_BYTE_BUDGET,
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
                "endpoint": SEARCH_ENDPOINT,
                "default_search_depth": "basic",
                "default_max_results": DEFAULT_MAX_RESULTS,
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
    return TavilySearchWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def search(payload):
    return _get_actor().search(payload)


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
