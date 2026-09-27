"""LlamaParse document-parse actor contract checks."""

import importlib

import pytest
import actors.parse.main as llamaparse
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
DOC_URL = "https://example.com/contract.pdf"
JOB_ID = "job-abc123"
PRESIGNED = (
    "https://llama-parse-results.s3.amazonaws.com/r/abc.md"
    "?X-Amz-Signature=deadbeef&X-Amz-Expires=3600"
)
PARSE_BASE = {"hire_id": 42, "document_url": DOC_URL}


def submit_reply(job_id=JOB_ID, status="PENDING"):
    return {"id": job_id, "status": status}


def fetch_reply(status="SUCCESS", url=PRESIGNED, size=1234, nested=True):
    job = {"id": JOB_ID, "status": status}
    if url is not None:
        job["result_content_metadata"] = {
            "md": {"presigned_url": url, "size_bytes": size, "exists": True}
        }
    return {"job": job} if nested else job


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, sender=BUYER, **payload):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    fields = dict(PARSE_BASE)
    fields.update(payload)
    accepted = codec.decode(actor.parse(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = llamaparse._run_request__resume(
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


def run_parse(actor, host, result=None, sender=BUYER, **payload):
    accepted, job = submit(actor, host, sender=sender, **payload)
    resume(host, job, submit_reply() if result is None else result)
    return accepted, job


def fetch(actor, host, submit_id, sender=BUYER, hire_id=42):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    accepted = codec.decode(
        actor.fetch_result(
            codec.encode({"hire_id": hire_id, "request_id": submit_id})
        )
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def run_fetch(actor, host, submit_id, result=None, sender=BUYER, hire_id=42):
    accepted, job = fetch(actor, host, submit_id, sender=sender, hire_id=hire_id)
    final = resume(host, job, fetch_reply() if result is None else result)
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


def test_parse_dispatch_is_exact_and_stores_job_id(actor, host):
    accepted, job = submit(actor, host)
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "parse",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == llamaparse.PARSE_ENDPOINT
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer ${LLAMAPARSE_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": llamaparse.USER_AGENT,
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), llamaparse.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    import json

    assert json.loads(kwargs["raw_body"]) == {
        "source_url": DOC_URL,
        "tier": llamaparse.PARSE_TIER,
        "version": llamaparse.PARSE_VERSION,
    }
    resume(host, job, submit_reply())
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    data = out["output"]["data"]
    assert data["job_id"] == JOB_ID
    assert data["vendor_status"] == "PENDING"


def test_fetch_result_dispatch_is_exact_and_returns_presigned_url(actor, host):
    run_parse(actor, host)
    accepted, final, job = run_fetch(actor, host, 1)
    assert accepted["ok"] is True and accepted["operation"] == "fetch_result"
    kwargs = kwargs_of(job)
    assert kwargs["method"] == "GET"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["headers"]["Authorization"] == "Bearer ${LLAMAPARSE_API_KEY}"
    assert kwargs["headers"]["User-Agent"] == llamaparse.USER_AGENT
    assert kwargs["url"] == (
        llamaparse.RESULT_ENDPOINT_PREFIX + JOB_ID + "?expand=" + llamaparse.RESULT_EXPAND
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    data = out["output"]["data"]
    assert data["ready"] is True
    assert data["result_type"] == "markdown"
    assert data["result_url"] == PRESIGNED
    assert data["size_bytes"] == 1234


def test_fetch_result_pending_job_is_not_ready(actor, host):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(actor, host, 1, result=fetch_reply(status="PENDING", url=None))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"] == {"ready": False, "vendor_status": "PENDING"}
    assert "result_url" not in out["output"]["data"]


def test_fetch_result_reads_top_level_result_metadata(actor, host):
    # Live shape (verified 2026-09-15): `status` is under `job` but
    # result_content_metadata is a TOP-LEVEL sibling (key `markdown`). The
    # wrapper must extract the presigned URL from the top level, not from `job`.
    run_parse(actor, host)
    reply = {
        "job": {"status": "COMPLETED"},
        "result_content_metadata": {
            "markdown": {"presigned_url": PRESIGNED, "size_bytes": 2929, "exists": True}
        },
    }
    accepted, _, _ = run_fetch(actor, host, 1, result=reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    data = out["output"]["data"]
    assert data["ready"] is True and data["result_url"] == PRESIGNED and data["size_bytes"] == 2929


@pytest.mark.parametrize("status", ["ERROR", "FAILED", "CANCELLED"])
def test_fetch_result_failed_job_is_error(actor, host, status):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(
        actor, host, 1, result=fetch_reply(status=status, url=None)
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]


def test_fetch_result_flat_response_shape_also_works(actor, host):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(
        actor, host, 1, result=fetch_reply(nested=False)
    )
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["data"]["result_url"] == PRESIGNED


def test_fetch_result_rejects_result_url_on_key_auth_host(actor, host):
    run_parse(actor, host)
    bad = "https://api.cloud.llamaindex.ai/api/v2/parse/%s/result/markdown" % JOB_ID
    accepted, _, _ = run_fetch(actor, host, 1, result=fetch_reply(url=bad))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]


def test_valid_result_url_enforces_host_tree_and_presigned_marker():
    for good in (
        PRESIGNED,  # llama-parse-results.s3.amazonaws.com ?X-Amz-Signature=
        "https://x.s3.amazonaws.com/r/abc.md?sig=y",
        "https://storage.googleapis.com/b/o?X-Goog-Signature=abc",
        # old-style S3 query auth — LlamaParse's real presigned shape (verified live 2026-09-15)
        "https://llama-platform-file-parsing.s3.amazonaws.com/p/c/doc.md"
        "?AWSAccessKeyId=AKIA&Signature=abc%2Bdef&Expires=1700000000&x-amz-security-token=tok",
    ):
        assert llamaparse._valid_result_url(good), good
    for bad in (
        # key-auth host tree — bare parent, subdomain, and trailing-dot FQDN
        "https://api.cloud.llamaindex.ai/api/v2/parse/x/result/markdown?sig=z",
        "https://cloud.llamaindex.ai/r?sig=z",
        "https://llamaindex.ai/r?sig=z",
        "https://api.cloud.llamaindex.ai./r?sig=z",  # trailing dot dodge
        "https://evil.com@api.cloud.llamaindex.ai/r?sig=z",  # userinfo -> real host is key-auth
        # missing a presigned signature marker
        "https://x.s3.amazonaws.com/r/abc.md",
        "https://cdn.example.com/plain.md",
        # scheme / length / template
        "http://x.s3.amazonaws.com/r?sig=z",
        "https://x/${LLAMAPARSE_API_KEY}?sig=z",
        "https://x/" + "a" * llamaparse.MAX_RESULT_URL_CHARS + "?sig=z",
        "",
        None,
        7,
    ):
        assert not llamaparse._valid_result_url(bad), bad


def test_fetch_result_requires_completed_parse(actor, host):
    # A parse that has not resumed yet has no job id; fetch_result refuses it.
    submit(actor, host)
    out = codec.decode(
        actor.fetch_result(codec.encode({"hire_id": 42, "request_id": 1}))
    )
    assert out["ok"] is False
    assert len(host.get_send_log()) == 1  # only the parse job; no fetch dispatched


def test_fetch_result_cross_hire_is_refused(actor, host):
    # hire 42 (BUYER) submits; hire 43 (ATTACKER) cannot fetch its job.
    run_parse(actor, host)
    register(actor, host, 43, ATTACKER)
    host.set_context(sender=ATTACKER, tx_hash=(50).to_bytes(32, "big"))
    refused = codec.decode(
        actor.fetch_result(codec.encode({"hire_id": 43, "request_id": 1}))
    )
    assert refused["ok"] is False
    # And the attacker cannot claim it under the real hire either.
    host.set_context(sender=ATTACKER, tx_hash=(51).to_bytes(32, "big"))
    with pytest.raises(ValueError):
        actor.fetch_result(codec.encode({"hire_id": 42, "request_id": 1}))


@pytest.mark.parametrize(
    "scheme_url",
    ["https://example.com/a.pdf", "http://example.com/a.pdf"],
)
def test_supported_document_url_schemes_dispatch(actor, host, scheme_url):
    import json

    accepted, job = submit(actor, host, document_url=scheme_url)
    assert accepted["ok"] is True
    assert json.loads(kwargs_of(job)["raw_body"])["source_url"] == scheme_url


@pytest.mark.parametrize(
    "payload",
    [
        {"document_url": ""},
        {"document_url": "   "},
        {"document_url": 7},
        {"document_url": None},
        {"document_url": "not-a-url"},
        {"document_url": "ftp://example.com/a.pdf"},
        {"document_url": "s3://bucket/key"},
        {"document_url": "https://example.com/a b.pdf"},
        {"document_url": "https://example.com/a\nb.pdf"},
        {"document_url": "https://example.com/" + "a" * 3_000},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(PARSE_BASE)
    fields.update(payload)
    out = codec.decode(actor.parse(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_missing_document_url_never_dispatches(actor, host):
    no_url = {k: v for k, v in PARSE_BASE.items() if k != "document_url"}
    assert codec.decode(actor.parse(codec.encode(no_url)))["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    for payload in (
        {"document_url": "https://example.com/${LLAMAPARSE_API_KEY}.pdf"},
        {"document_url": "https://${LLAMAPARSE_API_KEY}.example.com/a.pdf"},
    ):
        fields = dict(PARSE_BASE)
        fields.update(payload)
        out = codec.decode(actor.parse(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    run_parse(actor, host)
    assert read(actor, host, 1, sender=ATTACKER)["ok"] is False
    assert read(actor, host, 1, hire_id=43)["ok"] is False
    assert read(actor, host, 1)["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.parse(codec.encode(dict(PARSE_BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.parse(codec.encode(dict(PARSE_BASE, hire_id=42)))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"detail": "Invalid API key"}, "Invalid API key"),
        (
            {"detail": [{"loc": ["body", "source_url"], "msg": "field required", "type": "missing"}]},
            "field required",
        ),
        ({"error": "document could not be downloaded"}, "document could not be downloaded"),
        ({"message": "rate limit exceeded"}, "rate limit exceeded"),
        ("<html>502 Bad Gateway</html>", "502 Bad Gateway"),
    ],
)
def test_submit_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _ = submit(actor, host)
    resume(host, _last_parse_job(host), vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]
    assert "data" not in out["output"]


def _last_parse_job(host):
    return last_job(host)


@pytest.mark.parametrize(
    "vendor",
    [
        {},
        {"status": "PENDING"},
        {"id": "", "status": "PENDING"},
        {"id": 7, "status": "PENDING"},
        {"id": "bad id with spaces", "status": "PENDING"},
        "<html>not json</html>",
    ],
)
def test_malformed_submit_shapes_are_errors(actor, host, vendor):
    accepted, job = submit(actor, host)
    resume(host, job, vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]


@pytest.mark.parametrize(
    "vendor",
    [
        {"job": {"status": "SUCCESS"}},
        {"job": {"status": "SUCCESS", "result_content_metadata": {}}},
        {"job": {"status": "SUCCESS", "result_content_metadata": {"md": {"presigned_url": ""}}}},
        {"job": {"status": "SUCCESS", "result_content_metadata": {"md": {"presigned_url": 7}}}},
        {"job": {"status": "SUCCESS", "result_content_metadata": {"md": {"presigned_url": "http://x/no-tls"}}}},
        {"job": {"status": "WEIRD"}},
    ],
)
def test_malformed_fetch_success_shapes_are_errors(actor, host, vendor):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(actor, host, 1, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]


def test_oversized_vendor_error_is_bounded(actor, host):
    accepted, job = submit(actor, host)
    resume(host, job, {"detail": "z" * 100_000})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert len(out["output"]["error"].encode("utf-8")) <= llamaparse.MAX_ERROR_CHARS
    assert len(codec.encode(out)) < llamaparse.RETURN_BYTE_BUDGET


def test_encoded_result_is_bounded(actor, host):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(actor, host, 1)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert len(codec.encode(out)) < llamaparse.RETURN_BYTE_BUDGET


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, job = submit(actor, host)
    resume(host, job, submit_reply())
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, submit_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(llamaparse.LlamaParseWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(llamaparse.LlamaParseWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host)
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, submit_reply(), sender=forged)


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
    assert kwargs["headers"]["Authorization"] == "Bearer ${LLAMAPARSE_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), llamaparse.SECRET_KEY]]
    assert "${LLAMAPARSE_API_KEY}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(llamaparse.MAX_RETAINED_PER_HIRE):
        accepted, _ = submit(actor, host)
        assert accepted["ok"] is True
    full, _ = submit(actor, host)
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
    while accepted_count < llamaparse.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit(actor, host, sender=sender, hire_id=hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == llamaparse.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_fetch_remains_reachable_at_both_capacity_limits(actor, host):
    sources = []
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20)):
        register(actor, host, hire_id, sender); senders[hire_id] = sender
    for index in range(llamaparse.MAX_RETAINED_REQUESTS):
        hire_id = (42, 43, 44)[index % 3]; sender = senders[hire_id]
        accepted, job = submit(actor, host, sender=sender, hire_id=hire_id)
        resume(host, job, submit_reply(job_id="job-%d" % index))
        sources.append((accepted["request_id"], hire_id, sender))
    source_id, hire_id, sender = sources[0]
    accepted, job = fetch(actor, host, source_id, sender=sender, hire_id=hire_id)
    assert accepted["ok"] is True and job is not None
    assert len(actor.storage[llamaparse.KEY_REQUEST_IDS]) == 17
    expiry = actor.storage["job:" + str(source_id)]["expires_height"]
    host.set_block_height(expiry)
    replacement, replacement_job = submit(
        actor, host, sender=sender, hire_id=hire_id,
        document_url="https://example.com/replacement.pdf",
    )
    assert replacement["ok"] is True and replacement_job is not None


def test_running_fetch_retries_after_reload_and_source_cleanup(actor, host):
    parsed, _ = run_parse(actor, host)
    source_expiry = read(actor, host, parsed["request_id"])["expires_height"]
    host.set_block_height(source_expiry - llamaparse.RESULT_RETENTION_BLOCKS + 1)
    fetched, _, _ = run_fetch(actor, host, parsed["request_id"], fetch_reply(status="RUNNING", url=None))
    fetch_id = fetched["request_id"]
    host.set_block_height(source_expiry)
    assert read(actor, host, parsed["request_id"])["ok"] is False
    reloaded = importlib.reload(llamaparse); restored = reloaded.LlamaParseWrapper()
    accepted, job = fetch(restored, host, parsed["request_id"]); assert accepted["request_id"] == fetch_id
    resume(host, job, fetch_reply(status="RUNNING", url=None))
    accepted, job = fetch(restored, host, parsed["request_id"]); assert accepted["request_id"] == fetch_id
    resume(host, job, fetch_reply())
    assert read(restored, host, fetch_id)["output"]["data"]["result_url"] == PRESIGNED


def test_fetch_error_and_timeout_remain_retryable(actor, host):
    parsed, _ = run_parse(actor, host)
    fetched, _, _ = run_fetch(actor, host, parsed["request_id"], {"error": "temporary"})
    accepted, job = fetch(actor, host, parsed["request_id"]); assert accepted["request_id"] == fetched["request_id"]
    deadline = read(actor, host, fetched["request_id"])["deadline_height"]
    host.set_block_height(deadline); assert read(actor, host, fetched["request_id"])["status"] == "error"
    accepted, job = fetch(actor, host, parsed["request_id"]); assert accepted["request_id"] == fetched["request_id"]
    resume(host, job, fetch_reply())
    assert read(actor, host, fetched["request_id"])["output"]["data"]["ready"] is True


def test_deadline_expiry_is_terminal_and_late_result_is_stale(actor, host):
    accepted, job = submit(actor, host)
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, submit_reply())
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    payload = codec.encode(PARSE_BASE)
    first = codec.decode(actor.parse(payload))
    duplicate = codec.decode(actor.parse(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, document_url="https://example.com/first.pdf")
    second, second_job = submit(actor, host, document_url="https://example.com/second.pdf")
    resume(host, second_job, submit_reply(job_id="job-second"))
    resume(host, first_job, submit_reply(job_id="job-first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["job_id"] == "job-first"
    assert read(actor, host, second["request_id"])["output"]["data"]["job_id"] == "job-second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    run_parse(actor, host)
    accepted, _, _ = run_fetch(actor, host, 1)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= llamaparse.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    run_parse(actor, host)
    run_fetch(actor, host, 1, result=fetch_reply(url=PRESIGNED, size=999))
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    encoded = codec.encode(meta).decode("latin1")
    assert PRESIGNED not in encoded
    assert "s3.amazonaws.com" not in encoded
    assert JOB_ID not in encoded


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


def test_register_hire_requires_registrar(actor, host):
    # register_hire is the payment/authorization boundary: only the Store
    # registrar set at init may register a hire. A non-registrar caller must be
    # refused, and must leave no state behind (mirrors the reference wrappers).
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError, match="registrar"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=99)))
    host.set_context(sender=OWNER)
    ok = codec.decode(actor.register_hire(codec.encode(dict(HIRE, hire_id=99))))
    assert ok["ok"] is True and ok["hire_id"] == 99


def test_init_is_one_time(actor, host):
    # The actor fixture already initialized once; a second init must be refused
    # so the registrar/owner cannot be silently reset.
    host.set_context(sender=OWNER)
    with pytest.raises(ValueError, match="initialized"):
        actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "llamaparse"
    assert cap["handlers"] == ["parse", "fetch_result"]
    assert cap["poll_handler"] == "get_result"
    assert cap["slot"] == "document-parse"
    assert cap["mutating"] is True
    assert cap["result_byte_budget"] == llamaparse.RETURN_BYTE_BUDGET
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            llamaparse._bounded_int(bad, "request_id", 1, 1000)
    assert llamaparse._bounded_int(7, "request_id", 1, 1000) == 7
    assert llamaparse._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join("tests", "test_llamaparse_parity.py")
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    shutil.copytree(here, work, ignore=shutil.ignore_patterns(".offline", ".venv", "__pycache__"))
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
