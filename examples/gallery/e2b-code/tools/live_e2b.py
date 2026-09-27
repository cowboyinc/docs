# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4", "e2b-code-interpreter==2.10.0"]
# ///
"""Signed E2B request/result delivery and optional direct SDK comparisons.

Run with `uv run live_e2b.py --help`. Calls require a deployed Actor and funded
wallet. The compare command also creates paid E2B sandboxes with the vendor SDK.
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


def decode(blob):
    stream = io.BytesIO(blob)
    value = cbor2.CBORDecoder(stream).decode()
    if stream.read(1):
        raise ValueError("trailing bytes after CBOR value")
    return value


def receipt_event(receipt, actor, topic):
    if receipt.get("status") != "Success":
        raise ValueError("transaction did not execute successfully")
    matches = []
    for event in receipt.get("events", []):
        if (isinstance(event, list) and len(event) == 3
                and isinstance(event[0], str) and event[0].lower() == actor.lower()
                and event[1] == topic):
            matches.append(decode(bytes.fromhex(event[2].removeprefix("0x"))))
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise ValueError(f"expected one {topic} map from the Actor")
    return matches[0]


def remaining(deadline, maximum):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("deadline reached; retain the transaction or request ID before retrying")
    return min(seconds, maximum)


def execute(args, handler, payload, topic, deadline):
    try:
        result = subprocess.run(
            [args.cowboy, "actor", "execute", "--actor", args.actor,
             "--handler", handler, "--payload", "0x" + cbor2.dumps(payload, canonical=True).hex(),
             "--private-key", str(args.wallet_key), "--no-wait"],
            check=True, capture_output=True, text=True,
            env=dict(os.environ, COWBOY_RPC_URL=args.rpc, NO_COLOR="1"),
            timeout=remaining(deadline, 60),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # These exceptions include argv, which can contain wallet material.
        raise RuntimeError("signed transaction command failed; inspect submission before retrying") from None
    hashes = re.findall(
        r"^\s*(?:Transaction|Tx) hash:\s*(?:0x)?([0-9a-fA-F]{64})\s*$",
        result.stdout, re.MULTILINE | re.IGNORECASE,
    )
    if len(hashes) != 1:
        raise ValueError("CLI output must contain exactly one labeled transaction hash")
    tx_hash = "0x" + hashes[0].lower()
    print(f"{handler}: transaction {tx_hash}", file=sys.stderr, flush=True)
    url = args.rpc.rstrip("/") + "/transaction/" + tx_hash + "/receipt"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=remaining(deadline, 15)) as response:
                receipt = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            time.sleep(remaining(deadline, 2))
        else:
            return receipt_event(receipt, args.actor, topic)


def get_result(args, request_id, deadline):
    data = bytearray()
    total = None
    while total is None or len(data) < total:
        chunk = execute(
            args, "get_result",
            {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": len(data)},
            "wrapper.result_chunk", deadline,
        )
        if (type(chunk.get("request_id")) is not int or chunk["request_id"] != request_id
                or type(chunk.get("hire_id")) is not int or chunk["hire_id"] != args.hire_id
                or type(chunk.get("offset")) is not int or chunk["offset"] != len(data)
                or not isinstance(chunk.get("data"), bytes)
                or not 0 < len(chunk["data"]) <= 3_900):
            raise ValueError("result chunk does not match the requested identity or offset")
        if total is None:
            total = chunk.get("total")
            if type(total) is not int or not 0 < total <= 38_000:
                raise ValueError("invalid encoded result size")
        if chunk.get("total") != total or len(data) + len(chunk["data"]) > total:
            raise ValueError("result size changed between chunks")
        data.extend(chunk["data"])
    result = decode(data)
    if (not isinstance(result, dict)
            or type(result.get("request_id")) is not int or result["request_id"] != request_id
            or type(result.get("hire_id")) is not int or result["hire_id"] != args.hire_id
            or result.get("operation") != "run"):
        raise ValueError("decoded result belongs to a different request")
    status = result.get("status")
    if status not in ("pending", "ok", "error") or result.get("ok") is not (status != "error"):
        raise ValueError("invalid result status")
    if status != "pending":
        output = result.get("output")
        if (not isinstance(output, dict) or output.get("operation") != "run"
                or output.get("ok") is not (status == "ok")):
            raise ValueError("result output does not match its terminal status")
    return result


def invoke(args, payload):
    deadline = time.monotonic() + args.wait_seconds
    accepted = execute(args, "run", dict(payload, hire_id=args.hire_id),
                       "wrapper.request_accepted", deadline)
    request_id = accepted.get("request_id")
    if (accepted.get("ok") is not True or accepted.get("status") != "pending"
            or accepted.get("operation") != "run"
            or accepted.get("poll_handler") != "get_result"
            or type(accepted.get("hire_id")) is not int or accepted["hire_id"] != args.hire_id
            or type(request_id) is not int or not 1 <= request_id <= 2**64 - 1):
        raise ValueError("invalid request acceptance event")
    print(f"Accepted request {request_id}; use its ID for later reads.", file=sys.stderr, flush=True)
    while True:
        result = get_result(args, request_id, deadline)
        if result["status"] != "pending":
            return result
        time.sleep(remaining(deadline, 4))


def direct_run(key, code, language):
    # The official SDK supplies routing, authentication, parsing and cleanup.
    # It does not import the Actor or Runner tool implementation.
    from e2b_code_interpreter import Sandbox

    with Sandbox.create(api_key=key, timeout=180,
                        network={"allow_public_traffic": False}) as sandbox:
        return sandbox.run_code(code, language=language, timeout=20, request_timeout=5)


CASES = [
    ("stdout", "print('hello from e2b')", "python", "hello from e2b\n", None),
    ("arithmetic", "print(6 * 7)", "python", "42\n", None),
    ("expression", "21 + 21", "python", "", "42"),
    ("pandas", "import pandas as pd; print(int(pd.Series([1,2]).sum()))", "python", "3\n", None),
    ("javascript", "console.log(1 + 2)", "javascript", "3\n", None),
    ("error", "raise ValueError('boom')", "python", "", None),
]


def compare(case, direct, result):
    name, _, _, stdout, expression = case
    is_error = name == "error"
    if result.get("status") != ("error" if is_error else "ok") or result.get("ok") is not (not is_error):
        raise ValueError(f"{name}: Actor did not return the expected outcome")
    output = result.get("output", {})
    data = output.get("data", {})
    if output.get("ok") is not (not is_error):
        raise ValueError(f"{name}: failed tool result")
    if is_error:
        error = data.get("error") or {}
        if (direct.error is None or direct.error.name != "ValueError"
                or direct.error.value != "boom"
                or error.get("name") != "ValueError" or error.get("value") != "boom"):
            raise ValueError("expected the program's ValueError, not a transport failure")
    elif direct.error is not None or data.get("error") is not None:
        raise ValueError(f"{name}: execution failed")
    if "".join(direct.logs.stdout) != stdout or data.get("stdout") != stdout:
        raise ValueError(f"{name}: stdout differs from the independent expected value")
    if "".join(direct.logs.stderr) != data.get("stderr"):
        raise ValueError(f"{name}: stderr differs")
    if expression is not None:
        if direct.text != expression or data.get("results") != [{"text": expression}]:
            raise ValueError(f"{name}: expression result differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--wallet-key", required=True, type=Path)
    parser.add_argument("--cowboy", default="cowboy")
    parser.add_argument("--wait-seconds", type=int, default=300)
    commands = parser.add_subparsers(dest="command", required=True)
    call = commands.add_parser("call", help="submit once and read the accepted request")
    call.add_argument("--payload-json", required=True)
    read = commands.add_parser("result", help="read an existing request without resubmitting")
    read.add_argument("request_id", type=int)
    check = commands.add_parser("compare", help="paid vendor SDK versus signed Actor checks")
    check.add_argument("--key-file", type=Path, required=True)
    check.add_argument("--only", choices=[case[0] for case in CASES])
    args = parser.parse_args()
    if args.command == "call":
        result = invoke(args, json.loads(args.payload_json))
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] != "ok":
            raise SystemExit(1)
    elif args.command == "result":
        result = get_result(args, args.request_id, time.monotonic() + args.wait_seconds)
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "error":
            raise SystemExit(1)
    else:
        key = args.key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("vendor key file is empty")
        for case in CASES:
            name, code, language, _, _ = case
            if args.only and args.only != name:
                continue
            direct = direct_run(key, code, language)
            result = invoke(args, {"code": code, "language": language})
            compare(case, direct, result)
            print(f"PASS {name}: request {result['request_id']} matches the vendor SDK")


if __name__ == "__main__":
    main()
