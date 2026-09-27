"""Off-chain CIP-7 helpers used by the Watchtower demo."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cbor2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


CONTENT_KEY_HKDF_SALT = b"cip-7-content-key-v1"
PUBLISHER_KEY_DERIVE_DOMAIN = b"cowboy.watchtower.publisher-ed25519.v1"
XCHACHA_NONCE_LEN = 24


@dataclass(frozen=True)
class StreamEnvelope:
    stream_id: bytes
    sequence: int
    kind: str
    content_type: str
    payload_inline: bytes
    payload_format: str
    key_epoch: int | None
    timestamp_unix_ms: int
    signing_key_id: int
    publisher_sig: bytes
    payload_hash: bytes

    def to_cbor_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "version": 1,
            "sequence": self.sequence,
            "timestamp_unix_ms": self.timestamp_unix_ms,
            "kind": self.kind,
            "content_type": self.content_type,
            "tags": {},
            "payload_format": self.payload_format,
            "payload_inline": self.payload_inline,
            "payload_hash": self.payload_hash,
            "key_epoch": self.key_epoch,
            "signing_key_id": self.signing_key_id,
            "publisher_sig": self.publisher_sig,
        }


def canonical_cbor(value) -> bytes:
    return cbor2.dumps(value, canonical=True)


def bytes_from_hex(value: str) -> bytes:
    text = value.strip()
    if text.startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text)


def parse_stream_id(value: str) -> bytes:
    text = value.strip()
    if text.startswith("0x"):
        return bytes_from_hex(text)
    return text.encode("utf-8")


def read_hex_or_raw_file(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw
    candidate = text[2:] if text.startswith("0x") else text
    if candidate and len(candidate) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in candidate):
        return bytes.fromhex(candidate)
    return raw


def write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise FileExistsError(f"{path} already exists; refusing to overwrite secret key material")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    previous = b""
    counter = 1
    while len(okm) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        okm += previous
        counter += 1
    return okm[:length]


def derive_content_key(stream_secret: bytes, chain_id: int, stream_id: bytes, key_epoch: int) -> bytes:
    info = int(chain_id).to_bytes(8, "big") + stream_id + int(key_epoch).to_bytes(8, "big")
    return hkdf_sha256(stream_secret, CONTENT_KEY_HKDF_SALT, info, 32)


def publisher_private_key(wallet_private_key: bytes) -> Ed25519PrivateKey:
    seed = hashlib.sha256(PUBLISHER_KEY_DERIVE_DOMAIN + wallet_private_key).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def publisher_public_key(wallet_private_key: bytes) -> bytes:
    return publisher_private_key(wallet_private_key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def publisher_private_seed(wallet_private_key: bytes) -> bytes:
    return publisher_private_key(wallet_private_key).private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def stream_signing_payload(
    *,
    stream_id: bytes,
    sequence: int,
    timestamp_unix_ms: int,
    kind: str,
    content_type: str,
    payload_format: str,
    payload_hash: bytes,
    key_epoch: int | None,
    signing_key_id: int = 1,
) -> bytes:
    return canonical_cbor(
        {
            "stream_id": stream_id,
            "version": 1,
            "sequence": int(sequence),
            "timestamp_unix_ms": int(timestamp_unix_ms),
            "kind": kind,
            "content_type": content_type,
            "tags": {},
            "payload_format": payload_format,
            "payload_hash": payload_hash,
            "key_epoch": key_epoch,
            "signing_key_id": int(signing_key_id),
        }
    )


def stream_message_aad(stream_id: bytes, sequence: int, key_epoch: int, kind: str, content_type: str) -> bytes:
    return canonical_cbor(
        {
            "stream_id": stream_id,
            "sequence": int(sequence),
            "key_epoch": int(key_epoch),
            "kind": kind,
            "content_type": content_type,
        }
    )


def _rotl32(value: int, bits: int) -> int:
    return ((value << bits) & 0xFFFFFFFF) | (value >> (32 - bits))


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 7)


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("XChaCha20 key must be 32 bytes")
    if len(nonce16) != 16:
        raise ValueError("HChaCha20 nonce must be 16 bytes")
    constants = b"expand 32-byte k"
    words = [
        *[int.from_bytes(constants[i : i + 4], "little") for i in range(0, 16, 4)],
        *[int.from_bytes(key[i : i + 4], "little") for i in range(0, 32, 4)],
        *[int.from_bytes(nonce16[i : i + 4], "little") for i in range(0, 16, 4)],
    ]
    state = list(words)
    for _ in range(10):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)
    return b"".join(state[i].to_bytes(4, "little") for i in (0, 1, 2, 3, 12, 13, 14, 15))


def xchacha20poly1305_encrypt(key: bytes, nonce24: bytes, plaintext: bytes, aad: bytes) -> bytes:
    if len(nonce24) != XCHACHA_NONCE_LEN:
        raise ValueError("XChaCha20 nonce must be 24 bytes")
    subkey = hchacha20(key, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:]
    return nonce24 + ChaCha20Poly1305(subkey).encrypt(nonce12, plaintext, aad)


def xchacha20poly1305_decrypt(key: bytes, envelope: bytes, aad: bytes) -> bytes:
    if len(envelope) < XCHACHA_NONCE_LEN + 16:
        raise ValueError("ciphertext envelope too short")
    nonce24 = envelope[:XCHACHA_NONCE_LEN]
    subkey = hchacha20(key, nonce24[:16])
    nonce12 = b"\x00\x00\x00\x00" + nonce24[16:]
    return ChaCha20Poly1305(subkey).decrypt(nonce12, envelope[XCHACHA_NONCE_LEN:], aad)


def build_stream_envelope(
    *,
    wallet_private_key: bytes,
    stream_id: bytes,
    sequence: int,
    timestamp_unix_ms: int,
    kind: str,
    content_type: str,
    payload: bytes,
    payload_format: str,
    key_epoch: int | None,
) -> StreamEnvelope:
    payload_hash = hashlib.sha256(payload).digest()
    signing_payload = stream_signing_payload(
        stream_id=stream_id,
        sequence=sequence,
        timestamp_unix_ms=timestamp_unix_ms,
        kind=kind,
        content_type=content_type,
        payload_format=payload_format,
        payload_hash=payload_hash,
        key_epoch=key_epoch,
    )
    signature = publisher_private_key(wallet_private_key).sign(signing_payload)
    return StreamEnvelope(
        stream_id=stream_id,
        sequence=sequence,
        kind=kind,
        content_type=content_type,
        payload_inline=payload,
        payload_format=payload_format,
        key_epoch=key_epoch,
        timestamp_unix_ms=timestamp_unix_ms,
        signing_key_id=1,
        publisher_sig=signature,
        payload_hash=payload_hash,
    )


def verify_stream_envelope(message: dict, publisher_public_key_bytes: bytes) -> None:
    payload_inline = message["payload_inline"]
    expected_hash = hashlib.sha256(payload_inline).digest()
    if expected_hash != message["payload_hash"]:
        raise ValueError("payload_hash mismatch")
    signing_payload = stream_signing_payload(
        stream_id=message["stream_id"],
        sequence=message["sequence"],
        timestamp_unix_ms=message["timestamp_unix_ms"],
        kind=message["kind"],
        content_type=message["content_type"],
        payload_format=message["payload_format"],
        payload_hash=message["payload_hash"],
        key_epoch=message["key_epoch"],
        signing_key_id=message["signing_key_id"],
    )
    Ed25519PublicKey.from_public_bytes(publisher_public_key_bytes).verify(
        message["publisher_sig"], signing_payload
    )


def build_paid_payload(
    *,
    wallet_private_key: bytes,
    stream_secret: bytes,
    chain_id: int,
    stream_id: bytes,
    sequence: int,
    key_epoch: int,
    plaintext: bytes,
    timestamp_unix_ms: int,
    kind: str = "form4_cluster",
    content_type: str = "application/json",
    nonce24: bytes | None = None,
) -> bytes:
    content_key = derive_content_key(stream_secret, chain_id, stream_id, key_epoch)
    nonce = nonce24 or hashlib.sha256(stream_id + sequence.to_bytes(8, "big")).digest()[:24]
    aad = stream_message_aad(stream_id, sequence, key_epoch, kind, content_type)
    ciphertext = xchacha20poly1305_encrypt(content_key, nonce, plaintext, aad)
    envelope = build_stream_envelope(
        wallet_private_key=wallet_private_key,
        stream_id=stream_id,
        sequence=sequence,
        timestamp_unix_ms=timestamp_unix_ms,
        kind=kind,
        content_type=content_type,
        payload=ciphertext,
        payload_format="CIPHERTEXT",
        key_epoch=key_epoch,
    )
    return canonical_cbor(envelope.to_cbor_dict())


def build_init_payload(
    *,
    name: str,
    description: str,
    stream_id: bytes,
    publisher_public_key: bytes,
    access_mode: str,
    ring_buffer_capacity: int,
) -> bytes:
    if access_mode not in ("OPEN", "SUBSCRIBER_PAID"):
        raise ValueError("access_mode must be OPEN or SUBSCRIBER_PAID")
    if len(publisher_public_key) != 32:
        raise ValueError("publisher_public_key must be 32 bytes")
    return canonical_cbor(
        {
            "name": name,
            "description": description,
            "stream_id": stream_id,
            "initial_publisher_key": publisher_public_key,
            "access_mode": access_mode,
            "ring_buffer_capacity": int(ring_buffer_capacity),
        }
    )


def _cmd_publisher_key(args: argparse.Namespace) -> int:
    wallet = read_hex_or_raw_file(args.wallet_key_file)
    print(f"publisher_public_key=0x{publisher_public_key(wallet).hex()}")
    print(f"publisher_private_seed=0x{publisher_private_seed(wallet).hex()}")
    return 0


def _cmd_x25519_keypair(args: argparse.Namespace) -> int:
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if args.private_output:
        write_private_file(args.private_output, private_bytes)
    if args.public_output:
        args.public_output.write_text("0x" + public_bytes.hex() + "\n", encoding="utf-8")
    print(f"recipient_private_key=0x{private_bytes.hex()}")
    print(f"recipient_public_key=0x{public_bytes.hex()}")
    return 0


def _cmd_init_payload(args: argparse.Namespace) -> int:
    wallet = read_hex_or_raw_file(args.wallet_key_file)
    payload = build_init_payload(
        name=args.name,
        description=args.description,
        stream_id=parse_stream_id(args.stream_id),
        publisher_public_key=publisher_public_key(wallet),
        access_mode=args.access_mode,
        ring_buffer_capacity=args.ring_buffer_capacity,
    )
    args.output.write_bytes(payload)
    print(f"init_payload={args.output}")
    print(f"publisher_public_key=0x{publisher_public_key(wallet).hex()}")
    return 0


def _cmd_paid_message(args: argparse.Namespace) -> int:
    wallet = read_hex_or_raw_file(args.wallet_key_file)
    stream_secret = read_hex_or_raw_file(args.stream_secret_file)
    plaintext = args.plaintext_file.read_bytes()
    message = build_paid_payload(
        wallet_private_key=wallet,
        stream_secret=stream_secret,
        chain_id=args.chain_id,
        stream_id=parse_stream_id(args.stream_id),
        sequence=args.sequence,
        key_epoch=args.key_epoch,
        plaintext=plaintext,
        timestamp_unix_ms=args.timestamp_unix_ms or int(time.time() * 1000),
        kind=args.kind,
        content_type=args.content_type,
        nonce24=bytes_from_hex(args.nonce) if args.nonce else None,
    )
    args.output.write_bytes(message)
    content_key = derive_content_key(stream_secret, args.chain_id, parse_stream_id(args.stream_id), args.key_epoch)
    print(f"message={args.output}")
    print(f"content_key=0x{content_key.hex()}")
    print(f"publisher_public_key=0x{publisher_public_key(wallet).hex()}")
    return 0


def _cmd_stream_secret(args: argparse.Namespace) -> int:
    secret = secrets.token_bytes(32)
    write_private_file(args.output, secret)
    print(f"stream_secret={args.output}")
    print(f"stream_secret_hex=0x{secret.hex()}")
    return 0


def _cmd_derive_content_key(args: argparse.Namespace) -> int:
    stream_secret = read_hex_or_raw_file(args.stream_secret_file)
    key = derive_content_key(stream_secret, args.chain_id, parse_stream_id(args.stream_id), args.key_epoch)
    print(f"content_key=0x{key.hex()}")
    return 0


def _cmd_extract_signal(args: argparse.Namespace) -> int:
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("no rows found")
    args.output.write_text(json.dumps(rows[0], sort_keys=True, separators=(",", ":")), encoding="utf-8")
    print(f"plaintext={args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    publisher_key = sub.add_parser("publisher-key")
    publisher_key.add_argument("--wallet-key-file", type=Path, required=True)
    publisher_key.set_defaults(func=_cmd_publisher_key)

    x25519_keypair = sub.add_parser("x25519-keypair")
    x25519_keypair.add_argument("--private-output", type=Path)
    x25519_keypair.add_argument("--public-output", type=Path)
    x25519_keypair.set_defaults(func=_cmd_x25519_keypair)

    stream_secret = sub.add_parser("stream-secret")
    stream_secret.add_argument("--output", type=Path, required=True)
    stream_secret.set_defaults(func=_cmd_stream_secret)

    init_payload = sub.add_parser("init-payload")
    init_payload.add_argument("--wallet-key-file", type=Path, required=True)
    init_payload.add_argument("--stream-id", required=True)
    init_payload.add_argument("--name", required=True)
    init_payload.add_argument("--description", default="")
    init_payload.add_argument("--access-mode", choices=["OPEN", "SUBSCRIBER_PAID"], default="SUBSCRIBER_PAID")
    init_payload.add_argument("--ring-buffer-capacity", type=int, default=16)
    init_payload.add_argument("--output", type=Path, required=True)
    init_payload.set_defaults(func=_cmd_init_payload)

    paid_message = sub.add_parser("paid-message")
    paid_message.add_argument("--wallet-key-file", type=Path, required=True)
    paid_message.add_argument("--stream-secret-file", type=Path, required=True)
    paid_message.add_argument("--stream-id", required=True)
    paid_message.add_argument("--chain-id", type=int, required=True)
    paid_message.add_argument("--sequence", type=int, required=True)
    paid_message.add_argument("--key-epoch", type=int, required=True)
    paid_message.add_argument("--plaintext-file", type=Path, required=True)
    paid_message.add_argument("--timestamp-unix-ms", type=int)
    paid_message.add_argument("--kind", default="form4_cluster")
    paid_message.add_argument("--content-type", default="application/json")
    paid_message.add_argument("--nonce")
    paid_message.add_argument("--output", type=Path, required=True)
    paid_message.set_defaults(func=_cmd_paid_message)

    derive_key = sub.add_parser("derive-content-key")
    derive_key.add_argument("--stream-secret-file", type=Path, required=True)
    derive_key.add_argument("--stream-id", required=True)
    derive_key.add_argument("--chain-id", type=int, required=True)
    derive_key.add_argument("--key-epoch", type=int, required=True)
    derive_key.set_defaults(func=_cmd_derive_content_key)

    extract_signal = sub.add_parser("extract-signal")
    extract_signal.add_argument("--input", type=Path, required=True)
    extract_signal.add_argument("--output", type=Path, required=True)
    extract_signal.set_defaults(func=_cmd_extract_signal)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv or sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
