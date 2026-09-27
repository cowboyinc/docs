import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from offchain.manage import Slot
from offchain.nonces import NonceStore
from offchain.volume import ConflictError
from .test_manage import AtomicTestVolume, ACTOR, NOW, OWNER, payload, rig
from .test_cbfs_volume import FAKE


def test_nonce_is_shared_across_workers_and_survives_recreation():
    volume = AtomicTestVolume()
    key = (123, "local", ACTOR, OWNER, b"one-nonce")
    def consume(_):
        return NonceStore(volume, clock=lambda: NOW)(key, NOW + 60, b"d" * 32)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(consume, range(16)))
    assert results.count(True) == 1
    assert results.count(False) == 15
    assert not NonceStore(volume, clock=lambda: NOW)(key, NOW + 100, b"d" * 32)
    assert b"one-nonce" not in repr(volume.files).encode()


def test_nonce_retries_conflict_and_rechecks_expiry():
    volume = AtomicTestVolume()
    key = (123, "local", ACTOR, OWNER, b"nonce")
    now = [NOW]
    def conflict(root, writes):
        now[0] += 61
        raise ConflictError()
    volume.commit = conflict
    assert not NonceStore(volume, clock=lambda: now[0])(key, NOW + 60, b"d" * 32)
    assert volume.files == {}


def test_nonce_recovers_lost_ack_only_within_original_call():
    volume = AtomicTestVolume()
    commit = volume.commit
    def commit_then_lose_ack(root, writes):
        commit(root, writes)
        raise ConflictError("commit landed but acknowledgement was lost")
    volume.commit = commit_then_lose_ack
    store = NonceStore(volume, clock=lambda: NOW)
    key = (123, "local", ACTOR, OWNER, b"lost-ack")
    assert store(key, NOW + 60, b"d" * 32)
    assert volume.revision == 1
    assert not store(key, NOW + 60, b"d" * 32)
    assert not NonceStore(volume, clock=lambda: NOW)(key, NOW + 60, b"d" * 32)
    assert not store(key, NOW + 60, b"x" * 32)
    assert volume.revision == 1


def test_identical_nonce_racers_have_one_winner_despite_lost_ack():
    volume = AtomicTestVolume()
    commit = volume.commit
    def commit_then_lose_ack(root, writes):
        commit(root, writes)
        raise ConflictError("commit landed but acknowledgement was lost")
    volume.commit = commit_then_lose_ack
    # Share even the store instance: attempt ownership must belong to each call.
    store = NonceStore(volume, clock=lambda: NOW)
    key = (123, "local", ACTOR, OWNER, b"identical-race")
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: store(key, NOW + 60, b"d" * 32), range(16)))
    assert results.count(True) == 1
    assert results.count(False) == 15
    assert volume.revision == 1


def test_legacy_nonce_record_cannot_authorize_same_digest_retry():
    volume = AtomicTestVolume()
    store = NonceStore(volume, clock=lambda: NOW)
    key = (123, "local", ACTOR, OWNER, b"legacy")
    assert store(key, NOW + 60, b"d" * 32)
    path, raw = next(iter(volume.files.items()))
    record = json.loads(raw)
    record.pop("attempt_id")
    record["schema"] = 1
    volume.files[path] = json.dumps(record).encode()
    assert not store(key, NOW + 60, b"d" * 32)


def test_nonce_backend_failure_never_accepts():
    volume = AtomicTestVolume()
    def fail(*_):
        raise OSError("unavailable")
    volume.commit = fail
    with pytest.raises(OSError):
        NonceStore(volume, clock=lambda: NOW)((123, "local", ACTOR, OWNER, b"n"), NOW + 60, b"d" * 32)


def test_nonce_scope_includes_account_and_deployment():
    store = NonceStore(AtomicTestVolume(), clock=lambda: NOW)
    key = (123, "local", ACTOR, OWNER, b"nonce")
    assert store(key, NOW + 60, b"d" * 32)
    for index, value in ((0, 124), (1, "other"), (2, b"b" * 20), (3, b"z" * 20), (4, b"other")):
        changed = list(key)
        changed[index] = value
        assert store(tuple(changed), NOW + 60, b"d" * 32)


class Grants:
    def __init__(self):
        self.calls = []

    def put(self, slot, account, signing_key, encryption_key):
        self.calls.append(("put", slot.hire_id, account, signing_key, encryption_key))
        return {"owner": "ab" * 20, "volume": "hire-7", "private": "must not leak"}

    def remove(self, slot, account):
        self.calls.append(("remove", slot.hire_id, account))


