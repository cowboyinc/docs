"""Deepgram pre-recorded transcription actor contract checks."""

import json

import pytest
import actors.deepgram.main as deepgram
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec


ATTACKER = b"\x44" * 20
AUDIO = "https://dpgr.am/spacewalk.wav"
BASE = {"hire_id": 42, "url": AUDIO}


def vendor_reply(transcript="one small step for man", request_id="req-abc", confidence=0.987):
    return {
        "metadata": {
            "request_id": request_id,
            "duration": 4.5,
            "models": ["30089e05-99d1-4376-b32e-c263170674af"],
        },
        "results": {
            "channels": [
                {"alternatives": [{"transcript": transcript, "confidence": confidence}]}
            ]
        },
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
    fields = dict(BASE)
    fields.update(payload)
    accepted = codec.decode(actor.transcribe(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender)
    out = deepgram._run_request__resume(
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
    fields = dict(BASE, hire_id=hire_id)
    fields.update(payload)
    accepted = codec.decode(actor.transcribe(codec.encode(fields)))
    return accepted, last_job(host) if accepted.get("ok") else None


def test_dispatch_is_exact_and_result_is_request_scoped(actor, host):
    accepted, job = submit(
        actor,
        host,
        model="nova-2",
        language="en",
        punctuate=True,
        smart_format=True,
        diarize=False,
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "transcribe",
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert read(actor, host, 1)["status"] == "pending"
    kwargs = kwargs_of(job)
    assert kwargs["url"] == (
        deepgram.LISTEN_ENDPOINT
        + "?model=nova-2&language=en&punctuate=true&smart_format=true&diarize=false"
    )
    assert kwargs["method"] == "POST"
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Token ${DEEPGRAM_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": deepgram.USER_AGENT,
    }
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), deepgram.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    assert json.loads(kwargs["raw_body"]) == {"url": AUDIO}
    resume(host, job, vendor_reply(transcript="one small step for man"))
    out = read(actor, host, 1)
    assert out["status"] == "ok"
    assert out["output"]["data"]["transcript"] == "one small step for man"
    assert out["output"]["data"]["confidence"] == 0.987
    assert out["output"]["data"]["duration"] == 4.5
    assert out["output"]["data"]["request_id"] == "req-abc"


def test_default_model_only_query_and_url_body(actor, host):
    _, job = submit(actor, host)
    kwargs = kwargs_of(job)
    assert kwargs["url"] == deepgram.LISTEN_ENDPOINT + "?model=nova-3"
    assert json.loads(kwargs["raw_body"]) == {"url": AUDIO}


def test_query_options_are_percent_encoded_and_bounded(actor, host):
    out = codec.decode(actor.transcribe(codec.encode(dict(BASE, model="not-a-model"))))
    assert out["ok"] is False
    assert not host.get_send_log()
    _, job = submit(actor, host, language="pt-BR")
    assert "language=pt-BR" in kwargs_of(job)["url"]


def test_empty_transcript_is_valid_success(actor, host):
    # Silence transcribes to an empty string — a valid result, not an error.
    accepted, _, _ = run_full(actor, host, result=vendor_reply(transcript=""))
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok"
    assert out["output"]["data"]["transcript"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "ftp://example.com/a.wav"},
        {"url": "not-a-url"},
        {"url": "https://example.com/a b.wav"},
        {"url": 7},
        {"url": "x" * (deepgram.MAX_URL_CHARS + 1)},
        {"model": "whisper-xl"},
        {"language": "en_US!"},
        {"punctuate": "yes"},
        {"smart_format": 1},
        {"diarize": "true"},
        {"unexpected": "value"},
    ],
)
def test_invalid_inputs_refuse_before_dispatch(actor, host, payload):
    fields = dict(BASE)
    fields.update(payload)
    out = codec.decode(actor.transcribe(codec.encode(fields)))
    assert out["ok"] is False
    assert not host.get_send_log()


def test_secret_templates_never_dispatch(actor, host):
    for payload in (
        {"url": "https://x.com/${DEEPGRAM_API_KEY}.wav"},
        {"model": "${DEEPGRAM_API_KEY}"},
        {"language": "${SECRET}"},
        {"url": "https://x.com/a.wav", "language": "en-${X}"},
    ):
        fields = dict(BASE)
        fields.update(payload)
        out = codec.decode(actor.transcribe(codec.encode(fields)))
        assert out["ok"] is False
    assert not host.get_send_log()


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _, _ = run_full(actor, host)
    assert read(actor, host, accepted["request_id"], sender=ATTACKER)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    out = codec.decode(actor.transcribe(codec.encode(dict(BASE, hire_id=99))))
    assert out["ok"] is False
    host.set_context(sender=ATTACKER)
    with pytest.raises(ValueError):
        actor.transcribe(codec.encode(dict(BASE, hire_id=42)))
    assert not host.get_send_log()


@pytest.mark.parametrize(
    "vendor,marker",
    [
        ({"err_code": "Bad Request", "err_msg": "could not process the JSON payload", "request_id": "x"}, "could not process"),
        ({"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials.", "request_id": "x"}, "Invalid credentials"),
        ({"error": "Token could not be found"}, "Token could not be found"),
        ({"err_code": "RATE_LIMIT_EXCEEDED", "err_msg": "Too many requests"}, "Too many requests"),
        ("<html>502 Bad Gateway</html>", "502 Bad Gateway"),
    ],
)
def test_vendor_errors_are_never_false_success(actor, host, vendor, marker):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert marker in out["output"]["error"]
    assert "data" not in out["output"]


@pytest.mark.parametrize(
    "vendor",
    [
        {},
        {"metadata": {"request_id": "x"}},
        {"results": {}},
        {"results": {"channels": []}},
        {"results": {"channels": [{"alternatives": []}]}},
        {"results": {"channels": [{"alternatives": [{"transcript": 7}]}]}},
        {"results": {"channels": [{"alternatives": [{}]}]}},
    ],
)
def test_malformed_success_shapes_are_errors(actor, host, vendor):
    accepted, _, _ = run_full(actor, host, result=vendor)
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False


def test_oversized_transcript_is_bounded_and_truncated(actor, host):
    accepted, _, _ = run_full(
        actor, host, result=vendor_reply(transcript="z" * 200_000)
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "ok" and out["output"]["ok"] is True
    data = out["output"]["data"]
    assert data["truncated"] is True
    assert len(data["transcript"].encode("utf-8")) <= deepgram.TRANSCRIPT_BYTE_BUDGET
    assert len(codec.encode(out)) < deepgram.RETURN_BYTE_BUDGET


def test_oversized_vendor_error_is_bounded(actor, host):
    accepted, _, _ = run_full(
        actor, host, result={"err_code": "Bad Request", "err_msg": "z" * 100_000, "request_id": "x"}
    )
    out = read(actor, host, accepted["request_id"])
    assert out["status"] == "error" and out["ok"] is False
    assert len(out["output"]["error"].encode("utf-8")) <= deepgram.MAX_ERROR_CHARS
    assert len(codec.encode(out)) < deepgram.RETURN_BYTE_BUDGET


def test_duplicate_and_late_callback_rejected(actor, host):
    accepted, _, job = run_full(actor, host)
    assert read(actor, host, accepted["request_id"])["status"] == "ok"
    late = resume(host, job, vendor_reply())
    assert late is None or late.get("ok") is False
    assert read(actor, host, accepted["request_id"])["status"] == "ok"


def test_resume_path_is_not_public(actor, host):
    assert getattr(deepgram.DeepgramTranscribeWrapper._run_request, "_is_continuation", False)
    cap = codec.decode(actor.capability(None))
    for name in cap["handlers"] + [cap["poll_handler"]]:
        assert "resume" not in name and "runner_result" not in name and "callback" not in name
    assert not hasattr(deepgram.DeepgramTranscribeWrapper, "on_runner_result")


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
    assert kwargs["headers"]["Authorization"] == "Token ${DEEPGRAM_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), deepgram.SECRET_KEY]]
    assert "${DEEPGRAM_API_KEY}" not in kwargs["raw_body"].decode("utf-8")


def test_request_capacity_is_per_hire(actor, host):
    for _ in range(deepgram.MAX_RETAINED_PER_HIRE):
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
    while accepted_count < deepgram.MAX_RETAINED_REQUESTS + 1:
        for hire_id, sender in senders.items():
            accepted, _ = submit_as(actor, host, sender, hire_id)
            if not accepted["ok"]:
                refused = accepted
                break
            accepted_count += 1
        if refused:
            break
    assert accepted_count == deepgram.MAX_RETAINED_REQUESTS
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
    first = codec.decode(actor.transcribe(payload))
    duplicate = codec.decode(actor.transcribe(payload))
    assert first["ok"] is True
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]


def test_overlapping_requests_keep_their_results(actor, host):
    first, first_job = submit(actor, host, url="https://x/1.wav")
    second, second_job = submit(actor, host, url="https://x/2.wav")
    resume(host, second_job, vendor_reply(transcript="second", request_id="id-2"))
    resume(host, first_job, vendor_reply(transcript="first", request_id="id-1"))
    assert read(actor, host, first["request_id"])["output"]["data"]["transcript"] == "first"
    assert read(actor, host, second["request_id"])["output"]["data"]["transcript"] == "second"


def test_get_result_receipt_chunk_fits_event_cap(actor, host):
    accepted, _, _ = run_full(actor, host)
    host.events.clear()
    out = read(actor, host, accepted["request_id"], receipt_offset=0)
    chunks = [payload for name, payload in host.events if name == "wrapper.result_chunk"]
    assert out["status"] == "ok" and len(chunks) == 1
    assert chunks[0]["request_id"] == accepted["request_id"]
    assert chunks[0]["total"] == len(codec.encode(out))
    assert len(chunks[0]["data"]) <= deepgram.MAX_RESULT_CHUNK_BYTES
    assert len(codec.encode(chunks[0])) <= 4_096


def test_get_last_output_is_metadata_only(actor, host):
    accepted, _, _ = run_full(
        actor, host, result=vendor_reply(transcript="PRIVATE-TRANSCRIPT")
    )
    assert read(actor, host, accepted["request_id"])["output"]["data"]["transcript"] == "PRIVATE-TRANSCRIPT"
    host.set_context(sender=b"\x77" * 20)
    meta = codec.decode(actor.get_last_output(None))
    assert "data" not in meta and "error" not in meta and "transcript" not in meta
    assert "PRIVATE-TRANSCRIPT" not in codec.encode(meta).decode("latin1")


def test_hire_registration_is_idempotent_and_immutable(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + ATTACKER.hex())))


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


