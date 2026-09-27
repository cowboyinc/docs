from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.simple_rebalancer.main import SimpleRebalancingAgent


def _agent():
    actor = SimpleRebalancingAgent()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"pool_address":"demo-pool","threshold_bps":500}') == b"ok: initialized"
    return actor, chain


def test_positive_drift_beyond_threshold_decreases_eth() -> None:
    actor, chain = _agent()
    result = codec.decode(chain.call(actor.rebalance_if_needed, codec.encode({"current_ratio": 1.10, "average_ratio": 1.0})))
    assert result["drift_bps"] == 1000
    assert result["action"] == "decrease_eth"
    assert codec.decode(chain.call(actor.get_last_check, codec.encode({}))) == result


def test_small_drift_holds_and_counts_checks() -> None:
    actor, chain = _agent()
    result = codec.decode(chain.call(actor.rebalance_if_needed, codec.encode({"current_ratio": 0.99, "average_ratio": 1.0})))
    assert result["action"] == "hold"
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"pool_address": "demo-pool", "owner": stats["owner"], "threshold_bps": 500, "check_count": 1}


def test_nonpositive_average_ratio_is_an_error() -> None:
    actor, chain = _agent()
    result = codec.decode(chain.call(actor.rebalance_if_needed, codec.encode({"current_ratio": 1.0, "average_ratio": 0})))
    assert result == {"status": "error", "error": "average_ratio must be positive"}
    assert codec.decode(chain.call(actor.get_last_check, codec.encode({}))) == result
