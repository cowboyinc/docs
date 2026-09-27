"""Runnable image generation metadata example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Return image generation metadata as JSON only."


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
class ImageGenerator:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Image Generator"
        self.storage["__description__"] = "Off-chain image generation via runner"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["default_style"] = str(obj.get("default_style", "editorial"))
        self.storage["job_count"] = 0
        self.storage["last_image"] = {"status": "idle"}
        runtime.emit_event("image.init", {"default_style": self.storage["default_style"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def generate_image(self, payload):
        obj = payload or {}
        job_id = int(self.storage.get("job_count", 0)) + 1
        self.storage["job_count"] = job_id
        request = {
            "job_id": job_id,
            "prompt": str(obj.get("prompt", "Cowboy landscape")),
            "style": str(obj.get("style", self.storage.get("default_style", "editorial"))),
        }
        return self._generate_image_job(codec.encode(request))

    @runner.continuation
    async def _generate_image_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "prompt": request.get("prompt", ""),
                    "style": request.get("style", ""),
                    "instruction": "Return JSON with revised_prompt(string), asset_uri(string), palette(array), caption(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=220,
        )
        return self._finalize_image(ctx.request, ctx.result)

    def _finalize_image(self, payload, analysis):
        request = payload or {}
        result = _json_object(analysis, "image generator")
        image = {
            "job_id": int(request.get("job_id", 0)),
            "prompt": str(request.get("prompt", "")),
            "style": str(request.get("style", "")),
            "revised_prompt": str(result.get("revised_prompt", request.get("prompt", ""))),
            "asset_uri": str(result.get("asset_uri", f"demo://generated/{int(request.get('job_id', 0))}")),
            "palette": result.get("palette", []),
            "caption": str(result.get("caption", "")),
        }
        self.storage["last_image"] = image
        runtime.emit_event("image.result", image)
        return codec.encode(image)

    @pure
    @public(decode="cbor")
    def get_last_image(self, payload):
        response = self.storage.get("last_image", {"status": "idle"})
        runtime.emit_event("get_last_image", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "default_style": self.storage.get("default_style", "editorial"),
            "job_count": int(self.storage.get("job_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return ImageGenerator()


def init(payload):
    return _get_actor().init(payload)


def generate_image(payload):
    return _get_actor().generate_image(payload)


def generate_image__resume(payload):
    return _get_actor()._generate_image_job__resume(payload)


def _generate_image_job__resume(payload):
    return _get_actor()._generate_image_job__resume(payload)


def get_last_image(payload):
    return _get_actor().get_last_image(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
