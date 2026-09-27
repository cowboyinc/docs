"""Load the SDK decorators against the in-memory host with Node limits."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SDK = Path(os.environ.get("COWBOY_SDK_PATH", HERE.parents[3] / "node/pvm/Lib")).resolve()
spec = importlib.util.spec_from_file_location(
    "pvm_host", SDK / "cowboy_sdk/tests/mock_pvm_host.py"
)
host_mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_mock)
sys.modules["pvm_host"] = host_mock
sys.path.insert(0, str(SDK))

import actors.browse.main as kernel
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

    emit_host = host_mock.emit_event

    def emit(name, payload, schema=None):
        assert len(payload) <= 4_096
        result = emit_host(name, payload, schema=schema)
        events.append((name, codec.decode(payload)))
        return result

    monkeypatch.setattr(host_mock, "set_state", bounded_write)
    monkeypatch.setattr(host_mock, "emit_event", emit)
    monkeypatch.setattr(host_mock, "events", events, raising=False)
    yield host_mock
    host_mock.reset()


@pytest.fixture
def actor(host):
    wrapper = kernel.KernelBrowseWrapper()
    wrapper.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    wrapper.register_hire(codec.encode(HIRE))
    host.set_context(sender=BUYER)
    host.events.clear()
    return wrapper
