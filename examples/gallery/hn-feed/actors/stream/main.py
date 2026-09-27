import hashlib
import json

from cowboy_sdk import OWNER, SELF, Address, actor, callable_by, codec, deferred, public, pure, runtime


STREAM_ID = "0x484e5f53545245414d00000000000000000000000000000000000000000000"  # "HN_STREAM" zero-padded
UNSUBSCRIBE_SIG_DOMAIN = "cowboy.examples.17-hn-feed.unsubscribe.v1"
MAX_INLINE_PAYLOAD_BYTES = 16_384
MAX_RING_BUFFER_MESSAGES = 128
MAX_EVENT_LOG_ENTRIES = 64
MAX_SEEN_OBJECT_IDS = 512
MAX_GET_SINCE_LIMIT = 100
MAX_FILTER_DEPTH = 4
MAX_FILTER_PREDICATES = 16
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_HALF_N = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0  # SECP256K1_N // 2
SECP256K1_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

TECH_KEYWORDS = (
    "ai",
    "gpt",
    "llm",
    "python",
    "rust",
    "javascript",
    "cloud",
    "crypto",
    "nvidia",
    "startup",
    "openai",
    "anthropic",
)




def _canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _hex_bytes(value, byte_len):
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != byte_len * 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _mod_inv(value, modulus):
    return pow(value % modulus, modulus - 2, modulus)


def _point_neg(point):
    if point is None:
        return None
    return (point[0], (-point[1]) % SECP256K1_P)


def _point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None

    if left == right:
        if y1 == 0:
            return None
        slope = (3 * x1 * x1) * _mod_inv(2 * y1, SECP256K1_P)
    else:
        slope = (y2 - y1) * _mod_inv(x2 - x1, SECP256K1_P)
    slope %= SECP256K1_P

    x3 = (slope * slope - x1 - x2) % SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % SECP256K1_P
    return (x3, y3)


def _point_mul(scalar, point):
    scalar %= SECP256K1_N
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _is_on_curve(point):
    if point is None:
        return False
    x, y = point
    return (y * y - x * x * x - 7) % SECP256K1_P == 0


