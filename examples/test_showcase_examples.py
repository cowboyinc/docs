"""Checks for the four showcase examples the docs point new builders at.

These are the examples a reader copies from, so what they demonstrate has to be
the SDK's own surface rather than a local re-implementation of it: ownership out
of the runtime's reserved slot, admin handlers gated by ``@callable_by(OWNER)``,
a timer fire routed by ``@timer``, a continuation entrypoint tagged ``@public``.
Each test drives the real actor through ``cowboy_sdk.testing.SimulatedChain``, so
a claim about what a decorator enforces is settled by the SDK, not by this file.

The last two tests are the acceptance check: the showcase files import nothing
but the public ``cowboy_sdk`` surface and the stdlib, with a negative control so
the check is known to fail on the regression it exists to catch.

Needs the sibling `node` checkout for the SDK, the same layout the demos assume.

Run: python3 examples/test_showcase_examples.py (or under pytest)
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

from cowboy_sdk import Address, codec, mock_host, runtime  # noqa: E402
from cowboy_sdk.errors import PermissionDeniedError  # noqa: E402
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
    spec = importlib.util.spec_from_file_location(
        "ex_" + os.path.basename(path)[:-3], os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SHOWCASE = [
    "core/02-senders-and-permissions/auth_roles.py",
    "core/07-timers-and-automation/timer_counter.py",
    "core/09-runner-llm/runner_llm.py",
    "gallery/liquidity-pools/v2_pool.py",
]

OWNER_ADDR = b"\xa1" * 20
WRITER_ADDR = b"\xb2" * 20
STRANGER_ADDR = b"\xc3" * 20


def _hex(raw):
    return Address(raw).to_hex(checksum=False)


def _auth_roles(owner_payload=b""):
    """A deployed AuthRoles with ``OWNER_ADDR`` as the initializing sender."""
    mock_host.reset()
    module = _load("core/02-senders-and-permissions/auth_roles.py")
    chain = SimulatedChain(module.AuthRoles())
    chain.set_sender(OWNER_ADDR)
    chain.call("init", owner_payload)
    return chain


# ─── ownership comes from the runtime, not from actor storage ─────────────────
@with_real_sdk
def test_auth_roles_owner_lives_in_the_runtime_slot():
    chain = _auth_roles()
    assert runtime.get_owner() == OWNER_ADDR, runtime.get_owner().hex()
    # The reserved slot is the only record: a storage copy would be the thing
    # @callable_by(OWNER) does *not* read, and so the thing that can drift.
    assert chain.state(b"owner") is None, chain.state(b"owner")
    roles = codec.decode(chain.call("get_roles", codec.encode({})))
    assert roles == {"owner": _hex(OWNER_ADDR), "roles": {"writer": []}}, roles
    print("ok test_auth_roles_owner_lives_in_the_runtime_slot")


@with_real_sdk
def test_auth_roles_init_owner_argument_transfers_ownership():
    chain = _auth_roles(b'{"owner": "' + _hex(WRITER_ADDR).encode() + b'"}')
    assert runtime.get_owner() == WRITER_ADDR, runtime.get_owner().hex()
    try:
        chain.call("grant_role", codec.encode({"role": "writer", "address": _hex(OWNER_ADDR)}))
        raise AssertionError("the former owner could still grant a role")
    except PermissionDeniedError:
        pass
    print("ok test_auth_roles_init_owner_argument_transfers_ownership")


@with_real_sdk
def test_auth_roles_reinit_is_a_no_op_and_cannot_retake_ownership():
    """Deploy runs init atomically, and the demos call it again — so the second
    call has to be reachable, and has to change nothing."""
    chain = _auth_roles()
    chain.call("grant_role", codec.encode({"role": "writer", "address": _hex(WRITER_ADDR)}))

    chain.set_sender(STRANGER_ADDR)
    again = codec.decode(chain.call("init", b'{"owner": "' + _hex(STRANGER_ADDR).encode() + b'"}'))
    assert again == {"status": "already_initialized", "owner": _hex(OWNER_ADDR)}, again
    assert runtime.get_owner() == OWNER_ADDR, runtime.get_owner().hex()
    roles = codec.decode(chain.call("get_roles", codec.encode({})))["roles"]
    assert roles == {"writer": [_hex(WRITER_ADDR)]}, roles
    print("ok test_auth_roles_reinit_is_a_no_op_and_cannot_retake_ownership")


# ─── the two authorization layers ─────────────────────────────────────────────
@with_real_sdk
def test_auth_roles_admin_handlers_are_owner_only():
    chain = _auth_roles()
    grant = codec.encode({"role": "writer", "address": _hex(WRITER_ADDR)})
    chain.set_sender(STRANGER_ADDR)
    for handler in ("grant_role", "revoke_role"):
        try:
            chain.call(handler, grant)
            raise AssertionError("%s was reachable by a stranger" % handler)
        except PermissionDeniedError:
            pass
    chain.set_sender(OWNER_ADDR)
    granted = codec.decode(chain.call("grant_role", grant))
    assert granted == {"status": "granted", "role": "writer",
                       "address": _hex(WRITER_ADDR)}, granted
    print("ok test_auth_roles_admin_handlers_are_owner_only")


@with_real_sdk
def test_auth_roles_writer_allowlist_is_checked_in_the_handler():
    chain = _auth_roles()
    entry = codec.encode({"content": "delegated"})

    # @public, so the call reaches the handler; the allowlist is what rejects it.
    chain.set_sender(WRITER_ADDR)
    assert codec.decode(chain.call("write_entry", entry)) == {"error": "UNAUTHORIZED"}

    chain.set_sender(OWNER_ADDR)
    chain.call("grant_role", codec.encode(
        {"role": "writer", "address": _hex(WRITER_ADDR)}))
    chain.set_sender(WRITER_ADDR)
    written = codec.decode(chain.call("write_entry", entry))
    assert written == {"status": "written", "entry": {
        "entry_id": 1, "author": _hex(WRITER_ADDR), "content": "delegated"}}, written

    chain.set_sender(OWNER_ADDR)
    chain.call("revoke_role", codec.encode(
        {"role": "writer", "address": _hex(WRITER_ADDR)}))
    chain.set_sender(WRITER_ADDR)
    assert codec.decode(chain.call("write_entry", entry)) == {"error": "UNAUTHORIZED"}

    # The owner never needed the role, and the revoke left earlier entries alone.
    chain.set_sender(OWNER_ADDR)
    assert codec.decode(chain.call("write_entry", entry))["status"] == "written"
    entries = codec.decode(chain.call("get_entries", codec.encode({})))["entries"]
    assert [e["author"] for e in entries] == [_hex(WRITER_ADDR), _hex(OWNER_ADDR)], entries
    print("ok test_auth_roles_writer_allowlist_is_checked_in_the_handler")


@with_real_sdk
def test_auth_roles_role_members_are_stored_as_normalized_addresses():
    """A checksummed or unprefixed grant has to match the sender on write_entry."""
    chain = _auth_roles()
    mixed = Address(WRITER_ADDR).to_hex()          # EIP-55 mixed case
    assert mixed != _hex(WRITER_ADDR), mixed
    chain.call("grant_role", codec.encode({"role": "writer", "address": mixed}))
    roles = codec.decode(chain.call("get_roles", codec.encode({})))["roles"]
    assert roles["writer"] == [_hex(WRITER_ADDR)], roles
    chain.set_sender(WRITER_ADDR)
    assert codec.decode(chain.call(
        "write_entry", codec.encode({"content": "x"})))["status"] == "written"
    print("ok test_auth_roles_role_members_are_stored_as_normalized_addresses")


# ─── init survives the second call every demo makes ───────────────────────────
# `cowboy actor deploy` invokes init in the deploy transaction (init_handler
# defaults to "init"), and the SDK sets __INITIALIZED__ once it returns. Each
# demo.sh then calls init again as its own step, so an init that is neither
# @public nor idempotent breaks the demo two ways: deny-by-default rejects the
# call, and `set -e` stops the script before it reaches the interesting handler.
# The last two elements seed a key that accumulates while the actor runs. Without
# that, a re-run of init writes its same defaults over pristine state and a
# snapshot comparison calls it idempotent when it is only redundant.
_INIT_TWICE = [
    ("core/02-senders-and-permissions/auth_roles.py", "AuthRoles", b"{}",
     b"entries", [{"entry_id": 1, "author": _hex(WRITER_ADDR), "content": "kept"}]),
    ("core/07-timers-and-automation/timer_counter.py", "TimerCounter",
     b'{"interval_blocks": 2, "max_ticks": 2}', b"count", 7),
    ("core/09-runner-llm/runner_llm.py", "RunnerLLMActor", b"{}",
     b"request_count", 3),
]


@with_real_sdk
def test_showcase_init_is_callable_and_idempotent_after_deploy():
    offenders = []
    for rel, class_name, payload, key, accumulated in _INIT_TWICE:
        mock_host.reset()
        module = _load(rel)
        chain = SimulatedChain(getattr(module, class_name)())
        chain.set_sender(OWNER_ADDR)
        chain.call("init", payload)                      # the deploy-time init
        assert runtime.is_initialized(), rel

        mock_host.set_state(key, codec.encode(accumulated))
        before = chain.snapshot()
        try:
            chain.call("init", payload)                  # the demo's own step
        except Exception as exc:
            offenders.append("%s: second init raised %s" % (rel, type(exc).__name__))
            continue
        after = chain.snapshot()
        changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
        if changed:
            offenders.append("%s: second init rewrote %s" % (rel, sorted(changed)))
    assert offenders == [], offenders
    print("ok test_showcase_init_is_callable_and_idempotent_after_deploy")


# ─── the timer fire handler is reachable by a fire and by nothing else ────────
@with_real_sdk
def test_timer_counter_fire_handler_is_closed_to_outside_callers():
    mock_host.reset()
    module = _load("core/07-timers-and-automation/timer_counter.py")
    actor = module.TimerCounter()
    chain = SimulatedChain(actor)
    chain.set_sender(OWNER_ADDR)
    chain.call("init", b'{"interval_blocks": 2, "max_ticks": 2}')
    chain.call("start", codec.encode({}))

    # @timer, not @public: an outside caller cannot advance the counter by hand.
    try:
        chain.call("on_timer", b"")
        raise AssertionError("on_timer was reachable by an outside caller")
    except PermissionDeniedError:
        pass
    assert codec.decode(chain.state(b"count")) == 0, chain.state(b"count")

    # A fire carries the actor's own address as sender, which deny-by-default
    # admits — the same path test_timer_scheduling.py drives end to end.
    chain.set_sender(bytes(actor.address.as_ref()))
    fired = chain.advance_block(2)
    assert [handler for handler, _ in fired] == ["on_timer"], fired
    assert codec.decode(chain.state(b"count")) == 1, chain.state(b"count")
    print("ok test_timer_counter_fire_handler_is_closed_to_outside_callers")


# ─── the continuation entrypoint declares its own permission ──────────────────
@with_real_sdk
def test_runner_llm_entrypoint_is_public_and_init_claims_ownership():
    mock_host.reset()
    module = _load("core/09-runner-llm/runner_llm.py")
    assert getattr(module.RunnerLLMActor.ask_llm, "_call_perm", None) == "public", (
        "@public must sit outside @runner.continuation so the tag lands on the "
        "compiled entrypoint")
    chain = SimulatedChain(module.RunnerLLMActor())
    chain.set_sender(OWNER_ADDR)
    chain.call("init", b"")
    assert runtime.get_owner() == OWNER_ADDR, runtime.get_owner().hex()
    assert codec.decode(chain.state(b"owner")) == _hex(OWNER_ADDR), chain.state(b"owner")
    print("ok test_runner_llm_entrypoint_is_public_and_init_claims_ownership")


# ─── acceptance: the showcase set reaches for nothing but the public SDK ──────
# Modules a showcase example may import besides cowboy_sdk. Deliberately short:
# an example that needs more than the deterministic stdlib the PVM allows is
# teaching something other than the SDK.
_ALLOWED_IMPORTS = {"json"}

# Receivers whose private attributes would be SDK internals.
_SDK_NAMESPACES = {
    "cowboy_sdk", "runtime", "codec", "runner", "timer", "call", "send",
    "events", "stream", "watchtower", "permissions", "payload_codec",
}


def _private_api_uses(tree):
    """Lines reaching past the public SDK: a private import, a private
    attribute off an SDK namespace, or the host module itself."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root != "cowboy_sdk" and root not in _ALLOWED_IMPORTS:
                    hits.append((node.lineno, "imports %s" % alias.name))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or (root != "cowboy_sdk" and root not in _ALLOWED_IMPORTS):
                hits.append((node.lineno, "imports from %s" % (node.module or ".")))
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    hits.append((node.lineno, "imports private %s" % alias.name))
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            value = node.value
            if isinstance(value, ast.Name) and value.id in _SDK_NAMESPACES:
                hits.append((node.lineno, "%s.%s" % (value.id, node.attr)))
    return sorted(set(hits))