def test_capability_pins_supported_surface(actor):
    cap = codec.decode(actor.capability(None))
    assert cap["vendor"] == "deepgram"
    assert cap["handlers"] == ["transcribe"]
    assert cap["poll_handler"] == "get_result"
    assert cap["hire_activation"] == {"protocol": "store-admin-v1"}
    assert cap["mutating"] is True
    assert cap["idempotency"].startswith("none")
    assert cap["endpoint"] == "POST /v1/listen"
    assert set(cap["input"]) == set(deepgram.SUPPORTED_INPUT_FIELDS)
    assert any("transport cap" in w for w in cap["warnings"])


def test_bounded_int_rejects_floats_and_non_finite():
    for bad in (float("inf"), float("-inf"), float("nan"), 1.5, 3.0, [1], {"a": 1}):
        with pytest.raises((ValueError, TypeError)):
            deepgram._bounded_int(bad, "request_id", 1, 1000)
    assert deepgram._bounded_int(7, "request_id", 1, 1000) == 7
    assert deepgram._bounded_int("7", "request_id", 1, 1000) == 7


def test_parity_drift_guard_runs_under_pytest_and_fires_on_drift(tmp_path):
    # guard must run under pytest and fail on a mutated contract.
    import json, os, shutil, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.dirname(here)
    script = "test_deepgram_parity.py"
    # (1) positive — against the pinned fixture the guard passes.
    ok = subprocess.run([sys.executable, os.path.join(here, script)],
                        cwd=here, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # (2) negative — a corrupted routes fixture makes the guard exit non-zero.
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
