import json

from cowboy_sdk import actor, pure, runtime


def _payload(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8") if payload else "{}"
    try:
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


@actor
class WatchtowerSignalSubscriber:
    def _state(self):
        if self.storage.get("owner") is None:
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        if self.storage.get("signals") is None:
            self.storage["signals"] = []

    def init(self, payload=None):
        self._state()
        runtime.emit_event("watchtower.signal_subscriber.initialized", {"owner": self.storage["owner"]})
        return b"ok: signal subscriber initialized"

    def handle_signal(self, payload=None):
        self._state()
        signal = _payload(payload)
        ticker = str(signal.get("ticker", "")).upper()
        action = str(signal.get("action", "")).upper()
        if action != "BUY" or not ticker:
            return b"error: expected BUY signal with ticker"
        signals = list(self.storage.get("signals") or [])
        signals.append(signal)
        self.storage["signals"] = signals[-32:]
        runtime.emit_event("watchtower.buy_signal", {"action": "BUY", "ticker": ticker})
        return ("BUY " + ticker).encode("utf-8")

    @pure
    def latest(self, payload=None):
        self._state()
        signals = list(self.storage.get("signals") or [])
        return json.dumps(signals[-1] if signals else {}, sort_keys=True).encode("utf-8")


def _get_actor():
    return WatchtowerSignalSubscriber()


def init(payload=None):
    return _get_actor().init(payload)


def handle_signal(payload=None):
    return _get_actor().handle_signal(payload)


def latest(payload=None):
    return _get_actor().latest(payload)
