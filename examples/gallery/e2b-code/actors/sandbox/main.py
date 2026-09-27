"""Hire-gated E2B execution with request-specific, retained results.

One Runner MCP call owns sandbox creation, execution and cleanup. Only its
bounded final result returns to the Actor; live sandbox capabilities stay with
that tool. Caller code is hex-encoded before CBSS argument substitution.
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


VENDOR = "e2b"
ACTOR_VERSION = "2.1.0"
MCP_SERVER = "e2b"
MCP_TOOL = "execute"
MCP_TIMEOUT_SECONDS = 26
SECRET_KEY = "E2B_API_KEY"

# The tool owns the sandbox lifecycle. This is its fallback sandbox TTL;
# execution and cleanup must finish within the tool's separate 24-second limit.
DEFAULT_SANDBOX_TIMEOUT = 180
MAX_SANDBOX_TIMEOUT = 600
LANGUAGES = ("", "python", "javascript", "typescript", "r", "java", "bash")
MAX_CODE_BYTES = 24_000

# The Runner tool bounds its final JSON before the callback. Leave space for
# the Actor's request identity and retained-result metadata.
RETURN_BYTE_BUDGET = 38_000
TOOL_BYTE_BUDGET = 36_976  # RETURN_BYTE_BUDGET - 1_024
MAX_RESULT_CHUNK_BYTES = 3_900  # Leaves framing space under the 4096-byte event cap.
MAX_RESULT_ITEMS = 20
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


def _tool_output(result):
    """Accept one bounded text result from the configured Runner MCP tool."""
    invalid = {"ok": False, "operation": "run", "error": "invalid E2B tool result", "stage": "tool"}
    reply = result.data if isinstance(result, runner.RunnerResult) else result
    if (not isinstance(reply, dict) or reply.get("server") != MCP_SERVER
            or reply.get("tool") != MCP_TOOL or reply.get("is_error") is not False):
        return invalid
    content = reply.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get("type") != "text"):
        return invalid
    text = content[0].get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > TOOL_BYTE_BUDGET:
        return invalid
    try:
        value = json.loads(text)
    except Exception:
        return invalid
    if (not isinstance(value, dict) or type(value.get("ok")) is not bool
            or set(value) - {"ok", "operation", "data", "error", "stage"}
            or value.get("operation", "run") != "run"):
        return invalid
    output = {"ok": value["ok"], "operation": "run"}
    data = value.get("data")
    if data is not None:
        if (not isinstance(data, dict)
                or set(data) - {"stdout", "stderr", "results", "artifacts", "error"}
                or not isinstance(data.get("stdout"), str)
                or not isinstance(data.get("stderr"), str)
                or not isinstance(data.get("results"), list)
                or len(data["results"]) > MAX_RESULT_ITEMS
                or not isinstance(data.get("artifacts"), list)
                or any(item not in ("png", "jpeg", "svg", "pdf", "latex") for item in data["artifacts"])):
            return invalid
        for item in data["results"]:
            if not isinstance(item, dict) or not item or set(item) - {"text", "json"}:
                return invalid
            if "text" in item and not isinstance(item["text"], str):
                return invalid
        error = data.get("error")
        if error is not None and (not isinstance(error, dict)
                or set(error) != {"name", "value", "traceback"}
                or any(not isinstance(field, str) for field in error.values())):
            return invalid
        if value["ok"] is not (error is None):
            return invalid
        output["data"] = data
    elif value["ok"]:
        return invalid
    if value.get("error") is not None:
        if not isinstance(value["error"], str):
            return invalid
        output["error"] = value["error"]
    if value.get("stage") is not None:
        if value["stage"] not in ("create", "execute", "cleanup", "output", "busy"):
            return invalid
        output["stage"] = value["stage"]
    if not value["ok"] and data is None and not output.get("error"):
        return invalid
    return output


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


@actor
class E2BCodeWrapper:
    """Sells sandboxed code execution per-call to any hiring actor."""

    @public
    def init(self, payload):
        if self.storage.get(KEY_REGISTRAR):
            raise ValueError("init: actor is already initialized")
        cfg = _parse(payload)
        owner = _address_hex(runtime.assume_ownership_if_unowned(), "owner")
        registrar = _address_hex(cfg.get("store_registrar") or owner, "store_registrar")
        self.storage["__name__"] = "E2B Code"
        self.storage["__description__"] = (
            "Run untrusted Python/JS in an E2B sandbox: code in, stdout and "
            "results out. Vendor key held in CBSS."
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

    @public
    @deferred
    def run(self, payload):
        """payload: {"hire_id": int, "code": str, "language": str?, "timeout": int?}.
        Returns an acceptance record; read the result from get_result."""
        req = _parse(payload)
        hire_id, hire, refusal = self._hire(req, "run")
        if refusal is not None:
            return refusal
        code = str(req.get("code") or "")
        if not code.strip():
            return codec.encode({"ok": False, "error": "code is required"})
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return codec.encode({"ok": False, "error": "code exceeds %d bytes" % MAX_CODE_BYTES})
        language = str(req.get("language") or "").strip().lower()
        if language not in LANGUAGES:
            return codec.encode({"ok": False, "error": "language must be one of %s"
                                 % ", ".join(x for x in LANGUAGES if x)})
        ttl = _clamp_int(req.get("timeout"), 5, MAX_SANDBOX_TIMEOUT, DEFAULT_SANDBOX_TIMEOUT)
        return self._accept(hire_id, hire, code, language, ttl)

    def _accept(self, hire_id, hire, code, language, ttl):
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
            "operation": "run", "status": "pending", "cid": cid,
            "deadline_height": runtime.get_block_height() + REQUEST_LIFETIME_BLOCKS,
        }
        self.storage["request:" + str(request_id)] = record
        self.storage[KEY_REQUEST_IDS] = ids + [request_id]
        self.storage[KEY_CALLS] = request_id
        self._run_request(codec.encode({
            "request_id": request_id, "code": code, "language": language, "ttl": ttl,
            "hire": self._pins(hire_id, hire), "secret_account": self.storage[KEY_SECRET_ACCOUNT],
        }))
        accepted = {"ok": True, "request_id": request_id, "hire_id": hire_id,
                    "operation": "run", "status": "pending", "poll_handler": "get_result"}
        runtime.emit_event("wrapper.request_accepted", accepted)
        return codec.encode(accepted)

    @runner.continuation
    async def _run_request(self, payload):
        req = _parse(payload)
        ctx = capture()
        ctx.request_id = req["request_id"]
        # Hex keeps caller ${...} text out of CBSS template substitution. The
        # tool decodes it only after receiving its separate api_key argument.
        result = await runner.mcp(
            server=MCP_SERVER, tool=MCP_TOOL,
            arguments={"api_key": "${%s}" % SECRET_KEY,
                       "code_hex": req["code"].encode("utf-8").hex(),
                       "language": req["language"] or "python",
                       "timeout_seconds": req["ttl"]},
            timeout_seconds=MCP_TIMEOUT_SECONDS,
            secrets=[[req["secret_account"], SECRET_KEY]],
            hire=req["hire"],
        )
        return self._finish(ctx.request_id, result)

    def _finish(self, request_id, result):
        key = "request:" + str(request_id)
        record = self.storage.get(key)
        if not record or record["status"] != "pending":
            return codec.encode({"ok": False, "error": "request is no longer pending"})
        if runtime.get_block_height() >= record["deadline_height"]:
            output = {"ok": False, "operation": "run",
                      "error": "request result did not arrive before its deadline"}
        else:
            output = _tool_output(result)
        record["status"] = "ok" if output["ok"] else "error"
        record["output"] = output
        record["expires_height"] = runtime.get_block_height() + RESULT_RETENTION_BLOCKS
        if len(codec.encode(self._response(record))) > RETURN_BYTE_BUDGET:
            output = {"ok": False, "operation": "run",
                      "error": "result exceeds the encoded result byte budget"}
            record["status"] = "error"
            record["output"] = output
        record.pop("cid", None)
        self.storage[key] = record
        self.storage[KEY_LAST_STATUS] = record["status"]
        self.storage[KEY_LAST_OUTPUT] = output
        runtime.emit_event("wrapper.request_finished",
                           {"request_id": request_id, "hire_id": record["hire_id"],
                            "status": record["status"]})
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
                self._finish(request_id, None)
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
            "vendor": VENDOR, "slot": "code-execution",
            "handlers": ["run"], "poll_handler": "get_result",
            "returns": "e2b-async-result",
            "input": {"hire_id": "int", "code": "str", "language": "python|javascript|typescript|r|java|bash?",
                      "timeout": "int? (5..%d seconds)" % MAX_SANDBOX_TIMEOUT},
            "output": "acceptance record; get_result returns {stdout, stderr, results, artifacts, error}",
            "languages": [x for x in LANGUAGES if x],
            "execution": {"server": MCP_SERVER, "tool": MCP_TOOL,
                          "lifecycle_timeout_seconds": 24},
            "excluded": ["file_upload", "filesystem_persistence", "long_lived_sandboxes",
                         "custom_templates", "binary_display_blobs"],
            "hire_activation": {"protocol": "store-admin-v1"},
            "result_byte_budget": RETURN_BYTE_BUDGET})

    @public
    def get_last_output(self, payload):
        return codec.encode(self.storage.get(KEY_LAST_OUTPUT, {}))

    @public
    def get_config(self, payload):
        return codec.encode({"vendor": VENDOR, "call_count": self.storage.get(KEY_CALLS, 0),
                             "store_registrar": self.storage.get(KEY_REGISTRAR, ""),
                             "last_status": self.storage.get(KEY_LAST_STATUS)})


def _get_actor():
    return E2BCodeWrapper()


def init(payload): return _get_actor().init(payload)
def register_hire(payload): return _get_actor().register_hire(payload)
def run(payload): return _get_actor().run(payload)
def get_result(payload): return _get_actor().get_result(payload)
def capability(payload): return _get_actor().capability(payload)
def get_last_output(payload): return _get_actor().get_last_output(payload)
def get_config(payload): return _get_actor().get_config(payload)


def _run_request__resume(payload):
    try:
        return _get_actor()._run_request__resume(payload)
    except ContinuationNotFoundError:
        return codec.encode({"ok": False, "error": "stale or duplicate callback"})
