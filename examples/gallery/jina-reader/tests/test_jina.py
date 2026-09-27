"""Contract checks: real SDK dispatch, callbacks, and caller result reads."""

import json

import actors.jina.main as jina
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec, continuation

READ_VENDOR = {"data": {"title": "Page", "url": "https://x.test", "content": "hello 🤠"}}
SEARCH_VENDOR = {"data": [{"title": "A", "url": "https://a.test", "content": "one"},
                          {"title": "B", "url": "https://b.test", "content": "two"}]}
API_VENDOR = {"model": "jina-embeddings-v3", "data": [{"embedding": [0.1, 0.2]}]}


def submit(actor, host, handler, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    job = codec.decode(host.get_send_log()[-1]["payload"]) if accepted.get("ok") else None
    return accepted, job


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    return codec.decode(jina._run_request__resume(codec.encode(
        {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": result})))


def read(actor, host, request_id, sender=BUYER, **fields):
    host.set_context(sender=sender)
    return codec.decode(actor.get_result(codec.encode(
        {"request_id": request_id, "hire_id": 42, **fields})))


def kwargs(job):
    return job["payload"]["kwargs"]


def test_read_dispatch_and_authenticated_result(actor, host):
    accepted, job = submit(actor, host, "read", url="https://example.com/a b")
    assert accepted["status"] == "pending" and "output" not in accepted
    request = kwargs(job)
    assert request["url"].startswith("https://r.jina.ai/https://example.com")
    assert request["method"] == "GET" and "body" not in request
    assert request["headers"]["Authorization"] == "Bearer ${JINA_API_KEY}"
    assert request["secrets"] == [["0x" + OWNER.hex(), "JINA_API_KEY"]]
    assert request["hire"] == {k: HIRE[k] for k in ("hire_id", "declaration_id", "version", "artifact_id")}
    assert "timeout_ms" not in request  # the ignored Actor HTTP timeout setting is removed
    assert job["reply_handler"] == "_run_request__resume"
    # result is not available until the callback resumes the continuation
    assert read(actor, host, accepted["request_id"])["status"] == "pending"
    resume(host, job, READ_VENDOR)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert out["output"]["data"]["title"] == "Page" and out["output"]["operation"] == "read"


def test_search_dispatch_shapes_a_list(actor, host):
    accepted, job = submit(actor, host, "search", query="cowboy", count=3)
    request = kwargs(job)
    assert request["url"] == "https://s.jina.ai/?q=cowboy&count=3"
    assert request["headers"]["X-Respond-With"] == "no-content"
    resume(host, job, SEARCH_VENDOR)
    out = read(actor, host, accepted["request_id"])["output"]
    assert out["ok"] and isinstance(out["data"], list) and out["data"][0]["title"] == "A"


def test_post_and_catalog_routing(actor, host):
    _, job = submit(actor, host, "embeddings", model="jina-embeddings-v3", input=["hi"])
    request = kwargs(job)
    assert request["url"] == jina.EMBEDDINGS_ENDPOINT and request["method"] == "POST"
    assert request["headers"]["Content-Type"] == "application/json"
    assert "body" not in request
    assert json.loads(request["raw_body"]) == {"input": ["hi"], "model": "jina-embeddings-v3"}
    _, job = submit(actor, host, "catalog", operation="get_model", model_id="jina-clip-v2")
    assert kwargs(job)["url"] == jina.API_ROOT + "/v1/models/jina-clip-v2"
    assert kwargs(job)["method"] == "GET" and "body" not in kwargs(job)
    _, job = submit(actor, host, "catalog", operation="list_batches", limit=5)
    assert kwargs(job)["url"] == jina.API_ROOT + "/v1/batches?limit=5"
    _, job = submit(actor, host, "catalog", operation="list_classifiers_post")
    request = kwargs(job)
    assert request["url"] == jina.API_ROOT + "/v1/classifiers"
    assert request["method"] == "POST" and json.loads(request["raw_body"]) == {}


def test_fractional_json_uses_raw_body(actor, host):
    _, job = submit(actor, host, "embeddings", model="m", input=[[0.25, -0.5]])
    request = kwargs(job)
    assert "body" not in request
    assert json.loads(request["raw_body"])["input"] == [[0.25, -0.5]]


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, job = submit(actor, host, "read", url="https://example.com")
    resume(host, job, READ_VENDOR)
    assert read(actor, host, accepted["request_id"], sender=b"\x44" * 20)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_secret_template_refused_before_dispatch(actor, host):
    host.set_context(sender=BUYER)
    n = len(host.get_send_log())
    for handler, payload in (
        ("read", {"url": "https://x", "instruction": "use ${JINA_API_KEY}"}),
        ("search", {"query": "${JINA_API_KEY}"}),
        ("embeddings", {"input": ["${JINA_API_KEY}"]}),
    ):
        with pytest.raises(ValueError):
            getattr(actor, handler)(codec.encode({"hire_id": 42, **payload}))
    assert len(host.get_send_log()) == n


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    assert codec.decode(actor.read(codec.encode({"hire_id": 99, "url": "https://x"})))["ok"] is False
    host.set_context(sender=b"\x44" * 20)
    with pytest.raises(ValueError):
        actor.read(codec.encode({"hire_id": 42, "url": "https://x"}))
    assert not host.get_send_log()


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, job = submit(actor, host, "read", url="https://example.com")
    assert resume(host, job, READ_VENDOR)["status"] == "ok"
    assert resume(host, job, {"error": "late"})["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_forged_callback_rejected(actor, host):
    accepted, job = submit(actor, host, "read", url="https://example.com")
    for forged in (BUYER, OWNER, b"\x44" * 20):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, READ_VENDOR, sender=forged)
    assert read(actor, host, accepted["request_id"])["status"] == "pending"


def test_vendor_error_and_non_json_become_errors(actor, host):
    accepted, job = submit(actor, host, "embeddings", model="m", input=["x"])
    resume(host, job, {"error": {"message": "bad request"}})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["output"]["ok"] is False
    accepted, job = submit(actor, host, "read", url="https://example.com")
    resume(host, job, {"unexpected": "no data key"})
    assert read(actor, host, accepted["request_id"])["status"] == "error"
    accepted, job = submit(actor, host, "embeddings", model="m", input=["x"])
    resume(host, job, {"raw": "upstream unavailable", "content_type": "text/plain"})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "non-JSON" in out["output"]["error"]


def test_embedding_vectors_are_exact_or_an_explicit_size_error(actor, host):
    exact = {"model": "jina-embeddings-v3", "data": [
        {"index": index, "embedding": [0.125] * 256} for index in range(3)
    ]}
    accepted, job = submit(actor, host, "embeddings", model="m", input=["x"])
    resume(host, job, exact)
    assert read(actor, host, accepted["request_id"])["output"]["data"] == exact

    accepted, job = submit(actor, host, "embeddings", model="m", input=["x"])
    oversized = {"model": "jina-embeddings-v3", "data": [
        {"index": index, "embedding": [0.123456789] * 1_024} for index in range(5)
    ]}
    resume(host, job, oversized)
    out = read(actor, host, accepted["request_id"])
    assert len(codec.encode(out)) < jina.RETURN_BYTE_BUDGET
    assert out["status"] == "error"
    assert out["output"]["error"] == "vendor result exceeds the encoded result byte budget"


def test_last_output_is_metadata_only(actor, host):
    accepted, job = submit(actor, host, "embeddings", model="private-model", input=["x"])
    resume(host, job, {"model": "private-model", "data": [{"embedding": [0.25]}]})
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    stored = actor.storage[jina.KEY_LAST_OUTPUT]
    assert stored == {"operation": "embeddings", "ok": True}
    assert codec.decode(actor.get_last_output(None)) == stored


def test_integer_metadata_rejects_fractions_and_non_finite_values(actor, host):
    for value in (1.75, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="integer"):
            jina._integer(value, "version", 1, 100)
    host.set_context(sender=OWNER)
    with pytest.raises(ValueError, match="integer"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=43, version=1.75)))
    assert actor.storage.get("hire:43") is None


def test_request_capacity_is_bounded(actor, host):
    jobs = [submit(actor, host, "read", url="https://e%d.test" % i)
            for i in range(jina.MAX_RETAINED_REQUESTS)]
    full, _ = submit(actor, host, "read", url="https://overflow.test")
    assert full["ok"] is False and "capacity" in full["error"]
    resume(host, jobs[0][1], READ_VENDOR)
    assert read(actor, host, jobs[0][0]["request_id"])["output"]["ok"] is True


def test_hire_registration_is_idempotent_and_guards_conflicts(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    conflicting = dict(HIRE, hirer="0x" + (b"\x66" * 20).hex())
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(conflicting))


def test_capability_lists_handlers_and_poll(actor, host):
    cap = codec.decode(actor.capability(None))
    assert cap["poll_handler"] == "get_result" and cap["vendor"] == "jina"
    assert "read" in cap["handlers"] and "catalog" in cap["handlers"]
    assert cap["hire_activation"]["protocol"] == "store-admin-v1"


def test_actor_chunks_reassemble_to_the_full_response(actor, host):
    accepted, job = submit(actor, host, "embeddings", model="m", input=["x"])
    resume(host, job, {"model": "m", "data": [{"embedding": [0.1] * 300}]})
    host.set_context(sender=BUYER)
    encoded = actor.get_result(codec.encode({"hire_id": 42, "request_id": accepted["request_id"]}))
    chunks = bytearray()
    while len(chunks) < len(encoded):
        host.events.clear()
        actor.get_result(codec.encode({"hire_id": 42, "request_id": accepted["request_id"],
                                       "receipt_offset": len(chunks)}))
        name, event = host.events[-1]
        assert name == "wrapper.result_chunk"
        assert event["offset"] == len(chunks) and event["total"] == len(encoded)
        assert len(event["data"]) <= jina.MAX_RESULT_CHUNK_BYTES
        chunks.extend(event["data"])
    assert bytes(chunks) == encoded


def test_live_reader_assembles_cbor_and_rejects_wrong_chunks(monkeypatch):
    from types import SimpleNamespace
    import cbor2
    import live_jina as live

    args = SimpleNamespace(hire_id=42, wait_seconds=300)
    expected = {"request_id": 7, "hire_id": 42, "status": "ok",
                "output": {"ok": True, "operation": "read", "data": {"content": "🤠" * 2000}}}
    encoded = cbor2.dumps(expected)

    def chunk(args, handler, payload, event_name, **kw):
        offset = payload["receipt_offset"]
        assert handler == "get_result" and event_name == "wrapper.result_chunk"
        return {"request_id": 7, "hire_id": 42, "offset": offset,
                "total": len(encoded), "data": encoded[offset:offset + 3900]}

    monkeypatch.setattr(live, "execute", chunk)
    assert live.get_result(args, 7) == expected
    for bad in ({"request_id": 8}, {"hire_id": 43}, {"offset": 1}, {"total": 38_001}, {"data": b""}):
        monkeypatch.setattr(live, "execute", lambda *a, bad=bad, **k: dict(chunk(*a, **k), **bad))
        with pytest.raises(ValueError):
            live.get_result(args, 7)
    with pytest.raises(ValueError, match="trailing"):
        live.decode(encoded + b"\x00")


def test_live_receipt_requires_exact_actor_event_and_success():
    import cbor2
    import live_jina as live

    actor = "0x" + ACTOR.hex()
    event = [actor, "wrapper.request_accepted", "0x" + cbor2.dumps({"request_id": 7}).hex()]
    assert live.receipt_event({"status": "Success", "events": [event]}, actor, event[1]) == {"request_id": 7}
    for receipt in ({"status": "Failed", "events": [event]},
                    {"status": "Success", "events": [event, event]},
                    {"status": "Success", "events": [["0x" + BUYER.hex(), *event[1:]]]}):
        with pytest.raises(ValueError):
            live.receipt_event(receipt, actor, event[1])


def test_live_wait_keeps_pending_request_and_times_out(monkeypatch):
    from types import SimpleNamespace
    import live_jina as live

    args = SimpleNamespace(hire_id=42, wait_seconds=3)
    accepted = {"ok": True, "request_id": 7, "hire_id": 42, "status": "pending"}
    reads = []
    monkeypatch.setattr(live, "execute", lambda *a: accepted)
    monkeypatch.setattr(live, "get_result",
                        lambda args, rid, **k: reads.append(rid) or {"request_id": rid, "hire_id": 42, "status": "pending"})
    times = iter([0, 1, 1, 2, 2, 4])
    monkeypatch.setattr(live.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="request 7 is still pending"):
        live.invoke(args, "read", {"url": "https://x"})
    assert reads == [7, 7]


def test_live_comparison_rejects_bad_outcome_and_checks_shape():
    import live_jina as live

    err = {"error": {"message": "no such model"}}
    ok_result = {"status": "ok", "output": {"ok": True, "operation": "embeddings",
                                            "data": {"model": "m", "data": [{"embedding": [0.1]}]}}}
    # a vendor error where success was expected must raise
    with pytest.raises(ValueError):
        live.compare("embeddings", err, ok_result, True)
    # matching model + row count passes
    direct = {"model": "m", "data": [{"embedding": [0.1]}]}
    live.compare("embeddings", direct, ok_result, True)
    # expected-error case: direct error + wrapped error
    live.compare("catalog", err, {"status": "error", "output": {"ok": False, "operation": "catalog",
                                                                "error": "no such model"}}, False)


def test_pinned_route_fixture_matches_actor():
    import check_jina_parity
    check_jina_parity.test_pinned_routes()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
