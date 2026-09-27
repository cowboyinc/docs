import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.watcher.main import SYSTEM_PROMPT, FraudWatcher


def test_init_stores_threshold_and_counters() -> None:
    actor = FraudWatcher()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"risk_threshold":70}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("fraud.init", {"risk_threshold": 70})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"risk_threshold": 70, "case_count": 0, "flagged_count": 0}
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == {"status": "idle"}


def test_analyze_transaction_counts_and_dispatches_llm_job() -> None:
    actor = FraudWatcher()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.analyze_transaction, codec.encode({"from_addr": "0x1111", "to_addr": "0x2222", "amount": 9800, "context": "rapid withdrawals"}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["case_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "_analyze_transaction_job__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 220}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["transaction"] == {"case_id": 1, "from_addr": "0x1111", "to_addr": "0x2222", "amount": 9800.0, "context": "rapid withdrawals"}


def test_finalize_case_flags_scores_at_or_above_threshold() -> None:
    actor = FraudWatcher()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"risk_threshold":70}')
    high = RunnerResult('{"risk_score": 85, "risk_level": "high", "patterns": ["structuring"], "recommendation": "block"}')
    case = codec.decode(actor._finalize_transaction_case({"case_id": 1}, high))
    assert case == {"case_id": 1, "risk_score": 85, "risk_level": "high", "patterns": ["structuring"], "recommendation": "block"}
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("fraud.result", case)
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == case

    low = RunnerResult('{"risk_score": 12, "risk_level": "low", "patterns": [], "recommendation": "allow"}')
    actor._finalize_transaction_case({"case_id": 2}, low)
    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["flagged_count"] == 1
