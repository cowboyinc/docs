"""Event payloads and storage values must be objects, not hand-serialized text.

Two encodings do the work for actor code and neither one wants help:

  * ``runtime.emit_event(name, payload)`` canonical-CBOR-encodes anything that is
    not bytes-like. Handing it ``json.dumps(obj)`` nests JSON text inside a CBOR
    text string, so a consumer decodes a string where it expects the object.
    The node SDK does not merely tolerate this — it rejects it: emit_event raises
    ``CodecError`` for a str that parses as a JSON object or array.
  * ``self.storage[key] = value`` CBOR-encodes on write and decodes on read, so
    an int stored is an int read. ``str()`` on the way in plus ``int()`` on the
    way out is a round-trip through a format nobody asked for, and it makes the
    decoded state view (``cowboy actor state``) print ``"0"`` where the reader
    expects ``0``.

Going native has one edge the string form did not have: the encoder emits no bignum
tags, so it fails at ``2**64``, while CIP-20 quantities are ``u128``. An uncaught
failure is safe — the tx frame is discarded, token writes included — but it is
opaque and already paid for, so v2_pool preflights the ceiling and returns a named
error. The last group of tests covers that, and pins which exception each encode
path raises (``OverflowError`` for a storage write, ``CodecError`` for an emit).

The lint below walks every example actor's AST, so a new example cannot reintroduce
either shape. The behavioural tests drive the real v2_pool through a cowboy_sdk stub
that carries the node's JSON-string rejection and its integer ceiling, so a write or
an emit that would fail on chain fails here too.

Run: python3 examples/test_emit_and_storage_encoding.py (or under pytest)
"""

import ast
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types

import cbor2

HERE = pathlib.Path(__file__).resolve().parent


# ─── the lint ─────────────────────────────────────────────────────────────────

def _actor_sources():
    """Every example .py, minus this harness's siblings and generated trees."""
    for path in sorted(HERE.rglob("*.py")):
        parts = path.relative_to(HERE).parts
        if "__pycache__" in parts or path.name.startswith("test_"):
            continue
        yield path


def _emit_payloads(tree):
    """(lineno, payload_node) for every ``*.emit_event(name, payload)`` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "emit_event" and len(node.args) >= 2:
            yield node.lineno, node.args[1]


def _is_json_dumps(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
    )


def test_no_example_double_encodes_an_event_payload() -> None:
    offenders = []
    for path in _actor_sources():
        tree = ast.parse(path.read_text())
        for lineno, payload in _emit_payloads(tree):
            if _is_json_dumps(payload):
                offenders.append(f"{path.relative_to(HERE)}:{lineno}: emit_event(json.dumps(...))")
            # `.encode()` on a json.dumps result slips past the str guard as
            # bytes, so it reaches the chain as JSON — same double-encode.
            if (
                isinstance(payload, ast.Call)
                and isinstance(payload.func, ast.Attribute)
                and payload.func.attr == "encode"
                and _is_json_dumps(payload.func.value)
            ):
                offenders.append(
                    f"{path.relative_to(HERE)}:{lineno}: emit_event(json.dumps(...).encode())"
                )
    assert offenders == [], offenders
    print("ok test_no_example_double_encodes_an_event_payload")


# A key expression that is not a literal — a loop variable, a computed key. Kept
# distinct from "not a storage access" so the table-driven form below can still be
# matched by name.
_DYNAMIC = object()


def _storage_key(node):
    """The key of a ``self.storage[...]`` subscript: the str for a literal key,
    ``_DYNAMIC`` for an expression, None when this is not a storage subscript."""
    if not isinstance(node, ast.Subscript):
        return None
    value = node.value
    if not (isinstance(value, ast.Attribute) and value.attr == "storage"):
        return None
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return _DYNAMIC


def _key_label(key):
    return "<computed>" if key is _DYNAMIC else key


def _storage_get_default(node):
    """(key, default_node) for ``self.storage.get(key, default)``, else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr != "get" or len(node.args) != 2:
        return None
    owner = node.func.value
    if not (isinstance(owner, ast.Attribute) and owner.attr == "storage"):
        return None
    key = node.args[0]
    literal = isinstance(key, ast.Constant) and isinstance(key.value, str)
    return (key.value if literal else _DYNAMIC), node.args[1]


