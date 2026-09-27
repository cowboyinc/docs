"""Kernel browse actor contract checks (visit + stateful step ops)."""

import json

import pytest
import actors.browse.main as kernel
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
SECOND = b"\x55" * 20
PAGE = "https://example.com/p"
BASE = {"hire_id": 42, "url": PAGE}
SESSION = "ydal5siz5f6yusjh3it8jui5"
SESSION_B = "zzzz5siz5f6yusjh3it8bbbb"


def vendor_reply(url=PAGE, title="Example Domain", text="hello world", links=None, session_id=None):
    # Mirror the real /invocations envelope: nested + stringified output.
    page = {
        "ok": True,
        "url": url,
        "title": title,
        "text": text,
        "links": [{"t": "Home", "href": "https://example.com/"}] if links is None else links,
    }
    if session_id is not None:
        page["session_id"] = session_id
    return {
        "action_name": "step",
        "id": "ydal5siz5f6yusjh3it8jui5",
        "status": "succeeded",
        "output": json.dumps(page),
    }


def open_reply(session_id=SESSION):
    return {"status": "succeeded", "output": json.dumps({"ok": True, "session_id": session_id})}


def close_reply(session_id=SESSION):
    return {"status": "succeeded", "output": json.dumps({"ok": True, "closed": session_id})}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def body_of(job):
    return json.loads(kwargs_of(job)["raw_body"])


def step_payload(job):
    return json.loads(body_of(job)["payload"])


