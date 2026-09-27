"""Steel browser wrapper contract checks (screenshot / pdf)."""

import json

import pytest
import actors.steel.main as steel
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
THIRD = b"\x55" * 20


def vendor_screenshot(url="https://steel-assets.s3.amazonaws.com/shot.png"):
    return {"url": url}


def vendor_pdf(url="https://steel-assets.s3.amazonaws.com/doc.pdf"):
    return {"url": url}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, handler, sender=BUYER, hire_id=42, url="https://example.com", **payload):
    host.set_context(
        sender=sender,
        tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"),
    )
    accepted = codec.decode(
        getattr(actor, handler)(codec.encode({"hire_id": hire_id, "url": url, **payload}))
    )
    return accepted, (last_job(host) if accepted.get("ok") else None)


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = steel._run_request__resume(
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


def run_full(actor, host, handler, result, **payload):
    accepted, job = submit(actor, host, handler, **payload)
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


# --- dispatch shape / routing -------------------------------------------------


def test_screenshot_dispatch_shape(actor, host):
    accepted, job = submit(
        actor, host, "screenshot", url="https://example.com", full_page=True, delay=500, use_proxy=True
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "screenshot",
        "status": "pending",
        "poll_handler": "get_result",
    }
    kwargs = kwargs_of(job)
    assert kwargs["url"] == steel.API_ROOT + "/v1/screenshot"
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        steel.AUTH_HEADER: "${STEEL_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": steel.USER_AGENT,
        steel.IDEMPOTENCY_HEADER: "%s-1" % ("0x" + OWNER.hex()),
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "STEEL_API_KEY"]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert json.loads(kwargs["raw_body"]) == {
        "url": "https://example.com",
        "fullPage": True,
        "delay": 500,
        "useProxy": True,
    }


def test_pdf_dispatch_shape_and_no_full_page(actor, host):
    accepted, job = submit(actor, host, "pdf", url="https://example.com", delay=100, use_proxy=False)
    kwargs = kwargs_of(job)
    assert kwargs["method"] == "POST" and kwargs["url"] == steel.API_ROOT + "/v1/pdf"
    body = json.loads(kwargs["raw_body"])
    assert body == {"url": "https://example.com", "delay": 100, "useProxy": False}
    # full_page is a screenshot-only field; pdf must never carry it
    accepted2, job2 = submit(actor, host, "pdf", url="https://example.com", full_page=True)
    assert "fullPage" not in json.loads(kwargs_of(job2)["raw_body"])


def test_idempotency_key_is_deterministic_not_random(actor, host):
    # derived from the (fixed) request id → a runner retry of the same
    # continuation replays an identical header (vendor-side dedup), never a
    # per-attempt random uuid that would defeat dedup on retry.
    owner = "0x" + OWNER.hex()
    _, job1 = submit(actor, host, "screenshot")
    assert kwargs_of(job1)["headers"][steel.IDEMPOTENCY_HEADER] == "%s-1" % owner
    resume(host, job1, vendor_screenshot())
    _, job2 = submit(actor, host, "pdf")
    assert kwargs_of(job2)["headers"][steel.IDEMPOTENCY_HEADER] == "%s-2" % owner


def test_screenshot_and_pdf_round_trip(actor, host):
    a1, _, _ = run_full(actor, host, "screenshot", vendor_screenshot())
    out1 = read(actor, host, a1["request_id"])
    assert out1["status"] == "ok" and out1["output"]["data"]["url"].endswith(".png")
    a2, _, _ = run_full(actor, host, "pdf", vendor_pdf())
    assert read(actor, host, a2["request_id"])["output"]["data"]["url"].endswith(".pdf")


# --- input validation / secret guard -----------------------------------------


