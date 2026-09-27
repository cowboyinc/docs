import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.disclosure.main import SYSTEM_PROMPT, CongressionalCopyTrader

INIT = b'{"target_member":"Nancy Pelosi","broker":"paper-broker","max_position_usd":10000}'


def test_init_stores_portfolio_limits() -> None:
    actor = CongressionalCopyTrader()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, INIT) == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("copy_trader.init", {"member": "Nancy Pelosi"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {
        "target_member": "Nancy Pelosi",
        "broker": "paper-broker",
        "max_position_usd": 10000,
        "trade_count": 0,
    }
    assert codec.decode(chain.call(actor.get_last_trade, codec.encode({}))) == {"status": "idle"}


def test_ingest_disclosure_counts_and_dispatches_llm_job() -> None:
    actor = CongressionalCopyTrader()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    chain.call(actor.ingest_disclosure, codec.encode({"ticker": "nvda", "side": "BUY", "reported_size_usd": 18000, "delay_days": 34, "filing_text": "PTR shows a purchase."}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["trade_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "ingest_disclosure__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 180}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["member"] == "Nancy Pelosi"
    assert prompt["ticker"] == "NVDA"
    assert prompt["side"] == "buy"
    assert prompt["reported_size_usd"] == 18000
    assert prompt["context"] == "PTR shows a purchase."


def test_finalize_trade_caps_size_at_max_position() -> None:
    actor = CongressionalCopyTrader()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    disclosure = {"trade_id": 1, "ticker": "NVDA", "side": "buy", "reported_size_usd": 18000, "delay_days": 34}
    analysis = RunnerResult('{"follow_trade": true, "size_usd": 18000, "confidence": 72, "thesis": "Semis momentum."}')
    trade = codec.decode(actor._finalize_disclosure_trade(disclosure, analysis))
    assert trade == {
        "trade_id": 1,
        "member": "Nancy Pelosi",
        "broker": "paper-broker",
        "ticker": "NVDA",
        "side": "buy",
        "follow_trade": True,
        "size_usd": 10000,
        "confidence": 72,
        "thesis": "Semis momentum.",
        "delay_days": 34,
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("copy_trader.trade", trade)
    assert codec.decode(chain.call(actor.get_last_trade, codec.encode({}))) == trade
