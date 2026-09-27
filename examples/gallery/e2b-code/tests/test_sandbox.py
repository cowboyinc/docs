"""SimulatedChain smoke tests for the sandbox target (offline, no vendor)."""

import pytest

from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.sandbox.main import E2BCodeWrapper

REGISTRAR = "0x" + "44" * 20


def test_init_records_registrar_and_config():
    actor = E2BCodeWrapper()
    chain = SimulatedChain(actor)
    out = codec.decode(chain.call(actor.init, codec.encode({"store_registrar": REGISTRAR})))
    assert out == {"ok": True, "vendor": "e2b"}
    config = codec.decode(chain.call(actor.get_config, b""))
    assert config["store_registrar"] == REGISTRAR
    assert config["vendor"] == "e2b" and config["call_count"] == 0


def test_capability_advertises_run_and_poll():
    actor = E2BCodeWrapper()
    chain = SimulatedChain(actor)
    cap = codec.decode(chain.call(actor.capability, b""))
    assert cap["handlers"] == ["run"]
    assert cap["poll_handler"] == "get_result"


def test_register_hire_requires_the_registrar():
    actor = E2BCodeWrapper()
    chain = SimulatedChain(actor)
    chain.set_sender(REGISTRAR)
    chain.call(actor.init, codec.encode({"store_registrar": REGISTRAR}))
    hire = {"hire_id": 7, "hirer": "0x" + "33" * 20,
            "declaration_id": "0x" + "55" * 32, "version": 1, "artifact_id": 1}
    chain.set_sender("0x" + "66" * 20)
    with pytest.raises(ValueError, match="registrar"):
        chain.call(actor.register_hire, codec.encode(hire))
    chain.set_sender(REGISTRAR)
    out = codec.decode(chain.call(actor.register_hire, codec.encode(hire)))
    assert out["ok"] is True and out["hire_id"] == 7
