"""Contract checks: real-SDK dispatch, callbacks, and caller result reads."""

import json
import importlib
import importlib.util
import sys

import actors.browse.main as bb
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER, SDK
from cowboy_sdk import PermissionDeniedError, codec, continuation

# Browserbase API reference envelopes, independent of Actor output shaping.
# https://docs.browserbase.com/reference/api/fetch-a-page
# https://docs.browserbase.com/reference/api/web-search
FETCH_OK = {"id": "fetch-1", "statusCode": 200, "headers": {},
            "content": "# Title\n\nbody text", "contentType": "text/markdown", "encoding": "utf-8"}
SEARCH_OK = {"requestId": "search-1", "query": "cowboy sdk",
            "results": [{"url": "https://a.example", "title": "A"},
                         {"url": "https://b.example", "title": "B"}]}
BROWSE_STARTED = {"runId": "18e1fcee-run", "status": "RUNNING"}
BROWSE_DONE = {"runId": "18e1fcee-run", "status": "COMPLETED", "result": {"answer": "42"}}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, handler, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(bb, handler)(codec.encode({"hire_id": 42, **payload})))
    job = last_job(host) if accepted.get("ok") else None
    return accepted, job


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = bb._run_request__resume(codec.encode(
        {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": result}))
    return codec.decode(out) if out else None


def run_full(actor, host, handler, result, **payload):
    accepted, job = submit(actor, host, handler, **payload)
    final = resume(host, job, result)
    return accepted, final, job


def read(actor, host, request_id, sender=BUYER, **fields):
    host.set_context(sender=sender)
    return codec.decode(bb.get_result(codec.encode({"request_id": request_id, "hire_id": 42, **fields})))


def test_fetch_dispatch_secret_custody_and_result(actor, host):
    accepted, final, job = run_full(actor, host, "fetch", FETCH_OK,
                                    url="https://example.com", format="markdown")
    assert accepted["status"] == "pending" and "output" not in accepted
    k = kwargs_of(job)
    assert k["url"] == "https://api.browserbase.com/v1/fetch" and k["method"] == "POST"
    assert k["headers"]["X-BB-API-Key"] == "${BROWSERBASE_API_KEY}"
    assert k["headers"]["Content-Type"] == "application/json"
    assert k["secrets"] == [["0x" + OWNER.hex(), "BROWSERBASE_API_KEY"]]
    assert k["hire"] == {"hire_id": 42, "declaration_id": "0x" + "55" * 32,
                         "version": 3, "artifact_id": 7}
    assert "timeout_ms" not in k and "body" not in k
    assert json.loads(k["raw_body"]) == {"url": "https://example.com", "format": "markdown"}
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["data"] == FETCH_OK


def test_search_dispatch_and_result(actor, host):
    accepted, _, job = run_full(actor, host, "search", SEARCH_OK, query="cowboy sdk", num_results=5)
    k = kwargs_of(job)
    assert k["url"] == "https://api.browserbase.com/v1/search"
    assert json.loads(k["raw_body"]) == {"query": "cowboy sdk", "numResults": 5}
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["data"]["results"][0]["url"] == "https://a.example"


def test_browse_starts_a_run_and_browse_result_polls_it(actor, host):
    started, sfinal, sjob = run_full(actor, host, "browse", BROWSE_STARTED, task="find the price")
    sk = kwargs_of(sjob)
    assert sk["url"] == "https://api.browserbase.com/v1/agents/runs" and sk["method"] == "POST"
    assert json.loads(sk["raw_body"]) == {"task": "find the price"}
    out = read(actor, host, started["request_id"])
    assert out["output"]["data"] == {"run_id": "18e1fcee-run", "status": "RUNNING", "done": False}
    # poll it — a GET with the run id in the path, no body or Content-Type
    done, dfinal, djob = run_full(actor, host, "browse_result", BROWSE_DONE, run_id="18e1fcee-run")
    dk = kwargs_of(djob)
    assert dk["method"] == "GET" and dk["url"] == "https://api.browserbase.com/v1/agents/runs/18e1fcee-run"
    assert "body" not in dk and "raw_body" not in dk and "Content-Type" not in dk["headers"]
    out = read(actor, host, done["request_id"])
    assert out["output"]["data"]["done"] is True
    assert out["output"]["data"]["result"] == {"answer": "42"}


def test_secret_templates_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    assert codec.decode(actor.fetch(codec.encode(
        {"hire_id": 42, "url": "https://x.example", "format": "json",
         "schema": {"k": "${BROWSERBASE_API_KEY}"}})))["ok"] is False
    assert codec.decode(actor.search(codec.encode(
        {"hire_id": 42, "query": "${BROWSERBASE_API_KEY}"})))["ok"] is False
    assert codec.decode(actor.browse(codec.encode(
        {"hire_id": 42, "task": "leak ${BROWSERBASE_API_KEY}"})))["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host, "fetch", FETCH_OK, url="https://example.com")
    assert read(actor, host, accepted["request_id"], sender=b"\x44" * 20)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    assert codec.decode(actor.fetch(codec.encode({"hire_id": 99, "url": "https://x.example"})))["ok"] is False
    host.set_context(sender=b"\x44" * 20)
    with pytest.raises(ValueError):
        actor.fetch(codec.encode({"hire_id": 42, "url": "https://x.example"}))
    assert not host.get_send_log()


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host, "fetch", FETCH_OK, url="https://example.com")
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True
    late = resume(host, job, FETCH_OK)
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_forged_callback_rejected(actor, host):
    accepted, job = submit(actor, host, "fetch", url="https://example.com")
    for forged in (BUYER, OWNER, b"\x44" * 20):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, FETCH_OK, sender=forged)


def test_pending_until_callback_resolves(actor, host):
    accepted, job = submit(actor, host, "fetch", url="https://example.com")
    assert read(actor, host, accepted["request_id"])["status"] == "pending"
    resume(host, job, FETCH_OK)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_vendor_error_is_status_error(actor, host):
    accepted, _, _ = run_full(actor, host, "fetch", {"error": "rate limited"}, url="https://example.com")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "rate limited" in out["output"]["error"]


def test_raw_format_fetch_returns_the_raw_body(actor, host):
    raw = dict(FETCH_OK, content="<html><body>hi</body></html>", contentType="text/html")
    accepted, _, _ = run_full(actor, host, "fetch", raw, url="https://example.com", format="raw")
    out = read(actor, host, accepted["request_id"])
    assert out["output"]["ok"] is True and out["output"]["data"]["content"] == raw["content"]


@pytest.mark.parametrize("reply", [
    {"raw": "PGh0bWw+NTAyIEJhZCBHYXRld2F5PC9odG1sPg==", "content_type": "text/html"},
    {}, None, [], "not JSON", {"message": "upstream unavailable"},
])
@pytest.mark.parametrize("handler,payload", [
    ("fetch", {"url": "https://example.com"}), ("search", {"query": "cowboy"}),
])
def test_malformed_vendor_response_is_a_stored_error(actor, host, handler, payload, reply):
    accepted, _, _ = run_full(actor, host, handler, reply, **payload)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["output"]["ok"] is False


def test_fetch_preserves_target_http_status(actor, host):
    # Browserbase can successfully fetch a page whose own response is 404.
    reply = dict(FETCH_OK, statusCode=404, content="Not found")
    accepted, _, _ = run_full(actor, host, "fetch", reply, url="https://example.com/missing")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["data"]["statusCode"] == 404


@pytest.mark.parametrize("handler,field,payload,reply", [
    ("fetch", "schema", {"url": "https://example.com", "format": "json"}, dict(FETCH_OK, content={"price": 0.5})),
    ("browse", "result_schema", {"task": "find the price"}, BROWSE_STARTED),
])
def test_fractional_schema_uses_verbatim_json_bytes(actor, host, handler, field, payload, reply):
    schema = {"type": "object", "properties": {"price": {"type": "number", "minimum": 0.5}}}
    accepted, _, job = run_full(actor, host, handler, reply, **dict(payload, **{field: schema}))
    kw = kwargs_of(job)
    assert "body" not in kw and isinstance(kw["raw_body"], bytes)
    assert json.loads(kw["raw_body"])["schema" if handler == "fetch" else "resultSchema"] == schema
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_schema_refuses_non_json_numbers_before_dispatch(actor, host, value):
    with pytest.raises(ValueError):
        bb.fetch(codec.encode({"hire_id": 42, "url": "https://example.com", "format": "json",
                              "schema": {"minimum": value}}))
    assert not host.get_send_log()


def test_oversized_response_is_bounded(actor, host):
    # This checks post-callback shaping only, not Runner transport of this size.
    big = dict(FETCH_OK, content="z" * 200_000)
    accepted, _, _ = run_full(actor, host, "fetch", big, url="https://example.com")
    out = read(actor, host, accepted["request_id"])
    assert len(codec.encode(out)) < bb.RETURN_BYTE_BUDGET
    assert out["output"]["ok"] is True


def test_large_agent_result_preserves_polling_controls(actor, host):
    reply = dict(BROWSE_DONE, result={"text": "z" * 200_000})
    accepted, _, _ = run_full(actor, host, "browse_result", reply, run_id=reply["runId"])
    out = read(actor, host, accepted["request_id"])
    data = out["output"]["data"]
    assert (data["run_id"], data["status"], data["done"]) == (reply["runId"], "COMPLETED", True)
    assert data["result"]["_cowboy"]["truncated"] is True
    assert len(codec.encode(out)) <= 38_000


def test_input_validation(actor, host):
    host.set_context(sender=BUYER)
    bad = [
        ("fetch", {"url": "ftp://nope"}),
        ("fetch", {"url": "https://x.example", "format": "pdf"}),
        ("fetch", {"url": "https://x.example", "format": "markdown", "schema": {"k": 1}}),
        ("search", {"query": "   "}),
        ("search", {"query": "x" * 300}),
        ("browse", {"task": ""}),
        ("browse_result", {"run_id": "bad/id"}),
    ]
    for handler, payload in bad:
        assert codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))["ok"] is False
    assert not host.get_send_log()


