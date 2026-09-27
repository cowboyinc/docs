"""SimulatedChain smoke tests for the watcher target (offline, no network)."""

import json

from cowboy_sdk.testing import SimulatedChain

from actors.watcher.main import PriceWatcher


def test_init_is_idempotent_and_defaults_to_stopped():
    actor = PriceWatcher()
    chain = SimulatedChain(actor)
    out = json.loads(chain.call(actor.init, b'{"min_drop_pct": 0}'))
    assert out["status"] == "initialized"
    assert out["cfg"]["min_drop_pct"] == 0
    assert json.loads(chain.call(actor.init, b"{}")) == {"status": "already_initialized"}
    assert chain.state(b"running") is not None  # stored, and autostart defaults off


def test_track_registers_watch_and_redacts_chat_id():
    actor = PriceWatcher()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    payload = {"url": "https://api.example.com/spot", "name": "demo-watch",
               "chat_id": "12345", "items": [{"name": "BTC-USD", "path": "data.amount"}]}
    out = json.loads(chain.call(actor.track, json.dumps(payload).encode()))
    assert out["status"] == "tracked" and out["mode"] == "manual"
    watches = json.loads(chain.call(actor.get_watches, b"{}"))["watches"]
    watch = watches[str(out["watch_id"])]
    assert watch["name"] == "demo-watch"
    assert "chat_id" not in watch and "headers" not in watch


def test_track_requires_url():
    actor = PriceWatcher()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    assert json.loads(chain.call(actor.track, b"{}")) == {"error": "url required"}
