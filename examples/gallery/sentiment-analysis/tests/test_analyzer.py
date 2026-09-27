import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.analyzer.main import SYSTEM_PROMPT, SentimentAnalyzer


def test_init_stores_topic_and_threshold() -> None:
    actor = SentimentAnalyzer()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"topic":"ETH","alert_threshold":-50}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("sentiment.init", {"topic": "ETH"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"topic": "ETH", "alert_threshold": -50, "request_count": 0}
    assert codec.decode(chain.call(actor.get_last_report, codec.encode({}))) == {"status": "idle"}


def test_analyze_sentiment_counts_and_dispatches_llm_job() -> None:
    actor = SentimentAnalyzer()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"topic":"ETH"}')
    texts = ["ETH dev activity looks strong.", "Macro still looks shaky."]
    chain.call(actor.analyze_sentiment, codec.encode({"texts": texts}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["request_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "_analyze_sentiment_job__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 220}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["topic"] == "ETH"
    assert prompt["texts"] == texts


def test_finalize_report_triggers_alert_at_threshold() -> None:
    actor = SentimentAnalyzer()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"topic":"ETH","alert_threshold":-50}')
    report = codec.decode(actor._finalize_sentiment_report({"request_id": 1}, RunnerResult('{"sentiment_score": -50, "outlook": "bearish", "summary": "Risk-off."}')))
    assert report == {"request_id": 1, "topic": "ETH", "sentiment_score": -50, "outlook": "bearish", "summary": "Risk-off.", "alert_triggered": True}
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("sentiment.result", report)
    assert codec.decode(chain.call(actor.get_last_report, codec.encode({}))) == report

    calm = codec.decode(actor._finalize_sentiment_report({"request_id": 2}, RunnerResult('{"sentiment_score": 20}')))
    assert calm["outlook"] == "neutral"
    assert calm["alert_triggered"] is False
