"""Hire-gated Alpha Vantage market-data actor."""

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


VENDOR = "alphavantage"
ACTOR_VERSION = "1.0.0"
API_ROOT = "https://www.alphavantage.co/query"
SECRET_KEY = "ALPHAVANTAGE_API_KEY"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

# Only functions whose whole response fits the runner result transport cap are
# exposed. INCOME_STATEMENT/BALANCE_SHEET/CASH_FLOW (~122KB), NEWS_SENTIMENT
# (~125KB), and TIME_SERIES_MONTHLY (full history) all exceed it: the Runner
# cannot deliver the body, the continuation never resumes, and the request parks.
# The Runner supports one top-level `extract_path`, but these whole-response
# handlers do not use it because their callback contract expects the vendor
# envelope. See README "Excluded".
FUNCTIONS = {
    "quote": ("GLOBAL_QUOTE", "Global Quote"),
    "daily": ("TIME_SERIES_DAILY", "Time Series (Daily)"),
    "overview": ("OVERVIEW", "Symbol"),
}
# No server-side extraction. The Runner's simplified top-level selector changes
# the callback shape, so responses are fetched whole and bounded client-side in
# `_limited_response`.
EXTRACT_PATHS = {}

OUTPUTSIZES = ("compact",)

MAX_SYMBOL_BYTES = 16
MAX_REQUEST_BYTES = 4_096

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


def _decode_result(result, _depth=0):
    if result is None or _depth > 4:
        return {}
    if not isinstance(result, (bytes, bytearray, str, dict, list)):
        if hasattr(result, "data"):
            return _decode_result(result.data, _depth + 1)
        if hasattr(result, "body"):
            return _decode_result(result.body, _depth + 1)
        if hasattr(result, "result"):
            return _decode_result(result.result, _depth + 1)
        if hasattr(result, "value"):
            return _decode_result(result.value, _depth + 1)
        if hasattr(result, "text"):
            return _decode_result(result.text, _depth + 1)
        if hasattr(result, "raw"):
            return _decode_result(result.raw, _depth + 1)
        return {"raw_text": str(result)}
    if isinstance(result, (bytes, bytearray)):
        try:
            return _decode_result(bytes(result).decode("utf-8"), _depth + 1)
        except Exception:
            return {"raw_text": ""}
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except Exception:
            return {"raw_text": result}
        return _decode_result(value, _depth + 1) if isinstance(value, (dict, list)) else {
            "value": value
        }
    if isinstance(result, list):
        return {"results": result}
    return result


# Alpha Vantage returns API errors in HTTP 200 responses.
_ERROR_KEYS = ("Error Message", "Information", "Note", "error", "detail", "raw", "raw_text")


