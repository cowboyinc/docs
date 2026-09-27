"""Exa Search — Store wrapper actor (search slot).
Handlers:
  init            - one-time setup (secret account, registrar, defaults)
  register_hire   - Store registrar records a verified Free Hire
  search          - accept a hire-scoped asynchronous search request
  api             - accept one fixed Exa API operation
  get_result      - the registered hirer reads its request result
  capability      - machine-readable description of what this wrapper sells
  get_config      - current limits
  set_limits      - owner-only tuning, clamped to the byte budget
"""

import json
import re
from cowboy_sdk import (
    Address,
    ContinuationNotFoundError,
    OWNER,
    actor,
    callable_by,
    capture,
    codec,
    continuation,
    public,
    runner,
    runtime,
)


# --- vendor block -----------------------------------------------------------
# Everything vendor-specific lives here.

VENDOR = "exa"
ACTOR_VERSION = "2.1.0"
VENDOR_ENDPOINT = "https://api.exa.ai/search"
API_ROOT = "https://api.exa.ai"
SECRET_KEY = "EXA_API_KEY"          # CBSS name; released per-call to the runner
AUTH_HEADER = "x-api-key"           # runner substitutes ${...} in url, headers AND body


# --- result-size budget -----------------------------------------------------
# Keep stored responses within 40KB, below the current Node job envelope's
# 64KiB return allowance. Request shaping cannot bound unknown vendor fields;
# the request lifecycle recovers when an oversized reply produces no callback.

RESULT_BYTE_BUDGET = 40_000
SAFETY_MARGIN = 0.6                 # target ~24KB, leaving room for envelopes
RESULT_OVERHEAD_BYTES = 2_200       # bounded title + URL + date + JSON/CBOR fields
MAX_EVENT_PAYLOAD_BYTES = 4_096     # Node's MAX_EVENT_PAYLOAD_BYTES
# With maximum u64 ids, offset 39,999, total 40,000, and all event fields,
# 4,015 data bytes encode to exactly 4,096 canonical-CBOR bytes. One more data
# byte would exceed Node's event limit.
MAX_RESULT_CHUNK_BYTES = 4_015

MAX_RESULTS_CEILING = 10
MAX_DOMAINS = 20
SEARCH_TYPES = ("auto", "fast", "instant")
CATEGORIES = ("company", "research paper", "news", "personal site", "financial report", "people")
DEFAULT_NUM_RESULTS = 5
DEFAULT_CHARS_PER_RESULT = 1_200
MIN_CHARS_PER_RESULT = 200
MAX_QUERY_BYTES = 512
MAX_TITLE_BYTES = 800
MAX_PUBLISHED_BYTES = 160
KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_HIRE_PREFIX = "hire:"
KEY_CALLS = "call_count"
KEY_NUM_RESULTS = "cfg:num_results"
KEY_CHARS = "cfg:chars_per_result"
KEY_REQUEST_IDS = "request_ids"
KEY_REQUEST_PREFIX = "request:"

# A request does not create an unbounded continuation. The actor keeps at most
# this many request records and active continuations. This stays well below the
# SDK's hard limit of 100 active continuations. Each final result is also
# bounded by RESULT_BYTE_BUDGET, so the retained set stays below the actor's
# base 1 MiB storage quota.
MAX_RETAINED_REQUESTS = 16

# Hire records use one storage key each. The former single `hires` map crossed
# Node's 64 KiB per-value limit after a few hundred normal records. This fixed
# count bounds total Actor storage while each record stays far below that
# per-value limit.
MAX_REGISTERED_HIRES = 512

# Node currently gives HTTP jobs 30-block runner-selection windows and can
# reselect three times. This is an actor-owned recovery threshold after those
# windows, with margin. It is not an HTTP timeout and it does not run at an
# exact wall-clock time: a later search or result read performs the cleanup.
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150


