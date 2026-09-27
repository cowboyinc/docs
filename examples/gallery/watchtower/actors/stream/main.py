# Source of truth: cowboy/actors/streaming/stream_actor.py
"""
StreamActor - On-chain data feed as an actor.

A feed IS an actor. Providers publish data, consumers subscribe
to receive automatic push delivery via send_message().

Methods:
  init           - Deploy: set owner, metadata, empty subscribers/publishers
  publish        - Publishers only: push payload to all subscribers
  subscribe      - Any actor: add sender to subscriber list
  unsubscribe    - Any actor: remove sender from subscriber list
  add_publisher  - Owner only: authorize a publisher address
  remove_publisher - Owner only: revoke publisher authorization
  get_metadata   - Anyone: return feed metadata
  get_subscribers - Anyone: return subscriber list
  get_stats      - Anyone: publication/subscriber counts
  get_latest     - Anyone: last published data
  initialize_paid_stream_config - Owner: initialize SKM paid-mode config
  register_content_keys - Owner: register/re-wrap epoch content keys with SKM
  acquire_epoch_access - Subscriber/payer: acquire paid epoch key access
"""

import hashlib

import pvm_host
from cowboy_sdk import Address, codec, runtime

STREAM_KEY_MANAGER_ADDR = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x12"
STREAM_KEY_MANAGER_CALL_CYCLES = 100_000_000
MAX_PAYLOAD_INLINE_BYTES = 16 * 1024
DEFAULT_RING_BUFFER_CAPACITY = 1024

CIP7_STREAM_ID_KEY = b"cip7:stream_id"
CIP7_ACCESS_MODE_KEY = b"cip7:access_mode"
CIP7_HEAD_SEQUENCE_KEY = b"cip7:head_sequence"
CIP7_FLOOR_SEQUENCE_KEY = b"cip7:floor_sequence"
CIP7_RING_BUFFER_CAPACITY_KEY = b"cip7:ring_buffer_capacity"
CIP7_CURRENT_SIGNING_KEY_ID_KEY = b"cip7:current_signing_key_id"
CIP7_KEY_HISTORY_KEY = b"cip7:key_history"

# -- Helpers ------------------------------------------------------------------

def _get_json(key):
    """Load a JSON value from state."""
    val = pvm_host.get_state(key)
    if not val:
        return None
    return _json_loads(val.decode("utf-8"))


def _set_json(key, obj):
    """Store a JSON value to state."""
    pvm_host.set_state(key, _json_dumps(obj).encode("utf-8"))


def _get_str(key, default=""):
    val = pvm_host.get_state(key)
    if not val:
        return default
    return val.decode("utf-8")


def _set_str(key, value):
    pvm_host.set_state(key, value.encode("utf-8"))


def _get_int(key, default=0):
    val = pvm_host.get_state(key)
    if not val or len(val) < 8:
        return default
    return int.from_bytes(val[:8], "big")


def _set_int(key, value):
    pvm_host.set_state(key, int(value).to_bytes(8, "big"))


def _get_cbor(key, default=None):
    val = pvm_host.get_state(key)
    if not val:
        return default
    return codec.decode(val)


def _set_cbor(key, value):
    pvm_host.set_state(key, codec.encode(value))


