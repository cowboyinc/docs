import json

from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.stream.main import HNStream
from actors.subscriber.main import HN_STREAM_ID, TradingSubscriber

STREAM_ADDR = "0x" + "5e" * 20

HIT = {"objectID": "40001", "title": "OpenAI ships a smaller GPT model", "url": "https://example.com/a", "points": 182, "author": "alice", "num_comments": 97, "created_at": "2026-04-15T09:00:00Z"}


def _signed_batch():
    """Build a pull_batch exactly as the stream actor publishes it, so the
    subscriber's payload-hash and demo-signature checks run against real data."""
    stream = HNStream()
    chain = SimulatedChain(stream)
    chain.call(stream.init, json.dumps({"min_score": 80}).encode())
    chain.call(stream.on_ingest_result, codec.encode({"hits": [HIT]}))
    return codec.decode(chain.call(stream.get_since, codec.encode({"from_sequence": 0})))


def _subscribed_subscriber():
    actor = TradingSubscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(
        chain.call(actor.subscribe_to_hn, codec.encode({"stream_address": STREAM_ADDR, "mode": "PUSH_WITH_PULL_FALLBACK", "min_score": 120, "category": "tech"}))
    )
    assert reply["status"] == "subscription_requested"
    return actor, chain


def test_subscribe_to_hn_sends_the_subscribe_envelope() -> None:
    actor, chain = _subscribed_subscriber()
    sent = mock_host.get_send_log()[-1]
    envelope = codec.decode(sent["payload"])
    assert envelope["type"] == "subscribe"
    assert envelope["mode"] == "PUSH_WITH_PULL_FALLBACK"
    assert {"field": "tags.score", "op": "gte", "value": 120.0} in envelope["filter"]["all"]
    bad = codec.decode(chain.call(actor.subscribe_to_hn, codec.encode({"stream_address": STREAM_ADDR, "mode": "SIDEWAYS"})))
    assert bad == {"error": "INVALID_MODE"}


def test_pull_batch_from_the_stream_generates_a_signal() -> None:
    batch = _signed_batch()
    actor, chain = _subscribed_subscriber()
    reply = codec.decode(chain.call(actor.process_pull_batch, codec.encode(batch)))
    assert reply["status"] == "processed_batch"
    assert reply["articles_processed"] == 1
    assert reply["signals_generated"] == 1
    signals = codec.decode(chain.call(actor.get_signals, codec.encode({"limit": 10})))["signals"]
    assert len(signals) == 1
    assert signals[0]["article_id"] == "40001"
    assert "openai" in signals[0]["matched_keywords"]
    # Replaying the same batch is deduplicated by (stream_id, sequence).
    again = codec.decode(chain.call(actor.process_pull_batch, codec.encode(batch)))
    assert again["articles_processed"] == 0
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["signals_generated"] == 1


def test_tampered_message_is_dropped() -> None:
    batch = _signed_batch()
    batch["messages"][0]["payload_inline_hex"] = json.dumps({"object_id": "40001", "title": "forged", "score": 9999}).encode().hex()
    actor, chain = _subscribed_subscriber()
    reply = codec.decode(chain.call(actor.process_pull_batch, codec.encode(batch)))
    assert reply["articles_processed"] == 0
    assert reply["signals_generated"] == 0


def test_handle_message_rejects_unregistered_streams() -> None:
    actor = TradingSubscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    reply = codec.decode(chain.call(actor.handle_message, codec.encode({"type": "pull_batch", "stream_id": HN_STREAM_ID, "messages": []})))
    assert reply == {"error": "UNAUTHORIZED_STREAM"}
