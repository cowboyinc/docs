"""Real-SDK dispatch, callback authorization, retained results and persistence."""

import json
import os
from pathlib import Path
import subprocess
import sys

import actors.sandbox.main as e2b
import pytest
from conftest import ACTOR, BUYER, HIRE, OWNER
from cowboy_sdk import PermissionDeniedError, codec, continuation

SUCCESS = {"ok": True, "operation": "run", "data": {
    "stdout": "hello\n", "stderr": "", "results": [{"text": "42"}],
    "artifacts": [], "error": None}, "error": None}


def tool_reply(output=SUCCESS, **fields):
    # Current runner-mcp executor wire shape, independent of the Actor decoder.
    return {"server": "e2b", "tool": "execute", "is_error": False,
            "content": [{"type": "text", "text": json.dumps(output, separators=(",", ":"))}],
            **fields}


def last_job(host):
    return codec.decode(host.get_send_log()[-1]["payload"])


def submit(actor, host, **payload):
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(actor.run(codec.encode({"hire_id": 42, **payload})))
    return accepted, last_job(host) if accepted.get("ok") else None


def resume(host, job, result, sender=ACTOR):
    host.set_context(sender=sender, tx_hash=(100 + len(host.get_send_log())).to_bytes(32, "big"))
    out = e2b._run_request__resume(codec.encode({"cid": job["cid"],
        "reply_handler": job["reply_handler"], "result": result}))
    return codec.decode(out) if out else None


def read(actor, host, request_id, sender=BUYER, **fields):
    host.set_context(sender=sender)
    return codec.decode(actor.get_result(codec.encode({"request_id": request_id, "hire_id": 42, **fields})))


def finish(actor, host, output=SUCCESS, **payload):
    accepted, job = submit(actor, host, **payload)
    resume(host, job, tool_reply(output))
    return accepted, job


def test_one_mcp_dispatch_and_authenticated_result(actor, host):
    accepted, job = submit(actor, host, code="print(42)")
    assert accepted == {"ok": True, "request_id": 1, "hire_id": 42,
                        "operation": "run", "status": "pending", "poll_handler": "get_result"}
    assert job["job_type"] == "mcp"
    kw = job["payload"]["kwargs"]
    # Node job_envelope accepts server in args[0], but tool only in kwargs.
    # Positional SDK examples therefore do not prove this dispatch contract.
    assert job["payload"]["args"] == []
    assert kw["server"] == "e2b" and kw["tool"] == "execute"
    assert kw["arguments"] == {"api_key": "${E2B_API_KEY}", "code_hex": "7072696e7428343229",
                               "language": "python", "timeout_seconds": 180}
    assert kw["timeout_seconds"] == 26
    assert kw["secrets"] == [["0x" + OWNER.hex(), "E2B_API_KEY"]]
    assert kw["hire"] == {"hire_id": 42, "declaration_id": "0x" + "55" * 32,
                           "version": 3, "artifact_id": 7}
    assert read(actor, host, 1)["status"] == "pending"
    resume(host, job, tool_reply())
    assert len(host.get_send_log()) == 1
    result = read(actor, host, 1)
    assert result["status"] == "ok" and result["output"]["data"] == SUCCESS["data"]


def test_only_request_id_is_saved_and_code_cannot_receive_cbss_key(actor, host):
    code = "print('${E2B_API_KEY} / ${OTHER} / 🌈')"
    accepted, job = submit(actor, host, code=code, language="javascript", timeout=600)
    saved = continuation.load_cont(job["cid"])["ctx"].to_dict()
    assert saved == {"request_id": accepted["request_id"]}
    arguments = job["payload"]["kwargs"]["arguments"]
    rendered = json.loads(json.dumps(arguments).replace("${E2B_API_KEY}", "synthetic-secret"))
    assert rendered["api_key"] == "synthetic-secret"
    assert bytes.fromhex(rendered["code_hex"]).decode() == code
    assert rendered["language"] == "javascript" and rendered["timeout_seconds"] == 600
    assert "sandbox" not in str(saved).lower() and "token" not in str(saved).lower()


@pytest.mark.parametrize("stage", ["create", "execute", "cleanup", "output", "busy"])
def test_provider_failures_finish_once_and_retain_their_stage(actor, host, stage):
    output = {"ok": False, "error": "E2B execution failed", "stage": stage}
    accepted, job = finish(actor, host, output, code="print(1)")
    assert len(host.get_send_log()) == 1
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "error" and result["output"] == dict(output, operation="run")
    assert resume(host, job, tool_reply())["ok"] is False
    assert read(actor, host, accepted["request_id"]) == result


