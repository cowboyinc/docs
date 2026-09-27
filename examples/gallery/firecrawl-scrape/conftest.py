"""Use the actual SDK decorators with the SDK's in-memory host."""

import _sdk_path  # noqa: F401  (examples/_sdk_path.py, on pythonpath via pyproject.toml)

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
# cowboy_sdk itself comes from _sdk_path, which loads the package by file
# location; putting node/pvm/Lib on sys.path would shadow CPython's stdlib.

import actors.scrape.main as fc
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
    # SimulatedChain-based tests swap sys.modules["pvm_host"] for their own
    # proxy; rebind it so these fixtures and the actor talk to the same host.
    monkeypatch.setitem(sys.modules, "pvm_host", host_mock)
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    events = []
    write = host_mock.set_state

    def bounded_write(key, value):
        assert len(value) <= 65_536, "Node storage value limit"
        write(key, value)
        assert sum(len(k) + len(v) for k, v in host_mock._storage.items()) <= 1_048_576

    emit_host = host_mock.emit_event

    def emit(name, payload, schema=None):
        assert len(payload) <= 4096, "Node event payload limit"
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
    actor = fc.FirecrawlScrapeWrapper()
    actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    actor.register_hire(codec.encode(HIRE))
    host.set_context(sender=BUYER)
    host.events.clear()
    return actor