def _recover_secp256k1_address(digest, signature):
    raw = _hex_bytes(signature, 65)
    if raw is None or len(digest) != 32:
        return ""

    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    recovery_id = raw[64]
    if recovery_id in (27, 28):
        recovery_id -= 27
    if recovery_id not in (0, 1):
        return ""
    if not (1 <= r < SECP256K1_N and 1 <= s <= SECP256K1_HALF_N):
        return ""

    x = r
    if x >= SECP256K1_P:
        return ""
    y2 = (pow(x, 3, SECP256K1_P) + 7) % SECP256K1_P
    y = pow(y2, (SECP256K1_P + 1) // 4, SECP256K1_P)
    if (y * y) % SECP256K1_P != y2:
        return ""
    if (y & 1) != recovery_id:
        y = SECP256K1_P - y
    r_point = (x, y)
    if not _is_on_curve(r_point):
        return ""

    msg = int.from_bytes(digest, "big") % SECP256K1_N
    q_point = _point_mul(
        _mod_inv(r, SECP256K1_N),
        _point_add(_point_mul(s, r_point), _point_neg(_point_mul(msg, SECP256K1_G))),
    )
    if not _is_on_curve(q_point):
        return ""

    pubkey = q_point[0].to_bytes(32, "big") + q_point[1].to_bytes(32, "big")
    return Address(runtime.keccak256(pubkey)[-20:]).to_hex(checksum=False)


@actor
class HNStream:
    def _state(self):
        if self.storage.get("owner") is None:
            owner_bytes = runtime.get_owner()
            self.storage["owner"] = Address(owner_bytes).to_hex(checksum=False) if owner_bytes else ""
        defaults = {
            "min_score": 50,
            "head_sequence": 0,
            "messages": [],
            "subscriptions": {},
            "seen_object_ids": [],
            "articles_ingested": 0,
            "push_failures": 0,
            "event_log": [],
            "current_signing_key_id": 1,
            "unsubscribe_nonces": {},
        }
        for key, default in defaults.items():
            if self.storage.get(key) is None:
                self.storage[key] = default

    def _emit_event(self, name, payload):
        self._state()
        events = list(self.storage.get("event_log") or [])
        events.append({"event": name, "payload": payload})
        self.storage["event_log"] = events[-MAX_EVENT_LOG_ENTRIES:]
        runtime.emit_event(name, payload)

    def _classify_category(self, title):
        lowered = str(title or "").lower()
        for keyword in TECH_KEYWORDS:
            if keyword in lowered:
                return "tech"
        return "general"

    def _normalize_filter(self, value):
        if not isinstance(value, dict) or not value:
            return None

        if "all" in value or "any" in value or "not" in value:
            return value

        category = value.get("category")
        min_score = float(value.get("min_score", 0) or 0)
        nodes = [{"field": "kind", "op": "eq", "value": "news"}]
        if category:
            nodes.append({"field": "tags.category", "op": "eq", "value": category})
        nodes.append({"field": "tags.score", "op": "gte", "value": min_score})
        return {"all": nodes}

    def _validate_filter(self, filt):
        if filt is None:
            return True, None

        predicate_count = [0]

        def walk(node, depth):
            if depth > MAX_FILTER_DEPTH:
                return False, "FILTER_TOO_DEEP"
            if not isinstance(node, dict) or not node:
                return False, "INVALID_FILTER_NODE"
            if "all" in node or "any" in node:
                key = "all" if "all" in node else "any"
                children = node.get(key)
                if not isinstance(children, list) or not children:
                    return False, "INVALID_FILTER_GROUP"
                for child in children:
                    ok, error = walk(child, depth + 1)
                    if not ok:
                        return False, error
                return True, None
            if "not" in node:
                return walk(node.get("not"), depth + 1)

            field = node.get("field")
            op = node.get("op")
            if not isinstance(field, str) or not isinstance(op, str):
                return False, "INVALID_FILTER_PREDICATE"
            predicate_count[0] += 1
            if predicate_count[0] > MAX_FILTER_PREDICATES:
                return False, "TOO_MANY_PREDICATES"
            return True, None

        return walk(filt, 1)

    def _resolve_message_field(self, message, field):
        if field == "kind":
            return message.get("kind")
        if field == "sequence":
            return message.get("sequence")
        if field == "timestamp_unix_ms":
            return message.get("timestamp_unix_ms")
        if field.startswith("tags."):
            tags = message.get("tags") or {}
            return tags.get(field.split(".", 1)[1])
        return None

    def _evaluate_filter(self, message, filt):
        if not filt:
            return True

        def eval_node(node):
            if "all" in node:
                return all(eval_node(child) for child in node.get("all", []))
            if "any" in node:
                return any(eval_node(child) for child in node.get("any", []))
            if "not" in node:
                return not eval_node(node.get("not") or {})

            actual = self._resolve_message_field(message, node.get("field", ""))
            op = node.get("op")
            expected = node.get("value")

            if op == "exists":
                return actual is not None
            if op == "eq":
                return actual == expected
            if op == "neq":
                return actual != expected
            if op == "gte":
                return actual is not None and actual >= expected
            if op == "gt":
                return actual is not None and actual > expected
            if op == "lte":
                return actual is not None and actual <= expected
            if op == "lt":
                return actual is not None and actual < expected
            if op == "in":
                return isinstance(expected, list) and actual in expected
            return False

        return eval_node(filt)

    def _active_subscriber_count(self):
        count = 0
        for sub in dict(self.storage.get("subscriptions") or {}).values():
            if sub.get("status") == "ACTIVE":
                count += 1
        return count

    def _floor_sequence(self):
        messages = list(self.storage.get("messages") or [])
        if messages:
            return int(messages[0].get("sequence", 1) or 1)
        head = int(self.storage.get("head_sequence", 0) or 0)
        return 1 if head == 0 else head

    def _remember_object_id(self, object_id):
        seen = list(self.storage.get("seen_object_ids") or [])
        if object_id in seen:
            return False
        seen.append(object_id)
        self.storage["seen_object_ids"] = seen[-MAX_SEEN_OBJECT_IDS:]
        return True

    def _build_message(self, article, sequence, score, category):
        payload_bytes = _canonical_json_bytes(article)
        if len(payload_bytes) > MAX_INLINE_PAYLOAD_BYTES:
            return None

        timestamp = int(runtime.get_block_height()) * 1000
        tags = {
            "source": "hn",
            "score": float(score),
            "category": category,
        }
        payload_hash = _sha256_hex(payload_bytes)
        signing_payload = {
            "stream_id": STREAM_ID,
            "version": 1,
            "sequence": sequence,
            "timestamp_unix_ms": timestamp,
            "kind": "news",
            "content_type": "application/json",
            "tags": tags,
            "payload_format": "PLAINTEXT",
            "payload_hash": payload_hash,
            "key_epoch": None,
            "signing_key_id": int(self.storage.get("current_signing_key_id", 1) or 1),
        }
        return {
            **signing_payload,
            "payload_inline_hex": payload_bytes.hex(),
            "publisher_sig": _sha256_hex(_canonical_json_bytes(signing_payload)),
        }

    def _message_batch(self, from_sequence, limit):
        messages = []
        for message in list(self.storage.get("messages") or []):
            sequence = int(message.get("sequence", 0) or 0)
            if sequence <= from_sequence:
                continue
            messages.append(message)
            if len(messages) >= limit:
                break
        return messages

    def _send_push_update(self, message):
        subscriptions = dict(self.storage.get("subscriptions") or {})
        for subscriber, sub in subscriptions.items():
            if sub.get("status") != "ACTIVE":
                continue
            mode = str(sub.get("mode", "PUSH_WITH_PULL_FALLBACK"))
            if mode not in ("PUSH", "PUSH_WITH_PULL_FALLBACK"):
                continue
            if not self._evaluate_filter(message, sub.get("filter")):
                continue
            try:
                runtime.send_message(
                    subscriber,
                    codec.encode(
                        {
                            "type": "stream_update",
                            "stream_id": STREAM_ID,
                            "latest_sequence": int(message.get("sequence", 0) or 0),
                            "messages": [message],
                        }
                    ),
                )
            except Exception:
                self.storage["push_failures"] = int(self.storage.get("push_failures", 0) or 0) + 1
                self._emit_event(
                    "hn_stream.push_failed",
                    {"subscriber": subscriber, "sequence": int(message.get("sequence", 0) or 0)},
                )

    def _subscribe(self, subscriber, payload, allow_update=True):
        self._state()
        if not subscriber:
            return {"error": "MISSING_SUBSCRIBER"}

        mode = str(payload.get("mode", "PUSH_WITH_PULL_FALLBACK"))
        if mode not in ("PUSH", "PULL", "PUSH_WITH_PULL_FALLBACK"):
            return {"error": "INVALID_MODE"}

        filt = self._normalize_filter(payload.get("filter"))
        ok, error = self._validate_filter(filt)
        if not ok:
            return {"error": error}

        subscriptions = dict(self.storage.get("subscriptions") or {})
        existing = subscriptions.get(subscriber)
        if existing and not allow_update:
            return {
                    "status": "already_subscribed",
                    "subscriber": subscriber,
                    "mode": existing.get("mode"),
                    "filter": existing.get("filter"),
                    "unsubscribe_signer": existing.get("unsubscribe_signer") or subscriber,
                }

        unsubscribe_signer = Address(payload.get("unsubscribe_signer") or subscriber).to_hex(checksum=False)
        subscriptions[subscriber] = {
            "mode": mode,
            "status": "ACTIVE",
            "filter": filt,
            "start_cursor": int(payload.get("start_cursor", 0) or 0),
            "unsubscribe_signer": unsubscribe_signer,
        }
        self.storage["subscriptions"] = subscriptions
        self._emit_event(
            "hn_stream.subscribed",
            {"subscriber": subscriber, "mode": mode, "unsubscribe_signer": unsubscribe_signer},
        )
        return {
                "status": "subscribed",
                "subscriber": subscriber,
                "mode": mode,
                "filter": filt,
                "unsubscribe_signer": unsubscribe_signer,
            }

    def _unsubscribe_digest(self, subscriber, nonce):
        payload = {
            "domain": UNSUBSCRIBE_SIG_DOMAIN,
            "stream_actor": self.address.to_hex(checksum=False),
            "stream_id": STREAM_ID,
            "subscriber": subscriber,
            "nonce": int(nonce),
        }
        return runtime.keccak256(_canonical_json_bytes(payload))

    def _verify_unsubscribe_signature(self, subscriber, nonce, signature, signer):
        recovered = _recover_secp256k1_address(self._unsubscribe_digest(subscriber, nonce), signature)
        return bool(recovered) and recovered == signer

    def _handle_signed_unsubscribe(self, payload):
        raw_subscriber = payload.get("subscriber")
        subscriber = Address(raw_subscriber).to_hex(checksum=False) if raw_subscriber else ""
        if not subscriber:
            return {"error": "MISSING_SUBSCRIBER"}
        try:
            nonce = int(payload.get("nonce", 0) or 0)
        except Exception:
            return {"error": "INVALID_UNSUBSCRIBE_NONCE"}
        if nonce <= 0:
            return {"error": "INVALID_UNSUBSCRIBE_NONCE"}

        subscriptions = dict(self.storage.get("subscriptions") or {})
        subscription = subscriptions.get(subscriber)
        if not isinstance(subscription, dict):
            return {"error": "UNAUTHORIZED_SUBSCRIBER"}

        nonces = dict(self.storage.get("unsubscribe_nonces") or {})
        if nonce <= int(nonces.get(subscriber, 0) or 0):
            return {"error": "REPLAYED_UNSUBSCRIBE"}

        signer = subscription.get("unsubscribe_signer") or subscriber
        if not self._verify_unsubscribe_signature(subscriber, nonce, payload.get("unsubscribe_sig"), signer):
            return {"error": "INVALID_UNSUBSCRIBE_SIGNATURE"}

        nonces[subscriber] = nonce
        self.storage["unsubscribe_nonces"] = nonces
        if subscription.get("status") == "ACTIVE":
            subscriptions[subscriber]["status"] = "CANCELLED"
            self.storage["subscriptions"] = subscriptions
        self._emit_event(
            "hn_stream.unsubscribed",
            {"subscriber": subscriber, "nonce": nonce, "unsubscribe_signer": signer},
        )
        return {"status": "unsubscribed", "subscriber": subscriber, "nonce": nonce}

    def _publish_hits(self, hits):
        self._state()
        owner_bytes = runtime.get_owner()
        owner = Address(owner_bytes).to_hex(checksum=False) if owner_bytes else ""
        if not owner or self.sender.to_hex(checksum=False) != owner:
            return {"error": "UNAUTHORIZED"}
        if not isinstance(hits, list):
            return {"error": "INVALID_INGEST_RESULT"}

        head = int(self.storage.get("head_sequence", 0) or 0)
        messages = list(self.storage.get("messages") or [])
        published = 0
        min_score = int(self.storage.get("min_score", 50) or 50)

        for hit in hits:
            object_id = str(hit.get("objectID", ""))
            score = int(hit.get("points", 0) or 0)
            if not object_id or score < min_score:
                continue
            if not self._remember_object_id(object_id):
                continue

            title = str(hit.get("title", ""))
            article = {
                "object_id": object_id,
                "title": title,
                "url": str(hit.get("url", "")),
                "score": score,
                "author": str(hit.get("author", "")),
                "num_comments": int(hit.get("num_comments", 0) or 0),
                "created_at": str(hit.get("created_at", "")),
            }
            category = self._classify_category(title)
            head += 1
            message = self._build_message(article, head, score, category)
            if not message:
                continue

            messages.append(message)
            messages = messages[-MAX_RING_BUFFER_MESSAGES:]
            self._send_push_update(message)
            self._emit_event(
                "hn_stream.published",
                {
                    "sequence": head,
                    "title": title,
                    "category": category,
                    "score": score,
                },
            )
            published += 1

        self.storage["head_sequence"] = head
        self.storage["messages"] = messages
        self.storage["articles_ingested"] = int(self.storage.get("articles_ingested", 0) or 0) + published
        return {
                "status": "ingested",
                "hits_seen": len(hits),
                "articles_published": published,
                "latest_sequence": head,
            }

    def init(self, payload):
        owner = runtime.assume_ownership_if_unowned()
        data = json.loads(payload) if payload else {}
        self.storage["owner"] = Address(owner).to_hex(checksum=False) if owner else ""
        self.storage["min_score"] = int(data.get("min_score", 50) or 50)
        self.storage["head_sequence"] = 0
        self.storage["messages"] = []
        self.storage["subscriptions"] = {}
        self.storage["seen_object_ids"] = []
        self.storage["articles_ingested"] = 0
        self.storage["push_failures"] = 0
        self.storage["event_log"] = []
        self.storage["current_signing_key_id"] = 1
        self.storage["unsubscribe_nonces"] = {}
        result = {"status": "initialized", "stream_id": STREAM_ID}
        self._emit_event("hn_stream.init", result)
        return codec.encode(result)

    @public(decode="cbor")
    def subscribe(self, payload):
        data = (payload or {})
        sender = self.sender.to_hex(checksum=False)
        raw_subscriber = data.get("subscriber")
        declared_subscriber = Address(raw_subscriber).to_hex(checksum=False) if raw_subscriber else ""
        if declared_subscriber and declared_subscriber != sender:
            return {"error": "SUBSCRIBER_MISMATCH"}
        return self._subscribe(sender, data)

    @public(decode="cbor")
    def unsubscribe(self, payload):
        self._state()
        sender = self.sender.to_hex(checksum=False)
        raw_subscriber = (payload or {}).get("subscriber")
        declared_subscriber = Address(raw_subscriber).to_hex(checksum=False) if raw_subscriber else ""
        if declared_subscriber and declared_subscriber != sender:
            return {"error": "SUBSCRIBER_MISMATCH"}

        subscriptions = dict(self.storage.get("subscriptions") or {})
        if sender in subscriptions:
            subscriptions[sender]["status"] = "CANCELLED"
            self.storage["subscriptions"] = subscriptions
        self._emit_event("hn_stream.unsubscribed", {"subscriber": sender})
        return {"status": "unsubscribed", "subscriber": sender}

    @public(decode="cbor")
    @deferred
    def handle_message(self, payload):
        self._state()
        data = (payload or {})
        message_type = str(data.get("type", ""))
        sender = self.sender.to_hex(checksum=False)

        if message_type == "subscribe":
            subscriber = Address(data.get("subscriber") or sender).to_hex(checksum=False)
            return self._subscribe(subscriber, data, allow_update=False)
        if message_type == "unsubscribe":
            return self._handle_signed_unsubscribe(data)
        if message_type == "get_since":
            subscriber = Address(data.get("subscriber") or data.get("reply_to") or sender).to_hex(checksum=False)
            if not subscriber:
                return {"error": "MISSING_SUBSCRIBER"}

            subscriptions = dict(self.storage.get("subscriptions") or {})
            subscription = subscriptions.get(subscriber) or {}
            if subscription.get("status") != "ACTIVE":
                return {"error": "UNAUTHORIZED_SUBSCRIBER"}

            reply_to = Address(data.get("reply_to") or subscriber).to_hex(checksum=False)
            if reply_to != subscriber:
                return {"error": "REPLY_TO_MISMATCH"}

            from_sequence = int(data.get("from_sequence", 0) or 0)
            limit = int(data.get("limit", MAX_GET_SINCE_LIMIT) or MAX_GET_SINCE_LIMIT)
            limit = max(1, min(MAX_GET_SINCE_LIMIT, limit))
            batch = self._message_batch(from_sequence, limit)
            response = {
                "type": "pull_batch",
                "stream_id": STREAM_ID,
                "from_sequence": from_sequence,
                "latest_sequence": int(self.storage.get("head_sequence", 0) or 0),
                "messages": batch,
            }
            runtime.send_message(reply_to, codec.encode(response))
            return {"status": "batch_sent", "from_sequence": from_sequence, "count": len(batch)}
        return {"error": "UNKNOWN_MESSAGE_TYPE"}

    @callable_by(OWNER, decode="cbor")
    @deferred
    def on_ingest_result(self, payload):
        return self._publish_hits((payload or {}).get("hits"))

    @callable_by(OWNER, decode="cbor")
    @deferred
    def publish(self, payload):
        data = (payload or {})
        if "hits" in data:
            return self._publish_hits(data.get("hits"))
        return self._publish_hits([data])

    @callable_by(SELF, decode="raw")
    def on_timer(self, payload):
        data = json.loads(payload) if payload else {}
        timer = str(data.get("tag") or data.get("timer") or "ingest")
        return codec.encode({"status": "timer_ready", "timer": timer})

    @public(decode="cbor")
    @pure
    def get_since(self, payload):
        self._state()
        data = (payload or {})
        from_sequence = int(data.get("from_sequence", 0) or 0)
        limit = int(data.get("limit", MAX_GET_SINCE_LIMIT) or MAX_GET_SINCE_LIMIT)
        limit = max(1, min(MAX_GET_SINCE_LIMIT, limit))
        messages = self._message_batch(from_sequence, limit)
        result = {
            "stream_id": STREAM_ID,
            "from_sequence": from_sequence,
            "latest_sequence": int(self.storage.get("head_sequence", 0) or 0),
            "messages": messages,
        }
        runtime.emit_event("hn_stream.since", result)
        return result

    @public(decode="cbor")
    @pure
    def get_head(self, payload):
        self._state()
        result = {
            "stream_id": STREAM_ID,
            "head_sequence": int(self.storage.get("head_sequence", 0) or 0),
        }
        runtime.emit_event("hn_stream.head", result)
        return result

    @public(decode="cbor")
    @pure
    def get_hn_stats(self, payload):
        self._state()
        result = {
            "stream_id": STREAM_ID,
            "head_sequence": int(self.storage.get("head_sequence", 0) or 0),
            "floor_sequence": self._floor_sequence(),
            "subscriber_count": self._active_subscriber_count(),
            "articles_ingested": int(self.storage.get("articles_ingested", 0) or 0),
            "min_score": int(self.storage.get("min_score", 50) or 50),
            "push_failures": int(self.storage.get("push_failures", 0) or 0),
            "recent_event_count": len(list(self.storage.get("event_log") or [])),
        }
        runtime.emit_event("hn_stream.stats", result)
        return result

    @public(decode="cbor")
    @pure
    def get_recent_events(self, payload):
        self._state()
        limit = int((payload or {}).get("limit", 10) or 10)
        limit = max(1, limit)
        result = {"events": list(self.storage.get("event_log") or [])[-limit:]}
        runtime.emit_event("hn_stream.recent_events", result)
        return result


def create_hn_stream():
    return HNStream()


def _get_actor():
    return HNStream()


def init(payload):
    return _get_actor().init(payload)


def subscribe(payload):
    return _get_actor().subscribe(payload)


def unsubscribe(payload):
    return _get_actor().unsubscribe(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def on_ingest_result(payload):
    return _get_actor().on_ingest_result(payload)


def publish(payload):
    return _get_actor().publish(payload)


def on_timer(payload):
    return _get_actor().on_timer(payload)


def get_since(payload):
    return _get_actor().get_since(payload)


def get_head(payload):
    return _get_actor().get_head(payload)


def get_hn_stats(payload):
    return _get_actor().get_hn_stats(payload)


def get_recent_events(payload):
    return _get_actor().get_recent_events(payload)
