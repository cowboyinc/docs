"""`/v1` handlers (CIP §4, §6): session open/close and the chat-completions
relay, metered through the journal.

Transport is injected (`Upstream`), so the handler core is testable without a
network. The runner executor wires the real HTTP client, the hire volume, and
the route context. Prompts and completions never touch the volume or logs.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol

from . import keys
from .journal import DuplicateRequest, Journal
from .volume import Volume, read_json

ALLOWED_PARAMS = {
    "model", "messages", "stream", "stream_options", "n",
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "seed",
    "presence_penalty", "frequency_penalty", "user",
}
UNSUPPORTED_PARAMS = {"tools", "tool_choice", "functions", "function_call", "response_format", "logprobs", "top_logprobs", "logit_bias", "parallel_tool_calls", "audio", "modalities", "prediction"}


class Upstream(Protocol):
    def request(self, target: str, body: bytes) -> "UpstreamResponse": ...


@dataclass
class UpstreamResponse:
    status: int
    headers: dict
    chunks: Iterator[bytes]
    closer: object = None  # releases the transport even if `chunks` was never pulled

    def close(self) -> None:
        close = getattr(self.chunks, "close", None)
        if close:
            close()
        if self.closer is not None:
            self.closer()


class StreamBody:
    """Iterator over relay bytes whose close() is safe at any point: before the
    first pull (the wrapped generator never ran, so its finally never would),
    mid-stream (delegates to the generator's finally), or repeatedly."""

    def __init__(self, gen: Iterator[bytes], on_unstarted_close):
        self._gen = gen
        self._on_unstarted_close = on_unstarted_close
        self._started = False
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        self._started = True
        return next(self._gen)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._gen.close()
        else:
            self._on_unstarted_close()


class Refused(Exception):
    def __init__(self, status: int, message: str, type_: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.type_, self.code = status, message, type_, code

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type_, "param": None, "code": self.code}}


@dataclass
class Session:
    session_id: str
    key: keys.ParsedKey
    record: keys.KeyRecord
    journal: Journal
    catalog: dict
    opened_at: int
    last_refresh: int
    last_flush: int
    requests: int = 0
    in_flight: int = 0
    closed: bool = False


