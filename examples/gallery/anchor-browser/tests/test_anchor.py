"""Anchor Browser usage actor contract checks."""

import pytest
import actors.anchor.main as anchor
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20


def billing_reply(credits=1234.5, tier="pro"):
    return {
        "data": {
            "credits": credits,
            "credits_used": 42.0,
            "included_credits": 1000,
            "billing_period": tier,
            "tier": tier,
            "gifts_balance": 0,
            "max_concurrent_browsers": 5,
            "cost_limit": None,
        }
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
        actor.usage(codec.encode({"hire_id": 42, **payload}))
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = anchor._run_request__resume(
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
    final = resume(host, job, billing_reply() if result is None else result)
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
        actor.usage(codec.encode({"hire_id": hire_id, **payload}))
    )
    return accepted, last_job(host) if accepted.get("ok") else None


def test_usage_dispatch_is_get_with_no_body_and_result_is_request_scoped(actor, host):
    accepted, job = submit(actor, host, granularity="day")
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "usage",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == anchor.BILLING_ENDPOINT + "?granularity=day"
    assert kwargs["method"] == "GET"
    # a GET job must omit body AND Content-Type (on-chain E1604 otherwise)
    assert "body" not in kwargs and "raw_body" not in kwargs
    assert "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "anchor-api-key": "${ANCHOR_API_KEY}",
        "User-Agent": anchor.USER_AGENT,
    }
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), anchor.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    resume(host, job, billing_reply())
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == billing_reply()


def test_default_dispatch_has_no_query_string(actor, host):
    _, job = submit(actor, host)
    assert kwargs_of(job)["url"] == anchor.BILLING_ENDPOINT


@pytest.mark.parametrize(
    "payload",
    [
        {"granularity": "hour"},
        {"granularity": "year"},
        {"granularity": 7},
        {"granularity": ""},
        {"from_date": "2026-01-01"},
        {"to_date": "2026-02-01"},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    base = {"hire_id": 42}
    base.update(payload)
    out = codec.decode(actor.usage(codec.encode(base)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_and_oversized_input_never_dispatch(actor, host):
    for payload in (
        {"granularity": "${ANCHOR_API_KEY}"},
        {"unexpected": {"nested": ["${OTHER_SECRET}"]}},
        {"unexpected": "x" * anchor.MAX_REQUEST_BYTES},
    ):
        out = codec.decode(actor.usage(codec.encode({"hire_id": 42, **payload})))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.usage(codec.encode({"hire_id": 99})))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.usage(codec.encode({"hire_id": 42}))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"error": {"code": 401, "message": "invalid api key"}}, "invalid api key"),
        ({"error": {"code": 429, "message": "rate limit exceeded"}}, "rate limit exceeded"),
        ({"error": {"code": 500}}, "500"),
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
        {},
        {"data": []},
        {"data": {"credits_used": 1.0}},
        {"data": {"credits": "lots", "credits_used": 1.0}},
        {"data": {"credits": True, "credits_used": 1.0}},
        {"data": {"credits": 1.0, "credits_used": 1.0, "tier": 7}},
        {"data": {"credits": 1.0, "credits_used": 1.0}, "usage": []},
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, vendor):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


def test_oversized_response_is_bounded(actor, host):
    big = billing_reply()
    big["usage"] = {
        "from_date": "2026-01-01",
        "to_date": "2026-02-01",
        "granularity": "day",
        "total_credits_used": 999.0,
        "data": [
            {"date": "2026-01-%02d" % (i % 28 + 1), "credits_used": i + 0.5}
            for i in range(4_000)
        ],
    }
    accepted, _, _ = run_full(actor, host, result=big)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert len(codec.encode(out)) < anchor.RETURN_BYTE_BUDGET
    assert out["output"]["data"]["_cowboy"]["truncated"] is True


