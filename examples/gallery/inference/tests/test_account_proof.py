import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coincurve import PrivateKey

from offchain import account_proof as ap, cbor

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "account_proof.json")
KEY = PrivateKey(bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"))
FIELDS = dict(chain_id=9999, network="canyon", actor=bytes.fromhex("00112233445566778899aabbccddeeff00112233"), verb="POST", handler="offchain.manage.Management.handle", raw_target="/manage/api/keys?slot=cloud", body=b'{"name":"openrouter"}', expiry=1_757_600_000, nonce=bytes.fromhex("0102030405060708090a0b0c0d0e0f10"))


def test_matches_shared_fixture():
    """The fixture is what the runner (Gate 4), gateway (Gate 5) and the page
    signer verify against. Regenerate deliberately, never by accident."""
    fx = json.load(open(FIXTURE))
    f = fx["request"]
    fields = dict(chain_id=f["chain_id"], network=f["network"], actor=bytes.fromhex(f["actor_hex"]), verb=f["verb"], handler=f["handler"], raw_target=f["raw_target"], body=bytes.fromhex(f["body_hex"]), expiry=f["expiry"], nonce=bytes.fromhex(f["nonce_hex"]))
    assert fields == FIELDS
    pre = ap.preimage(**fields)
    assert pre.hex() == fx["preimage_hex"]
    assert pre.startswith(b"cowboy/cip15/runner-route-auth/v1")
    assert cbor.decode(pre[len(ap.DOMAIN_TAG):]) == [9999, "canyon", FIELDS["actor"], "POST", FIELDS["handler"], FIELDS["raw_target"].encode(), __import__("hashlib").sha256(FIELDS["body"]).digest(), FIELDS["expiry"], FIELDS["nonce"]]
    assert ap.digest(**fields).hex() == fx["digest_hex"]
    sig = ap.sign(PrivateKey(bytes.fromhex(fx["private_key_hex"])), **fields)
    assert sig.hex() == fx["signature_hex"]  # RFC 6979 deterministic
    acct = ap.verify(sig=sig, now=fields["expiry"] - 10, **fields)
    assert acct.address.hex() == fx["address_hex"] and acct.digest == ap.digest(**fields)
    assert ap.claim_key(chain_id=9999, network="canyon", actor=FIELDS["actor"], caller=acct.address, nonce=FIELDS["nonce"]).hex() == fx["claim_key_hex"]


def test_every_field_is_bound():
    sig = ap.sign(KEY, **FIELDS)
    good = ap.verify(sig=sig, now=FIELDS["expiry"] - 10, **FIELDS).address
    for k, v in [("chain_id", 9998), ("network", "mesa"), ("actor", bytes(20)), ("verb", "GET"), ("handler", "other"), ("raw_target", "/manage/api/keys?slot=other"), ("raw_target", "/manage/api/keys/?slot=cloud"), ("body", b"{}"), ("expiry", FIELDS["expiry"] + 1), ("nonce", b"\x00" * 16)]:
        f = dict(FIELDS, **{k: v})
        try:
            other = ap.verify(sig=sig, now=FIELDS["expiry"] - 10, **f).address
        except ap.ProofError:
            continue
        assert other != good, k


def test_horizon_low_s_and_recid_policy():
    sig = ap.sign(KEY, **FIELDS)
    ok = lambda now: ap.verify(sig=sig, now=now, **FIELDS)
    ok(FIELDS["expiry"])            # now == expiry accepted
    ok(FIELDS["expiry"] - 3600)     # exactly at the horizon accepted
    with pytest.raises(ap.ProofError):
        ok(FIELDS["expiry"] + 1)
    with pytest.raises(ap.ProofError):
        ok(FIELDS["expiry"] - 3601)
    high_s = sig[:32] + (ap._N - int.from_bytes(sig[32:64], "big")).to_bytes(32, "big") + bytes([sig[64] ^ 1])
    with pytest.raises(ap.ProofError):
        ok_sig = ap.verify(sig=high_s, now=FIELDS["expiry"] - 10, **FIELDS)
    for bad in (sig[:64], sig[:64] + bytes([27]), sig[:64] + bytes([28]), sig[:64] + bytes([4]), b"\x00" * 65):
        with pytest.raises(ap.ProofError):
            ap.verify(sig=bad, now=FIELDS["expiry"] - 10, **FIELDS)
    for bad_fields in (dict(nonce=b""), dict(nonce=b"x" * 257), dict(network="x" * 65), dict(network="cañon"), dict(raw_target="manage")):
        with pytest.raises(ap.ProofError):
            ap.preimage(**dict(FIELDS, **bad_fields))