@pytest.mark.parametrize(
    "handler,payload",
    [
        ("screenshot", {"url": "ftp://example.com"}),
        ("screenshot", {"url": "not-a-url"}),
        ("screenshot", {"url": 7}),
        ("screenshot", {"url": "https://example.com", "full_page": 1}),
        ("screenshot", {"url": "https://example.com", "delay": -1}),
        ("screenshot", {"url": "https://example.com", "delay": steel.MAX_DELAY_MS + 1}),
        ("screenshot", {"url": "https://example.com/\nHost: evil"}),
        ("pdf", {"url": "https://example.com/" + "x" * 3000}),
        ("pdf", {"url": "https://example.com", "use_proxy": "yes"}),
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, handler, payload):
    out = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    cases = [
        ("screenshot", {"url": "https://x.com/${STEEL_API_KEY}"}),
        ("pdf", {"url": "https://x.com", "extra": {"nested": ["${OTHER_SECRET}"]}}),
    ]
    for handler, payload in cases:
        out = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_oversized_input_never_dispatches(actor, host):
    out = codec.decode(
        actor.screenshot(
            codec.encode({"hire_id": 42, "url": "https://example.com", "junk": "x" * steel.MAX_REQUEST_BYTES})
        )
    )
    assert out["ok"] is False
    assert not host.get_send_log()


# --- authorization ------------------------------------------------------------


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host, "screenshot", vendor_screenshot())
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.screenshot(codec.encode({"hire_id": 99, "url": "https://example.com"})))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.screenshot(codec.encode({"hire_id": 42, "url": "https://example.com"}))
    assert not host.get_send_log()


# --- vendor error / shape honesty ---------------------------------------------


