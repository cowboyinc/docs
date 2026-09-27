"""Tests for the `stream` target (module-level CIP-7 StreamActor over pvm_host).

The module binds `pvm_host` at import time, so each test rebinds it to the
fresh SimulatedChain host proxy before calling handlers.
"""

import importlib
import sys

import pytest

from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

OWNER = b"\x11" * 20
SUBSCRIBER = b"\x33" * 20

SimulatedChain()  # provides pvm_host for import time
stream = importlib.import_module("actors.stream.main")


def _chain() -> SimulatedChain:
    chain = SimulatedChain()
    stream.pvm_host = sys.modules["pvm_host"]
    return chain


def test_init_records_owner_and_metadata() -> None:
    chain = _chain()
    reply = chain.call(stream.init, b'{"name": "form4-open", "description": "d"}')
    assert reply == b"ok: stream initialized"
    meta = chain.call(stream.get_metadata, b"")
    assert b'"name":"form4-open"' in meta
    assert chain.call(stream.init, b"{}") == b"error: already initialized"


def test_subscribe_then_publish_pushes_to_subscriber() -> None:
    from cowboy_sdk import mock_host

    chain = _chain()
    chain.call(stream.init, b'{"name": "feed"}')
    chain.set_sender(SUBSCRIBER)
    assert chain.call(stream.subscribe, b"") == b"ok: subscribed"
    chain.set_sender(OWNER)  # owner is the initial publisher
    reply = chain.call(stream.publish, b'{"ticker": "AAPL", "action": "BUY"}')
    assert reply == b"ok: published to 1/1 subscribers"
    sends = mock_host.get_send_log()
    assert len(sends) == 1
    assert b'"type":"stream_data"' in sends[0]["payload"]
    assert b'"ticker":"AAPL"' in chain.call(stream.get_latest, b"")


def test_publish_requires_authorized_publisher() -> None:
    chain = _chain()
    chain.call(stream.init, b'{"name": "feed"}')
    chain.set_sender(b"\x44" * 20)
    assert chain.call(stream.publish, b'{"x": 1}') == b"error: not authorized publisher"


def test_cip7_init_sets_sequence_cursor() -> None:
    chain = _chain()
    reply = chain.call(stream.init, codec.encode({
        "stream_id": b"form4/paid",
        "initial_publisher_key": b"\x07" * 32,
        "access_mode": "SUBSCRIBER_PAID",
        "ring_buffer_capacity": 16,
        "name": "form4-paid",
    }))
    assert reply == b"ok: cip7 stream initialized"
    head = codec.decode(chain.call(stream.get_head, b""))
    assert head == {"head_sequence": 0, "floor_sequence": 1}


@pytest.mark.parametrize("published", [False, True])
def test_getters_preserve_state_and_emit_no_events(published: bool) -> None:
    chain = _chain()
    chain.call(stream.init, b'{"name":"read-test"}')
    chain.set_sender(SUBSCRIBER)
    chain.call(stream.subscribe, b"")
    chain.set_sender(OWNER)
    if published:
        chain.call(stream.publish, b'{"ticker":"AAPL"}')
    before_state = chain.snapshot()
    before_events = chain.events()
    assert before_events  # Write operations still emit events.

    assert b'"name":"read-test"' in chain.call(stream.get_metadata, b"")
    assert SUBSCRIBER.hex().encode() in chain.call(stream.get_subscribers, b"")
    assert b'"total_subscribers":1' in chain.call(stream.get_stats, b"")
    latest = chain.call(stream.get_latest, b"")
    if published:
        assert b'"ticker":"AAPL"' in latest
    else:
        assert latest == b"no data published yet"
    assert chain.snapshot() == before_state
    assert chain.events() == before_events
