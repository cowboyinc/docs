import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.wallet.main import SYSTEM_PROMPT, WalletCopyTrader

INIT = b'{"source_wallet":"0xbarrondemo","destination_book":"paper-book","max_notional_usd":5000}'


def test_init_stores_wallet_and_limits() -> None:
    actor = WalletCopyTrader()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, INIT) == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("wallet_copy.init", {"source_wallet": "0xbarrondemo"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {
        "source_wallet": "0xbarrondemo",
        "destination_book": "paper-book",
        "max_notional_usd": 5000,
        "trade_count": 0,
    }
    assert codec.decode(chain.call(actor.get_last_trade, codec.encode({}))) == {"status": "idle"}


def test_ingest_trade_counts_and_dispatches_llm_job() -> None:
    actor = WalletCopyTrader()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    chain.call(actor.ingest_trade, codec.encode({"ticker": "aapl", "side": "SELL", "source_size_usd": 4000, "urgency": "high", "signal_text": "Trimmed position."}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["trade_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "ingest_trade__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 160}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["source_wallet"] == "0xbarrondemo"
    assert prompt["ticker"] == "AAPL"
    assert prompt["side"] == "sell"
    assert prompt["urgency"] == "high"
    assert prompt["notes"] == "Trimmed position."


def test_finalize_trade_caps_size_at_max_notional() -> None:
    actor = WalletCopyTrader()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    request = {"trade_id": 1, "ticker": "AAPL", "side": "buy", "source_size_usd": 9000}
    analysis = RunnerResult('{"copy_trade": true, "target_size_usd": 9000, "confidence": 64, "note": "Mirror at open."}')
    trade = codec.decode(actor._finalize_trade(request, analysis))
    assert trade == {
        "trade_id": 1,
        "source_wallet": "0xbarrondemo",
        "destination_book": "paper-book",
        "ticker": "AAPL",
        "side": "buy",
        "copy_trade": True,
        "target_size_usd": 5000,
        "confidence": 64,
        "note": "Mirror at open.",
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("wallet_copy.trade", trade)
    assert codec.decode(chain.call(actor.get_last_trade, codec.encode({}))) == trade
