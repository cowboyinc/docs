"""Behavioral tests for the DoS/replay hardening (Codex 2nd-pass findings 1-3).

These drive the REAL twilio.py handlers with a minimal in-process cowboy_sdk
stub and a dict-backed storage, so the assertions exercise the actual
active-store / replay-guard / inflight logic — not a re-implementation.

Run: python3 test_dos_hardening.py
"""

import json
import pathlib
import sys
import types
import importlib.util

HERE = pathlib.Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent

# ── minimal cowboy_sdk stub (only what twilio.py touches) ─────────────────────
_ident = lambda f: f  # noqa: E731


def _route(*_a, **_k):
    return _ident


sdk = types.ModuleType("cowboy_sdk")
sdk.actor = _ident
sdk.pure = _ident
sdk.deferred = _ident
# public: bare (@public) or parameterized (@public(decode="cbor")).
sdk.public = lambda fn=None, *, decode="raw": (fn if fn is not None else _ident)
sdk.capture = lambda: types.SimpleNamespace()
sdk.OWNER = "OWNER"
sdk.callable_by = lambda *_a, **_k: _ident


class _Runner:
    continuation = staticmethod(_ident)

    @staticmethod
    def agent(*a, **k):
        return None

    @staticmethod
    def http(*a, **k):
        return None

    @staticmethod
    def handle_runner_result(*a, **k):
        return None


sdk.runner = _Runner()


# `await runner.agent(...)` yields a RunnerResult, not a raw dict.
# Minimal faithful mirror of pvm/Lib/cowboy_sdk/runner.py::RunnerResult (.text).
class _RunnerResult:
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    @property
    def data(self):
        # Alias of .raw (mirrors pvm/Lib/cowboy_sdk/runner.py::RunnerResult) — the
        # actors unwrap via getattr(result, "data", result) before any dict check.
        return self.raw

    @property
    def text(self):
        r = self.raw
        for _ in range(8):
            if not isinstance(r, dict):
                break
            for k in ("content", "text", "response", "data", "answer"):
                if k in r:
                    r = r[k]
                    break
            else:
                break
        if r is None:
            return ""
        if isinstance(r, (bytes, bytearray)):
            return bytes(r).decode("utf-8", "replace")
        return r if isinstance(r, str) else str(r)


# codec: identity-ish CBOR stand-in (dict<->bytes via repr is enough here since
# we only round-trip within the test).
import pickle as _pickle  # noqa: E402


class _Codec:
    # CBOR stand-in: round-trips dicts AND bytes (the real codec's job here).
    @staticmethod
    def encode(v):
        return _pickle.dumps(v)

    @staticmethod
    def decode(b):
        return _pickle.loads(bytes(b))


sdk.codec = _Codec()

# runtime: sender is a test-controlled global; everything else no-op.
_STATE = {"sender": b"\x00" * 20, "self": b"\x11" * 20, "height": 1}
# Funded self-timers the actors schedule (D36 fix): (height, payload, access_limit).
_TIMERS = []


class _Runtime:
    @staticmethod
    def get_sender():
        return _STATE["sender"]

    @staticmethod
    def get_actor_address():
        return _STATE["self"]

    @staticmethod
    def emit_event(*a, **k):
        return None

    @staticmethod
    def assume_ownership_if_unowned():
        return None

    @staticmethod
    def get_timestamp_ms():
        return 0

    @staticmethod
    def get_block_height():
        return _STATE["height"]

    @staticmethod
    def schedule_timer_ex(height, payload, fee_payer=None, gas_limit=None,
                          access_limit=None, expires_at=None):
        # Mirrors the deployed (access-metered) SDK signature LITERALLY —
        # node origin/devnet 22e325b9 pvm/Lib/cowboy_sdk/runtime.py:809-816:
        #   schedule_timer_ex(height, payload, fee_payer=None, gas_limit=None,
        #                     access_limit=None, expires_at=None)
        # devnet is the examples' deploy target; `main` trails it and is pre-meter,
        # and bronco bundles a FROZEN pre-meter SDK WITHOUT access_limit, so bronco
        # rejects this call — a known frozen-SDK skew (COW-3899, same class as the
        # decode= skew), NOT a chain fact; the actor targets devnet, keep the kwarg.
        _TIMERS.append((height, payload, access_limit))
        return b"timer-id"

    @staticmethod
    def set_raw(*a, **k):
        return None


