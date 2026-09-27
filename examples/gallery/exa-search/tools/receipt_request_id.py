"""Read the Exa request id from current Cowboy CLI and receipt contracts."""

import json
import re
import sys
import struct


EVENT_TOPIC = "wrapper.search_accepted"
RESULT_TOPIC = "wrapper.result_chunk"
MAX_RESULT_BYTES = 40_000
MAX_RESULT_CHUNK_BYTES = 4_015
TX_HASH_RE = re.compile(
    r"^\s*(?:Transaction|Tx) hash:\s*(?:0x)?([0-9a-fA-F]{64})\s*$", re.MULTILINE
)


class CborReader:
    """Small decoder for canonical CBOR values used in Actor events."""

    def __init__(self, raw):
        self.raw = raw
        self.pos = 0

    def exact(self, size):
        end = self.pos + size
        if end > len(self.raw):
            raise ValueError("unexpected end of CBOR")
        value = self.raw[self.pos:end]
        self.pos = end
        return value

    def length(self, minor):
        if minor < 24:
            return minor
        if minor == 24:
            return self.exact(1)[0]
        if minor == 25:
            return int.from_bytes(self.exact(2), "big")
        if minor == 26:
            return int.from_bytes(self.exact(4), "big")
        if minor == 27:
            return int.from_bytes(self.exact(8), "big")
        raise ValueError("unsupported CBOR length")

    def read(self):
        initial = self.exact(1)[0]
        major = initial >> 5
        minor = initial & 31
        if major == 0:
            return self.length(minor)
        if major == 1:
            return -1 - self.length(minor)
        if major == 2:
            return self.exact(self.length(minor))
        if major == 3:
            return self.exact(self.length(minor)).decode("utf-8")
        if major == 4:
            return [self.read() for _ in range(self.length(minor))]
        if major == 5:
            return {self.read(): self.read() for _ in range(self.length(minor))}
        if major == 7 and minor == 27:
            return struct.unpack(">d", self.exact(8))[0]
        if major == 7 and minor in (20, 21, 22):
            return {20: False, 21: True, 22: None}[minor]
        raise ValueError("unsupported CBOR value")


def decode_cbor(raw):
    reader = CborReader(raw)
    value = reader.read()
    if reader.pos != len(raw):
        raise ValueError("trailing bytes after CBOR value")
    return value


def extract_transaction_hash(output):
    matches = TX_HASH_RE.findall(output)
    if len(matches) != 1:
        raise ValueError("actor execute output must contain one transaction hash")
    return "0x" + matches[0].lower()


def _address(value):
    text = str(value).strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", text):
        raise ValueError("expected Actor must be a 20-byte 0x address")
    return text


def _decoded_events(receipt, expected_actor, topic):
    if not isinstance(receipt, dict) or receipt.get("status") != "Success":
        raise ValueError("receipt is not successful")
    expected_actor = _address(expected_actor)
    events = receipt.get("events") if isinstance(receipt, dict) else None
    if not isinstance(events, list):
        raise ValueError("receipt has no events list")

    payloads = []
    for event in events:
        if not isinstance(event, (list, tuple)) or len(event) != 3:
            continue
        emitter, event_topic, data = event
        if str(event_topic) != topic or str(emitter).lower() != expected_actor:
            continue
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", data):
            raise ValueError("%s event data is not 0x hex" % topic)
        payloads.append(decode_cbor(bytes.fromhex(data[2:])))
    return payloads


def extract_request_id(receipt, expected_actor):
    request_ids = []
    for payload in _decoded_events(receipt, expected_actor, EVENT_TOPIC):
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if type(request_id) is not int or not 1 <= request_id <= 2**64 - 1:
            raise ValueError("search acceptance event has no valid request id")
        request_ids.append(request_id)

    if len(request_ids) != 1:
        raise ValueError("receipt must contain one search acceptance event from the Actor")
    return request_ids[0]


def extract_result_chunk(receipt, expected_actor, request_id, hire_id):
    chunks = []
    for payload in _decoded_events(receipt, expected_actor, RESULT_TOPIC):
        if not isinstance(payload, dict):
            raise ValueError("result chunk event must contain a map")
        if payload.get("request_id") != request_id or payload.get("hire_id") != hire_id:
            continue

        offset = payload.get("offset")
        length = payload.get("length")
        total_bytes = payload.get("total_bytes")
        data = payload.get("data")
        if type(offset) is not int or not 0 <= offset < MAX_RESULT_BYTES:
            raise ValueError("result chunk has an invalid offset")
        if type(length) is not int or not 1 <= length <= MAX_RESULT_CHUNK_BYTES:
            raise ValueError("result chunk has an invalid length")
        if type(total_bytes) is not int or not 1 <= total_bytes <= MAX_RESULT_BYTES:
            raise ValueError("result chunk has an invalid total byte count")
        if not isinstance(data, bytes) or len(data) != length:
            raise ValueError("result chunk data does not match its length")
        if offset + length > total_bytes:
            raise ValueError("result chunk extends past the encoded result")
        chunks.append(payload)

    if len(chunks) != 1:
        raise ValueError("receipt must contain one matching result chunk event from the Actor")
    return chunks[0]


def extract_secret_version(receipt, expected_owner):
    if not isinstance(receipt, dict) or receipt.get("status") != "Success":
        raise ValueError("secret transaction is not successful")
    owner = bytes.fromhex(_address(expected_owner)[2:])
    matches = []
    for event in receipt.get("events", []):
        if not isinstance(event, (list, tuple)) or len(event) != 3 or event[1] != "cbss.secret.created":
            continue
        if not isinstance(event[2], str) or not re.fullmatch(r"0x[0-9a-fA-F]{120}", event[2]):
            raise ValueError("invalid secret creation event")
        raw = bytes.fromhex(event[2][2:])
        if raw[:20] != owner:
            raise ValueError("secret event belongs to a different account")
        version = int.from_bytes(raw[52:], "big")
        if version == 0:
            raise ValueError("invalid secret version")
        matches.append(("0x" + raw[20:52].hex(), version))
    if len(matches) != 1:
        raise ValueError("receipt must contain one secret creation event")
    return matches[0]


def main(argv):
    if len(argv) == 2 and argv[0] == "secret-version":
        key_hash, version = extract_secret_version(json.load(sys.stdin), argv[1])
        print(key_hash, version)
        return
    if argv == ["tx-hash"]:
        print(extract_transaction_hash(sys.stdin.read()))
        return
    if len(argv) == 2 and argv[0] == "request-id":
        print(extract_request_id(json.load(sys.stdin), argv[1]))
        return
    if argv == ["decode-result"]:
        print(json.dumps(decode_cbor(sys.stdin.buffer.read()), ensure_ascii=False))
        return
    raise SystemExit(
        "usage: receipt_request_id.py tx-hash | request-id <expected-actor> | "
        "decode-result | secret-version <owner>"
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
