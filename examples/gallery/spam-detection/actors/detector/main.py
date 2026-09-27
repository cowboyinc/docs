"""Runnable spam detection example using a conservative runner continuation."""

import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Classify messages for spam risk. Respond with JSON only."


def _get_last_case_dict(storage):
    case_id = int(storage.get("last_case_id", 0))
    if case_id <= 0:
        return {"status": "idle"}
    return {
        "case_id": case_id,
        "channel": storage.get("last_case_channel", "email"),
        "spam_score": int(storage.get("last_case_spam_score", 50)),
        "label": storage.get("last_case_label", "review"),
        "action": storage.get("last_case_action", "review"),
        "reasons": storage.get("last_case_reasons", []),
        "threshold": int(storage.get("spam_threshold", 70)),
        "is_spam": bool(storage.get("last_case_is_spam", False)),
        "raw_result": storage.get("last_case_raw_result", ""),
    }


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
class SpamDetector:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Spam Detector"
        self.storage["__description__"] = "LLM-powered spam detection for content moderation"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["spam_threshold"] = int(obj.get("spam_threshold", 70))
        self.storage["message_count"] = 0
        self.storage["last_case_id"] = 0
        runtime.emit_event("spam.init", {"spam_threshold": self.storage["spam_threshold"]})
        return b"ok: initialized"

    @public(decode="raw")
    @runner.continuation
    async def scan_content(self, payload):
        obj = codec.decode(payload) if payload else {}
        case_id = int(self.storage.get("message_count", 0)) + 1
        self.storage["message_count"] = case_id
        request = {
            "case_id": case_id,
            "message": str(obj.get("content", "")),
            "channel": str(obj.get("channel", "email")),
        }

        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "channel": request.get("channel", "email"),
                    "content": request.get("message", ""),
                    "instruction": "Return JSON with keys spam_score(int), label(string), action(string), reasons(array).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=180,
        )
        return self._finalize_scan_content(ctx.request, ctx.result)

    def _finalize_scan_content(self, payload, analysis):
        request = payload or {}
        raw_text = analysis.text
        result = _json_object(analysis, "spam detector")
        threshold = int(self.storage.get("spam_threshold", 70))
        spam_score = int(result.get("spam_score", 50))
        # Normalize here rather than on read: the model is asked for an array but
        # is not bound to send one, and `reasons` is a list in the get_last_case
        # response contract.
        reasons = result.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = []
        label = str(result.get("label", "review"))
        action = str(result.get("action", "review"))
        is_spam = spam_score >= threshold
        self.storage["last_case_id"] = int(request.get("case_id", 0))
        self.storage["last_case_channel"] = str(request.get("channel", "email"))
        self.storage["last_case_spam_score"] = spam_score
        self.storage["last_case_label"] = label
        self.storage["last_case_action"] = action
        self.storage["last_case_reasons"] = reasons
        self.storage["last_case_is_spam"] = is_spam
        self.storage["last_case_raw_result"] = raw_text[:400]
        runtime.emit_event(
            "spam.result",
            {"case_id": int(request.get("case_id", 0)), "label": label, "spam_score": spam_score, "is_spam": is_spam},
        )
        return b"ok: case stored"

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

    @pure
    @public(decode="cbor")
    def get_last_case(self, payload):
        response = _get_last_case_dict(self.storage)
        runtime.emit_event("get_last_case", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "spam_threshold": int(self.storage.get("spam_threshold", 70)),
            "message_count": int(self.storage.get("message_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return SpamDetector()


def init(payload):
    return _get_actor().init(payload)


def scan_content(payload):
    return _get_actor().scan_content(payload)


def scan_content__resume(payload):
    return _get_actor().scan_content__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_last_case(payload):
    return _get_actor().get_last_case(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
