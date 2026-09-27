"""Minimal runnable rebalancing example with deterministic local logic."""

import json
from cowboy_sdk import OWNER, actor, callable_by, public, pure, runtime


@actor
class SimpleRebalancingAgent:
    def init(self, payload):
        owner = runtime.assume_ownership_if_unowned()
        self.storage["__name__"] = "Simple Rebalancing Agent"
        self.storage["__description__"] = "Minimal portfolio rebalancing logic"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["owner"] = owner.hex()
        self.storage["pool_address"] = str(obj.get("pool_address", "demo-pool"))
        self.storage["threshold_bps"] = int(obj.get("threshold_bps", 500))
        self.storage["check_count"] = 0
        self.storage["last_check"] = {"status": "idle"}
        runtime.emit_event("simple_rebalancer.init", self.storage["last_check"])
        return b"ok: initialized"

    @callable_by(OWNER, decode="cbor")
    def rebalance_if_needed(self, payload):
        obj = payload or {}
        current_ratio = float(obj.get("current_ratio", 0.0))
        average_ratio = float(obj.get("average_ratio", 0.0))
        if average_ratio <= 0:
            result = {"status": "error", "error": "average_ratio must be positive"}
            self.storage["last_check"] = result
            return result

        threshold_bps = int(self.storage.get("threshold_bps", 500))
        drift_bps = int(((current_ratio - average_ratio) / average_ratio) * 10_000)
        action = "hold"
        if drift_bps <= -threshold_bps:
            action = "increase_eth"
        elif drift_bps >= threshold_bps:
            action = "decrease_eth"

        self.storage["check_count"] = int(self.storage.get("check_count", 0)) + 1
        result = {
            "status": "evaluated",
            "pool_address": self.storage.get("pool_address", ""),
            "current_ratio": current_ratio,
            "average_ratio": average_ratio,
            "drift_bps": drift_bps,
            "threshold_bps": threshold_bps,
            "action": action,
        }
        self.storage["last_check"] = result
        runtime.emit_event("simple_rebalancer.checked", result)
        return result

    @pure
    @public(decode="cbor")
    def get_last_check(self, payload):
        return self.storage.get("last_check", {"status": "idle"})

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        return {
            "pool_address": self.storage.get("pool_address", ""),
            "owner": self.storage.get("owner", ""),
            "threshold_bps": int(self.storage.get("threshold_bps", 500)),
            "check_count": int(self.storage.get("check_count", 0)),
        }


def _get_actor():
    return SimpleRebalancingAgent()


def init(payload):
    return _get_actor().init(payload)


def rebalance_if_needed(payload):
    return _get_actor().rebalance_if_needed(payload)


def get_last_check(payload):
    return _get_actor().get_last_check(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
