"""Hire-gated Anchor Browser account-usage actor."""

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


VENDOR = "anchor"
ACTOR_VERSION = "1.0.0"
BILLING_ENDPOINT = "https://api.anchorbrowser.io/v1/billing"
SECRET_KEY = "ANCHOR_API_KEY"
AUTH_HEADER = "anchor-api-key"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

GRANULARITIES = ("day", "week", "month")
MAX_REQUEST_BYTES = 16_384
SUPPORTED_INPUT_FIELDS = ("hire_id", "granularity")

# Anchor endpoints intentionally NOT wrapped, with the reason each is unsafe for
# a bounded, synchronous, on-chain Store wrapper. See README for the full audit.
EXCLUDED_ENDPOINTS = {
    "POST /v1/tools/fetch/webpage": (
        "Web Unlocker returns raw full-page text/html with no server-side length "
        "bound; a page routinely exceeds the runner->actor transport cap and the "
        "continuation parks (client-side trimming runs after transport)"
    ),
    "POST /v1/screenshot": "returns binary image data, unbounded and non-JSON",
    "GET /v1/sessions/{id}/pdf": "returns a binary PDF, unbounded and non-JSON",
    "GET /v1/sessions/{id}/recordings/primary": "returns a video file, unbounded",
    "POST /v1/sessions": (
        "returns a live cdp_url WebSocket credential (public on chain) and needs a "
        "persistent socket an on-chain actor cannot hold; also account-global"
    ),
    "POST /v1/sessions/async": "same session/CDP credential and socket constraints",
    "GET /v1/sessions/{id}": "exposes the session cdp_url credential; account-global",
    "POST /v1/perform-web-task": (
        "agentic and long-running (returns a workflow_id to poll); may exceed the "
        "runner synchronous window and its result is potentially unbounded"
    ),
    "POST /v2/tasks/{id}/run": "agentic long-running create-then-poll; unbounded result",
    "* /v1/profiles": "persistent account-global browser identity state, not hire-isolated",
    "* /v1/identities": "persistent account-global identity; some routes mint identity tokens",
    "* /v1/applications": "account-global identity/credential configuration",
    "* /v1/webhooks": "account-global config; creation returns a signing secret",
    "* /v1/extensions": "account-global multipart artifacts",
    "* /v1/certificates": "account-global credentials",
    "* /v1/integrations": "account-global integration configuration",
    "GET /v1/events/{name}": "blocking wait-for-event; exceeds the synchronous window",
    "GET /v1/billing (hour granularity, custom window)": (
        "unbounded usage time-series; only day/week/month over the default window is "
        "exposed so the response stays bounded"
    ),
}

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
        return {"data": result}
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

    def candidates(node, path=()):
        strings = []
        lists = []
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "_cowboy":
                    continue
                child_strings, child_lists = candidates(child, path + (key,))
                strings.extend(child_strings)
                lists.extend(child_lists)
        elif isinstance(node, list):
            if len(node) > 1:
                lists.append((len(codec.encode(node)), path))
            for index, child in enumerate(node):
                child_strings, child_lists = candidates(child, path + (index,))
                strings.extend(child_strings)
                lists.extend(child_lists)
        elif isinstance(node, str) and node:
            strings.append((len(node.encode("utf-8")), path))
        return strings, lists

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
        strings, lists = candidates(out)
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
        if lists:
            _, path = max(lists, key=lambda item: item[0])
            node = at(path)
            del node[max(1, len(node) // 2):]
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


def _declaration_id(value):
    text = str(value or "").strip().lower()
    try:
        raw = bytes.fromhex(text[2:] if text.startswith("0x") else text)
    except ValueError as exc:
        raise ValueError("declaration_id must be a 32-byte hex value") from exc
    if len(raw) != 32:
        raise ValueError("declaration_id must be a 32-byte hex value")
    return "0x" + raw.hex()


def _choice(value, field, choices):
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    value = value.strip().lower()
    if value not in choices:
        raise ValueError("%s must be one of: %s" % (field, ", ".join(choices)))
    return value


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "anchor request failed"
    error = decoded.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message[:500]
        code = error.get("code")
        if code is not None:
            return ("anchor error code %s" % code)[:500]
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
    if "raw" in decoded:
        return "anchor returned a non-JSON response"
    return "anchor request failed"


def _is_vendor_error(decoded):
    return (
        not isinstance(decoded, dict)
        or "error" in decoded
        or any(key in decoded for key in ("detail", "raw", "raw_text"))
    )


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _billing_summary(decoded):
    """Return Anchor's billing summary object.

    The API docs nest the summary (credits / credits_used / tier / ...) under a
    top-level ``data`` object, but the live API currently returns those same
    fields at the top level with no ``data`` wrapper. Accept either so the
    wrapper survives that documented-vs-live drift: prefer a ``data`` object
    that actually carries the summary, else treat ``decoded`` itself as it.
    """
    if isinstance(decoded, dict):
        nested = decoded.get("data")
        if isinstance(nested, dict) and _is_number(nested.get("credits")):
            return nested
    return decoded


def _valid_usage_response(decoded):
    if not isinstance(decoded, dict):
        return False
    summary = _billing_summary(decoded)
    if not isinstance(summary, dict):
        return False
    if not _is_number(summary.get("credits")):
        return False
    if not _is_number(summary.get("credits_used")):
        return False
    tier = summary.get("tier")
    if tier is not None and not isinstance(tier, str):
        return False
    usage = decoded.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            return False
        series = usage.get("data")
        if series is not None and not isinstance(series, list):
            return False
    return True


def _usage_query(req):
    unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS)
    if unsupported:
        raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
    query = []
    if "granularity" in req:
        query.append("granularity=" + _choice(req["granularity"], "granularity", GRANULARITIES))
    return query


@actor
class AnchorBrowserWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Anchor Browser Usage"
        self.storage["__description__"] = (
            "Bounded Anchor Browser account usage for registered Store Hires. "
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
        # metadata only — KEY_LAST_OUTPUT never carries vendor data (M2)
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    @public
    @deferred
    def usage(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "usage")
        if refusal is not None:
            return refusal
        try:
            query = _usage_query(req)
        except ValueError as exc:
            return self._refuse("usage", str(exc))
        return self._accept(hire_id, hire, query)

    def _accept(self, hire_id, hire, query):
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
            "operation": "usage",
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        url = BILLING_ENDPOINT + ("?" + "&".join(query) if query else "")
        # GET carries NO body and NO Content-Type — a body kwarg on a GET job is
        # rejected on chain (E1604) and reverts before the request is recorded.
        job = {
            "url": url,
            "method": "GET",
            "headers": {
                "Accept": "application/json",
                AUTH_HEADER: "${%s}" % SECRET_KEY,
                "User-Agent": USER_AGENT,
            },
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True,
            "request_id": request_id,
            "hire_id": hire_id,
            "operation": "usage",
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
                "operation": "usage",
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            if _is_vendor_error(decoded):
                output = {
                    "ok": False,
                    "operation": "usage",
                    "error": _vendor_error(decoded),
                }
            elif not _valid_usage_response(decoded):
                output = {
                    "ok": False,
                    "operation": "usage",
                    "error": "unexpected usage response from anchor",
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
                        "operation": "usage",
                        "error": "result could not be bounded under the byte budget",
                    }
                else:
                    output = {"ok": True, "operation": "usage", "data": data}
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
                "operation": "usage",
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
                "operation": "usage",
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
                "slot": "usage",
                "handlers": ["usage"],
                "poll_handler": "get_result",
                "returns": "anchor-usage-async-result",
                "input": {
                    "hire_id": "int",
                    "granularity": "day|week|month?",
                },
                "output": "acceptance record; get_result returns vendor data under output.data",
                "hire_activation": {"protocol": "store-admin-v1"},
                "endpoint": "GET " + BILLING_ENDPOINT,
                "auth_header": AUTH_HEADER,
                "granularities": list(GRANULARITIES),
                "excluded_endpoints": sorted(EXCLUDED_ENDPOINTS),
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
                "endpoint": BILLING_ENDPOINT,
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
    return AnchorBrowserWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def usage(payload):
    return _get_actor().usage(payload)


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
