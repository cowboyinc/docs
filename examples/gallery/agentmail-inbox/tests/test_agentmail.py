"""Contract checks: real-SDK dispatch, callbacks, and caller result reads."""

import json

import actors.inbox.main as am
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec, continuation

INBOX_OK = {"inbox_id": "agent@agentmail.to", "display_name": "Agent"}
SEND_OK = {"message_id": "msg_1", "thread_id": "thr_1"}
LIST_OK = {"messages": [{"message_id": "m1", "from": "a@b.com", "subject": "Hi", "preview": "hello"}]}
MSG_OK = {"message_id": "m1", "from": "a@b.com", "subject": "Hi", "text": "the body"}
SEARCH_OK = {"messages": [{"message_id": "m1", "subject": "Hi"}]}
INBOX = "agent@agentmail.to"


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, handler, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    job = last_job(host) if accepted.get("ok") else None
    return accepted, job


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = am._run_request__resume(codec.encode(
        {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": result}))
    return codec.decode(out) if out else None


def run_full(actor, host, handler, result, **payload):
    accepted, job = submit(actor, host, handler, **payload)
    final = resume(host, job, result)
    return accepted, final, job


def read(actor, host, request_id, sender=BUYER, **fields):
    host.set_context(sender=sender)
    return codec.decode(actor.get_result(codec.encode({"request_id": request_id, "hire_id": 42, **fields})))


ATTACKER = b"\x44" * 20


def register(actor, host, hire_id, hirer):
    host.set_context(sender=OWNER)
    actor.register_hire(codec.encode({
        "hire_id": hire_id, "hirer": "0x" + hirer.hex(),
        "declaration_id": "0x" + "55" * 32, "version": 3, "artifact_id": 7}))


def submit_as(actor, host, sender, hire_id, handler, **payload):
    host.set_context(sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": hire_id, **payload})))
    job = last_job(host) if accepted.get("ok") else None
    return accepted, job


def test_create_inbox_dispatch_secret_custody_and_result(actor, host):
    accepted, final, job = run_full(actor, host, "create_inbox", INBOX_OK, username="agent")
    assert accepted["status"] == "pending" and "output" not in accepted
    k = kwargs_of(job)
    assert k["url"] == am.INBOXES and k["method"] == "POST"
    assert k["headers"]["Authorization"] == "Bearer ${AGENTMAIL_API_KEY}"
    assert k["headers"]["Content-Type"] == "application/json"
    assert k["secrets"] == [["0x" + OWNER.hex(), "AGENTMAIL_API_KEY"]]
    assert k["hire"] == {"hire_id": 42, "declaration_id": "0x" + "55" * 32, "version": 3, "artifact_id": 7}
    assert "timeout_ms" not in k
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["data"] == INBOX_OK


def test_send_dispatch_and_result(actor, host):
    accepted, _, job = run_full(actor, host, "send", SEND_OK,
                                inbox_id=INBOX, to="dest@x.com", subject="Hi", text="body")
    k = kwargs_of(job)
    assert k["url"].endswith("/inboxes/%s/messages/send" % INBOX) and k["method"] == "POST"
    # POST carries the payload as raw_body (verbatim bytes), never body
    assert "body" not in k and json.loads(k["raw_body"]) == {"to": "dest@x.com", "subject": "Hi", "text": "body"}
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["data"]["message_id"] == "msg_1"


def test_list_messages_is_the_receive_path_and_is_a_get(actor, host):
    accepted, _, job = run_full(actor, host, "list_messages", LIST_OK, inbox_id=INBOX, limit=5)
    k = kwargs_of(job)
    assert k["method"] == "GET" and k["url"].startswith(am.INBOXES + "/%s/messages?" % INBOX)
    assert "limit=5" in k["url"]
    # a GET job must omit body/raw_body + Content-Type entirely (node rejects a GET with a body: E1604)
    assert "body" not in k and "raw_body" not in k and "Content-Type" not in k["headers"]
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["data"]["messages"][0]["message_id"] == "m1"


def test_get_message_and_search_are_gets(actor, host):
    a, _, gj = run_full(actor, host, "get_message", MSG_OK, inbox_id=INBOX, message_id="m1")
    gk = kwargs_of(gj)
    assert gk["method"] == "GET" and gk["url"].endswith("/messages/m1")
    assert "body" not in gk and "raw_body" not in gk
    assert read(actor, host, a["request_id"])["output"]["data"]["text"] == "the body"
    b, _, sj = run_full(actor, host, "search", SEARCH_OK, inbox_id=INBOX, q="Hi")
    sk = kwargs_of(sj)
    assert sk["method"] == "GET" and "/messages/search?" in sk["url"] and "q=Hi" in sk["url"]
    assert read(actor, host, b["request_id"])["output"]["data"]["messages"][0]["message_id"] == "m1"


def test_reply_dispatch(actor, host):
    _, _, job = run_full(actor, host, "reply", SEND_OK, inbox_id=INBOX, message_id="m1", text="re: hi")
    k = kwargs_of(job)
    assert k["method"] == "POST" and k["url"].endswith("/messages/m1/reply")
    assert "body" not in k and json.loads(k["raw_body"]) == {"text": "re: hi"}


def test_secret_templates_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    # A ${...} in an outbound field would leak the key into the email — refuse it.
    assert codec.decode(actor.send(codec.encode(
        {"hire_id": 42, "inbox_id": INBOX, "to": "d@x.com", "subject": "hi",
         "text": "key is ${AGENTMAIL_API_KEY}"})))["ok"] is False
    assert codec.decode(actor.send(codec.encode(
        {"hire_id": 42, "inbox_id": INBOX, "to": "${AGENTMAIL_API_KEY}@x.com",
         "subject": "hi", "text": "body"})))["ok"] is False
    assert codec.decode(actor.create_inbox(codec.encode(
        {"hire_id": 42, "username": "${AGENTMAIL_API_KEY}"})))["ok"] is False
    assert codec.decode(actor.search(codec.encode(
        {"hire_id": 42, "inbox_id": INBOX, "q": "${AGENTMAIL_API_KEY}"})))["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host, "create_inbox", INBOX_OK)
    assert read(actor, host, accepted["request_id"], sender=b"\x44" * 20)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    assert codec.decode(actor.create_inbox(codec.encode({"hire_id": 99})))["ok"] is False
    host.set_context(sender=b"\x44" * 20)
    with pytest.raises(ValueError):
        actor.create_inbox(codec.encode({"hire_id": 42}))
    assert not host.get_send_log()


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host, "create_inbox", INBOX_OK)
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True
    late = resume(host, job, INBOX_OK)
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_resume_path_is_a_continuation_not_a_public_callback(actor, host):
    # The vulnerable pre-canonical design exposed an @public on_runner_result any
    # caller could invoke with a forged result. The canonical design routes vendor
    # results ONLY through the @runner.continuation resume, whose caller (the
    # runner / JOB_DISPATCHER) the node authenticates — a forged _run_request__resume
    # is rejected on-chain (E1230, verified on canyon). That caller check is a node
    # property this SDK head's offline mock does not model (see the xfail below), so
    # here we assert the structural guarantee the offline layer CAN see: the result
    # path is a continuation, and no callback handler sits in the public surface.
    assert getattr(am.AgentMailInboxWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert "_run_request" not in cap["handlers"]


def test_forged_callback_rejected(actor, host):
    accepted, job = submit(actor, host, "create_inbox")
    for forged in (BUYER, OWNER, b"\x44" * 20):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, INBOX_OK, sender=forged)


def test_pending_until_callback_resolves(actor, host):
    accepted, job = submit(actor, host, "list_messages", inbox_id=INBOX)
    assert read(actor, host, accepted["request_id"])["status"] == "pending"
    resume(host, job, LIST_OK)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


@pytest.mark.parametrize("vendor,marker", [
    ({"error": "inbox not found"}, "inbox not found"),
    # AgentMail's real error shape uses code/message/name/docs — not "error" —
    # so this must still be classified an error, not a false success.
    ({"name": "LimitExceededError", "code": "limit_exceeded", "message": "Inbox limit exceeded",
      "docs": "https://docs.agentmail.to/errors#limit_exceeded", "limit": 3}, "Inbox limit exceeded"),
    # code + message alone (no name/docs) is still an error, not a false success.
    ({"code": "bad_request", "message": "Invalid field"}, "Invalid field"),
])
def test_vendor_error_is_status_error(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, "create_inbox", vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False and marker in out["output"]["error"]


def test_missing_required_field_is_not_a_false_success(actor, host):
    # a non-error dict that lacks the operation's required field (send -> message_id)
    # is malformed, not a success.
    accepted, _, _ = run_full(actor, host, "send", {"thread_id": "t1"},
                              inbox_id=INBOX, to="d@x.com", subject="hi", text="b")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_oversized_response_is_bounded(actor, host):
    big = {"messages": [{"message_id": "m%d" % i, "text": "z" * 2000} for i in range(200)]}
    accepted, _, _ = run_full(actor, host, "list_messages", big, inbox_id=INBOX)
    out = read(actor, host, accepted["request_id"])
    assert len(codec.encode(out)) < am.RETURN_BYTE_BUDGET
    assert out["status"] == "error" and "exact result fields" in out["output"]["error"]


@pytest.mark.parametrize("operation,result", [
    ("create_inbox", {"inbox_id": None}),
    ("send", {"message_id": ""}),
    ("list_messages", {"messages": None}),
    ("search", {"messages": "bad"}),
])
def test_required_success_shapes(operation, result, actor, host):
    fields = {"inbox_id": INBOX}
    if operation == "send": fields.update(to="d@x.com", text="b")
    if operation == "search": fields["q"] = "x"
    accepted, _, _ = run_full(actor, host, operation, result, **fields)
    assert read(actor, host, accepted["request_id"])["status"] == "error"


def test_page_token_is_forwarded_and_preserved_exactly(actor, host):
    token = "opaque/+== cursor"
    accepted, job = submit(actor, host, "list_messages", inbox_id=INBOX, page_token=token)
    assert "page_token=opaque%2F%2B%3D%3D%20cursor" in kwargs_of(job)["url"]
    resume(host, job, {"messages": [], "next_page_token": token})
    assert read(actor, host, accepted["request_id"])["output"]["data"]["next_page_token"] == token


def test_input_validation(actor, host):
    host.set_context(sender=BUYER)
    bad = [
        ("send", {"inbox_id": INBOX, "subject": "hi", "text": "b"}),          # no recipient
        ("send", {"inbox_id": INBOX, "to": "d@x.com", "subject": "hi"}),      # no text/html
        ("send", {"inbox_id": "bad/inbox", "to": "d@x.com", "text": "b"}),    # bad inbox id
        ("get_message", {"inbox_id": INBOX, "message_id": "bad/id"}),         # bad message id
        ("search", {"inbox_id": INBOX}),                                      # no q
    ]
    for handler, payload in bad:
        assert codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))["ok"] is False
    assert not host.get_send_log()


def test_global_request_capacity_is_bounded(actor, host):
    # Spread pending requests across enough hires that the per-hire cap does not
    # trip first; the total request table is still globally bounded.
    senders = {42: BUYER}
    for hid, addr in [(43, ATTACKER), (44, b"\x55" * 20), (45, b"\x66" * 20)]:
        register(actor, host, hid, addr)
        senders[hid] = addr
    accepted_ok, refused_global = 0, False
    for _ in range(am.MAX_RETAINED_REQUESTS + 8):
        for hid, sender in senders.items():
            acc, _ = submit_as(actor, host, sender, hid, "list_messages", inbox_id=INBOX)
            if acc["ok"]:
                accepted_ok += 1
            elif "request capacity is full" in acc["error"]:
                refused_global = True
                break
        if refused_global:
            break
    assert refused_global and accepted_ok == am.MAX_RETAINED_REQUESTS


def test_request_capacity_is_per_hire(actor, host):
    # One hire cannot monopolize the request table: it is capped below the global
    # limit, and a different hire keeps its own quota.
    for _ in range(am.MAX_RETAINED_PER_HIRE):
        acc, _ = submit_as(actor, host, BUYER, 42, "list_messages", inbox_id=INBOX)
        assert acc["ok"] is True
    full, _ = submit_as(actor, host, BUYER, 42, "list_messages", inbox_id=INBOX)
    assert full["ok"] is False and "in-flight" in full["error"]
    register(actor, host, 43, ATTACKER)
    other, _ = submit_as(actor, host, ATTACKER, 43, "list_messages", inbox_id=INBOX)
    assert other["ok"] is True


def test_uncertain_create_does_not_consume_request_capacity(actor, host):
    host.set_block_height(10)
    accepted, job = submit(actor, host, "create_inbox")
    host.set_block_height(10 + am.REQUEST_LIFETIME_BLOCKS)
    resume(host, job, None)

    # The uncertain create remains recorded for inbox reservation, but ordinary
    # requests still get the full per-hire capacity.
    for _ in range(am.MAX_RETAINED_PER_HIRE):
        ordinary, ordinary_job = submit(actor, host, "list_messages", inbox_id=INBOX)
        assert ordinary["ok"] is True
        resume(host, ordinary_job, LIST_OK)
    full, _ = submit(actor, host, "list_messages", inbox_id=INBOX)
    assert full["ok"] is False and "in-flight" in full["error"]


def test_uncertain_create_still_reserves_inbox_capacity(actor, host):
    host.set_block_height(10)
    accepted, job = submit(actor, host, "create_inbox")
    host.set_block_height(10 + am.REQUEST_LIFETIME_BLOCKS)
    resume(host, job, None)

    height = 10 + am.REQUEST_LIFETIME_BLOCKS
    for i in range(am.MAX_INBOXES_PER_HIRE - 1):
        host.set_block_height(height)
        create, create_job = submit(actor, host, "create_inbox", username="inbox%d" % i)
        assert create["ok"] is True
        resume(host, create_job, {"inbox_id": "inbox%d@agentmail.to" % i})
        height += am.RESULT_RETENTION_BLOCKS + 1
    host.set_block_height(height)
    reserved, _ = submit(actor, host, "create_inbox", username="full")
    assert reserved["ok"] is False and "inbox capacity" in reserved["error"]


def test_inbox_is_bound_to_creating_hire(actor, host):
    # Hire 42 creates an inbox; on success it is bound to hire 42.
    accepted, job = submit_as(actor, host, BUYER, 42, "create_inbox")
    resume(host, job, {"inbox_id": "victim@agentmail.to"})
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True
    jobs_after_create = len(host.get_send_log())

    # A different hire may not send from, reply into, read, or search that inbox.
    register(actor, host, 43, ATTACKER)
    for handler, extra in [
        ("send", {"to": "x@y.com", "subject": "s", "text": "b"}),
        ("reply", {"message_id": "m1", "text": "b"}),
        ("list_messages", {}),
        ("get_message", {"message_id": "m1"}),
        ("search", {"q": "hi"}),
    ]:
        acc, _ = submit_as(actor, host, ATTACKER, 43, handler,
                           inbox_id="victim@agentmail.to", **extra)
        assert acc["ok"] is False and "another hire" in acc["error"], (handler, acc)
    # None of the refused cross-hire calls dispatched a job.
    assert len(host.get_send_log()) == jobs_after_create

    # The owning hire still uses its own inbox.
    ok, job = submit_as(actor, host, BUYER, 42, "list_messages", inbox_id="victim@agentmail.to")
    assert ok["ok"] is True and job is not None


def test_inbox_binding_is_case_insensitive(actor, host):
    # The vendor may return a mixed-case address; a different-case reference to
    # the same inbox must not dodge the ownership check.
    accepted, job = submit_as(actor, host, BUYER, 42, "create_inbox")
    resume(host, job, {"inbox_id": "Victim@AgentMail.to"})
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True
    register(actor, host, 43, ATTACKER)
    # attacker references the lowercase form -> still denied
    acc, _ = submit_as(actor, host, ATTACKER, 43, "list_messages", inbox_id="victim@agentmail.to")
    assert acc["ok"] is False and "another hire" in acc["error"]
    # the owner referencing a different-case form is still allowed
    ok, job2 = submit_as(actor, host, BUYER, 42, "list_messages", inbox_id="VICTIM@agentmail.to")
    assert ok["ok"] is True and job2 is not None


def test_unbound_inbox_is_allowed(actor, host):
    # An inbox this actor never created (brought in from AgentMail directly) is
    # unbound, so any hire may use it.
    register(actor, host, 43, ATTACKER)
    acc, job = submit_as(actor, host, ATTACKER, 43, "list_messages",
                         inbox_id="external@agentmail.to")
    assert acc["ok"] is True and job is not None


def test_inbox_capacity_is_per_hire(actor, host):
    # A hire can bind at most MAX_INBOXES_PER_HIRE inboxes. Advance past retention
    # between creates so the per-hire request slot frees but the binding persists.
    height = 10
    for i in range(am.MAX_INBOXES_PER_HIRE):
        host.set_block_height(height)
        acc, job = submit_as(actor, host, BUYER, 42, "create_inbox")
        assert acc["ok"] is True, acc
        resume(host, job, {"inbox_id": "inbox%d@agentmail.to" % i})
        assert read(actor, host, acc["request_id"])["output"]["ok"] is True
        height += am.RESULT_RETENTION_BLOCKS + 5
    host.set_block_height(height)
    full, _ = submit_as(actor, host, BUYER, 42, "create_inbox")
    assert full["ok"] is False and "inbox capacity" in full["error"]


def test_mutating_posts_carry_idempotency_key(actor, host):
    # A runner retry of a mutating POST must not double-send: the job carries a
    # stable Idempotency-Key. GET reads carry none.
    _, _, sj = run_full(actor, host, "send", SEND_OK,
                        inbox_id=INBOX, to="d@x.com", subject="hi", text="b")
    assert kwargs_of(sj)["headers"].get("Idempotency-Key")
    _, _, gj = run_full(actor, host, "list_messages", LIST_OK, inbox_id=INBOX)
    assert "Idempotency-Key" not in kwargs_of(gj)["headers"]
    created, create_job = submit(actor, host, "create_inbox")
    create_kwargs = kwargs_of(create_job)
    body = json.loads(create_kwargs["raw_body"])
    assert body["client_id"]
    assert "Idempotency-Key" not in create_kwargs["headers"]


def test_deadline_expiry_is_error_and_late_callback_is_stale(actor, host):
    accepted, job = submit(actor, host, "list_messages", inbox_id=INBOX)
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, LIST_OK)
    assert late is None or late.get("ok") is False


def test_timed_out_create_keeps_a_finite_capacity_reservation(actor, host):
    accepted, _ = submit(actor, host, "create_inbox")
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    assert read(actor, host, accepted["request_id"])["status"] == "error"
    assert actor._inflight_creates(42) == (1, 1)
    host.set_block_height(deadline + am.UNCERTAIN_CREATE_BLOCKS)
    actor._cleanup()
    assert actor._inflight_creates(42) == (0, 0)


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    first = codec.decode(actor.list_messages(codec.encode({"hire_id": 42, "inbox_id": INBOX})))
    assert first["ok"] is True
    dup = codec.decode(actor.create_inbox(codec.encode({"hire_id": 42})))
    assert dup["ok"] is False and "transaction" in dup["error"]


def test_overlapping_requests_keep_their_own_results(actor, host):
    a, _, _ = run_full(actor, host, "get_message", {"message_id": "m1", "text": "AAA"},
                       inbox_id=INBOX, message_id="m1")
    b, _, _ = run_full(actor, host, "get_message", {"message_id": "m2", "text": "BBB"},
                       inbox_id=INBOX, message_id="m2")
    assert a["request_id"] != b["request_id"]
    assert read(actor, host, a["request_id"])["output"]["data"]["text"] == "AAA"
    assert read(actor, host, b["request_id"])["output"]["data"]["text"] == "BBB"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    # Force a full-size chunk (a big body) so the emitted event is exercised at
    # its cap, not trivially small; conftest asserts the encoded event <= 4096.
    big = {"message_id": "m1", "from": "a@b.com", "subject": "Hi", "text": "z" * 30000}
    accepted, _, _ = run_full(actor, host, "get_message", big, inbox_id=INBOX, message_id="m1")
    host.events.clear()
    host.set_context(sender=BUYER)
    resp = codec.decode(actor.get_result(codec.encode(
        {"request_id": accepted["request_id"], "hire_id": 42, "receipt_offset": 0})))
    assert resp["status"] == "ok"
    chunks = [p for name, p in host.events if name == "wrapper.result_chunk"]
    assert chunks and chunks[0]["offset"] == 0
    assert chunks[0]["total"] == len(codec.encode(resp)) > am.MAX_RESULT_CHUNK_BYTES
    assert len(chunks[0]["data"]) <= am.MAX_RESULT_CHUNK_BYTES
    # the full encoded event must fit the node's 4096-byte event-payload cap
    assert len(codec.encode({"request_id": accepted["request_id"], "hire_id": 42, "offset": 0,
                             "total": chunks[0]["total"], "data": chunks[0]["data"]})) <= 4096


def test_hire_registration_is_idempotent_and_guards_conflicts(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + (b"\x66" * 20).hex())))


