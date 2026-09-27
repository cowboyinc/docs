"""hl_common — shared Hyperliquid actor plumbing (CIP-26 library)."""

import functools
import json
import math

from cowboy_sdk import AddressError, runtime, to_address_bytes

_KECCAK_EMPTY = bytes.fromhex(
    "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)


def mark_pending(storage, runtime_api, flag, height=None, now_ms=None):
    """Mark one continuation loop in flight using block and wall clocks."""
    if height is None:
        height = int(runtime_api.get_block_height())
    if now_ms is None:
        now_ms = int(runtime_api.get_timestamp_ms())
    storage[flag + "_pending"] = True
    storage[flag + "_started_block"] = int(height)
    storage[flag + "_started_ms"] = int(now_ms)


def tick_pending_guard(
    storage, runtime_api, flag, event_prefix, guard_blocks, guard_ms
):
    """Release lost continuation work after either bounded wait expires.

    Returns ``(allowed, height, recovered)``. A block bound alone stretches as
    a chain slows, so the wall-clock bound is required to make recovery time
    predictable after a validator restart or another orphaned continuation.
    """
    height = int(runtime_api.get_block_height())
    now_ms = int(runtime_api.get_timestamp_ms())
    pending = bool(storage.get(flag + "_pending"))
    started = int(storage.get(flag + "_started_block", 0))
    started_ms = int(storage.get(flag + "_started_ms", 0) or 0)
    waited = height - started
    waited_ms = now_ms - started_ms if started_ms else 0
    stale_by_clock = bool(started_ms) and waited_ms > int(guard_ms)

    if pending and waited <= int(guard_blocks) and not stale_by_clock:
        runtime_api.emit_event(
            event_prefix + ".skipped",
            {
                "loop": flag,
                "waited": waited,
                "guard": int(guard_blocks),
                "waited_ms": waited_ms,
            },
        )
        return False, height, False

    if pending:
        runtime_api.emit_event(
            event_prefix + ".continuation_lost",
            {
                "loop": flag,
                "waited_blocks": waited,
                "guard_blocks": int(guard_blocks),
                "started_block": started,
                "height": height,
                "waited_ms": waited_ms,
                "guard_ms": int(guard_ms),
                "released_by": "clock" if stale_by_clock else "blocks",
            },
        )

    mark_pending(storage, runtime_api, flag, height, now_ms)
    return True, height, pending


def _candidates():
    """Yield keccak candidates, host function first (it's the cheap one on-chain)."""
    try:
        from cowboy_sdk import runtime

        yield runtime.keccak256
    except Exception:
        pass
    try:
        from eth_utils import keccak as _eth_keccak

        yield _eth_keccak
    except Exception:
        pass


@functools.cache
def _resolve_keccak():
    """First candidate that actually computes Keccak-256. Cached after the first call."""
    for fn in _candidates():
        try:
            if bytes(fn(b"")) == _KECCAK_EMPTY:
                return fn
        except Exception:
            continue
    raise RuntimeError(
        "no working keccak256 available: every candidate failed the "
        "known-answer test (a SHA-256 or SHA3-256 stub will fail here by design)"
    )


def keccak(data: bytes) -> bytes:
    return _resolve_keccak()(bytes(data))


def _pack_uint(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n <= 0xFF:
        return b"\xcc" + n.to_bytes(1, "big")
    if n <= 0xFFFF:
        return b"\xcd" + n.to_bytes(2, "big")
    if n <= 0xFFFFFFFF:
        return b"\xce" + n.to_bytes(4, "big")
    if n <= 0xFFFFFFFFFFFFFFFF:
        return b"\xcf" + n.to_bytes(8, "big")
    raise ValueError("integer too large for msgpack: %d" % n)


def _pack_int(n: int) -> bytes:
    if n >= 0:
        return _pack_uint(n)
    if n >= -32:
        return (n & 0xFF).to_bytes(1, "big")
    if n >= -(2**7):
        return b"\xd0" + (n & 0xFF).to_bytes(1, "big")
    if n >= -(2**15):
        return b"\xd1" + (n & 0xFFFF).to_bytes(2, "big")
    if n >= -(2**31):
        return b"\xd2" + (n & 0xFFFFFFFF).to_bytes(4, "big")
    if n >= -(2**63):
        return b"\xd3" + (n & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
    raise ValueError("integer too small for msgpack: %d" % n)


_MIN_NORMAL = 2.2250738585072014e-308


def _f64_bits(x: float) -> int:
    """IEEE-754 binary64 bit pattern, without `struct`."""
    sign = 1 if math.copysign(1.0, x) < 0 else 0

    if math.isnan(x):
        return (sign << 63) | (0x7FF << 52) | (1 << 51)
    if x in (float("inf"), float("-inf")):
        return (sign << 63) | (0x7FF << 52)
    if x == 0.0:
        return sign << 63

    if abs(x) < _MIN_NORMAL:
        return (sign << 63) | int(math.ldexp(abs(x), 1074))

    m, e = math.frexp(abs(x))
    biased = e + 1022
    if biased >= 0x7FF:
        return (sign << 63) | (0x7FF << 52)
    frac = int((m * 2.0 - 1.0) * (2**52))
    return (sign << 63) | (biased << 52) | frac


def _pack_f64(x: float) -> bytes:
    return b"\xcb" + _f64_bits(x).to_bytes(8, "big")


def _pack_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    n = len(raw)
    if n < 32:
        return bytes([0xA0 | n]) + raw
    if n <= 0xFF:
        return b"\xd9" + n.to_bytes(1, "big") + raw
    if n <= 0xFFFF:
        return b"\xda" + n.to_bytes(2, "big") + raw
    return b"\xdb" + n.to_bytes(4, "big") + raw


def _pack_bin(b: bytes) -> bytes:
    n = len(b)
    if n <= 0xFF:
        return b"\xc4" + n.to_bytes(1, "big") + b
    if n <= 0xFFFF:
        return b"\xc5" + n.to_bytes(2, "big") + b
    return b"\xc6" + n.to_bytes(4, "big") + b


def _pack_array(items) -> bytes:
    n = len(items)
    if n < 16:
        head = bytes([0x90 | n])
    elif n <= 0xFFFF:
        head = b"\xdc" + n.to_bytes(2, "big")
    else:
        head = b"\xdd" + n.to_bytes(4, "big")
    return head + b"".join(_packb(v) for v in items)


def _pack_map(d: dict) -> bytes:
    n = len(d)
    if n < 16:
        head = bytes([0x80 | n])
    elif n <= 0xFFFF:
        head = b"\xde" + n.to_bytes(2, "big")
    else:
        head = b"\xdf" + n.to_bytes(4, "big")

    return head + b"".join(_packb(k) + _packb(v) for k, v in d.items())


def _packb(obj) -> bytes:
    """Serialize `obj` the way `msgpack.packb(obj)` does (default settings)."""
    if obj is None:
        return b"\xc0"

    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        return _pack_int(obj)
    if isinstance(obj, float):
        return _pack_f64(obj)
    if isinstance(obj, str):
        return _pack_str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return _pack_bin(bytes(obj))
    if isinstance(obj, dict):
        return _pack_map(obj)
    if isinstance(obj, (list, tuple)):
        return _pack_array(obj)
    raise TypeError("cannot msgpack %r" % type(obj).__name__)


def packb(obj) -> bytes:
    """Public alias — `msgpack.packb` equivalent for the HL action subset."""
    return _packb(obj)


_DOMAIN_TYPEHASH = b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
_AGENT_TYPEHASH = b"Agent(string source,bytes32 connectionId)"


_DOMAIN_NAME = "Exchange"
_DOMAIN_VERSION = "1"
_DOMAIN_CHAIN_ID = 1337
_DOMAIN_VERIFYING_CONTRACT = b"\x00" * 20


def _word(b: bytes) -> bytes:
    """Left-pad to a 32-byte ABI word."""
    if len(b) > 32:
        raise ValueError("value wider than one ABI word")
    return b"\x00" * (32 - len(b)) + b


def _domain_separator() -> bytes:
    return keccak(
        keccak(_DOMAIN_TYPEHASH)
        + keccak(_DOMAIN_NAME.encode())
        + keccak(_DOMAIN_VERSION.encode())
        + _word(_DOMAIN_CHAIN_ID.to_bytes(32, "big"))
        + _word(_DOMAIN_VERIFYING_CONTRACT)
    )


def _agent_struct_hash(source: str, connection_id: bytes) -> bytes:
    if len(connection_id) != 32:
        raise ValueError("connectionId must be 32 bytes, got %d" % len(connection_id))
    return keccak(keccak(_AGENT_TYPEHASH) + keccak(source.encode()) + connection_id)


def _address_to_bytes(addr: str) -> bytes:
    raw = bytes.fromhex(addr[2:] if addr.startswith(("0x", "0X")) else addr)
    if len(raw) != 20:
        raise ValueError("address must be 20 bytes, got %d" % len(raw))
    return raw


def action_hash(action, nonce: int, vault_address=None, expires_after=None) -> bytes:
    """Port of `hyperliquid.utils.signing.action_hash`."""
    data = _packb(action)
    data += nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01"
        data += _address_to_bytes(vault_address)
    if expires_after is not None:
        data += b"\x00"
        data += expires_after.to_bytes(8, "big")
    return keccak(data)


def hl_digest(
    action, nonce: int, vault_address=None, expires_after=None, is_mainnet: bool = False
) -> bytes:
    """The 32 bytes to put in `X-Cowboy-Sign-Digest`."""
    ah = action_hash(action, nonce, vault_address, expires_after)
    source = "a" if is_mainnet else "b"
    return keccak(b"\x19\x01" + _domain_separator() + _agent_struct_hash(source, ah))


def normalize_address(value, field="address", allow_empty=True) -> str:
    """Return one lower-case 20-byte hex address or reject malformed input."""
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    try:
        return "0x" + to_address_bytes(text).hex()
    except AddressError as exc:
        raise ValueError(field + " must be a 20-byte hex address") from exc


def sign_headers(
    action,
    nonce,
    vault_address=None,
    expires_after=None,
    is_mainnet=False,
    key_ref="${HL_AGENT_KEY}",
) -> dict:
    """Ready-made headers for `runner.http(...)`."""
    digest = hl_digest(action, nonce, vault_address, expires_after, is_mainnet)
    return {
        "X-Cowboy-Sign-Key": key_ref,
        "X-Cowboy-Sign-Digest": "0x" + digest.hex(),
        "Content-Type": "application/json",
    }


def _parse(payload) -> dict:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8") if payload else "{}"
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _json_bytes(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _b64_text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    s = str(value or "")
    # RustPython's stdlib base64 decoder lazily imports a helper on first use.
    # Actor activation intentionally empties sys.meta_path, so that import
    # fails inside runner callbacks even though ``import base64`` succeeded at
    # deployment time. Decode the small standard alphabet directly instead.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    out = bytearray()
    acc = 0
    bits = 0
    valid = 0
    for ch in s:
        if ch == "=":
            break
        value6 = alphabet.find(ch)
        if value6 < 0:
            if ch in (" ", "\t", "\r", "\n"):
                continue
            return s
        valid += 1
        acc = (acc << 6) | value6
        bits += 6
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 255)
            # Retain only the unconsumed low bits. Without this, ``acc`` grows
            # with the whole response and breaches the PVM's 4096-bit integer
            # bound on ordinary RSS bodies.
            acc = acc & ((1 << bits) - 1) if bits else 0
    if not valid:
        return "" if not s else s
    return bytes(out).decode("utf-8", "replace")


def _result_text(result):
    # `await runner.<job>()` yields a cowboy_sdk RunnerResult, which is
    # explicitly NOT a dict ("isinstance(result, dict) is False here" per its
    # own docstring) — it wraps the decoded reply in `.raw`. Without unwrapping,
    # every branch below missed and we fell through to `str(result)`, which
    # renders RunnerResult.text -> extract_text(raw). extract_text matches only
    # content/text/response/data/answer, and an HTTP result is
    # {"raw": <base64>, "content_type": ...} — note `content_type` is NOT
    # `content`, so nothing matched and it returned `str(dict)`, the Python repr.
    # Callers then parsed that repr: _parse_rss found no <item> and every ingest
    # staged 0 candidates while reporting success. Unwrap first so the
    # base64 `raw` branch below actually runs.
    if not isinstance(result, (dict, bytes, bytearray, str)):
        unwrapped = getattr(result, "raw", None)
        if unwrapped is not None:
            result = unwrapped

    if isinstance(result, dict):
        if "raw" in result:
            return _b64_text(result["raw"])
        c = result.get("content")
        if isinstance(c, list):
            return "".join(str(it.get("text", "")) for it in c if isinstance(it, dict))
        if c is not None:
            return str(c)
        return ""
    if isinstance(result, bytes):
        return result.decode("utf-8", "replace")
    return str(result or "")


def _normalize_result(result):
    # Same trap as _result_text: `await runner.llm()/http()` yields a cowboy_sdk
    # RunnerResult, which is deliberately NOT a dict/list/str, so every branch
    # below missed and execution fell to the final `{"raw_text": str(result)}`.
    # That stringified the model's JSON into a raw_text blob instead of parsing
    # it, so `_parse_synthesis`'s `data.get("thesis")` never found anything and
    # synthesis completed with 0 theses and 0 ideas -- silently, every run.
    # Unwrap to .raw first so the dict/str branches (incl. ```-fenced JSON) run.
    if not isinstance(result, (dict, list, str, bytes, bytearray)):
        unwrapped = getattr(result, "raw", None)
        if unwrapped is not None:
            result = unwrapped

    if isinstance(result, list):
        return {"value": result}
    if isinstance(result, dict):
        if "raw" in result:
            return _normalize_result(_b64_text(result["raw"]))
        for key in ("data", "text"):
            if key in result:
                return _normalize_result(result[key])
        if "content" in result:
            c = result["content"]
            if isinstance(c, list):
                for it in c:
                    if isinstance(it, dict) and "text" in it:
                        return _normalize_result(it["text"])
            return _normalize_result(c)
        if "data" in result:
            return _normalize_result(result["data"])
        return result
    if isinstance(result, str):
        s = result.strip()
        if s.startswith("```"):
            nl = s.find("\n")
            s = s[nl + 1 :] if nl != -1 else s
            if s.endswith("```"):
                s = s[:-3]
            s = s.strip()
        try:
            value = json.loads(s)
            return value if isinstance(value, dict) else {"value": value}
        except Exception:
            pass
        lo, hi = s.find("{"), s.rfind("}")
        if lo != -1 and hi > lo:
            try:
                value = json.loads(s[lo : hi + 1])
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return {"raw_text": result}
    return {"raw_text": str(result)}
