"""Exercise the actual SDK permission wrappers with its CPython mock host.

Set COWBOY_SDK_PATH to the node/pvm/Lib checkout. These are not native PVM tests.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SDK_LIB = os.environ.get("COWBOY_SDK_PATH")
if SDK_LIB:
    # Append: prepend/PYTHONPATH would shadow CPython's stdlib with PVM's.
    sys.path.append(SDK_LIB)
sdk = pytest.importorskip("cowboy_sdk", reason="Set COWBOY_SDK_PATH to exercise the actor SDK seam")
from cowboy_sdk import codec, mock_host, runtime
from cowboy_sdk.errors import PermissionDeniedError

spec = importlib.util.spec_from_file_location("inference_actor", Path(__file__).parents[1] / "actor.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

OWNER = b"o" * 20
ACTOR = b"a" * 20


@pytest.fixture(autouse=True)
def host(monkeypatch):
    mock_host.reset()
    # The SDK mock omits event emission; retain real codec/permission wrappers.
    monkeypatch.setattr(
        mock_host,
        "emit_event",
        lambda name, body, schema=None: {"emit_id": 1, "sync_outcomes": [], "deferred_subs": []},
        raising=False,
    )
    mock_host.set_context(sender=OWNER, actor_addr=ACTOR)
    module.init(b"")


def config():
    return dict(slot="cloud", hire_id=7, declaration_id="decl", version=1,
                artifact_id=3, volume_owner="ab" * 20, volume_name="hire-7",
                public_base_url="https://api.example", active_key_cap=8)


def test_init_accepts_the_deploy_cli_default_payloads():
    for empty in (b"{}", b"\xa0", None):
        mock_host.reset()
        mock_host.set_context(sender=OWNER, actor_addr=ACTOR)
        module.init(empty)
        assert module.InferenceActor().storage[module.SLOTS_KEY] == {}
    mock_host.reset()
    mock_host.set_context(sender=OWNER, actor_addr=ACTOR)
    with pytest.raises(ValueError):
        module.init(codec.encode({"slot": "x"}))


def test_owner_configuration_and_reinit_guard():
    module.configure_slot(codec.encode(config()))
    stored = module.InferenceActor().storage[module.SLOTS_KEY]
    assert stored["cloud"]["hire_id"] == 7
    mock_host.set_context(sender=b"x" * 20)
    with pytest.raises(PermissionDeniedError):
        module.configure_slot(codec.encode(dict(config(), hire_id=99)))
    with pytest.raises(PermissionDeniedError):
        module.init(b"")
    assert module.InferenceActor().storage[module.SLOTS_KEY] == stored
    assert runtime.get_state(b"__cowboy/routes") is None


@pytest.mark.parametrize("field,value", [("active_key_cap", 0), ("active_key_cap", True), ("hire_id", -1), ("version", 0), ("volume_owner", "ab" * 32), ("volume_name", "../other"), ("slot", "x" * 129), ("public_base_url", "https://api.example/?secret=x")])
def test_invalid_slot_configuration_does_not_write(field, value):
    with pytest.raises(ValueError):
        module.configure_slot(codec.encode(dict(config(), **{field: value})))
    assert module.InferenceActor().storage[module.SLOTS_KEY] == {}


def test_caller_cannot_store_a_hirer_claim_or_secret():
    for field in ("hirer", "secret", "signing_key"):
        with pytest.raises(ValueError):
            module.configure_slot(codec.encode(dict(config(), **{field: "bad"})))
    assert module.InferenceActor().storage[module.SLOTS_KEY] == {}


def test_slot_count_bound_and_existing_slot_replacement():
    instance = module.InferenceActor()
    instance.storage[module.SLOTS_KEY] = {str(i): {} for i in range(module.MAX_SLOTS)}
    with pytest.raises(ValueError):
        module.configure_slot(codec.encode(config()))
    module.configure_slot(codec.encode(dict(config(), slot="0")))
    assert len(instance.storage[module.SLOTS_KEY]) == module.MAX_SLOTS


def test_owner_installs_the_routes_table_verbatim():
    blob = b"\xa2gversion\x01froutes\x80"  # {"version":1,"routes":[]}
    module.set_routes(blob)
    assert runtime.get_state(b"__cowboy/routes") == blob
    for bad in (b"", b"x" * (module.MAX_ROUTES_BYTES + 1)):
        with pytest.raises(ValueError):
            module.set_routes(bad)
    assert runtime.get_state(b"__cowboy/routes") == blob
    mock_host.set_context(sender=b"x" * 20)
    with pytest.raises(PermissionDeniedError):
        module.set_routes(b"\xa0")
    assert runtime.get_state(b"__cowboy/routes") == blob