def test_grant_uses_retained_hirer_and_only_returns_public_coordinates(rig):
    service, _, state, request = rig
    service.grants = Grants()
    state["hire"] = replace(state["hire"], status="paused")
    result = service.handle(request("PUT", "/manage/api/read-grant?slot=cloud",
                                    json.dumps({"signing_key": "11" * 32, "encryption_key": "22" * 32}).encode()))
    assert result["status"] == 200
    assert payload(result) == {"owner": "ab" * 20, "volume": "hire-7", "access": "read_only"}
    assert service.grants.calls == [("put", 7, OWNER, bytes.fromhex("11" * 32), bytes.fromhex("22" * 32))]
    assert service.handle(request("DELETE", "/manage/api/read-grant?slot=cloud"))["status"] == 200
    assert service.grants.calls[-1] == ("remove", 7, OWNER)


@pytest.mark.parametrize("body", [b'null', b'[]', b'{}', b'{"account":"other"}',
    json.dumps({"signing_key": "00" * 32, "encryption_key": "22" * 32}).encode(),
    json.dumps({"signing_key": "11" * 32, "encryption_key": "22" * 32, "access": "read_write"}).encode()])
def test_grant_rejects_invalid_or_expanded_authority(rig, body):
    service, _, _, request = rig
    service.grants = Grants()
    assert service.handle(request("PUT", "/manage/api/read-grant?slot=cloud", body))["status"] == 400
    assert service.grants.calls == []


def test_wrong_wallet_cannot_create_or_remove_grant(rig):
    service, _, state, request = rig
    service.grants = Grants()
    state["account"] = b"z" * 20
    for method in ("PUT", "DELETE"):
        assert service.handle(request(method, "/manage/api/read-grant?slot=cloud"))["status"] == 403
    assert service.grants.calls == []


def test_real_proof_to_management_cbfs_cli_and_grant_composition(tmp_path):
    """Real crypto + subprocess adapter, fake CLI storage/chain only."""
    from coincurve import PrivateKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from offchain import account_proof as ap
    from offchain.keys import issue
    from offchain.manage import Hire, Request
    from offchain.management_runtime import AccountProof, VolumeBinding, build_management
    wallet = PrivateKey(b"\x01" * 32)
    owner = ap.address_of(wallet.public_key)
    signer = Ed25519PrivateKey.generate()
    pins = ("declaration", "version", "artifact")
    slots = {"cloud": Slot(7, pins, "https://api.example", 3), "second": Slot(8, pins, "https://api2.example", 3)}
    bindings = {hire: VolumeBinding(f"hire-{hire}", str(tmp_path / f"hire-{hire}"), ACTOR, binary=FAKE) for hire in (7, 8)}
    nonce_binding = VolumeBinding("nonces", str(tmp_path / "nonces"), ACTOR, binary=FAKE)
    def build():
        return build_management(actor=ACTOR, chain_id=123, network="local", slots=slots,
            hire_lookup=lambda _: Hire(owner, pins, "active"), volumes=bindings,
            nonce_volume=nonce_binding, grant_bindings=bindings, clock=lambda: NOW,
            issue_key=lambda slot, name, now: issue(slot, name, signer, 123, ACTOR, now=now))
    def req(method="POST", path="/manage/api/keys?slot=cloud", body=b'{"name":"OR"}', nonce=b"nonce", key=wallet):
        fields = dict(chain_id=123, network="local", actor=ACTOR, handler="offchain.manage.Management.handle",
                      verb=method, raw_target=path, body=body, expiry=NOW + 60, nonce=nonce)
        proof = AccountProof(ap.sign(key, **fields), NOW + 60, nonce)
        return Request(method, path, body, ACTOR, 123, "local", fields["handler"], proof)
    service = build()
    original = req()
    assert service.handle(original)["status"] == 201
    # Recreating the handler still sees the committed replay record.
    assert build().handle(original)["status"] == 403
    # Signing another slot with the same nonce cannot evade the actor-wide store.
    assert build().handle(req(path="/manage/api/keys?slot=second"))["status"] == 403
    assert service.handle(replace(req(nonce=b"new"), body=b'{"name":"tampered"}'))["status"] == 403
    assert service.handle(req(nonce=b"wrong", key=PrivateKey(b"\x02" * 32)))["status"] == 403
    assert service.handle(replace(req(nonce=b"handler"), handler="other"))["status"] == 403
    assert service.handle(replace(req(nonce=b"chain"), chain_id=124))["status"] == 403
    result = service.handle(req("PUT", "/manage/api/read-grant?slot=cloud",
        json.dumps({"signing_key": "11" * 32, "encryption_key": "22" * 32}).encode(), nonce=b"grant"))
    assert result["status"] == 200
    assert payload(result) == {"owner": ACTOR.hex(), "volume": "hire-7", "access": "read_only"}
    lines = [json.loads(line) for line in (tmp_path / "hire-7" / "calls.log").read_text().splitlines()]
    grant_call = next(line for line in lines if line[:2] == ["volume", "allow"])
    assert grant_call[grant_call.index("--account") + 1] == "0x" + owner.hex()
    assert grant_call[grant_call.index("--mode") + 1] == "ro"
    nonce_files = nonce_binding.volume().list("nonces/")
    claimed = json.loads(nonce_binding.volume().read(nonce_files[0]))
    assert len(claimed["digest"]) == 64


