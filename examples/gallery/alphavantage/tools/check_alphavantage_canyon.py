#!/usr/bin/env python3
"""Adversarial Canyon differential for Alpha Vantage routes."""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

import live_alphavantage as comparison


codec = comparison.codec
av = comparison.av


CASES = [
    ("quote", {"symbol": "IBM"}),
    ("daily", {"symbol": "IBM"}),
    ("overview", {"symbol": "IBM"}),
]


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
    """Reconstruct a receipt from the CLI's inline `actor execute` output.

    The current CLI does not print an outer transaction hash for `actor
    execute`; instead it waits for the finalized receipt (exiting non-zero if
    none is observed before --receipt-timeout-seconds) and prints it as:

        Status: String("Success")
          - <topic>: 0x<hex>

    So we read the receipt the CLI already decoded rather than re-fetching one
    by a hash that is not in the output. Each printed event is attributed to
    the executed actor; downstream filtering is by topic, so system events such
    as `deferred_tx.created` are ignored without being CBOR-decoded.
    """
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
        # Subprocess exceptions include argv (the --private-key value) and
        # captured output, which can carry wallet material. Keep them out of
        # the error report (browserbase #372 client scrub).
        raise RuntimeError(
            "actor execute failed or timed out; inspect the wallet transaction before retrying"
        ) from None
    return receipt_from_output(args, completed.stdout + "\n" + completed.stderr)


def decoded_events(transaction_receipt, actor, topic):
    assert transaction_receipt.get("status") == "Success", (
        "transaction did not execute successfully: %r" % transaction_receipt.get("status")
    )
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
    assert len(accepted) == 1, "%s acceptance event missing from exact tx" % handler
    record = accepted[0]
    assert record["ok"] is True and record["operation"] == handler
    assert record["hire_id"] == args.hire_id and record["status"] == "pending"
    return record["request_id"]


def await_terminal(args, request_id):
    deadline = time.time() + 240
    while time.time() < deadline:
        record = request_record(args, request_id)
        if isinstance(record, dict) and record.get("status") in ("ok", "error"):
            return record
        time.sleep(4)
    raise TimeoutError("request %d did not finish" % request_id)


def signed_result(args, request_id, wallet_key=None):
    offset, total, chunks = 0, None, []
    while total is None or offset < total:
        tx_receipt = execute(
            args,
            "get_result",
            {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": offset},
            wallet_key=wallet_key,
        )
        events = decoded_events(tx_receipt, args.actor, "wrapper.result_chunk")
        assert len(events) == 1, "signed get_result emitted no result chunk"
        chunk = events[0]
        assert type(chunk.get("request_id")) is int and chunk["request_id"] == request_id
        assert type(chunk.get("hire_id")) is int and chunk["hire_id"] == args.hire_id
        assert type(chunk.get("offset")) is int and chunk["offset"] == offset
        assert isinstance(chunk.get("data"), bytes)
        assert 0 < len(chunk["data"]) <= av.MAX_RESULT_CHUNK_BYTES
        if total is None:
            total = chunk["total"]
            assert type(total) is int and 0 < total <= av.RETURN_BYTE_BUDGET
        assert type(chunk.get("total")) is int and chunk["total"] == total
        assert offset + len(chunk["data"]) <= total
        chunks.append(bytes(chunk["data"]))
        offset += len(chunk["data"])
    encoded = b"".join(chunks)
    assert len(encoded) == total
    result = codec.decode(encoded)
    assert result["request_id"] == request_id and result["hire_id"] == args.hire_id
    assert result.get("operation") in comparison.ROUTES
    status = result.get("status")
    assert status in ("pending", "ok", "error")
    assert result.get("ok") is (status != "error")
    if status != "pending":
        output = result.get("output")
        assert isinstance(output, dict)
        assert output.get("ok") is (status == "ok")
        assert output.get("operation") == result["operation"]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument(
        "--key-file",
        default=os.environ.get(
            "ALPHAVANTAGE_API_KEY_FILE", "/private/tmp/alphavantage-key.txt"
        ),
    )
    parser.add_argument("--rpc", default="https://rpc.canyon.cowboylabs.net")
    parser.add_argument("--wallet-key", default=os.path.expanduser("~/.cowboy/cal-key"))
    parser.add_argument(
        "--wrong-wallet-key",
        help="optional second funded wallet; must not receive a result chunk",
    )
    parser.add_argument("--cowboy", default="/private/tmp/cowboy-canyon")
    parser.add_argument("--only", help="run a single handler (conserves the daily quota)")
    parser.add_argument(
        "--request-id",
        type=int,
        help="read one existing request through signed result chunks; makes no vendor call",
    )
    args = parser.parse_args()
    if args.request_id is not None:
        print(json.dumps(signed_result(args, args.request_id), ensure_ascii=False))
        return
    key = open(args.key_file, encoding="utf-8").read().strip()

    cases = [c for c in CASES if not args.only or c[0] == args.only]
    for handler, payload in cases:
        request_id = submit(args, handler, payload)
        terminal = await_terminal(args, request_id)
        assert terminal["status"] == "ok", "%s request %d: %r" % (handler, request_id, terminal)
        result = signed_result(args, request_id)
        assert result["status"] == "ok" and result["output"]["ok"] is True
        direct_url = comparison.expected_url(handler, payload, key=key)
        status, direct = comparison.http(direct_url, key)
        assert status == 200, "%s direct call returned %s: %r" % (handler, status, direct)
        required = comparison.ROUTES[handler]["required"]
        expected = comparison.selected_vendor_data(handler, direct)
        assert required in expected, "%s direct response missing %r" % (handler, required)
        fidelity = comparison.assert_fidelity(result["output"]["data"], expected, required)
        if comparison.ROUTES[handler]["extract"] is not None and fidelity == "exact":
            fidelity = "selected-exact"
        if args.wrong_wallet_key:
            denied = execute(
                args,
                "get_result",
                {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": 0},
                wallet_key=args.wrong_wallet_key,
            )
            assert not decoded_events(denied, args.actor, "wrapper.result_chunk"), (
                "wrong identity received a result chunk"
            )
        print("PASS %-10s request=%d %s Canyon/direct match" % (handler, request_id, fidelity))

    print("PASS: all Canyon routes are request-bound, transport-safe, and vendor-honest")


if __name__ == "__main__":
    main()
