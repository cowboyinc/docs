"""Hyperbrowser scrape actor contract checks."""

import importlib
import json

import pytest
import actors.scrape.main as hyperbrowser
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
URL = "https://example.com/article"
JOB_ID = "job-abc123"
BASE = {"hire_id": 42, "url": URL}


def completed_reply(markdown="# Title\nbody", links=None, metadata=None, job_id=JOB_ID):
    return {
        "jobId": job_id,
        "status": "completed",
        "data": {
            "metadata": metadata if metadata is not None else {"title": "Example"},
            "markdown": markdown,
            "links": links if links is not None else ["https://example.com/a"],
        },
    }


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def scrape_submit(actor, host, sender=BUYER, hire_id=42, **payload):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    fields = dict(BASE, hire_id=hire_id)
    fields.update(payload)
    accepted = codec.decode(actor.scrape(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = hyperbrowser._run_request__resume(
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


def run_scrape(actor, host, job_id=JOB_ID, **payload):
    accepted, job = scrape_submit(actor, host, **payload)
    if job is not None:
        resume(host, job, {"jobId": job_id})
    return accepted, job


def fetch_call(actor, host, source_request_id, sender=BUYER, hire_id=42):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    accepted = codec.decode(
        actor.fetch(
            codec.encode({"hire_id": hire_id, "source_request_id": source_request_id})
        )
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def run_fetch(actor, host, source_request_id, vendor_result, sender=BUYER, hire_id=42):
    accepted, job = fetch_call(actor, host, source_request_id, sender=sender, hire_id=hire_id)
    if job is None:
        return accepted, None, None
    final = resume(host, job, vendor_result)
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


# --- scrape submit -----------------------------------------------------------


def test_scrape_dispatch_is_exact_and_uses_raw_key_header(actor, host):
    accepted, job = scrape_submit(actor, host)
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "scrape",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == hyperbrowser.SCRAPE_ENDPOINT
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "x-api-key": "${HYPERBROWSER_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": hyperbrowser.USER_AGENT,
    }
    # Raw api-key, NOT Bearer.
    assert kwargs["headers"]["x-api-key"] == "${HYPERBROWSER_API_KEY}"
    assert "Authorization" not in kwargs["headers"]
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), hyperbrowser.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    # No sessionOptions (no stealth/proxy/captcha); onlyMainContent forced true.
    assert json.loads(kwargs["raw_body"]) == {
        "url": URL,
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }
    resume(host, job, {"jobId": JOB_ID})
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == {"job_id": JOB_ID, "job_status": "submitted"}


def test_no_stealth_or_session_options_are_ever_sent(actor, host):
    _, job = scrape_submit(actor, host)
    body = json.loads(kwargs_of(job)["raw_body"])
    assert "sessionOptions" not in body
    text = json.dumps(body)
    for banned in ("useStealth", "useProxy", "solveCaptchas", "proxyCountry", "adblock"):
        assert banned not in text


@pytest.mark.parametrize(
    "formats,expected",
    [
        (None, ["markdown"]),
        (["links"], ["links"]),
        (["markdown", "links"], ["links", "markdown"]),
        (["links", "markdown", "links"], ["links", "markdown"]),
    ],
)
def test_formats_are_validated_and_ordered(actor, host, formats, expected):
    payload = {} if formats is None else {"formats": formats}
    _, job = scrape_submit(actor, host, **payload)
    assert json.loads(kwargs_of(job)["raw_body"])["scrapeOptions"]["formats"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "   "},
        {"url": 7},
        {"url": None},
        {"url": "not-a-url"},
        {"url": "ftp://example.com/a"},
        {"url": "https://example.com/a b"},
        {"url": "https://example.com/a\nb"},
        {"url": "https://example.com/" + "a" * 3_000},
        {"formats": ["html"]},
        {"formats": ["screenshot"]},
        {"formats": []},
        {"formats": "markdown"},
        {"unexpected": "value"},
    ],
)
def test_invalid_scrape_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(BASE)
    fields.update(payload)
    host.set_context(sender=BUYER)
    out = codec.decode(actor.scrape(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_missing_url_never_dispatches(actor, host):
    host.set_context(sender=BUYER)
    no_url = {k: v for k, v in BASE.items() if k != "url"}
    assert codec.decode(actor.scrape(codec.encode(no_url)))["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    for payload in (
        {"url": "https://example.com/${HYPERBROWSER_API_KEY}"},
        {"url": "https://${HYPERBROWSER_API_KEY}.example.com/a"},
        {"formats": ["${SECRET}"]},
    ):
        fields = dict(BASE)
        fields.update(payload)
        out = codec.decode(actor.scrape(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


# --- fetch -------------------------------------------------------------------


def test_fetch_dispatch_is_a_bodyless_get(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, job = fetch_call(actor, host, scrape_accepted["request_id"])
    assert accepted["ok"] is True and accepted["operation"] == "fetch"
    kwargs = kwargs_of(job)
    assert kwargs["method"] == "GET"
    assert kwargs["url"] == hyperbrowser.SCRAPE_ENDPOINT + "/" + JOB_ID
    # GET must omit body entirely (E1604) and send no Content-Type.
    assert "body" not in kwargs and "raw_body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "x-api-key": "${HYPERBROWSER_API_KEY}",
        "User-Agent": hyperbrowser.USER_AGENT,
    }
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), hyperbrowser.SECRET_KEY]]


def test_fetch_completed_returns_bounded_content(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(
        actor,
        host,
        scrape_accepted["request_id"],
        completed_reply(markdown="# Hello\nworld", links=["https://example.com/x"]),
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    data = out["output"]["data"]
    assert data["job_status"] == "completed"
    assert data["markdown"] == "# Hello\nworld"
    assert data["links"] == ["https://example.com/x"]
    assert data["metadata"] == {"title": "Example"}


@pytest.mark.parametrize("status", ["pending", "running"])
def test_fetch_incomplete_is_retryable(actor, host, status):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(
        actor, host, scrape_accepted["request_id"], {"jobId": JOB_ID, "status": status}
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"] == {"job_status": status}


@pytest.mark.parametrize(
    "reply,marker",
    [
        ({"jobId": JOB_ID, "status": "failed", "error": "navigation timeout"}, "navigation timeout"),
        ({"jobId": JOB_ID, "status": "stopped"}, "stopped"),
        ({"message": "Invalid API key"}, "Invalid API key"),
        ({"message": "job not found"}, "job not found"),
        ("<html>502 Bad Gateway</html>", "non-JSON"),
    ],
)
def test_fetch_errors_are_never_false_success(actor, host, reply, marker):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(actor, host, scrape_accepted["request_id"], reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]
    assert "data" not in out["output"]


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"jobId": JOB_ID},
        {"jobId": JOB_ID, "status": "completed"},
        {"jobId": JOB_ID, "status": "completed", "data": None},
        {"jobId": JOB_ID, "status": 7},
    ],
)
def test_fetch_malformed_success_shapes_are_errors(actor, host, reply):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(actor, host, scrape_accepted["request_id"], reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "data" not in out["output"]


def test_fetch_rejects_wrong_source_and_cross_hire(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    request_id = scrape_accepted["request_id"]
    # Non-existent source.
    assert fetch_call(actor, host, 9999)[0]["ok"] is False
    # A different hire (registered to the attacker) cannot fetch buyer's scrape.
    register(actor, host, 43, ATTACKER)
    other, other_job = fetch_call(actor, host, request_id, sender=ATTACKER, hire_id=43)
    assert other["ok"] is False and other_job is None
    # The rightful buyer+hire can.
    ok, ok_job = fetch_call(actor, host, request_id)
    assert ok["ok"] is True and ok_job is not None


def test_fetch_requires_a_completed_scrape(actor, host):
    # Submit but do not resume: the scrape is still pending, so fetch refuses.
    accepted, _ = scrape_submit(actor, host)
    out, job = fetch_call(actor, host, accepted["request_id"])
    assert out["ok"] is False and job is None


def test_oversized_fetch_content_is_bounded(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(
        actor,
        host,
        scrape_accepted["request_id"],
        completed_reply(markdown="z" * 200_000, links=[]),
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert len(codec.encode(out)) < hyperbrowser.RETURN_BYTE_BUDGET
    data = out["output"]["data"]
    assert "markdown" not in data
    assert data["_cowboy"]["markdown_omitted"] is True
    assert data["job_status"] == "completed"


def test_oversized_exact_fields_are_an_explicit_error(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(
        actor, host, scrape_accepted["request_id"],
        completed_reply(markdown="small", links=["https://example.com/" + "x" * 50_000]),
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error"
    assert "exact status, metadata, and links" in out["output"]["error"]


def test_oversized_vendor_error_is_bounded(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    accepted, _, _ = run_fetch(
        actor,
        host,
        scrape_accepted["request_id"],
        {"jobId": JOB_ID, "status": "failed", "error": "z" * 100_000},
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert len(out["output"]["error"].encode("utf-8")) <= hyperbrowser.MAX_ERROR_CHARS
    assert len(codec.encode(out)) < hyperbrowser.RETURN_BYTE_BUDGET


# --- gating / lifecycle ------------------------------------------------------


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    rid = scrape_accepted["request_id"]
    assert read(actor, host, rid, sender=ATTACKER)["ok"] is False
    assert read(actor, host, rid, hire_id=43)["ok"] is False
    assert read(actor, host, rid)["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.scrape(codec.encode(dict(BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.scrape(codec.encode(dict(BASE, hire_id=42)))
    assert not host.get_send_log()


def test_duplicate_and_late_callback_rejected(actor, host):
    scrape_accepted, job = run_scrape(actor, host)
    assert read(actor, host, scrape_accepted["request_id"])["status"] == "ok"
    late = resume(host, job, {"jobId": JOB_ID})
    assert late is None or late.get("ok") is False
    assert read(actor, host, scrape_accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(hyperbrowser.HyperbrowserScrapeWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(hyperbrowser.HyperbrowserScrapeWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = scrape_submit(actor, host)
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, {"jobId": JOB_ID}, sender=forged)


def test_continuation_contains_only_request_id(actor, host):
    accepted, job = scrape_submit(actor, host)
    continuations = [
        codec.decode(value)
        for key, value in host._storage.items()
        if key.startswith(b"__continuation:")
    ]
    assert len(continuations) == 1
    assert continuations[0].get("ctx") == {"request_id": accepted["request_id"]}
    kwargs = kwargs_of(job)
    assert kwargs["headers"]["x-api-key"] == "${HYPERBROWSER_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), hyperbrowser.SECRET_KEY]]
    assert "${HYPERBROWSER_API_KEY}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(hyperbrowser.MAX_RETAINED_PER_HIRE):
        accepted, _ = scrape_submit(actor, host, hire_id=42)
        assert accepted["ok"] is True
    full, _ = scrape_submit(actor, host, hire_id=42)
    assert full["ok"] is False and "retained request capacity" in full["error"]
    register(actor, host, 43, ATTACKER)
    other, job = scrape_submit(actor, host, sender=ATTACKER, hire_id=43)
    assert other["ok"] is True and job is not None


def test_global_request_capacity_is_bounded(actor, host):
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20), (45, b"\x66" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    accepted_count = 0
    refused = None
    while accepted_count < hyperbrowser.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = scrape_submit(actor, host, sender=sender, hire_id=hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == hyperbrowser.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_dependent_fetch_is_reachable_at_global_and_per_hire_capacity(actor, host):
    sources = []
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    for index in range(hyperbrowser.MAX_RETAINED_REQUESTS):
        hire_id = (42, 43, 44)[index % 3]
        sender = senders[hire_id]
        accepted, job = scrape_submit(actor, host, sender=sender, hire_id=hire_id)
        assert accepted["ok"] is True
        resume(host, job, {"jobId": "job-%d" % index})
        sources.append((accepted["request_id"], hire_id, sender))
    source_id, hire_id, sender = sources[0]
    accepted, job = fetch_call(actor, host, source_id, sender=sender, hire_id=hire_id)
    assert accepted["ok"] is True and job is not None
    assert len(actor.storage[hyperbrowser.KEY_REQUEST_IDS]) == hyperbrowser.MAX_RETAINED_REQUESTS + 1
    job_expiry = actor.storage["job:" + str(source_id)]["expires_height"]
    host.set_block_height(job_expiry)
    replacement, replacement_job = scrape_submit(
        actor, host, sender=sender, hire_id=hire_id, url="https://example.com/replacement"
    )
    assert replacement["ok"] is True and replacement_job is not None


def test_running_fetch_reuses_receipt_after_reload_and_source_cleanup(actor, host):
    scrape, _ = run_scrape(actor, host)
    source_expiry = read(actor, host, scrape["request_id"])["expires_height"]
    host.set_block_height(source_expiry - hyperbrowser.RESULT_RETENTION_BLOCKS + 1)
    fetch, _, _ = run_fetch(
        actor, host, scrape["request_id"], {"jobId": JOB_ID, "status": "running"}
    )
    fetch_id = fetch["request_id"]
    host.set_block_height(source_expiry)
    assert read(actor, host, scrape["request_id"])["ok"] is False
    reloaded = importlib.reload(hyperbrowser)
    restored = reloaded.HyperbrowserScrapeWrapper()
    accepted, job = fetch_call(restored, host, scrape["request_id"])
    assert accepted["ok"] is True and accepted["request_id"] == fetch_id
    resume(host, job, {"jobId": JOB_ID, "status": "running"})
    accepted, job = fetch_call(restored, host, scrape["request_id"])
    assert accepted["ok"] is True and accepted["request_id"] == fetch_id
    resume(host, job, completed_reply(markdown="done"))
    out = read(restored, host, fetch_id)
    assert out["output"]["data"]["job_status"] == "completed"
    assert out["output"]["data"]["markdown"] == "done"


def test_fetch_transport_error_remains_retryable(actor, host):
    scrape, _ = run_scrape(actor, host)
    fetch, _, _ = run_fetch(
        actor, host, scrape["request_id"], {"message": "temporary gateway error"}
    )
    assert read(actor, host, fetch["request_id"])["status"] == "error"
    accepted, job = fetch_call(actor, host, scrape["request_id"])
    assert accepted["ok"] is True and accepted["request_id"] == fetch["request_id"]
    resume(host, job, completed_reply(markdown="recovered"))
    assert read(actor, host, fetch["request_id"])["output"]["data"]["markdown"] == "recovered"


def test_timed_out_fetch_remains_retryable(actor, host):
    scrape, _ = run_scrape(actor, host)
    fetch, _ = fetch_call(actor, host, scrape["request_id"])
    deadline = read(actor, host, fetch["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    assert read(actor, host, fetch["request_id"])["status"] == "error"
    accepted, job = fetch_call(actor, host, scrape["request_id"])
    assert accepted["ok"] is True and accepted["request_id"] == fetch["request_id"]
    resume(host, job, completed_reply(markdown="after timeout"))
    assert read(actor, host, fetch["request_id"])["output"]["data"]["markdown"] == "after timeout"


def test_retried_fetch_preserves_duplicate_transaction_guard(actor, host):
    scrape, _ = run_scrape(actor, host)
    fetch, _, _ = run_fetch(
        actor, host, scrape["request_id"], {"jobId": JOB_ID, "status": "running"}
    )
    host.set_context(sender=BUYER, tx_hash=(99).to_bytes(32, "big"))
    payload = codec.encode({"hire_id": 42, "source_request_id": scrape["request_id"]})
    first = codec.decode(actor.fetch(payload))
    duplicate = codec.decode(actor.fetch(payload))
    assert first["ok"] is True and first["request_id"] == fetch["request_id"]
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_deadline_expiry_is_terminal_and_late_result_is_stale(actor, host):
    accepted, job = scrape_submit(actor, host)
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, {"jobId": JOB_ID})
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    payload = codec.encode(BASE)
    first = codec.decode(actor.scrape(payload))
    duplicate = codec.decode(actor.scrape(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = scrape_submit(actor, host, url="https://example.com/first")
    second, second_job = scrape_submit(actor, host, url="https://example.com/second")
    resume(host, second_job, {"jobId": "job-second"})
    resume(host, first_job, {"jobId": "job-first"})
    assert read(actor, host, first["request_id"])["output"]["data"]["job_id"] == "job-first"
    assert read(actor, host, second["request_id"])["output"]["data"]["job_id"] == "job-second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    scrape_accepted, _ = run_scrape(actor, host)
    host.events.clear()
    out = read(actor, host, scrape_accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == scrape_accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= hyperbrowser.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    scrape_accepted, _ = run_scrape(actor, host, job_id="job-PRIVATE")
    accepted, _, _ = run_fetch(
        actor,
        host,
        scrape_accepted["request_id"],
        completed_reply(markdown="SECRET-CONTENT-XYZ", job_id="job-PRIVATE"),
    )
    assert read(actor, host, accepted["request_id"])["output"]["data"]["markdown"] == "SECRET-CONTENT-XYZ"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    encoded = codec.encode(meta).decode("latin1")
    assert "SECRET-CONTENT-XYZ" not in encoded
    assert "job-PRIVATE" not in encoded


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
    with pytest.raises(ValueError, match="already initialized"):
        actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "hyperbrowser"
    assert cap["handlers"] == ["scrape", "fetch"]
    assert cap["poll_handler"] == "get_result"
    assert cap["slot"] == "web-scrape"
    assert set(cap["scrape_input"]) == set(hyperbrowser.SUPPORTED_INPUT_FIELDS_SCRAPE)
    assert set(cap["fetch_input"]) == set(hyperbrowser.SUPPORTED_INPUT_FIELDS_FETCH)
    assert cap["result_byte_budget"] == hyperbrowser.RETURN_BYTE_BUDGET
    assert cap["max_retained_with_fetches"] == hyperbrowser.MAX_RETAINED_WITH_FETCHES
    assert cap["max_retained_with_fetches_per_hire"] == hyperbrowser.MAX_RETAINED_WITH_FETCHES_PER_HIRE
    assert cap["job_lifetime_blocks"] == hyperbrowser.JOB_LIFETIME_BLOCKS
    # G5 + stealth exclusions are advertised.
    excluded = " ".join(cap["excluded"])
    assert "sessions" in excluded and "screenshot" in excluded.replace("_format", "")


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            hyperbrowser._bounded_int(bad, "request_id", 1, 1000)
    assert hyperbrowser._bounded_int(7, "request_id", 1, 1000) == 7
    assert hyperbrowser._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join("tests", "test_hyperbrowser_parity.py")
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
