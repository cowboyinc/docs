"""Hire-gated Hyperbrowser web-scrape Store wrapper.

Every vendor call is hire-gated and asynchronous. A handler validates its input
and returns an acceptance record with a request id; the same buyer and Hire read
the eventual result through ``get_result``. The Hyperbrowser API key stays in
CBSS as ``HYPERBROWSER_API_KEY`` and only ever rides the job as the
``${HYPERBROWSER_API_KEY}`` reference in an ``x-api-key`` header (Hyperbrowser
uses a raw api-key header, NOT Bearer).

Hyperbrowser scrape is submit->poll: ``POST /api/scrape`` returns only a small
``{jobId}`` and the result is retrieved later from a SEPARATE, key-authenticated
``GET /api/scrape/{jobId}`` that returns large inline page content. There is no
synchronous mode and no hosted/presigned result-URL option (unlike Reducto's
force_url_result). So a single POST cannot deliver content, and the result GET
can neither be handed to the buyer as a poll URL (it needs the key) nor reliably
fit the runner transport cap. The wrapper therefore exposes the honest two-step
shape:

- ``scrape`` -> one ``runner.http`` POST that returns the small ``job_id``.
- ``fetch``  -> one ``runner.http`` GET that the ACTOR performs (the key rides as
  a ``${...}`` reference, never handed to the buyer) and returns the bounded
  scraped content. ``fetch`` references the buyer's OWN prior ``scrape`` request
  by ``source_request_id`` (already buyer+Hire gated), so no raw vendor job id is
  ever a caller input and there is no cross-Hire resource hole.

Transport cap: a large page can still overflow the runner->actor cap before the
continuation can inspect it. The runner's top-level ``extract_path`` selection
cannot reshape or truncate nested scrape content, so formats are bounded to
``markdown`` and ``links`` with ``onlyMainContent`` forced true. Once a reply
arrives, exact status, metadata and links are preserved; optional markdown is
omitted with an explicit marker if needed to fit the actor result budget.

Sessions (``/api/session*``) are excluded (G5): a session returns a live CDP
``wsEndpoint``/``token`` connect credential that ``get_result`` would place in
public consensus storage. Stealth / anti-bot / proxy / CAPTCHA-solving session
options are deliberately NOT exposed on the handler surface. See README.
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


VENDOR = "hyperbrowser"
ACTOR_VERSION = "1.0.0"
API_ROOT = "https://api.hyperbrowser.ai"
SCRAPE_ENDPOINT = "https://api.hyperbrowser.ai/api/scrape"
SECRET_KEY = "HYPERBROWSER_API_KEY"
# Hyperbrowser authenticates with a raw api-key header, NOT Bearer. The value is
# the ${...} reference resolved runner-side; the key never enters actor state.
AUTH_HEADER = "x-api-key"
# Explicit non-default User-Agent: some vendor edges (Cloudflare) 403 default
# HTTP-client UAs before the key is checked. The offline mock never calls the
# vendor, so a missing UA would only fail on chain.
USER_AGENT = "cowboy-store-wrapper/1.0.0"

# Only the small/bounded text formats. html (largest) and screenshot (base64,
# and its return type is unverified) are never requested -> they would overflow
# the transport cap and park.
ALLOWED_FORMATS = ("links", "markdown")
DEFAULT_FORMATS = ["markdown"]
JOB_DONE = "completed"
JOB_FAILED = ("failed", "stopped")

MAX_URL_CHARS = 2_048
MAX_ID_CHARS = 200
MAX_ERROR_CHARS = 500
MAX_REQUEST_BYTES = 8_000

RUNNER_RESULT_BYTE_BUDGET = 65_536
RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976
MAX_RESULT_CHUNK_BYTES = 3_900
MAX_RETAINED_REQUESTS = 16
MAX_RETAINED_PER_HIRE = 6
MAX_RETAINED_WITH_FETCHES = 32
MAX_RETAINED_WITH_FETCHES_PER_HIRE = 12
MAX_REGISTERED_HIRES = 512
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150
JOB_LIFETIME_BLOCKS = 600

SUPPORTED_INPUT_FIELDS_SCRAPE = ("hire_id", "url", "formats")
SUPPORTED_INPUT_FIELDS_FETCH = ("hire_id", "source_request_id")

KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_CALLS = "call_count"
KEY_REQUEST_IDS = "request_ids"
KEY_JOB_IDS = "job_ids"
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
    if isinstance(result, runner.RunnerResult):
        return _decode_result(result.raw, depth + 1)
    if not isinstance(result, (bytes, bytearray, str, dict, list)):
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
    # Refuse any `${...}` in caller input: a substituted secret could otherwise
    # be echoed into the outbound request or an error and leak the key.
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


def _target_url(value):
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    text = value.strip()
    if not text.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    if len(text) > MAX_URL_CHARS:
        raise ValueError("url must be at most %d characters" % MAX_URL_CHARS)
    if any(ord(ch) < 0x20 or ch.isspace() or ord(ch) == 0x7f for ch in text):
        raise ValueError("url must not contain whitespace or control characters")
    return text


def _formats(value):
    if value is None:
        return list(DEFAULT_FORMATS)
    if not isinstance(value, list) or not value:
        raise ValueError("formats must be a non-empty list")
    seen = []
    for item in value:
        if item not in ALLOWED_FORMATS:
            raise ValueError(
                "formats may only contain: %s" % ", ".join(ALLOWED_FORMATS)
            )
        if item not in seen:
            seen.append(item)
    # Deterministic order so the outbound body and parity fixture are stable.
    return [fmt for fmt in ALLOWED_FORMATS if fmt in seen]


def _scrape_body(req):
    unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS_SCRAPE)
    if unsupported:
        raise ValueError("unsupported request fields: %s" % ", ".join(unsupported))
    body = {
        "url": _target_url(req.get("url")),
        # No sessionOptions: stealth/useProxy/solveCaptchas/proxy* are never sent.
        # onlyMainContent trims boilerplate, keeping the eventual result smaller.
        "scrapeOptions": {
            "formats": _formats(req.get("formats")),
            "onlyMainContent": True,
        },
    }
    if len(_json_bytes(body)) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds %d bytes" % MAX_REQUEST_BYTES)
    return body


def _valid_job_id(value):
    if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS:
        return False
    # URL-safe id only: it is interpolated into GET /api/scrape/{jobId}.
    return all(ch.isalnum() or ch in "-_.~" for ch in value)


def _is_vendor_error(decoded):
    # Hyperbrowser success is {jobId} (submit) or {jobId,status,...} (result).
    # The transport error envelope is {message}; a non-dict / non-JSON / raw
    # reply is also an error and must never be labeled success.
    if not isinstance(decoded, dict):
        return True
    if "raw" in decoded or "raw_text" in decoded:
        return True
    if "message" in decoded and "jobId" not in decoded:
        return True
    return False


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "hyperbrowser request failed"
    for key in ("message", "error"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return _truncate_text_bytes(value, MAX_ERROR_CHARS)
        if isinstance(value, dict):
            for inner in ("message", "error", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return _truncate_text_bytes(text, MAX_ERROR_CHARS)
    if "raw" in decoded or "raw_text" in decoded:
        return "hyperbrowser returned a non-JSON response"
    return "hyperbrowser request failed"


@actor
class HyperbrowserScrapeWrapper:
    """Sells bounded Hyperbrowser scrape submit/fetch calls to registered Hires."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Hyperbrowser Scrape"
        self.storage["__description__"] = (
            "Bounded Hyperbrowser single-URL web scrape for registered Store "
            "Hires. Vendor key held in CBSS; submit then fetch bounded content."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        self.storage[KEY_JOB_IDS] = []
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
        # M2: KEY_LAST_OUTPUT is public state that never expires; store metadata
        # only, never the full output (which can carry job ids or scraped data).
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    # --- handlers ------------------------------------------------------------

    @public
    @deferred
    def scrape(self, payload):
        """payload: hire_id, url, optional formats (subset of markdown/links).

        Submits a scrape job; the small reply is a job_id. Retrieve the bounded
        content later with `fetch`."""
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "scrape")
        if refusal is not None:
            return refusal
        try:
            body = _scrape_body(req)
        except ValueError as exc:
            return self._refuse("scrape", str(exc))
        return self._accept(hire_id, hire, "scrape", "POST", SCRAPE_ENDPOINT, body)

    @public
    @deferred
    def fetch(self, payload):
        """payload: hire_id, source_request_id (a prior completed `scrape`).

        Performs GET /api/scrape/{job_id} with the CBSS key and returns bounded
        content. The buyer references their OWN scrape request, so no raw vendor
        job id is a caller input and there is no cross-Hire hole."""
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "fetch")
        if refusal is not None:
            return refusal
        unsupported = sorted(key for key in req if key not in SUPPORTED_INPUT_FIELDS_FETCH)
        if unsupported:
            return self._refuse("fetch", "unsupported request fields: %s" % ", ".join(unsupported))
        try:
            source_id = _bounded_int(
                req.get("source_request_id"), "source_request_id", 1, 2**64 - 1
            )
        except ValueError as exc:
            return self._refuse("fetch", str(exc))
        self._cleanup()
        source = self.storage.get("job:" + str(source_id)) or {}
        if (
            source.get("buyer") != _sender_hex()
            or source.get("hire_id") != hire_id
        ):
            return self._refuse(
                "fetch",
                "source scrape request not found for caller and hire, or not completed",
            )
        job_id = source.get("job_id")
        if not _valid_job_id(job_id):
            return self._refuse("fetch", "source scrape request has no valid job id")
        fetch_id = source.get("fetch_request_id")
        if fetch_id:
            if not self.storage.get("request:" + str(fetch_id)):
                source.pop("fetch_request_id", None)
                self.storage["job:" + str(source_id)] = source
                fetch_id = None
        if fetch_id:
            return self._retry_fetch(fetch_id, source_id, hire_id, hire, job_id)
        return self._accept(
            hire_id, hire, "fetch", "GET", SCRAPE_ENDPOINT + "/" + job_id, None,
            source_id,
        )

    def _accept(self, hire_id, hire, operation, method, url, body, source_id=None):
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
        global_limit = MAX_RETAINED_WITH_FETCHES if operation == "fetch" else MAX_RETAINED_REQUESTS
        hire_limit = MAX_RETAINED_WITH_FETCHES_PER_HIRE if operation == "fetch" else MAX_RETAINED_PER_HIRE
        if len(ids) >= global_limit:
            return codec.encode(
                {"ok": False, "error": "request capacity is full; retry after results expire"}
            )
        retained_for_hire = sum(
            1
            for request_id in ids
            if (self.storage.get("request:" + str(request_id)) or {}).get("hire_id")
            == hire_id
        )
        if retained_for_hire >= hire_limit:
            return codec.encode(
                {"ok": False, "error": "this hire's retained request capacity is full"}
            )
        if operation == "scrape":
            jobs = list(self.storage.get(KEY_JOB_IDS, []))
            pending = [
                self.storage.get("request:" + str(request_id)) or {}
                for request_id in ids
            ]
            pending = [record for record in pending if record.get("operation") == "scrape" and record.get("status") == "pending"]
            if len(jobs) + len(pending) >= MAX_RETAINED_REQUESTS:
                return codec.encode({"ok": False, "error": "active scrape job capacity is full"})
            pending_for_hire = sum(1 for record in pending if record.get("hire_id") == hire_id)
            jobs_for_hire = sum(
                1 for job_source_id in jobs
                if (self.storage.get("job:" + str(job_source_id)) or {}).get("hire_id") == hire_id
            )
            if jobs_for_hire + pending_for_hire >= MAX_RETAINED_PER_HIRE:
                return codec.encode({"ok": False, "error": "this hire's active scrape job capacity is full"})
        request_id = _bounded_int(
            int(self.storage.get(KEY_CALLS, 0)) + 1, "request_id", 1, 2**64 - 1
        )
        now = int(runtime.get_block_height())
        record = {
            "request_id": request_id,
            "hire_id": hire_id,
            "buyer": buyer,
            "operation": operation,
            "status": "pending",
            "cid": cid,
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        if source_id is not None:
            binding = self.storage.get("job:" + str(source_id)) or {}
            binding["fetch_request_id"] = request_id
            self.storage["job:" + str(source_id)] = binding
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: "${%s}" % SECRET_KEY,
            "User-Agent": USER_AGENT,
        }
        job = {
            "url": url,
            "method": method,
            "headers": headers,
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
            # raw_body sends exact JSON bytes so the node cannot re-adapt it.
            job["raw_body"] = _json_bytes(body)
        # GET fetch omits the body kwarg entirely (a GET carrying a body is
        # rejected on chain with E1604) and sends no Content-Type.
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

    def _retry_fetch(self, request_id, source_id, hire_id, hire, job_id):
        key = "request:" + str(request_id)
        record = self.storage.get(key) or {}
        cid = continuation.new_cid(self, "_run_request")
        for retained_id in self.storage.get(KEY_REQUEST_IDS, []):
            retained = self.storage.get("request:" + str(retained_id)) or {}
            if retained.get("status") == "pending" and retained.get("cid") == cid:
                return codec.encode(
                    {"ok": False, "error": "transaction already has a pending request"}
                )
        if record.get("status") == "pending":
            return self._refuse("fetch", "the previous fetch is still pending")
        now = int(runtime.get_block_height())
        record["status"] = "pending"
        record["cid"] = cid
        record["deadline_height"] = now + REQUEST_LIFETIME_BLOCKS
        record.pop("output", None)
        record.pop("finished_height", None)
        record.pop("expires_height", None)
        self.storage[key] = record
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: "${%s}" % SECRET_KEY,
            "User-Agent": USER_AGENT,
        }
        job = {
            "url": SCRAPE_ENDPOINT + "/" + job_id,
            "method": "GET",
            "headers": headers,
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {
            "ok": True, "request_id": request_id, "hire_id": hire_id,
            "operation": "fetch", "status": "pending", "poll_handler": "get_result",
        }
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        # Continuation ctx is PUBLIC on-chain state: it carries only the request
        # id (a routing pin), never a credential. The key rides the job as a
        # `${...}` reference resolved runner-side, not through this ctx.
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
        decoded = {}
        if int(runtime.get_block_height()) >= int(record["deadline_height"]):
            output = {
                "ok": False,
                "operation": operation,
                "error": "request result did not arrive before its deadline",
            }
        else:
            decoded = _decode_result(result)
            output = self._build_output(operation, decoded)
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
        if operation == "scrape" and output["ok"]:
            data = output.get("data") or {}
            job_id = data.get("job_id")
            now = int(runtime.get_block_height())
            self.storage["job:" + str(request_id)] = {
                "source_request_id": request_id,
                "hire_id": record["hire_id"],
                "buyer": record["buyer"],
                "job_id": job_id,
                "expires_height": now + JOB_LIFETIME_BLOCKS,
            }
            jobs = list(self.storage.get(KEY_JOB_IDS, []))
            if request_id not in jobs:
                self.storage[KEY_JOB_IDS] = jobs + [request_id]
        if operation == "fetch" and result is not None:
            status = decoded.get("status") if isinstance(decoded, dict) else None
            if status == JOB_DONE or status in JOB_FAILED:
                self._delete_job_for_fetch(request_id)
        self.storage[KEY_LAST_STATUS] = record["status"]
        # M2: metadata only into never-expiring public state (no job id / data).
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

    def _build_output(self, operation, decoded):
        if _is_vendor_error(decoded):
            return {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
        if operation == "scrape":
            job_id = decoded.get("jobId")
            if not _valid_job_id(job_id):
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "unexpected scrape response from hyperbrowser",
                }
            return {
                "ok": True,
                "operation": operation,
                "data": {"job_id": job_id, "job_status": "submitted"},
            }
        # operation == "fetch": GET /api/scrape/{jobId}
        status = decoded.get("status")
        if not isinstance(status, str) or not status:
            return {
                "ok": False,
                "operation": operation,
                "error": "unexpected fetch response from hyperbrowser",
            }
        if status in JOB_FAILED:
            return {
                "ok": False,
                "operation": operation,
                "error": _truncate_text_bytes(
                    (decoded.get("error") if isinstance(decoded.get("error"), str) else "")
                    or ("scrape job %s" % status),
                    MAX_ERROR_CHARS,
                ),
            }
        if status != JOB_DONE:
            # Not finished yet — a valid transient state; the buyer re-fetches.
            return {
                "ok": True,
                "operation": operation,
                "data": {"job_status": status},
            }
        vendor = decoded.get("data")
        if not isinstance(vendor, dict):
            return {
                "ok": False,
                "operation": operation,
                "error": "completed scrape had no data",
            }
        content = {"job_status": JOB_DONE}
        meta = vendor.get("metadata")
        if isinstance(meta, dict):
            content["metadata"] = meta
        if isinstance(vendor.get("markdown"), str):
            content["markdown"] = vendor["markdown"]
        if isinstance(vendor.get("links"), list):
            content["links"] = [item for item in vendor["links"] if isinstance(item, str)]
        if len(codec.encode(content)) > API_BYTE_BUDGET and "markdown" in content:
            content.pop("markdown")
            content["_cowboy"] = {"markdown_omitted": True, "limit_bytes": API_BYTE_BUDGET}
        if len(codec.encode(content)) > API_BYTE_BUDGET:
            return {
                "ok": False,
                "operation": operation,
                "error": "exact status, metadata, and links exceed the result byte budget",
            }
        return {"ok": True, "operation": operation, "data": content}

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
        jobs = list(self.storage.get(KEY_JOB_IDS, []))
        live_jobs = []
        for source_id in jobs:
            binding = self.storage.get("job:" + str(source_id))
            if binding and now < binding.get("expires_height", 0):
                live_jobs.append(source_id)
            elif binding:
                del self.storage["job:" + str(source_id)]
        if live_jobs != jobs:
            self.storage[KEY_JOB_IDS] = live_jobs
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

    def _delete_job_for_fetch(self, request_id):
        jobs = list(self.storage.get(KEY_JOB_IDS, []))
        retained = []
        for source_id in jobs:
            key = "job:" + str(source_id)
            binding = self.storage.get(key) or {}
            if binding.get("fetch_request_id") == request_id:
                if self.storage.get(key):
                    del self.storage[key]
            else:
                retained.append(source_id)
        self.storage[KEY_JOB_IDS] = retained

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
                "slot": "web-scrape",
                "handlers": ["scrape", "fetch"],
                "poll_handler": "get_result",
                "returns": "hyperbrowser-scrape-async-result",
                "mutating": {"scrape": True, "fetch": False},
                "idempotency": "none; Hyperbrowser scrape has no idempotency key, so a runner retry of the submit could re-scrape and double-bill",
                "flow": "call scrape (get job_id), wait for the job to finish, then call fetch(source_request_id) to read bounded content",
                "privacy": "all inputs and results are public chain data; do not scrape private/authenticated pages",
                "scrape_input": {
                    "hire_id": "int",
                    "url": "http(s) str",
                    "formats": "str[]? subset of %s (default %s)" % (
                        list(ALLOWED_FORMATS), DEFAULT_FORMATS
                    ),
                },
                "fetch_input": {"hire_id": "int", "source_request_id": "int (a prior scrape)"},
                "output": "acceptance record; get_result returns {job_id} (scrape) or {job_status,metadata,markdown?,links?} (fetch) under output.data",
                "excluded": [
                    "sessions", "session_options(stealth/useProxy/solveCaptchas/proxy*)",
                    "crawl", "batch_scrape", "extract", "html_format", "screenshot_format",
                ],
                "excluded_reason": (
                    "sessions return a live CDP wsEndpoint/token that get_result would place "
                    "in public consensus state (G5 live-credential wall); crawl/batch are "
                    "multi-page and async with no bounded delivery; html/screenshot and large "
                    "markdown overflow the runner transport cap and park; stealth/proxy/"
                    "captcha-solving options are deliberately not exposed"
                ),
                "hire_activation": {"protocol": "store-admin-v1"},
                "runner_result_byte_budget": RUNNER_RESULT_BYTE_BUDGET,
                "result_byte_budget": RETURN_BYTE_BUDGET,
                "max_result_chunk_bytes": MAX_RESULT_CHUNK_BYTES,
                "max_url_chars": MAX_URL_CHARS,
                "max_retained_requests": MAX_RETAINED_REQUESTS,
                "max_retained_per_hire": MAX_RETAINED_PER_HIRE,
                "max_retained_with_fetches": MAX_RETAINED_WITH_FETCHES,
                "max_retained_with_fetches_per_hire": MAX_RETAINED_WITH_FETCHES_PER_HIRE,
                "job_lifetime_blocks": JOB_LIFETIME_BLOCKS,
                "max_registered_hires": MAX_REGISTERED_HIRES,
                "request_lifetime_blocks": REQUEST_LIFETIME_BLOCKS,
                "result_retention_blocks": RESULT_RETENTION_BLOCKS,
            }
        )

    @public
    def get_last_output(self, payload):
        # Observability only — never a result read (that is authenticated
        # get_result). Metadata only, never the job id / vendor data / error.
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
                "api_root": API_ROOT,
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
    return HyperbrowserScrapeWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def scrape(payload):
    return _get_actor().scrape(payload)


def fetch(payload):
    return _get_actor().fetch(payload)


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
