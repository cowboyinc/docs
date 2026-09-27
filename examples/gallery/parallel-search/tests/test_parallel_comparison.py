#!/usr/bin/env python3
"""Live direct-versus-wrapper Parallel Search differential."""

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
FIXTURE = json.loads((PROJECT / "parallel_routes.json").read_text(encoding="utf-8"))
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
import actors.search.main as parallel
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("PARALLEL_API_KEY_FILE", "/private/tmp/parallel-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_PAYLOAD = {
    "search_queries": ["Parallel Web Systems products"],
    "objective": "Find current information about Parallel Web Systems products.",
    "mode": "turbo",
    "max_results": 3,
    "max_chars_total": 3_000,
    "max_chars_per_result": 1_000,
}


def expected_body(payload):
    body = {
        "search_queries": [str(item).strip() for item in payload["search_queries"]],
        "mode": str(payload.get("mode") or "fast").strip().lower(),
        "max_chars_total": int(payload.get("max_chars_total", 6_000)),
        "advanced_settings": {
            "max_results": int(payload.get("max_results", 5)),
            "excerpt_settings": {
                "max_chars_per_result": min(
                    int(payload.get("max_chars_per_result", 1_200)),
                    int(payload.get("max_chars_total", 6_000)),
                )
            },
        },
    }
    for field in ("objective", "session_id", "client_model"):
        value = payload.get(field)
        if value is not None and str(value).strip():
            body[field] = str(value).strip()
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
            "Content-Type": "application/json",
            "User-Agent": parallel.USER_AGENT,
            FIXTURE["auth"]["name"]: key,
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
    actor = parallel.ParallelSearchWrapper()
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
    accepted = codec.decode(actor.search(codec.encode({"hire_id": 42, **payload})))
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "search",
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
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": parallel.USER_AGENT,
        "x-api-key": "${PARALLEL_API_KEY}",
    }
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "PARALLEL_API_KEY"]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    status, vendor_reply = http(expected, key)
    assert status == 200, "Parallel returned HTTP %d: %r" % (status, vendor_reply)
    assert len(codec.encode(vendor_reply)) <= parallel.RUNNER_DATA_BYTE_BUDGET
    host_mock.set_context(sender=ACTOR)
    parallel._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": vendor_reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "search" and result["status"] == "ok"
    assert result["output"]["data"] == vendor_reply
    return result, vendor_reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    result, vendor_reply = wrapped(key, DEFAULT_PAYLOAD)
    print(
        "PASS: exact Parallel Search wrapper lifecycle (%d results, %d encoded bytes)"
        % (len(vendor_reply["results"]), len(codec.encode(result)))
    )


if __name__ == "__main__":
    main()
