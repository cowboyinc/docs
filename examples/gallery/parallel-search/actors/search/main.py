"""Hire-gated Parallel Search actor."""

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


VENDOR = "parallel"
ACTOR_VERSION = "1.0.0"
SEARCH_ENDPOINT = "https://api.parallel.ai/v1/search"
SECRET_KEY = "PARALLEL_API_KEY"
AUTH_HEADER = "x-api-key"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

MODES = ("turbo", "fast", "basic", "advanced")
DEFAULT_MODE = "fast"
MAX_QUERY_COUNT = 5
MAX_QUERY_CHARS = 200
MAX_OBJECTIVE_CHARS = 5_000
MAX_SESSION_CHARS = 1_000
MAX_CLIENT_MODEL_CHARS = 200
MIN_MAX_RESULTS = 1
MAX_MAX_RESULTS = 5
DEFAULT_MAX_RESULTS = 5
MIN_MAX_CHARS_TOTAL = 500
MAX_MAX_CHARS_TOTAL = 8_000
DEFAULT_MAX_CHARS_TOTAL = 6_000
MIN_CHARS_PER_RESULT = 200
MAX_CHARS_PER_RESULT = 2_000
DEFAULT_CHARS_PER_RESULT = 1_200
MAX_REQUEST_BYTES = 16_384
SUPPORTED_INPUT_FIELDS = (
    "hire_id",
    "search_queries",
    "objective",
    "mode",
    "max_results",
    "max_chars_total",
    "max_chars_per_result",
    "session_id",
    "client_model",
)

# Transport cap: the runner->actor result has a hard ~40-64KB transport limit. If
# the whole vendor reply exceeds it the continuation never resumes and the request
# parks. RUNNER_*_BYTE_BUDGET bound the request-side (few queries, <=5 results,
# capped chars) so the reply stays small; _limited_response only runs AFTER
# transport, so it cannot rescue an oversized reply. This is why only the bounded
# search surface is exposed and no server-side extractor (a stub returning {}) is used.
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
        return _decode_result(value, depth + 1) if isinstance(value, (dict, list)) else {
            "value": value
        }
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
    try:
        out = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except Exception:
        return {"error": "non-serializable response"}
    original = len(codec.encode(out))
    if original <= budget:
        return out
    if not isinstance(out, dict):
        out = {"data": out}
    marker = {"truncated": True, "original_bytes": original, "limit_bytes": budget}
    out["_cowboy"] = marker

    while len(codec.encode(out)) > budget:
        excerpts = [
            (item, index, len(text.encode("utf-8")))
            for item in out.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("excerpts"), list)
            for index, text in enumerate(item["excerpts"])
            if isinstance(text, str) and text
        ]
        if excerpts:
            excess = len(codec.encode(out)) - budget
            total = sum(size for _, _, size in excerpts)
            target = max(0, total - excess - 256)
            ratio = float(target) / float(total) if total else 0.0
            changed = False
            for item, index, size in excerpts:
                shortened = _truncate_text_bytes(item["excerpts"][index], int(size * ratio))
                changed = changed or shortened != item["excerpts"][index]
                item["excerpts"][index] = shortened
            if changed:
                continue
        results = out.get("results")
        if isinstance(results, list) and results:
            results.pop()
            continue
        out = {"_cowboy": marker}
        break
    return out


# Secret guard: refuse any caller input holding "${...}". CBSS substitutes
# ${SECRET} at the runner, so an echoed substituted value could leak the API key.
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


def _search_queries(value):
    if not isinstance(value, list) or not value:
        raise ValueError("search_queries must be a nonempty list")
    if len(value) > MAX_QUERY_COUNT:
        raise ValueError("search_queries supports at most %d items" % MAX_QUERY_COUNT)
    return [
        _text(item, "search_queries item", MAX_QUERY_CHARS, required=True)
        for item in value
    ]


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "parallel request failed"
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
        if isinstance(value, dict):
            for inner in ("message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    if "raw" in decoded:
        return "parallel returned a non-JSON response"
    return "parallel request failed"


def _is_vendor_error(decoded):
    return (
        not isinstance(decoded, dict)
        or decoded.get("type") == "error"
        or any(key in decoded for key in ("error", "detail", "raw", "raw_text"))
    )


def _valid_search_response(decoded):
    if not isinstance(decoded, dict):
        return False
    if not isinstance(decoded.get("search_id"), str) or not decoded.get("search_id"):
        return False
    if not isinstance(decoded.get("session_id"), str) or not decoded.get("session_id"):
        return False
    results = decoded.get("results")
    if not isinstance(results, list):
        return False
    for item in results:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("url"), str)
            or not item.get("url")
        ):
            return False
        excerpts = item.get("excerpts")
        if not isinstance(excerpts, list) or any(not isinstance(x, str) for x in excerpts):
            return False
        if item.get("title") is not None and not isinstance(item.get("title"), str):
            return False
        if item.get("publish_date") is not None and not isinstance(
            item.get("publish_date"), str
        ):
            return False
    return True


