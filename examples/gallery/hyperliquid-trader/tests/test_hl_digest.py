"""Differential tests: our PVM-safe digest vs. the official Hyperliquid SDK.

`hl_digest` reimplements msgpack and EIP-712 from stdlib primitives because the
PVM ships neither `msgpack` nor (reliably) `struct`. A reimplementation of a
wire format is only as good as its agreement with the reference, so every layer
is diffed against the real thing rather than against hand-copied expectations:

    _packb        vs  msgpack.packb
    _f64_bits     vs  struct.pack('>d')
    action_hash   vs  hyperliquid.utils.signing.action_hash
    hl_digest     vs  eth_account's encode_typed_data over l1_payload

The last one is the test that matters: it asserts the bytes we hand the runner
in `X-Cowboy-Sign-Digest` are the bytes the SDK would have signed. Everything
above it exists to localize a failure when that one breaks.

Skips cleanly if the SDK isn't installed, so this suite stays runnable in
environments that only have the actor code.
"""

import sys
import types
from pathlib import Path

import pytest

# The digest code lives inside the actor module (single-file deploy), and the
# actor imports cowboy_sdk, which targets the PVM's RustPython. Stub it so the
# module imports under CPython — same convention as test_hl_paper_exec.py. The
# functions under test touch none of it.
_fake = types.ModuleType("cowboy_sdk")


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def _wrap(fn):
        return fn

    return _wrap


class _Runner:
    def continuation(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn

    async def http(self, *a, **k):
        return {}


_fake.actor = _identity
_fake.public = _identity
_fake.pure = _identity
_fake.deferred = _identity
_fake.callable_by = _identity
_fake.OWNER = object()
_fake.SELF = object()
_fake.runner = _Runner()
_fake.capture = lambda: types.SimpleNamespace()
_fake.runtime = types.SimpleNamespace()
_fake.AddressError = ValueError
_fake.to_address_bytes = lambda value: bytes.fromhex(
    str(value).removeprefix("0x").removeprefix("0X")
)
sys.modules["cowboy_sdk"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hl_live_exec as hl_digest  # noqa: E402

msgpack = pytest.importorskip("msgpack")
signing = pytest.importorskip("hyperliquid.utils.signing")


# ── fixtures: actions in the SDK's own field order ────────────────────────────

# A limit order as `order_wires_to_order_action` builds it. Field order here is
# load-bearing — msgpack preserves insertion order and the digest commits to it.
LIMIT_ORDER = {
    "type": "order",
    "orders": [
        {
            "a": 4,
            "b": True,
            "p": "1234.5",
            "s": "0.05",
            "r": False,
            "t": {"limit": {"tif": "Gtc"}},
        }
    ],
    "grouping": "na",
}

CANCEL = {"type": "cancel", "cancels": [{"a": 4, "o": 123456789}]}

UPDATE_LEVERAGE = {
    "type": "updateLeverage",
    "asset": 4,
    "isCross": True,
    "leverage": 10,
}

MULTI_ORDER = {
    "type": "order",
    "orders": [
        {
            "a": 0,
            "b": True,
            "p": "65000.0",
            "s": "0.001",
            "r": False,
            "t": {"limit": {"tif": "Alo"}},
        },
        {
            "a": 1,
            "b": False,
            "p": "3200.25",
            "s": "1.5",
            "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": "3100.0", "tpsl": "sl"}},
        },
    ],
    "grouping": "normalTpsl",
}

ACTIONS = [
    pytest.param(LIMIT_ORDER, id="limit_order"),
    pytest.param(CANCEL, id="cancel"),
    pytest.param(UPDATE_LEVERAGE, id="update_leverage"),
    pytest.param(MULTI_ORDER, id="multi_order_trigger"),
]


# ── layer 1: msgpack ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ACTIONS)
def test_packb_matches_msgpack(action):
    assert hl_digest.packb(action) == msgpack.packb(action)


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        127,
        128,
        255,
        256,
        65535,
        65536,
        2**32 - 1,
        2**32,
        2**64 - 1,
        -1,
        -32,
        -33,
        -128,
        -129,
        -32768,
        -32769,
        -(2**31),
        -(2**31) - 1,
        -(2**63),
        "",
        "a",
        "x" * 31,
        "x" * 32,
        "x" * 255,
        "x" * 256,
        "héllo ünïcode",
        True,
        False,
        None,
        [],
        [1, 2, 3],
        [[1], [2]],
        list(range(20)),
        {},
        {"a": 1},
        {"nested": {"deep": [1, {"x": None}]}},
        b"",
        b"\x00\xff",
    ],
)
def test_packb_matches_msgpack_for_scalars(value):
    assert hl_digest.packb(value) == msgpack.packb(value)