sdk.runtime = _Runtime()

# call/send: record and no-op (we don't cross actors in these tests).
_CALLS = []
sdk.call = lambda target, method, args=None, **k: _CALLS.append((target, method, args))
sdk.send = lambda target, payload=None, **k: _CALLS.append((target, "send", payload))


routes = types.ModuleType("cowboy_sdk.routes")
routes.get = _route
routes.post = _route
# Install the stub only while the actor modules import (they bind their
# cowboy_sdk names at import time), then restore whatever was there so the
# sibling SimulatedChain suites keep the real SDK.
_saved_sdk_modules = {
    name: sys.modules.get(name) for name in ("cowboy_sdk", "cowboy_sdk.routes")
}
sys.modules["cowboy_sdk"] = sdk
sys.modules["cowboy_sdk.routes"] = routes

# import the real actor AFTER the stub is installed
try:
    TWILIO_PATH = EXAMPLE_DIR / "actors" / "twilio" / "main.py"
    _twilio_spec = importlib.util.spec_from_file_location("twilio_test", TWILIO_PATH)
    T = importlib.util.module_from_spec(_twilio_spec)
    _twilio_spec.loader.exec_module(T)

    PROXY_PATH = EXAMPLE_DIR / "actors" / "relay" / "main.py"
    _proxy_spec = importlib.util.spec_from_file_location("twiliorelay_test", PROXY_PATH)
    P = importlib.util.module_from_spec(_proxy_spec)
    _proxy_spec.loader.exec_module(P)
finally:
    for _name, _mod in _saved_sdk_modules.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


class MockStorage(dict):
    def set_raw(self, k, v):
        self[k] = v


def new_actor(webhook_url="http://gw/twilio/sms"):
    a = T.TwilioActor()
    a.storage = MockStorage()
    _STATE["sender"] = b"\xEE" * 20  # owner/deployer
    a.init(None)
    a.storage[T.KEY_WEBHOOK_URL] = webhook_url
    a.storage[T.KEY_DECLARATION] = {"declaration_id": "0xabc", "version": 1, "artifact_id": 0}
    return a


def connect(a, number, proxy_bytes):
    # per-item tenant record + index entry (mirrors _put_tenant)
    a._put_tenant(number, {
        "hire_id": 1, "artifact_id": 0, "proxy": proxy_bytes.hex(),
        "state_volume": "", "hirer": "ee" * 20, "status": "active",
    })


def active_count(a):
    """Count live per-item request records via the per-tenant indexes."""
    n = 0
    for number in a._tenant_numbers():
        n += len(a._ta_ids(number))
    return n


def form_body(to, frm, sid, body="hi"):
    parts = [("AccountSid", "ACx"), ("MessageSid", sid), ("From", frm),
             ("To", to), ("Body", body), ("NumMedia", "0")]
    from urllib.parse import quote
    return "&".join(f"{k}={quote(v)}" for k, v in parts).encode("utf-8")


def envelope(body, sig="sig"):
    return T.codec.encode({
        "method": "POST", "path": "/twilio/sms",
        "headers": [["x-twilio-signature", sig]], "body": body,
    })


# ── Test 1: active never evicted; per-tenant pre-MAC load cap drops overflow ──
def test_active_not_evicted_and_tenant_cap():
    a = new_actor()
    proxy = b"\xAB" * 20
    num = "+15551110000"
    connect(a, num, proxy)
    per = T.MAX_TENANT_INFLIGHT
    # fill the tenant to its pre-MAC inflight cap (distinct correspondents so the
    # per-conversation single-flight doesn't drop them first)
    for u in range(per):
        a.twilio_http(envelope(form_body(num, f"+1999{u:07d}", f"SM{u:03d}")))
    assert active_count(a) == per, f"expected {per} live, got {active_count(a)}"
    # every live request is still resolvable (per-item a:<id> records are never
    # evicted — removed only on finish)
    for rid in range(1, per + 1):
        assert a._request_by_id(rid) is not None, f"active request {rid} was evicted!"
    # the (per+1)th relay for this tenant is dropped by the per-tenant load cap —
    # this is a LOAD bound, NOT the continuation guard (that's post-MAC, test 6)
    before = active_count(a)
    a.twilio_http(envelope(form_body(num, "+18880000000", "SMover")))
    assert active_count(a) == before, "per-tenant cap did not drop overflow"
    print(f"OK test_active_not_evicted_and_tenant_cap: {per} live none evicted; over-cap dropped")


