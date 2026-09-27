"""
Runner LLM Actor - Ask an LLM for a short response via an off-chain Runner.

Handlers:
  init         - Initialize actor with default config
  ask_llm      - Request a response from the LLM (async via Runner)
  get_response - Read the latest response, or a specific response by ID
  get_config   - Read current LLM config

Demonstrates:
  - @runner.continuation async pattern
  - runner.llm(...) prompt and system prompt usage
  - capture() for context across await points
  - Indexed storage keys (llm:{id}:response, llm:{id}:status)
  - Event emission for LLM results
"""

from cowboy_sdk import (Address, PermissionDeniedError, actor, runner, capture,
                        codec, runtime, public)


SYSTEM_PROMPT = "You are a concise technical explainer. Respond in one short paragraph."


@actor
class RunnerLLMActor:
    # Deploy runs init in the deploy transaction and the demo calls it again, which
    # deny-by-default would reject without @public. Returning early keeps that
    # second call from rewinding request_count and orphaning stored responses.
    @public(decode="raw")
    def init(self, payload):
        """Initialize actor state."""
        if runtime.is_initialized():
            return b"ok: already initialized"
        self.storage["__name__"] = "Runner LLM"
        self.storage["__description__"] = "Canonical runner.llm example via runner.continuation"
        self.storage["__version__"] = "1.0.0"
        # The runtime tracks the owner in a reserved slot; this storage copy only
        # exists so `cowboy actor state` shows it.
        owner = runtime.assume_ownership_if_unowned()
        self.storage["owner"] = Address(owner).to_hex(checksum=False) if owner else ""
        self.storage["request_count"] = 0
        self.storage["cfg:model_name"] = "llama"
        self.storage["cfg:max_tokens"] = 256
        self.storage["cfg:temperature"] = 0.2
        runtime.emit_event("llm.init", {"owner": self.storage["owner"]})
        return b"ok: initialized"

    # @public rides outside @runner.continuation so the tag lands on the compiled
    # entrypoint. decode="raw" is the only codec a continuation may declare — the
    # generated ask_llm__resume has its own payload path and carries no decode
    # tag, so anything but raw would cover only half the pair and be refused at
    # class creation. Declaring it still opts out of the gated default flip.
    @public(decode="raw")
    @runner.continuation
    async def ask_llm(self, payload):
        """Ask the LLM for a response."""
        obj = codec.decode(payload) if payload else {}
        prompt = str(obj.get("prompt", "Explain Cowboy runner continuations in one short paragraph."))
        request_id = self.storage.get("request_count", 0)
        self.storage["request_count"] = request_id + 1
        self.storage[f"llm:{request_id}:prompt"] = prompt
        self.storage[f"llm:{request_id}:status"] = "pending"

        ctx = capture()
        ctx.request_id = request_id

        result = await runner.llm(
            prompt,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=self.storage.get("cfg:max_tokens", 256),
            temperature=self.storage.get("cfg:temperature", 0.2),
        )

        request_id = ctx.request_id
        response_text = result.text

        block = runtime.get_block_height()

        self.storage[f"llm:{request_id}:response"] = response_text
        self.storage[f"llm:{request_id}:status"] = "done"
        self.storage[f"llm:{request_id}:block"] = block
        self.storage["latest_response"] = response_text

        runtime.emit_event(
            "llm.response",
            {"request_id": request_id, "response": response_text[:200]},
        )
        return b"ok: response stored"

    @public(decode="cbor")
    def get_response(self, payload):
        """Get the latest response, or a specific one by request_id."""
        obj = payload or {}
        request_id = obj.get("request_id")

        if request_id is not None:
            text = self.storage.get(f"llm:{int(request_id)}:response")
            status = self.storage.get(f"llm:{int(request_id)}:status", "unknown")
            if not text:
                return b"error: response not found"
            runtime.emit_event("llm.read", {"request_id": int(request_id)})
            return {"request_id": int(request_id), "response": text, "status": status}

        latest = self.storage.get("latest_response")
        if not latest:
            return b"no responses yet"
        runtime.emit_event("llm.read", {"latest": True})
        return latest

    @public(decode="cbor")
    def get_config(self, payload):
        """Return current config."""
        return {
            "model_name": self.storage.get("cfg:model_name", "llama"),
            "max_tokens": self.storage.get("cfg:max_tokens", 256),
            "temperature": self.storage.get("cfg:temperature", 0.2),
            "request_count": self.storage.get("request_count", 0),
        }


def _get_actor():
    return RunnerLLMActor()


def init(payload):
    return _get_actor().init(payload)


def ask_llm(payload):
    return _get_actor().ask_llm(payload)


def ask_llm__resume(payload):
    # @actor does not permission-wrap generated resume methods, so this entrypoint
    # is the only gate. The runner result arrives as a deferred ExecuteActor whose
    # sender is this actor itself (execution/src/execution/transaction.rs builds it
    # with `sender = actor`); anyone else calling here is replaying a forged result
    # against a live continuation, whose cid is derivable from public tx data.
    if runtime.get_sender() != runtime.get_actor_address():
        raise PermissionDeniedError(
            "ask_llm__resume is invoked by the runner callback only")
    return _get_actor().ask_llm__resume(payload)


def get_response(payload):
    return _get_actor().get_response(payload)


def get_config(payload):
    return _get_actor().get_config(payload)
