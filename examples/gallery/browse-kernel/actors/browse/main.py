"""Hire-gated Kernel cloud-browser Store wrapper.

Two capabilities, both hire-gated and asynchronous (an acceptance record now,
the bounded page state via ``get_result`` later):

  * ``visit`` — a STATELESS one-shot read. The open->goto->extract->close
    lifecycle runs SERVER-SIDE in the deployed ``cowboy-browse`` Kernel app's
    ``visit`` action, so the wrapper is ONE ``runner.http`` POST with no session.
  * ``step`` — a STATEFUL op against a PERSISTENT server-side Kernel session:
    op in {open, goto, click, type, press, scroll, extract, close}. Each op is
    still ONE ``runner.http`` POST to the Kernel app's ``step`` action.

PER-HIRE SESSION (the key safety property)
------------------------------------------
The caller NEVER supplies a ``session_id``. The actor stores AT MOST ONE session
per hire under ``session:<hire_id>`` and passes that hire's own stored session id
to the Kernel app. ``open`` (or the auto-open the Kernel app performs on the
first ``goto`` when the hire has no session) sets it; a confirmed ``close``
clears it, while a failed close leaves a retry-only binding. Because a hire can
only ever act on the session THIS actor stored for it — and a
caller-supplied ``session_id`` is refused as an unsupported field — a caller can
never name another hire's session. There is no cross-hire session-stealing hole.

Every vendor call is one ``runner.http`` launched from the private
``@runner.continuation _run_request``; the executor rejects an oversized page
result before it reaches the runner transport boundary. Mutating ops
(open/goto/click/type/press/scroll/close) carry an ``Idempotency-Key`` derived
from the request id; provider-side deduplication depends on Kernel's invocation
contract. ``extract`` and ``visit`` are read-only.
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


VENDOR = "kernel"
ACTOR_VERSION = "2.0.0"
INVOKE_URL = "https://api.onkernel.com/invocations"
# The deployed executor app (examples/gallery/browse-kernel/kernel-app) and its
# two actions. The Kernel API drives browsers over a CDP websocket that
# runner.http cannot hold, so Playwright runs inside this app: `visit` does
# open->goto->extract->close server-side in one call; `step` runs ONE Playwright
# op against a persistent server-side session.
KERNEL_APP = "cowboy-browse"
VISIT_ACTION = "visit"
STEP_ACTION = "step"
SECRET_KEY = "KERNEL_API_KEY"  # static vendor key; no OAuth, no refresh
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"
USER_AGENT = "cowboy-store-wrapper/2.0.0"

# op ordering here is the pinned public op set; the parity guard checks it.
STEP_OPS = ("open", "goto", "click", "type", "press", "scroll", "extract", "close")
# Mutating ops change server-side state and carry a stable request identity.
# `extract` re-reads and `visit` is a one-shot read — both read-only.
MUTATING_OPS = ("open", "goto", "click", "type", "press", "scroll", "close")
READONLY_OPS = ("extract",)
# ops that require an already-open session at accept time (nothing to act on a
# page never navigated to). `open` creates one; `goto` auto-opens when absent.
OPS_NEED_SESSION = ("click", "type", "press", "scroll", "extract", "close")

VISIT_INPUT_FIELDS = ("hire_id", "url")
# `session_id` is intentionally NOT accepted from the caller — the actor sources
# it from the hire's own stored session. A supplied one is refused as unsupported.
STEP_INPUT_FIELDS = ("hire_id", "op", "url", "selector", "text", "key", "dy", "stealth", "timeout_seconds")

MAX_URL_CHARS = 2_048
MAX_TITLE_CHARS = 400
MAX_LINKS = 40
MAX_LINK_TEXT_CHARS = 200
MAX_LINK_HREF_CHARS = 2_048
MAX_ERROR_CHARS = 500
MAX_ID_CHARS = 200
MAX_REQUEST_BYTES = 8_000
MAX_SELECTOR_CHARS = 1_024
MAX_TEXT_CHARS = 4_096
MAX_KEY_CHARS = 64
MAX_SESSION_ID_CHARS = 128
MAX_SCROLL_DELTA = 1_000_000
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3_600

RUNNER_RESULT_BYTE_BUDGET = 65_536
RETURN_BYTE_BUDGET = 38_000
TEXT_BYTE_BUDGET = 34_000  # leaves framing room for url/title/links under the budget
MAX_RESULT_CHUNK_BYTES = 3_900
MAX_RETAINED_REQUESTS = 16
MAX_RETAINED_PER_HIRE = 6
MAX_REGISTERED_HIRES = 512
MAX_ACTIVE_SESSIONS = 8  # server-side browsers cost money; bound the fleet
# A close remains reachable when ordinary receipts fill the request budget.
# At most one close can be pending per active session, so this remains finite.
MAX_RETAINED_WITH_CLOSES = 24
REQUEST_LIFETIME_BLOCKS = 150
RESULT_RETENTION_BLOCKS = 150
# Maximum session deadline: an abandoned session is best-effort closed and its
# record cleared on the next full cleanup pass. Kernel also self-expires the
# server-side browser on its own timeout_seconds, so this is proactive housekeeping.
SESSION_LIFETIME_BLOCKS = 300

KEY_REGISTRAR = "store_registrar"
KEY_SECRET_ACCOUNT = "secret_account"
KEY_HIRE_COUNT = "hire_count"
KEY_CALLS = "call_count"
KEY_REQUEST_IDS = "request_ids"
KEY_SESSION_HIRES = "session_hires"
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


def _unwrap_invocation(decoded):
    """A Kernel /invocations reply is {status, output:"<json string>"}; the
    action's real result lives in `output`.

    Hardening: when an envelope shape is present (`output` or `status`), status
    MUST be exactly "succeeded" — a missing/other status is treated as an error,
    never a false success. A reply already shaped like the action result (no
    envelope keys) passes through."""
    if not isinstance(decoded, dict):
        return decoded
    if "output" not in decoded and "status" not in decoded:
        return decoded
    status = str(decoded.get("status") or "")
    out = decoded.get("output")
    if isinstance(out, str) and out:
        try:
            out = json.loads(out)
        except Exception:
            return {"raw_text": out[:MAX_ERROR_CHARS]}
    if status != "succeeded":
        detail = decoded.get("error") or out or (status or "missing status")
        return {"error": "invocation %s" % (status or "missing status"), "message": str(detail)[:MAX_ERROR_CHARS]}
    return out if isinstance(out, dict) else {"raw": out}


def _json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _invocation_body(action_name, step):
    # payload is a JSON STRING the app JSON.parses; the op + its fields ride
    # inside it, never as top-level invocation keys. raw_body sends exact bytes.
    payload = json.dumps(
        step, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return _json_bytes({"app_name": KERNEL_APP, "action_name": action_name, "payload": payload})


def _truncate_text_bytes(text, byte_limit):
    raw = text.encode("utf-8")
    if len(raw) <= byte_limit:
        return text
    return raw[:max(0, byte_limit)].decode("utf-8", "ignore")


def _contains_secret_template(value):
    # SECURITY CONTROL: the runner substitutes ${NAME} into the body before
    # sending, so a caller ${...} could paste the resolved key into an outbound
    # field. Refuse it in all caller input before dispatch.
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


def _http_url(value):
    """Only http(s). A javascript:/file:/data: URL would execute locally in the
    browser rather than fetching. A bare domain is normalised to https."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("url is required")
    if len(text.encode("utf-8")) > MAX_URL_CHARS:
        raise ValueError("url must be at most %d bytes" % MAX_URL_CHARS)
    lowered = text.lower()
    if (
        "://" not in text
        and not lowered.startswith(("javascript:", "file:", "data:", "about:"))
        and "." in text.split("/")[0]
    ):
        text = "https://" + text
        lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("url must be an http(s) URL")
    if any(ch in text for ch in ("\r", "\n", '"', "\\")):
        raise ValueError("url contains illegal characters")
    for char in text:
        if ord(char) < 0x20 or char.isspace():
            raise ValueError("url must not contain whitespace or control characters")
    return text


