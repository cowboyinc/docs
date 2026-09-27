"""Focused tests for the RunnerResult migration.

These pin the canonical runner-result shapes that the examples now
depend on, and drive a representative example from each accessor family end to
end so the extraction + dict-guard produces the expected output.

`RunnerResult` / `extract_text` below are a faithful copy of the source of
truth on the node branch
`cow-2379-node-5-normalize-runner-result-type-runnerresult`:
  pvm/Lib/cowboy_sdk/runner.py  (extract_text + class RunnerResult)
Canonical shapes targeted:
  - LLM text             -> {"content": "<text>"}            (.text)
  - JSON-only LLM prompt -> {"content": "<JSON object str>"} (.json())
  - Agent                -> {"answer": "<text>", ...}        (.raw, or .text)
  - HTTP                 -> decoded dictionary               (.raw)

"answer" is last in the text-key walk, so an agent result reaches `.text` only
because it carries none of the earlier keys; the examples read `.raw` when they
want the sibling fields alongside the answer.

Run: python3 examples/test_runner_result_migration.py (or under pytest)
"""

import importlib.util
import json
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent


# ─── source-of-truth RunnerResult (mirrors node pvm/Lib/cowboy_sdk/runner.py) ──
_TEXT_KEYS = ("content", "text", "response", "data", "answer")


def extract_text(result) -> str:
    for _ in range(8):  # bounded: guard against degenerate/cyclic nesting
        if not isinstance(result, dict):
            break
        for key in _TEXT_KEYS:
            if key in result:
                result = result[key]
                break
        else:
            break
    if result is None:
        return ""
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", "replace")
    if isinstance(result, str):
        return result
    return str(result)


class RunnerResult:
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    @property
    def data(self):
        return self.raw

    @property
    def text(self) -> str:
        return extract_text(self.raw)

    def json(self):
        return json.loads(self.text)


# ─── minimal cowboy_sdk stub so example modules import + run in-process ────────
EVENTS = []      # [(name, payload)] emitted by the actor under test


def _install_fake_sdk():
    _ident = lambda f=None, *a, **k: (f if callable(f) else _ident)  # noqa: E731

    sdk = types.ModuleType("cowboy_sdk")
    sdk.actor = _ident
    sdk.pure = _ident
    sdk.public = lambda fn=None, *, decode="raw": (fn if fn is not None else _ident)
    sdk.capture = lambda: types.SimpleNamespace()
    sdk.Address = object

    runner = types.ModuleType("cowboy_sdk.runner")
    runner.continuation = _ident
    runner.RunnerResult = RunnerResult
    runner.extract_text = extract_text
    for job in ("llm", "http", "agent", "mcp", "container"):
        setattr(runner, job, lambda *a, **k: None)
    runner.handle_runner_result = lambda *a, **k: None
    sdk.runner = runner

    runtime = types.ModuleType("cowboy_sdk.runtime")
    def emit_event(name, payload=None, *, schema=None):
        EVENTS.append((name, payload))
        return {"emit_id": len(EVENTS), "sync_outcomes": [], "deferred_subs": []}

    runtime.emit_event = emit_event
    runtime.context = lambda: {}
    runtime.get_block_height = lambda: 100
    # alert_scheduler re-arms its own timer before returning its report.
    runtime.schedule_after = lambda delay, handler, payload=None, **opts: b"\xaa\xbb"
    sdk.runtime = runtime

    codec = types.ModuleType("cowboy_sdk.codec")
    codec.encode = lambda v: v          # identity so tests assert on the object
    codec.decode = lambda v: v
    sdk.codec = codec

    sys.modules["cowboy_sdk"] = sdk
    sys.modules["cowboy_sdk.runner"] = runner
    sys.modules["cowboy_sdk.runtime"] = runtime
    sys.modules["cowboy_sdk.codec"] = codec


