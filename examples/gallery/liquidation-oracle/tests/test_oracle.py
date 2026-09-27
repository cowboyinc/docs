import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.oracle.main import SYSTEM_PROMPT, LiquidationOracle


def test_init_stores_market_name() -> None:
    actor = LiquidationOracle()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"market_name":"demo-lending"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("liquidation.init", {"market_name": "demo-lending"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"market_name": "demo-lending", "case_count": 0}
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == {"status": "idle"}


def test_evaluate_account_counts_and_dispatches_llm_job() -> None:
    actor = LiquidationOracle()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.evaluate_account, codec.encode({"account": "0xabc123", "collateral_value": 100000, "debt_value": 76000, "liquidation_threshold": 0.8}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["case_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "_evaluate_account_job__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 180}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["account"] == "0xabc123"
    assert prompt["collateral_value"] == 100000.0
    assert prompt["debt_value"] == 76000.0
    assert prompt["liquidation_threshold"] == 0.8


def test_finalize_case_computes_health_ratio_and_defaults_risk_score() -> None:
    actor = LiquidationOracle()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"market_name":"demo-lending"}')
    request = {"case_id": 1, "account": "0xabc123", "collateral_value": 100000.0, "debt_value": 80000.0}
    case = codec.decode(actor._finalize_account_case(request, RunnerResult('{"recommendation": "warn"}')))
    assert case == {
        "case_id": 1,
        "account": "0xabc123",
        "market_name": "demo-lending",
        "health_ratio": 1.25,
        "risk_score": 38,
        "recommendation": "warn",
        "liquidation_price_note": "",
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("liquidation.result", case)
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == case
