import json
from types import SimpleNamespace

import pytest
from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.summarizer.main import SYSTEM_PROMPT, ContractSummarizer

SOURCE = "function mint(address to,uint256 amount) external onlyOwner { _mint(to, amount); }"


def test_init_stores_profile() -> None:
    actor = ContractSummarizer()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"profile_name":"evm-risk"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("contract.init", {"profile_name": "evm-risk"})
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"profile_name": "evm-risk", "analysis_count": 0}
    assert codec.decode(chain.call(actor.get_last_summary, codec.encode({}))) == {"status": "idle"}


def test_analyze_contract_records_request_and_dispatches_llm_job() -> None:
    actor = ContractSummarizer()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"profile_name":"evm-risk"}')
    chain.call(actor.analyze_contract, codec.encode({"address": "0x1234", "source": SOURCE}))

    assert chain.state("analysis_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["profile_name"] == "evm-risk"
    assert prompt["address"] == "0x1234"
    assert prompt["source_excerpt"] == SOURCE
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 260}
    assert job["reply_handler"] == "_analyze_contract_job__resume"


def test_finalize_contract_summary_parses_fenced_json() -> None:
    actor = ContractSummarizer()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    analysis = SimpleNamespace(
        text='```json\n{"risk_score": 80, "recommendation": "avoid", "red_flags": ["owner mint"], "summary": "risky"}\n```'
    )
    summary = codec.decode(actor._finalize_contract_summary({"analysis_id": 1, "address": "0x1234"}, analysis))
    assert summary == {
        "analysis_id": 1,
        "address": "0x1234",
        "risk_score": 80,
        "recommendation": "avoid",
        "red_flags": ["owner mint"],
        "summary": "risky",
    }
    assert codec.decode(chain.state("last_summary")) == summary
    assert chain.events()[-1][0] == "contract.result"


def test_finalize_contract_summary_rejects_prose() -> None:
    actor = ContractSummarizer()
    SimulatedChain(actor).call(actor.init, b"{}")
    with pytest.raises(ValueError, match="did not return JSON"):
        actor._finalize_contract_summary({"analysis_id": 1}, SimpleNamespace(text="Looks like a rug pull."))
