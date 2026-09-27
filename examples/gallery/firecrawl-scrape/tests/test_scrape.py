"""SimulatedChain smoke tests for the scrape target (offline, no vendor)."""

import pytest

from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.scrape.main import FirecrawlScrapeWrapper

REGISTRAR = "0x" + "44" * 20


def test_init_records_registrar_and_config():
    actor = FirecrawlScrapeWrapper()
    chain = SimulatedChain(actor)
    out = codec.decode(chain.call(actor.init, codec.encode({"store_registrar": REGISTRAR})))
    assert out["ok"] is True
    config = codec.decode(chain.call(actor.get_config, b""))
    assert config["store_registrar"] == REGISTRAR
    assert config["vendor"] == "firecrawl" and config["call_count"] == 0


def test_capability_advertises_scrape_surface():
    actor = FirecrawlScrapeWrapper()
    chain = SimulatedChain(actor)
    cap = codec.decode(chain.call(actor.capability, b""))
    assert cap["handlers"] == ["scrape", "api"]
    assert cap["slot"] == "scrape"


def test_register_hire_requires_the_registrar():
    actor = FirecrawlScrapeWrapper()
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
