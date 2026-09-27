"""Load the SDK decorators against the in-memory host with Node limits."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest


SDK = Path(
    os.environ.get(
        "COWBOY_SDK_PATH",
        Path(__file__).resolve().parents[4] / "node/pvm/Lib",
    )
).resolve()
spec = importlib.util.spec_from_file_location(
    "pvm_host", SDK / "cowboy_sdk/tests/mock_pvm_host.py"
)
host_mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_mock)
sys.modules["pvm_host"] = host_mock
# Load only the SDK package. Its RustPython Lib directory also contains shims
# such as threading.py that must not replace CPython's standard library.
spec = importlib.util.spec_from_file_location(
    "cowboy_sdk",
    SDK / "cowboy_sdk/__init__.py",
    submodule_search_locations=[str(SDK / "cowboy_sdk")],
)
sdk = importlib.util.module_from_spec(spec)
sys.modules["cowboy_sdk"] = sdk
spec.loader.exec_module(sdk)

import actors.deepgram.main as deepgram
from cowboy_sdk import codec


OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
HIRE = {
    "hire_id": 42,
    "hirer": "0x" + BUYER.hex(),
    "declaration_id": "0x" + "55" * 32,
    "version": 3,
    "artifact_id": 7,
}


@pytest.fixture
def host(monkeypatch):
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    events = []
    write = host_mock.set_state

    def bounded_write(key, value):
        assert len(value) <= 65_536
        write(key, value)
        assert sum(len(k) + len(v) for k, v in host_mock._storage.items()) <= 1_048_576

    def emit(name, payload):
        assert len(payload) <= 4_096
        events.append((name, codec.decode(payload)))

    monkeypatch.setattr(host_mock, "set_state", bounded_write)
    monkeypatch.setattr(host_mock, "emit_event", emit)
    monkeypatch.setattr(host_mock, "events", events, raising=False)
    yield host_mock
    host_mock.reset()


@pytest.fixture
def actor(host):
    wrapper = deepgram.DeepgramTranscribeWrapper()
    wrapper.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    wrapper.register_hire(codec.encode(HIRE))
    host.set_context(sender=BUYER)
    host.events.clear()
    return wrapper