def _json_dumps(obj):
    """Minimal JSON serializer for dicts, lists, strings, ints, bools, None."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        escaped = obj.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("\0", "\\u0000")
        return '"' + escaped + '"'
    if isinstance(obj, list):
        return "[" + ",".join(_json_dumps(v) for v in obj) + "]"
    if isinstance(obj, dict):
        pairs = []
        for k, v in obj.items():
            pairs.append(_json_dumps(str(k)) + ":" + _json_dumps(v))
        return "{" + ",".join(pairs) + "}"
    return '"' + str(obj) + '"'


def _json_loads(s):
    """Minimal JSON parser for the subset we produce."""
    s = s.strip()
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if s.startswith("["):
        return _parse_array(s)
    if s.startswith("{"):
        return _parse_object(s)
    try:
        return int(s)
    except ValueError:
        return s


def _parse_array(s):
    """Parse a JSON array string."""
    s = s.strip()
    if s == "[]":
        return []
    inner = s[1:-1]
    items = _split_json(inner)
    return [_json_loads(item) for item in items]


def _parse_object(s):
    """Parse a JSON object string."""
    s = s.strip()
    if s == "{}":
        return {}
    inner = s[1:-1]
    items = _split_json(inner)
    result = {}
    for item in items:
        colon = _find_colon(item)
        if colon < 0:
            continue
        key = _json_loads(item[:colon].strip())
        if not isinstance(key, str):
            continue
        val = _json_loads(item[colon + 1:].strip())
        result[key] = val
    return result


def _split_json(s):
    """Split a JSON string by commas at depth 0."""
    items = []
    depth = 0
    in_str = False
    start = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and in_str:
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            if c in ('[', '{'):
                depth += 1
            elif c in (']', '}'):
                depth -= 1
            elif c == ',' and depth == 0:
                items.append(s[start:i].strip())
                start = i + 1
        i += 1
    tail = s[start:].strip()
    if tail:
        items.append(tail)
    return items


def _find_colon(s):
    """Find the first colon at depth 0 outside strings."""
    in_str = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and in_str:
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif c == ':' and not in_str:
            return i
        i += 1
    return -1


def _extract_payload(raw):
    """Decode payload bytes to a dict or string."""
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8").strip()
    except Exception:
        return {}
    if text.startswith("{"):
        return _json_loads(text)
    return text


def _extract_cbor_payload(raw):
    """Decode canonical-CBOR payload bytes, returning None on non-CBOR input."""
    try:
        return codec.decode(_payload_bytes(raw))
    except Exception:
        return None


def _payload_bytes(raw):
    """Return raw handler payload bytes for canonical-CBOR host calls."""
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, memoryview):
        return raw.tobytes()
    return bytes(raw)


def _as_bytes(value, field, expected_len=None):
    if isinstance(value, bytes):
        out = value
    elif isinstance(value, bytearray):
        out = bytes(value)
    elif isinstance(value, memoryview):
        out = value.tobytes()
    elif isinstance(value, str):
        text = value[2:] if value.startswith("0x") else value
        try:
            out = bytes.fromhex(text)
        except ValueError:
            raise ValueError(field + " must be bytes or hex")
    else:
        raise ValueError(field + " must be bytes")
    if expected_len is not None and len(out) != expected_len:
        raise ValueError(field + " must be " + str(expected_len) + " bytes")
    return out


def _validate_tags(tags):
    if tags is None:
        return {}
    if not isinstance(tags, dict):
        raise ValueError("tags must be a map")
    for key, value in tags.items():
        if not isinstance(key, str):
            raise ValueError("tag keys must be text strings")
        if not (
            isinstance(value, str)
            or isinstance(value, bool)
            or isinstance(value, float)
        ):
            raise ValueError("tag values must be text, bool, or float")
        if isinstance(value, float) and (
            value != value or value == float("inf") or value == -float("inf")
        ):
            raise ValueError("numeric tag values must be finite")
    return tags


def _msg_key(sequence):
    return b"cip7:msg:" + int(sequence).to_bytes(8, "big")


def _signing_payload(
    stream_id,
    version,
    sequence,
    timestamp_unix_ms,
    kind,
    content_type,
    tags,
    payload_format,
    payload_hash,
    key_epoch,
    signing_key_id,
):
    return codec.encode({
        "stream_id": stream_id,
        "version": int(version),
        "sequence": int(sequence),
        "timestamp_unix_ms": int(timestamp_unix_ms),
        "kind": kind,
        "content_type": content_type,
        "tags": tags,
        "payload_format": payload_format,
        "payload_hash": payload_hash,
        "key_epoch": key_epoch,
        "signing_key_id": int(signing_key_id),
    })


def _active_key_for_sequence(sequence):
    history = _get_cbor(CIP7_KEY_HISTORY_KEY, []) or []
    selected = None
    for entry in history:
        if int(entry.get("effective_sequence", 0)) <= sequence:
            if selected is None or int(entry["effective_sequence"]) >= int(selected["effective_sequence"]):
                selected = entry
    return selected


def _init_cip7(data):
    ctx = pvm_host.context()

    if _get_cbor(CIP7_STREAM_ID_KEY) is not None or _get_str(b"owner"):
        return b"error: already initialized"

    try:
        stream_id = _as_bytes(data.get("stream_id"), "stream_id")
        publisher_key = _as_bytes(
            data.get("initial_publisher_key"),
            "initial_publisher_key",
            32,
        )
    except ValueError as exc:
        return ("error: " + str(exc)).encode("utf-8")

    access_mode = data.get("access_mode", "OPEN")
    if access_mode not in ("OPEN", "SUBSCRIBER_PAID"):
        return b"error: unsupported access_mode"

    capacity = int(data.get("ring_buffer_capacity", DEFAULT_RING_BUFFER_CAPACITY))
    if capacity < 1:
        return b"error: ring_buffer_capacity must be positive"

    owner = runtime.sender().to_hex(checksum=False)
    _set_str(b"owner", owner)
    _set_json(b"publishers", [owner])
    _set_json(b"subscribers", [])
    _set_json(b"stats", {"total_publications": 0, "total_subscribers": 0})
    _set_json(b"latest", {})
    _set_json(b"metadata", {
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "created_at": ctx.get("block_height", 0),
    })

    _set_cbor(CIP7_STREAM_ID_KEY, stream_id)
    _set_str(CIP7_ACCESS_MODE_KEY, access_mode)
    _set_int(CIP7_HEAD_SEQUENCE_KEY, 0)
    _set_int(CIP7_FLOOR_SEQUENCE_KEY, 1)
    _set_int(CIP7_RING_BUFFER_CAPACITY_KEY, capacity)
    _set_int(CIP7_CURRENT_SIGNING_KEY_ID_KEY, 1)
    _set_cbor(CIP7_KEY_HISTORY_KEY, [{
        "signing_key_id": 1,
        "publisher_key": publisher_key,
        "effective_sequence": 1,
    }])

    pvm_host.emit_event("cip7.stream.initialized", codec.encode({
        "stream_id": stream_id,
        "owner": owner,
        "access_mode": access_mode,
    }))
    return b"ok: cip7 stream initialized"


def _publish_cip7(data):
    stream_id = _get_cbor(CIP7_STREAM_ID_KEY)
    if stream_id is None:
        return b"error: stream not initialized"

    try:
        kind = data.get("kind", "data")
        if not isinstance(kind, str) or not kind:
            return b"error: kind must be a non-empty string"
        content_type = data.get("content_type", "application/json")
        if not isinstance(content_type, str) or not content_type:
            return b"error: content_type must be a non-empty string"
        tags = _validate_tags(data.get("tags", {}))
        payload_format = data.get("payload_format", "PLAINTEXT")
        payload_inline = _as_bytes(data.get("payload_inline", b""), "payload_inline")
        publisher_sig = _as_bytes(data.get("publisher_sig"), "publisher_sig", 64)
    except ValueError as exc:
        return ("error: " + str(exc)).encode("utf-8")

    if len(payload_inline) > MAX_PAYLOAD_INLINE_BYTES:
        return b"error: payload_inline exceeds 16 KiB"

    key_epoch = data.get("key_epoch")
    access_mode = _get_str(CIP7_ACCESS_MODE_KEY, "OPEN")
    if payload_format == "PLAINTEXT":
        if key_epoch is not None:
            return b"error: PLAINTEXT payloads must not set key_epoch"
    elif payload_format == "CIPHERTEXT":
        if not isinstance(key_epoch, int):
            return b"error: CIPHERTEXT payloads require integer key_epoch"
    else:
        return b"error: unsupported payload_format"
    if access_mode == "SUBSCRIBER_PAID" and payload_format != "CIPHERTEXT":
        return b"error: paid streams require ciphertext payloads"
    if access_mode == "OPEN" and payload_format != "PLAINTEXT":
        return b"error: open streams require plaintext payloads"

    head = _get_int(CIP7_HEAD_SEQUENCE_KEY, 0)
    sequence = head + 1
    try:
        if "stream_id" in data and _as_bytes(data.get("stream_id"), "stream_id") != stream_id:
            return b"error: stream_id mismatch"
    except ValueError as exc:
        return ("error: " + str(exc)).encode("utf-8")
    if "sequence" in data and int(data.get("sequence")) != sequence:
        return b"error: sequence mismatch"
    timestamp_unix_ms = int(
        data.get("timestamp_unix_ms", pvm_host.context().get("timestamp_ms", 0))
    )
    signing_key_id = _get_int(CIP7_CURRENT_SIGNING_KEY_ID_KEY, 1)
    active_key = _active_key_for_sequence(sequence)
    if active_key is None:
        return b"error: no active signing key"
    publisher_key = active_key["publisher_key"]
    signing_key_id = int(active_key.get("signing_key_id", signing_key_id))
    payload_hash = hashlib.sha256(payload_inline).digest()
    signing_payload = _signing_payload(
        stream_id,
        1,
        sequence,
        timestamp_unix_ms,
        kind,
        content_type,
        tags,
        payload_format,
        payload_hash,
        key_epoch,
        signing_key_id,
    )

    if not pvm_host.ed25519_verify(publisher_key, signing_payload, publisher_sig):
        pvm_host.emit_event("cip7.stream.publish_rejected", codec.encode({
            "stream_id": stream_id,
            "sequence": sequence,
            "reason": "invalid publisher signature",
        }))
        return b"error: invalid publisher signature"

    message = {
        "stream_id": stream_id,
        "version": 1,
        "sequence": sequence,
        "timestamp_unix_ms": timestamp_unix_ms,
        "kind": kind,
        "content_type": content_type,
        "tags": tags,
        "payload_format": payload_format,
        "payload_inline": payload_inline,
        "payload_hash": payload_hash,
        "key_epoch": key_epoch,
        "signing_key_id": signing_key_id,
        "publisher_sig": publisher_sig,
    }
    encoded_message = codec.encode(message)
    pvm_host.set_state(_msg_key(sequence), encoded_message)
    _set_int(CIP7_HEAD_SEQUENCE_KEY, sequence)

    capacity = _get_int(CIP7_RING_BUFFER_CAPACITY_KEY, DEFAULT_RING_BUFFER_CAPACITY)
    floor = _get_int(CIP7_FLOOR_SEQUENCE_KEY, 1)
    while sequence - floor + 1 > capacity:
        if hasattr(pvm_host, "delete_state"):
            pvm_host.delete_state(_msg_key(floor))
        floor += 1
        _set_int(CIP7_FLOOR_SEQUENCE_KEY, floor)

    stats = _get_json(b"stats") or {"total_publications": 0, "total_subscribers": 0}
    stats["total_publications"] = stats.get("total_publications", 0) + 1
    _set_json(b"stats", stats)
    pub_ctx = pvm_host.context()
    _set_json(b"latest", {
        "sequence": sequence,
        "publisher": Address(pub_ctx.get("sender", "")).to_hex(checksum=False) if pub_ctx.get("sender") else "",
        "block_height": pub_ctx.get("block_height", 0),
    })

    pvm_host.emit_event("cip7.stream.message_published", codec.encode({
        "stream_id": stream_id,
        "sequence": sequence,
        "payload_hash": payload_hash,
        "signing_key_id": signing_key_id,
    }))
    return encoded_message


# -- Handlers -----------------------------------------------------------------

def init(payload):
    """Deploy: set owner, metadata, empty subscribers/publishers."""
    cbor_data = _extract_cbor_payload(payload)
    if isinstance(cbor_data, dict) and "initial_publisher_key" in cbor_data:
        return _init_cip7(cbor_data)

    ctx = pvm_host.context()

    existing_owner = _get_str(b"owner")
    if existing_owner and len(existing_owner) > 0:
        return b"error: already initialized"

    owner = runtime.sender().to_hex(checksum=False)
    block_height = ctx.get("block_height", 0)

    data = _extract_payload(payload)

    name = ""
    description = ""
    frequency = 0
    schema = {}
    if isinstance(data, dict):
        name = data.get("name", "")
        description = data.get("description", "")
        frequency = data.get("frequency", 0)
        schema = data.get("schema", {})

    _set_str(b"owner", owner)
    _set_json(b"publishers", [owner])
    _set_json(b"subscribers", [])
    _set_json(b"metadata", {
        "name": name,
        "description": description,
        "frequency": frequency,
        "schema": schema,
        "created_at": block_height,
    })
    _set_json(b"stats", {"total_publications": 0, "total_subscribers": 0})
    _set_json(b"latest", {})

    pvm_host.emit_event("stream.init", ("owner=" + owner + ",name=" + name).encode())
    return b"ok: stream initialized"


def publish(payload):
    """Publishers only: push payload to all subscribers via send_message()."""
    cbor_data = _extract_cbor_payload(payload)
    if isinstance(cbor_data, dict) and "publisher_sig" in cbor_data:
        return _publish_cip7(cbor_data)
    if _get_cbor(CIP7_STREAM_ID_KEY) is not None:
        return b"error: cip7 publish requires canonical CBOR envelope"

    ctx = pvm_host.context()
    sender = runtime.sender().to_hex(checksum=False)
    block_height = ctx.get("block_height", 0)

    publishers = _get_json(b"publishers") or []
    if sender not in publishers:
        return b"error: not authorized publisher"

    data = _extract_payload(payload)

    subscribers = _get_json(b"subscribers") or []

    # Build the message to deliver
    message = _json_dumps({
        "type": "stream_data",
        "payload": data,
        "publisher": sender,
        "block_height": block_height,
    })
    message_bytes = message.encode("utf-8")

    # Push to every subscriber
    delivered = 0
    for sub_hex in subscribers:
        try:
            runtime.send_message(sub_hex, message_bytes)
            delivered += 1
        except Exception:
            pvm_host.emit_event("stream.send_error",
                                ("subscriber=" + sub_hex).encode())

    # Update stats and latest
    stats = _get_json(b"stats") or {"total_publications": 0, "total_subscribers": 0}
    stats["total_publications"] = stats.get("total_publications", 0) + 1
    _set_json(b"stats", stats)

    _set_json(b"latest", {
        "payload": data,
        "publisher": sender,
        "block_height": block_height,
    })

    pvm_host.emit_event("stream.publish",
                        ("delivered=" + str(delivered) + "/" + str(len(subscribers))).encode())
    return ("ok: published to " + str(delivered) + "/" + str(len(subscribers)) + " subscribers").encode()


MAX_SUBSCRIBERS = 1024  # #294: prevent unbounded subscriber list growth


def subscribe(payload):
    """Any actor: add sender to subscriber list."""
    ctx = pvm_host.context()
    sender = runtime.sender().to_hex(checksum=False)

    subscribers = _get_json(b"subscribers") or []
    if sender in subscribers:
        return b"ok: already subscribed"

    if len(subscribers) >= MAX_SUBSCRIBERS:
        return b"error: subscriber limit reached"

    subscribers.append(sender)
    _set_json(b"subscribers", subscribers)

    stats = _get_json(b"stats") or {"total_publications": 0, "total_subscribers": 0}
    stats["total_subscribers"] = len(subscribers)
    _set_json(b"stats", stats)

    pvm_host.emit_event("stream.subscribe", ("subscriber=" + sender).encode())
    return b"ok: subscribed"


def unsubscribe(payload):
    """Any actor: remove sender from subscriber list."""
    ctx = pvm_host.context()
    sender = runtime.sender().to_hex(checksum=False)

    subscribers = _get_json(b"subscribers") or []
    if sender not in subscribers:
        return b"ok: not subscribed"

    subscribers.remove(sender)
    _set_json(b"subscribers", subscribers)

    stats = _get_json(b"stats") or {"total_publications": 0, "total_subscribers": 0}
    stats["total_subscribers"] = len(subscribers)
    _set_json(b"stats", stats)

    pvm_host.emit_event("stream.unsubscribe", ("subscriber=" + sender).encode())
    return b"ok: unsubscribed"


def add_publisher(payload):
    """Owner only: authorize a publisher address."""
    ctx = pvm_host.context()
    sender = runtime.sender().to_hex(checksum=False)
    owner = _get_str(b"owner")

    if sender != owner:
        return b"error: only owner can add publishers"

    data = _extract_payload(payload)
    new_pub = ""
    if isinstance(data, dict):
        new_pub = data.get("address", "")
    elif isinstance(data, str):
        new_pub = data
    new_pub = new_pub.strip()
    if not new_pub:
        return b"error: missing address"
    new_pub = Address(new_pub).to_hex(checksum=False)

    publishers = _get_json(b"publishers") or []
    if new_pub in publishers:
        return b"ok: already a publisher"

    publishers.append(new_pub)
    _set_json(b"publishers", publishers)

    pvm_host.emit_event("stream.add_publisher", ("publisher=" + new_pub).encode())
    return b"ok: publisher added"


def remove_publisher(payload):
    """Owner only: revoke publisher authorization."""
    ctx = pvm_host.context()
    sender = runtime.sender().to_hex(checksum=False)
    owner = _get_str(b"owner")

    if sender != owner:
        return b"error: only owner can remove publishers"

    data = _extract_payload(payload)
    rm_pub = ""
    if isinstance(data, dict):
        rm_pub = data.get("address", "")
    elif isinstance(data, str):
        rm_pub = data
    rm_pub = rm_pub.strip()
    if not rm_pub:
        return b"error: missing address"
    rm_pub = Address(rm_pub).to_hex(checksum=False)

    publishers = _get_json(b"publishers") or []
    if rm_pub not in publishers:
        return b"ok: not a publisher"

    publishers.remove(rm_pub)
    _set_json(b"publishers", publishers)

    pvm_host.emit_event("stream.remove_publisher", ("publisher=" + rm_pub).encode())
    return b"ok: publisher removed"


def get_metadata(payload):
    """Anyone: return feed metadata without emitting events during /actor/read."""
    metadata = _get_json(b"metadata") or {}
    result = _json_dumps(metadata)
    return result.encode("utf-8")


def get_subscribers(payload):
    """Anyone: return subscriber list."""
    subscribers = _get_json(b"subscribers") or []
    result = _json_dumps(subscribers)
    return result.encode("utf-8")


def get_stats(payload):
    """Anyone: publication/subscriber counts."""
    stats = _get_json(b"stats") or {"total_publications": 0, "total_subscribers": 0}
    result = _json_dumps(stats)
    return result.encode("utf-8")


def get_latest(payload):
    """Anyone: last published data."""
    latest = _get_json(b"latest") or {}
    if not latest:
        return b"no data published yet"
    result = _json_dumps(latest)
    return result.encode("utf-8")


def get_head(payload):
    """Return the canonical CIP-7 sequence cursor."""
    return codec.encode({
        "head_sequence": _get_int(CIP7_HEAD_SEQUENCE_KEY, 0),
        "floor_sequence": _get_int(CIP7_FLOOR_SEQUENCE_KEY, 1),
    })


def get_since(payload):
    """Return canonical StreamMessage envelopes after the requested cursor."""
    data = _extract_cbor_payload(payload) or {}
    cursor = int(data.get("cursor", 0))
    limit = int(data.get("limit", 100))
    if limit < 1:
        limit = 1
    if limit > 256:
        limit = 256
    floor = _get_int(CIP7_FLOOR_SEQUENCE_KEY, 1)
    head = _get_int(CIP7_HEAD_SEQUENCE_KEY, 0)
    start = max(cursor + 1, floor)
    out = []
    seq = start
    while seq <= head and len(out) < limit:
        raw = pvm_host.get_state(_msg_key(seq))
        if raw:
            out.append(codec.decode(raw))
        seq += 1
    return codec.encode({
        "messages": out,
        "head_sequence": head,
        "floor_sequence": floor,
    })


def get_key_at_sequence(payload):
    """Return the publisher key-schedule entry effective for a sequence."""
    data = _extract_cbor_payload(payload) or {}
    sequence = int(data.get("sequence", _get_int(CIP7_HEAD_SEQUENCE_KEY, 0)))
    entry = _active_key_for_sequence(sequence)
    if entry is None:
        return b"error: no active signing key"
    return codec.encode(entry)


def initialize_paid_stream_config(payload):
    """Initialize paid-mode SKM config for this stream.

    Payload is canonical-CBOR `PaidStreamConfig`. SKM authorization enforces
    that the transaction sender is the configured stream owner.
    """
    result = pvm_host.call_actor(
        STREAM_KEY_MANAGER_ADDR,
        "initialize_paid_stream_config",
        _payload_bytes(payload),
        STREAM_KEY_MANAGER_CALL_CYCLES,
    )
    pvm_host.emit_event("cip7.stream.paid_stream_config_initialized", result)
    return result


def register_content_keys(payload):
    """Register or re-wrap epoch content keys with the Stream Key Manager."""
    result = pvm_host.call_actor(
        STREAM_KEY_MANAGER_ADDR,
        "register_content_keys",
        _payload_bytes(payload),
        STREAM_KEY_MANAGER_CALL_CYCLES,
    )
    pvm_host.emit_event("cip7.stream.content_keys_registered", result)
    return result


def acquire_epoch_access(payload):
    """Acquire paid epoch access and mint CBSS seal requests as needed."""
    result = pvm_host.acquire_epoch_access(_payload_bytes(payload))
    pvm_host.emit_event("cip7.stream.access_acquired", result)
    return result


def handle_message(payload):
    """Dispatch incoming send_message() calls by method field.

    send_message() always routes to handle_message on the target.
    We dispatch to subscribe/unsubscribe based on the payload.

    Security (H-6): Always use authenticated context sender — never trust
    caller-supplied address fields in the payload.
    """
    data = _extract_cbor_payload(payload)
    if not isinstance(data, dict):
        data = _extract_payload(payload)
    if not isinstance(data, dict):
        return b"error: expected dict with method field"

    method = data.get("method", "")

    if method == "subscribe":
        return subscribe(payload)
    if method == "unsubscribe":
        return unsubscribe(payload)
    if method == "initialize_paid_stream_config":
        return initialize_paid_stream_config(payload)
    if method == "register_content_keys":
        return register_content_keys(payload)
    if method == "acquire_epoch_access":
        return acquire_epoch_access(payload)
    if method == "publish":
        return publish(payload)
    if method == "get_head":
        return get_head(payload)
    if method == "get_since":
        return get_since(payload)
    if method == "get_key_at_sequence":
        return get_key_at_sequence(payload)

    return ("error: unknown method " + method).encode()