def test_request_capacity_is_bounded(actor, host):
    for _ in range(bb.MAX_RETAINED_REQUESTS):
        submit(actor, host, "fetch", url="https://example.com")
    full, _ = submit(actor, host, "fetch", url="https://example.com")
    assert full["ok"] is False and "capacity" in full["error"]


def test_deadline_expiry_is_error_and_late_callback_is_stale(actor, host):
    accepted, job = submit(actor, host, "fetch", url="https://example.com")
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, FETCH_OK)
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    first = codec.decode(actor.fetch(codec.encode({"hire_id": 42, "url": "https://example.com"})))
    assert first["ok"] is True
    dup = codec.decode(actor.search(codec.encode({"hire_id": 42, "query": "second"})))
    assert dup["ok"] is False and "transaction" in dup["error"]


def test_overlapping_requests_keep_their_own_results(actor, host):
    a, first = submit(actor, host, "fetch", url="https://a.example")
    b, second = submit(actor, host, "search", query="beta")
    assert a["request_id"] != b["request_id"]
    resume(host, second, SEARCH_OK)
    resume(host, first, dict(FETCH_OK, content="A"))
    assert read(actor, host, a["request_id"])["output"]["data"]["content"] == "A"
    assert read(actor, host, b["request_id"])["output"]["data"]["results"][0]["url"] == "https://a.example"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    accepted, _, _ = run_full(actor, host, "fetch", FETCH_OK, url="https://example.com")
    host.events.clear()
    host.set_context(sender=BUYER)
    resp = codec.decode(actor.get_result(codec.encode(
        {"request_id": accepted["request_id"], "hire_id": 42, "receipt_offset": 0})))
    assert resp["status"] == "ok"
    chunks = [p for name, p in host.events if name == "wrapper.result_chunk"]
    assert chunks and chunks[0]["offset"] == 0
    assert chunks[0]["total"] == len(codec.encode(resp))
    assert len(chunks[0]["data"]) <= bb.MAX_RESULT_CHUNK_BYTES


