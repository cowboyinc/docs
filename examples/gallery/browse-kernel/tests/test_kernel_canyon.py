#!/usr/bin/env python3
"""Signed Canyon verification for Kernel browse.

Default: submits one paid `visit`, reassembles the result from signed receipt
chunks, and confirms a wrong wallet is denied.

`--sequence`: drives a stateful `step` lifecycle live — goto (auto-opens a
per-hire session) -> extract -> optional benign click -> close — so multiple ops
run against one persistent server-side session.
"""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

import test_kernel_comparison as comparison


codec = comparison.codec
kernel = comparison.kernel


def rpc_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


def state(args):
    return rpc_json(args.rpc.rstrip("/") + "/actor/" + args.actor)


def storage_value(snapshot, key, default=None):
    raw = snapshot.get("storage", {}).get("0x" + key.encode().hex())
    return default if raw is None else codec.decode(bytes.fromhex(raw[2:]))


def request_record(args, request_id):
    return storage_value(state(args), "request:" + str(request_id), {})


def receipt_from_output(args, output):
    # CLI prints the finalized receipt inline: `Status:` + `- <topic>: 0x<hex>` lines.
    status_match = re.search(r'Status:\s*(?:String\()?"?([A-Za-z]+)', output)
    if not status_match:
        raise AssertionError("could not parse execute receipt status: " + output[-500:])
    events = [
        [args.actor, topic, data]
        for topic, data in re.findall(
            r"^\s*-\s+([\w.]+):\s*(0x[0-9a-fA-F]+)\s*$", output, re.MULTILINE
        )
    ]
    return {"status": status_match.group(1), "events": events}