# ── Test 2: a MAC-processed webhook cannot be replayed, even after a flood ────
def test_replay_rejected_after_flood():
    a = new_actor()
    proxy = b"\xAB" * 20
    num = "+15550000001"
    connect(a, num, proxy)

    # 1) a genuine webhook: relay → authenticated tenant_verified (MAC "passed")
    body = form_body(num, "+15551230000", "SMvalid", "hello")
    a.twilio_http(envelope(body))
    req = a._request_by_id(1)
    digest = req["digest"]
    forged_digest = None
    # proxy calls back (authenticated: sender == tenant.proxy) with the digest
    _STATE["sender"] = proxy
    r = a.tenant_verified(T.codec.encode(
        {"request_id": 1, "twilio_number": num, "digest": digest}))
    assert T.codec.decode(r).get("r") == "ok: verified"
    assert a._is_processed(digest)
    # finish it (as tenant_answer would)
    a._finish_request(1, req["conversation_key"], num, "answered")

    # 2) attacker floods forged INVALID webhooks (unique sids/digests). These
    #    never MAC-pass, so they never enter the replay shards and can't evict
    #    the genuine digest. (Each forged webhook that reaches `verify_and_ack`
    #    is finished via a non-proxy tenant_rejected so the inflight slot frees.)
    _STATE["sender"] = b"\xEE" * 20
    for i in range(300):
        a.twilio_http(envelope(form_body(num, f"+16{i:09d}", f"SMf{i:05d}", f"x{i}")))
        rid = a.storage[T.KEY_LAST_REQUEST_ID]
        entry = a._request_by_id(rid)
        if entry and entry["message_sid"] == f"SMf{i:05d}":
            if forged_digest is None:
                forged_digest = entry["digest"]
            _STATE["sender"] = proxy
            a.tenant_rejected(T.codec.encode({"request_id": rid, "twilio_number": num}))
            _STATE["sender"] = b"\xEE" * 20
    # the genuine digest survives; no forged digest entered the replay guard
    assert a._is_processed(digest), "genuine digest evicted from the replay guard"
    assert forged_digest is not None and not a._is_processed(forged_digest), \
        "forged webhook polluted the replay guard"

    # 3) replay the ORIGINAL valid webhook (same body+sig → same digest)
    last_before = a.storage[T.KEY_LAST_REQUEST_ID]
    a.twilio_http(envelope(body))
    if a.storage[T.KEY_LAST_REQUEST_ID] == last_before:
        pass  # cheap pre-filter dropped the replay (digest already processed) ✓
    else:
        # it reached active — the AUTHORITATIVE post-MAC check must reject it
        replay_rid = a.storage[T.KEY_LAST_REQUEST_ID]
        _STATE["sender"] = proxy
        r = a.tenant_verified(T.codec.encode(
            {"request_id": replay_rid, "twilio_number": num, "digest": digest}))
        assert T.codec.decode(r).get("r") == "err: replay", f"replay not rejected: {T.codec.decode(r)}"
    print("OK test_replay_rejected_after_flood: valid webhook not replayable after 300 forged")


# ── Test 3: no lifetime dispatch kill-switch remains ──────────────────────────
def test_no_lifetime_dispatch_cap():
    src = TWILIO_PATH.read_text()
    assert "MAX_DISPATCH_COUNT" not in src and "KEY_DISPATCH_COUNT" not in src, \
        "lifetime dispatch kill-switch still present"
    print("OK test_no_lifetime_dispatch_cap: no never-reset global dispatch cap")


