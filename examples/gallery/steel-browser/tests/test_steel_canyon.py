#!/usr/bin/env python3
"""Signed Canyon verification for the Steel browser wrapper (screenshot).

UNRUN in this build: it needs a deployed, entitled actor, a paid Trading Post
Hire, live runners, and a STEEL_API_KEY in CBSS. It submits a real
``screenshot``, waits for that exact request record to finish, and reads it back
through signed ``get_result`` receipt chunks as the registered hirer. A
``--wrong-wallet-key`` proves a different identity cannot read the result.
"""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

import test_steel_comparison as comparison


codec = comparison.codec
steel = comparison.steel


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
                args.cowboy, "actor", "execute", "--actor", args.actor,
                "--handler", handler, "--payload-json", json.dumps(payload, separators=(",", ":")),
                "--private-key", wallet_key or args.wallet_key, "--receipt-timeout-seconds", "180",
            ],
            check=True, capture_output=True, text=True, env=env,
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
    assert transaction_receipt.get("status") == "Success"
    found = []
    for event in transaction_receipt.get("events") or []:
        if (
            not isinstance(event, list) or len(event) != 3
            or str(event[0]).lower() != actor.lower() or event[1] != topic
        ):
            continue
        raw = event[2][2:] if str(event[2]).startswith("0x") else str(event[2])
        found.append(codec.decode(bytes.fromhex(raw)))
    return found


def submit(args, url, operation="screenshot"):
    tx_receipt = execute(args, operation, {"hire_id": args.hire_id, "url": url})
    accepted = decoded_events(tx_receipt, args.actor, "wrapper.request_accepted")
    assert len(accepted) == 1
    record = accepted[0]
    assert record["ok"] is True and record["operation"] == operation
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


def signed_result(args, request_id, operation="screenshot", wallet_key=None):
    offset, total, chunks = 0, None, []
    while total is None or offset < total:
        tx_receipt = execute(
            args, "get_result",
            {"hire_id": args.hire_id, "request_id": request_id, "receipt_offset": offset},
            wallet_key=wallet_key,
        )
        events = decoded_events(tx_receipt, args.actor, "wrapper.result_chunk")
        assert len(events) == 1
        chunk = events[0]
        assert chunk["request_id"] == request_id and chunk["hire_id"] == args.hire_id
        assert chunk["offset"] == offset and isinstance(chunk.get("data"), bytes)
        assert 0 < len(chunk["data"]) <= steel.MAX_RESULT_CHUNK_BYTES
        if total is None:
            total = chunk["total"]
            assert 0 < total <= steel.RETURN_BYTE_BUDGET
        assert chunk["total"] == total
        chunks.append(bytes(chunk["data"]))
        offset += len(chunk["data"])
    encoded = b"".join(chunks)
    assert len(encoded) == total
    result = codec.decode(encoded)
    assert result["request_id"] == request_id and result["hire_id"] == args.hire_id
    assert result.get("operation") == operation
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--rpc", default="https://rpc.canyon.cowboylabs.net")
    parser.add_argument("--wallet-key", default=os.path.expanduser("~/.cowboy/cal-key"))
    parser.add_argument("--wrong-wallet-key")
    parser.add_argument("--cowboy", default="/private/tmp/cowboy-canyon")
    args = parser.parse_args()

    # Exercise BOTH vendor routes on chain (screenshot + pdf hit distinct
    # endpoints); a single happy path is not route coverage.
    ids = {}
    for operation in ("screenshot", "pdf"):
        request_id = submit(args, args.url, operation)
        terminal = await_terminal(args, request_id)
        assert terminal["status"] == "ok", "%s request %d: %r" % (operation, request_id, terminal)
        result = signed_result(args, request_id, operation)
        assert result["status"] == "ok" and result["output"]["ok"] is True
        url = result["output"]["data"].get("url")
        assert isinstance(url, str) and url.startswith(("http://", "https://"))
        ids[operation] = request_id

    if args.wrong_wallet_key:
        denied = execute(
            args, "get_result",
            {"hire_id": args.hire_id, "request_id": ids["screenshot"], "receipt_offset": 0},
            wallet_key=args.wrong_wallet_key,
        )
        assert not decoded_events(denied, args.actor, "wrapper.result_chunk")

    print(
        "PASS: Steel screenshot request=%d pdf request=%d completed through signed Canyon receipts"
        % (ids["screenshot"], ids["pdf"])
    )


if __name__ == "__main__":
    main()
