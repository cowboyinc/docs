import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.copilot.main import SYSTEM_PROMPT, DAOCopilot


def test_init_stores_dao_profile() -> None:
    actor = DAOCopilot()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"dao_name":"Cowboy DAO"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("dao.init", {"dao_name": "Cowboy DAO"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"dao_name": "Cowboy DAO", "proposal_count": 0}
    assert codec.decode(chain.call(actor.get_last_report, codec.encode({}))) == {"status": "idle"}


def test_analyze_proposal_counts_and_dispatches_llm_job() -> None:
    actor = DAOCopilot()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.analyze_proposal, codec.encode({"title": "Fund runner grants", "body": "Allocate 250k."}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"dao_name": "Cowboy DAO", "proposal_count": 1}
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "_analyze_proposal_job__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 220}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["dao_name"] == "Cowboy DAO"
    assert prompt["title"] == "Fund runner grants"
    assert prompt["body"] == "Allocate 250k."


def test_finalize_proposal_report_stores_fenced_llm_json() -> None:
    actor = DAOCopilot()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"dao_name":"Cowboy DAO"}')
    analysis = RunnerResult('```json\n{"recommendation": "approve", "confidence": 80, "risks": ["treasury drawdown"], "summary": "Fund it."}\n```')
    report = codec.decode(actor._finalize_proposal_report({"proposal_id": 1, "title": "Fund runner grants"}, analysis))
    assert report == {
        "proposal_id": 1,
        "dao_name": "Cowboy DAO",
        "title": "Fund runner grants",
        "recommendation": "approve",
        "confidence": 80,
        "risks": ["treasury drawdown"],
        "summary": "Fund it.",
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("dao.result", report)
    assert codec.decode(chain.call(actor.get_last_report, codec.encode({}))) == report