@dataclass
class Service:
    volume: Volume
    upstream: Upstream
    slot: str
    hire_id: int
    staleness_s: int = 15
    flush_interval_s: int = 5
    max_body_bytes: int = 4 * 1024 * 1024  # non-stream response body and one SSE frame
    clock: object = field(default=lambda: int(time.time()))

    # -- session lifecycle -------------------------------------------------

    def open_session(self, headers: dict, ctx: dict) -> Session:
        """`session_open`: validate the bearer against the hire's key records and
        start a journal. Refuses with 401/403/429 as OpenAI-shaped errors."""
        parsed = self._bearer(headers)
        record = self._record(parsed)
        if not keys.check(parsed, record):
            raise Refused(401, "invalid or revoked API key", "invalid_request_error", "invalid_api_key")
        catalog = read_json(self.volume, "catalog.json")
        if not catalog or not catalog.get("models"):
            raise Refused(403, "inference is not available for this hire", "invalid_request_error", "hire_not_eligible")
        limits = catalog.get("limits") or {}
        if not all(isinstance(limits.get(k), int) and limits[k] > 0 for k in ("max_requests", "max_concurrency")):
            # Zero or absent ceilings are unavailable, never unlimited.
            raise Refused(503, "session limits are not configured for this hire", "server_error", "limits_unavailable")
        now = self.clock()
        session_id = ctx.get("session_id") or uuid.uuid4().hex
        journal = Journal(self.volume, slot=self.slot, hire_id=self.hire_id, session_id=session_id, price_version=catalog.get("pricing_version"), clock=self.clock)
        return Session(session_id, parsed, record, journal, catalog, now, now, now)

    def close_session(self, session: Session) -> dict:
        """`session_close`: flush the journal; the runner submits the aggregate."""
        session.closed = True
        session.journal.flush()
        session.last_flush = self.clock()
        return {"session_id": session.session_id, "requests": session.requests}

    # -- per_call: /v1/models ------------------------------------------------

    def models(self, headers: dict) -> dict:
        parsed = self._bearer(headers)
        if not keys.check(parsed, self._record(parsed)):
            raise Refused(401, "invalid or revoked API key", "invalid_request_error", "invalid_api_key")
        catalog = read_json(self.volume, "catalog.json") or {"models": {}}
        data = [{"id": mid, "object": "model", "created": 0, "owned_by": "cowboy"} for mid in sorted(catalog["models"])]
        return {"object": "list", "data": data}

    # -- session request: /v1/chat/completions -------------------------------

    def chat_completions(self, session: Session, request: dict) -> dict:
        """Returns {status, headers, body} where body is bytes or a byte iterator
        (SSE). Raises Refused before dispatch; never after headers are sent."""
        request_id = request["ctx"]["request_id"]
        now = self.clock()
        if session.closed:
            raise Refused(401, "session closed", "invalid_request_error", "session_closed")
        if now - session.last_refresh >= self.staleness_s:
            session.record = self._record(session.key)
            session.last_refresh = now
            if not keys.check(session.key, session.record):
                session.closed = True
                raise Refused(401, "invalid or revoked API key", "invalid_request_error", "invalid_api_key")
        limits = session.catalog["limits"]
        if session.requests >= limits["max_requests"]:
            raise Refused(429, "session request limit reached", "rate_limit_error", "session_limit")
        if session.in_flight >= limits["max_concurrency"]:
            raise Refused(429, "too many concurrent requests", "rate_limit_error", "concurrency_limit")

        body, model = self._validate(request.get("body") or b"", session.catalog)
        public_model = body["model"]
        stream = bool(body.get("stream", False))
        want_usage = bool((body.get("stream_options") or {}).get("include_usage", False))

        upstream_body = dict(body)
        upstream_body["model"] = model["upstream_model"]
        if stream:
            upstream_body["stream_options"] = {"include_usage": True}
        else:
            upstream_body.pop("stream_options", None)

        # Durable reservation before any dispatch (§6). A request_id this
        # session already holds, in this process or a resumed one, never
        # dispatches again: there is no transcript to replay, so it is refused.
        try:
            session.journal.reserve(
                request_id, key_id=session.record.key_id, model=public_model, target=model["target"],
                limits={"max_output_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or model["max_output_tokens"], "context_tokens": model["context_tokens"]},
            )
        except DuplicateRequest as dup:
            raise Refused(409, f"request_id already {dup.record.outcome}", "invalid_request_error", "duplicate_request")
        session.requests += 1
        session.in_flight += 1
        try:
            resp = self.upstream.request(model["target"], json.dumps(upstream_body).encode())
        except Exception:
            self._finish(session, request_id, "upstream_error", {"unknown": True})
            raise Refused(502, "upstream request failed", "server_error", "upstream_error")

        headers = {"x-request-id": request_id, "cache-control": "no-store"}
        if resp.status != 200:
            resp.close()
            self._finish(session, request_id, "upstream_error", {"unknown": True})
            raise Refused(502, f"upstream returned {resp.status}", "server_error", "upstream_error")

        if not stream:
            obj = None
            try:
                raw = _read_bounded(resp.chunks, self.max_body_bytes)
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    obj = None
            except Exception:
                obj = None
            finally:
                resp.close()
                if obj is None:
                    self._finish(session, request_id, "upstream_error", {"unknown": True})
            if obj is None:
                raise Refused(502, "upstream response unusable", "server_error", "upstream_error")
            obj["model"] = public_model
            self._finish(session, request_id, "completed", _usage_of(obj))
            headers["content-type"] = "application/json"
            return {"status": 200, "headers": headers, "body": json.dumps(obj).encode()}

        headers["content-type"] = "text/event-stream"
        body = StreamBody(self._relay(session, request_id, resp, public_model, want_usage),
                          on_unstarted_close=lambda: (resp.close(), self._finish(session, request_id, "client_aborted", {"unknown": True})))
        return {"status": 200, "headers": headers, "body": body}

    def _relay(self, session: Session, request_id: str, resp: UpstreamResponse, public_model: str, want_usage: bool) -> Iterator[bytes]:
        """SSE relay generator: forwards complete events as they arrive, rewrites
        the model id, captures usage, and records exactly one outcome."""
        usage = {"unknown": True}
        outcome = "client_aborted"
        done = False
        try:
            for event in _sse_events(resp.chunks, self.max_body_bytes):
                if event == b"[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(event)
                except ValueError:
                    outcome = "upstream_error"
                    yield _sse_error(Refused(502, "upstream sent malformed event", "server_error", "upstream_error"))
                    return
                if "error" in obj:
                    outcome = "upstream_error"
                    yield _sse(obj)
                    return
                if obj.get("usage"):
                    usage = _usage_of(obj)
                if obj.get("usage") is not None and not obj.get("choices"):
                    if want_usage:
                        obj["model"] = public_model
                        yield _sse(obj)
                    continue
                obj["model"] = public_model
                yield _sse(obj)
            if not done:
                # Upstream EOF without its terminator is a truncated stream,
                # never a success we terminate on its behalf.
                outcome = "upstream_error"
                yield _sse_error(Refused(502, "upstream stream ended before its terminator", "server_error", "upstream_error"))
                return
            outcome = "completed"
            yield b"data: [DONE]\n\n"
        except Exception:
            outcome = "upstream_error"
            yield _sse_error(Refused(502, "upstream stream failed", "server_error", "upstream_error"))
        finally:
            resp.close()
            self._finish(session, request_id, outcome, usage)

    # -- helpers ---------------------------------------------------------------

    def _bearer(self, headers: dict) -> keys.ParsedKey:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Bearer "):
            raise Refused(401, "missing bearer token", "invalid_request_error", "invalid_api_key")
        parsed = keys.parse(auth[7:].strip())
        if parsed is None or parsed.slot != self.slot:
            raise Refused(401, "invalid or revoked API key", "invalid_request_error", "invalid_api_key")
        return parsed

    def _record(self, parsed: keys.ParsedKey) -> keys.KeyRecord | None:
        raw = self.volume.read(f"keys/{parsed.key_id_hex}.json")
        return None if raw is None else keys.KeyRecord.from_json(raw)

    def _validate(self, raw: bytes, catalog: dict) -> tuple[dict, dict]:
        try:
            body = json.loads(raw)
        except ValueError:
            raise Refused(400, "request body is not valid JSON", "invalid_request_error")
        if not isinstance(body, dict):
            raise Refused(400, "request body must be an object", "invalid_request_error")
        unsupported = sorted(set(body) & UNSUPPORTED_PARAMS)
        if unsupported:
            raise Refused(400, f"unsupported parameter: {unsupported[0]}", "invalid_request_error", "unsupported_parameter")
        unknown = sorted(set(body) - ALLOWED_PARAMS)
        if unknown:
            raise Refused(400, f"unknown parameter: {unknown[0]}", "invalid_request_error", "unknown_parameter")
        model = catalog["models"].get(body.get("model"))
        if model is None:
            raise Refused(404, f"model not found: {body.get('model')!r}", "invalid_request_error", "model_not_found")
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or not all(
            isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("content"), str) for m in msgs
        ):
            raise Refused(400, "messages must be a non-empty list of {role, content: string}", "invalid_request_error")
        if body.get("n", 1) != 1:
            raise Refused(400, "only n=1 is supported", "invalid_request_error")
        if "max_tokens" in body and "max_completion_tokens" in body:
            raise Refused(400, "max_tokens and max_completion_tokens conflict", "invalid_request_error")
        cap = body.get("max_tokens", body.get("max_completion_tokens"))
        if cap is not None and (not isinstance(cap, int) or cap < 1 or cap > model["max_output_tokens"]):
            raise Refused(400, f"output token limit must be 1..{model['max_output_tokens']}", "invalid_request_error")
        if "stream_options" in body and not isinstance(body["stream_options"], dict):
            raise Refused(400, "stream_options must be an object", "invalid_request_error")
        # Conservative context bound: 1 token per 4 bytes of prompt, never fewer than one per message.
        prompt_bytes = sum(len(m["content"].encode()) + len(m["role"]) for m in msgs)
        if prompt_bytes // 4 + len(msgs) > model["context_tokens"]:
            raise Refused(400, "prompt exceeds the model's context limit", "invalid_request_error", "context_length_exceeded")
        return body, model

    def _finish(self, session: Session, request_id: str, outcome: str, usage: dict) -> None:
        session.in_flight -= 1
        session.journal.finish(request_id, outcome, usage)
        now = self.clock()
        if now - session.last_flush >= self.flush_interval_s:
            session.journal.flush()
            session.last_flush = now