def _is_vendor_error(decoded):
    if not isinstance(decoded, dict):
        return True
    return any(key in decoded for key in _ERROR_KEYS)


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "alphavantage request failed"
    for key in ("Error Message", "Information", "Note", "message", "error", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
        if isinstance(value, dict):
            for inner in ("message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    if "raw" in decoded:
        return "alphavantage returned a non-JSON response"
    return "alphavantage request failed"


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

    def is_date_map(node):
        if not isinstance(node, dict) or len(node) < 2:
            return False
        for key, child in node.items():
            if (
                not isinstance(key, str)
                or len(key) != 10
                or key[4] != "-"
                or key[7] != "-"
                or any(
                    key[index] not in "0123456789"
                    for index in (0, 1, 2, 3, 5, 6, 8, 9)
                )
                or not isinstance(child, dict)
            ):
                return False
        return True

    def candidates(node, path=()):
        strings, lists, date_maps = [], [], []
        if isinstance(node, dict):
            if path and is_date_map(node):
                date_maps.append((len(codec.encode(node)), path))
            for key, child in node.items():
                if key == "_cowboy":
                    continue
                child_strings, child_lists, child_date_maps = candidates(
                    child, path + (key,)
                )
                strings.extend(child_strings)
                lists.extend(child_lists)
                date_maps.extend(child_date_maps)
        elif isinstance(node, list):
            if len(node) > 1:
                lists.append((len(codec.encode(node)), path))
            for index, child in enumerate(node):
                child_strings, child_lists, child_date_maps = candidates(
                    child, path + (index,)
                )
                strings.extend(child_strings)
                lists.extend(child_lists)
                date_maps.extend(child_date_maps)
        elif isinstance(node, str) and node:
            strings.append((len(node.encode("utf-8")), path))
        return strings, lists, date_maps

    def resolve(path):
        node = out
        for part in path[:-1]:
            node = node[part]
        return node, path[-1]

    def at(path):
        node = out
        for part in path:
            node = node[part]
        return node

    while len(codec.encode(out)) > budget:
        strings, lists, date_maps = candidates(out)
        if date_maps:
            _, path = max(date_maps, key=lambda item: item[0])
            node = at(path)
            newest_first = sorted(node, reverse=True)
            for key in newest_first[max(1, len(newest_first) // 2):]:
                del node[key]
            continue
        if lists:
            _, path = max(lists, key=lambda item: item[0])
            node = at(path)
            del node[max(1, len(node) // 2):]
            continue
        if strings:
            excess = len(codec.encode(out)) - budget
            total = sum(size for size, _ in strings)
            target = max(0, total - excess - 256)
            ratio = float(target) / float(total) if total else 0.0
            changed = False
            for size, path in strings:
                parent, key = resolve(path)
                shortened = _truncate_text_bytes(parent[key], int(size * ratio))
                changed = changed or shortened != parent[key]
                parent[key] = shortened
            if changed:
                continue
        out = {"_cowboy": marker}
        break
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


def _clamp_int(value, low, high, default):
    if value is None or isinstance(value, bool):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _declaration_id(value):
    text = str(value or "").strip().lower()
    try:
        raw = bytes.fromhex(text[2:] if text.startswith("0x") else text)
    except ValueError as exc:
        raise ValueError("declaration_id must be a 32-byte hex value") from exc
    if len(raw) != 32:
        raise ValueError("declaration_id must be a 32-byte hex value")
    return "0x" + raw.hex()


def _symbol(value):
    text = str(value or "").strip().upper()
    if not text or len(text.encode("utf-8")) > MAX_SYMBOL_BYTES:
        raise ValueError("symbol is required")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
    if any(ch not in allowed for ch in text):
        raise ValueError("symbol has an unexpected character")
    return text


def _query(pairs):
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    parts = []
    for key, value in pairs:
        if value is None:
            continue
        for item in (value if isinstance(value, list) else [value]):
            encoded = "".join(
                chr(byte) if byte in safe else "%%%02X" % byte
                for byte in str(item).encode("utf-8")
            )
            parts.append("%s=%s" % (key, encoded))
    return ("?" + "&".join(parts)) if parts else ""


def _av_url(function, params):
    query = _query([("function", function)] + list(params))
    return API_ROOT + query + "&apikey=${%s}" % SECRET_KEY


@actor
class AlphaVantageWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Alpha Vantage"
        self.storage["__description__"] = (
            "Market data per call: quotes, compact daily time series, and company "
            "overview. Key held in CBSS."
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
        # metadata only — KEY_LAST_OUTPUT never expires; storing vendor data/errors
        # would leave the last result in public state forever (get_last_output is
        # observability, not a result read).
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    def _symbol_request(self, payload, operation):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, operation)
        if refusal is not None:
            return refusal
        try:
            symbol = _symbol(req.get("symbol"))
        except ValueError as exc:
            return self._refuse(operation, str(exc))
        function, required = FUNCTIONS[operation]
        return self._accept(
            hire_id, hire, operation, required, _av_url(function, [("symbol", symbol)])
        )

    @public
    @deferred
    def quote(self, payload):
        return self._symbol_request(payload, "quote")

    @public
    @deferred
    def daily(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "daily")
        if refusal is not None:
            return refusal
        try:
            symbol = _symbol(req.get("symbol"))
        except ValueError as exc:
            return self._refuse("daily", str(exc))
        outputsize = str(req.get("outputsize") or "compact").strip().lower()
        if outputsize not in OUTPUTSIZES:
            return self._refuse(
                "daily", "outputsize must be one of %s" % ", ".join(OUTPUTSIZES)
            )
        function, required = FUNCTIONS["daily"]
        url = _av_url(function, [("symbol", symbol), ("outputsize", outputsize)])
        return self._accept(hire_id, hire, "daily", required, url)

    @public
    @deferred
    def overview(self, payload):
        return self._symbol_request(payload, "overview")

    def _accept(self, hire_id, hire, operation, required, url):
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
                {
                    "ok": False,
                    "error": "request capacity is full; retry after results expire",
                }
            )
        retained_for_hire = sum(
            1
            for request_id in ids
            if (self.storage.get("request:" + str(request_id)) or {}).get("hire_id")
            == hire_id
        )
        if retained_for_hire >= MAX_RETAINED_PER_HIRE:
            return codec.encode(
                {
                    "ok": False,
                    "error": "this hire's retained request capacity is full",
                }
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
            "operation": operation,
            "required": required,
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        job = {
            "url": url,
            "method": "GET",
            "headers": {"Accept": "application/json", "User-Agent": USER_AGENT},
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True,
            "request_id": request_id,
            "hire_id": hire_id,
            "operation": operation,
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
        operation = record["operation"]
        if int(runtime.get_block_height()) >= int(record["deadline_height"]):
            output = {
                "ok": False,
                "operation": operation,
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            if _is_vendor_error(decoded):
                output = {
                    "ok": False,
                    "operation": operation,
                    "error": _vendor_error(decoded),
                }
            elif (
                record["required"] not in decoded
                or (
                    operation in ("quote", "daily")
                    and (
                        not isinstance(decoded.get(record["required"]), dict)
                        or not decoded.get(record["required"])
                    )
                )
                or (
                    operation == "overview"
                    and (
                        not isinstance(decoded.get(record["required"]), str)
                        or not decoded.get(record["required"])
                    )
                )
            ):
                output = {
                    "ok": False,
                    "operation": operation,
                    "error": "unexpected %s response from alphavantage" % operation,
                }
            else:
                data = _limited_response(decoded, API_BYTE_BUDGET)
                if (
                    not isinstance(data, dict)
                    or data.get("error") == "non-serializable response"
                    or set(data.keys()) == {"_cowboy"}
                ):
                    output = {
                        "ok": False,
                        "operation": operation,
                        "error": "result could not be bounded under the byte budget",
                    }
                else:
                    output = {"ok": True, "operation": operation, "data": data}
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
                "operation": operation,
                "error": "result exceeds the encoded result byte budget",
            }
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # metadata only (see _refuse) — never persist vendor `data` in KEY_LAST_OUTPUT.
        self.storage[KEY_LAST_OUTPUT] = {"ok": output["ok"], "operation": operation}
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "operation": operation,
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
                "slot": "market-data",
                "handlers": [
                    "quote",
                    "daily",
                    "overview",
                ],
                "poll_handler": "get_result",
                "returns": "alphavantage-async-result",
                "symbol_input": {"hire_id": "int", "symbol": "str"},
                "daily_input": {
                    "hire_id": "int",
                    "symbol": "str",
                    "outputsize": "compact?",
                },
                "output": "acceptance record; get_result returns vendor data under output.data",
                # Excluded because their whole response exceeds the runner result
                # transport cap (the continuation would park): financial statements
                # ~122KB, news ~125KB, monthly full history; plus intraday, weekly/
                # daily full history, fx/crypto, technical indicators, listing
                # status, realtime options.
                "excluded": [
                    "income_statement",
                    "balance_sheet",
                    "cash_flow",
                    "news_sentiment",
                    "monthly_time_series",
                    "weekly_full_history",
                    "daily_full_history",
                    "intraday",
                    "fx_and_crypto",
                    "technical_indicators",
                    "listing_status",
                    "realtime_options",
                ],
                "hire_activation": {"protocol": "store-admin-v1"},
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
    return AlphaVantageWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def quote(payload):
    return _get_actor().quote(payload)


def daily(payload):
    return _get_actor().daily(payload)


def overview(payload):
    return _get_actor().overview(payload)


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
