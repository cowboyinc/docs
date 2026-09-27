"""Browserbase Store wrapper actor — fetch, search, and agent browse.

Each vendor call is hire-gated and asynchronous: a handler validates input and
returns an acceptance record; the registered hirer reads the result from
`get_result`. Under the hood one request is a single `runner.http` to
Browserbase's REST layer with `${BROWSERBASE_API_KEY}` from CBSS, resumed once as
a continuation. `fetch` and `search` return in that one call; the agent API is
asynchronous, so `browse` starts a run and returns its id and `browse_result`
polls it, letting a caller poll across transactions rather than block. Replies
are bounded to 38,000 encoded bytes after the Runner callback.

Fetch does NOT execute JavaScript (Browserbase returns the raw server response);
for JS-rendered content use the agent `browse` path.
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


VENDOR = "browserbase"
ACTOR_VERSION = "2.0.0"
API_ROOT = "https://api.browserbase.com/v1"
FETCH_ENDPOINT = "https://api.browserbase.com/v1/fetch"
SEARCH_ENDPOINT = "https://api.browserbase.com/v1/search"
AGENTS_ENDPOINT = "https://api.browserbase.com/v1/agents/runs"
SECRET_KEY = "BROWSERBASE_API_KEY"
AUTH_HEADER = "X-BB-API-Key"

FETCH_FORMATS = ("", "raw", "markdown", "json")
DEFAULT_FETCH_FORMAT = "markdown"
MAX_URL_BYTES = 4_000
MAX_QUERY_BYTES = 200          # Browserbase search caps the query at 200 chars
MAX_TASK_BYTES = 4_000
MAX_SCHEMA_BYTES = 8_000
DEFAULT_SEARCH_RESULTS = 10
MAX_SEARCH_RESULTS = 25
MAX_RUN_ID_BYTES = 128

# Bound the full encoded response, not only the nested vendor data.
RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976  # RETURN_BYTE_BUDGET - 1_024
MAX_RESULT_CHUNK_BYTES = 3_900  # Leaves framing space under the 4096-byte event cap.
MAX_RETAINED_REQUESTS = 16
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

BROWSE_OPS = ("browse", "browse_result")


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


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "browserbase request failed"
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
        if isinstance(value, dict):
            for inner in ("message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    return "browserbase request failed"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _truncate_text_bytes(text, byte_limit):
    raw = text.encode("utf-8")
    if len(raw) <= byte_limit:
        return text
    return raw[:max(0, byte_limit)].decode("utf-8", "ignore")


def _limited_response(value, budget):
    """Bound the saved reply after delivery. This cannot bound Runner transport."""
    try:
        out = json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return {"error": "non-serializable response"}
    if len(_json_bytes(out)) <= budget:
        return out
    if not isinstance(out, dict):
        out = {"data": out}
    out["_cowboy"] = {"truncated": True, "limit_bytes": budget}

    def candidates(node, path=()):
        strings, lists = [], []
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "_cowboy":
                    continue
                cs, cl = candidates(child, path + (key,))
                strings.extend(cs)
                lists.extend(cl)
        elif isinstance(node, list):
            if node:
                lists.append((len(_json_bytes(node)), path))
            for index, child in enumerate(node):
                cs, cl = candidates(child, path + (index,))
                strings.extend(cs)
                lists.extend(cl)
        elif isinstance(node, str) and node:
            strings.append((len(node.encode("utf-8")), path))
        return strings, lists

    def resolve(path):
        node = out
        for part in path[:-1]:
            node = node[part]
        return node, path[-1]

    while len(_json_bytes(out)) > budget:
        strings, lists = candidates(out)
        if strings:
            excess = len(_json_bytes(out)) - budget
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
            _, path = max(lists, key=lambda item: (item[0], repr(item[1])))
            node = out
            for part in path:
                node = node[part]
            del node[max(0, len(node) // 2):]
            continue
        out = {"_cowboy": out["_cowboy"]}
        break
    return out


def _contains_secret_template(value):
    """A ${NAME} in caller data must be refused: the key rides every call in a
    header, and Browserbase echoes fields (e.g. the search query) back, so a
    substituted ${BROWSERBASE_API_KEY} in an echoed field would leak the key."""
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(_contains_secret_template(k) or _contains_secret_template(v)
                   for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_template(v) for v in value)
    return False


def _address_hex(value, field):
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


def _clamp_int(value, low, high, default):
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


def _http_url(value):
    """A normalized http(s) URL, or "" if not one. Never raises."""
    text = str(value or "").strip()
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in text):
        return ""
    parts = lowered.split("://", 1)
    if len(parts) != 2 or not parts[1].split("/", 1)[0]:
        return ""
    return text


def _run_id_segment(value):
    """One percent-encoded path segment for the agent run id. Separators are
    refused, not encoded, so a caller cannot repoint the URL."""
    value = str(value or "").strip()
    if not value or len(value) > MAX_RUN_ID_BYTES or any(c in value for c in "/?#\r\n"):
        raise ValueError("run_id is invalid")
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(chr(c) if c in safe else "%%%02X" % c for c in value.encode("utf-8"))


@actor
class BrowserbaseBrowseWrapper:
    """Sells Browserbase fetch / search / agent browse per-call to any hirer."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Browserbase Browse"
        self.storage["__description__"] = (
            "Fetch pages to markdown, web search, and agentic JS browsing. "
            "Vendor key held in CBSS."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        runtime.emit_event("wrapper.init",
                           {"vendor": VENDOR, "owner": owner, "store_registrar": registrar})
        return codec.encode({"ok": True, "vendor": VENDOR})

    @public
    def register_hire(self, payload):
        """Record a finalized Hire the Store registrar verified from chain state.
        A hire_id is immutable; an exact retry is idempotent."""
        if _sender_hex() != self.storage.get(KEY_REGISTRAR, ""):
            raise ValueError("register_hire: Store registrar required")
        cfg = _parse(payload)
        hire_id = _bounded_int(cfg.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hirer = _address_hex(cfg.get("hirer"), "hirer")
        record = {"hire_id": hire_id, "hirer": hirer,
                  "declaration_id": _declaration_id(cfg.get("declaration_id")),
                  "version": _bounded_int(cfg.get("version"), "version", 1, 2**32 - 1),
                  "artifact_id": _bounded_int(cfg.get("artifact_id"), "artifact_id", 0, 2**32 - 1)}
        key = "hire:" + str(hire_id)
        existing = self.storage.get(key)
        if existing:
            if existing != record:
                raise ValueError("register_hire: hire_id already registered with different data")
            return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})
        count = self.storage[KEY_HIRE_COUNT]
        if count >= MAX_REGISTERED_HIRES:
            raise ValueError("register_hire: registered Hire capacity is full")
        self.storage[key] = record
        self.storage[KEY_HIRE_COUNT] = count + 1
        runtime.emit_event("wrapper.hire_registered", {"hire_id": hire_id, "hirer": hirer})
        return codec.encode({"ok": True, "hire_id": hire_id, "hirer": hirer})

    def _hire(self, req, operation):
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        hire = self.storage.get("hire:" + str(hire_id)) or {}
        if not hire:
            return hire_id, {}, codec.encode(
                {"ok": False, "error": "hire %d is not activated on this actor" % hire_id,
                 "hint": "the Store registrar must call register_hire for this hire"})
        if _sender_hex() != hire.get("hirer"):
            raise ValueError("%s: caller is not the registered hirer" % operation)
        return hire_id, hire, None

    def _pins(self, hire_id, hire):
        return {"hire_id": hire_id, "declaration_id": hire["declaration_id"],
                "version": hire["version"], "artifact_id": hire["artifact_id"]}

    # --- handlers: validate, then return an acceptance record -----------------

    @public
    @deferred
    def fetch(self, payload):
        """payload: {"hire_id": int, "url": str, "format": raw|markdown|json?,
                     "schema": object? (json only)}. Does not render JavaScript."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "fetch")
        if refusal is not None:
            return refusal
        url = _http_url(req.get("url"))
        if not url:
            return self._refuse("fetch", "url is required and must be http(s)")
        if len(url.encode("utf-8")) > MAX_URL_BYTES:
            return self._refuse("fetch", "url exceeds %d bytes" % MAX_URL_BYTES)
        fmt = str(req.get("format") or "").strip().lower()
        if fmt not in FETCH_FORMATS:
            return self._refuse("fetch", "format must be one of raw, markdown, json")
        fmt = fmt or DEFAULT_FETCH_FORMAT
        body = {"url": url, "format": fmt}
        schema = req.get("schema")
        if schema is not None:
            if fmt != "json":
                return self._refuse("fetch", "schema is only valid with format=json")
            if not isinstance(schema, dict):
                return self._refuse("fetch", "schema must be an object")
            if len(_json_bytes(schema)) > MAX_SCHEMA_BYTES:
                return self._refuse("fetch", "schema exceeds %d bytes" % MAX_SCHEMA_BYTES)
            body["schema"] = schema
        for flag in ("allowRedirects", "allowInsecureSsl", "proxies"):
            if isinstance(req.get(flag), bool):
                body[flag] = req[flag]
        if _contains_secret_template(body):
            return self._refuse("fetch", "request may not contain runner secret templates")
        return self._accept(hire_id, hire, "fetch", FETCH_ENDPOINT, "POST", body)

    @public
    @deferred
    def search(self, payload):
        """payload: {"hire_id": int, "query": str, "num_results": int?}.
        Returns result metadata (url, title); fetch a url for its content."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "search")
        if refusal is not None:
            return refusal
        query = str(req.get("query") or "").strip()
        if not query:
            return self._refuse("search", "query is required")
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            return self._refuse("search", "query exceeds %d bytes" % MAX_QUERY_BYTES)
        if _contains_secret_template(query):
            return self._refuse("search", "query may not contain runner secret templates")
        num = _clamp_int(req.get("num_results"), 1, MAX_SEARCH_RESULTS, DEFAULT_SEARCH_RESULTS)
        return self._accept(hire_id, hire, "search", SEARCH_ENDPOINT, "POST",
                            {"query": query, "numResults": num})

    @public
    @deferred
    def browse(self, payload):
        """payload: {"hire_id": int, "task": str, "result_schema": object?}.

        Starts an async agent run that renders JS; the result is delivered as a
        run id. Poll it with browse_result — the run is not finished yet."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "browse")
        if refusal is not None:
            return refusal
        task = str(req.get("task") or "").strip()
        if not task:
            return self._refuse("browse", "task is required")
        if len(task.encode("utf-8")) > MAX_TASK_BYTES:
            return self._refuse("browse", "task exceeds %d bytes" % MAX_TASK_BYTES)
        body = {"task": task}
        schema = req.get("result_schema")
        if schema is not None:
            if not isinstance(schema, dict):
                return self._refuse("browse", "result_schema must be an object")
            if len(_json_bytes(schema)) > MAX_SCHEMA_BYTES:
                return self._refuse("browse", "result_schema exceeds %d bytes" % MAX_SCHEMA_BYTES)
            body["resultSchema"] = schema
        if _contains_secret_template(body):
            return self._refuse("browse", "request may not contain runner secret templates")
        return self._accept(hire_id, hire, "browse", AGENTS_ENDPOINT, "POST", body)

    @public
    @deferred
    def browse_result(self, payload):
        """payload: {"hire_id": int, "run_id": str}. Polls an agent run started
        by browse; `result` is present only once status is terminal."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "browse_result")
        if refusal is not None:
            return refusal
        try:
            run_id = _run_id_segment(req.get("run_id"))
        except ValueError as exc:
            return self._refuse("browse_result", str(exc))
        return self._accept(hire_id, hire, "browse_result",
                            AGENTS_ENDPOINT + "/" + run_id, "GET", None)

    def _refuse(self, operation, error):
        out = {"ok": False, "operation": operation, "error": error}
        self.storage[KEY_LAST_STATUS] = "error"
        self.storage[KEY_LAST_OUTPUT] = out
        runtime.emit_event("wrapper.error", {"vendor": VENDOR, "operation": operation,
                                             "reason": str(error)[:200]})
        return codec.encode(out)

    def _accept(self, hire_id, hire, operation, url, method, body):
        buyer = _sender_hex()
        self._cleanup()
        ids = self.storage[KEY_REQUEST_IDS]
        cid = continuation.new_cid(self, "_run_request")
        if any(self.storage["request:" + str(i)].get("cid") == cid for i in ids):
            return codec.encode({"ok": False, "error": "transaction already has a pending request"})
        if len(ids) >= MAX_RETAINED_REQUESTS:
            return codec.encode({"ok": False, "error": "request capacity is full; retry after results expire"})
        request_id = _bounded_int(self.storage[KEY_CALLS] + 1, "request_id", 1, 2**64 - 1)
        record = {
            "request_id": request_id, "hire_id": hire_id, "buyer": buyer,
            "operation": operation, "status": "pending", "cid": cid,
            "deadline_height": runtime.get_block_height() + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        # Build the runner.http job here. A body or Content-Type must be OMITTED
        # on a GET — the node rejects a GET job that carries a body (E1604
        # "invalid job schema"), so the key is absent, not "".
        headers = {"Accept": "application/json", AUTH_HEADER: "${%s}" % SECRET_KEY}
        job = {"url": url, "method": method, "headers": headers,
               "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
               "hire": self._pins(hire_id, hire)}
        if method != "GET" and body is not None:
            headers["Content-Type"] = "application/json"
            # Preserve fractional JSON Schema values through the existing raw-body path.
            job["raw_body"] = _json_bytes(body)
        self._run_request(codec.encode({
            "request_id": request_id, "operation": operation, "job": job}))
        accepted = {"ok": True, "request_id": request_id, "hire_id": hire_id,
                    "operation": operation, "status": "pending", "poll_handler": "get_result"}
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        ctx = capture()
        ctx.request_id = req["request_id"]
        ctx.operation = req["operation"]
        result = await runner.http(**req["job"])
        return self._finish(ctx.request_id, ctx.operation, result)

    def _finish(self, request_id, operation, result):
        key = "request:" + str(request_id)
        record = self.storage.get(key)
        if not record or record["status"] != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        if runtime.get_block_height() >= record["deadline_height"]:
            output = {"ok": False, "operation": operation,
                      "error": "request result did not arrive before its deadline"}
        else:
            output = self._shape(operation, result)
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        record["expires_height"] = runtime.get_block_height() + RESULT_RETENTION_BLOCKS
        if len(codec.encode(self._response(record))) > RETURN_BYTE_BUDGET:
            output = {"ok": False, "operation": operation,
                      "error": "result exceeds the encoded result byte budget"}
            record["status"] = "error"
            record["output"] = output
        record.pop("cid", None)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        self.storage[KEY_LAST_OUTPUT] = output
        runtime.emit_event("wrapper.request_finished",
                           {"request_id": request_id, "hire_id": record["hire_id"],
                            "operation": operation, "status": record["status"]})
        return codec.encode(self._response(record))

    def _shape(self, operation, result):
        decoded = result.data
        if not isinstance(decoded, dict) or any(
            key in decoded for key in ("error", "detail", "raw", "raw_text")
        ):
            return {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
        if operation in BROWSE_OPS:
            run_id = decoded.get("runId")
            status = decoded.get("status")
            if not isinstance(run_id, str) or not run_id or status not in (
                "PENDING", "RUNNING", "COMPLETED", "FAILED", "STOPPED", "TIMED_OUT"
            ):
                return {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
            terminal = status in ("COMPLETED", "FAILED", "STOPPED", "TIMED_OUT")
            data = {"run_id": run_id, "status": status, "done": terminal}
            if decoded.get("result") is not None:
                if not isinstance(decoded["result"], dict):
                    return {"ok": False, "operation": operation, "error": "invalid browserbase run result"}
                # The run ID and status are polling controls, not page content.
                # Limit only the vendor result so truncation cannot change them.
                data["result"] = _limited_response(
                    decoded["result"], API_BYTE_BUDGET - len(_json_bytes(data)) - 11)
            return {"ok": True, "operation": operation, "data": data}
        # Even format=raw uses Browserbase's JSON fetch envelope. The Runner's
        # {raw, content_type} fallback is not a successful Browserbase response.
        if operation == "fetch":
            valid = (isinstance(decoded.get("id"), str) and bool(decoded["id"])
                     and type(decoded.get("statusCode")) is int
                     and 100 <= decoded["statusCode"] <= 599
                     and isinstance(decoded.get("headers"), dict)
                     and isinstance(decoded.get("content"), (str, dict))
                     and isinstance(decoded.get("contentType"), str)
                     and isinstance(decoded.get("encoding"), str))
        else:
            valid = (isinstance(decoded.get("requestId"), str) and bool(decoded["requestId"])
                     and isinstance(decoded.get("query"), str)
                     and isinstance(decoded.get("results"), list)
                     and all(isinstance(item, dict) and isinstance(item.get("url"), str)
                             and isinstance(item.get("title"), str) for item in decoded["results"]))
        if not valid:
            return {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
        return {"ok": True, "operation": operation, "data": _limited_response(decoded, API_BYTE_BUDGET)}

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
                self._finish(request_id, record["operation"], None)
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
        request_id = _bounded_int(req.get("request_id"), "request_id", 1, 2**64 - 1)
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        self._cleanup(request_id)
        record = self.storage.get("request:" + str(request_id))
        if (not record
                or record["buyer"] != _sender_hex()
                or record["hire_id"] != hire_id):
            return codec.encode({"ok": False, "error": "request not found for caller and hire"})
        encoded = codec.encode(self._response(record))
        if "receipt_offset" in req:
            offset = _bounded_int(req["receipt_offset"], "receipt_offset", 0, len(encoded) - 1)
            runtime.emit_event("wrapper.result_chunk", {
                "request_id": request_id, "hire_id": hire_id, "offset": offset,
                "total": len(encoded), "data": encoded[offset: offset + MAX_RESULT_CHUNK_BYTES]})
        return encoded

    @public
    def capability(self, payload):
        return codec.encode({
            "vendor": VENDOR,
            "slot": "browse",
            "handlers": ["fetch", "search", "browse", "browse_result"],
            "poll_handler": "get_result",
            "returns": "browserbase-async-result",
            "fetch_input": {"hire_id": "int", "url": "str",
                            "format": "raw|markdown|json?", "schema": "object? (json only)"},
            "search_input": {"hire_id": "int", "query": "str", "num_results": "int?"},
            "browse_input": {"hire_id": "int", "task": "str", "result_schema": "object?"},
            "browse_result_input": {"hire_id": "int", "run_id": "str"},
            "output": "acceptance record; get_result returns the Browserbase response under `data`",
            "notes": ["fetch does not render JS; use browse for JS-rendered pages",
                      "browse is async: browse starts a run, browse_result polls it",
                      "search returns metadata only; fetch a url for its content"],
            "excluded": ["cdp_sessions", "websocket_driving", "file_downloads"],
            "hire_activation": {"protocol": "store-admin-v1"},
            "result_byte_budget": RETURN_BYTE_BUDGET,
        })

    @public
    def get_last_output(self, payload):
        return codec.encode(self.storage.get(KEY_LAST_OUTPUT, {}))

    @public
    def get_config(self, payload):
        return codec.encode({
            "vendor": VENDOR,
            "response_byte_limit": RETURN_BYTE_BUDGET,
            "call_count": self.storage.get(KEY_CALLS, 0),
            "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
            "last_status": self.storage.get(KEY_LAST_STATUS),
        })


def _get_actor():
    return BrowserbaseBrowseWrapper()


def init(payload): return _get_actor().init(payload)
def register_hire(payload): return _get_actor().register_hire(payload)
def fetch(payload): return _get_actor().fetch(payload)
def search(payload): return _get_actor().search(payload)
def browse(payload): return _get_actor().browse(payload)
def browse_result(payload): return _get_actor().browse_result(payload)
def get_result(payload): return _get_actor().get_result(payload)
def capability(payload): return _get_actor().capability(payload)
def get_last_output(payload): return _get_actor().get_last_output(payload)
def get_config(payload): return _get_actor().get_config(payload)


def _run_request__resume(payload):
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