def _usage_of(obj: dict) -> dict:
    u = obj.get("usage")
    if isinstance(u, dict) and isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
        return {"known": {"input": u["prompt_tokens"], "output": u["completion_tokens"]}}
    return {"unknown": True}


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def _sse_error(err: Refused) -> bytes:
    return _sse(err.body())


class FrameTooLarge(Exception):
    pass


def _read_bounded(chunks: Iterable[bytes], limit: int) -> bytes:
    """Join a body, refusing once it exceeds `limit` bytes."""
    out = bytearray()
    for chunk in chunks:
        out += chunk
        if len(out) > limit:
            raise FrameTooLarge(f"body exceeds {limit} bytes")
    return bytes(out)


def _sse_events(chunks: Iterable[bytes], max_frame_bytes: int) -> Iterator[bytes]:
    """Yield the joined `data:` payload of each complete SSE event, buffering
    partial frames across network chunks. LF and CRLF line endings are both
    accepted; a single frame over `max_frame_bytes` aborts the stream.
    Comments and other fields are ignored."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        # Normalize CRLF, holding back a trailing CR that may pair with the next chunk.
        hold = b""
        if buf.endswith(b"\r"):
            buf, hold = buf[:-1], b"\r"
        buf = buf.replace(b"\r\n", b"\n")
        while True:
            idx = buf.find(b"\n\n")
            if idx < 0:
                break
            if idx > max_frame_bytes:
                raise FrameTooLarge(f"SSE frame exceeds {max_frame_bytes} bytes")
            frame, buf = buf[:idx], buf[idx + 2:]
            data = _frame_data(frame)
            if data is not None:
                yield data
        if len(buf) > max_frame_bytes:
            raise FrameTooLarge(f"SSE frame exceeds {max_frame_bytes} bytes")
        buf += hold
    if buf.strip():
        data = _frame_data(buf.replace(b"\r\n", b"\n"))
        if data is not None:
            yield data


def _frame_data(frame: bytes) -> bytes | None:
    lines = [ln[5:].lstrip(b" ") for ln in frame.split(b"\n") if ln.startswith(b"data:")]
    return b"\n".join(lines) if lines else None