def test_code_error_preserves_structured_details(actor, host):
    error = {"name": "ValueError", "value": "boom", "traceback": "Traceback…"}
    output = {"ok": False, "operation": "run", "data": dict(SUCCESS["data"], error=error),
              "error": "code execution failed", "stage": "execute"}
    accepted, _ = finish(actor, host, output, code="raise ValueError('boom')")
    result = read(actor, host, accepted["request_id"])
    assert result["ok"] is False and result["output"]["data"]["error"] == error


@pytest.mark.parametrize("reply", [
    None, {"sandboxID": "private-sandbox", "envdAccessToken": "private-token"},
    tool_reply(is_error=True), tool_reply(server="other"), tool_reply(tool="other"),
    tool_reply(content=[]), tool_reply(content=[{"type": "text", "text": "<html>502</html>"}]),
    tool_reply(dict(SUCCESS, sandboxID="private-sandbox")),
    tool_reply(dict(SUCCESS, ok=False)),
])
def test_malformed_tool_replies_become_retained_errors(actor, host, reply):
    accepted, job = submit(actor, host, code="print(1)")
    resume(host, job, reply)
    result = read(actor, host, accepted["request_id"])
    assert result["ok"] is False and result["output"]["stage"] == "tool"
    assert "private-token" not in str(result) and "private-sandbox" not in str(result)


def test_tool_must_bound_output_before_callback(actor, host):
    accepted, job = submit(actor, host, code="print('x')")
    resume(host, job, tool_reply(dict(SUCCESS, data=dict(SUCCESS["data"], stdout="x" * 40_000))))
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "error"
    assert len(codec.encode(result)) <= e2b.RETURN_BYTE_BUDGET


def test_binary_names_json_values_and_bounded_result_chunks(actor, host):
    data = dict(SUCCESS["data"], stdout="☃" * 11_000, artifacts=["png"],
                results=[{"json": {"answer": 42}}])
    output = dict(SUCCESS, data=data)
    # Tool serialization uses UTF-8. Escaped Unicode would exceed its JSON budget.
    reply = tool_reply(content=[{"type": "text", "text": json.dumps(output, ensure_ascii=False)}])
    accepted, job = submit(actor, host, code="print('x')")
    resume(host, job, reply)
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "ok"
    assert result["output"]["data"] == data
    encoded = codec.encode(result)
    assert len(encoded) <= e2b.RETURN_BYTE_BUDGET
    assembled = bytearray()
    while len(assembled) < len(encoded):
        host.events.clear()
        read(actor, host, accepted["request_id"], receipt_offset=len(assembled))
        chunks = [value for topic, value in host.events if topic == "wrapper.result_chunk"]
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["request_id"] == accepted["request_id"] and chunk["hire_id"] == 42
        assert chunk["offset"] == len(assembled) and chunk["total"] == len(encoded)
        assembled.extend(chunk["data"])
    assert assembled == encoded


def test_get_result_rejects_wrong_caller_and_hire(actor, host):
    accepted, _ = finish(actor, host, code="print(1)")
    assert read(actor, host, accepted["request_id"], sender=b"\x44" * 20)["ok"] is False
    assert read(actor, host, accepted["request_id"], hire_id=43)["ok"] is False
    assert read(actor, host, accepted["request_id"])["output"]["ok"] is True


def test_unregistered_and_stolen_hires_never_dispatch(actor, host):
    host.set_context(sender=BUYER)
    assert codec.decode(actor.run(codec.encode({"hire_id": 99, "code": "print(1)"})))["ok"] is False
    host.set_context(sender=b"\x44" * 20)
    with pytest.raises(ValueError):
        actor.run(codec.encode({"hire_id": 42, "code": "print(1)"}))
    assert not host.get_send_log()


def test_forged_callback_is_rejected_without_completing_request(actor, host):
    accepted, job = submit(actor, host, code="print(1)")
    for sender in (BUYER, OWNER, b"\x44" * 20):
        with pytest.raises(PermissionDeniedError):
            resume(host, job, tool_reply(), sender=sender)
        assert read(actor, host, accepted["request_id"])["status"] == "pending"


def test_duplicate_and_late_callback_cannot_replace_a_result(actor, host):
    accepted, job = finish(actor, host, code="print(1)")
    original = read(actor, host, accepted["request_id"])
    assert resume(host, job, tool_reply({"ok": False, "error": "late"}))["ok"] is False
    assert read(actor, host, accepted["request_id"]) == original


def test_input_validation(actor, host):
    host.set_context(sender=BUYER)
    for payload in ({"code": " "}, {"code": "x" * 24_001}, {"code": "print(1)", "language": "cobol"}):
        assert codec.decode(actor.run(codec.encode(dict(payload, hire_id=42))))["ok"] is False
    assert not host.get_send_log()


def test_same_transaction_duplicate_request_is_refused(actor, host):
    host.set_context(sender=BUYER, tx_hash=(7).to_bytes(32, "big"))
    assert codec.decode(actor.run(codec.encode({"hire_id": 42, "code": "print(1)"})))["ok"] is True
    duplicate = codec.decode(actor.run(codec.encode({"hire_id": 42, "code": "print(2)"})))
    assert duplicate["ok"] is False and "transaction" in duplicate["error"]
    assert len(host.get_send_log()) == 1