def _is_stringified_scalar(node):
    """A str literal that is really an int or a bool wearing quotes."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    return node.value.lstrip("-").isdigit() or node.value in ("True", "False")


def _is_hand_serialized(node):
    """``json.dumps(x)``, bare or ``.encode()``-wrapped.

    Unlike ``str(x)`` — which is usually a legitimate coercion of an untrusted
    payload field — there is no reason to hand-serialize into a store that already
    CBOR-encodes. It also breaks the reader: a JSON *list* pushed through a
    dict-only decoder comes back empty.
    """
    if _is_json_dumps(node):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "encode"
        and _is_json_dumps(node.func.value)
    )


def _bad_storage_value(node):
    """The label for a value that must not be written to storage, else None."""
    if _is_stringified_scalar(node):
        return repr(node.value)
    if _is_hand_serialized(node):
        return "json.dumps(...)"
    return None


def _table_driven_writes(node):
    """(lineno, key, literal) for the table form price_watcher uses:

        for key, val in (("initialized", "1"), ("watches", {}), ...):
            self.storage[key] = val

    A plain `self.storage["k"] = "1"` walk never sees these — the written value is
    a loop variable and the key never appears as a subscript literal. Requiring
    the body to actually store `val` under `key` keeps unrelated 2-tuple loops out.
    """
    target = node.target
    if not (isinstance(target, ast.Tuple) and len(target.elts) == 2
            and all(isinstance(elt, ast.Name) for elt in target.elts)):
        return
    key_name, val_name = (elt.id for elt in target.elts)
    if not isinstance(node.iter, (ast.Tuple, ast.List)):
        return

    stores_the_pair = any(
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and _storage_key(stmt.targets[0]) is _DYNAMIC
        and isinstance(stmt.targets[0].slice, ast.Name)
        and stmt.targets[0].slice.id == key_name
        and isinstance(stmt.value, ast.Name)
        and stmt.value.id == val_name
        for stmt in ast.walk(node)
    )
    if not stores_the_pair:
        return

    for entry in node.iter.elts:
        if not (isinstance(entry, (ast.Tuple, ast.List)) and len(entry.elts) == 2):
            continue
        key, written = entry.elts
        bad = _bad_storage_value(written)
        if bad is None:
            continue
        label = key.value if isinstance(key, ast.Constant) else "<computed>"
        yield entry.lineno, label, bad


def test_no_example_reads_storage_through_a_stringified_default() -> None:
    """A numeric-string default (``.get("reserve_a", "0")``) is the read half of a
    str()/int() round-trip: it only makes sense if the write stringified too.

    The read side is what gets checked because the write side is not decidable —
    ``storage[k] = str(x)`` is indistinguishable from the legitimate coercion of an
    untrusted payload field to a string, which examples do in ~30 places.
    """
    offenders = []
    for path in _actor_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            found = _storage_get_default(node)
            if found is None:
                continue
            key, default = found
            if _is_stringified_scalar(default):
                offenders.append(
                    f"{path.relative_to(HERE)}:{node.lineno}: "
                    f'storage.get("{_key_label(key)}", {default.value!r})'
                )
    assert offenders == [], offenders
    print("ok test_no_example_reads_storage_through_a_stringified_default")


def test_no_example_hand_serializes_a_storage_value() -> None:
    """Two shapes that both defeat the CBOR round-trip.

    ``storage["initialized"] = "1"`` is a bool wearing a string; a truthiness read
    handles it either way, so it stays correct against state an older build wrote.
    ``storage[k] = json.dumps(x)`` is worse — it needs a matching hand-decode on
    read, and if the decoder is narrower than the value (a dict-only loader given a
    list) the data comes back empty with nothing raised.
    """
    offenders = []
    for path in _actor_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                for lineno, key, written in _table_driven_writes(node):
                    offenders.append(
                        f"{path.relative_to(HERE)}:{lineno}: ({key!r}, {written}) -> storage"
                    )
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            key = _storage_key(node.targets[0])
            bad = _bad_storage_value(node.value) if key is not None else None
            if bad is None:
                continue
            offenders.append(
                f"{path.relative_to(HERE)}:{node.lineno}: "
                f'storage["{_key_label(key)}"] = {bad}'
            )
    assert offenders == [], offenders
    print("ok test_no_example_hand_serializes_a_storage_value")


# ─── cowboy_sdk stub: real CBOR + the node's emit_event contract ──────────────

EVENTS = []      # [(name, cbor_bytes)]
TOKENS = {}      # token_id_hex -> total supply
TOKEN_OPS = []   # [(kind, token_id_hex, amount)] — every balance-moving host call


class CodecError(Exception):
    """Mirrors cowboy_sdk.errors.CodecError."""


# Mirrors the host's MAX_EVENT_PAYLOAD_BYTES, which node reuses to bound the sniff.
MAX_EVENT_PAYLOAD_BYTES = 4096

# cbor2 has no 2**64 ceiling — it writes bignum tag 2 — so encoding through it
# directly would let a value that fails on chain pass here. Which exception the SDK
# raises depends on the path: `self.storage[...]` calls codec.encode and surfaces
# the raw OverflowError; emit_event and handler returns go through
# payload_codec._encode_value, which reclassifies it to CodecError.
# test_node_codec_agrees_on_the_u64_boundary pins both paths against the real SDK.
MAX_ENCODABLE_UINT = 2**64 - 1


def _check_encodable(value):
    """Mirrors the node encoder's integer capacity, recursively."""
    if isinstance(value, bool):
        return
    if isinstance(value, int) and not -(2**64) <= value <= MAX_ENCODABLE_UINT:
        raise OverflowError("int too big to convert")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_encodable(key)
            _check_encodable(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_encodable(item)


def _encode(value):
    """Mirrors cowboy_sdk.codec.encode — the storage path, OverflowError and all."""
    _check_encodable(value)
    return cbor2.dumps(value, canonical=True)


def _encode_payload(value):
    """Mirrors payload_codec._encode_value: codec.encode, capacity leaks reclassified."""
    try:
        return _encode(value)
    except (OverflowError, RecursionError, UnicodeEncodeError) as exc:
        raise CodecError(f"cbor encode failed: {exc}") from exc


def _reject_json_string(payload):
    """Mirrors node pvm/Lib/cowboy_sdk/runtime.py::_reject_json_string.

    Only object/array-shaped JSON is rejected; a bare "hello" — or "42", "true",
    which are also valid JSON — stays a legal text payload. Past the sniff bound
    the check fails open, as node's does: a longer str already exceeds the host's
    event cap, so the emit fails there instead.
    """
    if len(payload) > MAX_EVENT_PAYLOAD_BYTES:
        return
    stripped = payload.strip()
    if not stripped or (stripped[0], stripped[-1]) not in (("{", "}"), ("[", "]")):
        return
    try:
        json.loads(stripped)
    except Exception:
        return
    raise CodecError("emit_event payload is a JSON string, but event payloads are canonical CBOR")


def _emit_event(name, payload, *, schema=None):
    """Mirrors node pvm/Lib/cowboy_sdk/runtime.py::emit_event."""
    if schema is not None and (not isinstance(schema, (bytes, bytearray)) or len(schema) != 32):
        raise ValueError("event schema must be a 32-byte identifier or None")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        EVENTS.append((name, bytes(payload)))
    else:
        if isinstance(payload, str):
            _reject_json_string(payload)
        EVENTS.append((name, _encode_payload(payload)))
    return {"emit_id": len(EVENTS), "sync_outcomes": [], "deferred_subs": []}


class Storage:
    """Mirrors cowboy_sdk.actor._ActorStorage: CBOR on write, decode on read."""

    def __init__(self):
        self._raw = {}

    def __setitem__(self, key, value):
        self._raw[key.encode()] = _encode(value)

    def __getitem__(self, key):
        raw = self._raw.get(key.encode())
        return None if raw is None else cbor2.loads(raw)

    def get(self, key, default=None):
        value = self[key]
        return default if value is None else value

    def raw(self, key):
        return self._raw.get(key.encode())


def _install_fake_sdk():
    _ident = lambda f=None, *a, **k: (f if callable(f) else _ident)  # noqa: E731

    class _Address:
        def __init__(self, value):
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).hex()
            self._hex = str(value or "").lower().removeprefix("0x")

        def __eq__(self, other):
            return self._hex == getattr(other, "_hex", other)

    sdk = types.ModuleType("cowboy_sdk")
    sdk.actor = _ident
    sdk.pure = _ident
    sdk.public = lambda fn=None, *, decode="raw": (fn if fn is not None else _ident)
    sdk.callable_by = lambda *a, **k: _ident
    sdk.OWNER = "OWNER"
    sdk.Address = _Address
    sdk.capture = lambda: types.SimpleNamespace()

    codec_stub = types.SimpleNamespace(encode=_encode_payload, decode=cbor2.loads)
    sdk.codec = codec_stub

    runner_stub = types.ModuleType("cowboy_sdk.runner")
    runner_stub.continuation = _ident
    sdk.runner = runner_stub

    runtime = types.ModuleType("cowboy_sdk.runtime")
    runtime.emit_event = _emit_event
    runtime.assume_ownership_if_unowned = lambda: None
    runtime.get_actor_address = lambda: "11" * 20
    runtime.token_create = lambda *a: bytes.fromhex("ab" * 32)
    runtime.token_total_supply = lambda tid: TOKENS.get(tid, 0)

    # Recorded so a test can assert a rejected handler moved nothing, rather
    # than only that it returned an error.
    def _mint(tid, to, amount):
        TOKEN_OPS.append(("mint", tid, amount))
        TOKENS[tid] = TOKENS.get(tid, 0) + amount

    def _burn(tid, amount):
        TOKEN_OPS.append(("burn", tid, amount))
        TOKENS[tid] = TOKENS.get(tid, 0) - amount

    runtime.token_mint = _mint
    runtime.token_burn = _burn
    runtime.token_transfer = lambda tid, to, amount: TOKEN_OPS.append(
        ("transfer", tid, amount))
    runtime.token_transfer_from = lambda tid, frm, to, amount: TOKEN_OPS.append(
        ("transfer_from", tid, amount))
    sdk.runtime = runtime

    sys.modules["cowboy_sdk"] = sdk
    sys.modules["cowboy_sdk.runtime"] = runtime
    sys.modules["cowboy_sdk.runner"] = runner_stub
    sys.modules["cowboy_sdk.codec"] = codec_stub