def test_packb_preserves_insertion_order_not_sorted():
    """The digest commits to insertion order, so a reordered dict must differ."""
    a = {"z": 1, "a": 2}
    b = {"a": 2, "z": 1}
    assert hl_digest.packb(a) == msgpack.packb(a)
    assert hl_digest.packb(a) != hl_digest.packb(b)


def test_packb_large_collections_use_16bit_headers():
    """Cross the fixarray/fixmap boundary into the 16-bit header forms."""
    big_list = list(range(100))
    big_map = {str(i): i for i in range(100)}
    assert hl_digest.packb(big_list) == msgpack.packb(big_list)
    assert hl_digest.packb(big_map) == msgpack.packb(big_map)


# ── layer 2: IEEE-754 without struct ──────────────────────────────────────────


@pytest.mark.parametrize(
    "x",
    [
        0.0,
        -0.0,
        1.0,
        -1.0,
        0.5,
        2.0,
        1234.5,
        -1234.5,
        3.141592653589793,
        1e308,
        1e-308,
        2.2250738585072014e-308,  # min normal
        5e-324,  # min subnormal
        1e-310,  # mid subnormal
        float.fromhex("0x0.fffffffffffffp-1022"),  # max subnormal
        float("inf"),
        float("-inf"),
    ],
)
def test_f64_bits_matches_struct(x):
    import struct

    assert hl_digest._f64_bits(x).to_bytes(8, "big") == struct.pack(">d", x)


def test_f64_nan_matches_struct():
    import struct

    packed = hl_digest._f64_bits(float("nan")).to_bytes(8, "big")
    assert packed == struct.pack(">d", float("nan"))


@pytest.mark.parametrize("x", [1.0, -1234.5, 0.1, 1e308, 5e-324])
def test_packb_float_matches_msgpack(x):
    assert hl_digest.packb(x) == msgpack.packb(x)


# ── layer 3: action_hash ──────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ACTIONS)
def test_action_hash_matches_sdk(action):
    nonce = 1700000000000
    assert hl_digest.action_hash(action, nonce) == signing.action_hash(
        action, None, nonce, None
    )


def test_action_hash_matches_sdk_with_vault():
    vault = "0x1234567890abcdef1234567890abcdef12345678"
    nonce = 1700000000123
    assert hl_digest.action_hash(LIMIT_ORDER, nonce, vault) == signing.action_hash(
        LIMIT_ORDER, vault, nonce, None
    )


def test_action_hash_matches_sdk_with_expires_after():
    nonce = 1700000000123
    expires = 1700000600000
    assert hl_digest.action_hash(
        LIMIT_ORDER, nonce, None, expires
    ) == signing.action_hash(LIMIT_ORDER, None, nonce, expires)


def test_action_hash_is_nonce_sensitive():
    a = hl_digest.action_hash(LIMIT_ORDER, 1700000000000)
    b = hl_digest.action_hash(LIMIT_ORDER, 1700000000001)
    assert a != b


# ── layer 4: the digest the runner signs ──────────────────────────────────────


def _sdk_digest(action, nonce, vault=None, expires=None, is_mainnet=False):
    """The exact bytes the SDK signs, via eth_account's EIP-712 encoder."""
    from eth_account.messages import encode_typed_data

    ah = signing.action_hash(action, vault, nonce, expires)
    phantom = signing.construct_phantom_agent(ah, is_mainnet)
    payload = signing.l1_payload(phantom)
    encoded = encode_typed_data(full_message=payload)
    # SignableMessage carries the pieces of EIP-191 version 0x01:
    # keccak(0x19 || 0x01 || domain_separator || struct_hash)
    return hl_digest.keccak(b"\x19" + encoded.version + encoded.header + encoded.body)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("is_mainnet", [False, True], ids=["testnet", "mainnet"])
