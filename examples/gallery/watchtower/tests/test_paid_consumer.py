import json
import sys
from pathlib import Path

import cbor2

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXAMPLE_DIR / "tools"))

import edgar_poller
import paid_consumer
import watchtower_crypto as crypto


def test_xchacha_roundtrip_and_paid_message_decrypts_to_cluster_plaintext():
    wallet = bytes.fromhex("01" + "00" * 31)
    stream_secret = bytes.fromhex("77" * 32)
    stream_id = b"form4/cluster"
    cluster = edgar_poller.cluster_buy_signals(edgar_poller.load_fixture_events())[0]
    plaintext = json.dumps(cluster, sort_keys=True, separators=(",", ":")).encode()

    message_cbor = crypto.build_paid_payload(
        wallet_private_key=wallet,
        stream_secret=stream_secret,
        chain_id=9999,
        stream_id=stream_id,
        sequence=1,
        key_epoch=1,
        plaintext=plaintext,
        timestamp_unix_ms=1770000000000,
        nonce24=bytes.fromhex("24" * 24),
    )
    content_key = crypto.derive_content_key(stream_secret, 9999, stream_id, 1)
    decoded = paid_consumer.decrypt_paid_message(
        message_cbor, content_key, crypto.publisher_public_key(wallet)
    )
    assert decoded["ticker"] == "AAPL"
    assert decoded["action"] == "BUY"

    wrong_epoch_key = crypto.derive_content_key(stream_secret, 9999, stream_id, 2)
    try:
        paid_consumer.decrypt_paid_message(
            message_cbor,
            wrong_epoch_key,
            crypto.publisher_public_key(wallet),
        )
    except Exception:
        pass
    else:
        raise AssertionError("paid message should not decrypt with another epoch's content key")

    tampered = cbor2.loads(message_cbor)
    tampered["payload_hash"] = bytes.fromhex("00" * 32)
    try:
        paid_consumer.decrypt_paid_message(
            cbor2.dumps(tampered, canonical=True),
            content_key,
            crypto.publisher_public_key(wallet),
        )
    except ValueError as exc:
        assert "payload_hash" in str(exc)
    else:
        raise AssertionError("tampered payload_hash should fail")


def test_first_buy_signal_projects_cluster_for_actor():
    cluster = edgar_poller.cluster_buy_signals(edgar_poller.load_fixture_events())
    signal = paid_consumer.first_buy_signal(cluster)
    assert signal["action"] == "BUY"
    assert signal["ticker"] == "AAPL"
    assert signal["event_count"] == 2


def test_parse_fetch_key_output():
    key = paid_consumer.parse_fetch_key_output("Recovered\ncontent_key=0x" + "aa" * 32 + "\n")
    assert key == bytes.fromhex("aa" * 32)


def test_paid_init_payload_uses_subscriber_paid_mode():
    wallet = bytes.fromhex("01" + "00" * 31)
    payload = crypto.build_init_payload(
        name="form4-paid",
        description="fixture",
        stream_id=b"form4/paid",
        publisher_public_key=crypto.publisher_public_key(wallet),
        access_mode="SUBSCRIBER_PAID",
        ring_buffer_capacity=16,
    )
    decoded = cbor2.loads(payload)
    assert decoded["stream_id"] == b"form4/paid"
    assert decoded["initial_publisher_key"] == crypto.publisher_public_key(wallet)
    assert decoded["access_mode"] == "SUBSCRIBER_PAID"
