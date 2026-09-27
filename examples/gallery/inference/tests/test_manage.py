import hashlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain.keys import issue
from offchain.journal import Journal
from offchain.manage import Account, Hire, Management, Request, Slot
from offchain.volume import ConflictError, Volume, encode_json

NOW = 1789080000
OWNER = b"h" * 20
ACTOR = b"a" * 20


class AtomicTestVolume(Volume):
    """Atomic dictionary snapshots, not an assertion about deployed CBFS."""
    def __init__(self):
        self.files = {}
        self.lock = threading.Lock()
        self.revision = 0

    def root(self):
        return str(self.revision)

    def read(self, path):
        return self.files.get(path)

    def list(self, prefix):
        return sorted(k for k in self.files if k.startswith(prefix))

    def commit(self, prev_root, writes):
        with self.lock:
            if prev_root != self.root():
                raise ConflictError()
            self.files = dict(self.files, **writes)
            self.revision += 1
            return self.root()


@pytest.fixture
def rig():
    volume = AtomicTestVolume()
    signer = Ed25519PrivateKey.generate()
    state = {"hire": Hire(OWNER, ("declaration", "version", "artifact"), "active"), "volume_calls": 0}
    nonces, lock = set(), threading.Lock()

    def consume(key, expiry, digest):
        with lock:
            if key in nonces:
                return False
            nonces.add(key)
            return True

    def verify(req):
        # Test authenticator binds every field. Production signature verifier is absent.
        assert req.proof == hashlib.sha256(repr(replace(req, proof=None)).encode()).digest()
        return Account(state.get("account", OWNER), state.get("expiry", NOW + 60), req.proof, req.proof)

    def get_volume(slot):
        state["volume_calls"] += 1
        return volume

    service = Management(actor=ACTOR, chain_id=123, network="local", slots={"cloud": Slot(7, state["hire"].pins, "https://api.example", 3)},
                         hire_lookup=lambda _: state["hire"], volume_for=get_volume,
                         verify_account=verify, consume_nonce=consume,
                         issue_key=lambda slot, name, now: issue(slot, name, signer, 123, ACTOR, now=now), clock=lambda: NOW)
    counter = 0

    def request(method="GET", path="/manage/api/keys?slot=cloud", body=b"", **changes):
        nonlocal counter
        counter += 1
        # Unique handler value models a per-request signed nonce for this test fake.
        req = Request(method, path, body, ACTOR, 123, "local", f"manage-{counter}", None)
        req = replace(req, **changes)
        return replace(req, proof=hashlib.sha256(repr(req).encode()).digest())

    return service, volume, state, request


def payload(result):
    return json.loads(result["body"])


def test_secret_once_after_commit_and_public_list(rig):
    service, volume, _, request = rig
    created = service.handle(request("POST", body=b'{"name":" OpenRouter "}'))
    assert created["status"] == 201
    assert created["headers"]["cache-control"] == "no-store"
    result = payload(created)
    assert result["secret"].startswith("cwir1.")
    assert result["key"]["name"] == "OpenRouter"
    persisted = volume.read(f'keys/{result["key"]["key_id"]}.json')
    assert result["secret"].encode() not in persisted
    assert "sha256" not in result["key"]
    listed = service.handle(request())
    assert result["secret"].encode() not in listed["body"]
    assert b"sha256" not in listed["body"]


@pytest.mark.parametrize("change", ["other_account", "unknown_slot", "pins", "expired", "future_expiry", "wrong_actor", "hire_bound"])
def test_authorization_denies_before_volume(rig, change):
    service, _, state, request = rig
    options = {}
    if change == "other_account": state["account"] = b"x" * 20
    if change == "unknown_slot": options["path"] = "/manage/api/keys?slot=missing"
    if change == "pins": state["hire"] = replace(state["hire"], pins=("bad", "bad", "bad"))
    if change == "expired": state["expiry"] = NOW - 1
    if change == "future_expiry": state["expiry"] = NOW + 3601
    if change == "wrong_actor": options["actor"] = b"b" * 20
    if change == "hire_bound": options["hire"] = 7
    result = service.handle(request(**options))
    assert payload(result) == {"error": "access_denied"}
    assert state["volume_calls"] == 0