@pytest.mark.parametrize(
    "handler,vendor,marker",
    [
        ("screenshot", {"statusCode": 401, "error": "Unauthorized", "message": "invalid api key"}, "invalid api key"),
        ("screenshot", {"statusCode": 400, "error": "Bad Request", "message": "bad url"}, "bad url"),
        ("pdf", "<html>gateway timeout</html>", "gateway timeout"),
        ("pdf", {"error": {"message": "render failed"}}, "render failed"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, handler, vendor, marker):
    accepted, _, _ = run_full(actor, host, handler, vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]


@pytest.mark.parametrize(
    "handler,vendor",
    [
        ("screenshot", {"url": "not-a-url"}),
        ("screenshot", {"url": 7}),
        ("pdf", {"path": "/tmp/x.pdf"}),   # no url
        ("pdf", {}),
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, handler, vendor):
    accepted, _, _ = run_full(actor, host, handler, vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "unexpected" in out["output"]["error"]


def test_oversized_response_is_bounded(actor, host):
    big = {"url": "https://steel-assets.s3.amazonaws.com/" + "z" * 80_000}
    accepted, _, _ = run_full(actor, host, "screenshot", big)
    out = read(actor, host, accepted["request_id"])
    assert len(codec.encode(out)) < steel.RETURN_BYTE_BUDGET
    if out["status"] == "ok":
        assert out["output"]["data"]["_cowboy"]["truncated"] is True


# --- callback hygiene ---------------------------------------------------------


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host, "screenshot", vendor_screenshot())
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_screenshot())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_deadline_expiry_is_terminal_and_late_result_is_stale(actor, host):
    accepted, job = submit(actor, host, "screenshot")
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    late = resume(host, job, vendor_screenshot())
    assert late is None or late.get("ok") is False


def test_resume_path_is_not_public(actor, host):
    assert getattr(steel.SteelBrowserWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(steel.SteelBrowserWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host, "screenshot")
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, vendor_screenshot(), sender=forged)


def test_continuation_holds_only_request_id_no_secret(actor, host):
    accepted, job = submit(actor, host, "screenshot")
    continuations = [
        codec.decode(value)
        for key, value in host._storage.items()
        if key.startswith(b"__continuation:")
    ]
    assert len(continuations) == 1
    # only the request id crosses the await — no credential in continuation state
    assert continuations[0].get("ctx") == {"request_id": accepted["request_id"]}
    # the secret rides the outbound job only as a ${...} reference
    assert kwargs_of(job)["headers"][steel.AUTH_HEADER] == "${STEEL_API_KEY}"
    assert kwargs_of(job)["secrets"] == [["0x" + OWNER.hex(), "STEEL_API_KEY"]]


# --- capacity / caps ----------------------------------------------------------


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(steel.MAX_RETAINED_PER_HIRE):
        accepted, _ = submit(actor, host, "screenshot")
        assert accepted["ok"] is True
    full, job = submit(actor, host, "screenshot")
    assert full["ok"] is False and "retained request capacity" in full["error"]
    assert job is None
    register(actor, host, 43, ATTACKER)
    other, other_job = submit(actor, host, "screenshot", sender=ATTACKER, hire_id=43)
    assert other["ok"] is True and other_job is not None


def test_global_request_capacity_is_bounded(actor, host):
    senders = {42: BUYER}
    for hire_id, sender in ((43, ATTACKER), (44, THIRD), (45, b"\x66" * 20)):
        register(actor, host, hire_id, sender)
        senders[hire_id] = sender
    accepted_count = 0
    refused = None
    while accepted_count < steel.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit(actor, host, "screenshot", sender=sender, hire_id=hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == steel.MAX_RETAINED_REQUESTS
    assert refused and "request capacity is full" in refused["error"]


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    payload = codec.encode({"hire_id": 42, "url": "https://example.com"})
    first = codec.decode(actor.screenshot(payload))
    duplicate = codec.decode(actor.screenshot(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, "screenshot", url="https://a.example")
    second, second_job = submit(actor, host, "pdf", url="https://b.example")
    resume(host, second_job, vendor_pdf("https://s3/b.pdf"))
    resume(host, first_job, vendor_screenshot("https://s3/a.png"))
    assert read(actor, host, first["request_id"])["output"]["data"]["url"] == "https://s3/a.png"
    assert read(actor, host, second["request_id"])["output"]["data"]["url"] == "https://s3/b.pdf"


# --- observability / registration --------------------------------------------


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    big = {"url": "https://steel-assets.s3.amazonaws.com/" + "z" * 8_000}
    accepted, _, _ = run_full(actor, host, "screenshot", big)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert len(chunks) == 1
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= steel.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(
        actor, host, "screenshot", vendor_screenshot("https://s3/PRIVATE.png")
    )
    assert "PRIVATE" in read(actor, host, accepted["request_id"])["output"]["data"]["url"]
    host.set_context(sender=THIRD)
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
    assert cap["vendor"] == "steel"
    assert cap["handlers"] == ["screenshot", "pdf"]
    assert cap["poll_handler"] == "get_result"
    assert "scrape" in cap["excluded"] and "sessions" in cap["excluded"]
    assert cap["max_url_chars"] == steel.MAX_URL_CHARS
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}


def test_bounded_int_rejects_floats_and_non_finite():
    # Firecrawl-aligned guard: a float (incl. nan/inf) is a non-(int|str) type and
    # is refused before int() is called, so it never raises an uncaught
    # OverflowError (int(float("inf"))) on a pre-auth id parse.
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            steel._bounded_int(bad, "request_id", 1, 1000)
    assert steel._bounded_int(7, "request_id", 1, 1000) == 7
    assert steel._bounded_int("7", "request_id", 1, 1000) == 7


def test_execute_scrubs_wallet_input_on_subprocess_failure(monkeypatch):
    # Browserbase #372 client scrub: a subprocess failure must not surface argv
    # (the --private-key value) or captured output into the error/traceback.
    import subprocess as _sp, traceback as _tb
    import pytest as _pt
    from types import SimpleNamespace as _NS
    import test_steel_canyon as _c
    secret = "synthetic-wallet-secret-do-not-print"
    a = _NS(cowboy="cowboy", actor="0xactor", rpc="http://rpc.local", wallet_key=secret)
    def _boom(command, **kwargs):
        raise _sp.CalledProcessError(1, command, output=secret, stderr=secret)
    monkeypatch.setattr(_c.subprocess, "run", _boom)
    with _pt.raises(RuntimeError) as err:
        _c.execute(a, "get_result", {"request_id": 1})
    assert secret not in "".join(_tb.format_exception(err.value))


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # Firecrawl parity: the offline contract/route drift guard must actually RUN
    # under pytest (it shipped as a standalone script only) and must FIRE on a
    # mutated contract rather than silently pass.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    script = "test_steel_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
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
    bad = subprocess.run([sys.executable, os.path.join(str(work), "tests", script)],
                         cwd=str(work), capture_output=True, text=True)
    assert bad.returncode != 0
