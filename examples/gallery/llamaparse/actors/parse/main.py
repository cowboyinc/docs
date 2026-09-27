"""Hire-gated LlamaParse document-parse actor.

Every step is asynchronous and hire-gated. `parse` submits a document URL and
returns an acceptance record; the registered hirer reads the LlamaParse job id
from `get_result`. `fetch_result` then polls that job and, once it finishes,
returns a small presigned-URL handle to the parsed markdown. The LlamaParse API
key stays in CBSS as `LLAMAPARSE_API_KEY` and only ever rides a job as a
`${...}` reference resolved runner-side.

Transport-cap design (the central constraint). LlamaParse is a submit->poll API
whose parsed markdown/json/text is routinely LARGE. The runner->actor transport
cap (~40-64KB, the "40KB cliff") means any whole inline result would silently
PARK the continuation: it never resumes and the request stays pending until its
deadline, then errors, while the submit tx still reports success. The runner has
only top-level `extract_path` selection and cannot reshape or truncate nested content, and client-side bounding runs *inside* the continuation, after
transport, so it never sees an oversized reply. So this wrapper never asks
LlamaParse for the inline result. It fetches the v2 result with
`expand=markdown_content_metadata`, which returns a small presigned S3 download
URL (plus size/exists) INSTEAD of the markdown itself. The reply the runner must
deliver is therefore always metadata + a URL and reliably fits the cap. The
buyer downloads the full parsed markdown from the presigned URL itself.

The presigned URL is publicly fetchable WITHOUT the LlamaParse key (the S3
signature is embedded in the query string) and short-lived, so returning it
hands over a temporary bearer download capability that is stored in public consensus state. The result endpoint `GET /api/v2/parse/{id}` is itself
Bearer-authenticated with the key and so is never handed to the buyer; only the
presigned URL drawn from its response is. This mirrors Reducto's force_url_result
lesson; the inline v1 `/result/markdown|json|text` and v2 `expand=markdown`
shapes are excluded because they park.

Parse is billable and LlamaParse v2 exposes no verified idempotency key, so a
runner retry could re-submit and double-bill; dispatch is one submit per request
(same-transaction cid dedup + a single continuation), see README.
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


VENDOR = "llamaparse"
ACTOR_VERSION = "1.0.0"
API_ROOT = "https://api.cloud.llamaindex.ai"
PARSE_ENDPOINT = "https://api.cloud.llamaindex.ai/api/v2/parse"
RESULT_ENDPOINT_PREFIX = "https://api.cloud.llamaindex.ai/api/v2/parse/"
RESULT_EXPAND = "markdown_content_metadata"
# v2 POST /parse REQUIRES tier + version (verified 2026-09-15 against the live
# API — a body omitting them 422s "Field required"). tier ∈ fast |
# cost_effective | agentic | agentic_plus; version "latest" floats to the newest
# parser. `expand=markdown_content_metadata` nulls the inline markdown/text and
# returns the content only as a presigned URL, so the reply stays small
# regardless of document size (no transport-cap park).
PARSE_TIER = "cost_effective"
PARSE_VERSION = "latest"
SECRET_KEY = "LLAMAPARSE_API_KEY"
AUTH_HEADER = "Authorization"
USER_AGENT = "cowboy-store-wrapper/1.0.0"

# The one host that authenticates result reads with the API key. A returned
# result URL on this host would be a credential, so it is refused.
KEY_AUTH_HOST = "api.cloud.llamaindex.ai"

ALLOWED_SCHEMES = ("https://", "http://")

SUCCESS_STATUSES = ("SUCCESS", "COMPLETED", "OK", "PARTIAL_SUCCESS")
PENDING_STATUSES = (
    "PENDING",
    "NOT_STARTED",
    "IN_PROGRESS",
    "PROCESSING",
    "RUNNING",
    "QUEUED",
)

MAX_URL_CHARS = 2_048
MAX_RESULT_URL_CHARS = 4_096
MAX_JOB_ID_CHARS = 64
MAX_ID_CHARS = 200
MAX_ERROR_CHARS = 500
MAX_REQUEST_BYTES = 8_000

SUPPORTED_PARSE_FIELDS = ("hire_id", "document_url")
SUPPORTED_FETCH_FIELDS = ("hire_id", "request_id")

RUNNER_RESULT_BYTE_BUDGET = 65_536
RETURN_BYTE_BUDGET = 38_000
MAX_RESULT_CHUNK_BYTES = 3_900
MAX_RETAINED_REQUESTS = 16
MAX_RETAINED_PER_HIRE = 6
MAX_RETAINED_WITH_FETCHES = 32
MAX_RETAINED_WITH_FETCHES_PER_HIRE = 12
MAX_REGISTERED_HIRES = 512
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150
JOB_LIFETIME_BLOCKS = 7_200

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
    return raw[: max(0, byte_limit)].decode("utf-8", "ignore")


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


def _job_id_token(value):
    # A LlamaParse job id is placed verbatim into the result URL path, so it is
    # constrained to a safe token even though it comes from the vendor.
    text = str(value or "").strip()
    if not text or len(text) > MAX_JOB_ID_CHARS:
        return ""
    for char in text:
        if not (char.isalnum() or char in "-_"):
            return ""
    return text


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
    # LlamaParse is FastAPI: errors are {"detail": ...}. Surface the vendor's own
    # message from whatever error-shaped field is present so a failure is never
    # relabeled a bland success; a non-JSON/raw reply is itself an error.
    if not isinstance(decoded, dict):
        return "llamaparse returned a non-JSON response"
    message = _detail_message(decoded.get("detail"))
    if message:
        return _truncate_text_bytes(message, MAX_ERROR_CHARS)
    job = decoded.get("job")
    if isinstance(job, dict):
        for key in ("error_message", "error", "message"):
            text = job.get(key)
            if isinstance(text, str) and text:
                return _truncate_text_bytes(text, MAX_ERROR_CHARS)
    for key in ("error", "message", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return _truncate_text_bytes(value, MAX_ERROR_CHARS)
    if "raw" in decoded:
        return "llamaparse returned a non-JSON response"
    return "unexpected response from llamaparse"


def _has_error_marker(decoded):
    if not isinstance(decoded, dict):
        return True
    return any(key in decoded for key in ("detail", "error", "raw", "raw_text"))


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


def _submit_job_id(decoded):
    if not isinstance(decoded, dict):
        return ""
    raw = decoded.get("id")
    if not isinstance(raw, str):
        raw = decoded.get("job_id")
    return _job_id_token(raw) if isinstance(raw, str) else ""


def _valid_submit(decoded):
    if not isinstance(decoded, dict) or _has_error_marker(decoded):
        return False
    return bool(_submit_job_id(decoded))


def _submit_data(decoded):
    data = {"job_id": _submit_job_id(decoded)}
    status = _clip_str(decoded.get("status"), MAX_ID_CHARS)
    if status:
        data["vendor_status"] = status
    return data


def _job_object(decoded):
    # A v2 retrieval nests fields under `job`; a create response is flat. Accept
    # either so the same decoder handles both without guessing.
    if isinstance(decoded, dict) and isinstance(decoded.get("job"), dict):
        return decoded["job"]
    return decoded if isinstance(decoded, dict) else {}


def _presigned_url(decoded):
    # Pull the markdown presigned URL. Live (verified 2026-09-15) the result puts
    # `status` under `job` but `result_content_metadata` at the TOP LEVEL (a
    # sibling of `job`), so search BOTH roots — the top-level object and its
    # `job` sub-object — for result_content_metadata.{md,markdown,markdown_full}
    # .presigned_url or a flat markdown_content_metadata.presigned_url.
    containers = []
    roots = [decoded]
    if isinstance(decoded, dict) and isinstance(decoded.get("job"), dict):
        roots.append(decoded["job"])
    for root in roots:
        if not isinstance(root, dict):
            continue
        rcm = root.get("result_content_metadata")
        if isinstance(rcm, dict):
            for key in ("md", "markdown", "markdown_full"):
                if isinstance(rcm.get(key), dict):
                    containers.append(rcm[key])
        for key in ("markdown_content_metadata", "markdown_full_content_metadata"):
            if isinstance(root.get(key), dict):
                containers.append(root[key])
    for meta in containers:
        url = meta.get("presigned_url") or meta.get("url")
        if isinstance(url, str) and url:
            size = _clip_int(meta.get("size_bytes"), 0, 2**63 - 1)
            return url, size
    return None, None


# The parsed-markdown handle is a presigned object-store URL (S3/GCS/Azure),
# publicly fetchable without the key for a short window. Two properties make it
# safe to hand to the buyer, and BOTH are ENFORCED here, not merely advertised:
#  (1) it is NOT on the key-auth host tree — the whole *.llamaindex.ai tree is
#      rejected (bare parent, any subdomain, and a trailing-dot FQDN), so the
#      key-authenticated result endpoint can never be returned as a credential;
#  (2) it carries a presigned signature marker — a URL without one is not the
#      short-lived download handle the vendor is documented to return.
_KEY_AUTH_HOST_TREE = ("llamaindex.ai",)
_PRESIGN_MARKERS = (
    "x-amz-signature=",   # AWS SigV4
    "x-amz-credential=",  # AWS SigV4
    "awsaccesskeyid=",    # AWS SigV2 / old-style query auth — LlamaParse's S3 uses this
    "signature=",         # AWS SigV2 signature / generic signed-query handle
    "x-goog-signature=",  # Google Cloud Storage
    "sig=",               # Azure SAS
)


def _valid_result_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_RESULT_URL_CHARS:
        return False
    lowered = url.lower()
    if not lowered.startswith("https://"):
        return False
    if "${" in url:
        return False
    # Userinfo is stripped (the real host is after the last '@') and a trailing
    # dot normalized so neither can dodge the host-tree rejection.
    authority = lowered[len("https://") :].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = authority.rsplit("@", 1)[-1].rsplit(":", 1)[0].rstrip(".")
    if any(host == h or host.endswith("." + h) for h in _KEY_AUTH_HOST_TREE):
        return False
    return any(marker in lowered for marker in _PRESIGN_MARKERS)


@actor
class LlamaParseWrapper:
    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "LlamaParse"
        self.storage["__description__"] = (
            "Bounded LlamaParse document parse for registered Store Hires. "
            "Vendor key held in CBSS; returns a presigned result URL."
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

    def _authorize(self, req, operation, allowed_fields):
        hire_id, hire, refusal = self._hire(req, operation)
        if refusal is not None:
            return hire_id, hire, refusal
        unsupported = sorted(key for key in req if key not in allowed_fields)
        if unsupported:
            return None, None, self._refuse(
                operation, "unsupported request fields: %s" % ", ".join(unsupported)
            )
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
        # only, never the full output (which can carry a result URL or job id).
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
        hire_id, hire, refusal = self._authorize(req, "parse", SUPPORTED_PARSE_FIELDS)
        if refusal is not None:
            return refusal
        try:
            source_url = _document_url(req.get("document_url"))
        except ValueError as exc:
            return self._refuse("parse", str(exc))
        body = {"source_url": source_url, "tier": PARSE_TIER, "version": PARSE_VERSION}
        if len(_json_bytes(body)) > MAX_REQUEST_BYTES:
            return self._refuse("parse", "request exceeds %d bytes" % MAX_REQUEST_BYTES)
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
        return self._dispatch(hire_id, "parse", job)

    @public
    @deferred
    def fetch_result(self, payload):
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(
            req, "fetch_result", SUPPORTED_FETCH_FIELDS
        )
        if refusal is not None:
            return refusal
        submit_id = _bounded_int(req.get("request_id"), "request_id", 1, 2**64 - 1)
        # The submit request carries the buyer+hire binding, so referencing it by
        # id (rather than accepting a raw job id) is what stops hire B fetching
        # hire A's job: B is not the buyer of A's submit request.
        self._cleanup()
        submit = self.storage.get("job:" + str(submit_id))
        if (
            not submit
            or submit.get("buyer") != _sender_hex()
            or submit.get("hire_id") != hire_id
        ):
            return self._refuse(
                "fetch_result", "parse request not found for caller and hire"
            )
        job_id = _job_id_token(submit.get("job_id"))
        if not job_id:
            return self._refuse("fetch_result", "parse submission has no job id")
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: "Bearer ${%s}" % SECRET_KEY,
            "User-Agent": USER_AGENT,
        }
        # A GET carrying a body (even "") is rejected by the node as E1604; the
        # `body`/`Content-Type` keys are OMITTED entirely, not set empty.
        job = {
            "url": RESULT_ENDPOINT_PREFIX + job_id + "?expand=" + RESULT_EXPAND,
            "method": "GET",
            "headers": headers,
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        fetch_id = submit.get("fetch_request_id")
        if fetch_id and not self.storage.get("request:" + str(fetch_id)):
            submit.pop("fetch_request_id", None)
            self.storage["job:" + str(submit_id)] = submit
            fetch_id = None
        if fetch_id:
            return self._retry_fetch(fetch_id, hire_id, job)
        return self._dispatch(hire_id, "fetch_result", job, submit_id)

    def _dispatch(self, hire_id, operation, job, submit_id=None):
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
        global_limit = MAX_RETAINED_WITH_FETCHES if operation == "fetch_result" else MAX_RETAINED_REQUESTS
        hire_limit = MAX_RETAINED_WITH_FETCHES_PER_HIRE if operation == "fetch_result" else MAX_RETAINED_PER_HIRE
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
        if operation == "parse":
            jobs = list(self.storage.get(KEY_JOB_IDS, []))
            pending = [self.storage.get("request:" + str(item)) or {} for item in ids]
            pending = [item for item in pending if item.get("operation") == "parse" and item.get("status") == "pending"]
            if len(jobs) + len(pending) >= MAX_RETAINED_REQUESTS:
                return codec.encode({"ok": False, "error": "active parse job capacity is full"})
            active_for_hire = sum(1 for item in jobs if (self.storage.get("job:" + str(item)) or {}).get("hire_id") == hire_id)
            pending_for_hire = sum(1 for item in pending if item.get("hire_id") == hire_id)
            if active_for_hire + pending_for_hire >= MAX_RETAINED_PER_HIRE:
                return codec.encode({"ok": False, "error": "this hire's active parse job capacity is full"})
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
        if submit_id is not None:
            binding = self.storage.get("job:" + str(submit_id)) or {}
            binding["fetch_request_id"] = request_id
            self.storage["job:" + str(submit_id)] = binding
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

    def _retry_fetch(self, request_id, hire_id, job):
        key = "request:" + str(request_id)
        record = self.storage.get(key) or {}
        cid = continuation.new_cid(self, "_run_request")
        for retained_id in self.storage.get(KEY_REQUEST_IDS, []):
            retained = self.storage.get("request:" + str(retained_id)) or {}
            if retained.get("status") == "pending" and retained.get("cid") == cid:
                return codec.encode({"ok": False, "error": "transaction already has a pending request"})
        if record.get("status") == "pending":
            return self._refuse("fetch_result", "the previous fetch is still pending")
        now = int(runtime.get_block_height())
        record["status"] = "pending"
        record["cid"] = cid
        record["deadline_height"] = now + REQUEST_LIFETIME_BLOCKS
        record.pop("output", None)
        record.pop("finished_height", None)
        record.pop("expires_height", None)
        self.storage[key] = record
        self._run_request(codec.encode({"request_id": request_id, "job": job}))
        accepted = {"ok": True, "request_id": request_id, "hire_id": hire_id, "operation": "fetch_result", "status": "pending", "poll_handler": "get_result"}
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
        if int(runtime.get_block_height()) >= int(record["deadline_height"]):
            output = {
                "ok": False,
                "operation": operation,
                "error": "request result did not arrive before its deadline",
            }
        elif operation == "parse":
            output = self._submit_output(result)
        else:
            output = self._fetch_output(result)
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
        if operation == "parse" and output["ok"]:
            job_id = ((output.get("data") or {}).get("job_id"))
            self.storage["job:" + str(request_id)] = {"source_request_id": request_id, "hire_id": record["hire_id"], "buyer": record["buyer"], "job_id": job_id, "expires_height": now + JOB_LIFETIME_BLOCKS}
            jobs = list(self.storage.get(KEY_JOB_IDS, []))
            if request_id not in jobs:
                self.storage[KEY_JOB_IDS] = jobs + [request_id]
        if operation == "fetch_result" and result is not None:
            decoded = _decode_result(result)
            status = str((_job_object(decoded)).get("status") or "").strip().upper()
            if status and status not in PENDING_STATUSES:
                self._delete_job_for_fetch(request_id)
        self.storage[KEY_LAST_STATUS] = record["status"]
        # M2: metadata only into never-expiring public state (no url/job id/data).
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

    def _submit_output(self, result):
        decoded = _decode_result(result)
        if not _valid_submit(decoded):
            return {"ok": False, "operation": "parse", "error": _error_text(decoded)}
        return {"ok": True, "operation": "parse", "data": _submit_data(decoded)}

    def _fetch_output(self, result):
        decoded = _decode_result(result)
        if _has_error_marker(decoded):
            return {"ok": False, "operation": "fetch_result", "error": _error_text(decoded)}
        job = _job_object(decoded)
        status = str(job.get("status") or "").strip().upper()
        if status in PENDING_STATUSES:
            return {
                "ok": True,
                "operation": "fetch_result",
                "data": {"ready": False, "vendor_status": status},
            }
        if status not in SUCCESS_STATUSES:
            return {
                "ok": False,
                "operation": "fetch_result",
                "error": _error_text(decoded) if status == "" else "parse job status: " + status,
            }
        url, size = _presigned_url(decoded)
        if not _valid_result_url(url):
            return {
                "ok": False,
                "operation": "fetch_result",
                "error": "parse result has no usable presigned markdown URL",
            }
        data = {"ready": True, "result_type": "markdown", "result_url": url}
        if size is not None:
            data["size_bytes"] = size
        return {"ok": True, "operation": "fetch_result", "data": data}

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
        for submit_id in jobs:
            binding = self.storage.get("job:" + str(submit_id))
            if binding and now < binding.get("expires_height", 0):
                live_jobs.append(submit_id)
            elif binding:
                del self.storage["job:" + str(submit_id)]
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
        retained = []
        for submit_id in self.storage.get(KEY_JOB_IDS, []):
            key = "job:" + str(submit_id)
            binding = self.storage.get(key) or {}
            if binding.get("fetch_request_id") == request_id:
                if self.storage.get(key):
                    del self.storage[key]
            else:
                retained.append(submit_id)
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
                "slot": "document-parse",
                "handlers": ["parse", "fetch_result"],
                "poll_handler": "get_result",
                "returns": "llamaparse-async-result",
                "mutating": True,
                "idempotency": "none verified; LlamaParse v2 /parse is billable and exposes no confirmed idempotency key, so a runner retry could re-submit and double-bill",
                "privacy": "all inputs and results are public chain data; the presigned result URL is a temporary bearer download capability",
                "flow": "parse(document_url) -> get_result shows {job_id}; fetch_result(request_id of the parse) -> get_result shows {ready, result_url}",
                "input": {
                    "parse": {"hire_id": "int", "document_url": "str (https/http URL to the document)"},
                    "fetch_result": {"hire_id": "int", "request_id": "int (the parse submission's request id)"},
                },
                "output": "acceptance record; get_result returns {job_id,vendor_status} for a parse and {ready,result_type,result_url,size_bytes} for a fetch_result under output.data",
                "result_url_note": "presigned, short-lived, publicly fetchable without the key; download it promptly",
                "excluded": [
                    "inline v2 expand=markdown/text/items (large inline content parks over the transport cap)",
                    "v1 /api/v1/parsing/job/{id}/result/markdown|json|text (inline content parks)",
                    "file upload / multipart (only remote document URLs are accepted)",
                    "webhooks",
                ],
                "hire_activation": {"protocol": "store-admin-v1"},
                "max_url_chars": MAX_URL_CHARS,
                "max_result_url_chars": MAX_RESULT_URL_CHARS,
                "runner_result_byte_budget": RUNNER_RESULT_BYTE_BUDGET,
                "result_byte_budget": RETURN_BYTE_BUDGET,
                "max_result_chunk_bytes": MAX_RESULT_CHUNK_BYTES,
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
    return LlamaParseWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def parse(payload):
    return _get_actor().parse(payload)


def fetch_result(payload):
    return _get_actor().fetch_result(payload)


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