def _load(path):
    # Reinstall right before every load — an actor module binds whichever stub
    # is live when it executes, and sibling test files install their own at
    # import time. Loading against theirs swaps the codec these tests assert on.
    _install_fake_sdk()
    spec = importlib.util.spec_from_file_location(
        "ex_" + pathlib.Path(path).stem, HERE / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _actor(module, cls_name):
    inst = getattr(module, cls_name).__new__(getattr(module, cls_name))
    inst.storage = {}
    return inst


# ─── unit: RunnerResult contracts on the canonical shapes ─────────────────────
def test_runner_result_canonical_contracts():
    # LLM text: {"content": "<text>"}
    assert RunnerResult({"content": "hi"}).text == "hi"
    # JSON-only LLM prompt: content is a JSON object string
    assert RunnerResult({"content": '{"a": 1, "b": "x"}'}).json() == {"a": 1, "b": "x"}
    # HTTP: decoded dictionary is .raw verbatim
    body = {"symbol": "BTC", "price": 100}
    assert RunnerResult(body).raw == body
    # agent: structured {"answer": "<text>", ...} — "answer" is a text key, so
    # .raw.get("answer") and .text agree. The examples read .raw when they want
    # the sibling fields too, and fall back to .text when .raw isn't a dict.
    agent = RunnerResult({"answer": "the answer", "tokens": 12})
    assert agent.raw.get("answer") == "the answer"
    assert agent.text == "the answer"
    # Precedence: "answer" is last, so an envelope carrying both yields content.
    assert RunnerResult({"content": "hi", "answer": "the answer"}).text == "hi"
    print("ok test_runner_result_canonical_contracts")


def test_non_object_canonical_result_is_rejected():
    # The examples require a dict before .get(), so a non-object canonical
    # result is always an error — but HOW it surfaces depends on the example.
    # A self-contained one (sentiment, oracle cache) raises; one holding a
    # single-flight lock takes a terminal path that releases it instead of
    # raising (see test_non_object_runner_results.py).
    # HTTP body that is not a decoded object:
    assert not isinstance(RunnerResult("plain text").raw, dict)
    # JSON-only LLM returning a non-object (bare string / array / number):
    assert not isinstance(RunnerResult({"content": '"just a string"'}).json(), dict)
    assert not isinstance(RunnerResult({"content": "[1, 2, 3]"}).json(), dict)
    print("ok test_non_object_canonical_result_is_rejected")


# ─── integration: drive one example per accessor family ───────────────────────
def test_sentiment_json_llm_equivalence():
    m = _load("gallery/sentiment-analysis/sentiment_analyzer.py")
    a = _actor(m, "SentimentAnalyzer")
    a.storage["topic"] = "ETH"
    a.storage["alert_threshold"] = -50
    canonical = RunnerResult({"content": json.dumps(
        {"sentiment_score": -75, "outlook": "bearish", "summary": "grim"})})
    report = a._finalize_sentiment_report({"request_id": 7}, canonical)
    assert report["sentiment_score"] == -75, report
    assert report["outlook"] == "bearish", report
    assert report["summary"] == "grim", report
    assert report["alert_triggered"] is True, report
    assert a.storage["last_report"] == report

    # non-object canonical result -> clear ValueError
    try:
        a._finalize_sentiment_report({}, RunnerResult({"content": '"nope"'}))
    except ValueError as e:
        assert "JSON object" in str(e)
    else:
        raise AssertionError("expected ValueError for non-object LLM result")
    print("ok test_sentiment_json_llm_equivalence")


def test_oracle_cache_http_raw_equivalence():
    m = _load("core/10-async-cache-refresh/oracle_cache.py")
    a = _actor(m, "OracleCache")
    canonical = RunnerResult({"symbol": "btc", "price": 64000, "as_of": "t1"})
    resp = a._finalize_refresh({"refresh_count": 3}, canonical)
    assert resp["record"] == {"symbol": "BTC", "price": 64000, "as_of": "t1"}, resp
    assert resp["refresh_count"] == 3, resp
    assert a.storage["cache"]["symbol"] == "BTC"

    # non-object HTTP result -> clear ValueError
    try:
        a._finalize_refresh({}, RunnerResult("not-json"))
    except ValueError as e:
        assert "JSON object" in str(e)
    else:
        raise AssertionError("expected ValueError for non-object HTTP result")
    print("ok test_oracle_cache_http_raw_equivalence")


def test_spam_detector_text_and_json():
    m = _load("gallery/spam-detection/spam_detector.py")
    a = _actor(m, "SpamDetector")
    a.storage["spam_threshold"] = 70
    canonical = RunnerResult({"content": json.dumps(
        {"spam_score": 91, "label": "spam", "action": "block", "reasons": ["links"]})})
    out = a._finalize_scan_content({"case_id": 5, "channel": "sms"}, canonical)
    assert out == b"ok: case stored", out
    assert a.storage["last_case_spam_score"] == 91
    assert a.storage["last_case_label"] == "spam"
    assert a.storage["last_case_is_spam"] is True
    # .text preserves the raw model text for the audit trail
    assert '"spam_score": 91' in a.storage["last_case_raw_result"]
    print("ok test_spam_detector_text_and_json")


def test_fenced_completion_is_parsed_not_rejected():
    """"Respond with JSON only" is a request, not a guarantee — models wrap
    JSON in ``` fences anyway, so the parse tolerates them."""
    m = _load("gallery/spam-detection/spam_detector.py")
    a = _actor(m, "SpamDetector")
    a.storage["spam_threshold"] = 70
    payload = json.dumps({"spam_score": 91, "label": "spam", "action": "block"})
    # spam_detector and content_moderator ask the model to echo the offending
    # content back, so a fence inside a string value is not exotic.
    echoes_a_fence = json.dumps(
        {"spam_score": 91, "reasons": ["pastes ``` code fences ```"]})
    for text in ("```json\n" + payload + "\n```",
                 "```\n" + payload + "\n```",
                 "```json\n" + payload,           # unterminated fence
                 "```jsonc\n" + payload + "\n```",     # any info string
                 "```json\n" + echoes_a_fence + "\n```"):
        a.storage["last_case_spam_score"] = 0
        a._finalize_scan_content({"case_id": 5}, RunnerResult({"content": text}))
        assert a.storage["last_case_spam_score"] == 91, text
        # The audit trail keeps the model's text verbatim, fences included.
        assert a.storage["last_case_raw_result"] == text, text
    print("ok test_fenced_completion_is_parsed_not_rejected")


def test_unparseable_completion_names_the_actor():
    """A prose reply raises inside json.loads, before any check on the parsed
    value — so the parse itself has to supply the context."""
    m = _load("gallery/spam-detection/spam_detector.py")
    a = _actor(m, "SpamDetector")
    for text, expected in (("I cannot classify this message.", "did not return JSON"),
                           ("", "did not return JSON"),
                           ("[1, 2]", "did not return a JSON object")):
        try:
            a._finalize_scan_content({}, RunnerResult({"content": text}))
        except ValueError as e:
            assert "spam detector" in str(e), e
            assert expected in str(e), e
        else:
            raise AssertionError("expected ValueError for " + repr(text))
    print("ok test_unparseable_completion_names_the_actor")


def test_scheduler_survives_an_unparseable_completion():
    """A bad completion must cost one check, not the whole schedule."""
    m = _load("gallery/alerts-and-schedulers/alert_scheduler.py")
    a = _actor(m, "AlertScheduler")
    a.storage["check_name"] = "latency"
    a.storage["threshold"] = 42
    a.storage["max_auto_checks"] = 3

    EVENTS.clear()
    request = {"request_id": 4, "observed_value": 55, "threshold": 42}
    report = a._finalize_run_check(
        request, RunnerResult({"content": "I can't produce JSON for this reading."}))

    assert report["next_timer_id"], "the recurring timer was not re-armed"
    assert a.storage["max_auto_checks"] == 2, a.storage
    # Falls back to the request's own values.
    assert report["normalized_value"] == 55, report
    assert report["alert"] is False, report
    assert a.storage["last_check"] == report
    assert [n for n, _ in EVENTS if n == "scheduler.result_unparsed"], EVENTS

    # A well-formed completion is unaffected by the fallback.
    good = a._finalize_run_check(request, RunnerResult({"content": json.dumps(
        {"normalized_value": 12, "alert": True, "summary": "spiking"})}))
    assert good["normalized_value"] == 12 and good["alert"] is True, good
    assert a.storage["max_auto_checks"] == 1, a.storage
    print("ok test_scheduler_survives_an_unparseable_completion")


def main():
    tests = [
        test_runner_result_canonical_contracts,
        test_non_object_canonical_result_is_rejected,
        test_sentiment_json_llm_equivalence,
        test_oracle_cache_http_raw_equivalence,
        test_spam_detector_text_and_json,
        test_fenced_completion_is_parsed_not_rejected,
        test_unparseable_completion_names_the_actor,
        test_scheduler_survives_an_unparseable_completion,
    ]
    for t in tests:
        t()
    print(f"\nall {len(tests)} runner-result migration tests passed")


if __name__ == "__main__":
    main()
