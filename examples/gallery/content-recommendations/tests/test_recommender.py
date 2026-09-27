import json
from types import SimpleNamespace

import pytest
from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.recommender.main import SYSTEM_PROMPT, ContentRecommender

CANDIDATES = [
    {"title": "Cowboy AI Agents", "tags": ["ai", "infra"]},
    {"title": "Sports Roundup", "tags": ["sports"]},
]


def test_init_stores_catalog_name() -> None:
    actor = ContentRecommender()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"catalog_name":"demo-catalog"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("recommend.init", {"catalog_name": "demo-catalog"})
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"catalog_name": "demo-catalog", "request_count": 0}
    assert codec.decode(chain.call(actor.get_last_recommendation, codec.encode({}))) == {"status": "idle"}


def test_recommend_content_records_request_and_dispatches_llm_job() -> None:
    actor = ContentRecommender()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"catalog_name":"demo-catalog"}')
    profile = {"likes": ["ai"], "reading_time": "short"}
    chain.call(actor.recommend_content, codec.encode({"profile": profile, "candidates": CANDIDATES}))

    assert chain.state("request_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["profile"] == profile
    assert prompt["candidates"] == CANDIDATES
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 260}
    assert job["reply_handler"] == "_recommend_content_job__resume"


def test_finalize_recommend_content_parses_fenced_json() -> None:
    actor = ContentRecommender()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"catalog_name":"demo-catalog"}')
    picks = [{"title": "Cowboy AI Agents", "score": 0.9, "reason": "matches ai"}]
    analysis = SimpleNamespace(text="```json\n" + json.dumps({"recommendations": picks}) + "\n```")
    recommendation = codec.decode(actor._finalize_recommend_content({"request_id": 1}, analysis))
    assert recommendation == {"request_id": 1, "catalog_name": "demo-catalog", "recommendations": picks}
    assert codec.decode(chain.state("last_recommendation")) == recommendation
    assert chain.events()[-1][0] == "recommend.result"


def test_finalize_recommend_content_rejects_non_object() -> None:
    actor = ContentRecommender()
    SimulatedChain(actor).call(actor.init, b"{}")
    with pytest.raises(ValueError, match="JSON object"):
        actor._finalize_recommend_content({"request_id": 1}, SimpleNamespace(text='["Cowboy AI Agents"]'))
