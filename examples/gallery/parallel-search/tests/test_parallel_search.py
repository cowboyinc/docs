"""Parallel Search actor contract checks."""

import json

import actors.search.main as parallel
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20


def vendor_reply(label="Parallel"):
    return {
        "search_id": "search_123",
        "results": [
            {
                "url": "https://example.com/parallel",
                "title": label,
                "publish_date": "2026-09-11",
                "excerpts": [label + " provides web search infrastructure."],
            }
        ],
        "warnings": None,
        "usage": [{"name": "sku_search", "count": 1}],
        "session_id": "session_123",
    }


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.search(codec.encode({"hire_id": 42, "search_queries": ["parallel web search"], **payload}))
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = parallel._run_request__resume(
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
    host.set_context(sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.search(
            codec.encode(
                {"hire_id": hire_id, "search_queries": ["parallel web search"], **payload}
            )
        )
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def test_search_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(
        actor,
        host,
        objective="Find current Parallel product information",
        mode="turbo",
        max_results=3,
        max_chars_total=4_000,
        max_chars_per_result=900,
        session_id="task-7",
        client_model="gpt-5.4",
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
    assert kwargs["url"] == parallel.SEARCH_ENDPOINT
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": parallel.USER_AGENT,
        "x-api-key": "${PARALLEL_API_KEY}",
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), parallel.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert json.loads(kwargs["raw_body"]) == {
        "search_queries": ["parallel web search"],
        "objective": "Find current Parallel product information",
        "mode": "turbo",
        "max_chars_total": 4_000,
        "advanced_settings": {
            "max_results": 3,
            "excerpt_settings": {"max_chars_per_result": 900},
        },
        "session_id": "task-7",
        "client_model": "gpt-5.4",
    }
    resume(host, job, vendor_reply())
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == vendor_reply()


def test_defaults_are_explicit_and_transport_bounded(actor, host):
    _, job = submit(actor, host)
    body = json.loads(kwargs_of(job)["raw_body"])
    assert body == {
        "search_queries": ["parallel web search"],
        "mode": parallel.DEFAULT_MODE,
        "max_chars_total": parallel.DEFAULT_MAX_CHARS_TOTAL,
        "advanced_settings": {
            "max_results": parallel.DEFAULT_MAX_RESULTS,
            "excerpt_settings": {
                "max_chars_per_result": parallel.DEFAULT_CHARS_PER_RESULT
            },
        },
    }
    assert body["max_chars_total"] <= parallel.MAX_MAX_CHARS_TOTAL
    assert body["advanced_settings"]["max_results"] <= parallel.MAX_MAX_RESULTS


@pytest.mark.parametrize(
    "payload",
    [
        {"search_queries": []},
        {"search_queries": "not-a-list"},
        {"search_queries": ["q"] * 6},
        {"search_queries": ["x" * 201]},
        {"search_queries": [7]},
        {"objective": 7},
        {"objective": "x" * 5_001},
        {"mode": "one-shot"},
        {"max_results": 0},
        {"max_results": 6},
        {"max_chars_total": 499},
        {"max_chars_total": 8_001},
        {"max_chars_per_result": 199},
        {"max_chars_per_result": 2_001},
        {"session_id": "x" * 1_001},
        {"client_model": "x" * 201},
        {"advanced_settings": {"max_results": 40}},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    base = {"hire_id": 42, "search_queries": ["parallel web search"]}
    base.update(payload)
    out = codec.decode(actor.search(codec.encode(base)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_and_oversized_input_never_dispatch(actor, host):
    for payload in (
        {"objective": "use ${PARALLEL_API_KEY}"},
        {"unexpected": {"nested": ["${OTHER_SECRET}"]}},
        {"unexpected": "x" * parallel.MAX_REQUEST_BYTES},
    ):
        out = codec.decode(
            actor.search(
                codec.encode(
                    {"hire_id": 42, "search_queries": ["parallel web search"], **payload}
                )
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
        actor.search(codec.encode({"hire_id": 99, "search_queries": ["parallel web search"]}))
    )
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.search(codec.encode({"hire_id": 42, "search_queries": ["parallel web search"]}))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"type": "error", "error": {"message": "invalid api key"}}, "invalid api key"),
        ({"error": "rate limit exceeded"}, "rate limit exceeded"),
        ("upstream returned HTML", "upstream returned HTML"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]


@pytest.mark.parametrize("extra", [{"detail": "metadata"}, {"error": None}])
def test_valid_success_allows_extra_error_named_fields(actor, host, extra):
    vendor = vendor_reply()
    vendor.update(extra)
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["data"] == vendor


@pytest.mark.parametrize(
    "vendor",
    [
        {"results": [], "session_id": "s"},
        {"search_id": "", "session_id": "s", "results": []},
        {"search_id": "s", "session_id": "", "results": []},
        {"search_id": "s", "session_id": "s", "results": {}},
        {"search_id": "s", "session_id": "s", "results": [{"url": "", "excerpts": []}]},
        {"search_id": "s", "session_id": "s", "results": [{"url": 7, "excerpts": []}]},
        {"search_id": "s", "session_id": "s", "results": [{"url": "u", "excerpts": [7]}]},
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, vendor):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


def test_oversized_response_is_bounded(actor, host):
    big = vendor_reply()
    big["results"] = [
        {"url": "https://example.com/%d" % i, "title": "z" * 500, "excerpts": ["y" * 4_000]}
        for i in range(10)
    ]
    accepted, _, _ = run_full(actor, host, result=big)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert len(codec.encode(out)) < parallel.RETURN_BYTE_BUDGET
    assert out["output"]["data"]["_cowboy"]["truncated"] is True
    assert out["output"]["data"]["search_id"] == big["search_id"]
    assert out["output"]["data"]["session_id"] == big["session_id"]
    kept = out["output"]["data"]["results"]
    assert [item["url"] for item in kept] == [item["url"] for item in big["results"][:len(kept)]]


def test_oversized_identifiers_fail_instead_of_changing_them(actor, host):
    vendor = vendor_reply()
    vendor["search_id"] = "s" * parallel.API_BYTE_BUDGET
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error"
    assert out["output"]["error"] == "result could not be bounded under the byte budget"


def test_nonserializable_success_is_error(actor, host):
    vendor = vendor_reply()
    vendor["usage"] = float("nan")
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error"
    assert out["output"]["error"] == "result could not be bounded under the byte budget"


def test_uncompressible_numeric_body_is_error(actor, host):
    body = vendor_reply()
    body["usage"] = [{"n%d" % i: i + 0.123456789 for i in range(5_000)}]
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
    assert getattr(parallel.ParallelSearchWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(parallel.ParallelSearchWrapper, "on_runner_result")


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
    assert kwargs["headers"]["x-api-key"] == "${PARALLEL_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), parallel.SECRET_KEY]]


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(parallel.MAX_RETAINED_PER_HIRE):
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
    while accepted_count < parallel.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == parallel.MAX_RETAINED_REQUESTS
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
    payload = codec.encode({"hire_id": 42, "search_queries": ["parallel web search"]})
    first = codec.decode(actor.search(payload))
    duplicate = codec.decode(actor.search(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, search_queries=["first parallel query"])
    second, second_job = submit(actor, host, search_queries=["second parallel query"])
    resume(host, second_job, vendor_reply("second"))
    resume(host, first_job, vendor_reply("first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["results"][0]["title"] == "first"
    assert read(actor, host, second["request_id"])["output"]["data"]["results"][0]["title"] == "second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    big = vendor_reply()
    big["results"][0]["excerpts"] = ["z" * 10_000]
    accepted, _, _ = run_full(actor, host, result=big)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= parallel.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(actor, host, result=vendor_reply("PRIVATE"))
    assert read(actor, host, accepted["request_id"])["output"]["data"]["results"][0]["title"] == "PRIVATE"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert "PRIVATE" not in codec.encode(meta).decode("latin1")
    # M2: KEY_LAST_OUTPUT never expires, so the persisted value itself must never
    # carry the vendor data, not just the get_last_output projection.
    stored = actor.storage.get(parallel.KEY_LAST_OUTPUT) or {}
    assert set(stored) <= {"operation", "ok"}
    assert "data" not in stored
    assert "PRIVATE" not in codec.encode(stored).decode("latin1")


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "parallel"
    assert cap["handlers"] == ["search"]
    assert cap["max_queries"] == 5
    assert cap["max_results"] == 5
    assert cap["max_chars_total"] == 8_000


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            parallel._bounded_int(bad, "request_id", 1, 1000)
    assert parallel._bounded_int(7, "request_id", 1, 1000) == 7
    assert parallel._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    script = "test_parallel_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script), "--offline"],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    project = os.path.dirname(here)
    work.mkdir()
    shutil.copytree(os.path.join(project, "actors"), work / "actors")
    shutil.copytree(here, work / "tests")
    shutil.copy(os.path.join(project, "parallel_routes.json"), work)
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
    bad = subprocess.run(
        [sys.executable, os.path.join(str(work), "tests", script), "--offline"],
        cwd=str(work), capture_output=True, text=True,
    )
    assert bad.returncode != 0