def test_hire_registration_is_idempotent_and_guards_conflicts(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + (b"\x66" * 20).hex())))


def test_hire_and_result_capacity_retention_and_full_chunks(actor, host):
    host.set_context(sender=OWNER)
    for hire_id in range(43, 43 + bb.MAX_REGISTERED_HIRES - 1):
        bb.register_hire(codec.encode(dict(HIRE, hire_id=hire_id)))
    with pytest.raises(ValueError, match="capacity"):
        bb.register_hire(codec.encode(dict(HIRE, hire_id=999)))
    for _ in range(bb.MAX_RETAINED_REQUESTS):
        accepted, _, _ = run_full(actor, host, "fetch", dict(FETCH_OK, content="a" * 36_000),
                                  url="https://example.com")
    full, _ = submit(actor, host, "fetch", url="https://example.com")
    assert full["ok"] is False and "capacity" in full["error"]
    last = read(actor, host, accepted["request_id"], receipt_offset=0)
    assert len(host.events[-1][1]["data"]) == 3_900
    # The fixture enforces encoded per-value, aggregate storage, and event caps.
    assert max(map(len, host._storage.values())) <= 65_536
    host.set_block_height(last["expires_height"])
    assert submit(actor, host, "fetch", url="https://example.com")[0]["ok"] is True


