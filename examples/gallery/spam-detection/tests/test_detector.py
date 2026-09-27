import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.detector.main import SYSTEM_PROMPT, SpamDetector


def test_init_stores_threshold() -> None:
    actor = SpamDetector()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"spam_threshold":70}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("spam.init", {"spam_threshold": 70})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"spam_threshold": 70, "message_count": 0}
    assert codec.decode(chain.call(actor.get_last_case, codec.encode({}))) == {"status": "idle"}


def test_scan_content_counts_and_dispatches_llm_job() -> None:
    actor = SpamDetector()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.scan_content, codec.encode({"channel": "discord", "content": "Guaranteed 100x returns."}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["message_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "scan_content__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 180}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["channel"] == "discord"
    assert prompt["content"] == "Guaranteed 100x returns."


def test_finalize_scan_stores_case_and_normalizes_reasons() -> None:
    actor = SpamDetector()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"spam_threshold":70}')
    raw = '{"spam_score": 92, "label": "spam", "action": "quarantine", "reasons": "urgency bait"}'
    assert actor._finalize_scan_content({"case_id": 1, "channel": "discord"}, RunnerResult(raw)) == b"ok: case stored"
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("spam.result", {"case_id": 1, "label": "spam", "spam_score": 92, "is_spam": True})
    case = codec.decode(chain.call(actor.get_last_case, codec.encode({})))
    assert case == {
        "case_id": 1,
        "channel": "discord",
        "spam_score": 92,
        "label": "spam",
        "action": "quarantine",
        "reasons": [],
        "threshold": 70,
        "is_spam": True,
        "raw_result": raw,
    }


def test_on_runner_result_rejects_unrecognized_callback() -> None:
    actor = SpamDetector()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert chain.call(actor.on_runner_result, codec.encode({"unrelated": 1})) == b"error: unrecognized callback"
    assert chain.call(actor.on_runner_result, b"\xff") == b"error: unrecognized callback"