API_OPERATIONS = {
    "search": ("POST", "/search"), "contents": ("POST", "/contents"),
    "answer": ("POST", "/answer"), "find_similar": ("POST", "/findSimilar"),
    "create_monitor": ("POST", "/monitors"), "list_monitors": ("GET", "/monitors"),
    "batch_monitors": ("POST", "/monitors/batch"),
    "get_monitor": ("GET", "/monitors/{id}"), "update_monitor": ("PATCH", "/monitors/{id}"),
    "delete_monitor": ("DELETE", "/monitors/{id}"),
    "trigger_monitor": ("POST", "/monitors/{id}/trigger"),
    "list_runs": ("GET", "/monitors/{id}/runs"),
    "get_run": ("GET", "/monitors/{id}/runs/{runId}"),
    "create_agent_run": ("POST", "/agent/runs"), "list_agent_runs": ("GET", "/agent/runs"),
    "get_agent_run": ("GET", "/agent/runs/{id}"),
    "delete_agent_run": ("DELETE", "/agent/runs/{id}"),
    "cancel_agent_run": ("POST", "/agent/runs/{id}/cancel"),
    "stop_agent_run": ("POST", "/agent/runs/{id}/stop"),
    "list_agent_run_events": ("GET", "/agent/runs/{id}/events"),
    "create_batch": ("POST", "/batches"), "list_batches": ("GET", "/batches"),
    "get_batch": ("GET", "/batches/{id}"), "delete_batch": ("DELETE", "/batches/{id}"),
    "cancel_batch": ("POST", "/batches/{id}/cancel"),
    "get_team": ("GET", "/v0/teams/me"),
    "create_webset": ("POST", "/v0/websets"), "list_websets": ("GET", "/v0/websets"),
    "get_webset": ("GET", "/v0/websets/{id}"), "update_webset": ("POST", "/v0/websets/{id}"),
    "delete_webset": ("DELETE", "/v0/websets/{id}"),
    "cancel_webset": ("POST", "/v0/websets/{id}/cancel"),
    "preview_webset": ("POST", "/v0/websets/preview"),
    "get_webset_item": ("GET", "/v0/websets/{webset}/items/{id}"),
    "delete_webset_item": ("DELETE", "/v0/websets/{webset}/items/{id}"),
    "list_webset_items": ("GET", "/v0/websets/{webset}/items"),
    "create_enrichment": ("POST", "/v0/websets/{webset}/enrichments"),
    "update_enrichment": ("PATCH", "/v0/websets/{webset}/enrichments/{id}"),
    "get_enrichment": ("GET", "/v0/websets/{webset}/enrichments/{id}"),
    "delete_enrichment": ("DELETE", "/v0/websets/{webset}/enrichments/{id}"),
    "cancel_enrichment": ("POST", "/v0/websets/{webset}/enrichments/{id}/cancel"),
    "create_webhook": ("POST", "/v0/webhooks"), "list_webhooks": ("GET", "/v0/webhooks"),
    "get_webhook": ("GET", "/v0/webhooks/{id}"),
    "update_webhook": ("PATCH", "/v0/webhooks/{id}"),
    "delete_webhook": ("DELETE", "/v0/webhooks/{id}"),
    "list_webhook_attempts": ("GET", "/v0/webhooks/{id}/attempts"),
    "list_events": ("GET", "/v0/events"), "get_event": ("GET", "/v0/events/{id}"),
    "create_webset_search": ("POST", "/v0/websets/{webset}/searches"),
    "get_webset_search": ("GET", "/v0/websets/{webset}/searches/{id}"),
    "cancel_webset_search": ("POST", "/v0/websets/{webset}/searches/{id}/cancel"),
    "create_v0_monitor": ("POST", "/v0/monitors"),
    "list_v0_monitors": ("GET", "/v0/monitors"),
    "get_v0_monitor": ("GET", "/v0/monitors/{id}"),
    "update_v0_monitor": ("PATCH", "/v0/monitors/{id}"),
    "delete_v0_monitor": ("DELETE", "/v0/monitors/{id}"),
    "list_v0_monitor_runs": ("GET", "/v0/monitors/{monitor}/runs"),
    "get_v0_monitor_run": ("GET", "/v0/monitors/{monitor}/runs/{id}"),
    "create_import": ("POST", "/v0/imports"), "list_imports": ("GET", "/v0/imports"),
    "get_import": ("GET", "/v0/imports/{id}"), "update_import": ("PATCH", "/v0/imports/{id}"),
    "delete_import": ("DELETE", "/v0/imports/{id}"),
}


def _parse(payload):
    """Decode an inbound handler payload into a dict."""
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
        except Exception:
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


def _decode_result(result):
    """Unwrap the SDK result, then decode the HTTP response body."""
    if isinstance(result, runner.RunnerResult):
        result = result.data
    if isinstance(result, (bytes, bytearray)):
        result = bytes(result).decode("utf-8", "replace")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {"raw_text": result}
    if isinstance(result, list):
        return {"results": result}
    return result if isinstance(result, dict) else {"error": "invalid vendor response"}