def test_pending_results_survive_module_reload_and_reverse_callbacks(actor, host):
    first, first_job = submit(actor, host, "fetch", url="https://example.com")
    second, second_job = submit(actor, host, "search", query="beta")
    saved = {name: module for name, module in sys.modules.copy().items()
             if name == "actors.browse.main" or name == "cowboy_sdk" or name.startswith("cowboy_sdk.")}

    def reload_modules():
        for name in list(sys.modules):
            if name == "actors.browse.main" or name == "cowboy_sdk" or name.startswith("cowboy_sdk."):
                del sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            "cowboy_sdk", SDK / "cowboy_sdk/__init__.py",
            submodule_search_locations=[str(SDK / "cowboy_sdk")])
        module = importlib.util.module_from_spec(spec)
        sys.modules["cowboy_sdk"] = module
        spec.loader.exec_module(module)
        return importlib.import_module("actors.browse.main")

    try:
        for job, reply in ((second_job, SEARCH_OK), (first_job, FETCH_OK)):
            fresh = reload_modules()
            host.set_context(sender=ACTOR)
            fresh._run_request__resume(codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}))
        fresh = reload_modules()
        host.set_context(sender=BUYER)
        for accepted, expected in ((first, FETCH_OK), (second, SEARCH_OK)):
            result = codec.decode(fresh.get_result(codec.encode({"hire_id": 42, "request_id": accepted["request_id"]})))
            assert result["output"]["data"] == expected
    finally:
        for name in list(sys.modules):
            if name == "actors.browse.main" or name == "cowboy_sdk" or name.startswith("cowboy_sdk."):
                del sys.modules[name]
        sys.modules.update(saved)
        # `import actors.browse.main as bb` binds via the parent package
        # attribute, so resync it to the restored module (not the reload copy).
        sys.modules["actors.browse"].main = saved["actors.browse.main"]


def test_capability_advertises_handlers_and_poll(actor, host):
    cap = codec.decode(actor.capability(None))
    assert cap["handlers"] == ["fetch", "search", "browse", "browse_result"]
    assert cap["poll_handler"] == "get_result" and cap["slot"] == "browse"
    assert cap["hire_activation"]["protocol"] == "store-admin-v1"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