def test_overlapping_requests_finish_out_of_order(actor, host):
    a, first = submit(actor, host, code="print('a')")
    b, second = submit(actor, host, code="print('b')")
    assert a["request_id"] != b["request_id"]
    resume(host, second, tool_reply(dict(SUCCESS, data=dict(SUCCESS["data"], stdout="B"))))
    resume(host, first, tool_reply(dict(SUCCESS, data=dict(SUCCESS["data"], stdout="A"))))
    assert read(actor, host, a["request_id"])["output"]["data"]["stdout"] == "A"
    assert read(actor, host, b["request_id"])["output"]["data"]["stdout"] == "B"


def test_deadline_cleanup_keeps_error_and_fences_late_callback(actor, host):
    accepted, job = submit(actor, host, code="print(1)")
    deadline = read(actor, host, accepted["request_id"])["deadline_height"]
    host.set_block_height(deadline)
    result = read(actor, host, accepted["request_id"])
    assert result["status"] == "error" and "deadline" in result["output"]["error"]
    assert resume(host, job, tool_reply())["ok"] is False
    assert read(actor, host, accepted["request_id"]) == result


def test_full_capacity_retains_results_until_expiry(actor, host):
    first = None
    for _ in range(e2b.MAX_RETAINED_REQUESTS):
        accepted, _ = finish(actor, host, code="x" * e2b.MAX_CODE_BYTES)
        first = first or accepted
    refusal, _ = submit(actor, host, code="print(1)")
    assert refusal["ok"] is False and "capacity" in refusal["error"]
    retained = read(actor, host, first["request_id"])
    assert retained["status"] == "ok"
    host.set_block_height(retained["expires_height"])
    accepted, _ = submit(actor, host, code="print(1)")
    assert accepted["ok"] is True
    assert read(actor, host, first["request_id"])["ok"] is False


def test_hire_registration_guards_conflicts_and_capacity(actor, host):
    host.set_context(sender=OWNER)
    assert codec.decode(actor.register_hire(codec.encode(HIRE)))["ok"] is True
    with pytest.raises(ValueError):
        actor.register_hire(codec.encode(dict(HIRE, hirer="0x" + (b"\x66" * 20).hex())))
    for hire_id in range(100, 100 + e2b.MAX_REGISTERED_HIRES - 1):
        assert codec.decode(actor.register_hire(codec.encode(dict(HIRE, hire_id=hire_id))))["ok"] is True
    with pytest.raises(ValueError, match="capacity"):
        actor.register_hire(codec.encode(dict(HIRE, hire_id=9999)))


def test_capability_states_private_tool_dependency(actor, host):
    cap = codec.decode(actor.capability(None))
    assert cap["handlers"] == ["run"] and cap["poll_handler"] == "get_result"
    assert cap["execution"] == {"server": "e2b", "tool": "execute", "lifecycle_timeout_seconds": 24}


def test_callback_and_read_survive_fresh_processes(actor, host):
    accepted, job = submit(actor, host, code="print(42)")
    state = {"storage": {key.hex(): value.hex() for key, value in host._storage.items()},
             "reply": codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"],
                                    "result": tool_reply()}).hex(),
             "request_id": accepted["request_id"]}
    worker = '''
import json,sys
sys.path.insert(0,sys.argv[1])          # example root: conftest, actors/
sys.path.insert(0,sys.argv[2])          # examples root: _sdk_path
import conftest as fixtures
import actors.sandbox.main as actor
from cowboy_sdk import codec
state=json.load(sys.stdin)
host=fixtures.host_mock
host.reset()
host._storage={bytes.fromhex(k):bytes.fromhex(v) for k,v in state["storage"].items()}
host.set_context(actor_addr=fixtures.ACTOR, sender=fixtures.ACTOR if sys.argv[3]=="resume" else fixtures.BUYER,
                 tx_hash=(999).to_bytes(32,"big"))
if sys.argv[3]=="resume":
    actor._run_request__resume(bytes.fromhex(state["reply"]))
else:
    state["result"]=codec.decode(actor.get_result(codec.encode({"hire_id":42,"request_id":state["request_id"]})))
state["storage"]={k.hex():v.hex() for k,v in host._storage.items()}
print(json.dumps(state))
'''
    for stage in ("resume", "read"):
        example_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, "-c", worker, str(example_root),
                                    str(example_root.parents[1]), stage],
                                   input=json.dumps(state), capture_output=True, text=True, check=True,
                                   env=dict(os.environ), timeout=30)
        state = json.loads(completed.stdout)
    assert state["result"]["status"] == "ok"
    assert state["result"]["output"]["data"] == SUCCESS["data"]