def execute(args, handler, payload, wallet_key=None):
    env = dict(os.environ, COWBOY_RPC_URL=args.rpc)
    try:
        completed = subprocess.run(
            [
                args.cowboy,
                "actor",
                "execute",
                "--actor",
                args.actor,
                "--handler",
                handler,
                "--payload-json",
                json.dumps(payload, separators=(",", ":")),
                "--private-key",
                wallet_key or args.wallet_key,
                "--receipt-timeout-seconds",
                "180",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        # Don't leak argv/output (wallet key material) into the error report.
        raise RuntimeError(
            "actor execute failed or timed out; inspect the wallet transaction before retrying"
        ) from None
    return receipt_from_output(args, completed.stdout + "\n" + completed.stderr)


def decoded_events(transaction_receipt, actor, topic):
    assert transaction_receipt.get("status") == "Success"
    found = []
    for event in transaction_receipt.get("events") or []:
        if (
            not isinstance(event, list)
            or len(event) != 3
            or str(event[0]).lower() != actor.lower()
            or event[1] != topic
        ):
            continue
        raw = event[2][2:] if str(event[2]).startswith("0x") else str(event[2])
        found.append(codec.decode(bytes.fromhex(raw)))
    return found


def submit(args, handler, payload):
    tx_receipt = execute(args, handler, {"hire_id": args.hire_id, **payload})
    accepted = decoded_events(tx_receipt, args.actor, "wrapper.request_accepted")
    assert len(accepted) == 1
    record = accepted[0]
    assert record["ok"] is True and record["operation"] == handler
    assert record["hire_id"] == args.hire_id and record["status"] == "pending"
    return record["request_id"]


def await_terminal(args, request_id):
    deadline = time.time() + 300
    while time.time() < deadline:
        record = request_record(args, request_id)
        if isinstance(record, dict) and record.get("status") in ("ok", "error"):
            return record
        time.sleep(4)
    raise TimeoutError("request %d did not finish" % request_id)


def signed_result(args, request_id, wallet_key=None):
    offset = 0
    total = None
    chunks = []
    while total is None or offset < total:
        tx_receipt = execute(
            args,
            "get_result",
            {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": offset},
            wallet_key=wallet_key,
        )
        events = decoded_events(tx_receipt, args.actor, "wrapper.result_chunk")
        assert len(events) == 1
        chunk = events[0]
        assert type(chunk.get("request_id")) is int and chunk["request_id"] == request_id
        assert type(chunk.get("hire_id")) is int and chunk["hire_id"] == args.hire_id
        assert type(chunk.get("offset")) is int and chunk["offset"] == offset
        assert isinstance(chunk.get("data"), bytes)
        assert 0 < len(chunk["data"]) <= kernel.MAX_RESULT_CHUNK_BYTES
        if total is None:
            total = chunk["total"]
            assert type(total) is int and 0 < total <= kernel.RETURN_BYTE_BUDGET
        assert type(chunk.get("total")) is int and chunk["total"] == total
        assert offset + len(chunk["data"]) <= total
        chunks.append(bytes(chunk["data"]))
        offset += len(chunk["data"])
    encoded = b"".join(chunks)
    assert len(encoded) == total
    result = codec.decode(encoded)
    assert result["request_id"] == request_id and result["hire_id"] == args.hire_id
    assert result.get("operation") in ("visit", "step")
    status = result.get("status")
    assert status in ("pending", "ok", "error")
    assert result.get("ok") is (status != "error")
    if status != "pending":
        output = result.get("output")
        assert isinstance(output, dict)
        assert output.get("ok") is (status == "ok")
        assert output.get("operation") == result.get("operation")
    return result


def validate_vendor_data(data, expected_url=None):
    assert isinstance(data, dict)
    assert isinstance(data.get("text"), str)
    assert isinstance(data.get("url"), str)
    assert isinstance(data.get("links"), list)
    if expected_url is not None:
        assert data["url"] == expected_url


def run_and_read(args, handler, payload):
    request_id = submit(args, handler, payload)
    terminal = await_terminal(args, request_id)
    assert terminal["status"] == "ok", "request %d: %r" % (request_id, terminal)
    result = signed_result(args, request_id)
    assert result["status"] == "ok" and result["output"]["ok"] is True
    return request_id, result


def run_visit(args):
    request_id, result = run_and_read(args, "visit", {"url": args.url})
    validate_vendor_data(result["output"]["data"], args.expect_url)
    if args.wrong_wallet_key:
        denied = execute(
            args,
            "get_result",
            {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": 0},
            wallet_key=args.wrong_wallet_key,
        )
        assert not decoded_events(denied, args.actor, "wrapper.result_chunk")
    print("PASS: Kernel browse visit request=%d completed through signed Canyon receipts" % request_id)


def run_sequence(args):
    # goto auto-opens a per-hire session and stores its id; the caller never
    # supplies one. extract re-reads the SAME session; close releases it.
    _, goto = run_and_read(args, "step", {"op": "goto", "url": args.url})
    validate_vendor_data(goto["output"]["data"], args.expect_url)
    session_id = goto["output"]["data"].get("session_id")
    assert isinstance(session_id, str) and session_id, "goto did not return a session id"
    _, extract = run_and_read(args, "step", {"op": "extract"})
    validate_vendor_data(extract["output"]["data"])
    if args.selector:
        _, _click = run_and_read(args, "step", {"op": "click", "selector": args.selector})
    _, close = run_and_read(args, "step", {"op": "close"})
    assert close["output"]["data"].get("closed"), "close did not confirm the session id"
    print("PASS: Kernel browse step sequence (goto->extract%s->close) on session %s"
          % ("->click" if args.selector else "", session_id))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", default="https://rpc.canyon.cowboylabs.net")
    parser.add_argument("--wallet-key", default=os.path.expanduser("~/.cowboy/cal-key"))
    parser.add_argument("--wrong-wallet-key")
    parser.add_argument("--cowboy", default="/private/tmp/cowboy-canyon")
    parser.add_argument("--url", default=comparison.DEFAULT_PAGE_URL, help="the page to browse")
    parser.add_argument("--expect-url")
    parser.add_argument("--selector", help="a benign, stable selector to click in the step sequence")
    parser.add_argument("--sequence", action="store_true", help="drive a step lifecycle (goto->extract->close)")
    parser.add_argument("--request-id", type=int)
    args = parser.parse_args()
    if args.request_id is not None:
        print(json.dumps(signed_result(args, args.request_id), ensure_ascii=False, default=str))
        return
    if args.sequence:
        run_sequence(args)
    else:
        run_visit(args)


if __name__ == "__main__":
    main()