# ── Test 4: continuation-resume exports are gated on sender == self ───────────
def test_resume_denies_non_self():
    reached = {"n": 0}
    T.TwilioActor.tenant_answer__resume = lambda self, p: reached.__setitem__("n", reached["n"] + 1)

    # external EOA / other actor forging a resume → denied, real resume NOT run
    _STATE["self"] = b"\x11" * 20
    _STATE["sender"] = b"\xAA" * 20  # attacker
    r = T.tenant_answer__resume(T.codec.encode({"request_id": 1}))
    assert T.codec.decode(r).get("r") == "denied", "non-self resume was NOT denied"
    assert reached["n"] == 0, "forged resume reached the continuation body"

    # genuine callback (from == actor self) → allowed
    _STATE["sender"] = b"\x11" * 20  # == self
    T.tenant_answer__resume(T.codec.encode({"request_id": 1}))
    assert reached["n"] == 1, "self resume was blocked"
    print("OK test_resume_denies_non_self: forged __resume denied; self-callback allowed")


# ── Test 5: Twilio preimage canonicalization matches upstream (dedupe equal) ──
def test_preimage_canonicalization():
    url = "https://gw.example/sms"
    cases = {
        # repeated-IDENTICAL values collapse (upstream sorted(set(values)))
        "Foo=a&Foo=a": "Fooa",
        # distinct duplicates keep both, value-sorted
        "Foo=b&Foo=a": "FooaFoob",
        # ordinary single-valued params, key-sorted, decoded
        "To=%2B1&Body=hi+there&From=%2B2": "Bodyhi thereFrom+2To+1",
    }
    for body, tail in cases.items():
        params = T._parse_form(body.encode("utf-8"))
        got = T._twilio_preimage(url, params).decode("utf-8")
        assert got == url + tail, f"{body!r}: got {got!r}, want {url + tail!r}"
    print("OK test_preimage_canonicalization: dup-identical collapses; matches upstream")


def test_stale_conversation_is_reclaimed_but_fresh_one_is_serialized():
    a = new_actor()
    proxy = b"\xAB" * 20
    num = "+15550000042"
    frm = "+15551112222"
    connect(a, num, proxy)
    conversation_key = T._conversation_key(num, frm)

    def seed(rid, created_height):
        a._put_request(rid, {
            "request_id": rid, "twilio_number": num,
            "conversation_key": conversation_key, "from_number": frm,
            "message_sid": f"SM{rid}", "text": "old", "digest": f"d{rid}",
            "status": "relayed", "created_height": created_height,
        })
        a._ta_add(num, rid)
        a._pending_set(conversation_key, rid)
        a.storage[T.KEY_LAST_REQUEST_ID] = rid
        a._inflight_add(num, +1)
        a._active_bytes_add(3)

    _STATE["height"] = 1000
    seed(1, 1000 - T.REQUEST_TIMEOUT_BLOCKS + 1)
    before = len(_CALLS)
    a.twilio_http(envelope(form_body(num, frm, "SMfresh")))
    assert len(_CALLS) == before, "fresh single-flight request was not serialized"
    assert a._pending_get(conversation_key) == 1

    _STATE["height"] = 1000 + T.REQUEST_TIMEOUT_BLOCKS
    a.twilio_http(envelope(form_body(num, frm, "SMretry")))
    replacement = a._pending_get(conversation_key)
    assert replacement != 1, "stale conversation slot was not reclaimed"
    assert a._request_by_id(1) is None, "stale active request was not removed"
    assert a._request_by_id(replacement)["created_height"] == _STATE["height"]
    assert a._inflight(num) == 1, "stale accounting leaked an in-flight slot"
    print("OK test_stale_conversation_is_reclaimed_but_fresh_one_is_serialized")


# ── Test 6: continuation capacity is RESERVED synchronously in tenant_verified ─
def _seed_relayed(a, num, rid, digest):
    """A relayed (pre-MAC) request, as twilio_http would create it."""
    a._put_request(rid, {
        "request_id": rid, "twilio_number": num, "conversation_key": f"c{rid}",
        "from_number": "+19990000000", "message_sid": f"SM{rid}", "text": "x",
        "digest": digest, "status": "relayed",
    })
    a._ta_add(num, rid)
    a._pending_set(f"c{rid}", rid)
    a._inflight_add(num, +1)


