"""Checks for the examples that read another actor synchronously.

These sites moved from a raw ``runtime.call_actor`` plus a hand-rolled result
decoder to ``cowboy_sdk.call``. Both halves of that swap are wire-visible — the
address coercion and CBOR argument encoding on the way out, the CBOR decode on
the way back — so each test pins what `call()` actually puts on the host
boundary against the expression the example used before, evaluated here rather
than described.

`cowboy_sdk.mock_host.set_call_handler` is the SDK's own hook for that boundary.

Needs the sibling `node` checkout for the SDK, the same layout the demos assume.

Run: python3 examples/test_cross_actor_calls.py (or under pytest)
"""

import ast
import functools
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.normpath(os.path.join(HERE, "..", "..", "node", "pvm", "Lib"))

_pkg = os.path.join(LIB, "cowboy_sdk")
if not os.path.isfile(os.path.join(_pkg, "testing.py")):
    raise SystemExit(
        "skip: this suite needs the sibling `node` checkout for the SDK "
        "(expected at %s). Clone cowboyinc/node alongside this repo." % LIB)


def _sdk_modules():
    return {n: m for n, m in sys.modules.items()
            if n == "cowboy_sdk" or n.startswith("cowboy_sdk.")}


def _swap_sdk(modules):
    """Make ``sys.modules``' cowboy_sdk entries exactly ``modules``, returning
    the set displaced."""
    previous = _sdk_modules()
    for name in previous:
        del sys.modules[name]
    sys.modules.update(modules)
    return previous


# Sibling suites (test_runner_result_migration.py, test_non_object_runner_results.py,
# 31-frontier/test_actors.py) install a stub `cowboy_sdk` into sys.modules and
# leave it there, live during both this module's import and its tests. Hence the
# swap in each direction: inheriting a stub gives actors fake decorators and no
# storage, and leaving ours behind breaks theirs.
_foreign = _swap_sdk({})

# Loaded as a package by path, not via sys.path: node/pvm/Lib ships a
# RustPython stdlib copy that must not shadow CPython's, and an installed pypi
# cowboy_sdk must not win over the repo one.
_spec = importlib.util.spec_from_file_location(
    "cowboy_sdk", os.path.join(_pkg, "__init__.py"), submodule_search_locations=[_pkg])
_sdk = importlib.util.module_from_spec(_spec)
sys.modules["cowboy_sdk"] = _sdk
_spec.loader.exec_module(_sdk)

from cowboy_sdk import Address, codec, mock_host  # noqa: E402
from cowboy_sdk.testing import SimulatedChain  # noqa: E402

_REAL_SDK = _swap_sdk(_foreign)


