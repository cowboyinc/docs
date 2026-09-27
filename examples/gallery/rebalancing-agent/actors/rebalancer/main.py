"""Deterministic rebalancing example using the runtime-supported actor API."""

import json
from cowboy_sdk import OWNER, actor, callable_by, public, pure, runtime


@actor
class RebalancingAgent:
    def init(self, payload):
        owner = runtime.assume_ownership_if_unowned()
        self.storage["__name__"] = "Rebalancing Agent"
        self.storage["__description__"] = "Portfolio rebalancing agent with target allocations"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["owner"] = owner.hex()
        self.storage["pool_address"] = str(obj.get("pool_address", "demo-pool"))
        self.storage["drift_threshold_bps"] = int(obj.get("drift_threshold_bps", 500))
        self.storage["request_count"] = 0
        self.storage["last_decision"] = {
            "status": "idle",
            "pool_address": self.storage["pool_address"],
            "threshold_bps": int(self.storage["drift_threshold_bps"]),
        }
        runtime.emit_event("rebalancer.init", self.storage["last_decision"])
        return b"ok: initialized"

    @callable_by(OWNER, decode="cbor")
    def check_rebalance(self, payload):
        obj = payload or {}
        current_ratio = float(obj.get("current_ratio", 0.0))
        average_ratio = float(obj.get("average_ratio", 0.0))
        if average_ratio <= 0:
            decision = {"status": "error", "error": "average_ratio must be positive"}
            self.storage["last_decision"] = decision
            return decision

        request_id = int(self.storage.get("request_count", 0)) + 1
        self.storage["request_count"] = request_id
        drift_bps = int(((current_ratio - average_ratio) / average_ratio) * 10_000)
        threshold = int(self.storage.get("drift_threshold_bps", 500))
        action = "hold"
        amount_pct = 0

        if drift_bps <= -threshold:
            action = "increase_eth"
            amount_pct = min(25, max(5, abs(drift_bps) // 100))
        elif drift_bps >= threshold:
            action = "decrease_eth"
            amount_pct = min(25, max(5, abs(drift_bps) // 100))

        if action == "hold":
            status = "within_threshold"
            confidence = 90
            rationale = "Current ratio is within the configured drift threshold."
        else:
            status = "analyzed"
            confidence = 75
            rationale = f"Drift of {drift_bps} bps exceeds threshold {threshold} bps."

        decision = {
            "request_id": request_id,
            "status": status,
            "pool_address": self.storage.get("pool_address", ""),
            "current_ratio": current_ratio,
            "average_ratio": average_ratio,
            "drift_bps": drift_bps,
            "recommended_action": action,
            "amount_pct": amount_pct,
            "confidence": confidence,
            "rationale": rationale,
        }
        self.storage["last_decision"] = decision
        runtime.emit_event("rebalancer.decision", decision)
        return decision

    @pure
    @public(decode="cbor")
    def get_last_decision(self, payload):
        return self.storage.get("last_decision", {"status": "idle"})

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        stats = {
            "owner": self.storage.get("owner", ""),
            "pool_address": self.storage.get("pool_address", ""),
            "threshold_bps": int(self.storage.get("drift_threshold_bps", 500)),
            "request_count": int(self.storage.get("request_count", 0)),
        }
        return stats


def _get_actor():
    return RebalancingAgent()


def init(payload):
    return _get_actor().init(payload)


def check_rebalance(payload):
    return _get_actor().check_rebalance(payload)


def get_last_decision(payload):
    return _get_actor().get_last_decision(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
