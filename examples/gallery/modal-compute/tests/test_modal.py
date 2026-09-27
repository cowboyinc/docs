"""Modal compute-endpoint actor contract checks."""

import base64
import json

import pytest
import actors.modal.main as modal_compute
from conftest import ACTOR, BUYER, GET_ENDPOINT, HIRE, OWNER, POST_ENDPOINT
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
BASE = {"hire_id": 42, "endpoint": "compute", "payload": {"n": 21}}


def vendor_reply(value=None):
    return {"square": 441} if value is None else value


def non_json_reply(text="modal-http: missing credentials for proxy authorization"):
    return {"raw": base64.b64encode(text.encode()).decode(), "content_type": "text/plain"}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def kwargs_of(job):
    return job["payload"]["kwargs"]


def submit(actor, host, **payload):
    host.set_context(
        sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big")
    )
    fields = dict(BASE)
    fields.update(payload)
    accepted = codec.decode(actor.invoke(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = modal_compute._run_request__resume(
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


def register_endpoint(actor, host, sender=OWNER, **cfg):
    host.set_context(sender=sender)
    return codec.decode(actor.register_endpoint(codec.encode(cfg)))


def submit_as(actor, host, sender, hire_id, **payload):
    host.set_context(
        sender=sender, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big")
    )
    fields = dict(BASE, hire_id=hire_id)
    fields.update(payload)
    accepted = codec.decode(actor.invoke(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def test_post_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(actor, host, payload={"n": 21})
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "invoke",
        "endpoint": "compute",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == POST_ENDPOINT["url"]
    assert kwargs["method"] == "POST"
    assert "timeout_ms" not in kwargs and "body" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Modal-Key": "${MODAL_TOKEN_ID}",
        "Modal-Secret": "${MODAL_TOKEN_SECRET}",
        "Idempotency-Key": modal_compute.IDEMPOTENCY_PREFIX + job["cid"].hex(),
        "User-Agent": modal_compute.USER_AGENT,
    }
    assert kwargs["secrets"] == [
        ["0x" + OWNER.hex(), "MODAL_TOKEN_ID"],
        ["0x" + OWNER.hex(), "MODAL_TOKEN_SECRET"],
    ]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert json.loads(kwargs["raw_body"]) == {"n": 21}
    resume(host, job, vendor_reply({"square": 441}))
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"] == {"square": 441}


def test_get_endpoint_omits_body_and_encodes_query(actor, host):
    accepted, job = submit(
        actor, host, endpoint="status", payload=None, query={"job": "abc 1", "n": 3}
    )
    assert accepted["ok"] is True and accepted["endpoint"] == "status"
    kwargs = kwargs_of(job)
    assert kwargs["method"] == "GET"
    # E1604: a GET job must omit the body kwarg entirely (and raw_body).
    assert "body" not in kwargs and "raw_body" not in kwargs
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["url"].startswith(GET_ENDPOINT["url"] + "?")
    assert "job=abc%201" in kwargs["url"] and "n=3" in kwargs["url"]
    resume(host, job, vendor_reply({"ready": True}))
    assert read(actor, host, accepted["request_id"])["output"]["data"] == {"ready": True}


def test_default_post_payload_is_empty_object(actor, host):
    fields = {"hire_id": 42, "endpoint": "compute"}
    host.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(actor.invoke(codec.encode(fields)))
    assert accepted["ok"] is True
    assert kwargs_of(last_job(host))["raw_body"] == b"{}"


def test_idempotency_key_is_stable_and_unique_per_request(actor, host):
    _, first = submit(actor, host)
    _, second = submit(actor, host, payload={"n": 22})
    first_key = kwargs_of(first)["headers"]["Idempotency-Key"]
    second_key = kwargs_of(second)["headers"]["Idempotency-Key"]
    assert first_key == modal_compute.IDEMPOTENCY_PREFIX + first["cid"].hex()
    assert first_key != second_key


@pytest.mark.parametrize(
    "payload",
    [
        {"endpoint": ""},
        {"endpoint": 7},
        {"endpoint": "ghost"},
        {"endpoint": "bad label"},
        {"payload": 7},
        {"payload": "not-an-object"},
        {"endpoint": "status", "payload": {"n": 1}},  # payload on GET endpoint
        {"query": {"a": 1}},  # query on POST endpoint
        {"endpoint": "status", "payload": None, "query": {"a": {"nested": 1}}},
        {"endpoint": "status", "payload": None, "query": {"a": [1, 2]}},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(BASE)
    fields.update(payload)
    out = codec.decode(actor.invoke(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_over_budget_payload_never_dispatches(actor, host):
    over = dict(BASE, payload={"blob": "x" * (modal_compute.MAX_REQUEST_BYTES + 10)})
    assert codec.decode(actor.invoke(codec.encode(over)))["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    for payload in (
        {"payload": {"cmd": "${MODAL_TOKEN_SECRET}"}},
        {"payload": {"${MODAL_TOKEN_ID}": "x"}},
        {"endpoint": "${SECRET}"},
        {"endpoint": "status", "payload": None, "query": {"q": "${MODAL_TOKEN_SECRET}"}},
    ):
        fields = dict(BASE)
        fields.update(payload)
        out = codec.decode(actor.invoke(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.invoke(codec.encode(dict(BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.invoke(codec.encode(dict(BASE, hire_id=42)))
    assert not host.get_send_log()


def test_non_json_reply_is_error_with_decoded_reason(actor, host):
    accepted, _, _ = run_full(actor, host, result=non_json_reply())
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "missing credentials" in out["output"]["error"]
    assert "data" not in out["output"]


def test_invalid_json_transport_reply_is_error(actor, host):
    accepted, _, _ = run_full(
        actor,
        host,
        result={
            "raw": base64.b64encode(b"{not-json").decode(),
            "content_type": "application/json",
        },
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and "{not-json" in out["output"]["error"]


@pytest.mark.parametrize(
    "reply",
    [
        {"raw": "", "content_type": "text/plain"},
        {"raw": "", "content_type": ""},
        "",
    ],
)
def test_non_json_empty_replies_are_errors(actor, host, reply):
    accepted, _, _ = run_full(actor, host, result=reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_none_delivery_is_error_not_false_success(actor, host):
    accepted, job = submit(actor, host)
    resume(host, job, None)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert "empty result" in out["output"]["error"]


def test_json_body_is_returned_verbatim(actor, host):
    accepted, _, _ = run_full(actor, host, result={"result": [1, 2, 3], "meta": {"ok": 1}})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"] == {"result": [1, 2, 3], "meta": {"ok": 1}}


@pytest.mark.parametrize(
    "reply",
    [
        {"raw": "application-owned"},
        {"raw": "application-owned", "content_type": "application/json", "kind": "application"},
        {"raw_text": "application-owned"},
        {"error": "application-owned", "message": "still application data"},
        {"__transport_non_json__": "application-owned"},
    ],
)
def test_application_json_fields_are_returned_verbatim(actor, host, reply):
    accepted, _, _ = run_full(actor, host, result=reply)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"] == reply


def test_oversized_reply_is_bounded_to_error(actor, host):
    accepted, _, _ = run_full(actor, host, result={"blob": "z" * 100_000})
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert len(codec.encode(out)) < modal_compute.RETURN_BYTE_BUDGET


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(modal_compute.ModalComputeWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(modal_compute.ModalComputeWrapper, "on_runner_result")


def test_forged_callback_rejected(actor, host):
    _, job = submit(actor, host)
    for forged in (BUYER, OWNER, ATTACKER):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, vendor_reply(), sender=forged)


def test_continuation_and_job_carry_no_resolved_credential(actor, host):
    accepted, job = submit(actor, host)
    continuations = [
        codec.decode(value)
        for key, value in host._storage.items()
        if key.startswith(b"__continuation:")
    ]
    assert len(continuations) == 1
    assert continuations[0].get("ctx") == {"request_id": accepted["request_id"]}
    kwargs = kwargs_of(job)
    # Only ${...} references ride the job; no resolved token value appears.
    assert kwargs["headers"]["Modal-Key"] == "${MODAL_TOKEN_ID}"
    assert kwargs["headers"]["Modal-Secret"] == "${MODAL_TOKEN_SECRET}"
    for value in kwargs["headers"].values():
        assert not value.startswith("wk-") and not value.startswith("ws-")
    assert "${MODAL_TOKEN_SECRET}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(modal_compute.MAX_RETAINED_PER_HIRE):
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
    while accepted_count < modal_compute.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == modal_compute.MAX_RETAINED_REQUESTS
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
    payload = codec.encode(BASE)
    first = codec.decode(actor.invoke(payload))
    duplicate = codec.decode(actor.invoke(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, payload={"n": 1})
    second, second_job = submit(actor, host, payload={"n": 2})
    resume(host, second_job, vendor_reply({"square": 4}))
    resume(host, first_job, vendor_reply({"square": 1}))
    assert read(actor, host, first["request_id"])["output"]["data"] == {"square": 1}
    assert read(actor, host, second["request_id"])["output"]["data"] == {"square": 4}


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    accepted, _, _ = run_full(actor, host)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= modal_compute.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(actor, host, result={"secret_number": 424242})
    assert read(actor, host, accepted["request_id"])["output"]["data"] == {"secret_number": 424242}
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta
    assert "424242" not in codec.encode(meta).decode("latin1")


def test_register_hire_requires_registrar(actor, host):
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError, match="registrar"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=99)))
    host.set_context(sender=OWNER)
    ok = codec.decode(actor.register_hire(codec.encode(dict(HIRE, hire_id=99))))
    assert ok["ok"] is True and ok["hire_id"] == 99


def test_init_is_one_time(actor, host):
    host.set_context(sender=OWNER)
    with pytest.raises(ValueError, match="initialized"):
        actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


def test_register_endpoint_is_registrar_only(actor, host):
    host.set_context(sender=BUYER)
    with pytest.raises(ValueError):
        actor.register_endpoint(
            codec.encode({"label": "extra", "url": "https://x--y.modal.run", "method": "POST"})
        )
    ok = register_endpoint(actor, host, label="extra", url="https://x--y.modal.run", method="POST")
    assert ok["ok"] is True and ok["method"] == "POST"
    labels = {e["label"] for e in codec.decode(actor.list_endpoints(None))["endpoints"]}
    assert {"compute", "status", "extra"} <= labels


@pytest.mark.parametrize(
    "url",
    [
        "http://x--y.modal.run",
        "https://evil.com",
        "https://x.modal.run.evil.com",
        "https://x.modal.run@evil.com",
        "https://x--y.modal.run?leak=1",
        "https://x--y.modal.run:8443",
        "https://x--y.modal.run\\evil",
        "https://evil.com%2Fx--y.modal.run",
        "https://x--y.modal.run%40evil.modal.run",
        "ftp://x--y.modal.run",
        "https://.modal.run",
        "not-a-url",
    ],
)
def test_register_endpoint_rejects_non_modal_hosts(actor, host, url):
    with pytest.raises(ValueError):
        register_endpoint(actor, host, label="bad", url=url, method="GET")


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "modal"
    assert cap["handlers"] == ["invoke"]
    assert cap["poll_handler"] == "get_result"
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert cap["mutating"] is True
    assert cap["auth"]["headers"] == ["Modal-Key", "Modal-Secret"]
    assert cap["endpoint_host_suffix"] == ".modal.run"
    assert "Sandbox" in cap["unsupported"]
    assert set(cap["input"]) == set(modal_compute.SUPPORTED_INPUT_FIELDS)


def test_bounded_int_rejects_floats_and_non_finite():
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            modal_compute._bounded_int(bad, "request_id", 1, 1000)
    assert modal_compute._bounded_int(7, "request_id", 1, 1000) == 7
    assert modal_compute._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.dirname(here)
    script = "test_modal_parity.py"
    # (1) positive: pinned fixture passes
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative: corrupted fixture fails
    work = tmp_path / "g"
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
