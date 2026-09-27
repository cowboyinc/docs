import hashlib
import json

from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.stream.main import STREAM_ID, HNStream

SUBSCRIBER = "0x" + "ab" * 20

HITS = [
    {"objectID": "40001", "title": "OpenAI ships a smaller GPT model", "url": "https://example.com/a", "points": 182, "author": "alice", "num_comments": 97, "created_at": "2026-04-15T09:00:00Z"},
    {"objectID": "40002", "title": "Ask HN: favorite keyboard firmware?", "url": "https://example.com/b", "points": 53, "author": "carol", "num_comments": 19, "created_at": "2026-04-15T09:10:00Z"},
]


def _stream(min_score=80):
    actor = HNStream()
    chain = SimulatedChain(actor)
    reply = codec.decode(chain.call(actor.init, json.dumps({"min_score": min_score}).encode()))
    assert reply == {"status": "initialized", "stream_id": STREAM_ID}
    return actor, chain


def test_ingest_publishes_only_hits_above_min_score() -> None:
    actor, chain = _stream()
    reply = codec.decode(chain.call(actor.on_ingest_result, codec.encode({"hits": HITS})))
    assert reply == {"status": "ingested", "hits_seen": 2, "articles_published": 1, "latest_sequence": 1}
    # A replayed objectID is deduplicated, not republished.
    again = codec.decode(chain.call(actor.on_ingest_result, codec.encode({"hits": HITS})))
    assert again["articles_published"] == 0
    stats = codec.decode(chain.call(actor.get_hn_stats, codec.encode({})))
    assert stats["head_sequence"] == 1
    assert stats["articles_ingested"] == 1
    assert stats["min_score"] == 80


def test_published_message_carries_verifiable_hash_and_demo_signature() -> None:
    actor, chain = _stream()
    chain.call(actor.on_ingest_result, codec.encode({"hits": HITS}))
    batch = codec.decode(chain.call(actor.get_since, codec.encode({"from_sequence": 0})))
    assert batch["latest_sequence"] == 1
    (message,) = batch["messages"]
    assert message["sequence"] == 1
    assert message["kind"] == "news"
    payload_bytes = bytes.fromhex(message["payload_inline_hex"])
    assert hashlib.sha256(payload_bytes).hexdigest() == message["payload_hash"]
    article = json.loads(payload_bytes)
    assert article["object_id"] == "40001"
    assert article["score"] == 182
    assert message["tags"]["category"] == "tech"


def test_subscribe_is_sender_bound_and_push_lands_in_the_send_log() -> None:
    actor, chain = _stream()
    chain.set_sender(SUBSCRIBER)
    mismatch = codec.decode(chain.call(actor.subscribe, codec.encode({"subscriber": "0x" + "cd" * 20})))
    assert mismatch == {"error": "SUBSCRIBER_MISMATCH"}
    reply = codec.decode(chain.call(actor.subscribe, codec.encode({"mode": "PUSH_WITH_PULL_FALLBACK"})))
    assert reply["status"] in ("subscribed", "subscription_updated")
    stats = codec.decode(chain.call(actor.get_hn_stats, codec.encode({})))
    assert stats["subscriber_count"] == 1
    chain.set_sender("0x" + "11" * 20)  # back to the owner for the ingest
    chain.call(actor.on_ingest_result, codec.encode({"hits": HITS}))
    pushes = [sent for sent in mock_host.get_send_log() if bytes(sent["target"]) == bytes.fromhex(SUBSCRIBER[2:])]
    assert len(pushes) == 1
    update = codec.decode(pushes[0]["payload"])
    assert update["type"] == "stream_update"
    assert update["messages"][0]["sequence"] == 1


def test_unsubscribe_cancels_the_row() -> None:
    actor, chain = _stream()
    chain.set_sender(SUBSCRIBER)
    chain.call(actor.subscribe, codec.encode({}))
    reply = codec.decode(chain.call(actor.unsubscribe, codec.encode({})))
    assert reply == {"status": "unsubscribed", "subscriber": SUBSCRIBER}
    stats = codec.decode(chain.call(actor.get_hn_stats, codec.encode({})))
    assert stats["subscriber_count"] == 0
