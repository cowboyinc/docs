"""Postmark email-send actor contract checks."""

import json

import pytest
import actors.postmark.main as postmark
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
BASE = {
    "hire_id": 42,
    "From": "Cowboy <ops@cowboy.example>",
    "To": "buyer@example.com",
    "Subject": "Store receipt",
    "TextBody": "Your order shipped.",
}


def vendor_reply(message_id="0a129aee-e1cd-480d-b08d-4f48548ff48d", **extra):
    """A canonical Postmark success body: ErrorCode 0 with a MessageID."""
    reply = {
        "To": "buyer@example.com",
        "SubmittedAt": "2026-09-12T00:00:00.0000000Z",
        "MessageID": message_id,
        "ErrorCode": 0,
        "Message": "OK",
    }
    reply.update(extra)
    return reply


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, **payload):
    host.set_context(
        sender=BUYER,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    fields = dict(BASE)
    fields.update(payload)
    accepted = codec.decode(actor.send(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = postmark._run_request__resume(
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


def submit_as(actor, host, sender, hire_id, **payload):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    fields = dict(BASE, hire_id=hire_id)
    fields.update(payload)
    accepted = codec.decode(actor.send(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def stored_last_output(host):
    """Read the raw KEY_LAST_OUTPUT value straight out of host storage.

    Storage keys are UTF-8 encoded verbatim (no hashing/prefix), so the actor's
    ``self.storage["last_output"]`` lands under ``b"last_output"`` and its value
    is the CBOR the actor wrote.
    """
    raw = host._storage.get(postmark.KEY_LAST_OUTPUT.encode("utf-8"))
    return None if raw is None else codec.decode(raw)


def test_send_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(
        actor,
        host,
        To=["buyer@example.com", "cc-me@example.com"],
        Subject="Store receipt",
        TextBody="Your order shipped.",
        HtmlBody="<p>Your order shipped.</p>",
        Cc="team@example.com",
        Bcc=["audit@example.com"],
        ReplyTo="support@cowboy.example",
        MessageStream="outbound",
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "send",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == postmark.SEND_ENDPOINT
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    # Postmark authenticates with a bare token header, NOT Bearer.
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "X-Postmark-Server-Token": "${POSTMARK_SERVER_TOKEN}",
        "Content-Type": "application/json",
        "Idempotency-Key": postmark.IDEMPOTENCY_PREFIX + job["cid"].hex(),
        "User-Agent": postmark.USER_AGENT,
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), postmark.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    # Postmark joins each recipient field into a single comma-separated string.
    assert json.loads(kwargs["raw_body"]) == {
        "From": "Cowboy <ops@cowboy.example>",
        "To": "buyer@example.com,cc-me@example.com",
        "Subject": "Store receipt",
        "TextBody": "Your order shipped.",
        "HtmlBody": "<p>Your order shipped.</p>",
        "Cc": "team@example.com",
        "Bcc": "audit@example.com",
        "ReplyTo": "support@cowboy.example",
        "MessageStream": "outbound",
    }
    resume(host, job, vendor_reply())
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == {
        "MessageID": "0a129aee-e1cd-480d-b08d-4f48548ff48d",
        "ErrorCode": 0,
        "SubmittedAt": "2026-09-12T00:00:00.0000000Z",
        "To": "buyer@example.com",
        "Message": "OK",
    }


def test_minimal_body_joins_recipients_into_string(actor, host):
    _, job = submit(actor, host)
    body = json.loads(kwargs_of(job)["raw_body"])
    assert body == {
        "From": "Cowboy <ops@cowboy.example>",
        "To": "buyer@example.com",
        "Subject": "Store receipt",
        "TextBody": "Your order shipped.",
    }


def test_idempotency_key_is_stable_and_unique_per_request(actor, host):
    _, first = submit(actor, host)
    _, second = submit(actor, host, Subject="second receipt")
    first_key = kwargs_of(first)["headers"]["Idempotency-Key"]
    second_key = kwargs_of(second)["headers"]["Idempotency-Key"]
    assert first_key == postmark.IDEMPOTENCY_PREFIX + first["cid"].hex()
    assert first_key != second_key
    assert len(first_key) <= 256


@pytest.mark.parametrize(
    "payload",
    [
        {"From": ""},
        {"From": "not-an-email"},
        {"From": "ops@cowboy.example\r\nBcc: sneaky@evil.com"},
        {"From": 7},
        {"To": ""},
        {"To": []},
        {"To": 7},
        {"To": "no-at-sign"},
        {"To": "two@@ex.com"},
        {"To": ["ok@example.com"] * 51},
        {"Cc": ["ok@example.com"] * 51},
        {"Bcc": "bad-address"},
        {"ReplyTo": "also bad"},
        {"Subject": ""},
        {"Subject": "x" * (postmark.MAX_SUBJECT_CHARS + 1)},
        {"Subject": "Hi\nInjected: value"},
        {"Subject": 5},
        {"HtmlBody": 7},
        {"TextBody": 7},
        {"MessageStream": "has space"},
        {"MessageStream": ""},
        {"MessageStream": 7},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(BASE)
    fields.update(payload)
    out = codec.decode(actor.send(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_missing_body_and_over_budget_never_dispatch(actor, host):
    no_body = {k: v for k, v in BASE.items() if k != "TextBody"}
    assert codec.decode(actor.send(codec.encode(no_body)))["ok"] is False
    over = dict(BASE, TextBody="x" * (postmark.MAX_REQUEST_BYTES + 10))
    assert codec.decode(actor.send(codec.encode(over)))["ok"] is False
    total = dict(
        BASE,
        To=["a%d@example.com" % i for i in range(50)],
        Cc="one-too-many@example.com",
    )
    assert codec.decode(actor.send(codec.encode(total)))["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    for payload in (
        {"Subject": "leak ${POSTMARK_SERVER_TOKEN}"},
        {"TextBody": "the token is ${POSTMARK_SERVER_TOKEN}"},
        {"HtmlBody": "<b>${POSTMARK_SERVER_TOKEN}</b>"},
        {"To": "${POSTMARK_SERVER_TOKEN}@example.com"},
        {"ReplyTo": ["ok@example.com", "${SECRET}@x.com"]},
        {"MessageStream": "${SECRET}"},
    ):
        fields = dict(BASE)
        fields.update(payload)
        out = codec.decode(actor.send(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.send(codec.encode(dict(BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.send(codec.encode(dict(BASE, hire_id=42)))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        # Postmark signals failure as a 200/422 body with a nonzero ErrorCode.
        ({"ErrorCode": 300, "Message": "Invalid 'To' address."}, "ErrorCode 300"),
        (
            {
                "ErrorCode": 406,
                "Message": "You tried to send to recipients that have been marked as inactive.",
            },
            "inactive",
        ),
        ({"ErrorCode": 10, "Message": "No Account or Server API tokens were supplied."}, "ErrorCode 10"),
        ({"ErrorCode": 401, "Message": "Bad or missing Server token."}, "Bad or missing Server token"),
        # A MessageID present alongside a nonzero ErrorCode is STILL a failure.
        (
            {"ErrorCode": 405, "Message": "Not allowed to send.", "MessageID": "should-be-ignored"},
            "Not allowed to send",
        ),
        ({"error": "forbidden"}, "forbidden"),
        ("<html>502 Bad Gateway</html>", "502 Bad Gateway"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]
    assert "data" not in out["output"]


def test_200_with_nonzero_error_code_is_a_failure(actor, host):
    """Postmark's failure signal is a nonzero ``ErrorCode`` in an otherwise 200
    body — not the HTTP status. Even with a ``MessageID`` present, that is a
    FAILURE and must never be surfaced as vendor data (Postmark-specific gotcha).
    """
    accepted, _, _ = run_full(
        actor,
        host,
        result={"ErrorCode": 406, "Message": "Inactive recipient", "MessageID": "present-but-failed"},
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]
    assert "406" in out["output"]["error"]
    assert "present-but-failed" not in codec.encode(out).decode("latin1")


@pytest.mark.parametrize(
    "vendor",
    [
        {},
        {"ErrorCode": 0},
        {"ErrorCode": 0, "MessageID": ""},
        {"ErrorCode": 0, "MessageID": 7},
        {"ErrorCode": 0, "MessageID": None},
        {"MessageID": "abc"},
        {"ErrorCode": "0", "MessageID": "abc"},
        {"ErrorCode": False, "MessageID": "abc"},
        {"ErrorCode": 0, "MessageID": "x" * (postmark.MAX_ID_CHARS + 1)},
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, vendor):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_oversized_vendor_error_is_bounded(actor, host):
    accepted, _, _ = run_full(
        actor, host, result={"ErrorCode": 300, "Message": "z" * 100_000}
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert len(out["output"]["error"].encode("utf-8")) <= postmark.MAX_ERROR_CHARS
    assert len(codec.encode(out)) < postmark.RETURN_BYTE_BUDGET


def test_oversized_success_reply_is_bounded_to_error(actor, host):
    # A success reply (ErrorCode 0 + MessageID) whose copied string fields are
    # huge trips the encoded result-byte budget in _finish and is converted to a
    # graceful error, never a truncated false success.
    accepted, _, _ = run_full(
        actor,
        host,
        result=vendor_reply(To="x" * 20_000, SubmittedAt="y" * 20_000, Message="z" * 20_000),
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "budget" in out["output"]["error"]
    assert len(codec.encode(out)) < postmark.RETURN_BYTE_BUDGET


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(postmark.PostmarkEmailWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(postmark.PostmarkEmailWrapper, "on_runner_result")


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
    assert kwargs["headers"]["X-Postmark-Server-Token"] == "${POSTMARK_SERVER_TOKEN}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), postmark.SECRET_KEY]]
    assert "${POSTMARK_SERVER_TOKEN}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(postmark.MAX_RETAINED_PER_HIRE):
        accepted, _ = submit_as(actor, host, BUYER, 42)
        assert accepted["ok"] is True
    full, _ = submit_as(actor, host, BUYER, 42)
    assert full["ok"] is False and "retained request capacity" in full["error"]
    register(actor, host, 43, ATTACKER)
    other, job = submit_as(actor, host, ATTACKER, 43)
    assert other["ok"] is True and job is not None


def test_global_request_capacity_is_bounded(actor, host):
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20), (45, b"\x66" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    accepted_count = 0
    refused = None
    while accepted_count < postmark.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == postmark.MAX_RETAINED_REQUESTS
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
    first = codec.decode(actor.send(payload))
    duplicate = codec.decode(actor.send(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, Subject="first")
    second, second_job = submit(actor, host, Subject="second")
    resume(host, second_job, vendor_reply("id-second"))
    resume(host, first_job, vendor_reply("id-first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["MessageID"] == "id-first"
    assert read(actor, host, second["request_id"])["output"]["data"]["MessageID"] == "id-second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    accepted, _, _ = run_full(actor, host)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= postmark.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_last_output_storage_is_metadata_only_after_finish_and_refuse(actor, host):
    """M2: the never-expiring public ``last_output`` key must hold ONLY metadata
    (``ok`` + ``operation``), never vendor data. Asserted against the raw stored
    dict in the mock host, not just the getter's projection."""
    # After a finish carrying a private MessageID in the vendor reply.
    accepted, _, _ = run_full(actor, host, result=vendor_reply("mid-PRIVATE"))
    assert read(actor, host, accepted["request_id"])["output"]["data"]["MessageID"] == "mid-PRIVATE"
    stored = stored_last_output(host)
    assert stored == {"ok": True, "operation": "send"}
    assert set(stored) == {"ok", "operation"}
    assert "data" not in stored and "MessageID" not in stored
    # The private MessageID must not appear anywhere in the raw stored bytes.
    assert b"mid-PRIVATE" not in host._storage[postmark.KEY_LAST_OUTPUT.encode("utf-8")]

    # After a refuse (invalid input): still metadata-only, no vendor payload/error.
    host.set_context(sender=BUYER)
    refused = codec.decode(actor.send(codec.encode(dict(BASE, Subject=""))))
    assert refused["ok"] is False
    stored = stored_last_output(host)
    assert stored == {"ok": False, "operation": "send"}
    assert set(stored) == {"ok", "operation"}
    assert "data" not in stored and "error" not in stored


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(actor, host, result=vendor_reply("mid-PRIVATE"))
    assert read(actor, host, accepted["request_id"])["output"]["data"]["MessageID"] == "mid-PRIVATE"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert "mid-PRIVATE" not in codec.encode(meta).decode("latin1")


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
    assert cap["vendor"] == "postmark"
    assert cap["handlers"] == ["send"]
    assert cap["poll_handler"] == "get_result"
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert cap["mutating"] is True
    assert cap["max_recipients"] == 50
    assert cap["max_subject_chars"] == 2_000
    assert set(cap["input"]) == set(postmark.SUPPORTED_INPUT_FIELDS)


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            postmark._bounded_int(bad, "request_id", 1, 1000)
    assert postmark._bounded_int(7, "request_id", 1, 1000) == 7
    assert postmark._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.dirname(here)
    script = "test_postmark_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    shutil.copytree(
        project,
        work,
        ignore=shutil.ignore_patterns(".offline", "__pycache__", ".pytest_cache"),
    )
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
    bad = subprocess.run([sys.executable, os.path.join(str(work), "tests", script)],
                         cwd=str(work), capture_output=True, text=True)
    assert bad.returncode != 0