def test_capability_advertises_handlers_and_poll(actor, host):
    cap = codec.decode(actor.capability(None))
    assert cap["handlers"] == ["create_inbox", "send", "reply", "list_messages", "get_message", "search"]
    assert cap["poll_handler"] == "get_result" and cap["slot"] == "email-inbox"
    assert cap["hire_activation"]["protocol"] == "store-admin-v1"


def test_get_last_output_is_metadata_only(actor, host):
    # The observability getter must never return the vendor data (fetched email
    # content) — not even to an arbitrary caller.
    a, _, _ = run_full(actor, host, "get_message", MSG_OK, inbox_id=INBOX, message_id="m1")
    assert read(actor, host, a["request_id"])["output"]["data"]["text"] == "the body"
    host.set_context(sender=b"\x77" * 20)  # any caller, not even a registered hire
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert meta["operation"] == "get_message" and meta["ok"] is True
    assert "the body" not in codec.encode(meta).decode("latin1")


def test_inbox_id_rejects_traversal_and_non_email(actor, host):
    host.set_context(sender=BUYER)
    for bad in ("..", ".", "no-at-sign", "a@b", "a@.com", "a@com.", "x..y@a.com", "@a.com", "a@@b.com"):
        r = codec.decode(actor.list_messages(codec.encode({"hire_id": 42, "inbox_id": bad})))
        assert r["ok"] is False, bad
    assert not host.get_send_log()
    # a real address still dispatches
    ok, job = submit(actor, host, "list_messages", inbox_id="valid@agentmail.to")
    assert ok["ok"] is True and job is not None


