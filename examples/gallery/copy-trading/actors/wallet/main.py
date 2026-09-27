"""Runnable wallet copy-trading demo for a named strategy publisher."""

import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = (
    "You turn a copied trade feed into structured execution guidance. "
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
class WalletCopyTrader:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Barron Copy Trader"
        self.storage["__description__"] = "Copy trading actor that mirrors a target wallet"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["source_wallet"] = str(obj.get("source_wallet", "0xbarrondemo"))
        self.storage["destination_book"] = str(obj.get("destination_book", "paper-book"))
        self.storage["max_notional_usd"] = int(obj.get("max_notional_usd", 5000))
        self.storage["trade_count"] = 0
        self.storage["last_trade"] = {"status": "idle"}
        runtime.emit_event("wallet_copy.init", {"source_wallet": self.storage["source_wallet"]})
        return b"ok: initialized"

    @public(decode="raw")
    @runner.continuation
    async def ingest_trade(self, payload):
        obj = codec.decode(payload) if payload else {}
        trade_id = int(self.storage.get("trade_count", 0)) + 1
        self.storage["trade_count"] = trade_id
        trade = {
            "trade_id": trade_id,
            "ticker": str(obj.get("ticker", "AAPL")).upper(),
            "side": str(obj.get("side", "buy")).lower(),
            "source_size_usd": int(obj.get("source_size_usd", 4000)),
            "signal_text": str(obj.get("signal_text", "")),
            "urgency": str(obj.get("urgency", "normal")),
        }

        ctx = capture()
        ctx.trade = trade
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "source_wallet": self.storage.get("source_wallet", ""),
                    "ticker": trade.get("ticker", ""),
                    "side": trade.get("side", ""),
                    "source_size_usd": trade.get("source_size_usd", 0),
                    "urgency": trade.get("urgency", ""),
                    "notes": trade.get("signal_text", ""),
                    "instruction": "Return JSON with keys copy_trade(boolean), target_size_usd(int), confidence(int), note(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=160,
        )
        return self._finalize_trade(ctx.trade, ctx.result)

    def _finalize_trade(self, payload, analysis):
        trade_request = payload or {}
        result = _json_object(analysis, "barron copy trader")
        trade = {
            "trade_id": int(trade_request.get("trade_id", 0)),
            "source_wallet": self.storage.get("source_wallet", ""),
            "destination_book": self.storage.get("destination_book", ""),
            "ticker": trade_request.get("ticker", ""),
            "side": trade_request.get("side", ""),
            "copy_trade": bool(result.get("copy_trade", True)),
            "target_size_usd": min(
                int(result.get("target_size_usd", trade_request.get("source_size_usd", 0))),
                int(self.storage.get("max_notional_usd", 5000)),
            ),
            "confidence": int(result.get("confidence", 50)),
            "note": str(result.get("note", "Processed source trade")),
        }
        self.storage["last_trade"] = trade
        runtime.emit_event("wallet_copy.trade", trade)
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
        return self.storage.get("last_trade", {"status": "idle"})

    @public(decode="cbor")
    @pure
    def get_stats(self, payload):
        return {
            "source_wallet": self.storage.get("source_wallet", ""),
            "destination_book": self.storage.get("destination_book", ""),
            "max_notional_usd": int(self.storage.get("max_notional_usd", 5000)),
            "trade_count": int(self.storage.get("trade_count", 0)),
        }


def _get_actor():
    return WalletCopyTrader()


def init(payload):
    return _get_actor().init(payload)


def ingest_trade(payload):
    return _get_actor().ingest_trade(payload)


def ingest_trade__resume(payload):
    return _get_actor().ingest_trade__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_last_trade(payload):
    return _get_actor().get_last_trade(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
