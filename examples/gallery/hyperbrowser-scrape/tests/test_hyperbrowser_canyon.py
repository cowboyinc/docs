#!/usr/bin/env python3
"""Signed Canyon verification for Hyperbrowser scrape.

Submits one paid scrape, waits for the submit to finish, reads the signed
job_id, then submits a fetch, waits for it to finish, reassembles the result
from actor-filtered signed receipt chunks, asserts the bounded content, and
confirms a wrong wallet is denied. Requires a deployed actor, a paid hire, and a
CBSS-backed key.
"""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

import test_hyperbrowser_comparison as comparison


codec = comparison.codec
hyperbrowser = comparison.hyperbrowser


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


def submit(args, handler, payload, operation):
    tx_receipt = execute(args, handler, payload)
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


def signed_result(args, request_id, operation, wallet_key=None):
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
        assert 0 < len(chunk["data"]) <= hyperbrowser.MAX_RESULT_CHUNK_BYTES
        if total is None:
            total = chunk["total"]
            assert type(total) is int and 0 < total <= hyperbrowser.RETURN_BYTE_BUDGET
        assert type(chunk.get("total")) is int and chunk["total"] == total
        assert offset + len(chunk["data"]) <= total
        chunks.append(bytes(chunk["data"]))
        offset += len(chunk["data"])
    encoded = b"".join(chunks)
    assert len(encoded) == total
    result = codec.decode(encoded)
    assert result["request_id"] == request_id and result["hire_id"] == args.hire_id
    assert result.get("operation") == operation
    status = result.get("status")
    assert status in ("pending", "ok", "error")
    assert result.get("ok") is (status != "error")
    if status != "pending":
        output = result.get("output")
        assert isinstance(output, dict)
        assert output.get("ok") is (status == "ok")
        assert output.get("operation") == operation
    return result


def validate_scrape_data(data):
    assert isinstance(data, dict)
    assert data.get("job_status") == "submitted"
    assert hyperbrowser._valid_job_id(data.get("job_id"))


def validate_fetch_data(data):
    assert isinstance(data, dict)
    assert data.get("job_status") == "completed"
    omitted = (data.get("_cowboy") or {}).get("markdown_omitted") is True
    assert ("markdown" in data) or ("links" in data) or ("metadata" in data) or omitted


def fetch_until_complete(args, source_request_id, attempts=20):
    for attempt in range(attempts):
        fetch_id = submit(
            args, "fetch",
            {"hire_id": args.hire_id, "source_request_id": source_request_id},
            "fetch",
        )
        terminal = await_terminal(args, fetch_id)
        assert terminal["status"] == "ok", "fetch %d: %r" % (fetch_id, terminal)
        result = signed_result(args, fetch_id, "fetch")
        data = result["output"]["data"]
        if data.get("job_status") == "completed":
            return fetch_id, result
        assert data.get("job_status") in ("pending", "running")
        if attempt + 1 < attempts:
            time.sleep(4)
    raise TimeoutError("Hyperbrowser scrape did not complete after %d fetch attempts" % attempts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--rpc", default="https://rpc.canyon.cowboylabs.net")
    parser.add_argument("--wallet-key", default=os.path.expanduser("~/.cowboy/cal-key"))
    parser.add_argument("--wrong-wallet-key")
    parser.add_argument("--cowboy", default="/private/tmp/cowboy-canyon")
    parser.add_argument("--url", default=comparison.DEFAULT_URL)
    parser.add_argument("--formats", nargs="*", default=["links"])
    args = parser.parse_args()

    scrape_id = submit(
        args, "scrape", {"hire_id": args.hire_id, "url": args.url, "formats": args.formats}, "scrape"
    )
    terminal = await_terminal(args, scrape_id)
    assert terminal["status"] == "ok", "scrape %d: %r" % (scrape_id, terminal)
    scrape_result = signed_result(args, scrape_id, "scrape")
    validate_scrape_data(scrape_result["output"]["data"])

    fetch_id, fetch_result = fetch_until_complete(args, scrape_id)
    validate_fetch_data(fetch_result["output"]["data"])

    if args.wrong_wallet_key:
        denied = execute(
            args,
            "get_result",
            {"hire_id": args.hire_id, "request_id": fetch_id, "receipt_offset": 0},
            wallet_key=args.wrong_wallet_key,
        )
        assert not decoded_events(denied, args.actor, "wrapper.result_chunk")
    print(
        "PASS: Hyperbrowser scrape=%d fetch=%d completed through signed Canyon receipts"
        % (scrape_id, fetch_id)
    )


if __name__ == "__main__":
    main()