def test_hl_digest_matches_sdk(action, is_mainnet):
    nonce = 1700000000000
    assert hl_digest.hl_digest(action, nonce, is_mainnet=is_mainnet) == _sdk_digest(
        action, nonce, is_mainnet=is_mainnet
    )


def test_hl_digest_matches_sdk_with_vault():
    vault = "0x1234567890abcdef1234567890abcdef12345678"
    nonce = 1700000000456
    assert hl_digest.hl_digest(LIMIT_ORDER, nonce, vault) == _sdk_digest(
        LIMIT_ORDER, nonce, vault
    )


def test_mainnet_and_testnet_digests_differ():
    """`source` is "a"/"b" — a testnet signature must not replay on mainnet."""
    nonce = 1700000000000
    assert hl_digest.hl_digest(
        LIMIT_ORDER, nonce, is_mainnet=False
    ) != hl_digest.hl_digest(LIMIT_ORDER, nonce, is_mainnet=True)


def test_digest_is_32_bytes():
    """The runner rejects anything but exactly 32 bytes."""
    assert len(hl_digest.hl_digest(LIMIT_ORDER, 1700000000000)) == 32


# ── the signature the runner would produce ────────────────────────────────────


def test_runner_signature_matches_sdk_end_to_end():
    """Sign our digest with the same key the SDK uses; r/s/v must agree.

    This is the whole rail in one assertion: if the runner signs the digest we
    computed, the venue sees exactly what `sign_l1_action` would have sent.
    """
    eth_account = pytest.importorskip("eth_account")
    from eth_account import Account

    priv = "0x" + "11" * 32
    wallet = Account.from_key(priv)
    nonce = 1700000000000

    sdk_sig = signing.sign_l1_action(wallet, LIMIT_ORDER, None, nonce, None, False)

    # What the runner does: sign_prehash over our digest.
    digest = hl_digest.hl_digest(LIMIT_ORDER, nonce, is_mainnet=False)
    signed = Account._keys.PrivateKey(bytes.fromhex(priv[2:])).sign_msg_hash(digest)

    # Compare as integers: the SDK emits minimal hex (eth_utils.to_hex) while
    # the runner emits fixed-width 32-byte hex. Same value, different text —
    # see test_runner_emits_zero_padded_hex_where_sdk_does_not.
    assert signed.r == int(sdk_sig["r"], 16)
    assert signed.s == int(sdk_sig["s"], 16)
    assert signed.v + 27 == sdk_sig["v"]


def test_runner_emits_zero_padded_hex_where_sdk_does_not():
    """Document the one wire difference from the reference client.

    `__SIG_R__`/`__SIG_S__` are filled by the runner as
    `format!("0x{}", hex::encode(&sig_bytes[..32]))` — always 64 hex chars. The
    Python SDK uses `eth_utils.to_hex`, which strips leading zeros. So for the
    ~1-in-256 signature whose r or s has a zero high byte, the runner sends
    `0x0e0d…` where the SDK would send `0xe0d…`.

    Both decode to the same integer and standard EVM tooling emits the padded
    form, so this is expected to be a non-issue — but it is the one formatting
    difference from the client HL actually tests against, and it is only
    *observable* on that 1-in-256 signature. Worth confirming on testnet.
    """
    from eth_account import Account

    priv = "0x" + "11" * 32
    wallet = Account.from_key(priv)
    nonce = 1700000000000
    sdk_sig = signing.sign_l1_action(wallet, LIMIT_ORDER, None, nonce, None, False)

    # This fixture happens to produce an s with a leading zero byte.
    assert len(sdk_sig["s"]) == 65, "fixture no longer exercises the padding case"
    runner_s = "0x" + int(sdk_sig["s"], 16).to_bytes(32, "big").hex()
    assert len(runner_s) == 66
    assert runner_s != sdk_sig["s"]
    assert int(runner_s, 16) == int(sdk_sig["s"], 16)


# ── header shape the runner expects ───────────────────────────────────────────


def test_sign_headers_shape():
    headers = hl_digest.sign_headers(LIMIT_ORDER, 1700000000000)
    assert headers["X-Cowboy-Sign-Key"] == "${HL_AGENT_KEY}"
    digest_hdr = headers["X-Cowboy-Sign-Digest"]
    assert digest_hdr.startswith("0x")
    assert len(digest_hdr) == 66  # 0x + 64 hex chars
