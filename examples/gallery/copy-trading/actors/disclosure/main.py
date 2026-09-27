"""Runnable Congressional disclosure copy-trading demo."""

import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = (
    "You summarize delayed congressional trading disclosures into structured JSON. "
    "Respond with JSON only."
)


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
class CongressionalCopyTrader:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Pelosi Copy Trader"
        self.storage["__description__"] = "Copy trading actor with position sizing"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["target_member"] = str(obj.get("target_member", "Nancy Pelosi"))
        self.storage["broker"] = str(obj.get("broker", "paper-broker"))
        self.storage["max_position_usd"] = int(obj.get("max_position_usd", 10000))
        self.storage["trade_count"] = 0
        self.storage["last_trade"] = {"status": "idle"}
        runtime.emit_event("copy_trader.init", {"member": self.storage["target_member"]})
        return b"ok: initialized"

    @public(decode="raw")
    @runner.continuation
    async def ingest_disclosure(self, payload):
        obj = codec.decode(payload) if payload else {}
        trade_id = int(self.storage.get("trade_count", 0)) + 1
        self.storage["trade_count"] = trade_id
        disclosure = {
            "trade_id": trade_id,
            "ticker": str(obj.get("ticker", "NVDA")).upper(),
            "side": str(obj.get("side", "buy")).lower(),
            "reported_size_usd": int(obj.get("reported_size_usd", 15000)),
            "delay_days": int(obj.get("delay_days", 35)),
            "filing_text": str(obj.get("filing_text", "")),
        }

        ctx = capture()
        ctx.disclosure = disclosure
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "member": self.storage.get("target_member", ""),
                    "ticker": disclosure.get("ticker", ""),
                    "side": disclosure.get("side", ""),
                    "reported_size_usd": disclosure.get("reported_size_usd", 0),
                    "delay_days": disclosure.get("delay_days", 0),
                    "context": disclosure.get("filing_text", ""),
                    "instruction": "Return JSON with keys follow_trade(boolean), size_usd(int), confidence(int), thesis(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=180,
        )
        return self._finalize_disclosure_trade(ctx.disclosure, ctx.result)

    def _finalize_disclosure_trade(self, payload, analysis):
        disclosure = payload or {}
        result = _json_object(analysis, "pelosi copy trader")
        capped_size = min(
            int(result.get("size_usd", disclosure.get("reported_size_usd", 0))),
            int(self.storage.get("max_position_usd", 10000)),
        )
        trade = {
            "trade_id": int(disclosure.get("trade_id", 0)),
            "member": self.storage.get("target_member", ""),
            "broker": self.storage.get("broker", ""),
            "ticker": disclosure.get("ticker", ""),
            "side": disclosure.get("side", ""),
            "follow_trade": bool(result.get("follow_trade", True)),
            "size_usd": capped_size,
            "confidence": int(result.get("confidence", 50)),
            "thesis": str(result.get("thesis", "Parsed delayed disclosure")),
            "delay_days": int(disclosure.get("delay_days", 0)),
        }
        self.storage["last_trade"] = trade
        runtime.emit_event("copy_trader.trade", trade)
        return codec.encode(trade)

    @pure
    @public(decode="raw")
    def on_runner_result(self, payload):
        # Decode the runner-result envelope, degrading to {} instead of raising.
        try:
            msg = codec.decode(payload) if isinstance(payload, bytes) else payload
        except Exception:
            msg = {}
        if isinstance(msg, dict) and msg.get("reply_handler"):
            runner.handle_runner_result(self, msg)
            return b"ok"

        return b"error: unrecognized callback"

    @public(decode="cbor")
    @pure
    def get_last_trade(self, payload):
        response = self.storage.get("last_trade", {"status": "idle"})
        runtime.emit_event("get_last_trade", response)
        return response

    @public(decode="cbor")
    @pure
    def get_stats(self, payload):
        response = {
            "target_member": self.storage.get("target_member", ""),
            "broker": self.storage.get("broker", ""),
            "max_position_usd": int(self.storage.get("max_position_usd", 10000)),
            "trade_count": int(self.storage.get("trade_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return CongressionalCopyTrader()


def init(payload):
    return _get_actor().init(payload)


def ingest_disclosure(payload):
    return _get_actor().ingest_disclosure(payload)


def ingest_disclosure__resume(payload):
    return _get_actor().ingest_disclosure__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_last_trade(payload):
    return _get_actor().get_last_trade(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
