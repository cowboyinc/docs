"""Runnable DAO copilot example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Analyze DAO proposals and respond with structured JSON only."


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
class DAOCopilot:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "DAO Copilot"
        self.storage["__description__"] = "LLM-powered DAO governance assistant"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["dao_name"] = str(obj.get("dao_name", "Cowboy DAO"))
        self.storage["proposal_count"] = 0
        self.storage["last_report"] = {"status": "idle"}
        runtime.emit_event("dao.init", {"dao_name": self.storage["dao_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def analyze_proposal(self, payload):
        obj = payload or {}
        proposal_id = int(self.storage.get("proposal_count", 0)) + 1
        self.storage["proposal_count"] = proposal_id
        request = {
            "proposal_id": proposal_id,
            "title": str(obj.get("title", "Untitled proposal")),
            "body": str(obj.get("body", "")),
        }
        return self._analyze_proposal_job(codec.encode(request))

    @runner.continuation
    async def _analyze_proposal_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "dao_name": self.storage.get("dao_name", ""),
                    "title": request.get("title", ""),
                    "body": request.get("body", ""),
                    "instruction": "Return JSON with recommendation(string), confidence(int), risks(array), summary(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=220,
        )
        return self._finalize_proposal_report(ctx.request, ctx.result)

    def _finalize_proposal_report(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "dao copilot")
        report = {
            "proposal_id": int(request.get("proposal_id", 0)),
            "dao_name": self.storage.get("dao_name", ""),
            "title": str(request.get("title", "")),
            "recommendation": str(result.get("recommendation", "abstain")),
            "confidence": int(result.get("confidence", 50)),
            "risks": result.get("risks", []),
            "summary": str(result.get("summary", "")),
        }
        self.storage["last_report"] = report
        runtime.emit_event("dao.result", report)
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
            "dao_name": self.storage.get("dao_name", ""),
            "proposal_count": int(self.storage.get("proposal_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return DAOCopilot()


def init(payload):
    return _get_actor().init(payload)


def analyze_proposal(payload):
    return _get_actor().analyze_proposal(payload)


def analyze_proposal__resume(payload):
    return _get_actor()._analyze_proposal_job__resume(payload)


def _analyze_proposal_job__resume(payload):
    return _get_actor()._analyze_proposal_job__resume(payload)


def get_last_report(payload):
    return _get_actor().get_last_report(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
