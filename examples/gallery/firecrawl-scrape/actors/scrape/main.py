"""Firecrawl v2 Store wrapper with caller-scoped asynchronous results.

scrape/api return an acceptance record. The registered hirer reads get_result
with that request_id; vendor data is never returned by the initial dispatch.
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

VENDOR = "firecrawl"
ACTOR_VERSION = "2.0.0"
API_ROOT = "https://api.firecrawl.dev/v2"
SECRET_KEY = "FIRECRAWL_API_KEY"  # CBSS name; released to the runner per call
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer "  # Firecrawl wants "Bearer <key>", not a bare key

# Fixed (method, path) routes. Paths are literal so a caller can never choose a
# URL; caller path fields and query values are percent-encoded separately.
API_OPERATIONS = {
    "scrape": ("POST", "/scrape"),
    "get_scrape": ("GET", "/scrape/{jobId}"),
    "interact_scrape": ("POST", "/scrape/{jobId}/interact"),
    "stop_interact_scrape": ("DELETE", "/scrape/{jobId}/interact"),
    "parse": ("POST", "/parse"),
    "batch_scrape": ("POST", "/batch/scrape"),
    "get_batch_scrape": ("GET", "/batch/scrape/{id}"),
    "cancel_batch_scrape": ("DELETE", "/batch/scrape/{id}"),
    "batch_scrape_errors": ("GET", "/batch/scrape/{id}/errors"),
    "crawl": ("POST", "/crawl"),
    "get_crawl": ("GET", "/crawl/{id}"),
    "cancel_crawl": ("DELETE", "/crawl/{id}"),
    "crawl_errors": ("GET", "/crawl/{id}/errors"),
    "crawl_params_preview": ("POST", "/crawl/params-preview"),
    "active_crawls": ("GET", "/crawl/active"),
    "map": ("POST", "/map"),
    "search": ("POST", "/search"),
    "search_feedback": ("POST", "/search/{jobId}/feedback"),
    "extract": ("POST", "/extract"),
    "get_extract": ("GET", "/extract/{id}"),
    "list_agents": ("GET", "/agent"),
    "start_agent": ("POST", "/agent"),
    "get_agent": ("GET", "/agent/{jobId}"),
    "cancel_agent": ("DELETE", "/agent/{jobId}"),
    "agent_trace": ("GET", "/agent/{jobId}/trace"),
    "agent_snapshot": ("GET", "/agent/{jobId}/snapshots/{snapshotId}"),
    "create_browser_session": ("POST", "/interact"),
    "list_browser_sessions": ("GET", "/interact"),
    "execute_browser_code": ("POST", "/interact/{sessionId}/execute"),
    "delete_browser_session": ("DELETE", "/interact/{sessionId}"),
    "create_monitor": ("POST", "/monitor"),
    "list_monitors": ("GET", "/monitor"),
    "get_monitor": ("GET", "/monitor/{monitorId}"),
    "update_monitor": ("PATCH", "/monitor/{monitorId}"),
    "delete_monitor": ("DELETE", "/monitor/{monitorId}"),
    "run_monitor": ("POST", "/monitor/{monitorId}/run"),
    "list_monitor_checks": ("GET", "/monitor/{monitorId}/checks"),
    "get_monitor_check": ("GET", "/monitor/{monitorId}/checks/{checkId}"),
    "research_search_papers": ("GET", "/search/research/papers"),
    "research_get_paper": ("GET", "/search/research/papers/{id}"),
    "research_related_papers": ("GET", "/search/research/papers/{id}/similar"),
    "developer_search": ("GET", "/search/developer"),
    "developer_search_post": ("POST", "/search/developer"),
    "ask_support": ("POST", "/support/ask"),
    "docs_search": ("POST", "/support/docs-search"),
    "feedback": ("POST", "/feedback"),
    "credit_usage": ("GET", "/team/credit-usage"),
    "credit_usage_historical": ("GET", "/team/credit-usage/historical"),
    "token_usage": ("GET", "/team/token-usage"),
    "token_usage_historical": ("GET", "/team/token-usage/historical"),
    "queue_status": ("GET", "/team/queue-status"),
    "activity": ("GET", "/team/activity"),
    "get_threat_protection": ("GET", "/team/threat-protection"),
    "update_threat_protection": ("PUT", "/team/threat-protection"),
}
PATH_FIELDS = ("id", "jobId", "monitorId", "checkId", "sessionId", "snapshotId")
BODY_METHODS = ("POST", "PATCH", "PUT")
MAX_REQUEST_BYTES = 28_000

# Bound the final encoded response, not only the nested vendor data. Actor-side
# limiting cannot shrink a response that never reaches the Runner callback.
RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976  # RETURN_BYTE_BUDGET - 1_024
MAX_RESULT_CHUNK_BYTES = 3_900  # Leaves framing space under the 4096-byte event cap.
MAX_RETAINED_REQUESTS = 16
MAX_REGISTERED_HIRES = 512
# HTTP jobs currently have 30-block selection windows and up to three retries.
# These are Actor cleanup thresholds, checked on later calls, not HTTP timeouts.
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150
KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_CALLS = "call_count"
KEY_REQUEST_IDS = "request_ids"
KEY_LAST_STATUS = "last_status"
KEY_LAST_RESULTS = "last_results"
KEY_LAST_OUTPUT = "last_output"


def _parse(payload) -> dict:
    if payload is None or payload == b"":
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            value = codec.decode(bytes(payload))
        except CodecError:
            value = json.loads(bytes(payload).decode("utf-8"))
    else:
        value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("payload must be an object")
    return value


def _decode_result(result) -> dict:
    value = result.data if isinstance(result, runner.RunnerResult) else result
    if not isinstance(value, dict):
        return {"success": False, "error": "vendor returned a non-JSON response"}
    return value


def _vendor_error(decoded):
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
        return "vendor returned a non-JSON response ({})".format(
            decoded.get("content_type", "unknown content-type")
        )
    return "vendor request failed"


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
    return raw[: max(0, byte_limit)].decode("utf-8", "ignore")


def _limited_response(value, budget):
    """Shrink arbitrary Firecrawl JSON under `budget` while keeping it valid JSON."""
    try:
        out = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        out = {"success": False, "error": "vendor returned a non-JSON response"}
    original = len(_json_bytes(out))
    if original <= budget:
        return out
    marker = {"truncated": True, "original_bytes": original, "limit_bytes": budget}
    out["_cowboy"] = marker

    def candidates(node, path=()):
        strings, lists = [], []
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "_cowboy":
                    continue
                child_strings, child_lists = candidates(child, path + (key,))
                strings.extend(child_strings)
                lists.extend(child_lists)
        elif isinstance(node, list):
            if node:
                lists.append((len(_json_bytes(node)), path))
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
            target = out
            for part in path:
                target = target[part]
            del target[max(0, len(target) // 2) :]
            continue
        out = {"success": out.get("success", True), "_cowboy": marker}
        break
    return out


def _contains_secret_template(value):
    """True if any string carries the runner `${NAME}` substitution syntax.

    Caller-controlled request data must be refused before dispatch so a hirer
    cannot inject the CBSS credential into a target-page header or a webhook.
    """
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
        raise ValueError(f"{field} must be a 20-byte address") from exc


def _sender_hex():
    return _address_hex(runtime.get_sender(), "sender")


def _bounded_int(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed < low or parsed > high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return parsed


def _http_url(value) -> str:
    # This is an early typo check. Firecrawl validates the full URL; the Actor
    # always sends HTTP jobs to API_ROOT. urllib.parse is not allowed in PVM.
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in text):
        return ""
    scheme, separator, rest = text.partition("://")
    if not separator or scheme.lower() not in ("http", "https"):
        return ""
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        return text if host.find("]") > 1 else ""
    return text if host.split(":", 1)[0] else ""


def _quote(value):
    # RFC 3986 unreserved bytes. Keep this small encoder in the Actor because
    # PVM does not allow urllib; the local test client uses the standard library.
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(chr(c) if c in safe else f"%{c:02X}" for c in value.encode("utf-8"))


def _declaration_id(value):
    text = str(value or "").strip().lower()
    try:
        raw = bytes.fromhex(text.removeprefix("0x"))
    except ValueError as exc:
        raise ValueError("declaration_id must be a 32-byte hex value") from exc
    if len(raw) != 32:
        raise ValueError("declaration_id must be a 32-byte hex value")
    return "0x" + raw.hex()


def _component(value, field) -> str:
    value = str(value or "").strip()
    if (
        not value
        or len(value) > 300
        or value in (".", "..")
        or any(c in value for c in "/?#\r\n")
    ):
        raise ValueError(f"{field} is invalid")
    return _quote(value)


def _query_string(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("query_params must be an object with string keys")
    pairs = []
    for key in sorted(value):
        items = value[key] if isinstance(value[key], list) else [value[key]]
        for item in items:
            if isinstance(item, bool):
                item = "true" if item else "false"
            elif not isinstance(item, (str, int, float)):
                raise TypeError("query parameter values must be scalar or arrays")
            pairs.append((key, str(item)))
    return (
        "?" + "&".join(_quote(k) + "=" + _quote(v) for k, v in pairs) if pairs else ""
    )


def _api_body(req, path_fields=()):
    """The documented Firecrawl request body: everything except Store metadata."""
    body = dict(req)
    for key in ("hire_id", "operation", "query_params") + tuple(path_fields):
        body.pop(key, None)
    return body


@actor
class FirecrawlScrapeWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Firecrawl Scrape"
        self.storage["__description__"] = (
            "Hire-gated Firecrawl v2 API with retrievable results."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        runtime.emit_event(
            "wrapper.init", {"vendor": VENDOR, "store_registrar": registrar}
        )
        return codec.encode({"ok": True, "vendor": VENDOR})

    @public
    def register_hire(self, payload):
        """The registrar records a verified Hire; exact retries are idempotent."""
        if _sender_hex() != self.storage.get(KEY_REGISTRAR):
            raise ValueError("register_hire: Store registrar required")
        cfg = _parse(payload)
        hire_id = _bounded_int(cfg.get("hire_id"), "hire_id", 1, 2**64 - 1)
        record = {
            "hire_id": hire_id,
            "hirer": _address_hex(cfg.get("hirer"), "hirer"),
            "declaration_id": _declaration_id(cfg.get("declaration_id")),
            "version": _bounded_int(cfg.get("version"), "version", 1, 2**32 - 1),
            "artifact_id": _bounded_int(
                cfg.get("artifact_id"), "artifact_id", 0, 2**32 - 1
            ),
        }
        key = "hire:" + str(hire_id)
        existing = self.storage.get(key)
        if existing and existing != record:
            raise ValueError(
                "register_hire: hire_id is already registered with different data"
            )
        if not existing:
            count = self.storage[KEY_HIRE_COUNT]
            if count >= MAX_REGISTERED_HIRES:
                raise ValueError("register_hire: registered Hire capacity is full")
            self.storage[key] = record
            self.storage[KEY_HIRE_COUNT] = count + 1
            runtime.emit_event(
                "wrapper.hire_registered",
                {
                    "hire_id": hire_id,
                    "hirer": record["hirer"],
                },
            )
        return codec.encode({"ok": True, "hire_id": hire_id, "hirer": record["hirer"]})

    @public
    @deferred  # dispatching the continuation's runner job is an async side effect
    def scrape(self, payload):
        req = _parse(payload)
        url = _http_url(req.get("url"))
        if not url:
            return codec.encode(
                {"ok": False, "error": "url is required and must be http(s)"}
            )
        # Only hire_id is wrapper metadata for this native scrape entrypoint.
        body = dict(req)
        body.pop("hire_id", None)
        body["url"] = url
        return self._accept(
            req.get("hire_id"), "scrape", "/scrape", "POST", body, native=True
        )

    @public
    @deferred  # dispatching the continuation's runner job is an async side effect
    def api(self, payload):
        req = _parse(payload)
        operation = str(req.get("operation", "")).strip()
        if operation not in API_OPERATIONS:
            raise ValueError("api: unsupported operation")
        method, path = API_OPERATIONS[operation]
        fields = [field for field in PATH_FIELDS if "{" + field + "}" in path]
        for field in fields:
            path = path.replace("{" + field + "}", _component(req.get(field), field))
        body = _api_body(req, fields)
        query = req.get("query_params")
        if _contains_secret_template(query):
            return codec.encode(
                {"ok": False, "error": "request contains a runner secret template"}
            )
        return self._accept(
            req.get("hire_id"), operation, path + _query_string(query), method, body
        )

    def _accept(self, hire_id, operation, path, method, body, native=False):
        hire_id = _bounded_int(hire_id, "hire_id", 1, 2**64 - 1)
        hire = self.storage.get("hire:" + str(hire_id))
        if not hire:
            return codec.encode(
                {
                    "ok": False,
                    "error": f"hire {hire_id} is not activated on this actor",
                }
            )
        buyer = _sender_hex()
        if buyer != hire["hirer"]:
            raise ValueError("caller is not the registered hirer")
        if _contains_secret_template(body):
            return codec.encode(
                {"ok": False, "error": "request contains a runner secret template"}
            )
        request_bytes = _json_bytes(body)
        if len(request_bytes) + len(path.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")

        self._cleanup()
        ids = self.storage[KEY_REQUEST_IDS]
        cid = continuation.new_cid(self, "_run_request")
        if any(self.storage["request:" + str(i)].get("cid") == cid for i in ids):
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
        request_id = _bounded_int(
            self.storage[KEY_CALLS] + 1, "request_id", 1, 2**64 - 1
        )
        record = {
            "request_id": request_id,
            "hire_id": hire_id,
            "buyer": buyer,
            "operation": operation,
            "native": native,
            "status": "pending",
            "cid": cid,
            "deadline_height": runtime.get_block_height() + REQUEST_LIFETIME_BLOCKS,
        }
        if native:
            record["source_url"] = _truncate_text_bytes(body["url"], 1024)
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: AUTH_SCHEME + f"${{{SECRET_KEY}}}",
        }
        job = {
            "url": API_ROOT + path,
            "method": method,
            "headers": headers,
            # Cross-account runtime refs are [account, key], unlike manifest refs.
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": {
                k: hire[k]
                for k in ("hire_id", "declaration_id", "version", "artifact_id")
            },
        }
        # A body or Content-Type on GET/DELETE stalls Firecrawl's CDN.
        if method in BODY_METHODS:
            headers["Content-Type"] = "application/json"
            job["body"] = request_bytes
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True,
            "request_id": request_id,
            "hire_id": hire_id,
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
        if not record or record["status"] != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        if runtime.get_block_height() >= record["deadline_height"]:
            result = {
                "success": False,
                "error": "request result did not arrive before its deadline",
            }
        decoded = _decode_result(result)
        failed = (
            decoded.get("success") is False or "error" in decoded or "raw" in decoded
        )
        if "raw" in decoded:
            decoded = {"success": False, "error": _vendor_error(decoded)}
        limited = _limited_response(decoded, API_BYTE_BUDGET)
        failed = failed or limited.get("success") is False
        reason = (
            _truncate_text_bytes(
                _vendor_error(decoded if decoded.get("error") else limited), 512
            )
            if failed
            else ""
        )
        output = (
            limited
            if record["native"]
            else {
                "ok": not failed,
                "operation": record["operation"],
                "data": limited,
            }
        )
        if failed and not record["native"]:
            output["error"] = reason
        record["status"] = "error" if failed else "ok"
        record["output"] = output
        record["expires_height"] = runtime.get_block_height() + RESULT_RETENTION_BLOCKS
        if len(codec.encode(self._response(record))) > RETURN_BYTE_BUDGET:
            failed = True
            output = {
                "success": False,
                "error": "vendor result exceeds the encoded result byte budget",
            }
            if not record["native"]:
                output = {
                    "ok": False,
                    "operation": record["operation"],
                    "data": output,
                    "error": output["error"],
                }
        record["status"] = "error" if failed else "ok"
        record["output"] = output
        record.pop("cid", None)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        self.storage[KEY_LAST_OUTPUT] = (
            {"ok": not failed, "operation": "scrape", "data": output}
            if record["native"]
            else output
        )
        if record["native"]:
            data = output.get("data", {})
            meta = data.get("metadata", {}) if isinstance(data, dict) else {}
            self.storage[KEY_LAST_RESULTS] = (
                []
                if failed or not isinstance(meta, dict)
                else [
                    {
                        "title": _truncate_text_bytes(str(meta.get("title", "")), 120),
                        "url": _truncate_text_bytes(
                            str(
                                meta.get("url")
                                or meta.get("sourceURL")
                                or record.get("source_url", "")
                            ),
                            1024,
                        ),
                    }
                ]
            )
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "status": record["status"],
            },
        )
        return codec.encode(self._response(record))

    def _response(self, record):
        response = {
            k: record[k] for k in ("request_id", "hire_id", "operation", "status")
        }
        response["ok"] = record["status"] != "error"
        if record["status"] == "pending":
            response["deadline_height"] = record["deadline_height"]
        else:
            response["output"] = record["output"]
            response["expires_height"] = record["expires_height"]
        return response

    def _cleanup(self, only=None):
        """Release timed-out continuations; retain completed results until expiry."""
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
                self._finish(
                    request_id,
                    {
                        "success": False,
                        "error": "request result did not arrive before its deadline",
                    },
                )
                record = self.storage[key]
            if record["status"] != "pending" and now >= record["expires_height"]:
                del self.storage[key]
            else:
                retained.append(request_id)
        if retained != ids:
            self.storage[KEY_REQUEST_IDS] = retained

    @public
    def get_result(self, payload):
        """Return this caller's result; optional receipt chunks support signed CLI calls."""
        req = _parse(payload)
        request_id = _bounded_int(req.get("request_id"), "request_id", 1, 2**64 - 1)
        hire_id = _bounded_int(req.get("hire_id"), "hire_id", 1, 2**64 - 1)
        self._cleanup(request_id)
        record = self.storage.get("request:" + str(request_id))
        if (
            not record
            or record["buyer"] != _sender_hex()
            or record["hire_id"] != hire_id
        ):
            return codec.encode(
                {"ok": False, "error": "request not found for caller and hire"}
            )
        encoded = codec.encode(self._response(record))
        if "receipt_offset" in req:
            offset = _bounded_int(
                req["receipt_offset"], "receipt_offset", 0, len(encoded) - 1
            )
            chunk = {
                "request_id": request_id,
                "hire_id": hire_id,
                "offset": offset,
                "total": len(encoded),
                "data": encoded[offset : offset + MAX_RESULT_CHUNK_BYTES],
            }
            runtime.emit_event("wrapper.result_chunk", chunk)
        return encoded

    @public
    def capability(self, payload):
        return codec.encode(
            {
                "vendor": VENDOR,
                "slot": "scrape",
                "handler": "scrape",
                "handlers": ["scrape", "api"],
                "input": {
                    "hire_id": "int",
                    "url": "str",
                    "...": "Firecrawl v2 /scrape fields",
                },
                "output": "acceptance record; get_result returns the request status and output",
                "returns": "firecrawl-async-result",
                "poll_handler": "get_result",
                "api_operations": sorted(API_OPERATIONS),
                "excluded": [
                    "inbound_webhook_events",
                    "mcp_transport",
                    "sse_streaming",
                    "local_cli_tooling",
                ],
                "hire_activation": {"protocol": "store-admin-v1"},
                "result_byte_budget": RETURN_BYTE_BUDGET,
            }
        )

    @public
    def get_last_results(self, payload):
        return codec.encode(
            {
                "status": self.storage.get(KEY_LAST_STATUS, "none"),
                "results": self.storage.get(KEY_LAST_RESULTS, []),
            }
        )

    @public
    def get_last_output(self, payload):
        return codec.encode(self.storage.get(KEY_LAST_OUTPUT, {}))

    @public
    def get_config(self, payload):
        return codec.encode(
            {
                "vendor": VENDOR,
                "response_byte_limit": RETURN_BYTE_BUDGET,
                "call_count": self.storage.get(KEY_CALLS, 0),
                "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                "last_status": self.storage.get(KEY_LAST_STATUS),
            }
        )


# Each invocation uses persisted state; construction must occur after import.
def init(payload):
    return FirecrawlScrapeWrapper().init(payload)


def register_hire(payload):
    return FirecrawlScrapeWrapper().register_hire(payload)


def scrape(payload):
    return FirecrawlScrapeWrapper().scrape(payload)


def api(payload):
    return FirecrawlScrapeWrapper().api(payload)


def get_result(payload):
    return FirecrawlScrapeWrapper().get_result(payload)


def _run_request__resume(payload):
    try:
        return FirecrawlScrapeWrapper()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})


def capability(payload):
    return FirecrawlScrapeWrapper().capability(payload)


def get_last_results(payload):
    return FirecrawlScrapeWrapper().get_last_results(payload)


def get_last_output(payload):
    return FirecrawlScrapeWrapper().get_last_output(payload)


def get_config(payload):
    return FirecrawlScrapeWrapper().get_config(payload)
