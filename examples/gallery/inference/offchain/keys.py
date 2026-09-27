"""Signed API keys (CIP §3.3 pre-admission permit) and their volume records.

Wire (shared-codec permit format from the runner-route wire companion):

    cwir1.<base64url-nopad(CBOR([1, chain_id, actor20, slot, key_id16, secret32, sig64]))>

`sig` is strict Ed25519 over the raw bytes

    b"cowboy/route-ingress/v1" || CBOR([1, chain_id, actor20, slot, key_id16, sha256(secret)])

Slot is a routing label (<=128 UTF-8 bytes), never hire identity; the whole
token is <=1024 bytes and must decode canonically. The gateway verifies `sig`
against the route declaration's ingress_verify_key before submitting any
RouteInvocation; the handler verifies the secret against the key record
(`keys/<key_id>.json`) and refuses revoked keys. The record never holds the
secret; the signature is not stored because it is recomputable and not secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, asdict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import cbor

PERMIT_DOMAIN = b"cowboy/route-ingress/v1"
PERMIT_VERSION = 1
TOKEN_PREFIX = "cwir1."
MAX_SLOT_BYTES = 128
MAX_TOKEN_BYTES = 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    if "=" in text:
        raise ValueError("padding present")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _check_fixed(chain_id: int, actor: bytes, slot: str, key_id: bytes) -> None:
    if not 0 <= chain_id < 1 << 64:
        raise ValueError("chain_id out of range")
    if len(actor) != 20 or len(key_id) != 16:
        raise ValueError("actor must be 20 bytes and key_id 16 bytes")
    if not 1 <= len(slot.encode("utf-8")) <= MAX_SLOT_BYTES:
        raise ValueError("slot must be 1..128 UTF-8 bytes")


def signing_bytes(chain_id: int, actor: bytes, slot: str, key_id: bytes, secret: bytes) -> bytes:
    _check_fixed(chain_id, actor, slot, key_id)
    return PERMIT_DOMAIN + cbor.encode([PERMIT_VERSION, chain_id, actor, slot, key_id, hashlib.sha256(secret).digest()])


@dataclass(frozen=True)
class ParsedKey:
    chain_id: int
    actor: bytes
    slot: str
    key_id: bytes
    secret: bytes
    sig: bytes

    @property
    def key_id_hex(self) -> str:
        return self.key_id.hex()

    @property
    def secret_sha256(self) -> str:
        return hashlib.sha256(self.secret).hexdigest()


@dataclass
class KeyRecord:
    """`keys/<key_id>.json`. Never holds the secret."""

    key_id: str
    name: str
    prefix: str
    sha256: str
    created_at: int
    revoked_at: int | None = None

    def path(self) -> str:
        return f"keys/{self.key_id}.json"

    def to_json(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n").encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "KeyRecord":
        return cls(**json.loads(raw))


def encode_token(chain_id: int, actor: bytes, slot: str, key_id: bytes, secret: bytes, sig: bytes) -> str:
    _check_fixed(chain_id, actor, slot, key_id)
    if len(secret) != 32 or len(sig) != 64:
        raise ValueError("secret must be 32 bytes and sig 64 bytes")
    token = TOKEN_PREFIX + _b64(cbor.encode([PERMIT_VERSION, chain_id, actor, slot, key_id, secret, sig]))
    if len(token) > MAX_TOKEN_BYTES:
        raise ValueError("token too long")
    return token


def issue(slot: str, name: str, issuing_sk: Ed25519PrivateKey, chain_id: int, actor: bytes, *, now: int) -> tuple[str, KeyRecord]:
    """Mint a new key for `slot`. Returns (token, record); the token is shown once."""
    if not 1 <= len(name.strip()) <= 80:
        raise ValueError("name must be 1..80 characters")
    key_id = os.urandom(16)
    secret = os.urandom(32)
    sig = issuing_sk.sign(signing_bytes(chain_id, actor, slot, key_id, secret))
    token = encode_token(chain_id, actor, slot, key_id, secret, sig)
    record = KeyRecord(
        key_id=key_id.hex(),
        name=name.strip(),
        prefix=key_id.hex()[:8],
        sha256=hashlib.sha256(secret).hexdigest(),
        created_at=now,
    )
    return token, record


def parse(token: str) -> ParsedKey | None:
    """Strict canonical decode; None for anything malformed."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > MAX_TOKEN_BYTES:
        return None
    try:
        items = cbor.decode(_unb64(token[len(TOKEN_PREFIX):]))
    except (ValueError, cbor.CborError):
        return None
    if not isinstance(items, list) or len(items) != 7:
        return None
    version, chain_id, actor, slot, key_id, secret, sig = items
    if version != PERMIT_VERSION or not isinstance(chain_id, int) or not isinstance(slot, str):
        return None
    if not all(isinstance(b, bytes) for b in (actor, key_id, secret, sig)):
        return None
    if len(actor) != 20 or len(key_id) != 16 or len(secret) != 32 or len(sig) != 64:
        return None
    if not 1 <= len(slot.encode("utf-8")) <= MAX_SLOT_BYTES:
        return None
    return ParsedKey(chain_id, actor, slot, key_id, secret, sig)


def verify_permit(parsed: ParsedKey, verify_pk: Ed25519PublicKey, chain_id: int, actor: bytes) -> bool:
    """The gateway-side check: the token names this chain and actor and its
    signature verifies. No secrets, one Ed25519 verify."""
    if parsed.chain_id != chain_id or parsed.actor != actor:
        return False
    try:
        verify_pk.verify(parsed.sig, signing_bytes(chain_id, actor, parsed.slot, parsed.key_id, parsed.secret))
        return True
    except InvalidSignature:
        return False


def check(parsed: ParsedKey, record: KeyRecord | None) -> bool:
    """The handler-side check: secret matches the record and it is not revoked."""
    if record is None or record.key_id != parsed.key_id_hex or record.revoked_at is not None:
        return False
    return hmac.compare_digest(parsed.secret_sha256, record.sha256)
