"""Contract checks for Alpha Vantage dispatch, callbacks, and result reads."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import actors.alphavantage.main as av
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
APIKEY = "&apikey=${ALPHAVANTAGE_API_KEY}"


def reply_for(url):
    if "function=GLOBAL_QUOTE" in url:
        return {"Global Quote": {"01. symbol": "IBM", "05. price": "234.0200"}}
    if "function=TIME_SERIES_DAILY" in url:
        return {"Meta Data": {"2. Symbol": "IBM"}, "Time Series (Daily)": {"2026-09-10": {"4. close": "234.02"}}}
    if "function=OVERVIEW" in url:
        return {"Symbol": "IBM", "Name": "International Business Machines", "Sector": "TECHNOLOGY"}
    return {"Error Message": "unexpected url"}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, handler, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    extract_path = kwargs_of(job).get("extract_path")
    if extract_path and isinstance(result, dict):
        result = {"result": result[extract_path]} if extract_path in result else {}
    host.set_context(sender=sender)
    out = av._run_request__resume(
        codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": result})
    )
    return codec.decode(out) if out else None


def read(actor, host, request_id, sender=BUYER, hire_id=42, **fields):
    host.set_context(sender=sender)
    return codec.decode(
        actor.get_result(codec.encode({"request_id": request_id, "hire_id": hire_id, **fields}))
    )


def run_full(actor, host, handler, result=None, **payload):
    accepted, job = submit(actor, host, handler, **payload)
    if result is None:
        result = reply_for(kwargs_of(job)["url"])
    final = resume(host, job, result)
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


def submit_as(actor, host, sender, hire_id, handler, **payload):
    host.set_context(sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": hire_id, **payload})))
    return accepted, last_job(host) if accepted.get("ok") else None


def test_quote_acceptance_dispatch_secret_custody_and_result(actor, host):
    accepted, job = submit(actor, host, "quote", symbol="ibm")
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "quote",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == av.API_ROOT + "?function=GLOBAL_QUOTE&symbol=IBM" + APIKEY
    assert kwargs["method"] == "GET"
    assert kwargs["headers"] == {"Accept": "application/json", "User-Agent": av.USER_AGENT}
    assert "${ALPHAVANTAGE_API_KEY}" in kwargs["url"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), av.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert "body" not in kwargs and "raw_body" not in kwargs
    assert "Content-Type" not in kwargs["headers"] and "timeout_ms" not in kwargs
    resume(host, job, reply_for(kwargs["url"]))
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"]["Global Quote"]["01. symbol"] == "IBM"


@pytest.mark.parametrize(
    "handler,payload,query,required",
    [
        ("quote", {"symbol": "IBM"}, "function=GLOBAL_QUOTE&symbol=IBM", "Global Quote"),
        ("daily", {"symbol": "IBM"}, "function=TIME_SERIES_DAILY&symbol=IBM&outputsize=compact", "Time Series (Daily)"),
        ("overview", {"symbol": "IBM"}, "function=OVERVIEW&symbol=IBM", "Symbol"),
    ],
)
def test_every_route_is_a_bodyless_get(actor, host, handler, payload, query, required):
    accepted, _, job = run_full(actor, host, handler, **payload)
    kwargs = kwargs_of(job)
    assert kwargs["url"] == av.API_ROOT + "?" + query + APIKEY
    assert kwargs["method"] == "GET"
    assert "body" not in kwargs and "raw_body" not in kwargs
    assert "Content-Type" not in kwargs["headers"] and "timeout_ms" not in kwargs
    out = read(actor, host, accepted["request_id"])
    if handler in av.EXTRACT_PATHS:
        assert kwargs["extract_path"] == required
        assert set(out["output"]["data"]) == {required}
    else:
        assert "extract_path" not in kwargs
    assert required in out["output"]["data"]


def test_job_sends_user_agent_and_no_auth_header(actor, host):
    _, job = submit(actor, host, "quote", symbol="IBM")
    headers = kwargs_of(job)["headers"]
    assert headers["User-Agent"] == av.USER_AGENT
    assert "X-API-KEY" not in headers and "Authorization" not in headers


def test_validation_refuses_before_dispatch(actor, host):
    bad = [
        ("quote", {"symbol": ""}),
        ("quote", {"symbol": "IBM&function=OVERVIEW"}),
        ("quote", {"symbol": "TOOLONGSYMBOL1234567"}),
        ("quote", {"symbol": "IB M"}),
        ("daily", {"symbol": "IBM", "outputsize": "huge"}),
        ("daily", {"symbol": "IBM", "outputsize": "full"}),
    ]
    for handler, payload in bad:
        out = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
        assert out["ok"] is False, (handler, payload)
    assert not host.get_send_log()


def test_secret_templates_and_oversized_input_never_dispatch(actor, host):
    for payload in (
        {"symbol": "${ALPHAVANTAGE_API_KEY}"},
        {"symbol": "IBM", "unused": {"nested": ["${OTHER_SECRET}"]}},
        {"symbol": "IBM", "unused": "x" * av.MAX_REQUEST_BYTES},
    ):
        out = codec.decode(actor.quote(codec.encode({"hire_id": 42, **payload})))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host, "quote", symbol="IBM")
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.quote(codec.encode({"hire_id": 99, "symbol": "IBM"})))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.quote(codec.encode({"hire_id": 42, "symbol": "IBM"}))
    assert not host.get_send_log()


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host, "quote", symbol="IBM")
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, reply_for(kwargs_of(job)["url"]))
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_a_continuation_not_a_public_callback(actor, host):
    assert getattr(av.AlphaVantageWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert "_run_request" not in cap["handlers"]
    assert not hasattr(av.AlphaVantageWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host, "quote", symbol="IBM")
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, reply_for(kwargs_of(job)["url"]), sender=forged)


def test_continuation_capture_contains_only_nonsensitive_routing(actor, host):
    accepted, job = submit(actor, host, "quote", symbol="IBM")
    continuations = [
        codec.decode(value)
        for key, value in host._storage.items()
        if key.startswith(b"__continuation:")
    ]
    assert len(continuations) == 1
    assert continuations[0].get("ctx") == {"request_id": accepted["request_id"]}
    kwargs = kwargs_of(job)
    assert "${ALPHAVANTAGE_API_KEY}" in kwargs["url"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), av.SECRET_KEY]]


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"Error Message": "Invalid API call. Please retry or visit the documentation."}, "Invalid API call"),
        ({"Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."}, "25 requests per day"),
        ({"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 5 calls per minute."}, "5 calls per minute"),
        ("upstream proxy returned HTML", "upstream proxy returned HTML"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, "quote", result=vendor, symbol="IBM")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]


def test_missing_required_vendor_field_is_error(actor, host):
    accepted, _, _ = run_full(actor, host, "quote", result={"symbol": "IBM"}, symbol="IBM")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


@pytest.mark.parametrize(
    "handler,result",
    [
        ("quote", {"Global Quote": "not-an-object"}),
        ("quote", {"Global Quote": {}}),
        ("daily", {"Time Series (Daily)": "not-a-date-map"}),
        ("daily", {"Time Series (Daily)": {}}),
        ("overview", {"Symbol": {"value": "IBM"}}),
        ("overview", {"Symbol": ""}),
    ],
)
def test_required_vendor_field_has_expected_container_type(actor, host, handler, result):
    accepted, _, _ = run_full(actor, host, handler, result=result, symbol="IBM")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


def test_oversized_response_is_bounded(actor, host):
    big = {
        "Meta Data": {"2. Symbol": "IBM"},
        "Time Series (Daily)": {
            "2026-%02d-%02d" % (month, day): {"4. close": "z" * 200}
            for month in range(1, 13)
            for day in range(1, 26)
        },
    }
    accepted, _, _ = run_full(actor, host, "daily", result=big, symbol="IBM")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert len(codec.encode(out)) < av.RETURN_BYTE_BUDGET
    assert out["output"]["data"]["_cowboy"]["truncated"] is True


def test_limited_response_is_bounded_in_cbor_for_floats(actor, host):
    body = {"feed": [{"a": i + 0.12345678901, "b": i + 0.98765432, "c": i * 1.1} for i in range(1500)]}
    assert len(codec.encode(body)) > av.API_BYTE_BUDGET
    limited = av._limited_response(body, av.API_BYTE_BUDGET)
    assert len(codec.encode(limited)) <= av.API_BYTE_BUDGET


def test_large_date_keyed_time_series_keeps_newest_complete_rows(actor, host):
    rows = {
        "2026-%02d-%02d" % (month, day): {
            "1. open": "123.4567",
            "2. high": "124.5678",
            "3. low": "122.3456",
            "4. close": "123.9876",
            "5. volume": "123456789",
        }
        for month in range(1, 13)
        for day in range(1, 29)
    }
    body = {"Meta Data": {"2. Symbol": "IBM"}, "Monthly Time Series": rows}
    assert len(codec.encode(body)) > 10_000
    limited = av._limited_response(body, 10_000)
    series = limited["Monthly Time Series"]
    assert limited["_cowboy"]["truncated"] is True
    assert len(codec.encode(limited)) <= 10_000
    assert len(series) < len(rows)
    assert max(series) == max(rows)
    assert series[max(rows)] == rows[max(rows)]


def test_single_item_list_is_bounded_without_livelock(actor, host):
    body = {"feed": [{"title": "x" * (av.API_BYTE_BUDGET * 2)}]}
    limited = av._limited_response(body, av.API_BYTE_BUDGET)
    assert len(codec.encode(limited)) <= av.API_BYTE_BUDGET
    assert limited["_cowboy"]["truncated"] is True
    assert len(limited["feed"]) == 1
    assert len(limited["feed"][0]["title"]) < av.API_BYTE_BUDGET


def test_uncompressible_numeric_body_is_error_not_false_success(actor, host):
    body = {"Global Quote": {"f%d" % i: i + 0.123456789 for i in range(4000)}}
    assert len(codec.encode(body)) > av.API_BYTE_BUDGET
    accepted, _, _ = run_full(actor, host, "quote", result=body, symbol="IBM")
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(
        actor, host, "overview", result={"Symbol": "IBM", "Name": "SECRETCORP"}, symbol="IBM"
    )
    assert read(actor, host, accepted["request_id"])["output"]["data"]["Name"] == "SECRETCORP"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert meta["operation"] == "overview" and meta["ok"] is True
    assert "SECRETCORP" not in codec.encode(meta).decode("latin1")
    # M2: the PERSISTED KEY_LAST_OUTPUT (never expires) must be metadata-only too,
    # not just the getter's projection.
    stored = actor.storage.get(av.KEY_LAST_OUTPUT)
    assert set(stored) <= {"ok", "operation"}
    assert "SECRETCORP" not in codec.encode(stored).decode("latin1")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(av.MAX_RETAINED_PER_HIRE):
        accepted, _ = submit_as(actor, host, BUYER, 42, "quote", symbol="IBM")
        assert accepted["ok"] is True
    full, _ = submit_as(actor, host, BUYER, 42, "quote", symbol="IBM")
    assert full["ok"] is False and "retained request capacity" in full["error"]
    register(actor, host, 43, ATTACKER)
    other, job = submit_as(actor, host, ATTACKER, 43, "quote", symbol="IBM")
    assert other["ok"] is True and job is not None


def test_global_request_capacity_is_bounded(actor, host):
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, b"\x55" * 20), (45, b"\x66" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    accepted_count = 0
    refused = None
    while accepted_count < av.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id, "quote", symbol="IBM")
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == av.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_deadline_expiry_is_error_and_late_callback_is_stale(actor, host):
    accepted, job = submit(actor, host, "quote", symbol="IBM")
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, reply_for(kwargs_of(job)["url"]))
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    first = codec.decode(actor.quote(codec.encode({"hire_id": 42, "symbol": "IBM"})))
    duplicate = codec.decode(actor.overview(codec.encode({"hire_id": 42, "symbol": "IBM"})))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_own_results(actor, host):
    first, first_job = submit(actor, host, "quote", symbol="IBM")
    second, second_job = submit(actor, host, "quote", symbol="MSFT")
    resume(host, second_job, {"Global Quote": {"01. symbol": "MSFT"}})
    resume(host, first_job, {"Global Quote": {"01. symbol": "IBM"}})
    assert read(actor, host, first["request_id"])["output"]["data"]["Global Quote"]["01. symbol"] == "IBM"
    assert read(actor, host, second["request_id"])["output"]["data"]["Global Quote"]["01. symbol"] == "MSFT"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    big = {
        "Meta Data": {"2. Symbol": "IBM"},
        "Time Series (Daily)": {
            "2026-01-%02d" % day: {"4. close": "z" * 400} for day in range(1, 26)
        },
    }
    accepted, _, _ = run_full(actor, host, "daily", result=big, symbol="IBM")
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out)) > av.MAX_RESULT_CHUNK_BYTES
    assert len(chunks[0]["data"]) <= av.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4096


def test_hire_records_are_per_key_idempotent_and_capped(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + (b"\x66" * 20).hex())))
    for hire_id in range(1_000, 1_000 + av.MAX_REGISTERED_HIRES - 1):
        assert register(actor, host, hire_id, BUYER)["ok"] is True
    assert actor.storage[av.KEY_HIRE_COUNT] == av.MAX_REGISTERED_HIRES
    assert actor.storage.get("hire:42") and actor.storage.get("hires") is None
    with pytest.raises(ValueError):
        register(actor, host, 9_999, BUYER)


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


def test_handlers_are_deferred_and_capability_advertises_polling(actor, host):
    handlers = [
        "quote",
        "daily",
        "overview",
        "get_result",
    ]
    for name in handlers:
        assert getattr(getattr(av.AlphaVantageWrapper, name), "_handler_mode", None) == "deferred"
    cap = codec.decode(actor.capability(None))
    assert cap["handlers"] == handlers[:-1]
    assert cap["poll_handler"] == "get_result"
    assert cap["slot"] == "market-data"
    assert cap["returns"] == "alphavantage-async-result"
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            av._bounded_int(bad, "request_id", 1, 1000)
    assert av._bounded_int(7, "request_id", 1, 1000) == 7
    assert av._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    project = Path(__file__).resolve().parent.parent
    script = project / "tools" / "check_alphavantage_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, str(script), "--offline"],
                        cwd=project, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    (work / "actors" / "alphavantage").mkdir(parents=True)
    (work / "tools").mkdir()
    shutil.copy2(project / "actors" / "alphavantage" / "main.py", work / "actors" / "alphavantage" / "main.py")
    shutil.copy2(project / "alphavantage_routes.json", work / "alphavantage_routes.json")
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
    path = work / "alphavantage_routes.json"
    with path.open() as fh:
        data = json.load(fh)
    with path.open("w") as fh:
        json.dump(_bust(data), fh)
    bad = subprocess.run([sys.executable, str(work / "tools" / script.name), "--offline"],
                         cwd=work, capture_output=True, text=True)
    assert bad.returncode != 0
