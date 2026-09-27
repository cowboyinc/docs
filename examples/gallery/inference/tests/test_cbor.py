import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain import cbor


@pytest.mark.parametrize(
    "value,hex_",
    [
        (0, "00"), (23, "17"), (24, "1818"), (255, "18ff"), (256, "190100"), (65535, "19ffff"),
        (65536, "1a00010000"), (1 << 32, "1b0000000100000000"),
        (b"", "40"), (b"\x01\x02", "420102"), ("", "60"), ("a", "6161"), ([], "80"), ([1, "a", b"b"], "83016161 4162".replace(" ", "")),
    ],
)
def test_vectors(value, hex_):
    assert cbor.encode(value).hex() == hex_
    assert cbor.decode(bytes.fromhex(hex_)) == value


def test_rejects_unsupported_and_noncanonical():
    for bad in (b"\x18\x17", b"\x19\x00\xff", b"\x1a\x00\x00\xff\xff", b"\x20", b"\xa0", b"\xf5", b"\x5f\xff", b"\x00\x00", b"\x41", b"\x61\xff"):
        with pytest.raises(cbor.CborError):
            cbor.decode(bad)
    for v in (-1, True, 1.5, {}, None):
        with pytest.raises(cbor.CborError):
            cbor.encode(v)