def test_reservation_cap_under_pvm_limit():
    a = new_actor()
    num = "+15550000000"
    proxy = b"\xAB" * 20
    connect(a, num, proxy)
    cap = T.MAX_GLOBAL_RUNNING
    assert cap < 100, "reservation cap must stay under the PVM 100-continuation limit"

    # seed cap+5 relayed requests, then verify each (authenticated as the proxy).
    # tenant_verified reserves the continuation slot SYNCHRONOUSLY and returns
    # ok:verified only while under cap; over-cap returns a non-accepted verdict.
    for i in range(1, cap + 6):
        _seed_relayed(a, num, i, f"d{i:05d}")
    _STATE["sender"] = proxy
    verified = overcap = 0
    for i in range(1, cap + 6):
        r = T.codec.decode(a.tenant_verified(T.codec.encode(
            {"request_id": i, "twilio_number": num, "digest": f"d{i:05d}"})))["r"]
        if r == "ok: verified":
            verified += 1
        elif r == "err: over capacity":
            overcap += 1
            # over-cap digests are NOT marked processed → a Twilio retry can
            # still succeed once capacity frees (not silently dropped forever)
            assert not a._is_processed(f"d{i:05d}"), "over-cap digest wrongly consumed"
    assert verified == cap, f"expected {cap} reserved, got {verified}"
    assert overcap == 5, f"expected 5 over-capacity, got {overcap}"
    assert a.storage[T.KEY_RUNNING] == cap, f"running={a.storage[T.KEY_RUNNING]}, want {cap}"
    assert a.storage[T.KEY_RUNNING] < 100

    # slow legitimate marker: finishing ONE reserved request frees exactly one
    # slot (rolling), and a previously-over-cap request now verifies on retry.
    a._finish_request(1, "c1", num, "answered")
    assert a.storage[T.KEY_RUNNING] == cap - 1, "finish did not release a reserved slot"
    # Twilio's retry arrives as a FRESH relayed request (new id, same content).
    # Because the over-cap digest was never consumed, it isn't replay-dropped, and
    # the freed slot now admits it.
    retry_id = cap + 100
    overcap_digest = f"d{cap + 1:05d}"           # request cap+1 was over-cap above
    _seed_relayed(a, num, retry_id, overcap_digest)
    r = T.codec.decode(a.tenant_verified(T.codec.encode(
        {"request_id": retry_id, "twilio_number": num, "digest": overcap_digest})))["r"]
    assert r == "ok: verified", f"freed slot not reusable on retry: {r}"
    assert a.storage[T.KEY_RUNNING] == cap
    print(f"OK test_reservation_cap_under_pvm_limit: {cap} reserved, 5 over-cap retriable, rolling release")


# ── Test 7: replay shards stay bounded (no KV value approaches the 64 KiB cap) ─
def test_replay_shards_stay_bounded():
    import hashlib as _hl
    a = new_actor()
    # mark far more genuine digests than any single value could hold as one blob
    # (a flat list of 40k 64-hex digests would be ~2.6 MiB — 40x over the cap)
    for i in range(40000):
        a._mark_processed(_hl.sha256(f"m{i}".encode()).hexdigest())
    max_len = 0
    for hi in "0123456789abcdef":                       # 16 shards (first hex char)
        shard = a.storage.get(T.KEY_PROCESSED_PREFIX + hi, [])
        assert len(shard) <= T.MAX_PROCESSED_PER_SHARD, \
            f"shard {hi} has {len(shard)} > {T.MAX_PROCESSED_PER_SHARD}"
        assert all(isinstance(d, (bytes, bytearray)) and len(d) == 32 for d in shard), \
            "replay shard must hold RAW 32-byte digests (compact storage)"
        max_len = max(max_len, len(shard))
    # worst-case CBOR value for a full shard: 128 * (32 raw + ~2) << 64 KiB
    worst_bytes = T.MAX_PROCESSED_PER_SHARD * (32 + 2)
    assert worst_bytes < 64 * 1024, f"worst shard ~{worst_bytes}B approaches 64KiB"
    print(f"OK test_replay_shards_stay_bounded: 40k digests, max shard {max_len} entries (~{max_len*34}B < 64KiB)")


# ── Test 8: worst-case AGGREGATE storage fits the per-actor byte quota ─────────
def _cbor_head(n):
    """Byte length of a canonical CBOR head for a major-type argument n."""
    if n < 24:
        return 1
    if n < 256:
        return 2
    if n < 65536:
        return 3
    if n < 2 ** 32:
        return 5
    return 9