def test_raw_target_and_body_are_verified_and_replay_is_refused(rig):
    service, _, state, request = rig
    req = request("POST", body=b'{"name":"first"}')
    assert service.handle(replace(req, body=b'{"name":"changed"}'))["status"] == 403
    assert service.handle(replace(req, raw_target="/manage/api/keys?slot=other"))["status"] == 403
    assert state["volume_calls"] == 0
    assert service.handle(req)["status"] == 201
    assert service.handle(req)["status"] == 403


@pytest.mark.parametrize("adapter", ["verify_account", "consume_nonce"])
def test_missing_verifier_and_nonce_store_fail_closed(rig, adapter):
    service, _, state, request = rig
    setattr(service, adapter, None)
    assert service.handle(request())["status"] == 503
    assert state["volume_calls"] == 0


def test_lost_commit_never_returns_secret(rig):
    service, volume, _, request = rig
    def conflict(*args): raise ConflictError()
    volume.commit = conflict
    result = service.handle(request("POST", body=b'{"name":"OR"}'))
    assert result["status"] == 503
    assert "secret" not in payload(result)
    assert not volume.files


def test_cap_is_checked_again_after_cas_conflict(rig):
    service, volume, _, request = rig
    commit = volume.commit
    other = [issue("cloud", "other", Ed25519PrivateKey.generate(), 123, ACTOR, now=NOW)[1] for _ in range(3)]
    first = True
    def race(root, writes):
        nonlocal first
        if first:
            first = False
            commit(root, {record.path(): record.to_json() for record in other})
        return commit(root, writes)
    volume.commit = race
    assert service.handle(request("POST", body=b'{"name":"loser"}'))["status"] == 409
    assert len(volume.files) == 3


def test_concurrent_create_cap(rig):
    service, volume, _, request = rig
    requests = [request("POST", body=json.dumps({"name": str(i)}).encode()) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda req: service.handle(req)["status"], requests))
    assert statuses.count(201) == 3
    assert set(statuses) <= {201, 409}
    assert len(volume.files) == 3


def test_paused_hire_can_read_and_revoke_idempotently(rig):
    service, volume, state, request = rig
    result = payload(service.handle(request("POST", body=b'{"name":"OR"}')))
    state["hire"] = replace(state["hire"], status="paused")
    assert service.handle(request())["status"] == 200
    assert service.handle(request("POST", body=b'{"name":"new"}'))["status"] == 409
    path = f'/manage/api/keys/{result["key"]["key_id"]}?slot=cloud'
    first = payload(service.handle(request("DELETE", path)))
    assert payload(service.handle(request("DELETE", path))) == first
    assert first["key"]["revoked_at"] == NOW


def test_catalog_cannot_leak_serving_targets(rig):
    service, volume, _, request = rig
    volume.mutate(lambda _: {"catalog.json": encode_json({"models": {"cowboy": {"target": "private", "upstream_model": "secret", "context_tokens": 100, "max_output_tokens": 10}}, "limits": {"max_requests": 4, "max_concurrency": 1}, "delegation": "secret"})})
    result = service.handle(request(path="/manage/api/catalog?slot=cloud"))
    assert result["status"] == 200
    assert b"private" not in result["body"] and b"secret" not in result["body"]


def add_record(volume, session, segment, *, request_id="same-id", usage=None, price="original", **changes):
    record = dict(slot="cloud", hire_id=7, session_id=session, request_id=request_id,
                  key_id="key", model="model", ts=NOW, finished_ts=NOW,
                  outcome="completed", usage=usage or {"known": {"input": 10, "output": 2}}, price_version=price)
    record.update(changes)
    volume.mutate(lambda _: {f"usage/{session}/{segment:06d}.jsonl": encode_json(record)})


