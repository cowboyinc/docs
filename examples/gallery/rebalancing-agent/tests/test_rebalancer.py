from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.rebalancer.main import RebalancingAgent


def _agent():
    actor = RebalancingAgent()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"pool_address":"demo-pool","drift_threshold_bps":500}') == b"ok: initialized"
    return actor, chain


def test_init_stores_config_and_idle_decision() -> None:
    actor, chain = _agent()
    assert codec.decode(chain.state("pool_address")) == "demo-pool"
    assert codec.decode(chain.state("drift_threshold_bps")) == 500
    idle = codec.decode(chain.call(actor.get_last_decision, codec.encode({})))
    assert idle["status"] == "idle"


def test_drift_below_negative_threshold_recommends_increase() -> None:
    actor, chain = _agent()
    decision = codec.decode(chain.call(actor.check_rebalance, codec.encode({"current_ratio": 0.91, "average_ratio": 1.0})))
    assert decision["drift_bps"] == -899  # int() truncates the float drift toward zero
    assert decision["recommended_action"] == "increase_eth"
    assert decision["amount_pct"] == 8
    assert decision["status"] == "analyzed"
    name, data = chain.events()[-1]
    assert name == "rebalancer.decision"
    assert codec.decode(data) == decision
    assert codec.decode(chain.call(actor.get_last_decision, codec.encode({}))) == decision


def test_drift_within_threshold_holds() -> None:
    actor, chain = _agent()
    decision = codec.decode(chain.call(actor.check_rebalance, codec.encode({"current_ratio": 1.02, "average_ratio": 1.0})))
    assert decision["recommended_action"] == "hold"
    assert decision["status"] == "within_threshold"
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["request_count"] == 1
    assert stats["threshold_bps"] == 500


def test_nonpositive_average_ratio_is_an_error() -> None:
    actor, chain = _agent()
    decision = codec.decode(chain.call(actor.check_rebalance, codec.encode({"current_ratio": 1.0, "average_ratio": 0})))
    assert decision == {"status": "error", "error": "average_ratio must be positive"}
