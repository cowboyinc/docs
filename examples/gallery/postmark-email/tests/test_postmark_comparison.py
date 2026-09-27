#!/usr/bin/env python3
"""Live direct-versus-wrapper Postmark email-send differential.

Sends one real email through Postmark, then feeds that exact vendor reply
through the wrapper lifecycle and requires exact output equality. Requires a
Postmark server token and a verified sender signature, so it does not run in
the offline sweep.

Postmark ships a special test token, ``POSTMARK_API_TEST``, that accepts any
well-formed request and returns a canned ``{ErrorCode:0, MessageID:...}`` reply
without actually sending — useful for a dry run against a throwaway payload.

The SDK is loaded via an explicit ``importlib`` spec (NOT ``sys.path.insert`` of
``pvm/Lib``, whose RustPython ``threading`` shim crashes a fresh CPython).
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
FIXTURE = json.loads((PROJECT / "postmark_routes.json").read_text(encoding="utf-8"))
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

import actors.postmark.main as postmark
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("POSTMARK_SERVER_TOKEN_FILE", "/private/tmp/postmark-token.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_PAYLOAD = {
    "From": "Cowboy Store <sender@example.com>",
    "To": "recipient@example.com",
    "Subject": "Cowboy Postmark wrapper smoke test",
    "TextBody": "Sent through the Cowboy Postmark Store wrapper.",
}


def recipients(value):
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return ",".join(str(item).strip() for item in parts if str(item).strip())


def expected_body(payload):
    body = {
        "From": str(payload["From"]).strip(),
        "To": recipients(payload["To"]),
        "Subject": str(payload["Subject"]).strip(),
    }
    if payload.get("HtmlBody"):
        body["HtmlBody"] = payload["HtmlBody"]
    if payload.get("TextBody"):
        body["TextBody"] = payload["TextBody"]
    for field in ("Cc", "Bcc", "ReplyTo"):
        if payload.get(field):
            body[field] = recipients(payload[field])
    if payload.get("MessageStream"):
        body["MessageStream"] = payload["MessageStream"]
    return body


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http(body, token):
    request = urllib.request.Request(
        FIXTURE["endpoint"],
        data=json_bytes(body),
        method="POST",
        headers={
            "Accept": "application/json",
            "X-Postmark-Server-Token": token,
            "Content-Type": "application/json",
            "User-Agent": postmark.USER_AGENT,
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
    actor = postmark.PostmarkEmailWrapper()
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


def wrapped(token, payload):
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
    # Postmark authenticates with a bare token header, NOT Bearer.
    assert kwargs["headers"]["X-Postmark-Server-Token"] == "${POSTMARK_SERVER_TOKEN}"
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["User-Agent"] == postmark.USER_AGENT
    assert kwargs["headers"]["Idempotency-Key"] == postmark.IDEMPOTENCY_PREFIX + job["cid"].hex()
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "POSTMARK_SERVER_TOKEN"]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    status, reply = http(expected, token)
    # Postmark's failure signal is a nonzero ErrorCode, not the HTTP status.
    assert reply.get("ErrorCode") == 0, "Postmark returned HTTP %d: %r" % (status, reply)
    assert isinstance(reply.get("MessageID"), str) and reply["MessageID"]
    host_mock.set_context(sender=ACTOR)
    postmark._run_request__resume(
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
    assert result["output"]["data"]["MessageID"] == reply["MessageID"]
    assert result["output"]["data"]["ErrorCode"] == 0
    return result, reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument(
        "--test-token",
        action="store_true",
        help="use Postmark's POSTMARK_API_TEST dry-run token instead of --key-file",
    )
    parser.add_argument("--from", dest="sender", default=DEFAULT_PAYLOAD["From"])
    parser.add_argument("--to", default=DEFAULT_PAYLOAD["To"])
    parser.add_argument("--subject", default=DEFAULT_PAYLOAD["Subject"])
    args = parser.parse_args()
    if args.test_token:
        token = "POSTMARK_API_TEST"
    else:
        token = open(args.key_file, encoding="utf-8").read().strip()
    payload = dict(
        DEFAULT_PAYLOAD,
        **{"From": args.sender, "To": args.to, "Subject": args.subject},
    )
    result, reply = wrapped(token, payload)
    print("PASS: exact Postmark email-send wrapper lifecycle (MessageID %s)" % reply["MessageID"])


if __name__ == "__main__":
    main()
