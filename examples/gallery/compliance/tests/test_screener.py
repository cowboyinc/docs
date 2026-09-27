import json
from types import SimpleNamespace

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.screener.main import SYSTEM_PROMPT, SanctionsFilter


def test_init_loads_default_lists() -> None:
    actor = SanctionsFilter()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b"{}") == b"ok: initialized"
    name, data = chain.events()[0]
    assert name == "sanctions.init"
    # Storage round-trips through canonical CBOR, so the list names come back sorted by length.
    assert sorted(codec.decode(data)["lists"]) == ["eu", "ofac", "un"]
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["screen_count"] == 0 and sorted(stats["lists"]) == ["eu", "ofac", "un"]


def test_screen_entity_clears_unlisted_query_without_runner() -> None:
    actor = SanctionsFilter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    result = codec.decode(chain.call(actor.screen_entity, codec.encode({"query": "0xCAFE"})))
    assert result == {"screen_id": 1, "query": "0xcafe", "status": "clear", "matches": []}
    assert codec.decode(chain.state("last_screen")) == result
    assert mock_host.get_send_log() == []
    assert chain.events()[-1][0] == "sanctions.screened"


def test_screen_entity_match_dispatches_llm_review() -> None:
    actor = SanctionsFilter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    chain.call(actor.screen_entity, codec.encode({"query": "0xdeadbeef"}))

    assert chain.state("screen_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["query"] == "0xdeadbeef"
    assert prompt["matches"] == [
        {"list": "ofac", "entry": {"type": "address", "value": "0xdeadbeef", "name": "Demo OFAC Entity"}}
    ]
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 180}
    assert job["reply_handler"] == "_screen_entity_job__resume"
    # The runner never answers in the simulator, so the screen is still idle.
    assert codec.decode(chain.call(actor.get_last_screen, codec.encode({}))) == {"status": "idle"}


def test_update_lists_and_finalize_screen() -> None:
    actor = SanctionsFilter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    lists = {"uk": [{"type": "name", "value": "ACME", "name": "ACME"}]}
    updated = codec.decode(chain.call(actor.update_lists, codec.encode({"lists": lists})))
    assert updated == {"updated": True, "lists": ["uk"]}

    request = {"screen_id": 2, "query": "acme", "matches": [{"list": "uk", "entry": lists["uk"][0]}]}
    review = SimpleNamespace(text='```json\n{"verdict": "blocked", "confidence": 95, "note": "exact"}\n```')
    screen = codec.decode(actor._finalize_screen(request, review))
    assert screen["status"] == "blocked" and screen["confidence"] == 95 and screen["note"] == "exact"
    assert screen["matches"] == request["matches"]
    assert codec.decode(chain.state("last_screen")) == screen
