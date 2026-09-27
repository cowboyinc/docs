"""CIP-15 §8.14 caller authentication for `auth = "account"` Runner routes,
in the form ratified in dashboard-impl (2026-09-11):

    preimage  = b"cowboy/cip15/runner-route-auth/v1"
                || canonical CBOR [chain_id: uint, network: text, actor: bstr20,
                                   verb: text, handler: text,
                                   raw_target: bstr (exact origin-form path+query,
                                                     no normalization),
                                   body_sha256: bstr32, expiry: uint (unix s),
                                   nonce: bstr (1..=256)]
    digest    = keccak256(preimage)
    signature = secp256k1 over digest, 65 bytes r || s || recid, low-S
                enforced, recid raw as emitted (0..3); 27/28 rejected.

Validity: now <= expiry <= now + 3600. Replay: a cross-runner durable CAS
claim keyed (chain_id, network, actor, recovered_caller, nonce), bound to the
request digest and retained through expiry + 300 s; that ledger lives in the
management runtime, not here. The gateway forwards sig/expiry/nonce/chain_id/
network as `x-cowboy-account-*` headers and asserts nothing. This module
never holds a key outside tests.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from coincurve import PrivateKey, PublicKey
from Crypto.Hash import keccak

from . import cbor

DOMAIN_TAG = b"cowboy/cip15/runner-route-auth/v1"
MAX_NONCE_BYTES = 256
MAX_NETWORK_BYTES = 64
MAX_EXPIRY_HORIZON_S = 3600
CLAIM_GRACE_S = 300
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_N = _N // 2


class ProofError(ValueError):
    pass


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def preimage(*, chain_id: int, network: str, actor: bytes, verb: str, handler: str, raw_target: str, body: bytes, expiry: int, nonce: bytes) -> bytes:
    if not 0 < chain_id < 1 << 64 or not 0 <= expiry < 1 << 64:
        raise ProofError("chain_id/expiry out of range")
    if len(actor) != 20:
        raise ProofError("actor must be 20 bytes")
    if not 1 <= len(nonce) <= MAX_NONCE_BYTES:
        raise ProofError("nonce must be 1..256 bytes")
    if not network or len(network.encode("utf-8")) > MAX_NETWORK_BYTES or not network.isascii():
        raise ProofError("network must be 1..64 ASCII bytes")
    if not verb or not handler or not raw_target.startswith("/"):
        raise ProofError("verb, handler and an origin-form raw_target are required")
    return DOMAIN_TAG + cbor.encode([chain_id, network, actor, verb, handler, raw_target.encode("utf-8"), hashlib.sha256(body).digest(), expiry, nonce])


def digest(**fields) -> bytes:
    return keccak256(preimage(**fields))


def address_of(public_key: PublicKey) -> bytes:
    return keccak256(public_key.format(compressed=False)[1:])[12:]


def sign(private_key: PrivateKey, **fields) -> bytes:
    """Test/client helper: 65-byte r||s||recid (low-S, recid raw) over the digest."""
    sig = private_key.sign_recoverable(digest(**fields), hasher=None)
    if int.from_bytes(sig[32:64], "big") > _HALF_N:
        raise ProofError("signer produced high-S")
    return sig


@dataclass(frozen=True)
class Account:
    address: bytes
    expiry: int
    nonce: bytes
    digest: bytes  # what the replay claim binds to


def verify(*, sig: bytes, chain_id: int, network: str, actor: bytes, verb: str, handler: str, raw_target: str, body: bytes, expiry: int, nonce: bytes, now: int | None = None, max_horizon_s: int = MAX_EXPIRY_HORIZON_S) -> Account:
    """Recover the caller. Raises ProofError on any failure; the caller maps it
    to the same denial as an unknown slot. chain_id/network/actor must be the
    verifier's own deployment values, never the request's claims."""
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != 65:
        raise ProofError("signature must be 65 bytes")
    if sig[64] > 3:
        raise ProofError("recid must be raw 0..3")
    s = int.from_bytes(sig[32:64], "big")
    if s == 0 or s > _HALF_N:
        raise ProofError("signature must be low-S")
    now = int(time.time()) if now is None else now
    if not now <= expiry <= now + max_horizon_s:
        raise ProofError("expiry outside the allowed horizon")
    d = digest(chain_id=chain_id, network=network, actor=actor, verb=verb, handler=handler, raw_target=raw_target, body=body, expiry=expiry, nonce=nonce)
    try:
        pk = PublicKey.from_signature_and_message(bytes(sig), d, hasher=None)
    except Exception as exc:  # coincurve raises ValueError on bad points
        raise ProofError("signature does not recover") from exc
    return Account(address_of(pk), expiry, bytes(nonce), d)


def claim_key(*, chain_id: int, network: str, actor: bytes, caller: bytes, nonce: bytes) -> bytes:
    """Replay-claim identity: sha256 of the ratified tuple, for a CAS ledger."""
    return hashlib.sha256(cbor.encode([chain_id, network, actor, caller, nonce])).digest()
