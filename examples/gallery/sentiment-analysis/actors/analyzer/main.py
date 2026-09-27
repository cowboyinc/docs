"""Runnable sentiment analysis example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Analyze sentiment and respond with JSON only."


def _json_object(result, who):
    """A JSON-only completion as a dict, or a ValueError naming the actor.

    `.json()` is `json.loads(.text)`: a fenced or prose-wrapped reply raises a
    bare "Expecting value: line 1 column 1" from inside the SDK, before any
    check on the parsed value runs. Models fence JSON whatever the prompt says.
    """
    text = result.text.strip()
    if text.startswith("```"):
        # Drop the whole opening fence line, so any info string works (```json,
        # ```jsonc, ```), and cut at the LAST fence rather than the next one, so
        # a ``` inside a string value doesn't truncate the document — models
        # echo user content into fields like `reasons`.
        _, _, rest = text.partition("\n")
        body, fence, _ = rest.rpartition("```")
        text = (body if fence else rest).strip()
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{who}: runner.llm did not return JSON ({e}): {text[:120]!r}")
    if not isinstance(obj, dict):
        raise ValueError(
            f"{who}: runner.llm did not return a JSON object, got {type(obj).__name__}")
    return obj


@actor
class SentimentAnalyzer:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Sentiment Analyzer"
        self.storage["__description__"] = "LLM-powered sentiment analysis actor"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["topic"] = str(obj.get("topic", "ETH"))
        self.storage["alert_threshold"] = int(obj.get("alert_threshold", -50))
        self.storage["request_count"] = 0
        self.storage["last_report"] = {"status": "idle"}
        runtime.emit_event("sentiment.init", {"topic": self.storage["topic"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def analyze_sentiment(self, payload):
        obj = payload or {}
        request_id = int(self.storage.get("request_count", 0)) + 1
        self.storage["request_count"] = request_id
        request = {"request_id": request_id, "texts": obj.get("texts", [])}
        return self._analyze_sentiment_job(codec.encode(request))

    @runner.continuation
    async def _analyze_sentiment_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "topic": self.storage.get("topic", ""),
                    "texts": request.get("texts", []),
                    "instruction": "Return JSON with sentiment_score(int), outlook(string), summary(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=220,
        )
        return self._finalize_sentiment_report(ctx.request, ctx.result)

    def _finalize_sentiment_report(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "sentiment analyzer")
        sentiment_score = int(result.get("sentiment_score", 0))
        report = {
            "request_id": int(request.get("request_id", 0)),
            "topic": self.storage.get("topic", ""),
            "sentiment_score": sentiment_score,
            "outlook": str(result.get("outlook", "neutral")),
            "summary": str(result.get("summary", "")),
            "alert_triggered": sentiment_score <= int(self.storage.get("alert_threshold", -50)),
        }
        self.storage["last_report"] = report
        runtime.emit_event("sentiment.result", report)
        return codec.encode(report)

    @pure
    @public(decode="cbor")
    def get_last_report(self, payload):
        response = self.storage.get("last_report", {"status": "idle"})
        runtime.emit_event("get_last_report", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "topic": self.storage.get("topic", ""),
            "alert_threshold": int(self.storage.get("alert_threshold", -50)),
            "request_count": int(self.storage.get("request_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return SentimentAnalyzer()


def init(payload):
    return _get_actor().init(payload)


def analyze_sentiment(payload):
    return _get_actor().analyze_sentiment(payload)


def analyze_sentiment__resume(payload):
    return _get_actor()._analyze_sentiment_job__resume(payload)


def _analyze_sentiment_job__resume(payload):
    return _get_actor()._analyze_sentiment_job__resume(payload)


def get_last_report(payload):
    return _get_actor().get_last_report(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