def _cbor_len(obj):
    """Exact canonical-CBOR encoded length of obj (ints / bytes / str / list /
    dict). Matches the framing the Cowboy codec uses, so this measures REAL
    stored value bytes for a constructed worst-case record — not a guess."""
    if isinstance(obj, bool):
        return 1
    if isinstance(obj, int):
        return _cbor_head(obj if obj >= 0 else -obj - 1)
    if isinstance(obj, (bytes, bytearray)):
        return _cbor_head(len(obj)) + len(obj)
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return _cbor_head(len(b)) + len(b)
    if isinstance(obj, (list, tuple)):
        return _cbor_head(len(obj)) + sum(_cbor_len(x) for x in obj)
    if isinstance(obj, dict):
        return _cbor_head(len(obj)) + sum(_cbor_len(k) + _cbor_len(v) for k, v in obj.items())
    raise TypeError(type(obj))


def _entry_bytes(key: str, value) -> int:
    """kv_bytes charge for one stored entry = len(user key) + len(cbor value)."""
    return len(key.encode("utf-8")) + _cbor_len(value)


def test_aggregate_storage_within_quota():
    """Codex #41 (per-ACTOR total): the sum of every collection's worst case must
    fit RENT_QUOTA_BASE (1 MiB default un-bonded). Sizes are the ACTUAL canonical
    CBOR byte length of constructed max-field records + their user-key lengths
    (kv_bytes = Σ key+value, accounts.rs), plus headroom for SDK continuation
    state — not guessed constants."""
    RENT_QUOTA_BASE = 1024 * 1024
    max_active = T.MAX_TENANTS * T.MAX_TENANT_INFLIGHT

    # worst-case active record with EMPTY text (text is budgeted separately, since
    # the global byte budget caps the SUM of live text, not per-record)
    big_id = 10 ** 12
    rec_meta = {
        "request_id": big_id, "twilio_number": "9" * T.MAX_NUMBER_BYTES,
        "conversation_key": "f" * 32, "from_number": "9" * T.MAX_NUMBER_BYTES,
        "message_sid": "S" * 34, "text": "", "digest": "f" * 64, "status": "over_capacity",
    }
    active_meta = max_active * _entry_bytes(T.KEY_ACTIVE_PREFIX + str(big_id), rec_meta)
    active_text = T.ACTIVE_TEXT_BUDGET_BYTES              # hard byte budget (a:<id> text)
    pending = max_active * _entry_bytes(T.KEY_PENDING_PREFIX + "f" * 32, big_id)

    tenant_rec = {
        "hire_id": big_id, "artifact_id": big_id, "proxy": "a" * 40,
        "state_volume": "v" * T.MAX_CONFIG_STR_BYTES, "hirer": "e" * 40, "status": "active",
    }
    tenants = T.MAX_TENANTS * _entry_bytes(T.KEY_TENANT_PREFIX + "9" * T.MAX_NUMBER_BYTES, tenant_rec)
    tenant_index = _entry_bytes(T.KEY_TENANT_INDEX, ["9" * T.MAX_NUMBER_BYTES] * T.MAX_TENANTS)
    ta = T.MAX_TENANTS * _entry_bytes(
        T.KEY_TENANT_ACTIVE_PREFIX + "9" * T.MAX_NUMBER_BYTES, [big_id] * T.MAX_TENANT_INFLIGHT)

    replay = 16 * _entry_bytes(T.KEY_PROCESSED_PREFIX + "f", [b"\xff" * 32] * T.MAX_PROCESSED_PER_SHARD)
    inflight = _entry_bytes(T.KEY_INFLIGHT, {("9" * T.MAX_NUMBER_BYTES): 8 for _ in range(T.MAX_TENANTS)})
    misc = 2 * 1024

    kv_total = (active_meta + active_text + pending + tenants + tenant_index
                + ta + replay + inflight + misc)

    # SDK continuation state: <= MAX_GLOBAL_RUNNING captured frames, each holding a
    # truncated text copy (<= MAX_TEXT_BYTES, since a:<id> is truncated) + fields.
    continuation = T.MAX_GLOBAL_RUNNING * (T.MAX_TEXT_BYTES + 512)
    grand_total = kv_total + continuation

    assert grand_total < RENT_QUOTA_BASE, (
        f"worst-case storage {grand_total} >= 1 MiB quota (kv={kv_total}, cont={continuation})")
    # also stay under the 10k per-actor key-count cap
    key_count = max_active * 2 + T.MAX_TENANTS * 2 + 16 + 8  # a:+p:, t:+ta:, shards, singletons
    assert key_count < 10000, f"worst-case key count {key_count} >= 10k"
    print(f"OK test_aggregate_storage_within_quota: worst-case {grand_total//1024} KiB "
          f"(kv {kv_total//1024} + cont {continuation//1024}), {key_count} keys < 10k")


