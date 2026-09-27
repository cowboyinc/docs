"""Canonical CBOR (RFC 8949 §4.2) for the small shapes the permit and account
proofs use: unsigned integers, byte strings, text strings, definite arrays.

Encoding is deterministic (shortest-form lengths, definite lengths only).
Decoding is strict: it refuses non-shortest lengths, indefinite lengths,
every other major type, and trailing bytes.
"""

from __future__ import annotations


class CborError(ValueError):
    pass


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    if n < 1 << 8:
        return bytes([major << 5 | 24, n])
    if n < 1 << 16:
        return bytes([major << 5 | 25]) + n.to_bytes(2, "big")
    if n < 1 << 32:
        return bytes([major << 5 | 26]) + n.to_bytes(4, "big")
    if n < 1 << 64:
        return bytes([major << 5 | 27]) + n.to_bytes(8, "big")
    raise CborError("integer too large")


def encode(value) -> bytes:
    if isinstance(value, bool):
        raise CborError("bool not supported")
    if isinstance(value, int):
        if value < 0:
            raise CborError("negative integers not supported")
        return _head(0, value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        return _head(3, len(data)) + data
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(encode(v) for v in value)
    raise CborError(f"unsupported type {type(value).__name__}")


def decode(data: bytes):
    value, end = _decode(data, 0)
    if end != len(data):
        raise CborError("trailing bytes")
    return value


def _decode(data: bytes, i: int):
    if i >= len(data):
        raise CborError("truncated")
    major, info = data[i] >> 5, data[i] & 0x1F
    i += 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        width = 1 << (info - 24)
        if i + width > len(data):
            raise CborError("truncated")
        n = int.from_bytes(data[i : i + width], "big")
        i += width
        floor = 24 if width == 1 else 1 << (8 * (width // 2))
        if n < floor:
            raise CborError("non-canonical length")
    else:
        raise CborError("indefinite or reserved length")
    if major == 0:
        return n, i
    if major in (2, 3):
        if i + n > len(data):
            raise CborError("truncated")
        raw = data[i : i + n]
        if major == 3:
            try:
                return raw.decode("utf-8"), i + n
            except UnicodeDecodeError as exc:
                raise CborError("invalid utf-8") from exc
        return bytes(raw), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = _decode(data, i)
            out.append(v)
        return out, i
    raise CborError(f"unsupported major type {major}")
