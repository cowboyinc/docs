#!/usr/bin/env python3
"""Live direct-versus-wrapper Reducto parse differential.

Parses one document directly and through the wrapper and requires exact output
equality. Requires a Reducto key, so it does not run in the offline sweep.
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
FIXTURE = json.loads((PROJECT / "reducto_routes.json").read_text(encoding="utf-8"))
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

import actors.reducto.main as reducto
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("REDUCTO_API_KEY_FILE", "/private/tmp/reducto-key.txt")
DEFAULT_DOC = os.environ.get(
    "REDUCTO_DOCUMENT_URL", "https://pdfobject.com/pdf/sample.pdf"
)
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


def expected_body(document_url):
    return {"input": str(document_url).strip(), "settings": {"force_url_result": True}}


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
            "User-Agent": reducto.USER_AGENT,
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


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = reducto.ReductoParseWrapper()
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


def wrapped(key, document_url):
    actor = fresh_actor()
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.parse(codec.encode({"hire_id": 42, "document_url": document_url}))
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "parse",
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    expected = expected_body(document_url)
    assert kwargs["url"] == FIXTURE["endpoint"]
    assert kwargs["method"] == FIXTURE["method"]
    assert kwargs["raw_body"] == json_bytes(expected)
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer ${REDUCTO_API_KEY}"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["User-Agent"] == reducto.USER_AGENT
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "REDUCTO_API_KEY"]]

    status, reply = http(expected, key)
    assert status == 200, "Reducto returned HTTP %d: %r" % (status, reply)
    result = reply.get("result")
    assert isinstance(result, dict) and result.get("type") == "url", (
        "expected a forced URL result, got: %r" % result
    )
    assert isinstance(result.get("url"), str) and result["url"]

    host_mock.set_context(sender=ACTOR)
    reducto._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    out = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert out["request_id"] == 1 and out["hire_id"] == 42
    assert out["operation"] == "parse" and out["status"] == "ok"
    data = out["output"]["data"]
    assert data["result_type"] == "url"
    assert data["result_url"] == result["url"]
    return out, reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--document-url", default=DEFAULT_DOC)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    out, reply = wrapped(key, args.document_url)
    print("PASS: exact Reducto parse wrapper lifecycle (result_id %s)" % reply["result"].get("result_id"))


if __name__ == "__main__":
    main()
