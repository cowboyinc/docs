"""Runnable sanctions screening example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Verify a sanctions screening match and respond with JSON only."


DEFAULT_LISTS = {
    "ofac": [{"type": "address", "value": "0xdeadbeef", "name": "Demo OFAC Entity"}],
    "un": [{"type": "name", "value": "ACME SHIPPING", "name": "ACME SHIPPING"}],
    "eu": [{"type": "address", "value": "0xbadf00d", "name": "Demo EU Entity"}],
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
class SanctionsFilter:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Sanctions Filter"
        self.storage["__description__"] = "Compliance filter for sanctions screening"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["lists"] = obj.get("lists", DEFAULT_LISTS)
        self.storage["screen_count"] = 0
        self.storage["last_screen"] = {"status": "idle"}
        runtime.emit_event("sanctions.init", {"lists": list(self.storage["lists"].keys())})
        return b"ok: initialized"

    @public(decode="cbor")
    def update_lists(self, payload):
        obj = payload or {}
        self.storage["lists"] = obj.get("lists", DEFAULT_LISTS)
        result = {"updated": True, "lists": list(self.storage["lists"].keys())}
        runtime.emit_event("sanctions.updated", result)
        return result

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def screen_entity(self, payload):
        obj = payload or {}
        screen_id = int(self.storage.get("screen_count", 0)) + 1
        self.storage["screen_count"] = screen_id
        query = str(obj.get("query", "")).lower()
        matches = []
        for list_name, entries in self.storage.get("lists", DEFAULT_LISTS).items():
            for entry in entries:
                value = str(entry.get("value", "")).lower()
                if query and query == value.lower():
                    matches.append({"list": list_name, "entry": entry})
        if not matches:
            result = {"screen_id": screen_id, "query": query, "status": "clear", "matches": []}
            self.storage["last_screen"] = result
            runtime.emit_event("sanctions.screened", result)
            return result

        request = {"screen_id": screen_id, "query": query, "matches": matches}
        return self._screen_entity_job(codec.encode(request))

    @runner.continuation
    async def _screen_entity_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "query": request.get("query", ""),
                    "matches": request.get("matches", []),
                    "instruction": "Return JSON with verdict(string), confidence(int), note(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=180,
        )
        return self._finalize_screen(ctx.request, ctx.result)

    def _finalize_screen(self, payload, review):
        request = payload or {}
        result = _json_object(review, "sanctions filter")
        screen = {
            "screen_id": int(request.get("screen_id", 0)),
            "query": str(request.get("query", "")),
            "status": str(result.get("verdict", "flag_for_review")),
            "confidence": int(result.get("confidence", 50)),
            "note": str(result.get("note", "")),
            "matches": request.get("matches", []),
        }
        self.storage["last_screen"] = screen
        runtime.emit_event("sanctions.screened", screen)
        return codec.encode(screen)

    @pure
    @public(decode="cbor")
    def get_last_screen(self, payload):
        response = self.storage.get("last_screen", {"status": "idle"})
        runtime.emit_event("get_last_screen", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "screen_count": int(self.storage.get("screen_count", 0)),
            "lists": list(self.storage.get("lists", DEFAULT_LISTS).keys()),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return SanctionsFilter()


def init(payload):
    return _get_actor().init(payload)


def update_lists(payload):
    return _get_actor().update_lists(payload)


def screen_entity(payload):
    return _get_actor().screen_entity(payload)


def screen_entity__resume(payload):
    return _get_actor()._screen_entity_job__resume(payload)


def _screen_entity_job__resume(payload):
    return _get_actor()._screen_entity_job__resume(payload)


def get_last_screen(payload):
    return _get_actor().get_last_screen(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