def test_nonce_claim_uses_shared_identity_fixture_and_retention():
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures/account_proof.json").read_text())
    f = fixture["request"]
    volume = AtomicTestVolume()
    now = [f["expiry"]]
    store = NonceStore(volume, clock=lambda: now[0])
    key = (f["chain_id"], f["network"], bytes.fromhex(f["actor_hex"]), bytes.fromhex(fixture["address_hex"]), bytes.fromhex(f["nonce_hex"]))
    digest = bytes.fromhex(fixture["digest_hex"])
    assert store(key, f["expiry"], digest)
    assert list(volume.files) == ["nonces/" + fixture["claim_key_hex"] + ".json"]
    now[0] += 300
    assert not store(key, now[0] + 60, digest)
    now[0] += 1
    assert store(key, now[0] + 60, digest)


def test_executor_replay_mode_is_explicit_and_never_falls_back(tmp_path):
    """Real wallet proof; fake authenticated executor claim for this seam test."""
    from coincurve import PrivateKey
    from offchain import account_proof as ap
    from offchain.manage import Hire, Request
    from offchain.management_runtime import AccountProof, VolumeBinding, build_management
    wallet = PrivateKey(b"\x03" * 32)
    owner = ap.address_of(wallet.public_key)
    pins = ("d", "v", "a")
    fields = dict(chain_id=123, network="local", actor=ACTOR, handler="offchain.manage.Management.handle",
                  verb="GET", raw_target="/manage/api/keys?slot=cloud", body=b"", expiry=NOW + 60, nonce=b"claim")
    proof = AccountProof(ap.sign(wallet, **fields), fields["expiry"], fields["nonce"])
    req = Request("GET", fields["raw_target"], b"", ACTOR, 123, "local", fields["handler"], proof)
    binding = VolumeBinding("hire", str(tmp_path / "hire"), ACTOR, binary=FAKE)
    options = dict(actor=ACTOR, chain_id=123, network="local", slots={"cloud": Slot(7, pins, "https://api.example")},
                   hire_lookup=lambda _: Hire(owner, pins, "active"), volumes={7: binding}, clock=lambda: NOW)
    claim = object()  # Models an authenticated context object, not HTTP JSON.
    seen, calls = set(), []
    def verify_claim(request, caller, claim_key, digest):
        calls.append((caller, claim_key, digest))
        assert caller == owner and digest == ap.digest(**fields)
        assert claim_key == ap.claim_key(chain_id=123, network="local", actor=ACTOR, caller=owner, nonce=b"claim")
        if request.executor_claim is not claim or claim in seen:
            return False
        seen.add(claim)
        return True
    service = build_management(**options, executor_claim_verifier=verify_claim)
    assert service.handle(req)["status"] == 403
    assert calls == []  # Missing claim never falls back to local consumption.
    assert service.handle(replace(req, executor_claim={"verified": True}))["status"] == 403
    assert service.handle(replace(req, executor_claim=claim))["status"] == 200
    assert service.handle(replace(req, executor_claim=claim))["status"] == 403
    assert not (tmp_path / "nonces").exists()
    # Merely attaching a claim cannot switch a management-owned replay guard.
    local = build_management(**options, nonce_volume=VolumeBinding("nonces", str(tmp_path / "nonces"), ACTOR, binary=FAKE))
    assert local.handle(replace(req, executor_claim=claim))["status"] == 403
    assert not (tmp_path / "nonces").exists()
    assert local.handle(req)["status"] == 200
    with pytest.raises(ValueError, match="replay protection"):
        build_management(**options)


def test_volume_binding_passes_trusted_checkpoint_to_both_cli_adapters(tmp_path):
    import sys
    from offchain.management_runtime import VolumeBinding
    binary = tmp_path / "cbfs-test"
    log = tmp_path / "arguments.jsonl"
    binary.write_text(f"#!{sys.executable}\nimport json,sys\nwith open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nprint(json.dumps({{'manifest_root': 'aa'*32, 'ok': True}}))\n")
    binary.chmod(0o700)
    checkpoint = str(tmp_path / "trusted checkpoint.json")
    binding = VolumeBinding("hire-7", str(tmp_path / "owner-state"), ACTOR,
                            binary=str(binary), rpc_url="http://127.0.0.1:4000",
                            extra_args=("--trusted-checkpoint", checkpoint))
    assert binding.volume().root() == "aa" * 32
    binding.grants().put_read_grant("0x" + OWNER.hex(), "11" * 32, "22" * 32)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    expected = ["--json", "--state-dir", binding.state_dir, "--rpc-url", binding.rpc_url, "--trusted-checkpoint", checkpoint]
    assert len(calls) == 2
    assert all(call[:len(expected)] == expected for call in calls)
    assert calls[0][len(expected):] == ["root", "hire-7"]
    assert calls[1][len(expected):len(expected) + 3] == ["volume", "allow", "hire-7"]
