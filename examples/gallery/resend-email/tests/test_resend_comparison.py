#!/usr/bin/env python3
"""Live direct-versus-wrapper Resend email-send differential.

Sends one real email through Resend, then feeds that exact vendor reply through
the wrapper lifecycle and requires exact output equality. Requires a Resend key
and a verified `from` domain, so it does not run in the offline sweep.
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
PROJECT = HERE.parent
FIXTURE = json.loads((PROJECT / "resend_routes.json").read_text(encoding="utf-8"))
SDK = Path(os.environ.get("COWBOY_SDK_PATH", PROJECT.parents[3] / "node/pvm/Lib")).resolve()
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
sys.path.insert(0, str(PROJECT))

import actors.resend.main as resend
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("RESEND_API_KEY_FILE", "/private/tmp/resend-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_PAYLOAD = {
    "from": "Cowboy Store <onboarding@resend.dev>",
    "to": "delivered@resend.dev",
    "subject": "Cowboy Resend wrapper smoke test",
    "text": "Sent through the Cowboy Resend Store wrapper.",
}


def recipients(value):
    values = [value] if isinstance(value, str) else list(value)
    return [str(item).strip() for item in values]


def expected_body(payload):
    body = {
        "from": str(payload["from"]).strip(),
        "to": recipients(payload["to"]),
        "subject": str(payload["subject"]).strip(),
    }
    if payload.get("html"):
        body["html"] = payload["html"]
    if payload.get("text"):
        body["text"] = payload["text"]
    for field in ("cc", "bcc", "reply_to"):
        if payload.get(field):
            body[field] = recipients(payload[field])
    return body


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http(body, key):
    request = urllib.request.Request(
        FIXTURE["endpoint"],
        data=json_bytes(body),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": resend.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = resend.ResendEmailWrapper()
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


def wrapped(key, payload):
    actor = fresh_actor()
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(actor.send(codec.encode({"hire_id": 42, **payload})))
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "send",
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    expected = expected_body(payload)
    assert kwargs["url"] == FIXTURE["endpoint"]
    assert kwargs["method"] == FIXTURE["method"]
    assert kwargs["raw_body"] == json_bytes(expected)
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer ${RESEND_API_KEY}"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["User-Agent"] == resend.USER_AGENT
    assert kwargs["headers"]["Idempotency-Key"] == resend.IDEMPOTENCY_PREFIX + job["cid"].hex()
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "RESEND_API_KEY"]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    status, reply = http(expected, key)
    assert status == 200, "Resend returned HTTP %d: %r" % (status, reply)
    assert isinstance(reply.get("id"), str) and reply["id"]
    host_mock.set_context(sender=ACTOR)
    resend._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(
        actor.get_result(codec.encode({"request_id": 1, "hire_id": 42}))
    )
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "send" and result["status"] == "ok"
    assert result["output"]["data"] == {"id": reply["id"]}
    return result, reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--from", dest="sender", default=DEFAULT_PAYLOAD["from"])
    parser.add_argument("--to", default=DEFAULT_PAYLOAD["to"])
    parser.add_argument("--subject", default=DEFAULT_PAYLOAD["subject"])
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    payload = dict(DEFAULT_PAYLOAD, **{"from": args.sender, "to": args.to, "subject": args.subject})
    result, reply = wrapped(key, payload)
    print("PASS: exact Resend email-send wrapper lifecycle (email id %s)" % reply["id"])


if __name__ == "__main__":
    main()