def _load(relpath, name):
    _install_fake_sdk()
    spec = importlib.util.spec_from_file_location(name, HERE / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


POOL = _load("gallery/liquidity-pools/v2_pool.py", "ex_v2_pool")
SPAM = _load("gallery/spam-detection/spam_detector.py", "ex_spam_detector")

TOKEN_A = "aa" * 32
TOKEN_B = "bb" * 32
POOL_HEX = "cc" * 20


def _pool():
    EVENTS.clear()
    TOKENS.clear()
    TOKEN_OPS.clear()
    actor = POOL.V2Pool.__new__(POOL.V2Pool)
    actor.storage = Storage()
    actor.sender = "ee" * 20
    actor.init(b"")
    actor.init_pool({"token_a": TOKEN_A, "token_b": TOKEN_B,
                     "fee_bps": 30, "pool_address": POOL_HEX})
    return actor


def _event(name):
    return [cbor2.loads(data) for topic, data in EVENTS if topic == name]


# ─── behavioural tests ────────────────────────────────────────────────────────

def test_pool_events_decode_to_objects_not_to_json_text() -> None:
    """The seam: what a consumer gets back out of the receipt. A CBOR decode has
    to yield the dict itself — under the old json.dumps it yielded a str, and
    against the current node SDK it does not survive the emit at all."""
    actor = _pool()
    actor.add_liquidity({"amount_a": 10**19, "amount_b": 25_000_000_000})
    actor.swap({"token_in": TOKEN_B, "amount_in": 1_000_000_000})
    actor.get_reserves({})
    # Every emit site the pool has, so none of the five is left unexercised.
    actor.remove_liquidity({"lp_amount": TOKENS[actor.storage["lp_token"]] // 2})

    for topic in ("pool_initialized", "liquidity_added", "swap", "reserves",
                  "liquidity_removed"):
        payloads = _event(topic)
        assert len(payloads) == 1, (topic, payloads)
        decoded = payloads[0]
        assert isinstance(decoded, dict), (topic, type(decoded), decoded)
        for field, value in decoded.items():
            assert not isinstance(value, (bytes, bytearray)), (topic, field, value)

    swapped = _event("swap")[0]
    assert isinstance(swapped["amount_out"], int), swapped
    assert isinstance(swapped["reserve_a"], int), swapped
    assert _event("pool_initialized")[0]["fee_bps"] == 30

    removed = _event("liquidity_removed")[0]
    assert isinstance(removed["lp_burned"], int), removed
    assert removed["reserve_a"] == actor.storage["reserve_a"], removed
    print("ok test_pool_events_decode_to_objects_not_to_json_text")


def test_json_dumps_payload_would_be_rejected() -> None:
    """The guard the lint exists to keep satisfied: a json.dumps payload is a hard
    failure on chain, not a style preference."""
    EVENTS.clear()
    try:
        _emit_event("pool_initialized", json.dumps({"status": "initialized"}))
    except CodecError:
        pass
    else:
        raise AssertionError("a JSON-object string must not emit")
    assert EVENTS == []

    # A bare text payload is still legal, and is not what the pool emits.
    _emit_event("note", "hello")
    assert cbor2.loads(EVENTS[0][1]) == "hello"
    print("ok test_json_dumps_payload_would_be_rejected")


def test_pool_storage_holds_native_types() -> None:
    """No stringification: the bytes in the trie are a CBOR int / bool, which is
    what `cowboy actor state` decodes and what the demo reads."""
    actor = _pool()
    actor.add_liquidity({"amount_a": 10**19, "amount_b": 25_000_000_000})

    for key in ("reserve_a", "reserve_b", "fee_bps"):
        value = actor.storage[key]
        assert isinstance(value, int) and not isinstance(value, bool), (key, value)
    assert actor.storage["initialized"] is True
    assert actor.storage["reserve_a"] == 10**19
    assert actor.storage["fee_bps"] == 30

    # 30 as a CBOR int is two bytes; as the text string "30" it is three.
    assert actor.storage.raw("fee_bps") == _encode(30)
    assert actor.storage.raw("fee_bps") != _encode("30")
    print("ok test_pool_storage_holds_native_types")


def test_reserves_survive_a_full_round_trip() -> None:
    """Reserves are read back and re-arithmetic'd on every handler, so a decode
    that returned text would raise rather than silently drift."""
    actor = _pool()
    actor.add_liquidity({"amount_a": 10**19, "amount_b": 25_000_000_000})
    added = _event("liquidity_added")[0]

    swapped = actor.swap({"token_in": TOKEN_B, "amount_in": 1_000_000_000})
    assert swapped["reserve_a"] == added["reserve_a"] - swapped["amount_out"]
    assert swapped["reserve_b"] == added["reserve_b"] + swapped["amount_in"]
    assert actor.storage["reserve_a"] == swapped["reserve_a"]

    reserves = actor.get_reserves({})
    assert reserves["reserve_a"] == swapped["reserve_a"], reserves
    assert reserves["reserve_b"] == swapped["reserve_b"], reserves
    print("ok test_reserves_survive_a_full_round_trip")


def _pool_with_flag(flag):
    """A deployed-but-not-initialized pool whose `initialized` slot holds `flag`."""
    EVENTS.clear()
    TOKENS.clear()
    TOKEN_OPS.clear()
    actor = POOL.V2Pool.__new__(POOL.V2Pool)
    actor.storage = Storage()
    actor.sender = "ee" * 20
    actor.init(b"")
    if flag is _ABSENT:
        del actor.storage._raw[b"initialized"]
    else:
        actor.storage["initialized"] = flag
    return actor


_ABSENT = object()


def test_init_pool_gate_reads_every_stored_form_of_the_flag() -> None:
    """The gate has to say "initialized" for exactly two stored values.

    A pool deployed before the flag went native holds the strings "0" / "1", and an
    in-place upgrade brings that state along. "0" is a truthy Python string, so a
    plain truthiness read reports an untouched pool as initialized — and since only
    init_pool clears that, the pool can never be initialized again.
    """
    cases = [
        (False, False, "fresh deploy, native"),
        (True, True, "initialized, native"),
        ("0", False, "upgraded pool, init ran but init_pool did not"),
        ("1", True, "upgraded pool, init_pool had run"),
        (_ABSENT, False, "no flag at all"),
    ]
    for flag, expect_gated, label in cases:
        actor = _pool_with_flag(flag)
        result = actor.init_pool({"token_a": TOKEN_A, "token_b": TOKEN_B})
        gated = result == {"error": "already initialized"}
        assert gated is expect_gated, (label, flag, result)
        if not expect_gated:
            # And initializing actually took: the flag is native afterwards.
            assert actor.storage["initialized"] is True, (label, flag)

    # The ordinary second call is still refused.
    actor = _pool()
    assert actor.init_pool({"token_a": TOKEN_A, "token_b": TOKEN_B}) == {
        "error": "already initialized"}
    print("ok test_init_pool_gate_reads_every_stored_form_of_the_flag")


# ─── the u64 ceiling: CIP-20 is u128, the encoder is not ──────────────────────

U64_MAX = 2**64 - 1


def _with_real_node_sdk(fn):
    """Run ``fn(codec_module)`` against the node's own cowboy_sdk, or return None.

    ``node/pvm/Lib`` is a full stdlib shadow, so it cannot go on sys.path — link
    the one package out to a temp dir instead. The fake cowboy_sdk this module
    installed has to come off sys.modules first and go back on after, and ``fn``
    has to finish inside the window: codec resolves ``from .errors import ...``
    lazily, which needs the real package still registered.
    """
    node_dir = os.environ.get("COWBOY_NODE_DIR") or (HERE.parents[1] / "node")
    sdk_dir = pathlib.Path(node_dir) / "pvm" / "Lib" / "cowboy_sdk"
    if not sdk_dir.is_dir():
        return None

    saved = {k: v for k, v in sys.modules.items()
             if k == "cowboy_sdk" or k.startswith("cowboy_sdk.")}
    tmp = tempfile.mkdtemp()
    try:
        for name in saved:
            del sys.modules[name]
        (pathlib.Path(tmp) / "cowboy_sdk").symlink_to(sdk_dir)
        sys.path.insert(0, tmp)
        from cowboy_sdk import codec as real_codec
        return fn(real_codec)
    finally:
        if tmp in sys.path:
            sys.path.remove(tmp)
        for name in [k for k in sys.modules
                     if k == "cowboy_sdk" or k.startswith("cowboy_sdk.")]:
            del sys.modules[name]
        sys.modules.update(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def _outcome(encode, value):
    """The hex encoding, or the name of the exception type it raised."""
    try:
        return encode(value).hex()
    except Exception as exc:
        return type(exc).__name__


def test_node_codec_agrees_on_the_u64_boundary() -> None:
    """cbor2 writes bignum tag 2 for 2**64; the node encoder raises. Pin the stub
    to the real SDK — both entry points, because they raise different things:
    codec.encode (what a storage write calls) surfaces OverflowError, while
    payload_codec._encode_value (what emit_event calls) reclassifies it.

    Skipped with a printed note when there is no sibling node/ checkout — the
    stub's own ceiling is asserted either way.
    """
    assert _encode(U64_MAX).hex() == "1bffffffffffffffff"
    for over in (2**64, 10**21, {"reserve_a": 2**64}, [2**64]):
        assert _outcome(_encode, over) == "OverflowError", over
        assert _outcome(_encode_payload, over) == "CodecError", over
    # Raw cbor2 is exactly what must NOT be used for these.
    assert cbor2.dumps(2**64, canonical=True).hex().startswith("c2")

    def compare(real_codec):
        from cowboy_sdk.payload_codec import _encode_value
        rows = []
        for value in (0, 30, U64_MAX, 2**64, 10**21):
            rows.append((
                value,
                _outcome(real_codec.encode, value),
                _outcome(_encode, value),
                _outcome(lambda v: _encode_value(v, "cbor"), value),
                _outcome(_encode_payload, value),
            ))
        return rows

    rows = _with_real_node_sdk(compare)
    if rows is None:
        print("ok test_node_codec_agrees_on_the_u64_boundary (node/ absent, stub-only)")
        return
    for value, real_store, stub_store, real_emit, stub_emit in rows:
        assert real_store == stub_store, ("storage path", value, real_store, stub_store)
        assert real_emit == stub_emit, ("emit path", value, real_emit, stub_emit)
    # The divergence is the point: same value, same ceiling, different exception.
    over = [r for r in rows if r[0] == 2**64][0]
    assert (over[1], over[3]) == ("OverflowError", "CodecError"), over
    print("ok test_node_codec_agrees_on_the_u64_boundary (checked against node/)")


def test_over_ceiling_amounts_are_refused_with_a_named_error() -> None:
    """CIP-20 amounts are u128 and a genesis token already holds 10**21, so this is
    reachable. Without the preflight the handler runs the transfers and then dies on
    the storage write — safe (the tx frame is discarded, per node's
    case4_token_failure_rollback) but opaque and already paid for. The assertions on
    TOKEN_OPS pin that the check runs before that work, not that it prevents loss."""
    actor = _pool()
    baseline = dict(TOKENS)
    TOKEN_OPS.clear()
    EVENTS.clear()

    assert actor.add_liquidity({"amount_a": 2**64, "amount_b": 25_000_000_000}) == {
        "error": "amount exceeds the pool's u64 ceiling"}
    assert TOKEN_OPS == [], TOKEN_OPS
    assert EVENTS == [], EVENTS
    assert TOKENS == baseline, TOKENS
    assert actor.storage["reserve_a"] == 0 and actor.storage["reserve_b"] == 0

    # Same for a deposit that fits on its own but pushes the running reserve over.
    # amount_a has to clear reserve_a/total_supply, or the
    # "insufficient liquidity minted" check fires first and proves nothing.
    assert "error" not in actor.add_liquidity(
        {"amount_a": U64_MAX - 10, "amount_b": 25_000_000_000})
    TOKEN_OPS.clear()
    EVENTS.clear()
    assert actor.add_liquidity({"amount_a": 1_000_000, "amount_b": 1_000_000}) == {
        "error": "amount exceeds the pool's u64 ceiling"}
    assert TOKEN_OPS == [], TOKEN_OPS
    assert actor.storage["reserve_a"] == U64_MAX - 10

    # And on the swap path, which moves tokens before it writes storage.
    TOKEN_OPS.clear()
    assert actor.swap({"token_in": TOKEN_B, "amount_in": 2**64}) == {
        "error": "amount exceeds the pool's u64 ceiling"}
    assert TOKEN_OPS == [], TOKEN_OPS
    print("ok test_over_ceiling_amounts_are_refused_with_a_named_error")


def test_a_rejected_first_deposit_moves_nothing() -> None:
    """Guard-before-mutation on the non-ceiling rejection path.

    A first deposit too small to clear MINIMUM_LIQUIDITY returns
    "insufficient liquidity minted". That return commits — it is an ordinary
    value, not a raise — so whatever the handler did first sticks. Both
    transfers and the MINIMUM_LIQUIDITY mint therefore have to sit after it, or
    a rejected deposit leaves the tokens in the pool with no LP tokens back and
    reserves untouched.
    """
    actor = _pool()
    TOKEN_OPS.clear()

    assert actor.add_liquidity({"amount_a": 10, "amount_b": 10}) == {
        "error": "insufficient liquidity minted"}
    assert TOKEN_OPS == [], TOKEN_OPS
    assert actor.storage["reserve_a"] == 0 and actor.storage["reserve_b"] == 0
    print("ok test_a_rejected_first_deposit_moves_nothing")


def test_the_ceiling_value_itself_still_works() -> None:
    """An off-by-one here would reject the largest legitimate deposit."""
    actor = _pool()
    result = actor.add_liquidity({"amount_a": U64_MAX, "amount_b": 1_000_000})
    assert "error" not in result, result
    assert actor.storage["reserve_a"] == U64_MAX
    assert actor.storage.raw("reserve_a") == bytes.fromhex("1bffffffffffffffff")
    assert _event("liquidity_added")[0]["reserve_a"] == U64_MAX
    print("ok test_the_ceiling_value_itself_still_works")


def test_spam_detector_reasons_survive_the_read() -> None:
    """A list stored natively comes back as that list.

    The lint sees the shape but not the consequence: `reasons` was hand-serialized
    on write and pushed through a dict-only JSON loader on read, so a non-dict —
    which a list always is — decoded to {} and `or []` turned that into an empty
    list. Every reason the model produced was dropped, silently.
    """
    storage = Storage()
    storage["last_case_id"] = 7
    storage["last_case_reasons"] = ["bulk sender", "link shortener"]

    case = SPAM._get_last_case_dict(storage)
    assert case["reasons"] == ["bulk sender", "link shortener"], case
    assert storage.raw("last_case_reasons") == _encode(
        ["bulk sender", "link shortener"]), "reasons must be CBOR, not JSON text"

    # An empty list stays an empty list rather than becoming the idle default.
    storage["last_case_reasons"] = []
    assert SPAM._get_last_case_dict(storage)["reasons"] == []

    # State written by the pre-fix build lived under a different key, so it is
    # invisible here: the read falls back to [] rather than leaking JSON text into
    # a list-typed field.
    legacy = Storage()
    legacy["last_case_id"] = 7
    legacy["last_case_reasons_json"] = json.dumps(["bulk sender"])
    assert SPAM._get_last_case_dict(legacy)["reasons"] == []
    print("ok test_spam_detector_reasons_survive_the_read")


def test_a_u128_lp_supply_does_not_reach_the_getter_emit() -> None:
    """get_reserves reads the LP supply straight from the u128 host, so it is the
    one value the write guards cannot bound."""
    actor = _pool()
    actor.add_liquidity({"amount_a": 10**18, "amount_b": 25_000_000_000})
    TOKENS[actor.storage["lp_token"]] = 10**21
    EVENTS.clear()

    assert actor.get_reserves({}) == {
        "error": "lp_total_supply exceeds the pool's u64 ceiling"}
    assert EVENTS == [], EVENTS
    print("ok test_a_u128_lp_supply_does_not_reach_the_getter_emit")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\nall {len(tests)} emit/storage encoding tests passed")


if __name__ == "__main__":
    main()
