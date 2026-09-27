#!/usr/bin/env python3
"""Live direct-versus-wrapper Kernel browse differential.

`visit` (default): browses one real page through the Kernel `visit` action, then
requires the wrapper's extracted page state to match it exactly.

`--sequence`: drives the stateful `step` lifecycle end-to-end through the wrapper
(goto auto-open -> extract -> close) against the live executor, and separately
runs a DIRECT open -> goto -> extract -> close, requiring the same resolved URL —
a differential over the multi-op session surface.

Uses importlib (not sys.path.insert) to avoid the RustPython threading shim that
shadows the stdlib and crashes the interpreter.
"""

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE = json.loads((ROOT / "kernel_routes.json").read_text(encoding="utf-8"))
SDK = Path(os.environ.get("COWBOY_SDK_PATH", ROOT.parents[3] / "node/pvm/Lib")).resolve()
if "pvm_host" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "pvm_host", SDK / "cowboy_sdk/tests/mock_pvm_host.py"
    )
    host_mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_mock)
    sys.modules["pvm_host"] = host_mock
else:
    host_mock = sys.modules["pvm_host"]
if "cowboy_sdk" not in sys.modules:
    package = SDK / "cowboy_sdk"
    spec = importlib.util.spec_from_file_location(
        "cowboy_sdk", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    sdk = importlib.util.module_from_spec(spec)
    sys.modules["cowboy_sdk"] = sdk
    spec.loader.exec_module(sdk)
sys.path.insert(0, str(ROOT))

import actors.browse.main as kernel
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("KERNEL_API_KEY_FILE", "/private/tmp/kernel-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_URL = kernel.INVOKE_URL
DEFAULT_PAGE_URL = "https://example.com"


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def invocation_body(action_name, step):
    payload = json.dumps(
        step, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return json_bytes({"app_name": kernel.KERNEL_APP, "action_name": action_name, "payload": payload})


def http(endpoint, body, key):
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": kernel.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def vendor_page(reply):
    # Unwrap the /invocations envelope the same way the actor does.
    return kernel._unwrap_invocation(reply)


def direct_step(endpoint, key, step):
    status, reply = http(endpoint, invocation_body(kernel.STEP_ACTION, step), key)
    assert status == 200, "Kernel returned HTTP %d: %r" % (status, reply)
    page = vendor_page(reply)
    assert not kernel._is_vendor_error(page), "direct step %r errored: %r" % (step.get("op"), page)
    return page


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = kernel.KernelBrowseWrapper()
    actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    actor.register_hire(
        codec.encode(
            {
                "hire_id": 42,
                "hirer": "0x" + BUYER.hex(),
                "declaration_id": "0x" + "55" * 32,
                "version": 3,
                "artifact_id": 7,
            }
        )
    )
    return actor


def wrapped_call(actor, handler_name, payload, tx):
    """Submit a visit/step call, dispatch the real HTTP the actor built, feed the
    reply back through the continuation, and return the get_result output."""
    host_mock.set_context(sender=BUYER, tx_hash=(tx).to_bytes(32, "big"))
    handler = getattr(actor, handler_name)
    accepted = codec.decode(handler(codec.encode(payload)))
    assert accepted["ok"] is True, "wrapper refused %r: %r" % (payload, accepted)
    request_id = accepted["request_id"]
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["url"] == kernel.INVOKE_URL
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer ${KERNEL_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "KERNEL_API_KEY"]]
    assert "${KERNEL_API_KEY}" not in kwargs["raw_body"].decode("utf-8")
    key = wrapped_call.key
    status, reply = http(kwargs["url"], kwargs["raw_body"], key)
    assert status == 200, "Kernel returned HTTP %d: %r" % (status, reply)
    host_mock.set_context(sender=ACTOR)
    kernel._run_request__resume(
        codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply})
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": request_id, "hire_id": 42})))
    return request_id, result


def wrapped_visit(key, endpoint, page_url):
    wrapped_call.key = key
    actor = fresh_actor()
    # exact-request assertions for the visit body
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(actor.visit(codec.encode({"hire_id": 42, "url": page_url})))
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "visit",
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["method"] == FIXTURE["method"]
    body = json.loads(kwargs["raw_body"])
    assert body["app_name"] == kernel.KERNEL_APP and body["action_name"] == kernel.VISIT_ACTION
    assert json.loads(body["payload"]) == {"url": page_url}
    assert "Idempotency-Key" not in kwargs["headers"]  # visit is read-only
    status, reply = http(kwargs["url"], kwargs["raw_body"], key)
    assert status == 200, "Kernel returned HTTP %d: %r" % (status, reply)
    page = vendor_page(reply)
    expected = kernel.KernelBrowseWrapper._extract_page(actor, page)
    assert expected is not None, "vendor reply had no page state: %r" % (page,)
    host_mock.set_context(sender=ACTOR)
    kernel._run_request__resume(
        codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply})
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "visit" and result["status"] == "ok"
    assert result["output"]["data"] == expected
    return result, page


def wrapped_step_sequence(key, endpoint, page_url):
    """Wrapper lifecycle goto(auto-open)->extract->close vs a direct
    open->goto->extract->close on the same URL. Compares the resolved URL."""
    wrapped_call.key = key
    actor = fresh_actor()
    # WRAPPED: goto auto-opens a per-hire session (no caller session_id).
    _, goto = wrapped_call(actor, "step", {"hire_id": 42, "op": "goto", "url": page_url}, tx=1)
    assert goto["status"] == "ok" and goto["op"] == "goto"
    session_id = goto["output"]["data"].get("session_id")
    assert isinstance(session_id, str) and session_id, "wrapper goto returned no session id"
    resolved = goto["output"]["data"]["url"]
    # a stored session drives the next op — the caller never supplies session_id.
    _, extract = wrapped_call(actor, "step", {"hire_id": 42, "op": "extract"}, tx=2)
    assert extract["status"] == "ok" and extract["output"]["data"]["url"] == resolved
    _, close = wrapped_call(actor, "step", {"hire_id": 42, "op": "close"}, tx=3)
    assert close["status"] == "ok" and close["output"]["data"].get("closed")

    # DIRECT: run the same lifecycle by hand and require the same resolved URL.
    opened = direct_step(endpoint, key, {"op": "open"})
    sid = opened["session_id"]
    d_goto = direct_step(endpoint, key, {"op": "goto", "session_id": sid, "url": page_url})
    d_extract = direct_step(endpoint, key, {"op": "extract", "session_id": sid})
    direct_step(endpoint, key, {"op": "close", "session_id": sid})
    assert d_goto["url"] == resolved == d_extract["url"], (
        "wrapper %r vs direct goto %r / extract %r" % (resolved, d_goto["url"], d_extract["url"])
    )
    return resolved, session_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--url", default=DEFAULT_URL, help="the /invocations endpoint")
    parser.add_argument("--page-url", default=DEFAULT_PAGE_URL, help="the page to browse")
    parser.add_argument("--sequence", action="store_true", help="run the step lifecycle differential")
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    if args.sequence:
        resolved, session_id = wrapped_step_sequence(key, args.url, args.page_url)
        print("PASS: exact Kernel step lifecycle differential (url %s, session %s)" % (resolved, session_id))
    else:
        result, page = wrapped_visit(key, args.url, args.page_url)
        print(
            "PASS: exact Kernel browse wrapper lifecycle (url %s)"
            % result["output"]["data"].get("url", "?")
        )


if __name__ == "__main__":
    main()
