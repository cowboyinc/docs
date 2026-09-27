import json
from types import SimpleNamespace

import pytest
from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.moderator.main import SYSTEM_PROMPT, ContentModerator


def test_init_stores_policy() -> None:
    actor = ContentModerator()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"policy_name":"community-default"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("moderator.init", {"policy_name": "community-default"})
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"policy_name": "community-default", "case_count": 0, "last_appeal": {"status": "none"}}


def test_moderate_content_records_case_and_dispatches_llm_job() -> None:
    actor = ContentModerator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"policy_name":"community-default"}')
    chain.call(actor.moderate_content, codec.encode({"content_type": "post", "content": "You are all idiots."}))

    assert chain.state("case_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["policy_name"] == "community-default"
    assert prompt["content_type"] == "post"
    assert prompt["content"] == "You are all idiots."
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 180}
    assert job["reply_handler"] == "_moderate_content_job__resume"
    # The runner never answers in the simulator, so the case is still idle.
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == {"status": "idle"}


def test_submit_appeal_records_last_appeal() -> None:
    actor = ContentModerator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    appeal = codec.decode(chain.call(actor.submit_appeal, codec.encode({"case_id": 1, "reason": "context"})))
    assert appeal == {"case_id": 1, "reason": "context", "status": "received"}
    assert chain.events()[-1][0] == "moderator.appeal"
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["last_appeal"] == appeal


def test_finalize_moderation_parses_fenced_json_and_rejects_prose() -> None:
    actor = ContentModerator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    request = {"case_id": 1, "content": "spam spam", "content_type": "comment"}
    review = SimpleNamespace(
        text='```json\n{"category": "spam", "action": "remove", "severity": 3, "rationale": "repeated"}\n```'
    )
    case = codec.decode(actor._finalize_moderation(request, review))
    assert case == {
        "case_id": 1,
        "content_type": "comment",
        "category": "spam",
        "action": "remove",
        "severity": 3,
        "rationale": "repeated",
    }
    assert codec.decode(chain.state("last_case")) == case
    assert chain.events()[-1][0] == "moderator.result"
    with pytest.raises(ValueError, match="did not return JSON"):
        actor._finalize_moderation(request, SimpleNamespace(text="This post is fine."))