@with_real_sdk
def test_showcase_examples_use_only_the_public_sdk():
    offenders = []
    for rel in SHOWCASE:
        source = open(os.path.join(HERE, rel), encoding="utf-8").read()
        for lineno, what in _private_api_uses(ast.parse(source)):
            offenders.append("%s:%d %s" % (rel, lineno, what))
    assert offenders == [], offenders
    print("ok test_showcase_examples_use_only_the_public_sdk")


@with_real_sdk
def test_the_acceptance_check_catches_a_private_api_use():
    """The check above is only worth having if it fails on the real regression."""
    reintroduced = (
        "import json\n"
        "import pvm_host\n"
        "from cowboy_sdk import actor, runtime\n"
        "from cowboy_sdk.payload_codec import _encode_value\n"
        "from .helpers import parse\n"
        "def h(self):\n"
        "    host = runtime._host()\n"
        "    return runtime.get_block_height()\n"
    )
    assert _private_api_uses(ast.parse(reintroduced)) == [
        (2, "imports pvm_host"),
        (4, "imports private _encode_value"),
        (5, "imports from helpers"),
        (7, "runtime._host"),
    ], _private_api_uses(ast.parse(reintroduced))
    # A local underscore helper is not an SDK private: the timer example's own
    # _timer_payload must not trip it.
    local = ("from cowboy_sdk import codec\n"
             "def _timer_payload(p):\n"
             "    return codec.decode(p)\n"
             "def h(p):\n"
             "    return _timer_payload(p)\n")
    assert _private_api_uses(ast.parse(local)) == [], local
    print("ok test_the_acceptance_check_catches_a_private_api_use")