# ── Test 9: stale/double finish is idempotent (no over-release) — Codex #49 ────
def test_stale_finish_is_idempotent():
    a = new_actor()
    num = "+15550000009"
    proxy = b"\xCD" * 20
    connect(a, num, proxy)
    # two live requests for the same tenant
    a.twilio_http(envelope(form_body(num, "+16660000000", "SMa")))
    a.twilio_http(envelope(form_body(num, "+16660000001", "SMb")))
    assert a._inflight(num) == 2
    r1 = a.storage[T.KEY_LAST_REQUEST_ID] - 1
    ck1 = a._request_by_id(r1)["conversation_key"]
    # finish request 1 once — inflight drops to 1
    a._finish_request(r1, ck1, num, "answered")
    assert a._inflight(num) == 1, "first finish did not release exactly one slot"
    # a STALE second finish for the same (already-removed) request must be a no-op,
    # NOT steal request 2's inflight slot
    a._finish_request(r1, ck1, num, "answered")
    assert a._inflight(num) == 1, "stale double-finish wrongly released another slot"
    assert a._active_bytes() >= 0
    print("OK test_stale_finish_is_idempotent: double finish releases exactly one slot")


# ── Test 10: agent paths use the canonical relative volume alias form ─────
def test_agent_paths_are_safe_relative_volume_aliases():
    import asyncio

    a = new_actor()
    num = "+15550000010"
    proxy = b"\xAB" * 20
    artifact_volume = "twilio-brain"
    state_volume = "twilio-state"
    a.storage[T.KEY_ARTIFACT_VOLUME] = artifact_volume
    a._put_tenant(num, {
        "hire_id": 1, "artifact_id": 0, "proxy": proxy.hex(),
        "state_volume": state_volume, "hirer": "ee" * 20, "status": "active",
    })
    a._put_request(1, {
        "request_id": 1, "twilio_number": num, "conversation_key": "conversation-1",
        "from_number": "+15551112222", "message_sid": "SMrelative", "text": "hello",
        "digest": "d" * 64, "status": "running",
    })
    a._ta_add(num, 1)
    a._pending_set("conversation-1", 1)
    a._inflight_add(num, +1)
    a._running_add(+1)

    captured = {}

    async def capture_agent(*args, **kwargs):
        captured.update(kwargs)
        return _RunnerResult({"answer": "hi"})

    original_agent = T.runner.agent
    T.runner.agent = capture_agent
    try:
        asyncio.run(a.tenant_answer({"request_id": 1, "twilio_number": num}))
    finally:
        T.runner.agent = original_agent

    assert captured["system_prompt_path"] == f"{artifact_volume}/twilio/system_prompt.txt"
    assert captured["session_dir"] == f"{state_volume}/.sessions"
    assert not captured["system_prompt_path"].startswith("/")
    assert not captured["session_dir"].startswith("/")
    print("OK test_agent_paths_are_safe_relative_volume_aliases: canonical relative aliases")


def test_proxy_uses_configured_outbound_sender():
    fields = P._outbound_form_fields(
        "+14046159881", "+18782158947", "hello", "+19292274824")
    assert fields == [
        ("To", "+14046159881"),
        ("From", "+19292274824"),
        ("Body", "hello"),
    ]
    assert P._form_encode(fields) == (
        b"To=%2B14046159881&From=%2B19292274824&Body=hello")
    print("OK test_proxy_uses_configured_outbound_sender: exact registered sender override")


