#!/usr/bin/env python3
"""Live direct-versus-wrapper Anchor Browser usage differential."""

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
FIXTURE = json.loads((PROJECT / "anchor_routes.json").read_text(encoding="utf-8"))
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

import actors.anchor.main as anchor
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("ANCHOR_API_KEY_FILE", "/private/tmp/anchor-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_PAYLOAD = {"granularity": "day"}


def expected_url(payload):
    query = []
    if "granularity" in payload:
        query.append("granularity=" + str(payload["granularity"]).strip().lower())
    return FIXTURE["endpoint"] + ("?" + "&".join(query) if query else "")


def http(url, key):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "anchor-api-key": key,
            "User-Agent": anchor.USER_AGENT,
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
    actor = anchor.AnchorBrowserWrapper()
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
    accepted = codec.decode(actor.usage(codec.encode({"hire_id": 42, **payload})))
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "usage",
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["url"] == expected_url(payload)
    assert kwargs["method"] == "GET"
    assert "body" not in kwargs and "raw_body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "anchor-api-key": "${ANCHOR_API_KEY}",
        "User-Agent": anchor.USER_AGENT,
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "ANCHOR_API_KEY"]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    status, vendor_reply = http(kwargs["url"], key)
    assert status == 200, "Anchor returned HTTP %d: %r" % (status, vendor_reply)
    assert len(codec.encode(vendor_reply)) <= anchor.RUNNER_DATA_BYTE_BUDGET
    host_mock.set_context(sender=ACTOR)
    anchor._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": vendor_reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "usage" and result["status"] == "ok"
    assert result["output"]["data"] == vendor_reply
    return result, vendor_reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    result, vendor_reply = wrapped(key, DEFAULT_PAYLOAD)
    print(
        "PASS: exact Anchor usage wrapper lifecycle (%d encoded bytes)"
        % len(codec.encode(result))
    )


if __name__ == "__main__":
    main()