# ─── the runner resume is reachable only by the runner callback ───────────────
@with_real_sdk
def test_runner_llm_resume_rejects_a_forged_result():
    """``@actor`` does not permission-wrap a generated ``__resume``, so the
    module-level entrypoint is the only gate. The real callback arrives as a
    deferred ExecuteActor whose sender is the actor itself; a stranger holding a
    cid — derived from public tx data — must not be able to resume."""
    mock_host.reset()
    module = _load("core/09-runner-llm/runner_llm.py")
    actor = module.RunnerLLMActor()
    chain = SimulatedChain(actor)
    chain.set_sender(OWNER_ADDR)
    chain.call("init", b"")
    chain.call("ask_llm", codec.encode({"prompt": "real"}))

    prefix = b"__continuation:"
    cid = [k[len(prefix):] for k in chain.snapshot() if k.startswith(prefix)][0]
    forged = codec.encode({"cid": cid, "result": {"text": "ATTACKER CONTROLLED"}})

    chain.set_sender(STRANGER_ADDR)
    try:
        module.ask_llm__resume(forged)
        raise AssertionError("a forged runner result was accepted")
    except PermissionDeniedError:
        pass
    assert codec.decode(chain.state(b"llm:0:status")) == "pending", "state moved"
    assert chain.state(b"latest_response") is None, chain.state(b"latest_response")

    # The callback's own sender — the actor — still gets through.
    chain.set_sender(bytes(actor.address.as_ref()))
    module.ask_llm__resume(forged)
    assert codec.decode(chain.state(b"latest_response")) == "ATTACKER CONTROLLED"
    assert codec.decode(chain.state(b"llm:0:status")) == "done"
    print("ok test_runner_llm_resume_rejects_a_forged_result")


