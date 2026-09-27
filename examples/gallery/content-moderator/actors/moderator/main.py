"""Runnable content moderation example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Moderate content and return policy JSON only."


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
class ContentModerator:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Content Moderator"
        self.storage["__description__"] = "LLM-based content moderation actor"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["policy_name"] = str(obj.get("policy_name", "community-default"))
        self.storage["case_count"] = 0
        self.storage["last_case"] = {"status": "idle"}
        self.storage["last_appeal"] = {"status": "none"}
        runtime.emit_event("moderator.init", {"policy_name": self.storage["policy_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def moderate_content(self, payload):
        obj = payload or {}
        case_id = int(self.storage.get("case_count", 0)) + 1
        self.storage["case_count"] = case_id
        request = {
            "case_id": case_id,
            "content": str(obj.get("content", "")),
            "content_type": str(obj.get("content_type", "post")),
        }
        return self._moderate_content_job(codec.encode(request))

    @runner.continuation
    async def _moderate_content_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "policy_name": self.storage.get("policy_name", ""),
                    "content_type": request.get("content_type", ""),
                    "content": request.get("content", ""),
                    "instruction": "Return JSON with category(string), action(string), severity(int), rationale(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=180,
        )
        return self._finalize_moderation(ctx.request, ctx.result)

    def _finalize_moderation(self, payload, review):
        request = payload or {}
        result = _json_object(review, "content moderator")
        case = {
            "case_id": int(request.get("case_id", 0)),
            "content_type": str(request.get("content_type", "")),
            "category": str(result.get("category", "review")),
            "action": str(result.get("action", "review")),
            "severity": int(result.get("severity", 1)),
            "rationale": str(result.get("rationale", "")),
        }
        self.storage["last_case"] = case
        runtime.emit_event("moderator.result", case)
        return codec.encode(case)

    @public(decode="cbor")
    def submit_appeal(self, payload):
        obj = payload or {}
        appeal = {
            "case_id": int(obj.get("case_id", 0)),
            "reason": str(obj.get("reason", "")),
            "status": "received",
        }
        self.storage["last_appeal"] = appeal
        runtime.emit_event("moderator.appeal", appeal)
        return appeal

    @pure
    @public(decode="cbor")
    def get_last_case(self, payload):
        response = self.storage.get("last_case", {"status": "idle"})
        runtime.emit_event("get_last_case", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "policy_name": self.storage.get("policy_name", ""),
            "case_count": int(self.storage.get("case_count", 0)),
            "last_appeal": self.storage.get("last_appeal", {"status": "none"}),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return ContentModerator()


def init(payload):
    return _get_actor().init(payload)


def moderate_content(payload):
    return _get_actor().moderate_content(payload)


def moderate_content__resume(payload):
    return _get_actor()._moderate_content_job__resume(payload)


def _moderate_content_job__resume(payload):
    return _get_actor()._moderate_content_job__resume(payload)


def submit_appeal(payload):
    return _get_actor().submit_appeal(payload)


def get_last_case(payload):
    return _get_actor().get_last_case(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