def test_cc_bcc_reply_to_are_validated(actor, host):
    host.set_context(sender=BUYER)
    base = {"hire_id": 42, "inbox_id": INBOX, "to": "d@x.com", "subject": "s", "text": "b"}
    bad = [
        dict(base, cc=["a@x.com"] * (am.MAX_RECIPIENTS + 1)),   # too many cc
        dict(base, bcc=[123]),                                   # non-string bcc entry
        dict(base, reply_to="z" * (am.MAX_ADDR_BYTES + 1)),      # oversize reply_to
        dict(base, cc="x" * (am.MAX_ADDR_BYTES + 1)),            # oversize scalar cc
    ]
    for payload in bad:
        assert codec.decode(actor.send(codec.encode(payload)))["ok"] is False, payload
    assert not host.get_send_log()
    ok, job = submit(actor, host, "send", inbox_id=INBOX, to="d@x.com", subject="s",
                     text="b", cc=["c@x.com"], bcc=["h@x.com"], reply_to="r@x.com")
    assert ok["ok"] is True and json.loads(kwargs_of(job)["raw_body"])["cc"] == ["c@x.com"]


def test_request_body_is_capped(actor, host):
    host.set_context(sender=BUYER)
    huge = "z" * am.MAX_TEXT_BYTES  # each field is in-range, but together they exceed the body cap
    r = codec.decode(actor.send(codec.encode(
        {"hire_id": 42, "inbox_id": INBOX, "to": "d@x.com", "subject": "s", "text": huge, "html": huge})))
    assert r["ok"] is False and "body budget" in r["error"]
    assert not host.get_send_log()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
