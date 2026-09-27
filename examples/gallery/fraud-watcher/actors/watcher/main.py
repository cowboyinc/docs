"""Runnable fraud watcher example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Classify transaction fraud risk and return JSON only."


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
class FraudWatcher:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Fraud Watcher"
        self.storage["__description__"] = "Transaction fraud detection actor"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["risk_threshold"] = int(obj.get("risk_threshold", 70))
        self.storage["case_count"] = 0
        self.storage["flagged_count"] = 0
        self.storage["last_case"] = {"status": "idle"}
        runtime.emit_event("fraud.init", {"risk_threshold": self.storage["risk_threshold"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def analyze_transaction(self, payload):
        obj = payload or {}
        case_id = int(self.storage.get("case_count", 0)) + 1
        self.storage["case_count"] = case_id
        request = {
            "case_id": case_id,
            "from_addr": str(obj.get("from_addr", "")),
            "to_addr": str(obj.get("to_addr", "")),
            "amount": float(obj.get("amount", 0)),
            "context": str(obj.get("context", "")),
        }
        return self._analyze_transaction_job(codec.encode(request))

    @runner.continuation
    async def _analyze_transaction_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "transaction": request,
                    "instruction": "Return JSON with risk_score(int), risk_level(string), patterns(array), recommendation(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=220,
        )
        return self._finalize_transaction_case(ctx.request, ctx.result)

    def _finalize_transaction_case(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "fraud watcher")
        risk_score = int(result.get("risk_score", 50))
        if risk_score >= int(self.storage.get("risk_threshold", 70)):
            self.storage["flagged_count"] = int(self.storage.get("flagged_count", 0)) + 1
        case = {
            "case_id": int(request.get("case_id", 0)),
            "risk_score": risk_score,
            "risk_level": str(result.get("risk_level", "medium")),
            "patterns": result.get("patterns", []),
            "recommendation": str(result.get("recommendation", "review")),
        }
        self.storage["last_case"] = case
        runtime.emit_event("fraud.result", case)
        return codec.encode(case)

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
            "risk_threshold": int(self.storage.get("risk_threshold", 70)),
            "case_count": int(self.storage.get("case_count", 0)),
            "flagged_count": int(self.storage.get("flagged_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return FraudWatcher()


def init(payload):
    return _get_actor().init(payload)


def analyze_transaction(payload):
    return _get_actor().analyze_transaction(payload)


def analyze_transaction__resume(payload):
    return _get_actor()._analyze_transaction_job__resume(payload)


def _analyze_transaction_job__resume(payload):
    return _get_actor()._analyze_transaction_job__resume(payload)


def get_last_case(payload):
    return _get_actor().get_last_case(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
