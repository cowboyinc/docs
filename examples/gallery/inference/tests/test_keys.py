import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offchain import cbor, keys

ACTOR = bytes(range(20))
CHAIN = 9999


def reissue(parsed, **changes):
    """Re-encode a parsed token with some fields replaced (signature kept)."""
    f = dict(chain_id=parsed.chain_id, actor=parsed.actor, slot=parsed.slot, key_id=parsed.key_id, secret=parsed.secret, sig=parsed.sig)
    f.update(changes)
    return keys.parse(keys.encode_token(**f))


def test_issue_parse_verify_check_round_trip():
    sk = Ed25519PrivateKey.generate()
    token, record = keys.issue("cloud", "openrouter", sk, CHAIN, ACTOR, now=100)
    assert token.startswith("cwir1.") and "=" not in token
    parsed = keys.parse(token)
    assert parsed and parsed.slot == "cloud" and parsed.chain_id == CHAIN and parsed.actor == ACTOR
    assert keys.verify_permit(parsed, sk.public_key(), CHAIN, ACTOR)
    assert keys.check(parsed, record)
    assert record.prefix == parsed.key_id_hex[:8]
    assert "secret" not in record.to_json().decode() and parsed.secret.hex() not in record.to_json().decode()


def test_signing_bytes_are_domain_plus_canonical_cbor():
    sk = Ed25519PrivateKey.generate()
    token, _ = keys.issue("cloud", "k", sk, CHAIN, ACTOR, now=1)
    p = keys.parse(token)
    import hashlib

    expected = b"cowboy/route-ingress/v1" + cbor.encode([1, CHAIN, ACTOR, "cloud", p.key_id, hashlib.sha256(p.secret).digest()])
    assert keys.signing_bytes(CHAIN, ACTOR, "cloud", p.key_id, p.secret) == expected
    sk.public_key().verify(p.sig, expected)


def test_permit_binds_chain_actor_slot_and_secret():
    sk = Ed25519PrivateKey.generate()
    token, _ = keys.issue("cloud", "k", sk, CHAIN, ACTOR, now=1)
    parsed = keys.parse(token)
    other_sk = Ed25519PrivateKey.generate()
    assert not keys.verify_permit(parsed, other_sk.public_key(), CHAIN, ACTOR)
    # Gateway configured for another chain/actor refuses a token naming this one.
    assert not keys.verify_permit(parsed, sk.public_key(), CHAIN + 1, ACTOR)
    assert not keys.verify_permit(parsed, sk.public_key(), CHAIN, bytes(20))
    # Re-encoded with a different slot / chain / secret: signature no longer covers it.
    for changed in (dict(slot="other"), dict(chain_id=CHAIN + 1), dict(secret=bytes(32))):
        t = reissue(parsed, **changed)
        assert t is not None
        assert not keys.verify_permit(t, sk.public_key(), t.chain_id, t.actor)


def test_handler_check_refuses_wrong_secret_and_revoked():
    sk = Ed25519PrivateKey.generate()
    token, record = keys.issue("cloud", "k", sk, CHAIN, ACTOR, now=1)
    parsed = keys.parse(token)
    wrong = reissue(parsed, secret=bytes(32))
    assert not keys.check(wrong, record)
    assert not keys.check(parsed, None)
    record.revoked_at = 2
    assert not keys.check(parsed, record)


def test_parse_rejects_malformed():
    sk = Ed25519PrivateKey.generate()
    token, _ = keys.issue("cloud", "k", sk, CHAIN, ACTOR, now=1)
    body = token[len("cwir1."):]
    bad = [
        "", "cwir1.", "cow_cloud_x", "bearer", token + "=", "cwir2." + body,
        "cwir1." + body[:-4],                          # truncated
        "cwir1." + keys._b64(cbor.encode([1, CHAIN, ACTOR, "cloud", bytes(16), bytes(32)])),  # 6 items
        "cwir1." + keys._b64(cbor.encode([2, CHAIN, ACTOR, "cloud", bytes(16), bytes(32), bytes(64)])),  # version
        "cwir1." + keys._b64(cbor.encode([1, CHAIN, bytes(19), "cloud", bytes(16), bytes(32), bytes(64)])),  # actor len
        "cwir1." + keys._b64(cbor.encode([1, CHAIN, ACTOR, "", bytes(16), bytes(32), bytes(64)])),  # empty slot
        "cwir1." + keys._b64(cbor.encode([1, CHAIN, ACTOR, "x" * 129, bytes(16), bytes(32), bytes(64)])),
        "cwir1." + keys._b64(cbor.encode([1, CHAIN, ACTOR, "cloud", bytes(16), bytes(32), bytes(64)]) + b"\x00"),  # trailing
    ]
    for b in bad:
        assert keys.parse(b) is None, b[:40]


def test_parse_rejects_non_canonical_cbor():
    # chain_id 5 encoded as 0x18 0x05 (non-shortest) inside an otherwise valid array.
    body = b"\x87" + b"\x01" + b"\x18\x05" + cbor.encode(ACTOR) + cbor.encode("cloud") + cbor.encode(bytes(16)) + cbor.encode(bytes(32)) + cbor.encode(bytes(64))
    assert keys.parse("cwir1." + keys._b64(body)) is None
    with pytest.raises(cbor.CborError):
        cbor.decode(b"\x18\x05")
    # Indefinite-length array refused.
    with pytest.raises(cbor.CborError):
        cbor.decode(b"\x9f\x01\xff")