def _search_body(req):
    unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS)
    if unsupported:
        raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
    body = {"search_queries": _search_queries(req.get("search_queries"))}
    objective = _text(req.get("objective"), "objective", MAX_OBJECTIVE_CHARS)
    if objective:
        body["objective"] = objective
    mode = str(req.get("mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise ValueError("mode must be turbo, fast, basic, or advanced")
    body["mode"] = mode
    total = _bounded_int(
        req.get("max_chars_total", DEFAULT_MAX_CHARS_TOTAL),
        "max_chars_total",
        MIN_MAX_CHARS_TOTAL,
        MAX_MAX_CHARS_TOTAL,
    )
    max_results = _bounded_int(
        req.get("max_results", DEFAULT_MAX_RESULTS),
        "max_results",
        MIN_MAX_RESULTS,
        MAX_MAX_RESULTS,
    )
    per_result = _bounded_int(
        req.get("max_chars_per_result", DEFAULT_CHARS_PER_RESULT),
        "max_chars_per_result",
        MIN_CHARS_PER_RESULT,
        MAX_CHARS_PER_RESULT,
    )
    body["max_chars_total"] = total
    body["advanced_settings"] = {
        "max_results": max_results,
        "excerpt_settings": {"max_chars_per_result": min(per_result, total)},
    }
    session_id = _text(req.get("session_id"), "session_id", MAX_SESSION_CHARS)
    client_model = _text(req.get("client_model"), "client_model", MAX_CLIENT_MODEL_CHARS)
    if session_id:
        body["session_id"] = session_id
    if client_model:
        body["client_model"] = client_model
    if len(_json_bytes(body)) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds %d bytes" % MAX_REQUEST_BYTES)
    return body


@actor
class ParallelSearchWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Parallel Search"
        self.storage["__description__"] = (
            "Bounded GA Parallel web search for registered Store Hires. "
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
        # M2: KEY_LAST_OUTPUT never expires; store metadata only (no error text).
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    # @deferred is required: @public auto-marks a handler @pure, and a pure handler
    # may not submit a runner job or schedule a continuation (E1205 on chain).
    @public
    @deferred
    def search(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "search")
        if refusal is not None:
            return refusal
        try:
            body = _search_body(req)
        except ValueError as exc:
            return self._refuse("search", str(exc))
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
            "operation": "search",
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        # Secret custody: the key rides the job only as the ${SECRET} reference plus
        # the secrets allow-list; the resolved value never enters actor/consensus state.
        # raw_body sends pre-serialized bytes verbatim (node body re-adaptation rejects
        # fractional JSON numbers); allow_nan=False keeps it strict JSON.
        job = {
            "url": SEARCH_ENDPOINT,
            "method": "POST",
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                AUTH_HEADER: "${%s}" % SECRET_KEY,
            },
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
        # capture() ctx is PUBLIC continuation state: hold only request_id, never a
        # resolved credential. The ${SECRET} ref stays a ref inside req["job"].
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
            if _valid_search_response(decoded):
                data = _limited_response(decoded, API_BYTE_BUDGET)
                if (
                    not isinstance(data, dict)
                    or data == {"error": "non-serializable response"}
                    or set(data.keys()) == {"_cowboy"}
                ):
                    output = {
                        "ok": False,
                        "operation": "search",
                        "error": "result could not be bounded under the byte budget",
                    }
                else:
                    output = {"ok": True, "operation": "search", "data": data}
            elif _is_vendor_error(decoded):
                output = {
                    "ok": False,
                    "operation": "search",
                    "error": _vendor_error(decoded),
                }
            else:
                output = {
                    "ok": False,
                    "operation": "search",
                    "error": "unexpected search response from parallel",
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
                "operation": "search",
                "error": "result exceeds the encoded result byte budget",
            }
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # M2: KEY_LAST_OUTPUT never expires, so it holds metadata only. Storing the
        # vendor `data` here would leave the last result in public state forever;
        # the buyer's data lives in the request record, which expires.
        self.storage[KEY_LAST_OUTPUT] = {"operation": "search", "ok": output["ok"]}
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
                "returns": "parallel-search-async-result",
                "input": {
                    "hire_id": "int",
                    "search_queries": "str[1..5]",
                    "objective": "str?",
                    "mode": "turbo|fast|basic|advanced?",
                    "max_results": "int?",
                    "max_chars_total": "int?",
                    "max_chars_per_result": "int?",
                    "session_id": "str?",
                    "client_model": "str?",
                },
                "output": "acceptance record; get_result returns vendor data under output.data",
                "hire_activation": {"protocol": "store-admin-v1"},
                "default_mode": DEFAULT_MODE,
                "max_queries": MAX_QUERY_COUNT,
                "max_results": MAX_MAX_RESULTS,
                "max_chars_total": MAX_MAX_CHARS_TOTAL,
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
                "default_mode": DEFAULT_MODE,
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
    return ParallelSearchWrapper()


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
