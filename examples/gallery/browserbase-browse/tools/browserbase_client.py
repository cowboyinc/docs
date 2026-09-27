# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4"]
# ///
"""Signed request/result delivery for the Browserbase demo and live checks."""

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

import cbor2


HANDLERS = ("fetch", "search", "browse", "browse_result")


def decode(raw):
    stream = io.BytesIO(raw)
    value = cbor2.CBORDecoder(stream).decode()
    if stream.read(1):
        raise ValueError("trailing bytes after CBOR value")
    return value


def receipt_event(receipt, actor, name):
    if not isinstance(receipt, dict) or receipt.get("status") != "Success":
        raise ValueError("transaction did not execute successfully")
    matches = []
    for event in receipt.get("events", []):
        if (isinstance(event, list) and len(event) == 3
                and isinstance(event[0], str) and event[0].lower() == actor.lower()
                and event[1] == name):
            value = decode(bytes.fromhex(event[2].removeprefix("0x")))
            if not isinstance(value, dict):
                raise ValueError("event payload is not an object")
            matches.append(value)
    if len(matches) != 1:
        raise ValueError(f"expected one {name} event from the actor")
    return matches[0]


def remaining(deadline, maximum):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("request wait deadline reached; reuse the accepted request ID")
    return min(seconds, maximum)


def execute(args, handler, payload, event_name, deadline):
    timeout = remaining(deadline, 60)
    try:
        result = subprocess.run(
            [args.cowboy, "actor", "execute", "--actor", args.actor,
             "--handler", handler, "--payload", "0x" + cbor2.dumps(payload, canonical=True).hex(),
             "--private-key", args.wallet_key, "--no-wait"],
            check=True, capture_output=True, text=True,
            env=dict(os.environ, COWBOY_RPC_URL=args.rpc, NO_COLOR="1"),
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        # Subprocess exceptions include argv and captured output, which can
        # contain wallet inputs. Keep those details out of the error report.
        raise RuntimeError("actor execute failed or timed out; inspect the wallet transaction before retrying") from None
    hashes = re.findall(r"^\s*(?:Transaction|Tx) hash:\s*(0x[0-9a-fA-F]{64})\s*$",
                        result.stdout, re.MULTILINE | re.IGNORECASE)
    if len(hashes) != 1:
        raise ValueError("CLI output must contain one labeled transaction hash")
    print(f"{handler}: transaction {hashes[0]}", file=sys.stderr, flush=True)
    url = args.rpc.rstrip("/") + "/transaction/" + hashes[0] + "/receipt"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=remaining(deadline, 15)) as response:
                receipt = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            time.sleep(remaining(deadline, 2))
        else:
            return receipt_event(receipt, args.actor, event_name)


def get_result(args, request_id, operation=None, *, deadline=None):
    """Read one exact result snapshot through signed receipt chunks."""
    deadline = time.monotonic() + args.wait_seconds if deadline is None else deadline
    data = bytearray()
    total = None
    while total is None or len(data) < total:
        chunk = execute(args, "get_result", {
            "request_id": request_id, "hire_id": args.hire_id, "receipt_offset": len(data),
        }, "wrapper.result_chunk", deadline)
        if (type(chunk.get("request_id")) is not int or chunk["request_id"] != request_id
                or type(chunk.get("hire_id")) is not int or chunk["hire_id"] != args.hire_id
                or type(chunk.get("offset")) is not int or chunk["offset"] != len(data)
                or not isinstance(chunk.get("data"), bytes)
                or not 0 < len(chunk["data"]) <= 3_900):
            raise ValueError("result chunk has the wrong identity, offset, or data")
        if total is None:
            total = chunk.get("total")
            if type(total) is not int or not 0 < total <= 38_000:
                raise ValueError("invalid encoded result size")
        if type(chunk.get("total")) is not int or chunk["total"] != total or len(data) + len(chunk["data"]) > total:
            raise ValueError("result size changed during delivery")
        data.extend(chunk["data"])
    result = decode(data)
    if (not isinstance(result, dict) or type(result.get("request_id")) is not int
            or result["request_id"] != request_id or type(result.get("hire_id")) is not int
            or result["hire_id"] != args.hire_id or result.get("operation") not in HANDLERS
            or (operation is not None and result["operation"] != operation)):
        raise ValueError("decoded result does not match the accepted request")
    status = result.get("status")
    if status not in ("pending", "ok", "error") or result.get("ok") is not (status != "error"):
        raise ValueError("invalid result status")
    if status != "pending":
        output = result.get("output")
        if (not isinstance(output, dict) or output.get("ok") is not (status == "ok")
                or output.get("operation") != result["operation"]):
            raise ValueError("invalid terminal result")
    return result


def invoke(args, handler, payload):
    deadline = time.monotonic() + args.wait_seconds
    accepted = execute(args, handler, dict(payload, hire_id=args.hire_id),
                       "wrapper.request_accepted", deadline)
    request_id = accepted.get("request_id")
    if (accepted.get("ok") is not True or accepted.get("status") != "pending"
            or type(accepted.get("hire_id")) is not int or accepted["hire_id"] != args.hire_id
            or accepted.get("operation") != handler or accepted.get("poll_handler") != "get_result"
            or type(request_id) is not int or not 1 <= request_id <= 2**64 - 1):
        raise ValueError("invalid request acceptance event")
    print(f"Accepted request {request_id} for hire {args.hire_id}; reuse this ID if polling stops.",
          file=sys.stderr, flush=True)
    while True:
        result = get_result(args, request_id, handler, deadline=deadline)
        if result["status"] != "pending":
            return result
        time.sleep(remaining(deadline, 4))


def add_connection_arguments(parser):
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--wallet-key", required=True)
    parser.add_argument("--cowboy", default="cowboy")
    parser.add_argument("--wait-seconds", type=int, default=300)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    call = commands.add_parser("call", help="submit once and wait for its exact result")
    call.add_argument("--handler", choices=HANDLERS, required=True)
    call.add_argument("--payload-json", required=True)
    read = commands.add_parser("result", help="read an existing request without resubmitting")
    read.add_argument("request_id", type=int)
    args = parser.parse_args()
    if args.command == "call":
        result = invoke(args, args.handler, json.loads(args.payload_json))
    else:
        result = get_result(args, args.request_id)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if result["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
