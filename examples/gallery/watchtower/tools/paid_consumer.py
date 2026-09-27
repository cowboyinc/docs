#!/usr/bin/env python3
"""Paid Watchtower consumer helpers for the Form 4 demo."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cbor2

import edgar_poller
import watchtower_crypto as crypto


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def first_buy_signal(clusters: list[dict]) -> dict:
    for cluster in clusters:
        if cluster.get("action") == "BUY" and cluster.get("ticker"):
            return {
                "schema": "cowboy.watchtower.signal.v1",
                "action": "BUY",
                "ticker": str(cluster["ticker"]).upper(),
                "reason": cluster.get("reason", "clustered insider buying"),
                "event_count": int(cluster.get("event_count", 0)),
                "notional_usd": float(cluster.get("notional_usd", 0)),
                "source_accessions": list(cluster.get("accessions", [])),
            }
    raise ValueError("no BUY signal in clusters")


def parse_fetch_key_output(output: str) -> bytes:
    for line in output.splitlines():
        if line.startswith("content_key="):
            return crypto.bytes_from_hex(line.split("=", 1)[1])
    raise ValueError("fetch-key output did not contain content_key=")


def decrypt_paid_message(message_cbor: bytes, content_key: bytes, publisher_public_key: bytes) -> dict:
    message = cbor2.loads(message_cbor)
    crypto.verify_stream_envelope(message, publisher_public_key)
    if message.get("payload_format") != "CIPHERTEXT":
        raise ValueError("expected CIPHERTEXT StreamMessage")
    aad = crypto.stream_message_aad(
        message["stream_id"],
        int(message["sequence"]),
        int(message["key_epoch"]),
        message["kind"],
        message["content_type"],
    )
    plaintext = crypto.xchacha20poly1305_decrypt(content_key, message["payload_inline"], aad)
    return json.loads(plaintext.decode("utf-8"))


def poll_delivery_ready(rpc_url: str, seal_request_id: str, timeout_s: int = 180) -> dict:
    import urllib.request

    deadline = time.time() + timeout_s
    url = f"{rpc_url.rstrip('/')}/cbss/cip7-delivery/{seal_request_id}"
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                bundle = json.loads(response.read().decode("utf-8"))
            last = bundle
            if bundle.get("status", {}).get("lifecycle") == "Ready":
                return bundle
        except Exception as exc:
            last = {"error": str(exc)}
        time.sleep(2)
    raise TimeoutError(f"delivery not Ready before timeout: {last}")


def run_fetch_key(cowboy_bin: str, feed: str, seal_request_id: str, recipient_key_file: Path, rpc_url: str) -> bytes:
    output = subprocess.check_output(
        [
            cowboy_bin,
            "watchtower",
            "--rpc-url",
            rpc_url,
            "feed",
            feed,
            "fetch-key",
            "--seal-request-id",
            seal_request_id,
            "--recipient-key-file",
            str(recipient_key_file),
        ],
        text=True,
    )
    return parse_fetch_key_output(output)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clusters", type=Path, help="cluster JSONL file to convert to a BUY signal")
    parser.add_argument("--message-cbor-hex", help="hex StreamMessage envelope to decrypt")
    parser.add_argument("--content-key", help="32-byte content key hex")
    parser.add_argument("--publisher-key", help="32-byte Ed25519 publisher public key hex")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.clusters:
        clusters = load_jsonl(args.clusters)
        print(json.dumps(first_buy_signal(clusters), sort_keys=True))
        return 0
    if args.message_cbor_hex:
        plaintext = decrypt_paid_message(
            crypto.bytes_from_hex(args.message_cbor_hex),
            crypto.bytes_from_hex(args.content_key),
            crypto.bytes_from_hex(args.publisher_key),
        )
        print(json.dumps(plaintext, sort_keys=True))
        return 0
    clusters = edgar_poller.cluster_buy_signals(edgar_poller.load_fixture_events())
    print(json.dumps(first_buy_signal(clusters), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
