import hashlib
import json

from cowboy_sdk import OWNER, Address, actor, callable_by, codec, deferred, public, pure, runtime


MAX_RECENT_SIGNALS = 32
MAX_SEEN_SEQUENCES = 256
HN_STREAM_ID = "0x484e5f53545245414d00000000000000000000000000000000000000000000"  # "HN_STREAM" zero-padded
KEYWORDS = (
    "ai",
    "llm",
    "gpt",
    "openai",
    "anthropic",
    "crypto",
    "bitcoin",
    "ethereum",
    "ipo",
    "acquisition",
    "funding",
    "nvidia",
)




def _canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


@actor
class TradingSubscriber:
    def _state(self):
        if self.storage.get("owner") is None:
            owner_bytes = runtime.get_owner()
            self.storage["owner"] = Address(owner_bytes).to_hex(checksum=False) if owner_bytes else ""
        defaults = {
            "subscriptions": {},
            "stream_cursors": {},
            "processed_sequences": {},
            "articles_processed": 0,
            "signals_generated": 0,
            "recent_signals": [],
        }
        for key, default in defaults.items():
            if self.storage.get(key) is None:
                self.storage[key] = default

    def _build_filter_dsl(self, category, min_score):
        return {
            "all": [
                {"field": "kind", "op": "eq", "value": "news"},
                {"field": "tags.category", "op": "eq", "value": category},
                {"field": "tags.score", "op": "gte", "value": float(min_score)},
            ]
        }

    def _verify_payload_hash(self, payload_bytes, message):
        expected = str(message.get("payload_hash", ""))
        return bool(expected) and _sha256_hex(payload_bytes) == expected

    def _verify_demo_signature(self, message):
        signing_payload = {
            "stream_id": message.get("stream_id"),
            "version": message.get("version", 1),
            "sequence": message.get("sequence"),
            "timestamp_unix_ms": message.get("timestamp_unix_ms"),
            "kind": message.get("kind"),
            "content_type": message.get("content_type", "application/json"),
            "tags": message.get("tags", {}),
            "payload_format": message.get("payload_format"),
            "payload_hash": message.get("payload_hash"),
            "key_epoch": message.get("key_epoch"),
            "signing_key_id": message.get("signing_key_id"),
        }
        expected_sig = _sha256_hex(_canonical_json_bytes(signing_payload))
        return expected_sig == str(message.get("publisher_sig", ""))

    def _decode_article(self, message):
        if str(message.get("payload_format", "")) != "PLAINTEXT":
            return None
        if str(message.get("content_type", "")) != "application/json":
            return None

        payload_hex = str(message.get("payload_inline_hex", ""))
        if not payload_hex:
            return None

        try:
            payload_bytes = bytes.fromhex(payload_hex)
        except ValueError:
            return None

        if not self._verify_payload_hash(payload_bytes, message):
            return None
        if not self._verify_demo_signature(message):
            return None

        try:
            article = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None
        return article if isinstance(article, dict) else None

    def _signal_type(self, title):
        lowered = str(title or "").lower()
        if any(keyword in lowered for keyword in ("ipo", "acquisition", "funding")):
            return "corporate_event"
        if any(keyword in lowered for keyword in ("crypto", "bitcoin", "ethereum")):
            return "crypto"
        return "tech_trend"

    def _make_signal(self, article, message):
        title = str(article.get("title", ""))
        lowered = title.lower()
        matched = [keyword for keyword in KEYWORDS if keyword in lowered]
        if not matched:
            return None

        score = float(article.get("score", 0) or 0)
        return {
            "stream_id": str(message.get("stream_id", "")),
            "sequence": int(message.get("sequence", 0) or 0),
            "article_id": str(article.get("object_id", "")),
            "title": title,
            "url": str(article.get("url", "")),
            "score": score,
            "matched_keywords": matched,
            "signal_type": self._signal_type(title),
            "signal_strength": min(1.0, (score / 100.0) * len(matched)),
            "block_height": int(runtime.get_block_height()),
        }

    def _seen_sequence(self, stream_id, sequence):
        seen = dict(self.storage.get("processed_sequences") or {})
        existing = list(seen.get(stream_id) or [])
        if sequence in existing:
            return True
        existing.append(sequence)
        seen[stream_id] = existing[-MAX_SEEN_SEQUENCES:]
        self.storage["processed_sequences"] = seen
        return False

    def _has_active_stream_subscription(self, stream_id):
        self._state()
        actual_stream_id = str(stream_id or "").strip().lower()
        if not actual_stream_id:
            return False

        for subscription in dict(self.storage.get("subscriptions") or {}).values():
            if not isinstance(subscription, dict) or subscription.get("status") != "ACTIVE":
                continue
            expected_stream_id = str(subscription.get("stream_id") or "").strip().lower()
            if expected_stream_id == actual_stream_id:
                return True
        return False

    def _process_batch(self, payload):
        self._state()
        stream_id = str(payload.get("stream_id", ""))
        cursor_map = dict(self.storage.get("stream_cursors") or {})
        current_cursor = int(cursor_map.get(stream_id, 0) or 0)
        latest_cursor = current_cursor
        processed = 0
        signals = []

        for message in payload.get("messages", []):
            sequence = int(message.get("sequence", 0) or 0)
            if sequence <= current_cursor or self._seen_sequence(stream_id, sequence):
                continue

            article = self._decode_article(message)
            if article is None:
                continue

            processed += 1
            if sequence > latest_cursor:
                latest_cursor = sequence

            signal = self._make_signal(article, message)
            if signal is not None:
                signals.append(signal)

        self.storage["articles_processed"] = int(self.storage.get("articles_processed", 0) or 0) + processed
        self.storage["signals_generated"] = int(self.storage.get("signals_generated", 0) or 0) + len(signals)
        cursor_map[stream_id] = latest_cursor
        self.storage["stream_cursors"] = cursor_map

        recent = list(self.storage.get("recent_signals") or [])
        recent.extend(signals)
        self.storage["recent_signals"] = recent[-MAX_RECENT_SIGNALS:]

        return {
                "status": "processed_batch",
                "stream_id": stream_id,
                "articles_processed": processed,
                "signals_generated": len(signals),
                "latest_cursor": latest_cursor,
            }

    def init(self, payload):
        owner = runtime.assume_ownership_if_unowned()
        self.storage["owner"] = Address(owner).to_hex(checksum=False) if owner else ""
        self.storage["subscriptions"] = {}
        self.storage["stream_cursors"] = {}
        self.storage["processed_sequences"] = {}
        self.storage["articles_processed"] = 0
        self.storage["signals_generated"] = 0
        self.storage["recent_signals"] = []
        return codec.encode({"status": "initialized"})

    @callable_by(OWNER, decode="cbor")
    @deferred
    def subscribe_to_hn(self, payload):
        self._state()
        data = (payload or {})
        raw_stream_address = data.get("stream_address", "")
        stream_address = Address(raw_stream_address).to_hex(checksum=False) if raw_stream_address else ""
        if not stream_address:
            return {"error": "MISSING_STREAM"}

        mode = str(data.get("mode", "PUSH_WITH_PULL_FALLBACK"))
        if mode not in ("PUSH", "PULL", "PUSH_WITH_PULL_FALLBACK"):
            return {"error": "INVALID_MODE"}

        category = str(data.get("category", "tech"))
        min_score = int(data.get("min_score", 100) or 100)
        stream_id = str(data.get("stream_id") or HN_STREAM_ID).strip().lower()
        raw_signer = data.get("unsubscribe_signer") or self.storage.get("owner")
        unsubscribe_signer = Address(raw_signer).to_hex(checksum=False) if raw_signer else ""
        filter_dsl = self._build_filter_dsl(category, min_score)
        subscriptions = dict(self.storage.get("subscriptions") or {})
        subscriptions[stream_address] = {
            "mode": mode,
            "status": "ACTIVE",
            "filter": filter_dsl,
            "stream_id": stream_id,
            "start_cursor": int(data.get("start_cursor", 0) or 0),
            "unsubscribe_signer": unsubscribe_signer,
        }
        self.storage["subscriptions"] = subscriptions

        envelope = {
            "type": "subscribe",
            "subscriber": self.address.to_hex(checksum=False),
            "unsubscribe_signer": unsubscribe_signer,
            "mode": mode,
            "filter": filter_dsl,
        }
        if data.get("start_cursor") is not None:
            envelope["start_cursor"] = int(data.get("start_cursor", 0) or 0)

        runtime.send_message(stream_address, codec.encode(envelope))
        return {
                "status": "subscription_requested",
                "stream": stream_address,
                "mode": mode,
                "filter": filter_dsl,
            }

    @public(decode="cbor")
    @deferred
    def handle_message(self, payload):
        data = (payload or {})
        if str(data.get("type", "")) in ("stream_update", "pull_batch"):
            if not self._has_active_stream_subscription(data.get("stream_id")):
                return {"error": "UNAUTHORIZED_STREAM"}
            return self._process_batch(data)
        return {"error": "UNKNOWN_MESSAGE_TYPE"}

    @callable_by(OWNER, decode="cbor")
    @deferred
    def process_pull_batch(self, payload):
        data = (payload or {})
        if str(data.get("type", "pull_batch")) != "pull_batch":
            return {"error": "INVALID_MESSAGE_TYPE"}
        data["type"] = "pull_batch"
        return self._process_batch(data)

    @public(decode="cbor")
    @pure
    def get_signals(self, payload):
        self._state()
        limit = int((payload or {}).get("limit", 10) or 10)
        signal_type = (payload or {}).get("signal_type")
        signals = list(self.storage.get("recent_signals") or [])
        if signal_type:
            signals = [signal for signal in signals if signal.get("signal_type") == signal_type]
        result = {
            "signals": signals[-limit:],
            "total_signals": int(self.storage.get("signals_generated", 0) or 0),
        }
        runtime.emit_event("trading_subscriber.signals", result)
        return result

    @public(decode="cbor")
    @pure
    def get_stats(self, payload):
        self._state()
        result = {
            "articles_processed": int(self.storage.get("articles_processed", 0) or 0),
            "signals_generated": int(self.storage.get("signals_generated", 0) or 0),
            "subscriptions": dict(self.storage.get("subscriptions") or {}),
            "stream_cursors": dict(self.storage.get("stream_cursors") or {}),
        }
        runtime.emit_event("trading_subscriber.stats", result)
        return result


def create_trading_subscriber():
    return TradingSubscriber()


def _get_actor():
    return TradingSubscriber()


def init(payload):
    return _get_actor().init(payload)


def subscribe_to_hn(payload):
    return _get_actor().subscribe_to_hn(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def process_pull_batch(payload):
    return _get_actor().process_pull_batch(payload)


def get_signals(payload):
    return _get_actor().get_signals(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