def test_uncompressible_numeric_body_is_error(actor, host):
    body = billing_reply()
    body["data"].update({"n%d" % i: i + 0.123456789 for i in range(5_000)})
    accepted, _, _ = run_full(actor, host, result=body)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, billing_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(anchor.AnchorBrowserWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(anchor.AnchorBrowserWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host)
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, billing_reply(), sender=forged)


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
    assert kwargs["headers"]["anchor-api-key"] == "${ANCHOR_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), anchor.SECRET_KEY]]


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(anchor.MAX_RETAINED_PER_HIRE):
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
    while accepted_count < anchor.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == anchor.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_deadline_expiry_is_terminal_and_late_result_is_stale(actor, host):
    accepted, job = submit(actor, host)
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, billing_reply())
    assert late is None or late.get("ok") is False


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    payload = codec.encode({"hire_id": 42})
    first = codec.decode(actor.usage(payload))
    duplicate = codec.decode(actor.usage(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host)
    second, second_job = submit(actor, host, granularity="week")
    resume(host, second_job, billing_reply(credits=2.0, tier="second"))
    resume(host, first_job, billing_reply(credits=1.0, tier="first"))
    assert read(actor, host, first["request_id"])["output"]["data"]["data"]["tier"] == "first"
    assert read(actor, host, second["request_id"])["output"]["data"]["data"]["tier"] == "second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    big = billing_reply()
    big["data"]["billing_period"] = "z" * 10_000
    accepted, _, _ = run_full(actor, host, result=big)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= anchor.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(actor, host, result=billing_reply(tier="PRIVATE"))
    assert read(actor, host, accepted["request_id"])["output"]["data"]["data"]["tier"] == "PRIVATE"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert "PRIVATE" not in codec.encode(meta).decode("latin1")


def test_top_level_billing_shape_is_accepted(actor, host):
    # The live Anchor API returns the billing summary at the top level (no
    # `data` wrapper), diverging from its own docs. The wrapper must classify
    # that as a valid usage response, not "unexpected usage response".
    reply = {
        "credits": 5,
        "credits_used": 0,
        "included_credits": 0,
        "billing_period": "2026-09",
        "tier": "monthly_free",
        "usage": {"granularity": "day", "total_credits_used": 0, "data": []},
    }
    accepted, _, _ = run_full(actor, host, result=reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    assert out["output"]["data"]["credits"] == 5
    assert out["output"]["data"]["credits_used"] == 0


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "anchor"
    assert cap["handlers"] == ["usage"]
    assert cap["auth_header"] == "anchor-api-key"
    assert cap["granularities"] == ["day", "week", "month"]
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert "POST /v1/tools/fetch/webpage" in cap["excluded_endpoints"]


def test_register_hire_requires_registrar(actor, host):
    # register_hire is the payment/authorization boundary: only the Store
    # registrar set at init may register a hire. A non-registrar caller must be
    # refused, and must leave no state behind (mirrors the reference wrappers).
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError, match="registrar"):
        actor.register_hire(codec.encode({**HIRE, "hire_id": 99}))
    host.set_context(sender=OWNER)
    ok = codec.decode(actor.register_hire(codec.encode({**HIRE, "hire_id": 99})))
    assert ok["ok"] is True and ok["hire_id"] == 99


def test_init_is_one_time(actor, host):
    # The actor fixture already initialized once; a second init must be refused
    # so the registrar/owner cannot be silently reset.
    host.set_context(sender=OWNER)
    with pytest.raises(ValueError, match="already initialized"):
        actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            anchor._bounded_int(bad, "request_id", 1, 1000)
    assert anchor._bounded_int(7, "request_id", 1, 1000) == 7
    assert anchor._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    script = "test_anchor_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script), "--offline"],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
    work = tmp_path / "g"
    project = os.path.dirname(here)
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
    bad = subprocess.run([sys.executable, os.path.join(str(work), "tests", script), "--offline"],
                         cwd=str(work), capture_output=True, text=True)
    assert bad.returncode != 0
