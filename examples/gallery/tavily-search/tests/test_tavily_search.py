"""Tavily Search actor contract checks."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import actors.tavily.main as tavily
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20


def vendor_reply(label="Tavily"):
    return {
        "query": "tavily web search",
        "answer": "Tavily provides search infrastructure.",
        "images": [],
        "results": [
            {
                "title": label,
                "url": "https://example.com/tavily",
                "content": label + " provides web search infrastructure.",
                "score": 0.91,
                "raw_content": None,
            }
        ],
        "response_time": 1.23,
        "usage": {"credits": 1},
        "request_id": "123e4567-e89b-12d3-a456-426614174111",
    }


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, **payload):
    host.set_context(
        sender=BUYER,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    accepted = codec.decode(
        actor.search(codec.encode({"hire_id": 42, "query": "tavily web search", **payload}))
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = tavily._run_request__resume(
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
    accepted = codec.decode(
        actor.search(
            codec.encode(
                {"hire_id": hire_id, "query": "tavily web search", **payload}
            )
        )
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def test_search_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(
        actor,
        host,
        search_depth="advanced",
        chunks_per_source=2,
        max_results=3,
        topic="finance",
        time_range="month",
        start_date="2026-08-01",
        end_date="2026-09-11",
        include_published_date=True,
        filter_by_published_date=True,
        include_answer="advanced",
        include_raw_content="markdown",
        include_images=True,
        include_image_descriptions=True,
        include_favicon=True,
        include_domains=["sec.gov", "reuters.com"],
        include_domains_mode="boost",
        exclude_domains=["example.org"],
        language="en",
        filter_by_language=True,
        auto_parameters=False,
        exact_match=True,
        include_usage=True,
        safe_search=True,
        project_id="cowboy-store",
        session_id="task-7",
        human_id="buyer-9",
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "search",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == tavily.SEARCH_ENDPOINT
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer ${TAVILY_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": tavily.USER_AGENT,
        "X-Project-ID": "cowboy-store",
        "X-Session-Id": "task-7",
        "X-Human-Id": "buyer-9",
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), tavily.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert json.loads(kwargs["raw_body"]) == {
        "query": "tavily web search",
        "search_depth": "advanced",
        "chunks_per_source": 2,
        "max_results": 3,
        "topic": "finance",
        "time_range": "month",
        "start_date": "2026-08-01",
        "end_date": "2026-09-11",
        "include_published_date": True,
        "filter_by_published_date": True,
        "include_answer": "advanced",
        "include_raw_content": "markdown",
        "include_images": True,
        "include_image_descriptions": True,
        "include_favicon": True,
        "include_domains": ["sec.gov", "reuters.com"],
        "include_domains_mode": "boost",
        "exclude_domains": ["example.org"],
        "language": "en",
        "filter_by_language": True,
        "auto_parameters": False,
        "exact_match": True,
        "include_usage": True,
        "safe_search": True,
    }
    resume(host, job, vendor_reply())
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == vendor_reply()


def test_defaults_are_explicit_and_transport_bounded(actor, host):
    _, job = submit(actor, host)
    body = json.loads(kwargs_of(job)["raw_body"])
    assert body == {
        "query": "tavily web search",
        "max_results": tavily.DEFAULT_MAX_RESULTS,
    }
    assert body["max_results"] <= tavily.MAX_MAX_RESULTS


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": 7},
        {"query": "x" * 401},
        {"search_depth": "deep"},
        {"chunks_per_source": 0},
        {"chunks_per_source": 4},
        {"max_results": -1},
        {"max_results": 11},
        {"topic": "sports"},
        {"time_range": "hour"},
        {"start_date": 20260101},
        {"start_date": "x" * 33},
        {"include_answer": "long"},
        {"include_raw_content": "html"},
        {"include_images": 1},
        {"include_domains": "sec.gov"},
        {"include_domains": []},
        {"include_domains": ["https://sec.gov"]},
        {"include_domains": ["x.com"] * 21},
        {"exclude_domains": [7]},
        {"include_domains_mode": "boost"},
        {"country": "united states", "topic": "news"},
        {"filter_by_language": True},
        {"safe_search": True, "search_depth": "fast"},
        {"chunks_per_source": 2, "search_depth": "ultra-fast"},
        {"include_image_descriptions": True},
        {"project_id": "x" * 129},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    base = {"hire_id": 42, "query": "tavily web search"}
    base.update(payload)
    out = codec.decode(actor.search(codec.encode(base)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_and_oversized_input_never_dispatch(actor, host):
    for payload in (
        {"query": "use ${TAVILY_API_KEY}"},
        {"unexpected": {"nested": ["${OTHER_SECRET}"]}},
        {"unexpected": "x" * tavily.MAX_REQUEST_BYTES},
    ):
        out = codec.decode(
            actor.search(
                codec.encode({"hire_id": 42, "query": "tavily search", **payload})
            )
        )
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(
        actor.search(codec.encode({"hire_id": 99, "query": "tavily web search"}))
    )
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.search(codec.encode({"hire_id": 42, "query": "tavily web search"}))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"type": "error", "error": {"message": "invalid api key"}}, "invalid api key"),
        ({"detail": {"error": "rate limit exceeded"}}, "rate limit exceeded"),
        ("upstream returned HTML", "upstream returned HTML"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]


@pytest.mark.parametrize(
    "vendor",
    [
        {"query": "q", "results": []},
        {"query": "", "results": []},
        {"query": "q", "results": {}},
        {"query": "q", "results": [{"title": "", "url": "u", "content": "c", "score": 1.0}]},
        {"query": "q", "results": [{"title": "t", "url": 7, "content": "c", "score": 1.0}]},
        {"query": "q", "results": [{"title": "t", "url": "u", "content": 7, "score": 1.0}]},
        {"query": "q", "results": [{"title": "t", "url": "u", "content": "c", "score": "high"}]},
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, vendor):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


def test_oversized_response_is_bounded(actor, host):
    big = vendor_reply()
    big["request_id"] = "request-id-must-remain-exact"
    big["images"] = [{"url": "https://images.example/exact.png", "description": "d" * 1_000}]
    big["results"] = [
        {
            "id": "result-id-%d" % i,
            "title": "z" * 500,
            "url": "https://example.com/%d" % i,
            "content": "y" * 1_800,
            "raw_content": "r" * 1_800,
            "score": 0.8,
        }
        for i in range(10)
    ]
    assert tavily.API_BYTE_BUDGET < len(codec.encode(big)) < tavily.RUNNER_RESULT_BYTE_BUDGET
    accepted, _, _ = run_full(actor, host, result=big)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert len(codec.encode(out)) < tavily.RETURN_BYTE_BUDGET
    data = out["output"]["data"]
    assert data["_cowboy"]["truncated"] is True
    assert data["request_id"] == big["request_id"]
    assert [item["id"] for item in data["results"]] == [item["id"] for item in big["results"]]
    assert [item["url"] for item in data["results"]] == [item["url"] for item in big["results"]]
    assert [item["title"] for item in data["results"]] == [item["title"] for item in big["results"]]
    assert data["images"][0]["url"] == big["images"][0]["url"]
    assert any(item["content"] != source["content"] for item, source in zip(data["results"], big["results"]))


def test_oversized_response_accepts_null_result_images(actor, host):
    big = vendor_reply()
    big["results"][0]["content"] = "x" * 40_000
    big["results"][0]["images"] = None
    accepted, _, _ = run_full(actor, host, result=big)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"]["results"][0]["images"] is None


def test_uncompressible_numeric_body_is_error(actor, host):
    body = vendor_reply()
    body["usage"] = {"n%d" % i: i + 0.123456789 for i in range(5_000)}
    accepted, _, _ = run_full(actor, host, result=body)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(tavily.TavilySearchWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(tavily.TavilySearchWrapper, "on_runner_result")


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
    assert kwargs["headers"]["Authorization"] == "Bearer ${TAVILY_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), tavily.SECRET_KEY]]


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(tavily.MAX_RETAINED_PER_HIRE):
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
    while accepted_count < tavily.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == tavily.MAX_RETAINED_REQUESTS
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
    payload = codec.encode({"hire_id": 42, "query": "tavily web search"})
    first = codec.decode(actor.search(payload))
    duplicate = codec.decode(actor.search(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, query="first tavily query")
    second, second_job = submit(actor, host, query="second tavily query")
    resume(host, second_job, vendor_reply("second"))
    resume(host, first_job, vendor_reply("first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["results"][0]["title"] == "first"
    assert read(actor, host, second["request_id"])["output"]["data"]["results"][0]["title"] == "second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    big = vendor_reply()
    big["results"][0]["content"] = "z" * 10_000
    accepted, _, _ = run_full(actor, host, result=big)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= tavily.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(actor, host, result=vendor_reply("PRIVATE"))
    assert read(actor, host, accepted["request_id"])["output"]["data"]["results"][0]["title"] == "PRIVATE"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert "PRIVATE" not in codec.encode(meta).decode("latin1")


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
    assert cap["vendor"] == "tavily"
    assert cap["handlers"] == ["search"]
    assert cap["max_query_chars"] == 400
    assert cap["max_results"] == 10
    assert cap["max_domains"] == 20
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert set(cap["input"]) == set(tavily.SUPPORTED_INPUT_FIELDS)
    assert cap["input"]["time_range"] == "day|week|month|year|d|w|m|y?"


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            tavily._bounded_int(bad, "request_id", 1, 1000)
    assert tavily._bounded_int(7, "request_id", 1, 1000) == 7
    assert tavily._bounded_int("7", "request_id", 1, 1000) == 7


def test_valid_search_response_accepts_empty_title():
    # Tavily's contract requires title/url present as strings, not non-empty; an
    # untitled result must be accepted, not turned into a paid error. A non-string
    # title is still rejected.
    resp = {
        "query": "q",
        "results": [{"title": "", "url": "https://example.com", "content": "", "score": 0.5}],
        "images": [],
        "answer": None,
        "response_time": 1.0,
    }
    assert tavily._valid_search_response(resp) is True
    bad = dict(resp, results=[{"title": 1, "url": "https://x", "content": "", "score": 0.1}])
    assert tavily._valid_search_response(bad) is False


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    project = Path(__file__).resolve().parent.parent
    script = project / "tools" / "check_tavily_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, str(script), "--offline"],
                        cwd=project, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    (work / "actors" / "tavily").mkdir(parents=True)
    (work / "tools").mkdir()
    shutil.copy2(project / "actors" / "tavily" / "main.py", work / "actors" / "tavily" / "main.py")
    shutil.copy2(project / "tavily_routes.json", work / "tavily_routes.json")
    shutil.copy2(script, work / "tools" / script.name)
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
    path = work / "tavily_routes.json"
    with path.open() as fh:
        data = json.load(fh)
    with path.open("w") as fh:
        json.dump(_bust(data), fh)
    bad = subprocess.run([sys.executable, str(work / "tools" / script.name), "--offline"],
                         cwd=work, capture_output=True, text=True)
    assert bad.returncode != 0
