"""Tests for the `feed-subscriber` target (module-level handlers over pvm_host).

The module binds `pvm_host` at import time, so each test rebinds it to the
fresh SimulatedChain host proxy before calling handlers.
"""

import importlib
import sys

from cowboy_sdk.testing import SimulatedChain

OWNER = b"\x11" * 20
FEED = b"\xfe" * 20

# Importing the module requires a pvm_host in sys.modules; a throwaway chain
# provides one for import time.
SimulatedChain()
fs = importlib.import_module("actors.feed-subscriber.main")


def _chain() -> SimulatedChain:
    chain = SimulatedChain()
    fs.pvm_host = sys.modules["pvm_host"]
    return chain


def test_init_records_owner() -> None:
    chain = _chain()
    assert chain.call(fs.init, b"") == b"ok: feed subscriber initialized"
    assert chain.state(b"owner") == b"0x" + OWNER.hex().encode()
    assert chain.call(fs.get_messages, b"") == b"[]"


def test_subscribe_is_owner_only_and_sends_subscribe_message() -> None:
    from cowboy_sdk import mock_host

    chain = _chain()
    chain.call(fs.init, b"")
    chain.set_sender(b"\x33" * 20)
    assert chain.call(
        fs.subscribe_to_feed, b'{"feed_address": "' + FEED.hex().encode() + b'"}'
    ) == b"error: only owner can subscribe"
    chain.set_sender(OWNER)
    reply = chain.call(fs.subscribe_to_feed, b'{"feed_address": "' + FEED.hex().encode() + b'"}')
    assert reply == ("ok: subscription sent to 0x" + FEED.hex()).encode()
    sends = mock_host.get_send_log()
    assert len(sends) == 1
    assert b'"method":"subscribe"' in sends[0]["payload"]


def test_handle_message_only_accepts_registered_feed() -> None:
    chain = _chain()
    chain.call(fs.init, b"")
    chain.call(fs.subscribe_to_feed, b'{"feed_address": "' + FEED.hex().encode() + b'"}')
    payload = b'{"type":"stream_data","payload":{"v":1},"publisher":"pub"}'
    chain.set_sender(b"\x44" * 20)
    assert chain.call(fs.handle_message, payload) == b"error: unauthorized sender"
    chain.set_sender(FEED)
    assert chain.call(fs.handle_message, payload) == b"ok: stored message 1"
    stored = chain.call(fs.get_messages, b"")
    assert b'"publisher":"pub"' in stored