def _vendor_error(decoded):
    """Best-effort error text from a failed vendor reply."""
    if not isinstance(decoded, dict):
        return "vendor request failed"
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for inner in ("message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text
    if "raw" in decoded:
        return "vendor returned a non-JSON response (%s)" % decoded.get(
            "content_type", "unknown content-type"
        )
    return "vendor request failed"


def _clamp(value, low, high, fallback):
    try:
        n = int(value)
    except Exception:
        return fallback
    return max(low, min(high, n))


def _truncate_utf8(value, max_bytes):
    """Return text whose UTF-8 encoding is no larger than max_bytes."""
    return str(value or "").encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _address_hex(value, field):
    """Return the SDK's canonical lower-case address representation."""
    try:
        return Address(value).to_hex(checksum=False)
    except Exception as exc:
        raise ValueError("%s must be a 20-byte address" % field) from exc


def _sender_hex():
    return _address_hex(runtime.get_sender(), "sender")


def _bounded_int(value, field, low, high):
    if isinstance(value, bool):
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


def _result_chunk_event(request_id, hire_id, offset, total_bytes, data):
    return codec.encode(
        {
            "request_id": request_id,
            "hire_id": hire_id,
            "offset": offset,
            "length": len(data),
            "total_bytes": total_bytes,
            "data": data,
        }
    )


def _domains(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % field)
    if len(value) > MAX_DOMAINS:
        raise ValueError("%s supports at most %d domains" % (field, MAX_DOMAINS))
    out = []
    pattern = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    for item in value:
        domain = str(item).strip().lower() if isinstance(item, str) else ""
        if len(domain) > 253 or re.fullmatch(pattern, domain) is None:
            raise ValueError("%s contains an invalid domain" % field)
        if domain not in out:
            out.append(domain)
    return out


def _quote(value):
    # urllib.parse is not on the PVM import allowlist.
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(chr(c) if c in safe else "%%%02X" % c for c in value.encode("utf-8"))


def _component(value, field):
    value = str(value or "").strip()
    if not value or value in (".", "..") or len(value) > 300 or any(c in value for c in "/?#\r\n"):
        raise ValueError("%s is invalid" % field)
    return _quote(value)


def _query_string(value):
    if value is None:
        return ""
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError("query_params must be an object with nonempty string keys")
    parts = []
    for key in sorted(value):
        items = value[key] if isinstance(value[key], list) else [value[key]]
        for item in items:
            if isinstance(item, bool):
                item = "true" if item else "false"
            elif not isinstance(item, (str, int, float)):
                raise ValueError("query parameter values must be scalar or arrays")
            parts.append(_quote(key) + "=" + _quote(str(item)))
    query = ("?" + "&".join(parts)) if parts else ""
    if len(query) > 28_000:
        raise ValueError("query exceeds 28000 bytes")
    return query


def _reject_secret_templates(req):
    # The Runner substitutes templates in HTTP bodies as well as headers.
    # Check decoded input so escaped JSON characters cannot bypass this boundary.
    if "${" in json.dumps(req):
        raise ValueError("secret templates are not allowed in caller input")


def _api_body(req, operation, path_fields=()):
    body = dict(req)
    for key in ("hire_id", "operation", "query_params") + tuple(path_fields):
        body.pop(key, None)
    if operation in ("search", "answer"):
        body["stream"] = False
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 28_000:
        raise ValueError("request exceeds 28000 bytes")
    return encoded


@actor
class ExaSearchWrapper:
    """Shares one Exa account among registered Hires for one trusted tenant."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")

        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar_raw = cfg.get("store_registrar") or owner
        registrar = _address_hex(registrar_raw, "store_registrar")
        self.storage["__name__"] = "Exa Search"
        self.storage["__description__"] = (
            "Semantic web search wrapper. Query in, ranked sources + bounded "
            "page contents out. One trusted tenant shares the Exa account. "
            "Vendor key held in CBSS."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []

        # An ignored init payload deploys a healthy-looking actor on defaults,
        # so read the overrides explicitly and clamp them here.
        num, chars = self._fit_budget(
            cfg.get("num_results", DEFAULT_NUM_RESULTS),
            cfg.get("chars_per_result", DEFAULT_CHARS_PER_RESULT),
        )
        self.storage[KEY_NUM_RESULTS] = num
        self.storage[KEY_CHARS] = chars

        runtime.emit_event(
            "wrapper.init",
            {
                "vendor": VENDOR,
                "owner": owner,
                "store_registrar": registrar,
                "num_results": num,
                "chars_per_result": chars,
            },
        )
        return codec.encode({"ok": True, "vendor": VENDOR})

    def _require_registrar(self):
        if _sender_hex() != self.storage.get(KEY_REGISTRAR, ""):
            raise ValueError("register_hire: Store registrar required")

    def _hire_key(self, hire_id):
        return KEY_HIRE_PREFIX + str(int(hire_id))

    def _hire(self, hire_id):
        return self.storage.get(self._hire_key(hire_id))

    @public
    def register_hire(self, payload):
        """Record a finalized Hire that Store Admin verified from chain state.

        A Hire id is immutable here. An exact retry is idempotent. The Actor
        never accepts Runner pins from a search caller.
        """
        self._require_registrar()
        cfg = _parse(payload)
        hire_id = _bounded_int(cfg.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hirer = _address_hex(cfg.get("hirer"), "hirer")
        declaration_id = _declaration_id(cfg.get("declaration_id"))
        version = _bounded_int(cfg.get("version"), "version", 1, 2**32 - 1)
        artifact_id = _bounded_int(cfg.get("artifact_id"), "artifact_id", 0, 2**32 - 1)

        record = {
            "hire_id": hire_id,
            "hirer": hirer,
            "declaration_id": declaration_id,
            "version": version,
            "artifact_id": artifact_id,
        }
        key = self._hire_key(hire_id)
        existing = self.storage.get(key)
        if existing:
            if existing != record:
                raise ValueError("register_hire: hire_id is already registered with different data")
            return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})

        hire_count = int(self.storage.get(KEY_HIRE_COUNT, 0))
        if hire_count >= MAX_REGISTERED_HIRES:
            raise ValueError("register_hire: registered Hire capacity is full")

        self.storage[key] = record
        self.storage[KEY_HIRE_COUNT] = hire_count + 1
        runtime.emit_event(
            "wrapper.hire_registered",
            {"hire_id": hire_id, "hirer": hirer},
        )
        return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})

    # --- the hireable capability -------------------------------------------

    @public
    def search(self, payload):
        """Accept a bounded search; read its result later with get_result."""
        req = _parse(payload)
        _reject_secret_templates(req)
        query = str(req.get("query", "")).strip()
        if not query:
            return codec.encode({"ok": False, "error": "query is required"})
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            return codec.encode({
                "ok": False,
                "error": "query must be at most %d UTF-8 bytes" % MAX_QUERY_BYTES,
            })
        num, chars = self._fit_budget(
            req.get("num_results", self.storage.get(KEY_NUM_RESULTS, DEFAULT_NUM_RESULTS)),
            req.get("chars_per_result", self.storage.get(KEY_CHARS, DEFAULT_CHARS_PER_RESULT)),
        )
        return self._accept_request(
            req.get("hire_id"),
            {"query": query, "num_results": num, "chars_per_result": chars},
            {
                "url": VENDOR_ENDPOINT,
                "method": "POST",
                "body": self._build_request(query, num, chars, self._search_options(req)),
            },
        )

    @public
    def api(self, payload):
        """Accept a fixed Exa API operation through the same result lifecycle."""
        req = _parse(payload)
        _reject_secret_templates(req)
        operation = str(req.get("operation", "")).strip()
        spec = API_OPERATIONS.get(operation)
        if spec is None:
            raise ValueError("unsupported operation")
        method, path = spec
        path_fields = []
        for field in ("id", "runId", "webset", "monitor"):
            token = "{" + field + "}"
            if token in path:
                path_fields.append(field)
                path = path.replace(token, _component(req.get(field), field))
        url = API_ROOT + path + _query_string(req.get("query_params"))
        job = {"url": url, "method": method}
        if method in ("POST", "PATCH"):
            job["body"] = _api_body(req, operation, path_fields)
        return self._accept_request(req.get("hire_id"), {"operation": operation}, job)

    def _accept_request(self, hire_id, fields, job):
        hire_id = _bounded_int(hire_id, "hire_id", 1, 2**64 - 1)
        hire = self._hire(hire_id)
        if not hire:
            return codec.encode({
                "ok": False,
                "error": "hire %d is not activated on this actor" % hire_id,
                "hint": "the Store registrar must call register_hire for this hire",
            })
        buyer = _sender_hex()
        if buyer != hire["hirer"]:
            raise ValueError("caller is not the registered hirer")

        self._reap_stale_requests()
        cid = continuation.new_cid(self, "_run_request")
        for request_id in self.storage.get(KEY_REQUEST_IDS, []):
            active = self._request(request_id)
            if active and active.get("continuation_cid") == cid.hex():
                return codec.encode({
                    "ok": False, "error": "transaction already has a pending request",
                })
        if not self._make_request_room():
            return codec.encode({
                "ok": False,
                "error": "request capacity is full; retry after retained results expire",
            })

        request_id = int(self.storage.get(KEY_CALLS, 0)) + 1
        now = int(runtime.get_block_height())
        record = dict(
            fields, request_id=request_id, hire_id=hire_id, buyer=buyer,
            status="pending", created_height=now,
            deadline_height=now + REQUEST_LIFETIME_BLOCKS,
            continuation_cid=cid.hex(),
        )
        self.storage[KEY_CALLS] = request_id
        self.storage[self._request_key(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = list(self.storage.get(KEY_REQUEST_IDS, [])) + [request_id]
        self._run_request(codec.encode(dict(job, request_id=request_id, hire=hire)))
        runtime.emit_event("wrapper.search_accepted", {
            "request_id": request_id, "hire_id": hire_id,
            "deadline_height": record["deadline_height"],
        })
        return codec.encode({
            "ok": True, "request_id": request_id,
            "status": "pending", "poll_handler": "get_result",
        })

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        ctx = capture()
        ctx.request_id = req["request_id"]
        # Runtime cross-account refs use [account, key]; owner:KEY is manifest syntax.
        secret_ref = [self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]
        body = {"body": req["body"]} if "body" in req else {}
        result = await runner.http(
            url=req["url"], method=req["method"], **body,
            headers={
                "Accept": "application/json", "Content-Type": "application/json",
                AUTH_HEADER: "${%s}" % SECRET_KEY,
            },
            secrets=[secret_ref],
            hire={
                "hire_id": req["hire"]["hire_id"],
                "declaration_id": req["hire"]["declaration_id"],
                "version": req["hire"]["version"],
                "artifact_id": req["hire"]["artifact_id"],
            },
        )
        return self._finish(ctx.request_id, result)

    def _finish(self, request_id, result):
        record = self._request(request_id)
        if not record or record.get("status") != "pending":
            runtime.emit_event("wrapper.late_result", {"request_id": request_id})
            return codec.encode({"ok": False, "error": "request is no longer pending"})

        now = int(runtime.get_block_height())
        if now >= record["deadline_height"]:
            self._mark_request_expired(record, now)
            runtime.emit_event("wrapper.late_result", {"request_id": request_id})
            return codec.encode(self._response(record))

        decoded = _decode_result(result)

        # The Runner returns the body without HTTP status, so use the vendor shape.
        is_api = "operation" in record
        failed = any(key in decoded for key in ("error", "detail", "raw", "raw_text"))
        if not is_api:
            failed = failed or not isinstance(decoded.get("results"), list)
        if failed:
            record["status"] = "error"
            record["error"] = _vendor_error(decoded)[:500]
        elif is_api:
            serialized = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > 28_000:
                decoded = {"truncated": True, "raw_json": _truncate_utf8(serialized, 28_000)}
            record["status"] = "ok"
            record["data"] = decoded
        else:
            record["status"] = "ok"
            record["results"] = self._shape_results(
                decoded, record["num_results"], record["chars_per_result"],
            )
        record["finished_height"] = now
        record["expires_height"] = now + RESULT_RETENTION_BLOCKS
        record.pop("continuation_cid", None)
        encoded = codec.encode(self._response(record))
        if len(encoded) > RESULT_BYTE_BUDGET:
            record.pop("data", None)
            record.pop("results", None)
            record["status"] = "error"
            record["error"] = "vendor result exceeds the encoded result byte budget"
            encoded = codec.encode(self._response(record))
            failed = True
        self.storage[self._request_key(request_id)] = record
        if failed:
            runtime.emit_event("wrapper.error", {
                "request_id": request_id, "vendor": VENDOR, "reason": record["error"][:200],
            })
        elif is_api:
            runtime.emit_event("wrapper.api", {
                "request_id": request_id, "vendor": VENDOR, "operation": record["operation"],
            })
        else:
            runtime.emit_event("wrapper.search", {
                "request_id": request_id, "vendor": VENDOR, "results": len(record["results"]),
            })
        return encoded

    def _response(self, record):
        status = record["status"]
        response = {
            "ok": status != "error", "request_id": record["request_id"], "status": status,
        }
        if "operation" in record:
            response["operation"] = record["operation"]
        if status == "pending":
            response["deadline_height"] = record["deadline_height"]
        else:
            response["expires_height"] = record["expires_height"]
            if status == "error":
                response["error"] = record["error"]
            elif "operation" in record:
                response["data"] = record["data"]
            else:
                response.update(query=record["query"], results=record["results"])
        return response

    # --- request lifecycle -------------------------------------------------

    def _request_key(self, request_id):
        return KEY_REQUEST_PREFIX + str(int(request_id))

    def _request(self, request_id):
        return self.storage.get(self._request_key(request_id))

    def _make_request_room(self):
        """Remove only records whose completed-result retention has ended."""
        request_ids = list(self.storage.get(KEY_REQUEST_IDS, []))
        if len(request_ids) < MAX_RETAINED_REQUESTS:
            return True
        now = int(runtime.get_block_height())
        for request_id in request_ids:
            record = self._request(request_id)
            if record and (record["status"] == "pending" or now < record["expires_height"]):
                continue
            if record:
                del self.storage[self._request_key(request_id)]
            request_ids.remove(request_id)
            self.storage[KEY_REQUEST_IDS] = request_ids
            return True
        return False

    def _mark_request_expired(self, record, now):
        """Persist the terminal lifecycle result for one pending request."""
        record["status"] = "error"
        record["error"] = (
            "request result did not arrive before the request lifecycle deadline"
        )
        record["finished_height"] = now
        record["expires_height"] = now + RESULT_RETENTION_BLOCKS
        record.pop("continuation_cid", None)
        self.storage[self._request_key(record["request_id"])] = record

    def _reap_stale_requests(self):
        """Release expired compiler continuations on a later actor call.

        This does not cancel or time out the Runner job. It only ends this
        actor's bounded request lifecycle and fences a later callback by
        deleting the compiler-owned CID before it can resume.
        """
        now = int(runtime.get_block_height())
        request_ids = []
        for request_id in self.storage.get(KEY_REQUEST_IDS, []):
            record = self._request(request_id)
            if not record:
                continue
            request_ids.append(request_id)
            if (
                record.get("status") == "pending"
                and now >= int(record.get("deadline_height", 0))
            ):
                cid_hex = record.get("continuation_cid", "")
                if cid_hex:
                    continuation.delete_cont(bytes.fromhex(cid_hex))
                self._mark_request_expired(record, now)
                runtime.emit_event(
                    "wrapper.search_reclaimed",
                    {"request_id": request_id, "deadline_height": record["deadline_height"]},
                )
        if request_ids != list(self.storage.get(KEY_REQUEST_IDS, [])):
            self.storage[KEY_REQUEST_IDS] = request_ids

    @public
    def get_result(self, payload):
        """Read one result as the buyer and Hire that created the request.

        Normal Actor callers receive the full return value. A signed EOA call
        can also request one receipt chunk because the current CLI does not
        display handler return bytes. Every chunk call repeats the same buyer
        and Hire authorization before it emits data.
        """
        req = _parse(payload)
        request_id = _bounded_int(
            req.get("request_id"), "request_id", 1, 2**64 - 1
        )
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        self._reap_stale_requests()
        record = self._request(request_id)
        if (
            not record
            or record.get("buyer") != _sender_hex()
            or int(record.get("hire_id", 0)) != hire_id
        ):
            return codec.encode(
                {"ok": False, "error": "request not found for caller and hire"}
            )

        response = self._response(record)
        encoded = codec.encode(response)
        if len(encoded) > RESULT_BYTE_BUDGET:
            raise ValueError("get_result: encoded result exceeds the result byte budget")

        has_offset = "receipt_offset" in req
        has_length = "receipt_length" in req
        if has_offset != has_length:
            raise ValueError("get_result: receipt_offset and receipt_length are required together")
        if has_offset:
            offset = _bounded_int(
                req.get("receipt_offset"),
                "receipt_offset",
                0,
                RESULT_BYTE_BUDGET - 1,
            )
            length = _bounded_int(
                req.get("receipt_length"),
                "receipt_length",
                1,
                MAX_RESULT_CHUNK_BYTES,
            )
            if offset >= len(encoded):
                raise ValueError("get_result: receipt_offset is past the encoded result")
            data = encoded[offset : offset + length]
            event = _result_chunk_event(
                request_id, hire_id, offset, len(encoded), data
            )
            if len(event) > MAX_EVENT_PAYLOAD_BYTES:
                raise ValueError("get_result: result chunk exceeds the event byte limit")
            runtime.emit_event("wrapper.result_chunk", event)

        return encoded

    # --- vendor-shaped request / response -----------------------------------

    def _search_options(self, req):
        search_type = str(req.get("search_type", "auto")).strip().lower()
        if search_type not in SEARCH_TYPES:
            raise ValueError("search_type must be auto, fast, or instant")
        options = {"type": search_type}
        category = str(req.get("category", "")).strip().lower()
        if category and category not in CATEGORIES:
            raise ValueError("category is not supported")
        if category:
            options["category"] = category
        included = _domains(req.get("include_domains"), "include_domains")
        excluded = _domains(req.get("exclude_domains"), "exclude_domains")
        start = req.get("start_published_date")
        end = req.get("end_published_date")
        location = str(req.get("user_location", "")).strip().upper()
        if location and (len(location) != 2 or not location.isalpha() or not location.isascii()):
            raise ValueError("user_location must be a two-letter country code")
        if category in ("company", "people") and excluded:
            raise ValueError("company and people searches do not support exclusions")
        if category == "people" and included:
            raise ValueError("people searches do not support domain filters")
        for key, value in (("includeDomains", included), ("excludeDomains", excluded),
                           ("startPublishedDate", start), ("endPublishedDate", end),
                           ("userLocation", location)):
            if value:
                options[key] = value
        return options

    def _build_request(self, query, num, chars, options=None):
        """Build a response-bounded Exa search request."""
        body = {"query": query, "numResults": num,
                "contents": {"text": {"maxCharacters": chars}}}
        body.update(options or {"type": "auto"})
        return json.dumps(body).encode("utf-8")

    def _shape_results(self, decoded, num, chars=DEFAULT_CHARS_PER_RESULT):
        """Exa returns {"results": [{title, url, text, publishedDate, ...}]}."""
        raw = decoded.get("results")
        if not isinstance(raw, list):
            return []
        shaped = []
        for item in raw[:num]:
            if not isinstance(item, dict):
                continue
            shaped.append(
                {
                    "title": _truncate_utf8(item.get("title", ""), MAX_TITLE_BYTES),
                    "url": str(item.get("url") or ""),
                    "text": _truncate_utf8(item.get("text", ""), chars * 4),
                    "published": _truncate_utf8(
                        item.get("publishedDate", ""), MAX_PUBLISHED_BYTES
                    ),
                }
            )
        return shaped

    # --- budget -------------------------------------------------------------

    def _fit_budget(self, num_in, chars_in):
        """Bound documented Exa text fields within the response budget.

        A new or malformed vendor field can still exceed the cap. Shrink text
        first because callers care more about result count than text length.
        """
        num = _clamp(num_in, 1, MAX_RESULTS_CEILING, DEFAULT_NUM_RESULTS)
        chars = _clamp(chars_in, MIN_CHARS_PER_RESULT, 20_000, DEFAULT_CHARS_PER_RESULT)

        allowance = int(RESULT_BYTE_BUDGET * SAFETY_MARGIN)
        affordable = max(
            MIN_CHARS_PER_RESULT,
            ((allowance // max(1, num)) - RESULT_OVERHEAD_BYTES) // 4,
        )
        return num, min(chars, affordable)

    # --- introspection / config ---------------------------------------------

    @public
    def capability(self, payload):
        """What this wrapper sells, for discovery by hiring actors."""
        return codec.encode(
            {
                "vendor": VENDOR,
                "slot": "search",
                "handler": "search",
                "handlers": ["search", "api"],
                "api_operations": sorted(API_OPERATIONS),
                "input": {
                    "hire_id": "int",
                    "query": "str",
                    "num_results": "int?",
                    "chars_per_result": "int?",
                    "search_type": "auto|fast|instant?",
                    "category": "company|research paper|news|personal site|financial report|people?",
                    "include_domains": "str[]?",
                    "exclude_domains": "str[]?",
                    "start_published_date": "ISO-8601 UTC?",
                    "end_published_date": "ISO-8601 UTC?",
                    "user_location": "ISO-3166-1 alpha-2?",
                },
                "output": {
                    "request_id": "int",
                    "status": "pending",
                    "poll_handler": "get_result",
                },
                "result_handler": "get_result",
                "result_input": {
                    "hire_id": "int",
                    "request_id": "int",
                    "receipt_offset": "int?",
                    "receipt_length": "int?",
                },
                "result_output": {
                    "status": "pending|ok|error",
                    "operation": "str?",
                    "data": "any?",
                    "expires_height": "int?",
                    "results": [{"title": "str", "url": "str", "text": "str"}],
                },
                "hire_activation": {"protocol": "store-admin-v1"},
                "max_results": MAX_RESULTS_CEILING,
                "result_byte_budget": RESULT_BYTE_BUDGET,
                "max_result_chunk_bytes": MAX_RESULT_CHUNK_BYTES,
                "request_lifetime_blocks": REQUEST_LIFETIME_BLOCKS,
                "result_retention_blocks": RESULT_RETENTION_BLOCKS,
                "max_retained_requests": MAX_RETAINED_REQUESTS,
                "max_registered_hires": MAX_REGISTERED_HIRES,
            }
        )

    @public
    def get_config(self, payload):
        request_ids = self.storage.get(KEY_REQUEST_IDS, [])
        active_count = sum(
            1
            for request_id in request_ids
            if (self._request(request_id) or {}).get("status") == "pending"
        )
        return codec.encode(
            {
                "vendor": VENDOR,
                "num_results": self.storage.get(KEY_NUM_RESULTS, DEFAULT_NUM_RESULTS),
                "chars_per_result": self.storage.get(KEY_CHARS, DEFAULT_CHARS_PER_RESULT),
                "call_count": self.storage.get(KEY_CALLS, 0),
                "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                "registered_hires": self.storage.get(KEY_HIRE_COUNT, 0),
                "max_registered_hires": MAX_REGISTERED_HIRES,
                "active_requests": active_count,
                "retained_requests": len(request_ids),
                "request_lifetime_blocks": REQUEST_LIFETIME_BLOCKS,
                "result_retention_blocks": RESULT_RETENTION_BLOCKS,
            }
        )

    @callable_by(OWNER)
    def set_limits(self, payload):
        cfg = _parse(payload)
        num, chars = self._fit_budget(
            cfg.get("num_results", self.storage.get(KEY_NUM_RESULTS, DEFAULT_NUM_RESULTS)),
            cfg.get("chars_per_result", self.storage.get(KEY_CHARS, DEFAULT_CHARS_PER_RESULT)),
        )
        self.storage[KEY_NUM_RESULTS] = num
        self.storage[KEY_CHARS] = chars
        return codec.encode({"ok": True, "num_results": num, "chars_per_result": chars})

    @public
    def on_runner_result(self, payload):
        """FSM resume entrypoint for runner continuations."""
        if not _resume_is_self():
            return codec.encode({"ok": False, "error": "runner callback required"})
        try:
            return runner.handle_runner_result(self, payload)
        except ContinuationNotFoundError:
            runtime.emit_event("wrapper.late_result", {"handler": "on_runner_result"})
            return codec.encode({"ok": False, "error": "stale or duplicate callback"})


def _get_actor():
    """Fresh instance per handler call.

    The chain invokes module-level functions, not methods, so each wrapper
    below needs an instance. Construction is lazy because `@actor.__init__`
    calls `runtime.actor_address()` — a host call that belongs at invocation
    time, not import time. Nothing is lost by not caching: actor state lives
    in `self.storage` (on-chain), never on the instance.
    """
    return ExaSearchWrapper()


def init(payload):
    return _get_actor().init(payload)


def api(payload):
    return _get_actor().api(payload)


def search(payload):
    return _get_actor().search(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def _resume_is_self():
    try:
        return _sender_hex() == _address_hex(runtime.get_actor_address(), "actor")
    except Exception:
        return False


def _run_request__resume(payload):
    if not _resume_is_self():
        return codec.encode({"ok": False, "error": "runner callback required"})
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        runtime.emit_event("wrapper.late_result", {"handler": "_run_request__resume"})
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})


def capability(payload):
    return _get_actor().capability(payload)


def get_result(payload):
    return _get_actor().get_result(payload)


def get_config(payload):
    return _get_actor().get_config(payload)


def set_limits(payload):
    return _get_actor().set_limits(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)
