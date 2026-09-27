"""Runnable alerts and schedulers example."""

import json

from cowboy_sdk import actor, deferred, runner, capture, codec, runtime, pure, public


SYSTEM_PROMPT = "Normalize the metric reading and return alert JSON only."


def _timer_payload(payload):
    """Decode a payload that may arrive from a timer fire or from a direct call.

    A timer routed by `schedule_after` delivers the envelope's `_payload` as a
    JSON list of byte values — the leading `[` marks that form, and the bytes it
    holds are the CBOR that was scheduled. A direct call brings CBOR, or the raw
    JSON the CLI sends.
    """
    if not payload:
        return {}
    if payload[:1] == b"[":
        payload = bytes(json.loads(payload))
    try:
        return codec.decode(payload) or {}
    except Exception:
        return json.loads(payload) or {}


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
class AlertScheduler:
    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Alert Scheduler"
        self.storage["__description__"] = "Scheduled alerts with timer-based execution"
        self.storage["__version__"] = "1.0.0"
        obj = json.loads(payload) if payload else {}
        self.storage["check_name"] = str(obj.get("check_name", "demo-check"))
        self.storage["threshold"] = int(obj.get("threshold", 42))
        self.storage["interval_blocks"] = int(obj.get("interval_blocks", 25))
        self.storage["max_auto_checks"] = int(obj.get("max_auto_checks", 0))
        self.storage["check_count"] = 0
        self.storage["alert_count"] = 0
        self.storage["last_check"] = {"status": "idle"}
        self.storage["next_timer_id"] = ""
        runtime.emit_event("scheduler.init", {"check_name": self.storage["check_name"]})
        return b"ok: initialized"

    @public(decode="cbor")
    def schedule_next_check(self, payload):
        obj = payload or {}
        delay_blocks = int(obj.get("delay_blocks", self.storage.get("interval_blocks", 25)))
        sample_value = int(obj.get("observed_value", self.storage.get("threshold", 42)))
        timer_id = runtime.schedule_after(
            delay_blocks, "run_check", codec.encode({"observed_value": sample_value})
        ).hex()
        self.storage["next_timer_id"] = timer_id
        result = {"scheduled": True, "timer_id": timer_id, "delay_blocks": delay_blocks}
        runtime.emit_event("scheduler.scheduled", result)
        return result

    @public(decode="raw")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def run_check(self, payload):
        obj = _timer_payload(payload)
        request_id = int(self.storage.get("check_count", 0)) + 1
        self.storage["check_count"] = request_id
        request = {
            "request_id": request_id,
            "observed_value": int(obj.get("observed_value", self.storage.get("threshold", 42))),
            "threshold": int(self.storage.get("threshold", 42)),
        }
        return self._run_check_job(codec.encode(request))

    @runner.continuation
    async def _run_check_job(self, payload):
        request = codec.decode(payload) if payload else {}
        ctx = capture()
        ctx.request = request
        ctx.result = await runner.llm(
            json.dumps(
                {
                    "check_name": self.storage.get("check_name", ""),
                    "observed_value": request.get("observed_value", 0),
                    "threshold": request.get("threshold", 0),
                    "instruction": "Return JSON with normalized_value(int), alert(boolean), summary(string).",
                },
                sort_keys=True,
            ),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=140,
        )
        return self._finalize_run_check(ctx.request, ctx.result)

    def _finalize_run_check(self, payload, analysis):
        request = payload or {}
        # Fail-soft, unlike the other JSON-only actors in the gallery: the
        # recurring timer is re-armed at the bottom of this method and nothing
        # else re-arms it, so raising on a prose or malformed completion would
        # stop the schedule permanently with no watchdog to restart it. A bad
        # completion costs one check, not the workflow.
        try:
            result = _json_object(analysis, "alert scheduler")
        except ValueError as e:
            result = {}
            runtime.emit_event("scheduler.result_unparsed", {
                "request_id": int(request.get("request_id", 0)),
                "error": str(e)[:160],
            })
        normalized_value = int(result.get("normalized_value", request.get("observed_value", 0)))
        alert = bool(result.get("alert", normalized_value < int(request.get("threshold", 0))))
        if alert:
            self.storage["alert_count"] = int(self.storage.get("alert_count", 0)) + 1
        report = {
            "request_id": int(request.get("request_id", 0)),
            "check_name": self.storage.get("check_name", ""),
            "normalized_value": normalized_value,
            "threshold": int(request.get("threshold", 0)),
            "alert": alert,
            "summary": str(result.get("summary", "")),
        }
        self.storage["last_check"] = report
        remaining = int(self.storage.get("max_auto_checks", 0))
        if remaining > 0:
            self.storage["max_auto_checks"] = remaining - 1
            timer_id = runtime.schedule_after(
                int(self.storage.get("interval_blocks", 25)),
                "run_check",
                codec.encode({"observed_value": normalized_value}),
            ).hex()
            self.storage["next_timer_id"] = timer_id
            report["next_timer_id"] = timer_id
        else:
            self.storage["next_timer_id"] = ""
            report["next_timer_id"] = ""
        runtime.emit_event("scheduler.result", report)
        return codec.encode(report)

    @pure
    @public(decode="cbor")
    def get_last_check(self, payload):
        response = self.storage.get("last_check", {"status": "idle"})
        runtime.emit_event("get_last_check", response)
        return response

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        response = {
            "check_name": self.storage.get("check_name", ""),
            "threshold": int(self.storage.get("threshold", 42)),
            "interval_blocks": int(self.storage.get("interval_blocks", 25)),
            "max_auto_checks": int(self.storage.get("max_auto_checks", 0)),
            "check_count": int(self.storage.get("check_count", 0)),
            "alert_count": int(self.storage.get("alert_count", 0)),
            "next_timer_id": self.storage.get("next_timer_id", ""),
        }
        runtime.emit_event("get_stats", response)
        return response


def _get_actor():
    return AlertScheduler()


def init(payload):
    return _get_actor().init(payload)


def schedule_next_check(payload):
    return _get_actor().schedule_next_check(payload)


def run_check(payload):
    return _get_actor().run_check(payload)


def run_check__resume(payload):
    return _get_actor()._run_check_job__resume(payload)


def _run_check_job__resume(payload):
    return _get_actor()._run_check_job__resume(payload)


def get_last_check(payload):
    return _get_actor().get_last_check(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
