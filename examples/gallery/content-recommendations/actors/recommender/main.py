"""Runnable content recommendation example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Generate compact recommendation JSON for content ranking."


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
class ContentRecommender:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Content Recommender"
        self.storage["__description__"] = "LLM-powered content recommendation engine"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["catalog_name"] = str(obj.get("catalog_name", "demo-catalog"))
        self.storage["request_count"] = 0
        self.storage["last_recommendation"] = {"status": "idle"}
        runtime.emit_event("recommend.init", {"catalog_name": self.storage["catalog_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def recommend_content(self, payload):
        obj = payload or {}
        request_id = int(self.storage.get("request_count", 0)) + 1
        self.storage["request_count"] = request_id
        request = {
            "request_id": request_id,
            "profile": obj.get("profile", {}),
            "candidates": obj.get("candidates", []),
        }
        return self._recommend_content_job(codec.encode(request))

    @runner.continuation
    async def _recommend_content_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "profile": request.get("profile", {}),
                    "candidates": request.get("candidates", []),
                    "instruction": "Return JSON with recommendations(array of title, score, reason).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=260,
        )
        return self._finalize_recommend_content(ctx.request, ctx.result)

    def _finalize_recommend_content(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "content recommender")
        recommendation = {
            "request_id": int(request.get("request_id", 0)),
            "catalog_name": self.storage.get("catalog_name", ""),
            "recommendations": result.get("recommendations", []),
        }
        self.storage["last_recommendation"] = recommendation
        runtime.emit_event("recommend.result", recommendation)
        return codec.encode(recommendation)

    @pure
    @public(decode="cbor")
    def get_last_recommendation(self, payload):
        response = self.storage.get("last_recommendation", {"status": "idle"})
        runtime.emit_event("get_last_recommendation", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "catalog_name": self.storage.get("catalog_name", ""),
            "request_count": int(self.storage.get("request_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return ContentRecommender()


def init(payload):
    return _get_actor().init(payload)


def recommend_content(payload):
    return _get_actor().recommend_content(payload)


def recommend_content__resume(payload):
    return _get_actor()._recommend_content_job__resume(payload)


def _recommend_content_job__resume(payload):
    return _get_actor()._recommend_content_job__resume(payload)


def get_last_recommendation(payload):
    return _get_actor().get_last_recommendation(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
