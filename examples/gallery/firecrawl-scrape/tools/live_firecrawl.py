# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4"]
# ///
"""Signed request/result client and optional direct-vs-chain Firecrawl checks.

Run with `uv run live_firecrawl.py --help`. No SDK imports or actor mocks.
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
        # Transaction receipts use 404 while pending. Vendor errors stay visible.
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
        if (
            isinstance(event, list)
            and len(event) >= 3
            and event[0].lower() == actor.lower()
            and event[1] == name
        ):
            matches.append(decode(bytes.fromhex(event[2].removeprefix("0x"))))
    if len(matches) != 1:
        raise ValueError(
            f"expected one {name} event from the actor, found {len(matches)}"
        )
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
        [
            args.cowboy,
            "actor",
            "execute",
            "--actor",
            args.actor,
            "--handler",
            handler,
            "--payload-json",
            json.dumps(payload),
            "--private-key",
            str(args.wallet_key),
            "--no-wait",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=remaining(deadline, 60),
    )
    match = re.search(
        r"(?:transaction|tx)\s+hash\s*:\s*(0x[0-9a-fA-F]{64})",
        result.stdout,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("CLI output did not contain a labeled transaction hash")
    print(f"{handler}: transaction {match[1]}", file=sys.stderr, flush=True)
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
    raise TimeoutError(
        f"transaction {match[1]} receipt did not arrive; inspect it before resubmitting"
    )


def get_result(args, request_id, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + args.wait_seconds
    data = bytearray()
    total = None
    while total is None or len(data) < total:
        chunk = execute(
            args,
            "get_result",
            {
                "hire_id": args.hire_id,
                "request_id": request_id,
                "receipt_offset": len(data),
            },
            "wrapper.result_chunk",
            deadline=deadline,
        )
        if (
            chunk.get("request_id") != request_id
            or chunk.get("hire_id") != args.hire_id
            or chunk.get("offset") != len(data)
            or not isinstance(chunk.get("data"), bytes)
            or not chunk["data"]
        ):
            raise ValueError(
                "result chunk does not match the requested identity or offset"
            )
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
    accepted = execute(
        args, handler, dict(payload, hire_id=args.hire_id), "wrapper.request_accepted"
    )
    request_id = accepted.get("request_id")
    if (
        accepted.get("ok") is not True
        or accepted.get("status") != "pending"
        or accepted.get("hire_id") != args.hire_id
        or type(request_id) is not int
        or request_id < 1
    ):
        raise ValueError("invalid request acceptance event")
    print(
        f"Accepted request {request_id} for hire {args.hire_id}",
        file=sys.stderr,
        flush=True,
    )
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        result = get_result(args, request_id, deadline=deadline)
        if result["status"] in ("ok", "error"):
            return result
        if result["status"] != "pending":
            raise ValueError("unknown request status")
        time.sleep(remaining(deadline, 4))
    raise TimeoutError(
        f"request {request_id} is still pending; reuse its ID instead of resubmitting"
    )


# Independent wire expectations. Normal cases must succeed; only the final case
# expects a vendor error. These calls use the same account as the deployed actor.
CASES = [
    (
        "scrape",
        "POST",
        "/scrape",
        {"url": "https://example.com", "formats": ["markdown"]},
        True,
    ),
    ("map", "POST", "/map", {"url": "https://example.com", "limit": 5}, True),
    ("credit_usage", "GET", "/team/credit-usage", {}, True),
    (
        "get_crawl",
        "GET",
        "/crawl/00000000-0000-4000-8000-000000000000",
        {"id": "00000000-0000-4000-8000-000000000000"},
        False,
    ),
]


def compare(operation, direct, result, expected_success):
    actual_success = isinstance(direct, dict) and direct.get("success") is True
    if actual_success != expected_success:
        raise ValueError(f"{operation}: direct vendor outcome was not the expected one")
    if result["status"] != ("ok" if expected_success else "error"):
        raise ValueError(f"{operation}: actor outcome was not the expected one")
    output = result["output"]
    wrapped = output["data"]
    if not expected_success:
        if direct.get("error") != wrapped.get("error") or not direct.get("error"):
            raise ValueError("vendor errors differ")
    elif operation == "scrape":
        if not direct["data"].get("markdown") or direct["data"]["markdown"] != wrapped[
            "data"
        ].get("markdown"):
            raise ValueError("scraped markdown differs")
    elif operation == "map":
        urls = lambda reply: {
            item["url"] if isinstance(item, dict) else item
            for item in reply.get("links", [])
        }
        if not urls(direct) or urls(direct) != urls(wrapped):
            raise ValueError("mapped URLs differ")
    else:
        # Credit usage can change between calls. Validate independent documented fields.
        for reply in (direct, wrapped):
            if reply.get("success") is not True or type(
                reply.get("data", {}).get("remainingCredits")
            ) not in (int, float):
                raise ValueError("credit usage response is malformed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--wallet-key", type=Path, required=True)
    parser.add_argument("--cowboy", default="cowboy")
    parser.add_argument("--wait-seconds", type=int, default=300)
    commands = parser.add_subparsers(dest="command", required=True)
    call = commands.add_parser(
        "call", help="submit one request and print its exact result"
    )
    call.add_argument("--handler", choices=("scrape", "api"), default="api")
    call.add_argument("--payload-json", required=True)
    read = commands.add_parser(
        "result", help="read an existing request without submitting it again"
    )
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
        for operation, method, path, payload, expected in CASES:
            direct = request(
                "https://api.firecrawl.dev/v2" + path,
                key=key,
                method=method,
                body=payload if method == "POST" else None,
            )
            result = invoke(args, "api", dict(payload, operation=operation))
            compare(operation, direct, result, expected)
            print(f"PASS {operation}: request {result['request_id']}")


if __name__ == "__main__":
    main()