def submit(actor, host, sender=BUYER, **payload):
    host.set_context(sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    fields = dict(BASE)
    fields.update(payload)
    accepted = codec.decode(actor.visit(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def submit_step(actor, host, op, sender=BUYER, hire_id=42, **fields):
    host.set_context(sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    payload = {"hire_id": hire_id, "op": op}
    payload.update(fields)
    accepted = codec.decode(actor.step(codec.encode(payload)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = kernel._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": result}
        )
    )
    return codec.decode(out) if out else None


def read(actor, host, request_id, sender=BUYER, hire_id=42, **fields):
    host.set_context(sender=sender)
    return codec.decode(
        actor.get_result(
            codec.encode({"request_id": request_id, "hire_id": hire_id, **fields})
        )
    )


def run_full(actor, host, result=None, **payload):
    accepted, job = submit(actor, host, **payload)
    final = resume(host, job, vendor_reply() if result is None else result)
    return accepted, final, job


def register(actor, host, hire_id, hirer):
    host.set_context(sender=OWNER)
    return codec.decode(
        actor.register_hire(
            codec.encode(
                {
                    "hire_id": hire_id,
                    "hirer": "0x" + hirer.hex(),
                    "declaration_id": "0x" + "55" * 32,
                    "version": 3,
                    "artifact_id": 7,
                }
            )
        )
    )


def open_session(actor, host, sender=BUYER, hire_id=42, session_id=SESSION):
    """Open a session for a hire and resume so it is stored `open`."""
    accepted, job = submit_step(actor, host, "open", sender=sender, hire_id=hire_id)
    assert accepted["ok"] is True
    resume(host, job, open_reply(session_id))
    return accepted["request_id"]


# --------------------------------------------------------------------------
# visit (stateless one-shot read)
# --------------------------------------------------------------------------


def test_visit_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(actor, host)
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "visit",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == kernel.INVOKE_URL
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer ${KERNEL_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": kernel.USER_AGENT,
    }
    assert "Idempotency-Key" not in kwargs["headers"]  # visit is read-only
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), kernel.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    body = body_of(job)
    assert body["app_name"] == "cowboy-browse"
    assert body["action_name"] == "visit"
    assert json.loads(body["payload"]) == {"url": PAGE}
    resume(host, job, vendor_reply(text="one small step"))
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"]["url"] == PAGE
    assert out["output"]["data"]["title"] == "Example Domain"
    assert out["output"]["data"]["text"] == "one small step"
    assert out["output"]["data"]["links"] == [{"t": "Home", "href": "https://example.com/"}]


def test_bare_domain_is_normalised_to_https(actor, host):
    _, job = submit(actor, host, url="example.com")
    assert json.loads(body_of(job)["payload"]) == {"url": "https://example.com"}


def test_get_result_returns_full_page_not_clipped(actor, host):
    text = "y" * 5000
    accepted, _, _ = run_full(
        actor, host, result=vendor_reply(text=text, links=[{"t": "A", "href": "https://a"}])
    )
    data = read(actor, host, accepted["request_id"])["output"]["data"]
    assert data["text"] == text
    assert "truncated" not in data
    assert data["links"] == [{"t": "A", "href": "https://a"}]
    assert data["url"] == PAGE and data["title"] == "Example Domain"


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "javascript:alert(1)"},
        {"url": "file:///etc/passwd"},
        {"url": "data:text/html,x"},
        {"url": "about:blank"},
        {"url": "ftp://example.com/a"},
        {"url": "https://example.com/a b"},
        {"url": 'https://x.com"+fetch("https://evil'},
        {"url": 7},
        {"url": "x" * (kernel.MAX_URL_CHARS + 1)},
        {"unexpected": "value"},
    ],
)
def test_invalid_visit_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(BASE)
    fields.update(payload)
    out = codec.decode(actor.visit(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    for payload in (
        {"url": "https://x.com/${KERNEL_API_KEY}"},
        {"url": "https://x.com/${SECRET}/a"},
        {"url": "${KERNEL_API_KEY}"},
    ):
        fields = dict(BASE)
        fields.update(payload)
        out = codec.decode(actor.visit(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


# --------------------------------------------------------------------------
# step — per-op dispatch bodies
# --------------------------------------------------------------------------


def test_step_open_dispatch(actor, host):
    accepted, job = submit_step(actor, host, "open")
    assert accepted["ok"] is True and accepted["operation"] == "step" and accepted["op"] == "open"
    body = body_of(job)
    assert body["action_name"] == "step"
    assert step_payload(job) == {"op": "open"}  # no session_id from the caller
    assert "Idempotency-Key" in kwargs_of(job)["headers"]  # mutating


def test_step_open_with_opts_dispatch(actor, host):
    _, job = submit_step(actor, host, "open", stealth=True, timeout_seconds=600)
    assert step_payload(job) == {"op": "open", "stealth": True, "timeout_seconds": 600}


def test_step_goto_auto_open_when_no_session(actor, host):
    accepted, job = submit_step(actor, host, "goto", url="https://ex.com/a")
    # a hire with no session auto-opens: empty session_id => the Kernel app opens.
    assert step_payload(job) == {"op": "goto", "session_id": "", "url": "https://ex.com/a"}
    assert "Idempotency-Key" in kwargs_of(job)["headers"]
    # the auto-open response carries a new session id; the actor stores it.
    resume(host, job, vendor_reply(url="https://ex.com/a", session_id="NEWSESS"))
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["data"]["session_id"] == "NEWSESS"
    # the next op uses the stored session id, not one supplied by the caller.
    _, job2 = submit_step(actor, host, "extract")
    assert step_payload(job2) == {"op": "extract", "session_id": "NEWSESS"}


def test_auto_open_error_with_session_id_remains_closeable(actor, host):
    accepted, job = submit_step(actor, host, "goto", url="https://ex.com/a")
    resume(
        host,
        job,
        {"status": "succeeded", "output": json.dumps({
            "ok": False,
            "error": "page result exceeds 32000 bytes",
            "session_id": "NEWSESS",
        })},
    )
    assert read(actor, host, accepted["request_id"])["status"] == "error"
    assert kernel_session(host, 42)["session_id"] == "NEWSESS"
    close, close_job = submit_step(actor, host, "close")
    assert close["ok"] is True and step_payload(close_job)["session_id"] == "NEWSESS"


def test_auto_open_error_with_non_string_session_id_does_not_promote(actor, host):
    accepted, job = submit_step(actor, host, "goto", url="https://ex.com/a")
    resume(
        host,
        job,
        {"status": "succeeded", "output": json.dumps({
            "ok": False, "error": "bad response", "session_id": 7,
        })},
    )
    assert read(actor, host, accepted["request_id"])["status"] == "error"
    assert kernel_session(host, 42) is None


def test_step_uses_per_hire_stored_session(actor, host):
    # open stores the hire's own session; the next op dispatches WITH that id.
    open_session(actor, host, session_id=SESSION)
    _, job = submit_step(actor, host, "goto", url="https://ex.com/z")
    assert step_payload(job) == {"op": "goto", "session_id": SESSION, "url": "https://ex.com/z"}


@pytest.mark.parametrize(
    "op,fields,expected",
    [
        ("goto", {"url": "https://ex.com/a"}, {"op": "goto", "session_id": SESSION, "url": "https://ex.com/a"}),
        ("click", {"selector": "#buy"}, {"op": "click", "session_id": SESSION, "selector": "#buy"}),
        ("type", {"selector": "#q", "text": "cowboys"}, {"op": "type", "session_id": SESSION, "selector": "#q", "text": "cowboys"}),
        ("press", {"key": "Enter"}, {"op": "press", "session_id": SESSION, "key": "Enter"}),
        ("press", {}, {"op": "press", "session_id": SESSION, "key": "Enter"}),
        ("scroll", {"dy": 1200}, {"op": "scroll", "session_id": SESSION, "dy": 1200}),
        ("scroll", {}, {"op": "scroll", "session_id": SESSION, "dy": 800}),
        ("extract", {}, {"op": "extract", "session_id": SESSION}),
        ("close", {}, {"op": "close", "session_id": SESSION}),
    ],
)
def test_step_op_bodies_source_the_stored_session(actor, host, op, fields, expected):
    open_session(actor, host, session_id=SESSION)
    accepted, job = submit_step(actor, host, op, **fields)
    assert accepted["ok"] is True and accepted["op"] == op
    assert body_of(job)["action_name"] == "step"
    assert step_payload(job) == expected


@pytest.mark.parametrize("op", list(kernel.MUTATING_OPS))
def test_mutating_ops_carry_idempotency_key(actor, host, op):
    if op != "open":
        open_session(actor, host, session_id=SESSION)
    fields = {}
    if op == "goto":
        fields = {"url": "https://ex.com/a"}
    elif op == "click":
        fields = {"selector": "#b"}
    elif op == "type":
        fields = {"selector": "#i", "text": "hi"}
    accepted, job = submit_step(actor, host, op, **fields)
    assert accepted["ok"] is True
    key = kwargs_of(job)["headers"].get("Idempotency-Key")
    assert key == "kernel-browse-%d" % accepted["request_id"]


@pytest.mark.parametrize("op", list(kernel.READONLY_OPS))
def test_readonly_ops_have_no_idempotency_key(actor, host, op):
    open_session(actor, host, session_id=SESSION)
    _, job = submit_step(actor, host, op)
    assert "Idempotency-Key" not in kwargs_of(job)["headers"]


def test_step_open_returns_session_id_only(actor, host):
    accepted, job = submit_step(actor, host, "open")
    resume(host, job, open_reply("SID123"))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["op"] == "open"
    assert out["output"]["data"] == {"session_id": "SID123"}


def test_step_close_clears_the_session(actor, host):
    open_session(actor, host, session_id=SESSION)
    accepted, job = submit_step(actor, host, "close")
    # The binding remains until Kernel confirms the close.
    denied, no_job = submit_step(actor, host, "extract")
    assert denied["ok"] is False and no_job is None and "closing" in denied["error"]
    resume(host, job, close_reply(SESSION))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["data"] == {"closed": SESSION}
    assert kernel_session(host, 42) is None


def test_failed_close_keeps_binding_and_retry_replaces_receipt(actor, host):
    open_session(actor, host)
    accepted, job = submit_step(actor, host, "close")
    resume(host, job, {"status": "failed", "output": "", "error": "temporary"})
    session = kernel_session(host, 42)
    assert session["status"] == "closing" and session["session_id"] == SESSION
    retry, retry_job = submit_step(actor, host, "close")
    assert retry["ok"] is True and step_payload(retry_job)["session_id"] == SESSION
    assert host._storage.get(("request:" + str(accepted["request_id"])).encode()) is None


def test_refused_close_retry_keeps_previous_failed_receipt(actor, host):
    open_session(actor, host)
    failed, job = submit_step(actor, host, "close")
    resume(host, job, {"status": "failed", "output": "", "error": "temporary"})
    host.set_context(sender=BUYER, tx_hash=(777).to_bytes(32, "big"))
    pending = codec.decode(actor.visit(codec.encode(BASE)))
    refused = codec.decode(actor.step(codec.encode({"hire_id": 42, "op": "close"})))
    assert pending["ok"] is True and refused["ok"] is False
    assert "transaction" in refused["error"]
    assert host._storage.get(("request:" + str(failed["request_id"])).encode()) is not None


def test_pending_close_cannot_be_duplicated(actor, host):
    open_session(actor, host)
    accepted, _ = submit_step(actor, host, "close")
    retry, job = submit_step(actor, host, "close")
    assert accepted["ok"] is True
    assert retry["ok"] is False and job is None and "still pending" in retry["error"]


def test_close_reachable_at_per_hire_and_global_receipt_capacity(actor, host):
    open_session(actor, host)
    for _ in range(kernel.MAX_RETAINED_PER_HIRE - 1):
        accepted, _ = submit(actor, host)
        assert accepted["ok"] is True
    close, _ = submit_step(actor, host, "close")
    assert close["ok"] is True

    actor2 = kernel.KernelBrowseWrapper()
    host.reset()
    host.set_context(sender=OWNER, actor_addr=ACTOR)
    actor2.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    senders = {}
    for i in range(4):
        hire_id, sender = 100 + i, bytes([80 + i]) * 20
        register(actor2, host, hire_id, sender)
        senders[hire_id] = sender
    open_session(actor2, host, sender=senders[100], hire_id=100)
    while len(codec.decode(host._storage[kernel.KEY_REQUEST_IDS.encode()])) < kernel.MAX_RETAINED_REQUESTS:
        for hire_id, sender in senders.items():
            accepted, _ = submit(actor2, host, sender=sender, hire_id=hire_id)
            if not accepted["ok"]:
                continue
            if len(codec.decode(host._storage[kernel.KEY_REQUEST_IDS.encode()])) == kernel.MAX_RETAINED_REQUESTS:
                break
    close, _ = submit_step(actor2, host, "close", sender=senders[100], hire_id=100)
    assert close["ok"] is True


# --------------------------------------------------------------------------
# step — session sourcing / anti-cross-hire property
# --------------------------------------------------------------------------


def test_ops_need_a_session_and_none_can_be_supplied(actor, host):
    # a hire with no session cannot click/type/press/scroll/extract/close.
    for op in ("click", "type", "press", "scroll", "extract", "close"):
        fields = {}
        if op in ("click", "type"):
            fields["selector"] = "#x"
        if op == "type":
            fields["text"] = "hi"
        out, job = submit_step(actor, host, op, **fields)
        assert out["ok"] is False and job is None
    assert not host.get_send_log()
    # the caller cannot supply a session_id at all — it is an unsupported field.
    out = codec.decode(actor.step(codec.encode({"hire_id": 42, "op": "extract", "session_id": SESSION})))
    assert out["ok"] is False and "unsupported request fields" in out["error"]
    assert "session_id" in out["error"]
    assert not host.get_send_log()


def test_second_hire_cannot_use_first_hires_session(actor, host):
    register(actor, host, 43, SECOND)
    # hire 42 opens and stores SESSION_A.
    open_session(actor, host, sender=BUYER, hire_id=42, session_id="SESSION_A")
    log_before = len(host.get_send_log())
    # hire 43 (a different hirer) has no session; it cannot click.
    out, job = submit_step(actor, host, "click", sender=SECOND, hire_id=43, selector="#b")
    assert out["ok"] is False and job is None and "no open session" in out["error"]
    # nor can it NAME hire 42's session id — session_id is rejected as unsupported.
    host.set_context(sender=SECOND, tx_hash=(999).to_bytes(32, "big"))
    out2 = codec.decode(
        actor.step(codec.encode({"hire_id": 43, "op": "click", "selector": "#b", "session_id": "SESSION_A"}))
    )
    assert out2["ok"] is False
    assert len(host.get_send_log()) == log_before  # no dispatch on either attempt
    # hire 42 still owns its own session and dispatches with SESSION_A.
    _, job42 = submit_step(actor, host, "extract", sender=BUYER, hire_id=42)
    assert step_payload(job42) == {"op": "extract", "session_id": "SESSION_A"}


def test_second_open_refused_while_session_active(actor, host):
    open_session(actor, host, session_id=SESSION)
    out, job = submit_step(actor, host, "open")
    assert out["ok"] is False and job is None and "already has an active session" in out["error"]


def test_ops_refused_while_session_opening(actor, host):
    # open is submitted but NOT resumed => session is "opening", id unknown.
    accepted, _ = submit_step(actor, host, "open")
    for op in ("goto", "click", "extract", "close"):
        fields = {"url": "https://x"} if op == "goto" else ({"selector": "#a"} if op == "click" else {})
        out, job = submit_step(actor, host, op, **fields)
        assert out["ok"] is False and job is None and "opening" in out["error"]


def test_failed_open_frees_the_session_slot(actor, host):
    accepted, job = submit_step(actor, host, "open")
    resume(host, job, {"status": "failed", "output": "", "error": "no capacity"})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error"
    # slot freed: a fresh open is allowed again.
    ok, job2 = submit_step(actor, host, "open")
    assert ok["ok"] is True and job2 is not None


# --------------------------------------------------------------------------
# step — input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,fields",
    [
        ("frobnicate", {}),
        ("goto", {"url": "javascript:alert(1)"}),
        ("goto", {"url": ""}),
        ("click", {"selector": ""}),
        ("click", {"selector": "x" * (kernel.MAX_SELECTOR_CHARS + 1)}),
        ("click", {"selector": "a\r\nb"}),
        ("click", {"selector": "a\tb"}),
        ("type", {"selector": "#i", "text": "a\nb"}),
        ("type", {"selector": "#i", "text": "a\x00b"}),
        ("type", {"selector": "#i"}),  # missing text
        ("press", {"key": "En\nter"}),
        ("scroll", {"dy": "abc"}),
        ("scroll", {"dy": 10**9}),
        ("open", {"stealth": "yes"}),
        ("open", {"timeout_seconds": 5}),
        ("open", {"badfield": 1}),
    ],
)
def test_step_invalid_inputs_refuse_before_dispatch(actor, host, op, fields):
    # ops that need a session get one first, so the failure is the param, not the session.
    if op in ("click", "type", "press", "scroll"):
        open_session(actor, host, session_id=SESSION)
    before = len(host.get_send_log())
    out, job = submit_step(actor, host, op, **fields)
    assert out["ok"] is False and job is None
    assert len(host.get_send_log()) == before


@pytest.mark.parametrize(
    "op,fields",
    [
        ("goto", {"url": "https://x.com/${KERNEL_API_KEY}"}),
        ("click", {"selector": "#${SECRET}"}),
        ("type", {"selector": "#i", "text": "${KERNEL_API_KEY}"}),
        ("press", {"key": "${SECRET}"}),
    ],
)
def test_step_secret_templates_never_dispatch(actor, host, op, fields):
    if op in ("click", "type", "press"):
        open_session(actor, host, session_id=SESSION)
    before = len(host.get_send_log())
    out, job = submit_step(actor, host, op, **fields)
    assert out["ok"] is False and job is None
    assert len(host.get_send_log()) == before


# --------------------------------------------------------------------------
# session lifecycle / cleanup
# --------------------------------------------------------------------------


def test_session_cleanup_on_deadline_closes_and_clears(actor, host):
    open_session(actor, host, session_id=SESSION)
    assert kernel_session(host, 42) is not None
    log_before = len(host.get_send_log())
    # advance past the session's idle deadline and trigger a full cleanup pass.
    deadline = kernel_session(host, 42)["deadline_height"]
    host.set_block_height(deadline)
    host.set_context(sender=BUYER, tx_hash=(4242).to_bytes(32, "big"))
    kernel.KernelBrowseWrapper._cleanup(actor)
    # session record is cleared ...
    assert kernel_session(host, 42) is None
    # ... and a best-effort close step was dispatched for that session.
    closes = [
        step_payload_of(codec.decode(entry["payload"]))
        for entry in host.get_send_log()[log_before:]
    ]
    assert {"op": "close", "session_id": SESSION} in closes
    expired = [name for name, _ in host.events if name == "wrapper.session_expired"]
    assert expired


def kernel_session(host, hire_id):
    key = ("session:" + str(hire_id)).encode()
    raw = host._storage.get(key)
    return codec.decode(raw) if raw is not None else None


def step_payload_of(job):
    kwargs = job.get("payload", {}).get("kwargs", {})
    raw = kwargs.get("raw_body")
    if not raw:
        return None
    return json.loads(json.loads(raw)["payload"])


def test_active_session_capacity_is_bounded(actor, host):
    host.set_context(sender=OWNER)
    for i in range(kernel.MAX_ACTIVE_SESSIONS):
        hire_id = 100 + i
        register(actor, host, hire_id, bytes([i + 1]) * 20)
        open_session(actor, host, sender=bytes([i + 1]) * 20, hire_id=hire_id, session_id="S%d" % i)
    register(actor, host, 999, b"\xfe" * 20)
    out, job = submit_step(actor, host, "open", sender=b"\xfe" * 20, hire_id=999)
    assert out["ok"] is False and job is None and "active session capacity" in out["error"]


# --------------------------------------------------------------------------
# shared authorization / result-delivery contract (visit + step)
# --------------------------------------------------------------------------


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.visit(codec.encode(dict(BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.visit(codec.encode(dict(BASE, hire_id=42)))
    with pytest.raises(ValueError):
        actor.step(codec.encode({"hire_id": 42, "op": "open"}))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"status": "succeeded", "output": json.dumps({"ok": False, "error": "browser not found"})}, "browser not found"),
        ({"status": "failed", "output": "", "error": "invocation crashed"}, "invocation failed"),
        ({"status": "succeeded", "output": json.dumps({"error": {"message": "timeout waiting for page"}})}, "timeout waiting"),
        ({"code": "bad_request", "message": "invalid payload"}, "invalid payload"),
        ("<html>502 Bad Gateway</html>", "502 Bad Gateway"),
        ({"status": "succeeded", "output": "not-json-at-all"}, "not-json-at-all"),
        ({"output": json.dumps({"ok": True, "url": "https://x", "text": "hi"})}, "invocation missing status"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]
    assert "data" not in out["output"]


@pytest.mark.parametrize(
    "page",
    [
        {"ok": True},
        {"ok": True, "title": "no url or text"},
        {"ok": True, "url": 7, "text": 9},
        {"ok": True, "links": [{"t": "a", "href": "https://a"}]},
        {"url": "https://x", "text": "hi"},  # ok missing => not a success
        {"ok": False, "url": "https://x", "text": "hi"},  # ok:false => error
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, page):
    reply = {"status": "succeeded", "output": json.dumps(page)}
    accepted, _, _ = run_full(actor, host, result=reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_oversized_text_is_bounded_and_truncated(actor, host):
    accepted, _, _ = run_full(
        actor, host, result=vendor_reply(text="z" * 200_000, links=[])
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    data = out["output"]["data"]
    assert data["truncated"] is True
    assert len(data["text"].encode("utf-8")) <= kernel.TEXT_BYTE_BUDGET
    assert len(codec.encode(out)) < kernel.RETURN_BYTE_BUDGET


def test_oversized_whole_reply_is_error_not_truncated_false_success(actor, host):
    links = [{"t": "L%d" % i, "href": "https://x/" + "h" * 1000} for i in range(40)]
    accepted, _, _ = run_full(actor, host, result=vendor_reply(text="z" * 20_000, links=links))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "byte budget" in out["output"]["error"]
    assert "data" not in out["output"]
    assert len(codec.encode(out)) < kernel.RETURN_BYTE_BUDGET


def test_oversized_url_and_href_are_explicit_errors(actor, host):
    cases = [
        vendor_reply(url="https://x/" + "u" * kernel.MAX_URL_CHARS),
        vendor_reply(links=[{"t": "exact", "href": "https://x/" + "h" * kernel.MAX_LINK_HREF_CHARS}]),
    ]
    for result in cases:
        accepted, _, _ = run_full(actor, host, result=result)
        out = read(actor, host, accepted["request_id"])
        assert out["status"] == "error"
        assert "URL exceeds" in out["output"]["error"]
        assert "data" not in out["output"]


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(kernel.KernelBrowseWrapper._run_request, "_is_continuation", False)
    assert getattr(kernel.KernelBrowseWrapper._run_close, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(kernel.KernelBrowseWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host)
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, vendor_reply(), sender=forged)


def test_continuation_contains_only_request_id(actor, host):
    accepted, job = submit(actor, host)
    continuations = [
        codec.decode(value)
        for key, value in host._storage.items()
        if key.startswith(b"__continuation:")
    ]
    assert len(continuations) == 1
    assert continuations[0].get("ctx") == {"request_id": accepted["request_id"]}
    kwargs = kwargs_of(job)
    assert kwargs["headers"]["Authorization"] == "Bearer ${KERNEL_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), kernel.SECRET_KEY]]
    assert "${KERNEL_API_KEY}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(kernel.MAX_RETAINED_PER_HIRE):
        accepted, _ = submit(actor, host, sender=BUYER)
        assert accepted["ok"] is True
    full, _ = submit(actor, host, sender=BUYER)
    assert full["ok"] is False and "retained request capacity" in full["error"]
    register(actor, host, 43, ATTACKER)
    other, job = submit(actor, host, sender=ATTACKER, hire_id=43)
    assert other["ok"] is True and job is not None


def test_global_request_capacity_is_bounded(actor, host):
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20), (45, b"\x66" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    accepted_count = 0
    refused = None
    while accepted_count < kernel.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit(actor, host, sender=sender, hire_id=hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == kernel.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_deadline_expiry_is_terminal_and_late_result_is_stale(actor, host):
    accepted, job = submit(actor, host)
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    payload = codec.encode(BASE)
    first = codec.decode(actor.visit(payload))
    duplicate = codec.decode(actor.visit(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, url="https://x/1")
    second, second_job = submit(actor, host, url="https://x/2")
    resume(host, second_job, vendor_reply(url="https://x/2", text="second"))
    resume(host, first_job, vendor_reply(url="https://x/1", text="first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["text"] == "first"
    assert read(actor, host, second["request_id"])["output"]["data"]["text"] == "second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    accepted, _, _ = run_full(actor, host)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= kernel.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(
        actor, host, result=vendor_reply(text="PRIVATE-PAGE-TEXT")
    )
    assert read(actor, host, accepted["request_id"])["output"]["data"]["text"] == "PRIVATE-PAGE-TEXT"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta and "text" not in meta
    assert "PRIVATE-PAGE-TEXT" not in codec.encode(meta).decode("latin1")


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


def test_register_hire_requires_registrar(actor, host):
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError, match="registrar"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=99)))
    host.set_context(sender=OWNER)
    ok = codec.decode(actor.register_hire(codec.encode(dict(HIRE, hire_id=99))))
    assert ok["ok"] is True and ok["hire_id"] == 99


def test_init_is_one_time(actor, host):
    host.set_context(sender=OWNER)
    with pytest.raises(ValueError, match="initialized"):
        actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "kernel"
    assert cap["handlers"] == ["visit", "step"]
    assert cap["poll_handler"] == "get_result"
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert cap["endpoint"] == "POST /invocations"
    assert cap["app"] == "cowboy-browse"
    assert cap["actions"] == ["visit", "step"]
    assert cap["step_ops"] == list(kernel.STEP_OPS)
    assert cap["mutating"] is True
    assert cap["mutating_ops"] == list(kernel.MUTATING_OPS)
    assert cap["result_byte_budget"] == kernel.RETURN_BYTE_BUDGET
    assert cap["max_result_chunk_bytes"] == kernel.MAX_RESULT_CHUNK_BYTES
    assert any("transport cap" in w for w in cap["warnings"])
    assert any("4000" in w for w in cap["warnings"])


def test_bounded_int_rejects_floats_and_non_finite():
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            kernel._bounded_int(bad, "request_id", 1, 1000)
    assert kernel._bounded_int(7, "request_id", 1, 1000) == 7
    assert kernel._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join("tests", "test_kernel_parity.py")
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    work = tmp_path / "g"
    shutil.copytree(here, work, ignore=shutil.ignore_patterns(".offline", "__pycache__"))
    fx = next(p for p in os.listdir(work) if p.endswith("_routes.json"))
    def _bust(v):
        if isinstance(v, dict):
            return {k: _bust(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_bust(x) for x in v]
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return "__DRIFT__"
        if isinstance(v, (int, float)):
            return -999999
        return v
    path = os.path.join(str(work), fx)
    with open(path) as fh:
        data = json.load(fh)
    with open(path, "w") as fh:
        json.dump(_bust(data), fh)
    bad = subprocess.run([sys.executable, os.path.join(str(work), script)],
                         cwd=str(work), capture_output=True, text=True)
    assert bad.returncode != 0
