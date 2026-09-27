"""AgentMail Store wrapper actor — an email inbox an actor can send and receive on.

Each vendor call is hire-gated and asynchronous: a handler validates input and
returns an acceptance record; the registered hirer reads the result from
`get_result`. Under the hood one request is a single `runner.http` to AgentMail's
REST API with `Bearer ${AGENTMAIL_API_KEY}` from CBSS, resumed once as a
continuation. Receiving is done by polling list_messages (webhooks/WebSocket push
exist but an on-chain actor cannot hold an inbound endpoint). Replies are bounded
under the 40KB runner-result cap.
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


VENDOR = "agentmail"
ACTOR_VERSION = "2.0.0"
API_ROOT = "https://api.agentmail.to/v0"
INBOXES = "https://api.agentmail.to/v0/inboxes"
SECRET_KEY = "AGENTMAIL_API_KEY"
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer "

MAX_ADDR_BYTES = 254
MAX_SUBJECT_BYTES = 998
MAX_TEXT_BYTES = 24_000
MAX_QUERY_BYTES = 400
MAX_MSG_ID_BYTES = 256
MAX_RECIPIENTS = 25
DEFAULT_LIST = 10
MAX_LIST = 50
MAX_SEARCH = 100

RETURN_BYTE_BUDGET = 38_000
API_BYTE_BUDGET = 36_976
MAX_REQUEST_BODY_BYTES = 40_000
MAX_RESULT_CHUNK_BYTES = 3_900
MAX_RETAINED_REQUESTS = 16
MAX_RETAINED_PER_HIRE = 6
MAX_INBOXES_PER_HIRE = 8
MAX_INBOXES_GLOBAL = 256
MAX_PAGE_TOKEN_BYTES = 4_096
UNCERTAIN_CREATE_BLOCKS = 7_200
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
KEY_INBOX_TOTAL = "inbox_total"


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
    """Normalize a runner result into a dict, unwrapping any SDK result wrapper."""
    if result is None or _depth > 4:
        return {}
    if isinstance(result, runner.RunnerResult):
        return _decode_result(result.raw, _depth + 1)
    if not isinstance(result, (bytes, bytearray, str, dict, list)):
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
        return _decode_result(value, _depth + 1) if isinstance(value, (dict, list)) else {"value": value}
    if isinstance(result, list):
        return {"messages": result}
    return result


_OP_REQUIRED = {
    "create_inbox": "inbox_id",
    "send": "message_id",
    "reply": "message_id",
    "list_messages": "messages",
    "get_message": "message_id",
    "search": "messages",
}


def _is_vendor_error(decoded):
    """True if the decoded body is an AgentMail error ({code, message, name} or
    error/detail/raw_text), not a result."""
    if not isinstance(decoded, dict):
        return True
    if any(k in decoded for k in ("error", "detail", "raw_text")):
        return True
    name = decoded.get("name")
    if isinstance(name, str) and name.endswith("Error"):
        return True
    # AgentMail's error envelope is {code, message, name, docs}; a success body
    # never carries both a string `code` and a string `message`, so treat that
    # pair as an error even when `name`/`docs` are absent.
    if isinstance(decoded.get("code"), str) and isinstance(decoded.get("message"), str):
        return True
    return False


def _vendor_error(decoded):
    if not isinstance(decoded, dict):
        return "agentmail request failed"
    for key in ("error", "message", "detail", "raw_text"):
        value = decoded.get(key)
        if isinstance(value, str) and value:
            return value[:500]
        if isinstance(value, dict):
            for inner in ("message", "detail", "description"):
                text = value.get(inner)
                if isinstance(text, str) and text:
                    return text[:500]
    return "agentmail request failed"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _valid_success(operation, decoded):
    if operation in ("create_inbox", "send", "reply", "get_message"):
        key = _OP_REQUIRED[operation]
        return isinstance(decoded.get(key), str) and bool(decoded.get(key))
    if operation in ("list_messages", "search"):
        if not isinstance(decoded.get("messages"), list):
            return False
        token = decoded.get("next_page_token")
        return token is None or isinstance(token, str)
    return True


def _contains_secret_template(value):
    """A ${NAME} in caller data is refused: the key rides every call, and worse,
    a ${AGENTMAIL_API_KEY} in a subject or body would have the runner paste the
    live key into the OUTBOUND email, leaking it to the recipient."""
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
    if isinstance(value, bool):
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


def _inbox_key(inbox_id):
    """Storage key for an inbox->hire binding, on a case-insensitive form so a
    different-case reference to the same address cannot dodge the ownership
    check (email addresses are case-insensitive; the vendor URL still uses the
    caller's original inbox string)."""
    return "inbox:" + str(inbox_id).strip().lower()


def _inbox_id(value):
    """An inbox id is an email address that goes into the URL path. Require a
    real address shape — exactly one '@', a non-empty local part, and a dotted
    domain — so a value like '.' or '..' (which would traverse out of the
    /inboxes/<id>/ path) is refused, and restrict to the ASCII email character
    set with no consecutive dots (no path separators, no percent-encoding:
    AgentMail expects the raw address in the path)."""
    text = str(value or "").strip()
    if not text or len(text.encode("utf-8")) > MAX_ADDR_BYTES:
        raise ValueError("inbox_id is required")
    for ch in text:
        if not (ord(ch) < 128 and (ch.isalnum() or ch in "@._-+")):
            raise ValueError("inbox_id has an unexpected character")
    if text.count("@") != 1 or ".." in text:
        raise ValueError("inbox_id must be an email address")
    local, _, domain = text.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("inbox_id must be an email address")
    return text


def _encode_segment(value, field, maximum):
    """One percent-encoded path segment (message id). Separators are refused."""
    value = str(value or "").strip()
    if not value or len(value.encode("utf-8")) > maximum or any(c in value for c in "/?#\r\n"):
        raise ValueError("%s is invalid" % field)
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(chr(c) if c in safe else "%%%02X" % c for c in value.encode("utf-8"))


def _query(pairs):
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    parts = []
    for key, value in pairs:
        if value is None:
            continue
        for item in (value if isinstance(value, list) else [value]):
            enc = "".join(chr(b) if b in safe else "%%%02X" % b
                          for b in str(item).encode("utf-8"))
            parts.append("%s=%s" % (key, enc))
    return ("?" + "&".join(parts)) if parts else ""


def _text(value, field, maximum, required=False):
    if value is None:
        if required:
            raise ValueError("%s is required" % field)
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    if len(value.encode("utf-8")) > maximum:
        raise ValueError("%s exceeds %d bytes" % (field, maximum))
    return value


@actor
class AgentMailInboxWrapper:
    """Sells an email inbox (send + receive) per-call to any hiring actor."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "AgentMail Inbox"
        self.storage["__description__"] = (
            "An email address an actor can send from and receive on: create "
            "inboxes, send, poll for mail, search. Vendor key held in CBSS."
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

    def _inbox_owner_conflict(self, hire_id, inbox):
        """True if this inbox is bound to a DIFFERENT hire on this actor. An
        inbox this actor created is owned by the hire that created it, so no
        other hire may send from, read, or search it. An unbound inbox (one this
        actor never created — a hirer may bring an address made directly at
        AgentMail) is allowed; only a cross-hire reference is refused."""
        owner = self.storage.get(_inbox_key(inbox))
        return owner is not None and owner != hire_id

    def _bind_inbox(self, hire_id, inbox_id):
        """Record that a just-created inbox belongs to this hire. Per-hire capped
        (checked before create), so the binding table stays bounded."""
        if not isinstance(inbox_id, str) or not inbox_id:
            return
        if len(inbox_id.encode("utf-8")) > MAX_ADDR_BYTES:
            return
        key = _inbox_key(inbox_id)
        if self.storage.get(key) is not None:
            return
        count_key = "inbox_count:" + str(hire_id)
        self.storage[key] = hire_id
        self.storage[count_key] = self.storage.get(count_key, 0) + 1
        self.storage[KEY_INBOX_TOTAL] = self.storage.get(KEY_INBOX_TOTAL, 0) + 1

    def _inflight_creates(self, hire_id):
        """(this hire's, total) pending create_inbox requests, counted so the
        per-hire and global inbox caps can reserve capacity at accept time —
        bindings are only recorded at finish (a later tx), so without this a
        burst of pipelined creates would all pass a stale count."""
        mine = total = 0
        for request_id in self.storage.get(KEY_REQUEST_IDS, []):
            record = self.storage.get("request:" + str(request_id))
            if record and record.get("operation") == "create_inbox" and (record.get("status") == "pending" or record.get("create_uncertain")):
                total += 1
                if record.get("hire_id") == hire_id:
                    mine += 1
        return mine, total

    def _refuse(self, operation, error):
        out = {"ok": False, "operation": operation, "error": error}
        self.storage[KEY_LAST_STATUS] = "error"
        self.storage[KEY_LAST_OUTPUT] = out
        runtime.emit_event("wrapper.error",
                           {"vendor": VENDOR, "operation": operation, "reason": str(error)[:200]})
        return codec.encode(out)

    @public
    @deferred
    def create_inbox(self, payload):
        """payload: {"hire_id": int, "username": str?, "display_name": str?}."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "create_inbox")
        if refusal is not None:
            return refusal
        body = {}
        try:
            username = _text(req.get("username"), "username", 128)
            display = _text(req.get("display_name"), "display_name", 128)
        except ValueError as exc:
            return self._refuse("create_inbox", str(exc))
        if username:
            body["username"] = username
        if display:
            body["display_name"] = display
        if _contains_secret_template(body):
            return self._refuse("create_inbox", "request may not contain runner secret templates")
        mine_flight, total_flight = self._inflight_creates(hire_id)
        if self.storage.get("inbox_count:" + str(hire_id), 0) + mine_flight >= MAX_INBOXES_PER_HIRE:
            return self._refuse("create_inbox", "this hire has reached its inbox capacity")
        if self.storage.get(KEY_INBOX_TOTAL, 0) + total_flight >= MAX_INBOXES_GLOBAL:
            return self._refuse("create_inbox", "inbox capacity is full")
        return self._accept(hire_id, hire, "create_inbox", INBOXES, "POST", body)

    @public
    @deferred
    def send(self, payload):
        """payload: {"hire_id": int, "inbox_id": str, "to": str|list, "subject": str,
                     "text": str, "html"?, "cc"?, "bcc"?, "reply_to"?, "labels"?}."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "send")
        if refusal is not None:
            return refusal
        try:
            inbox = _inbox_id(req.get("inbox_id"))
            body = self._message_body(req)
        except ValueError as exc:
            return self._refuse("send", str(exc))
        if self._inbox_owner_conflict(hire_id, inbox):
            return self._refuse("send", "inbox_id belongs to another hire")
        if _contains_secret_template(body):
            return self._refuse("send", "mail fields may not contain runner secret templates")
        url = INBOXES + "/" + inbox + "/messages/send"
        return self._accept(hire_id, hire, "send", url, "POST", body)

    @public
    @deferred
    def reply(self, payload):
        """payload: {"hire_id": int, "inbox_id": str, "message_id": str,
                     "text": str, "html"?, ...}."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "reply")
        if refusal is not None:
            return refusal
        try:
            inbox = _inbox_id(req.get("inbox_id"))
            msg = _encode_segment(req.get("message_id"), "message_id", MAX_MSG_ID_BYTES)
            body = self._message_body(req, require_recipient=False)
        except ValueError as exc:
            return self._refuse("reply", str(exc))
        if self._inbox_owner_conflict(hire_id, inbox):
            return self._refuse("reply", "inbox_id belongs to another hire")
        if _contains_secret_template(body):
            return self._refuse("reply", "mail fields may not contain runner secret templates")
        url = INBOXES + "/" + inbox + "/messages/" + msg + "/reply"
        return self._accept(hire_id, hire, "reply", url, "POST", body)

    def _message_body(self, req, require_recipient=True):
        body = {}
        to = req.get("to")
        if require_recipient:
            if not to:
                raise ValueError("to is required")
        if to is not None:
            recipients = to if isinstance(to, list) else [to]
            if len(recipients) > MAX_RECIPIENTS:
                raise ValueError("too many recipients")
            for addr in recipients:
                _text(addr, "to", MAX_ADDR_BYTES)
            body["to"] = to
        subject = _text(req.get("subject"), "subject", MAX_SUBJECT_BYTES)
        if subject is not None:
            body["subject"] = subject
        text = _text(req.get("text"), "text", MAX_TEXT_BYTES)
        html = _text(req.get("html"), "html", MAX_TEXT_BYTES)
        if text is None and html is None and require_recipient:
            raise ValueError("text or html is required")
        if text is not None:
            body["text"] = text
        if html is not None:
            body["html"] = html
        for field in ("cc", "bcc"):
            val = req.get(field)
            if val is not None:
                recipients = val if isinstance(val, list) else [val]
                if len(recipients) > MAX_RECIPIENTS:
                    raise ValueError("too many %s recipients" % field)
                for addr in recipients:
                    _text(addr, field, MAX_ADDR_BYTES)
                body[field] = val
        if req.get("reply_to") is not None:
            _text(req["reply_to"], "reply_to", MAX_ADDR_BYTES)
            body["reply_to"] = req["reply_to"]
        if isinstance(req.get("labels"), list):
            body["labels"] = [str(x)[:64] for x in req["labels"][:20]]
        return body

    @public
    @deferred
    def list_messages(self, payload):
        """payload: {"hire_id": int, "inbox_id": str, "limit": int?, "labels": [str]?,
                     "after": rfc3339?}. This is how an actor RECEIVES: poll it."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "list_messages")
        if refusal is not None:
            return refusal
        try:
            inbox = _inbox_id(req.get("inbox_id"))
        except ValueError as exc:
            return self._refuse("list_messages", str(exc))
        if self._inbox_owner_conflict(hire_id, inbox):
            return self._refuse("list_messages", "inbox_id belongs to another hire")
        limit = _clamp_int(req.get("limit"), 1, MAX_LIST, DEFAULT_LIST)
        labels = req.get("labels") if isinstance(req.get("labels"), list) else None
        after = _text(req.get("after"), "after", 64) if req.get("after") else None
        page_token = _text(req.get("page_token"), "page_token", MAX_PAGE_TOKEN_BYTES) if req.get("page_token") is not None else None
        if _contains_secret_template({"labels": labels, "after": after, "page_token": page_token}):
            return self._refuse("list_messages", "request may not contain runner secret templates")
        url = INBOXES + "/" + inbox + "/messages" + _query(
            [("limit", limit), ("labels", labels), ("after", after), ("page_token", page_token)])
        return self._accept(hire_id, hire, "list_messages", url, "GET", None)

    @public
    @deferred
    def get_message(self, payload):
        """payload: {"hire_id": int, "inbox_id": str, "message_id": str}. Returns
        the full message including parsed text (no raw MIME)."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "get_message")
        if refusal is not None:
            return refusal
        try:
            inbox = _inbox_id(req.get("inbox_id"))
            msg = _encode_segment(req.get("message_id"), "message_id", MAX_MSG_ID_BYTES)
        except ValueError as exc:
            return self._refuse("get_message", str(exc))
        if self._inbox_owner_conflict(hire_id, inbox):
            return self._refuse("get_message", "inbox_id belongs to another hire")
        url = INBOXES + "/" + inbox + "/messages/" + msg
        return self._accept(hire_id, hire, "get_message", url, "GET", None)

    @public
    @deferred
    def search(self, payload):
        """payload: {"hire_id": int, "inbox_id": str, "q": str, "limit": int?}."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "search")
        if refusal is not None:
            return refusal
        try:
            inbox = _inbox_id(req.get("inbox_id"))
            q = _text(req.get("q"), "q", MAX_QUERY_BYTES, required=True)
        except ValueError as exc:
            return self._refuse("search", str(exc))
        if self._inbox_owner_conflict(hire_id, inbox):
            return self._refuse("search", "inbox_id belongs to another hire")
        if _contains_secret_template(q):
            return self._refuse("search", "q may not contain runner secret templates")
        limit = _clamp_int(req.get("limit"), 1, MAX_SEARCH, DEFAULT_LIST)
        url = INBOXES + "/" + inbox + "/messages/search" + _query([("q", q), ("limit", limit)])
        return self._accept(hire_id, hire, "search", url, "GET", None)

    def _accept(self, hire_id, hire, operation, url, method, body):
        buyer = _sender_hex()
        self._cleanup()
        ids = self.storage[KEY_REQUEST_IDS]
        cid = continuation.new_cid(self, "_run_request")
        raw_body = None
        if method != "GET" and body is not None:
            if operation == "create_inbox":
                body = dict(body)
                body["client_id"] = cid.hex() if isinstance(cid, (bytes, bytearray)) else str(cid)
            raw_body = _json_bytes(body)
            if len(raw_body) > MAX_REQUEST_BODY_BYTES:
                return self._refuse(operation, "request exceeds the %d-byte body budget" % MAX_REQUEST_BODY_BYTES)
        if any(self.storage["request:" + str(i)].get("cid") == cid for i in ids):
            return codec.encode({"ok": False, "error": "transaction already has a pending request"})
        ordinary = [self.storage["request:" + str(i)] for i in ids
                    if not self.storage["request:" + str(i)].get("create_uncertain")]
        if len(ordinary) >= MAX_RETAINED_REQUESTS:
            return codec.encode({"ok": False, "error": "request capacity is full; retry after results expire"})
        mine = sum(1 for record in ordinary if record.get("hire_id") == hire_id)
        if mine >= MAX_RETAINED_PER_HIRE:
            return codec.encode({"ok": False,
                                 "error": "this hire has too many in-flight requests; poll get_result to free capacity"})
        request_id = _bounded_int(self.storage[KEY_CALLS] + 1, "request_id", 1, 2**64 - 1)
        record = {
            "request_id": request_id, "hire_id": hire_id, "buyer": buyer,
            "operation": operation, "status": "pending", "cid": cid,
            "deadline_height": runtime.get_block_height() + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        # GET omits body/Content-Type (a GET body is rejected E1604); POST uses
        # raw_body, not body (body re-adapts and rejects fractional JSON numbers).
        headers = {"Accept": "application/json", AUTH_HEADER: AUTH_SCHEME + "${%s}" % SECRET_KEY}
        job = {"url": url, "method": method, "headers": headers,
               "secrets": [[self.storage[KEY_SECRET_ACCOUNT], SECRET_KEY]],
               "hire": self._pins(hire_id, hire)}
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
            # Idempotency-Key: mutating POSTs (create/send/reply) are not naturally
            # idempotent, so a runner retry of this job would double-send. Key it on
            # the per-request cid (globally unique — derived from tx hash — so it does
            # not collide across a redeploy, unlike a from-1 request_id) and fix it for
            # the life of the request, so the vendor dedups a retry. Best-effort —
            # ignored if unsupported.
            if operation in ("send", "reply"):
                headers["Idempotency-Key"] = cid.hex() if isinstance(cid, (bytes, bytearray)) else str(cid)
            job["raw_body"] = raw_body
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
            if operation == "create_inbox":
                record["create_uncertain"] = True
        else:
            decoded = _decode_result(result)
            required = _OP_REQUIRED.get(operation)
            if _is_vendor_error(decoded):
                output = {"ok": False, "operation": operation, "error": _vendor_error(decoded)}
            elif required and not _valid_success(operation, decoded):
                output = {"ok": False, "operation": operation,
                          "error": "unexpected %s response from agentmail" % operation}
            else:
                if len(_json_bytes(decoded)) > API_BYTE_BUDGET:
                    output = {"ok": False, "operation": operation,
                              "error": "exact result fields exceed the result byte budget"}
                else:
                    output = {"ok": True, "operation": operation, "data": decoded}
                if operation == "create_inbox" and output["ok"]:
                    # The inbox now exists at the vendor; bind it to this hire so
                    # no other hire on this actor can send from or read it.
                    self._bind_inbox(record["hire_id"], decoded.get("inbox_id"))
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        record["expires_height"] = runtime.get_block_height() + RESULT_RETENTION_BLOCKS
        if record.get("create_uncertain"):
            record["expires_height"] = runtime.get_block_height() + UNCERTAIN_CREATE_BLOCKS
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
            "slot": "email-inbox",
            "handlers": ["create_inbox", "send", "reply", "list_messages",
                         "get_message", "search"],
            "poll_handler": "get_result",
            "returns": "agentmail-async-result",
            "create_inbox_input": {"hire_id": "int", "username": "str?", "display_name": "str?"},
            "send_input": {"hire_id": "int", "inbox_id": "str", "to": "str|list",
                           "subject": "str", "text": "str", "html": "str?"},
            "list_messages_input": {"hire_id": "int", "inbox_id": "str", "limit": "int?",
                                    "labels": "[str]?", "after": "rfc3339?", "page_token": "opaque str?"},
            "get_message_input": {"hire_id": "int", "inbox_id": "str", "message_id": "str"},
            "search_input": {"hire_id": "int", "inbox_id": "str", "q": "str", "limit": "int?"},
            "output": "acceptance record; get_result returns the AgentMail response under `data`",
            "notes": ["receive by polling list_messages; webhooks/websocket push "
                      "are unusable on-chain",
                      "list rows carry metadata + preview, not the body; get_message "
                      "returns the parsed text"],
            "excluded": ["webhooks", "websocket_push", "raw_mime_download", "attachments_upload"],
            "hire_activation": {"protocol": "store-admin-v1"},
            "result_byte_budget": RETURN_BYTE_BUDGET,
        })

    @public
    def get_last_output(self, payload):
        # Observability only — never a result read (that is authenticated
        # get_result). Returns metadata, never the vendor `data`/`error`: actor
        # state is public consensus data regardless, but this unauthenticated,
        # cross-hire getter must not casually serve one hire's fetched email
        # content to any caller.
        last = self.storage.get(KEY_LAST_OUTPUT) or {}
        return codec.encode({
            "operation": last.get("operation"),
            "ok": last.get("ok"),
            "status": self.storage.get(KEY_LAST_STATUS),
        })

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
    return AgentMailInboxWrapper()


def init(payload): return _get_actor().init(payload)
def register_hire(payload): return _get_actor().register_hire(payload)
def create_inbox(payload): return _get_actor().create_inbox(payload)
def send(payload): return _get_actor().send(payload)
def reply(payload): return _get_actor().reply(payload)
def list_messages(payload): return _get_actor().list_messages(payload)
def get_message(payload): return _get_actor().get_message(payload)
def search(payload): return _get_actor().search(payload)
def get_result(payload): return _get_actor().get_result(payload)
def capability(payload): return _get_actor().capability(payload)
def get_last_output(payload): return _get_actor().get_last_output(payload)
def get_config(payload): return _get_actor().get_config(payload)


def _run_request__resume(payload):
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
