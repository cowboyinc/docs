import json

from cowboy_sdk.testing import SimulatedChain

from actors.subscriber.main import WatchtowerSignalSubscriber

# handle_signal/latest carry no @public/@callable_by decorator, so the SDK's
# deny-by-default permission wrapper only admits intra-actor dispatch: call
# them as the actor itself (the mock host's actor address).
SELF = b"\x22" * 20


def test_init_records_owner() -> None:
    actor = WatchtowerSignalSubscriber()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b"") == b"ok: signal subscriber initialized"
    assert chain.events()[0][0] == "watchtower.signal_subscriber.initialized"


def test_handle_signal_stores_buy_and_latest_reads_it() -> None:
    actor = WatchtowerSignalSubscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.set_sender(SELF)
    signal = {"ticker": "AAPL", "action": "BUY", "event_count": 2}
    assert chain.call(actor.handle_signal, json.dumps(signal).encode()) == b"BUY AAPL"
    latest = json.loads(chain.call(actor.latest, b""))
    assert latest["ticker"] == "AAPL"
    assert latest["action"] == "BUY"


def test_handle_signal_rejects_non_buy() -> None:
    actor = WatchtowerSignalSubscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.set_sender(SELF)
    reply = chain.call(actor.handle_signal, b'{"ticker": "MSFT", "action": "SELL"}')
    assert reply == b"error: expected BUY signal with ticker"
