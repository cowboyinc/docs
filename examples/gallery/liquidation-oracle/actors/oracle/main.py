"""Runnable liquidation oracle example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Assess liquidation risk and return JSON only."


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
class LiquidationOracle:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Liquidation Oracle"
        self.storage["__description__"] = "Position monitoring and liquidation trigger"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["market_name"] = str(obj.get("market_name", "demo-lending"))
        self.storage["case_count"] = 0
        self.storage["last_case"] = {"status": "idle"}
        runtime.emit_event("liquidation.init", {"market_name": self.storage["market_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def evaluate_account(self, payload):
        obj = payload or {}
        case_id = int(self.storage.get("case_count", 0)) + 1
        self.storage["case_count"] = case_id
        request = {
            "case_id": case_id,
            "account": str(obj.get("account", "demo-account")),
            "collateral_value": float(obj.get("collateral_value", 100000)),
            "debt_value": float(obj.get("debt_value", 65000)),
            "liquidation_threshold": float(obj.get("liquidation_threshold", 0.8)),
        }
        return self._evaluate_account_job(codec.encode(request))

    @runner.continuation
    async def _evaluate_account_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "account": request.get("account", ""),
                    "collateral_value": request.get("collateral_value", 0),
                    "debt_value": request.get("debt_value", 0),
                    "liquidation_threshold": request.get("liquidation_threshold", 0),
                    "instruction": "Return JSON with risk_score(int), recommendation(string), liquidation_price_note(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=180,
        )
        return self._finalize_account_case(ctx.request, ctx.result)

    def _finalize_account_case(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "liquidation oracle")
        debt_value = float(request.get("debt_value", 0))
        collateral_value = float(request.get("collateral_value", 0))
        health_ratio = 0.0 if debt_value <= 0 else collateral_value / debt_value
        case = {
            "case_id": int(request.get("case_id", 0)),
            "account": str(request.get("account", "")),
            "market_name": self.storage.get("market_name", ""),
            "health_ratio": health_ratio,
            "risk_score": int(result.get("risk_score", max(0, 100 - int(health_ratio * 50)))),
            "recommendation": str(result.get("recommendation", "monitor")),
            "liquidation_price_note": str(result.get("liquidation_price_note", "")),
        }
        self.storage["last_case"] = case
        runtime.emit_event("liquidation.result", case)
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
            "market_name": self.storage.get("market_name", ""),
            "case_count": int(self.storage.get("case_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return LiquidationOracle()


def init(payload):
    return _get_actor().init(payload)


def evaluate_account(payload):
    return _get_actor().evaluate_account(payload)


def evaluate_account__resume(payload):
    return _get_actor()._evaluate_account_job__resume(payload)


def _evaluate_account_job__resume(payload):
    return _get_actor()._evaluate_account_job__resume(payload)


def get_last_case(payload):
    return _get_actor().get_last_case(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
