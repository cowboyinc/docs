"""Runnable audio transcription example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Produce transcript JSON from the provided audio metadata."


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
class AudioTranscriber:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Audio Transcriber"
        self.storage["__description__"] = "Off-chain audio transcription via runner"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["default_language"] = str(obj.get("default_language", "en"))
        self.storage["job_count"] = 0
        self.storage["last_transcript"] = {"status": "idle"}
        runtime.emit_event("transcriber.init", {"default_language": self.storage["default_language"]})
        return b"ok: initialized"

    @public(decode="cbor")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def transcribe_audio(self, payload):
        obj = payload or {}
        job_id = int(self.storage.get("job_count", 0)) + 1
        self.storage["job_count"] = job_id
        request = {
            "job_id": job_id,
            "audio_url": str(obj.get("audio_url", "https://example.com/demo.wav")),
            "language": str(obj.get("language", self.storage.get("default_language", "en"))),
            "hints": str(obj.get("hints", "")),
        }
        return self._transcribe_audio_job(codec.encode(request))

    @runner.continuation
    async def _transcribe_audio_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "audio_url": request.get("audio_url", ""),
                    "language": request.get("language", ""),
                    "hints": request.get("hints", ""),
                    "instruction": "Return JSON with transcript(string), summary(string), confidence(int).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=220,
        )
        return self._finalize_transcript(ctx.request, ctx.result)

    def _finalize_transcript(self, payload, transcript):
        request = payload or {}
        result = _json_object(transcript, "audio transcriber")
        report = {
            "job_id": int(request.get("job_id", 0)),
            "audio_url": str(request.get("audio_url", "")),
            "language": str(request.get("language", "")),
            "transcript": str(result.get("transcript", "")),
            "summary": str(result.get("summary", "")),
            "confidence": int(result.get("confidence", 50)),
        }
        self.storage["last_transcript"] = report
        runtime.emit_event("transcriber.result", report)
        return codec.encode(report)

    @pure
    @public(decode="cbor")
    def get_last_transcript(self, payload):
        response = self.storage.get("last_transcript", {"status": "idle"})
        runtime.emit_event("get_last_transcript", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "default_language": self.storage.get("default_language", "en"),
            "job_count": int(self.storage.get("job_count", 0)),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return AudioTranscriber()


def init(payload):
    return _get_actor().init(payload)


def transcribe_audio(payload):
    return _get_actor().transcribe_audio(payload)


def transcribe_audio__resume(payload):
    return _get_actor()._transcribe_audio_job__resume(payload)


def _transcribe_audio_job__resume(payload):
    return _get_actor()._transcribe_audio_job__resume(payload)


def get_last_transcript(payload):
    return _get_actor().get_last_transcript(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
