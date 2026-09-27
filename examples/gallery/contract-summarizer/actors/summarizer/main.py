"""Runnable contract summarizer example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Analyze smart contract risk and return JSON only."


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
class ContractSummarizer:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Contract Summarizer"
        self.storage["__description__"] = "LLM-powered contract summarization"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["profile_name"] = str(obj.get("profile_name", "evm-risk"))
        self.storage["analysis_count"] = 0
        self.storage["last_summary"] = {"status": "idle"}
        runtime.emit_event("contract.init", {"profile_name": self.storage["profile_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def analyze_contract(self, payload):
        obj = payload or {}
        analysis_id = int(self.storage.get("analysis_count", 0)) + 1
        self.storage["analysis_count"] = analysis_id
        request = {
            "analysis_id": analysis_id,
            "address": str(obj.get("address", "0x1234")),
            "source": str(obj.get("source", "")),
        }
        return self._analyze_contract_job(codec.encode(request))

    @runner.continuation
    async def _analyze_contract_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "profile_name": self.storage.get("profile_name", ""),
                    "address": request.get("address", ""),
                    "source_excerpt": str(request.get("source", ""))[:4000],
                    "instruction": "Return JSON with risk_score(int), recommendation(string), red_flags(array), summary(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=260,
        )
        return self._finalize_contract_summary(ctx.request, ctx.result)

    def _finalize_contract_summary(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "contract summarizer")
        summary = {
            "analysis_id": int(request.get("analysis_id", 0)),
            "address": str(request.get("address", "")),
            "risk_score": int(result.get("risk_score", 50)),
            "recommendation": str(result.get("recommendation", "review")),
            "red_flags": result.get("red_flags", []),
            "summary": str(result.get("summary", "")),
        }
        self.storage["last_summary"] = summary
        runtime.emit_event("contract.result", summary)
        return codec.encode(summary)

    @pure
    @public(decode="cbor")
    def get_last_summary(self, payload):
        response = self.storage.get("last_summary", {"status": "idle"})
        runtime.emit_event("get_last_summary", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "profile_name": self.storage.get("profile_name", ""),
            "analysis_count": int(self.storage.get("analysis_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return ContractSummarizer()


def init(payload):
    return _get_actor().init(payload)


def analyze_contract(payload):
    return _get_actor().analyze_contract(payload)


def analyze_contract__resume(payload):
    return _get_actor()._analyze_contract_job__resume(payload)


def _analyze_contract_job__resume(payload):
    return _get_actor()._analyze_contract_job__resume(payload)


def get_last_summary(payload):
    return _get_actor().get_last_summary(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