def test_usage_preserves_sessions_unknowns_and_immutable_prices(rig):
    service, volume, _, request = rig
    add_record(volume, "s1", 1, usage={"pending": True})
    add_record(volume, "s1", 2)
    add_record(volume, "s2", 1, usage={"unknown": True})
    add_record(volume, "s3", 1, price=None)
    volume.mutate(lambda _: {"pricing/original.json": encode_json({"schema": 1, "unit": "nano_usd_per_token", "models": {"model": {"input": 100, "output": 200}}}), "catalog.json": encode_json({"pricing_version": "new-price"})})
    result = payload(service.handle(request(path="/manage/api/usage?slot=cloud")))
    assert result["totals"] == dict(requests=3, input_tokens="20", output_tokens="4", unknown_usage_requests=1,
                                     known_usage_requests=2, usage_cost_nano_usd="1400", unpriced_requests=2,
                                     partial=True, cost_partial=True)
    assert result["last_committed_at"] is None


def test_only_unknown_usage_is_unavailable_not_zero(rig):
    service, volume, _, request = rig
    add_record(volume, "s1", 1, usage={"unknown": True})
    totals = payload(service.handle(request(path="/manage/api/usage?slot=cloud")))["totals"]
    assert totals["input_tokens"] is None
    assert totals["output_tokens"] is None
    assert totals["usage_cost_nano_usd"] is None
    assert totals["partial"]


def test_real_journal_segments_feed_management(rig):
    service, volume, _, request = rig
    journal = Journal(volume, slot="cloud", hire_id=7, session_id="session", price_version=None, clock=lambda: NOW + 1)
    journal.reserve("request", key_id="key", model="model", target="private-target", limits={})
    journal.finish("request", "completed", {"known": {"input": 11, "output": 12}})
    journal.flush()
    result = service.handle(request(path="/manage/api/usage?slot=cloud"))
    assert result["status"] == 200
    assert payload(result)["totals"]["requests"] == 1
    assert payload(result)["totals"]["input_tokens"] == "11"
    assert payload(result)["last_committed_at"] == NOW + 1
    assert b"private-target" not in result["body"]


def test_usage_scope_and_scan_bounds_fail_closed(rig):
    service, volume, _, request = rig
    add_record(volume, "s1", 1, hire_id=99)
    assert service.handle(request(path="/manage/api/usage?slot=cloud"))["status"] == 503
    volume.list = lambda prefix: [str(i) for i in range(4097)]
    assert payload(service.handle(request(path="/manage/api/usage?slot=cloud"))) == {"error": "usage_scan_limit"}


@pytest.mark.parametrize("query", ["slot=cloud&slot=cloud", "slot=cloud&from=bad", "slot=cloud&from=2020-01-01&to=2026-01-01"])
def test_invalid_queries(rig, query):
    service, _, _, request = rig
    assert service.handle(request(path="/manage/api/usage?" + query))["status"] == 400


def test_public_shell_requires_no_wallet_or_volume(rig):
    service, _, state, request = rig
    service.verify_account = None
    for path in ("/manage?slot=cloud", "/manage/assets/manage.js", "/manage/assets/manage.css"):
        result = service.handle(request(path=path))
        assert result["status"] == 200
        assert result["headers"]["cache-control"] == "no-store"
    assert state["volume_calls"] == 0
    assert service.handle(request())["status"] == 503


def test_numeric_segment_order_and_exact_large_totals(rig):
    service, volume, _, request = rig
    add_record(volume, "s1", 999999, usage={"pending": True})
    add_record(volume, "s1", 1000000, usage={"known": {"input": 2**63 + 1, "output": 0}})
    totals = payload(service.handle(request(path="/manage/api/usage?slot=cloud")))["totals"]
    assert totals["input_tokens"] == str(2**63 + 1)
    assert not totals["partial"]


def test_pricing_version_read_once_per_snapshot(rig):
    service, volume, _, request = rig
    for i in range(3): add_record(volume, f"s{i}", 1)
    reads = []
    read = volume.read
    def counted(path):
        reads.append(path)
        return read(path)
    volume.read = counted
    assert service.handle(request(path="/manage/api/usage?slot=cloud"))["status"] == 200
    assert reads.count("pricing/original.json") == 1
