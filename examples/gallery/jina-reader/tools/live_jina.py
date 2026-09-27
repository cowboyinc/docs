# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4"]
# ///
"""Signed request/result client and optional direct-vs-chain Jina checks.

Run with `uv run tools/live_jina.py --help`. No SDK imports or actor mocks. Each vendor
call is a transaction that returns a request_id; the result is read back through
signed get_result receipt chunks and reassembled into one CBOR value.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import cbor2

READ_ENDPOINT = "https://r.jina.ai"
SEARCH_ENDPOINT = "https://s.jina.ai"
API_ROOT = "https://api.jina.ai"


def request(url, *, key="", method="GET", body=None, timeout=60):
    headers = {"Accept": "application/json"}
    data = None
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if not key:
            raise
        with exc:
            return json.load(exc)


def decode(blob):
    stream = io.BytesIO(blob)
    value = cbor2.CBORDecoder(stream).decode()
    if stream.read(1):
        raise ValueError("trailing bytes after CBOR value")
    return value


def receipt_event(receipt, actor, name):
    if receipt.get("status") != "Success":
        raise ValueError("transaction did not execute successfully")
    matches = []
    for event in receipt.get("events", []):
        if (isinstance(event, list) and len(event) >= 3
                and event[0].lower() == actor.lower() and event[1] == name):
            matches.append(decode(bytes.fromhex(event[2].removeprefix("0x"))))
    if len(matches) != 1:
        raise ValueError("expected one %s event from the actor, found %d" % (name, len(matches)))
    return matches[0]


def remaining(deadline, maximum):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("result wait deadline reached")
    return min(seconds, maximum)


def execute(args, handler, payload, event_name, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + 180
    env = dict(os.environ, COWBOY_RPC_URL=args.rpc, NO_COLOR="1")
    result = subprocess.run(
        [args.cowboy, "actor", "execute", "--actor", args.actor, "--handler", handler,
         "--payload-json", json.dumps(payload), "--private-key", str(args.wallet_key), "--no-wait"],
        check=True, capture_output=True, text=True, env=env, timeout=remaining(deadline, 60))
    match = re.search(r"(?:transaction|tx)\s+hash\s*:\s*(0x[0-9a-fA-F]{64})",
                      result.stdout, re.IGNORECASE)
    if not match:
        raise ValueError("CLI output did not contain a labeled transaction hash")
    print("%s: transaction %s" % (handler, match[1]), file=sys.stderr, flush=True)
    url = args.rpc.rstrip("/") + "/transaction/" + match[1] + "/receipt"
    while time.monotonic() < deadline:
        try:
            receipt = request(url, timeout=remaining(deadline, 60))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            time.sleep(remaining(deadline, 2))
            continue
        return receipt_event(receipt, args.actor, event_name)
    raise TimeoutError("transaction %s receipt did not arrive; inspect it before resubmitting" % match[1])


def get_result(args, request_id, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + args.wait_seconds
    data = bytearray()
    total = None
    while total is None or len(data) < total:
        chunk = execute(args, "get_result",
                        {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": len(data)},
                        "wrapper.result_chunk", deadline=deadline)
        if (chunk.get("request_id") != request_id or chunk.get("hire_id") != args.hire_id
                or chunk.get("offset") != len(data) or not isinstance(chunk.get("data"), bytes)
                or not chunk["data"]):
            raise ValueError("result chunk does not match the requested identity or offset")
        if total is None:
            total = chunk.get("total")
            if type(total) is not int or not 0 < total <= 38_000:
                raise ValueError("invalid encoded result size")
        if chunk.get("total") != total or len(data) + len(chunk["data"]) > total:
            raise ValueError("result size changed between chunks")
        data.extend(chunk["data"])
    result = decode(data)
    if result.get("request_id") != request_id or result.get("hire_id") != args.hire_id:
        raise ValueError("decoded result belongs to a different request")
    return result


def invoke(args, handler, payload):
    accepted = execute(args, handler, dict(payload, hire_id=args.hire_id), "wrapper.request_accepted")
    request_id = accepted.get("request_id")
    if (accepted.get("ok") is not True or accepted.get("status") != "pending"
            or accepted.get("hire_id") != args.hire_id
            or type(request_id) is not int or request_id < 1):
        raise ValueError("invalid request acceptance event")
    print("Accepted request %d for hire %d" % (request_id, args.hire_id), file=sys.stderr, flush=True)
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        result = get_result(args, request_id, deadline=deadline)
        if result["status"] in ("ok", "error"):
            return result
        if result["status"] != "pending":
            raise ValueError("unknown request status")
        time.sleep(remaining(deadline, 4))
    raise TimeoutError("request %d is still pending; reuse its ID instead of resubmitting" % request_id)


# Independent wire expectations. Normal cases must succeed; only the final case
# expects a vendor error. These calls use the same account as the deployed actor.
CASES = [
    ("read", READ_ENDPOINT + "/https://example.com", "GET", None,
     {"url": "https://example.com"}, True),
    ("embeddings", API_ROOT + "/v1/embeddings", "POST",
     {"model": "jina-embeddings-v3", "input": ["cowboy actor network"]},
     {"model": "jina-embeddings-v3", "input": ["cowboy actor network"]}, True),
    ("catalog", API_ROOT + "/v1/models/does-not-exist-000", "GET", None,
     {"operation": "get_model", "model_id": "does-not-exist-000"}, False),
]


def compare(operation, direct, result, expected_success):
    actual_success = isinstance(direct, dict) and "error" not in direct and "detail" not in direct
    if actual_success != expected_success:
        raise ValueError("%s: direct vendor outcome was not the expected one" % operation)
    if result["status"] != ("ok" if expected_success else "error"):
        raise ValueError("%s: actor outcome was not the expected one" % operation)
    output = result["output"]
    if not expected_success:
        if output.get("ok") is not False or not output.get("error"):
            raise ValueError("%s: expected a wrapped error" % operation)
        return
    wrapped = output["data"]
    if operation == "read":
        got = wrapped["data"] if isinstance(wrapped, dict) else (wrapped[0] if wrapped else {})
        if not got.get("content") and not got.get("title"):
            raise ValueError("read returned no content or title")
    elif operation == "embeddings":
        if direct.get("model") != wrapped.get("model"):
            raise ValueError("embedding model differs")
        d = direct.get("data") or []
        w = wrapped.get("data") or []
        if not d or len(d) != len(w):
            raise ValueError("embedding row count differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--wallet-key", type=Path, required=True)
    parser.add_argument("--cowboy", default="cowboy")
    parser.add_argument("--wait-seconds", type=int, default=300)
    commands = parser.add_subparsers(dest="command", required=True)
    call = commands.add_parser("call", help="submit one request and print its exact result")
    call.add_argument("--handler", default="read")
    call.add_argument("--payload-json", required=True)
    read = commands.add_parser("result", help="read an existing request without resubmitting")
    read.add_argument("request_id", type=int)
    check = commands.add_parser("compare", help="paid direct-vs-chain checks")
    check.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "call":
        result = invoke(args, args.handler, json.loads(args.payload_json))
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] != "ok":
            raise SystemExit(1)
    elif args.command == "result":
        print(json.dumps(get_result(args, args.request_id), ensure_ascii=False))
    else:
        key = args.key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("vendor key file is empty")
        for operation, direct_url, method, direct_body, payload, expected in CASES:
            direct = request(direct_url, key=key, method=method, body=direct_body)
            result = invoke(args, operation if operation != "catalog" else "catalog", payload)
            compare(operation, direct, result, expected)
            print("PASS %s: request %d" % (operation, result["request_id"]))


if __name__ == "__main__":
    main()