def with_real_sdk(fn):
    """Run the test body against the real SDK, restoring any stub afterwards."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        displaced = _swap_sdk(_REAL_SDK)
        try:
            return fn(*args, **kwargs)
        finally:
            _swap_sdk(displaced)
    return wrapper


def _load(path):
    full = os.path.join(HERE, path)
    # 31-frontier's actors import their sibling modules by bare name.
    sibling = os.path.dirname(full)
    added = sibling not in sys.path
    if added:
        sys.path.insert(0, sibling)
    try:
        spec = importlib.util.spec_from_file_location(
            "ex_" + os.path.basename(path)[:-3], full)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added:
            sys.path.remove(sibling)


def _record_calls(reply):
    """Capture every outgoing call at the host boundary, answering with ``reply``."""
    seen = []

    def handler(target, method, payload, cycles_limit):
        seen.append((target, method, payload, cycles_limit))
        return reply

    mock_host.set_call_handler(handler)
    return seen


FEED_HEX = "22" * 20
CASINO_HEX = "33" * 20
ESCROW_HEX = "44" * 20
WORLD_HEX = "55" * 20


# ─── what goes onto the host boundary ─────────────────────────────────────────
@with_real_sdk
def test_casino_close_round_sends_what_the_raw_call_did():
    m = _load("gallery/casino-rounds/casino.py")
    actor = m.PredictionCasino()
    chain = SimulatedChain(actor)
    chain.call("init", b"")
    # Set directly rather than through configure(), which reaches for token
    # balances the mock host does not model.
    actor.storage["feed_actor"] = FEED_HEX
    actor.storage["self_actor"] = "11" * 20
    actor.storage["active_round_id"] = 1
    actor.storage[m._round_key(1, "state")] = "open"
    actor.storage[m._round_key(1, "bettors")] = []

    seen = _record_calls(codec.encode({"price": 77, "height": 4242}))
    # close_round hands off to settle_round, which is @callable_by(SELF) — the
    # sender a timer-driven close carries.
    chain.set_sender(bytes(actor.address.as_ref()))
    chain.call("close_round", codec.encode({"round_id": 1}))

    assert len(seen) == 1, seen
    target, method, payload, cycles = seen[0]
    # The expression the example used before the swap, evaluated here.
    assert target == Address("0x" + FEED_HEX).to_bytes(), target.hex()
    assert method == "generate_price", method
    assert payload == codec.encode({}), payload
    assert cycles == 1_000_000, cycles
    # The reply reaches settlement decoded, not as bytes.
    assert actor.storage[m._round_key(1, "resolved_price")] == 77, actor.storage
    assert actor.storage[m._round_key(1, "resolved_height")] == 4242, actor.storage
    print("ok test_casino_close_round_sends_what_the_raw_call_did")


@with_real_sdk
def test_bettor_inspect_round_sends_what_the_raw_call_did():
    m = _load("gallery/casino-rounds/bettor.py")
    actor = m.AutoBettor()
    chain = SimulatedChain(actor)
    chain.call("init", b"")
    chain.call("configure", codec.encode({
        "self_actor": "0x" + "11" * 20,
        "casino_actor": "0x" + CASINO_HEX,
        "token_id": "0x" + "99" * 32,
        "bet_interval_blocks": 6,
    }))
    chain.call("start", codec.encode({}))

    seen = _record_calls(codec.encode({"state": "closed", "close_height": 0}))
    chain.set_sender(bytes(actor.address.as_ref()))
    chain.set_block_height(chain.block_height + 5)
    assert chain.call("run", b"") == b"round_not_open"

    assert len(seen) == 1, seen
    target, method, payload, cycles = seen[0]
    assert target == Address("0x" + CASINO_HEX).to_bytes(), target.hex()
    assert method == "inspect_round", method
    assert payload == codec.encode({}), payload
    assert cycles == 1_000_000, cycles
    print("ok test_bettor_inspect_round_sends_what_the_raw_call_did")


@with_real_sdk
def test_game_end_turn_still_sends_an_empty_status_payload():
    m = _load("31-frontier/game_actor.py")
    actor = m.Game()
    chain = SimulatedChain(actor)
    chain.call("init", b"{}")
    actor.storage["world"] = WORLD_HEX

    status = {"cities_per_owner": {"1": 3, "2": 1},
              "pieces_per_owner": {"1": 4, "2": 2}, "date": 9}
    seen = _record_calls(codec.encode(status))
    out = codec.decode(chain.call("end_turn", codec.encode({})))

    # Arbitration ran on the reply, not on the except-branch fallback — which
    # would have yielded zeroes and declared a wipeout.
    assert (out["user_cities"], out["comp_cities"]) == (3, 1), out
    assert (out["user_pieces"], out["comp_pieces"]) == (4, 2), out
    assert out["winner"] == 0, out
    assert len(seen) == 1, seen
    target, method, payload, cycles = seen[0]
    # A bare 40-hex string, no 0x — `call` coerces it exactly as before.
    assert target == Address(WORLD_HEX).to_bytes(), target.hex()
    assert method == "status", method
    assert payload == b"", payload      # args=None is the old b"" literal
    assert cycles == 2_000_000, cycles
    print("ok test_game_end_turn_still_sends_an_empty_status_payload")


# ─── the removed decoders were already no-ops on a call() result ──────────────
@with_real_sdk
def test_escrow_result_reaches_the_buyer_decoded():
    m = _load("core/06-token-escrow/buyer_actor.py")
    actor = m.BuyerActor()
    chain = SimulatedChain(actor)
    chain.call("init", b"")
    chain.call("configure", codec.encode({
        "escrow_address": "0x" + ESCROW_HEX, "token_id": "0x" + "99" * 32}))

    escrow_reply = {"status": "deposited", "trade_id": "t1", "state": "DEPOSITED"}
    seen = _record_calls(codec.encode(escrow_reply))
    out = codec.decode(chain.call("deposit", codec.encode(
        {"trade_id": "t1", "seller": "0x" + "66" * 20, "amount": 5})))

    assert out["escrow_result"] == escrow_reply, out
    target, method, payload, cycles = seen[0]
    assert target == Address("0x" + ESCROW_HEX).to_bytes(), target.hex()
    assert method == "deposit" and cycles == 100_000, (method, cycles)
    assert codec.decode(payload) == {
        "trade_id": "t1", "seller": "0x" + "66" * 20, "amount": 5}, codec.decode(payload)
    print("ok test_escrow_result_reaches_the_buyer_decoded")


@with_real_sdk
def test_seller_lock_result_reaches_the_seller_decoded():
    m = _load("core/06-token-escrow/seller_actor.py")
    actor = m.SellerActor()
    chain = SimulatedChain(actor)
    chain.call("init", b"")
    chain.call("configure", codec.encode({"escrow_address": "0x" + ESCROW_HEX}))

    escrow_reply = {"status": "locked", "trade_id": "t1", "state": "LOCKED"}
    _record_calls(codec.encode(escrow_reply))
    out = codec.decode(chain.call("lock", codec.encode({"trade_id": "t1"})))

    assert out["escrow_result"] == escrow_reply, out
    print("ok test_seller_lock_result_reaches_the_seller_decoded")


@with_real_sdk
def test_deleted_decoder_was_a_no_op_on_every_reply_shape():
    """The removed `_decode_result` only did work when handed raw bytes.

    `call()` never hands it those — it returns `codec.decode(...)` — so every
    shape an escrow handler can reply with passed through untouched.
    """
    def removed_decoder(result):            # verbatim, as it stood before
        import json
        if isinstance(result, bytes):
            try:
                return codec.decode(result)
            except Exception:
                try:
                    return json.loads(result.decode("utf-8"))
                except Exception:
                    return result.hex()
        return result

    for reply in ({"status": "deposited"}, {"error": "OWNER_ONLY"}, [], "", 0, None):
        assert removed_decoder(reply) is reply, reply
    print("ok test_deleted_decoder_was_a_no_op_on_every_reply_shape")


# ─── acceptance: no example decodes a cross-actor result by hand ──────────────
_CALL_NAMES = {"call", "sdk_call", "call_actor"}
_DECODERS = {"decode", "loads"}


def _callee_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _simulated_chain_names(tree):
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and _callee_name(node.value) == "SimulatedChain"):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotation = node.annotation
            if (isinstance(annotation, ast.Name) and annotation.id == "SimulatedChain") or (
                    isinstance(annotation, ast.Constant)
                    and annotation.value == "SimulatedChain"):
                names.add(node.target.id)
    return names


def _is_sdk_call(node, simulated_chains):
    if isinstance(node.func, ast.Name):
        return node.func.id in _CALL_NAMES
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "call_actor":
        return True
    return not (
        node.func.attr == "call"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in simulated_chains
    ) and node.func.attr == "call"


def _hand_decoded_call_results(tree):
    """Lines where a value returned by a cross-actor call is decoded by hand.

    Tracks names bound from ``call`` / ``sdk_call`` / ``call_actor`` inside each
    function, then flags decoding them (``codec.decode``, ``json.loads``,
    ``.decode()``) or testing them for bytes — `call()` already returns a
    decoded value, so both mean someone re-implemented the codec boundary.
    Also catches the no-intermediate-name form, ``codec.decode(call(...))``.

    Name-scoped, so a result handed to a helper before being decoded there
    still slips past; the point is to stop recognizing one helper name.
    """
    hits = []
    simulated_chains = _simulated_chain_names(tree)
    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]
    for scope in scopes:
        bound = set()
        for node in ast.walk(scope):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and _is_sdk_call(node.value, simulated_chains)):
                bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if not bound:
            continue
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                if _callee_name(node) in _DECODERS:
                    for arg in node.args:
                        if isinstance(arg, ast.Name) and arg.id in bound:
                            hits.append(node.lineno)
                if (_callee_name(node) == "isinstance" and len(node.args) == 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in bound
                        and "bytes" in ast.dump(node.args[1])):
                    hits.append(node.lineno)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and _callee_name(node) in _DECODERS
                and any(isinstance(a, ast.Call) and _is_sdk_call(a, simulated_chains)
                        for a in node.args)):
            hits.append(node.lineno)
    return sorted(set(hits))


@with_real_sdk
def test_no_example_decodes_a_cross_actor_result_by_hand():
    offenders = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if root == HERE and name.startswith("test_"):
                continue  # the cross-example suites carry the needles as subject matter
            rel = os.path.relpath(path, HERE)
            source = open(path, encoding="utf-8").read()
            for lineno, line in enumerate(source.splitlines(), 1):
                if "runtime.call_actor" in line:
                    offenders.append("%s:%d raw call_actor" % (rel, lineno))
            for lineno in _hand_decoded_call_results(ast.parse(source)):
                offenders.append("%s:%d hand-decoded call result" % (rel, lineno))
    assert offenders == [], offenders
    print("ok test_no_example_decodes_a_cross_actor_result_by_hand")


@with_real_sdk
def test_the_acceptance_check_catches_a_reintroduced_decoder():
    """The check above is only worth having if it fails on the real regression."""
    reintroduced = (
        "def handler(self):\n"
        "    result = sdk_call(addr, 'tenant_verified', {})\n"
        "    verdict = ''\n"
        "    if isinstance(result, dict):\n"
        "        verdict = result.get('r', '')\n"
        "    elif isinstance(result, (bytes, bytearray)):\n"
        "        verdict = (codec.decode(bytes(result)) or {}).get('r', '')\n"
    )
    assert _hand_decoded_call_results(ast.parse(reintroduced)) == [6], reintroduced
    inline = "def h(self):\n    x = codec.decode(runtime.call_actor(a, 'm', b'', 1))\n"
    assert _hand_decoded_call_results(ast.parse(inline)) == [2], inline
    simulated = ("def h(self):\n"
                 "    chain = SimulatedChain(actor)\n"
                 "    x = codec.decode(chain.call('m', b''))\n")
    assert _hand_decoded_call_results(ast.parse(simulated)) == [], simulated
    actor_ref = "def h(self):\n    x = codec.decode(ref.call('m', b''))\n"
    assert _hand_decoded_call_results(ast.parse(actor_ref)) == [2], actor_ref
    # Guards are allowed, decoders are not: a dict check must not trip it.
    guard = ("def h(self):\n"
             "    result = call(addr, 'm', {})\n"
             "    return result.get('r') if isinstance(result, dict) else ''\n")
    assert _hand_decoded_call_results(ast.parse(guard)) == [], guard
    print("ok test_the_acceptance_check_catches_a_reintroduced_decoder")


def main():
    tests = [
        test_casino_close_round_sends_what_the_raw_call_did,
        test_bettor_inspect_round_sends_what_the_raw_call_did,
        test_game_end_turn_still_sends_an_empty_status_payload,
        test_escrow_result_reaches_the_buyer_decoded,
        test_seller_lock_result_reaches_the_seller_decoded,
        test_deleted_decoder_was_a_no_op_on_every_reply_shape,
        test_no_example_decodes_a_cross_actor_result_by_hand,
        test_the_acceptance_check_catches_a_reintroduced_decoder,
    ]
    for test in tests:
        test()
    print("\nall %d cross-actor call tests passed" % len(tests))


if __name__ == "__main__":
    main()
