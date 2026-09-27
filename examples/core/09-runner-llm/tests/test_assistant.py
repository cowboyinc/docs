from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.assistant.main import SYSTEM_PROMPT, RunnerLLMActor

DEPLOYER = "0x" + "11" * 20


def test_init_claims_owner_and_sets_config() -> None:
    actor = RunnerLLMActor()
    chain = SimulatedChain(actor, sender=DEPLOYER)
    assert chain.call(actor.init, b"{}") == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("llm.init", {"owner": DEPLOYER})
    config = codec.decode(chain.call(actor.get_config, codec.encode({})))
    assert config == {"model_name": "llama", "max_tokens": 256, "temperature": 0.2, "request_count": 0}
    assert chain.call(actor.init, b"{}") == b"ok: already initialized"


def test_ask_llm_records_pending_prompt_and_dispatches_runner_job() -> None:
    actor = RunnerLLMActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    chain.call(actor.ask_llm, codec.encode({"prompt": "What is a continuation?"}))

    assert chain.state("llm:0:prompt") == codec.encode("What is a continuation?")
    assert chain.state("llm:0:status") == codec.encode("pending")
    config = codec.decode(chain.call(actor.get_config, codec.encode({})))
    assert config["request_count"] == 1

    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    assert job["payload"]["args"] == ["What is a continuation?"]
    assert job["payload"]["kwargs"] == {
        "system_prompt": SYSTEM_PROMPT,
        "max_tokens": 256,
        "temperature": 0.2,
    }
    assert job["reply_handler"] == "ask_llm__resume"


def test_get_response_before_any_result() -> None:
    actor = RunnerLLMActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    assert chain.call(actor.get_response, codec.encode({})) == b"no responses yet"
    chain.call(actor.ask_llm, codec.encode({}))
    assert chain.call(actor.get_response, codec.encode({"request_id": 0})) == b"error: response not found"