# ─── timer scheduling runs in deferred mode (CIP-6 §8) ───────────────────────
@with_real_sdk
def test_timer_counter_start_is_deferred():
    """CIP-6 §8 lists ``runtime.schedule_timer`` among the async side effects a
    pure handler must not issue. The shipped SDK gates ``send``/``submit_job``
    but not the timer syscalls, so this pins the declaration rather than the
    error it would otherwise raise."""
    mock_host.reset()
    module = _load("core/07-timers-and-automation/timer_counter.py")
    from cowboy_sdk import handler_mode

    seen = {}
    real = runtime.schedule_after

    def spy(*args, **kwargs):
        seen["mode"] = handler_mode._get_mode()
        return real(*args, **kwargs)

    runtime.schedule_after = spy
    try:
        chain = SimulatedChain(module.TimerCounter())
        chain.set_sender(OWNER_ADDR)
        chain.call("init", b'{"interval_blocks": 2, "max_ticks": 2}')
        chain.call("start", codec.encode({}))
    finally:
        runtime.schedule_after = real
    assert seen.get("mode") == handler_mode.DEFERRED, seen
    print("ok test_timer_counter_start_is_deferred")


def main():
    tests = [
        test_auth_roles_owner_lives_in_the_runtime_slot,
        test_auth_roles_init_owner_argument_transfers_ownership,
        test_auth_roles_reinit_is_a_no_op_and_cannot_retake_ownership,
        test_auth_roles_admin_handlers_are_owner_only,
        test_auth_roles_writer_allowlist_is_checked_in_the_handler,
        test_auth_roles_role_members_are_stored_as_normalized_addresses,
        test_showcase_init_is_callable_and_idempotent_after_deploy,
        test_timer_counter_fire_handler_is_closed_to_outside_callers,
        test_runner_llm_entrypoint_is_public_and_init_claims_ownership,
        test_runner_llm_resume_rejects_a_forged_result,
        test_timer_counter_start_is_deferred,
        test_showcase_examples_use_only_the_public_sdk,
        test_the_acceptance_check_catches_a_private_api_use,
    ]
    for test in tests:
        test()
    print("\nall %d showcase example tests passed" % len(tests))


if __name__ == "__main__":
    main()