def test_twilio_ack_declares_xml_content_type():
    a = new_actor()
    response = T.codec.decode(a.twilio_http(envelope(form_body(
        "+15550009999", "+15551112222", "SMxml"))))
    assert response["status"] == 200
    assert ("content-type", "application/xml; charset=utf-8") in response["headers"]
    assert response["body"].endswith(b"<Response></Response>")
    print("OK test_twilio_ack_declares_xml_content_type: Twilio-compatible TwiML response")


# ── Test 11: each runner op is launched via a FUNDED self-timer (D36 fix) ─────
def test_funded_self_timers_replace_self_send_hops():
    """The launch of every runner op is a FUNDED self-timer at
    access_limit=TIMER_ACCESS_LIMIT (322), NOT a self-send. A self-send re-enters
    the actor as a deferred child capped at 162 accesses, which cannot fund the
    nested submit_job (D36 / E1005). The timer fires on the SCHEDULING actor, so
    attribution (from == this actor) is preserved — the reason the hop exists at
    all — while 322 = 160 handler + 162 reserves one full deferred child for the
    runner job. Covers all three sites: twilio.tenant_verified (run_agent),
    proxy.relay_event (run_verify), proxy.post_answer (run_send)."""
    assert T.TIMER_ACCESS_LIMIT == 322 and P.TIMER_ACCESS_LIMIT == 322

    # twilio.tenant_verified → run_agent timer on twilio
    a = new_actor()
    num = "+15550007777"
    proxy = b"\xAB" * 20
    connect(a, num, proxy)
    _seed_relayed(a, num, 1, "d" * 64)
    _STATE["sender"] = proxy
    _STATE["height"] = 500
    _TIMERS.clear()
    r = T.codec.decode(a.tenant_verified(T.codec.encode(
        {"request_id": 1, "twilio_number": num, "digest": "d" * 64})))
    assert r["r"] == "ok: verified", r
    assert len(_TIMERS) == 1, _TIMERS
    height, payload, access_limit = _TIMERS[0]
    assert access_limit == 322, access_limit
    assert height == 501, height           # scheduled next block
    assert T.codec.decode(payload) == {"kind": "run_agent", "request_id": 1}
    assert a._request_by_id(1)["status"] == "verified"   # single-use flip

    # proxy.relay_event → run_verify timer, proxy.post_answer → run_send timer
    p = P.TwilioRelayActor()
    p.storage = MockStorage()
    _STATE["sender"] = b"\xEE" * 20                       # deployer/owner for init
    p.init(json.dumps({"twilio": "cd" * 20, "account_sid": "ACx"}))
    _STATE["sender"] = bytes.fromhex("cd" * 20)           # authenticated == configured twilio

    _TIMERS.clear()
    p.relay_event(P.codec.encode(
        {"request_id": 7, "twilio_number": num, "preimage": b"pre", "sig": "sig"}))
    assert len(_TIMERS) == 1 and _TIMERS[0][2] == 322, _TIMERS
    assert P.codec.decode(_TIMERS[0][1]) == {"kind": "run_verify", "request_id": 7}

    p._verified_add(7)                                    # post_answer consumes this
    _TIMERS.clear()
    p.post_answer(P.codec.encode(
        {"request_id": 7, "to_number": "+15551230000", "from_number": num, "text": "hi"}))
    assert len(_TIMERS) == 1 and _TIMERS[0][2] == 322, _TIMERS
    assert P.codec.decode(_TIMERS[0][1]) == {"kind": "run_send", "request_id": 7}
    print("OK test_funded_self_timers_replace_self_send_hops: run_agent/run_verify/run_send via 322-access timers")


if __name__ == "__main__":
    test_active_not_evicted_and_tenant_cap()
    test_replay_rejected_after_flood()
    test_no_lifetime_dispatch_cap()
    test_resume_denies_non_self()
    test_preimage_canonicalization()
    test_stale_conversation_is_reclaimed_but_fresh_one_is_serialized()
    test_reservation_cap_under_pvm_limit()
    test_replay_shards_stay_bounded()
    test_aggregate_storage_within_quota()
    test_stale_finish_is_idempotent()
    test_agent_paths_are_safe_relative_volume_aliases()
    test_proxy_uses_configured_outbound_sender()
    test_twilio_ack_declares_xml_content_type()
    test_funded_self_timers_replace_self_send_hops()
    print("\nALL DoS-hardening tests passed.")
