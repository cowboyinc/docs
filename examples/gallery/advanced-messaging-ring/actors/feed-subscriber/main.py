"""
Feed Subscriber - Example consumer for Watchtower data feeds.

Subscribes to a StreamActor feed and stores received data on-chain.
Use this as a starting point for building actors that react to feed updates.

Methods:
  init              - Deploy: store owner
  subscribe_to_feed - Subscribe to a feed by address
  handle_message    - Receive and store stream data
  get_messages      - Return stored messages (last 100)

Usage:
  1. Deploy this actor:
     cowboy actor deploy feed-subscriber

  2. Subscribe to a feed:
     cowboy actor execute --actor <this_addr> --handler subscribe_to_feed \
       --payload '{"feed_address": "<feed_addr>"}'

  3. Read received messages:
     cowboy actor execute --actor <this_addr> --handler get_messages
"""

import pvm_host
from cowboy_sdk import Address, runtime


MAX_STORED_MESSAGES = 100
MAX_MESSAGE_SIZE = 65536  # 64KB


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


def _json_dumps(obj):
    """Minimal JSON serializer (no stdlib available in PVM)."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        escaped = obj.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
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
    """Minimal JSON parser (no stdlib available in PVM)."""
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
    s = s.strip()
    if s == "[]":
        return []
    inner = s[1:-1]
    items = _split_json(inner)
    return [_json_loads(item) for item in items]


def _parse_object(s):
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
        val = _json_loads(item[colon + 1:].strip())
        result[key] = val
    return result


def _split_json(s):
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


# -- Handlers -----------------------------------------------------------------

def init(payload):
    """Deploy: store owner address."""
    pvm_host.set_state(b"__name__", b"Feed Subscriber")
    pvm_host.set_state(b"__description__", b"Pub/sub feed subscriber actor")
    pvm_host.set_state(b"__version__", b"1.0.0")
    ctx = pvm_host.context()
    owner = runtime.sender().to_hex(checksum=False)

    _set_str(b"owner", owner)
    _set_int(b"message_count", 0)
    _set_str(b"messages", "[]")

    pvm_host.emit_event("subscriber.init", ("owner=" + owner).encode())
    return b"ok: feed subscriber initialized"


def subscribe_to_feed(payload):
    """Subscribe to a StreamActor feed.

    payload = {"feed_address": "abcdef1234..."}
    """
    # M-12: Only owner can subscribe
    sender = runtime.sender().to_hex(checksum=False)
    owner = _get_str(b"owner")
    if sender.lower() != owner.lower():
        return b"error: only owner can subscribe"

    data = _extract_payload(payload)
    feed_addr = ""
    if isinstance(data, dict):
        feed_addr = data.get("feed_address", "")
    elif isinstance(data, str):
        feed_addr = data
    feed_addr = feed_addr.strip()

    if not feed_addr:
        return b"error: feed_address required"
    feed_addr = Address(feed_addr).to_hex(checksum=False)

    my_addr = runtime.actor_address().to_hex(checksum=False)
    subscribe_msg = ('{"method":"subscribe","subscriber":"' + my_addr + '"}').encode("utf-8")
    runtime.send_message(feed_addr, subscribe_msg)

    _set_str(b"feed_address", feed_addr)
    pvm_host.emit_event("subscriber.subscribed", ("feed=" + feed_addr).encode())
    return ("ok: subscription sent to " + feed_addr).encode()


def handle_message(payload):
    """Receive stream data from a feed and store it.

    Called automatically when subscribed feed publishes new data.
    payload = {"type":"stream_data","payload":{...},"publisher":"...","block_height":N}
    """
    # L-3: Reject oversized payloads before any processing
    if payload and len(payload) > MAX_MESSAGE_SIZE:
        return b"error: message too large"

    ctx = pvm_host.context()

    # M-11: Verify sender is the registered feed address
    sender = runtime.sender().to_hex(checksum=False)
    feed_address = _get_str(b"feed_address")
    if not feed_address:
        return b"error: no feed subscription active"
    if sender.lower() != feed_address.lower():
        return b"error: unauthorized sender"

    block_height = ctx.get("block_height", 0)

    data = _extract_payload(payload)
    if not isinstance(data, dict):
        return b"error: expected stream_data message"

    stream_payload = data.get("payload", {})
    publisher = data.get("publisher", "unknown")

    # Store the message
    msg_count = _get_int(b"message_count", 0)
    msg_count += 1
    _set_int(b"message_count", msg_count)

    messages_raw = _get_str(b"messages", "[]")
    messages = _json_loads(messages_raw)
    if not isinstance(messages, list):
        messages = []

    messages.append({
        "payload": stream_payload,
        "publisher": publisher,
        "block_height": block_height,
        "index": msg_count,
    })

    # Keep only last MAX_STORED_MESSAGES
    if len(messages) > MAX_STORED_MESSAGES:
        messages = messages[-MAX_STORED_MESSAGES:]
    _set_str(b"messages", _json_dumps(messages))

    pvm_host.emit_event("subscriber.received",
                        ("msg_index=" + str(msg_count)).encode())
    return ("ok: stored message " + str(msg_count)).encode()


def get_messages(payload):
    """Return stored messages (last 100)."""
    messages_raw = _get_str(b"messages", "[]")
    return messages_raw.encode("utf-8")