def _clean_str(value, field, max_chars, allow_empty=False):
    """A bounded, control-char-free string for a Playwright selector/text/key.

    CR/LF and other control characters are refused: they could smuggle unwanted
    structure into the interaction and have no business in a selector or a
    keystroke. ${...} is refused separately (the actor-wide secret-template guard)."""
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    if not value and not allow_empty:
        raise ValueError("%s is required" % field)
    if len(value.encode("utf-8")) > max_chars:
        raise ValueError("%s must be at most %d bytes" % (field, max_chars))
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError("%s must not contain control characters" % field)
    return value


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "kernel request failed"
    for key in ("error", "message", "err_msg", "raw_text", "code"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return _truncate_text_bytes(value, MAX_ERROR_CHARS)
        if isinstance(value, dict):
            for inner in ("message", "error", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return _truncate_text_bytes(text, MAX_ERROR_CHARS)
    if "raw" in decoded:
        return "kernel returned a non-JSON response"
    if decoded.get("ok") is not True:
        return "kernel step did not succeed"
    return "kernel request failed"


def _is_vendor_error(decoded):
    # error signals: non-dict, an error/code/raw* envelope (incl. bad_request),
    # or an output whose ok is not exactly true. Never a false success.
    if not isinstance(decoded, dict):
        return True
    if any(key in decoded for key in ("error", "code", "raw", "raw_text")):
        return True
    return decoded.get("ok") is not True


def _bound_links(links):
    out = []
    if isinstance(links, list):
        for item in links[:MAX_LINKS]:
            if not isinstance(item, dict):
                continue
            entry = {}
            text = item.get("t")
            href = item.get("href")
            if isinstance(text, str) and text:
                entry["t"] = text[:MAX_LINK_TEXT_CHARS]
            if isinstance(href, str) and href:
                if len(href.encode("utf-8")) > MAX_LINK_HREF_CHARS:
                    raise ValueError("link URL exceeds %d bytes" % MAX_LINK_HREF_CHARS)
                entry["href"] = href
            if entry:
                out.append(entry)
    return out


@actor
class KernelBrowseWrapper:
    """Sells bounded cloud-browser page reads (`visit`) and stateful, per-hire
    browsing steps (`step`) to registered Hires."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "Kernel Browse"
        self.storage["__description__"] = (
            "Cloud-browser access via a Kernel session: a stateless one-shot "
            "read (visit) and stateful browsing steps (open/goto/click/type/"
            "press/scroll/extract/close) against a per-hire server-side session. "
            "Vendor key held in CBSS."
        )
        self.storage["__version__"] = ACTOR_VERSION
        self.storage[KEY_REGISTRAR] = registrar
        self.storage[KEY_SECRET_ACCOUNT] = owner
        self.storage[KEY_HIRE_COUNT] = 0
        self.storage[KEY_CALLS] = 0
        self.storage[KEY_REQUEST_IDS] = []
        self.storage[KEY_SESSION_HIRES] = []
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
            "artifact_id": _bounded_int(cfg.get("artifact_id"), "artifact_id", 0, 2**32 - 1),
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
        # last_output is public/never-expiring state — metadata only.
        self.storage[KEY_LAST_OUTPUT] = {"ok": False, "operation": operation}
        runtime.emit_event(
            "wrapper.error",
            {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]},
        )
        return codec.encode(output)

    # --- per-hire session bookkeeping --------------------------------------

    def _session(self, hire_id):
        return self.storage.get("session:" + str(hire_id))

    def _open_session_slot(self, hire_id, request_id, now):
        """Reserve the single per-hire session slot as `opening` — the real
        session id lands only when the open/auto-open result resumes."""
        self.storage["session:" + str(hire_id)] = {
            "hire_id": hire_id,
            "status": "opening",
            "session_id": "",
            "request_id": request_id,
            "created_height": now,
            "deadline_height": now + SESSION_LIFETIME_BLOCKS,
        }
        hires = list(self.storage.get(KEY_SESSION_HIRES, []))
        if hire_id not in hires:
            self.storage[KEY_SESSION_HIRES] = hires + [hire_id]

    def _promote_session(self, hire_id, session_id, request_id):
        rec = self._session(hire_id)
        # only the request that opened this slot may promote it.
        if not rec or rec.get("status") != "opening" or rec.get("request_id") != request_id:
            return
        now = int(runtime.get_block_height())
        rec["status"] = "open"
        rec["session_id"] = session_id[:MAX_SESSION_ID_CHARS]
        rec["deadline_height"] = now + SESSION_LIFETIME_BLOCKS
        self.storage["session:" + str(hire_id)] = rec

    def _drop_session(self, hire_id, only_if_opening=False):
        rec = self._session(hire_id)
        if not rec:
            return
        if only_if_opening and rec.get("status") != "opening":
            return
        del self.storage["session:" + str(hire_id)]
        hires = [h for h in self.storage.get(KEY_SESSION_HIRES, []) if h != hire_id]
        if hires != list(self.storage.get(KEY_SESSION_HIRES, [])):
            self.storage[KEY_SESSION_HIRES] = hires

    def _mark_session_closing(self, hire_id, request_id):
        rec = self._session(hire_id)
        if not rec:
            return
        rec["status"] = "closing"
        rec["request_id"] = request_id
        self.storage["session:" + str(hire_id)] = rec

    def _discard_request(self, request_id):
        key = "request:" + str(request_id)
        if self.storage.get(key):
            del self.storage[key]
        self.storage[KEY_REQUEST_IDS] = [
            item for item in self.storage.get(KEY_REQUEST_IDS, []) if item != request_id
        ]

    def _resolve_session(self, op, hire_id, operation):
        """Source the session id from the hire's OWN stored session — never from
        the caller. Returns (session_id, session_action, refusal). session_action
        is "open" for a slot-creating op, "close" for a slot-clearing op, else None."""
        rec = self._session(hire_id)
        status = rec.get("status") if rec else None
        if op == "open":
            if rec is not None:
                return "", None, self._refuse(operation, "hire already has an active session; close it first")
            return "", "open", None
        if op == "goto":
            if rec is None:
                return "", "open", None  # empty session_id => the Kernel app auto-opens
            if status == "opening":
                return "", None, self._refuse(operation, "session is still opening; poll the open result first")
            if status == "closing":
                return "", None, self._refuse(operation, "session is closing; retry close or wait for expiry")
            return rec["session_id"], None, None
        if op == "close":
            if rec is None:
                return "", None, self._refuse(operation, "no open session to close")
            if status == "opening":
                return "", None, self._refuse(operation, "session is still opening; cannot close it yet")
            if status == "closing":
                request = self.storage.get("request:" + str(rec.get("request_id", 0))) or {}
                if request.get("status") == "pending":
                    return "", None, self._refuse(operation, "session close is still pending")
            return rec["session_id"], "close", None
        # click/type/press/scroll/extract need an already-open session.
        if rec is None:
            return "", None, self._refuse(operation, "no open session; goto a page or open one first")
        if status == "opening":
            return "", None, self._refuse(operation, "session is still opening; poll the open result first")
        if status == "closing":
            return "", None, self._refuse(operation, "session is closing; retry close or wait for expiry")
        return rec["session_id"], None, None

    # --- visit (stateless one-shot read) -----------------------------------

    @public
    @deferred
    def visit(self, payload):
        """payload: hire_id, url. Stateless one-shot read (open->goto->extract->
        close server-side). Returns an acceptance record; read the bounded page
        state via get_result."""
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "visit")
        if refusal is not None:
            return refusal
        unsupported = sorted(key for key in req if key not in VISIT_INPUT_FIELDS)
        if unsupported:
            return self._refuse("visit", "unsupported request fields: %s" % ", ".join(unsupported))
        try:
            url = _http_url(req.get("url"))
        except ValueError as exc:
            return self._refuse("visit", str(exc))
        body = _invocation_body(VISIT_ACTION, {"url": url})
        return self._accept(hire_id, hire, "visit", None, body, mutating=False, session_action=None)

    # --- step (stateful op against a per-hire session) ---------------------

    @public
    @deferred
    def step(self, payload):
        """payload: hire_id, op, and only the fields the op needs. op in
        {open, goto, click, type, press, scroll, extract, close}. The session id
        is the hire's own stored session — never taken from the caller."""
        req = _parse(payload)
        hire_id, hire, refusal = self._authorize(req, "step")
        if refusal is not None:
            return refusal
        unsupported = sorted(key for key in req if key not in STEP_INPUT_FIELDS)
        if unsupported:
            # note: `session_id` is not in STEP_INPUT_FIELDS, so a caller-supplied
            # one is refused here — a hire can only act on its own stored session.
            return self._refuse("step", "unsupported request fields: %s" % ", ".join(unsupported))
        op = str(req.get("op") or "").strip().lower()
        if op not in STEP_OPS:
            return self._refuse("step", "op must be one of %s" % ", ".join(STEP_OPS))
        try:
            step = self._build_step(op, req)
        except ValueError as exc:
            return self._refuse("step", str(exc))
        # Expire a session before resolving it into a new provider request.
        self._cleanup()
        session_id, session_action, refusal = self._resolve_session(op, hire_id, "step")
        if refusal is not None:
            return refusal
        if op in OPS_NEED_SESSION or op == "goto" or op == "close":
            # every op except a fresh `open` names a session id in the body
            # (empty on a goto that auto-opens).
            step["session_id"] = session_id
        body = _invocation_body(STEP_ACTION, step)
        return self._accept(
            hire_id, hire, "step", op, body,
            mutating=(op in MUTATING_OPS), session_action=session_action,
        )

    def _build_step(self, op, req):
        step = {"op": op}
        if op == "open":
            self._add_open_opts(step, req)
        elif op == "goto":
            step["url"] = _http_url(req.get("url"))
            self._add_open_opts(step, req)  # only used if it auto-opens
        elif op == "click":
            step["selector"] = _clean_str(req.get("selector"), "selector", MAX_SELECTOR_CHARS)
        elif op == "type":
            step["selector"] = _clean_str(req.get("selector"), "selector", MAX_SELECTOR_CHARS)
            step["text"] = _clean_str(req.get("text"), "text", MAX_TEXT_CHARS, allow_empty=True)
        elif op == "press":
            key = req.get("key")
            step["key"] = _clean_str(key, "key", MAX_KEY_CHARS) if key is not None else "Enter"
        elif op == "scroll":
            dy = req.get("dy")
            step["dy"] = _bounded_int(dy, "dy", -MAX_SCROLL_DELTA, MAX_SCROLL_DELTA) if dy is not None else 800
        # extract/close take no extra params (session_id added by the caller)
        return step

    def _add_open_opts(self, step, req):
        if req.get("stealth") is not None:
            if not isinstance(req.get("stealth"), bool):
                raise ValueError("stealth must be a boolean")
            step["stealth"] = req["stealth"]
        if req.get("timeout_seconds") is not None:
            step["timeout_seconds"] = _bounded_int(
                req.get("timeout_seconds"), "timeout_seconds", MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS
            )

    # --- dispatch ----------------------------------------------------------

    def _headers(self, request_id, mutating):
        headers = {
            "Accept": "application/json",
            AUTH_HEADER: "%s ${%s}" % (AUTH_SCHEME, SECRET_KEY),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if mutating:
            # Stable across a runner retry (fixed to the request id) so a retried
            # mutating op dedups vendor-side rather than double-acting.
            headers["Idempotency-Key"] = "kernel-browse-%d" % request_id
        return headers

    def _accept(self, hire_id, hire, operation, op, invocation_body, mutating, session_action):
        buyer = _sender_hex()
        self._cleanup()
        previous = None
        if session_action == "close":
            session = self._session(hire_id) or {}
            previous = session.get("request_id") if session.get("status") == "closing" else None
        ids = list(self.storage.get(KEY_REQUEST_IDS, []))
        cid = continuation.new_cid(self, "_run_request")
        for request_id in ids:
            if (self.storage.get("request:" + str(request_id)) or {}).get("cid") == cid:
                return codec.encode(
                    {"ok": False, "error": "transaction already has a pending request"}
                )
        retained_count = len(ids) - (1 if previous in ids else 0)
        if retained_count >= (
            MAX_RETAINED_WITH_CLOSES if session_action == "close" else MAX_RETAINED_REQUESTS
        ):
            return codec.encode(
                {"ok": False, "error": "request capacity is full; retry after results expire"}
            )
        retained_for_hire = sum(
            1
            for request_id in ids
            if (self.storage.get("request:" + str(request_id)) or {}).get("hire_id") == hire_id
        )
        if retained_for_hire >= MAX_RETAINED_PER_HIRE and session_action != "close":
            return codec.encode(
                {"ok": False, "error": "this hire's retained request capacity is full"}
            )
        if session_action == "open" and len(self.storage.get(KEY_SESSION_HIRES, [])) >= MAX_ACTIVE_SESSIONS:
            return codec.encode(
                {"ok": False, "error": "active session capacity is full; close a session first"}
            )
        if previous:
            self._discard_request(previous)
            ids = list(self.storage.get(KEY_REQUEST_IDS, []))
        request_id = _bounded_int(
            int(self.storage.get(KEY_CALLS, 0)) + 1, "request_id", 1, 2**64 - 1
        )
        now = int(runtime.get_block_height())
        record = {
            "request_id": request_id,
            "hire_id": hire_id,
            "buyer": buyer,
            "operation": operation,
            "op": op,
            "status": "pending",
            "cid": cid,
            "mutating": mutating,
            "session_pending": session_action == "open",
            "created_height": now,
            "deadline_height": now + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        if session_action == "open":
            self._open_session_slot(hire_id, request_id, now)
        elif session_action == "close":
            self._mark_session_closing(hire_id, request_id)
        job = {
            "url": INVOKE_URL,
            "method": "POST",
            "headers": self._headers(request_id, mutating),
            # raw_body sends the exact invocation bytes; `body` re-adapt would
            # reject fractional numbers and re-serialise the JSON string field.
            "raw_body": invocation_body,
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
        if op is not None:
            accepted["op"] = op
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        ctx = capture()
        # Only request_id crosses the await — no credential, no secret, no url,
        # no session id.
        ctx.request_id = req["request_id"]
        result = await runner.http(**req["job"])
        return self._finish(ctx.request_id, result)

    @runner.continuation
    async def _run_close(self, payload):
        # Best-effort abandoned-session close fired from cleanup. Only the (public,
        # non-sensitive) session id crosses the await; the reply is discarded.
        req = _parse(payload)
        ctx = capture()
        ctx.session_id = req.get("session_id", "")
        await runner.http(**req["job"])
        return codec.encode({"ok": True, "closed": ctx.session_id})

    def _extract_page(self, decoded):
        if not isinstance(decoded, dict):
            return None
        url = decoded.get("url")
        title = decoded.get("title")
        text = decoded.get("text")
        # A real page carries at least a url or visible text.
        if not (isinstance(url, str) or isinstance(text, str)):
            return None
        data = {}
        if isinstance(url, str):
            if len(url.encode("utf-8")) > MAX_URL_CHARS:
                raise ValueError("page URL exceeds %d bytes" % MAX_URL_CHARS)
            data["url"] = url
        if isinstance(title, str):
            data["title"] = title[:MAX_TITLE_CHARS]
        text_str = text if isinstance(text, str) else ""
        # Deliver the executor's page text (it caps at ~4000 chars), bounded only
        # by the byte budget — never park a large reply by clipping after transport.
        bounded = _truncate_text_bytes(text_str, TEXT_BYTE_BUDGET)
        data["text"] = bounded
        if bounded != text_str:
            data["truncated"] = True
        data["links"] = _bound_links(decoded.get("links"))
        # An auto-open goto returns the new session id; surface it for the caller
        # (the actor already stored it — the caller does not supply it back).
        session_id = decoded.get("session_id")
        if isinstance(session_id, str) and session_id:
            data["session_id"] = session_id[:MAX_SESSION_ID_CHARS]
        return data

    def _result_data(self, op, decoded):
        """Op-specific successful-result extraction. Page ops (and visit) return
        page state; open returns a session id; close returns the closed id."""
        if op == "open":
            session_id = decoded.get("session_id")
            if isinstance(session_id, str) and session_id:
                return {"session_id": session_id[:MAX_SESSION_ID_CHARS]}
            return None
        if op == "close":
            closed = decoded.get("closed")
            data = {}
            if isinstance(closed, str) and closed:
                data["closed"] = closed[:MAX_SESSION_ID_CHARS]
            return data
        return self._extract_page(decoded)

    def _finish(self, request_id, result):
        key = "request:" + str(request_id)
        record = self.storage.get(key)
        if not record or record.get("status") != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        operation = record.get("operation", "visit")
        op = record.get("op")
        if result is None or int(runtime.get_block_height()) >= int(record["deadline_height"]):
            output = {
                "ok": False,
                "operation": operation,
                "error": "request result did not arrive before its deadline",
            }
            self._settle_session(record, success=False, data=None)
        else:
            decoded = _unwrap_invocation(_decode_result(result))
            if _is_vendor_error(decoded):
                output = {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
                self._settle_session(record, success=False, data=decoded)
            else:
                result_error = None
                try:
                    data = self._result_data(op, decoded)
                except ValueError as exc:
                    data = None
                    result_error = str(exc)
                if data is None:
                    output = {
                        "ok": False,
                        "operation": operation,
                        "error": result_error or "unexpected %s response from kernel" % (op or operation),
                    }
                    self._settle_session(record, success=False, data=None)
                else:
                    output = {"ok": True, "operation": operation, "data": data}
                    self._settle_session(record, success=True, data=data)
        if op is not None:
            output["op"] = op
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        now = int(runtime.get_block_height())
        record["finished_height"] = now
        record["expires_height"] = now + RESULT_RETENTION_BLOCKS
        record.pop("cid", None)
        response = self._response(record)
        # Transport cap: bound the WHOLE encoded reply, never a truncated false
        # success. If even the bounded page overflows, error it.
        if len(codec.encode(response)) > RETURN_BYTE_BUDGET:
            output = {
                "ok": False,
                "operation": operation,
                "error": "result exceeds the encoded result byte budget",
            }
            if op is not None:
                output["op"] = op
            record["status"] = "error"
            record["output"] = output
            response = self._response(record)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        # metadata only into never-expiring public state (no page text).
        self.storage[KEY_LAST_OUTPUT] = {"ok": output["ok"], "operation": operation}
        runtime.emit_event(
            "wrapper.request_finished",
            {
                "request_id": request_id,
                "hire_id": record["hire_id"],
                "operation": operation,
                "op": op,
                "status": record["status"],
            },
        )
        return codec.encode(response)

    def _settle_session(self, record, success, data):
        """Settle the session state owned by this request."""
        op = record.get("op")
        if op == "close":
            if success:
                self._drop_session(record["hire_id"])
            return
        if op != "open" and not (op == "goto" and record.get("session_pending")):
            return
        session_id = (data or {}).get("session_id")
        if isinstance(session_id, str) and session_id:
            self._promote_session(record["hire_id"], session_id, record["request_id"])
        else:
            # the open/auto-open failed (or returned no session id) — free the slot.
            self._drop_session(record["hire_id"], only_if_opening=True)

    def _response(self, record):
        response = {
            key: record[key]
            for key in ("request_id", "hire_id", "operation", "status")
        }
        if record.get("op") is not None:
            response["op"] = record["op"]
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
        # full-scan passes (accept time) also expire abandoned sessions.
        if only is None:
            self._expire_sessions(now)

    def _expire_sessions(self, now):
        """Best-effort: close and clear a hire's session once its maximum deadline
        passes. Abandoned server-side browsers cost money (and self-expire on
        Kernel's own timeout_seconds); this proactively closes them."""
        hires = list(self.storage.get(KEY_SESSION_HIRES, []))
        retained = []
        fired = False
        for hire_id in hires:
            rec = self._session(hire_id)
            if not rec:
                continue
            if now < int(rec.get("deadline_height", 0)):
                retained.append(hire_id)
                continue
            session_id = rec.get("session_id")
            # Fire at most one close per pass: new_cid is fixed per (tx, name), so
            # two _run_close in one tx would collide on their cid.
            if rec.get("status") == "open" and session_id and not fired:
                if self._fire_close(hire_id, session_id):
                    fired = True
            del self.storage["session:" + str(hire_id)]
            runtime.emit_event(
                "wrapper.session_expired", {"hire_id": hire_id, "had_session": bool(session_id)}
            )
        if retained != hires:
            self.storage[KEY_SESSION_HIRES] = retained

    def _fire_close(self, hire_id, session_id):
        hire = self.storage.get("hire:" + str(hire_id))
        if not hire:
            return False  # deregistered hire — cannot build lease pins; skip
        body = _invocation_body(STEP_ACTION, {"op": "close", "session_id": session_id})
        headers = self._headers(0, mutating=True)
        headers["Idempotency-Key"] = "kernel-browse-close-%s" % session_id[:MAX_SESSION_ID_CHARS]
        job = {
            "url": INVOKE_URL,
            "method": "POST",
            "headers": headers,
            "raw_body": body,
            "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
            "hire": self._pins(hire_id, hire),
        }
        self._run_close(codec.encode({"session_id": session_id, "job": job}))
        return True

    @public
    @deferred
    def get_result(self, payload):
        """Return this caller and Hire's request; optional receipt chunks
        support signed CLI calls."""
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
                "slot": "cloud-browser",
                "handlers": ["visit", "step"],
                "poll_handler": "get_result",
                "returns": "kernel-browse-async-result",
                "endpoint": "POST /invocations",
                "app": KERNEL_APP,
                "actions": [VISIT_ACTION, STEP_ACTION],
                "step_ops": list(STEP_OPS),
                "mutating_ops": list(MUTATING_OPS),
                "readonly_ops": list(READONLY_OPS) + [VISIT_ACTION],
                "mutating": True,
                "session": (
                    "the actor keeps ONE server-side Kernel session per hire; the "
                    "caller never supplies a session_id (open/goto set it, close "
                    "clears it). A hire can only act on its own session."
                ),
                "privacy": "the target url is public chain data; never pass a private/signed url that grants access",
                "input": {
                    "visit": {
                        "hire_id": "int",
                        "url": "str (http(s) URL; a bare domain is normalised to https)",
                    },
                    "step": {
                        "hire_id": "int",
                        "op": "one of %s" % ", ".join(STEP_OPS),
                        "url": "str (goto)",
                        "selector": "str (click/type)",
                        "text": "str (type)",
                        "key": "str (press; default Enter)",
                        "dy": "int (scroll; default 800)",
                        "stealth": "bool (open/goto-autoopen)",
                        "timeout_seconds": "int %d..%d (open/goto-autoopen)" % (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
                    },
                },
                "output": "acceptance record; get_result returns {url,title,text,links,session_id?} (page ops), {session_id} (open), or {closed} (close) under output.data",
                "warnings": [
                    "the executor caps page text at ~4000 chars — get_result "
                    "returns that bounded text, NOT the whole page",
                    "the executor rejects a page result above 32000 bytes before "
                    "it reaches the runner transport cap",
                    "mutating ops (%s) carry an Idempotency-Key; provider-side "
                    "deduplication depends on Kernel's invocation contract" % ", ".join(MUTATING_OPS),
                    "sessions are server-side and cost money; an abandoned session "
                    "is best-effort auto-closed after its maximum lifetime and also "
                    "self-expires on Kernel's own timeout_seconds",
                ],
                "hire_activation": {"protocol": "store-admin-v1"},
                "max_url_chars": MAX_URL_CHARS,
                "max_selector_chars": MAX_SELECTOR_CHARS,
                "max_text_chars": MAX_TEXT_CHARS,
                "runner_result_byte_budget": RUNNER_RESULT_BYTE_BUDGET,
                "result_byte_budget": RETURN_BYTE_BUDGET,
                "text_byte_budget": TEXT_BYTE_BUDGET,
                "max_links": MAX_LINKS,
                "max_result_chunk_bytes": MAX_RESULT_CHUNK_BYTES,
                "max_retained_requests": MAX_RETAINED_REQUESTS,
                "max_retained_with_closes": MAX_RETAINED_WITH_CLOSES,
                "max_retained_per_hire": MAX_RETAINED_PER_HIRE,
                "max_registered_hires": MAX_REGISTERED_HIRES,
                "max_active_sessions": MAX_ACTIVE_SESSIONS,
                "request_lifetime_blocks": REQUEST_LIFETIME_BLOCKS,
                "result_retention_blocks": RESULT_RETENTION_BLOCKS,
                "session_lifetime_blocks": SESSION_LIFETIME_BLOCKS,
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
            if (self.storage.get("request:" + str(request_id)) or {}).get("status") == "pending"
        )
        return codec.encode(
            {
                "vendor": VENDOR,
                "endpoint": INVOKE_URL,
                "app": KERNEL_APP,
                "actions": [VISIT_ACTION, STEP_ACTION],
                "response_byte_limit": RETURN_BYTE_BUDGET,
                "call_count": self.storage.get(KEY_CALLS, 0),
                "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                "registered_hires": self.storage.get(KEY_HIRE_COUNT, 0),
                "active_requests": active,
                "retained_requests": len(ids),
                "active_sessions": len(self.storage.get(KEY_SESSION_HIRES, [])),
                "last_status": self.storage.get(KEY_LAST_STATUS),
            }
        )


def _get_actor():
    return KernelBrowseWrapper()


def init(payload):
    return _get_actor().init(payload)


def register_hire(payload):
    return _get_actor().register_hire(payload)


def visit(payload):
    return _get_actor().visit(payload)


def step(payload):
    return _get_actor().step(payload)


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


def _run_close__resume(payload):
    try:
        return _get_actor()._run_close__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
