import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offchain import keys
from offchain.inference import Refused, Service, UpstreamResponse
from offchain.journal import Journal, latest_records
from offchain.volume import LocalVolume, encode_json

ACTOR = bytes(range(20))
CATALOG = {
    "models": {"cowboy/qwen": {"upstream_model": "qwen3", "target": "http://up/v1/chat/completions", "context_tokens": 100, "max_output_tokens": 64}},
    "pricing_version": "p1",
    "limits": {"max_requests": 3, "max_concurrency": 1},
}


class FakeUpstream:
    def __init__(self):
        self.script = []  # list of (status, chunks) or Exception
        self.calls = []
        self.on_call = None

    def request(self, target, body):
        self.calls.append((target, json.loads(body)))
        if self.on_call:
            self.on_call()
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, chunks = item
        return UpstreamResponse(status, {}, iter(chunks))


def make(tmp_path, clock=None):
    vol = LocalVolume(tmp_path / "vol")
    sk = Ed25519PrivateKey.generate()
    token, record = keys.issue("cloud", "k", sk, 1, ACTOR, now=1)
    vol.mutate(lambda v: {record.path(): record.to_json(), "catalog.json": encode_json(CATALOG)})
    up = FakeUpstream()
    t = {"now": 1000}
    svc = Service(vol, up, slot="cloud", hire_id=7, staleness_s=15, flush_interval_s=5, clock=lambda: t["now"])
    return vol, svc, up, token, record, t


def req(body, rid="r1"):
    return {"method": "POST", "path": "/v1/chat/completions", "query": "", "headers": {}, "body": json.dumps(body).encode(), "ctx": {"request_id": rid}}


CHAT = {"model": "cowboy/qwen", "messages": [{"role": "user", "content": "hi"}]}


def test_open_refuses_bad_missing_and_wrong_slot(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    for hdr in ({}, {"Authorization": "Bearer nope"}, {"authorization": "Basic x"}):
        with pytest.raises(Refused) as e:
            svc.open_session(hdr, {})
        assert e.value.status == 401
    other, _ = keys.issue("other", "k", Ed25519PrivateKey.generate(), 1, ACTOR, now=1)
    with pytest.raises(Refused):
        svc.open_session({"Authorization": f"Bearer {other}"}, {})
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    assert s.session_id == "s1" and s.record.key_id == record.key_id


def test_non_stream_reserves_before_dispatch_and_records_known_usage(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    up.on_call = lambda: (lambda rec: (_ for _ in ()).throw(AssertionError("not reserved")) if rec is None or rec["outcome"] != "in_flight" else None)(latest_records(vol).get(("s1", "r1")))
    completion = {"id": "x", "object": "chat.completion", "model": "qwen3", "choices": [{"message": {"role": "assistant", "content": "yo"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    up.script.append((200, [json.dumps(completion).encode()]))
    out = svc.chat_completions(s, req(CHAT))
    assert out["status"] == 200 and out["headers"]["x-request-id"] == "r1"
    body = json.loads(out["body"])
    assert body["model"] == "cowboy/qwen"
    assert up.calls[0][1]["model"] == "qwen3" and "stream_options" not in up.calls[0][1]
    t["now"] += 5
    svc.close_session(s)
    rec = latest_records(vol)[("s1", "r1")]
    assert rec["outcome"] == "completed" and rec["usage"] == {"known": {"input": 3, "output": 2}}
    assert rec["key_id"] == record.key_id and rec["price_version"] == "p1" and rec["hire_id"] == 7


def test_stream_relays_rewrites_model_and_meters(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    ev = lambda o: b"data: " + json.dumps(o).encode() + b"\n\n"
    d1 = {"id": "x", "object": "chat.completion.chunk", "model": "qwen3", "choices": [{"delta": {"content": "he"}}]}
    d2 = {"id": "x", "object": "chat.completion.chunk", "model": "qwen3", "choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]}
    usage = {"id": "x", "object": "chat.completion.chunk", "model": "qwen3", "choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 6}}
    raw = ev(d1) + ev(d2) + ev(usage) + b"data: [DONE]\n\n"
    # split at awkward byte boundaries to exercise frame buffering
    chunks = [raw[:7], raw[7:40], raw[40:-3], raw[-3:]]
    up.script.append((200, chunks))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True)))
    assert out["headers"]["content-type"] == "text/event-stream"
    frames = b"".join(out["body"]).split(b"\n\n")
    objs = [json.loads(f[6:]) for f in frames if f.startswith(b"data: {")]
    assert [o["model"] for o in objs] == ["cowboy/qwen", "cowboy/qwen"]  # usage-only event withheld
    assert frames[-2] == b"data: [DONE]"
    assert up.calls[0][1]["stream_options"] == {"include_usage": True}
    s.journal.flush()
    assert latest_records(vol)[("s1", "r1")]["usage"] == {"known": {"input": 4, "output": 6}}

    # With include_usage requested the final usage event is delivered.
    up.script.append((200, [raw]))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True, stream_options={"include_usage": True}), "r2"))
    frames = b"".join(out["body"]).split(b"\n\n")
    objs = [json.loads(f[6:]) for f in frames if f.startswith(b"data: {")]
    assert objs[-1]["usage"]["completion_tokens"] == 6 and objs[-1]["model"] == "cowboy/qwen"
    svc.close_session(s)
    assert latest_records(vol)[("s1", "r2")]["outcome"] == "completed"


def test_upstream_failures_are_502_and_unknown_usage(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    up.script.append((500, [b"boom"]))
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r1"))
    assert e.value.status == 502
    up.script.append(ConnectionError("refused"))
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r2"))
    assert e.value.status == 502
    # Mid-stream failure: error event, no [DONE].
    def broken():
        yield b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n'
        raise ConnectionError("reset")
    up.script.append((200, broken()))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True), "r3"))
    frames = b"".join(out["body"])
    assert b'"error"' in frames and b"[DONE]" not in frames
    svc.close_session(s)
    recs = latest_records(vol)
    assert all(recs[("s1", r)]["outcome"] == "upstream_error" and recs[("s1", r)]["usage"] == {"unknown": True} for r in ("r1", "r2", "r3"))
    assert s.in_flight == 0


def test_client_abort_records_client_aborted(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    def endless():
        while True:
            yield b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n'
    up.script.append((200, endless()))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True)))
    it = out["body"]
    next(it)
    it.close()  # client went away
    svc.close_session(s)
    rec = latest_records(vol)[("s1", "r1")]
    assert rec["outcome"] == "client_aborted" and rec["usage"] == {"unknown": True}
    assert s.in_flight == 0


def test_revocation_observed_within_staleness_bound(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    record.revoked_at = 1001
    vol.mutate(lambda v: {record.path(): record.to_json()})
    t["now"] += 14  # inside the bound: cached view still serves
    up.script.append((200, [json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()]))
    assert svc.chat_completions(s, req(CHAT, "r1"))["status"] == 200
    t["now"] += 1
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r2"))
    assert e.value.status == 401 and s.closed
    with pytest.raises(Refused):
        svc.models({"Authorization": f"Bearer {token}"})


def test_validation_and_limits(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    cases = [
        (b"not json", 400), (json.dumps([]).encode(), 400),
        (json.dumps(dict(CHAT, model="gpt-9")).encode(), 404),
        (json.dumps(dict(CHAT, n=2)).encode(), 400),
        (json.dumps(dict(CHAT, tools=[])).encode(), 400),
        (json.dumps(dict(CHAT, bogus=1)).encode(), 400),
        (json.dumps(dict(CHAT, max_tokens=10, max_completion_tokens=10)).encode(), 400),
        (json.dumps(dict(CHAT, max_tokens=65)).encode(), 400),
        (json.dumps(dict(CHAT, messages=[{"role": "user", "content": [{"type": "text"}]}])).encode(), 400),
        (json.dumps(dict(CHAT, messages=[{"role": "user", "content": "x" * 500}])).encode(), 400),
    ]
    for raw, status in cases:
        with pytest.raises(Refused) as e:
            svc.chat_completions(s, {"body": raw, "ctx": {"request_id": "rX"}})
        assert e.value.status == status, raw[:40]
    assert not up.calls and s.requests == 0
    # Session request cap.
    ok = (200, [json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()])
    for i in range(3):
        up.script.append(ok)
        svc.chat_completions(s, req(CHAT, f"r{i}"))
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r9"))
    assert e.value.status == 429


def test_models_lists_catalog(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    out = svc.models({"Authorization": f"Bearer {token}"})
    assert out == {"object": "list", "data": [{"id": "cowboy/qwen", "object": "model", "created": 0, "owned_by": "cowboy"}]}


def test_open_refuses_when_limits_are_absent_or_zero(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    for limits in ({}, {"max_requests": 0, "max_concurrency": 1}, {"max_requests": 3}):
        vol.mutate(lambda v: {"catalog.json": encode_json(dict(CATALOG, limits=limits))})
        with pytest.raises(Refused) as e:
            svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
        assert e.value.status == 503 and e.value.code == "limits_unavailable"


def test_duplicate_request_id_never_dispatches_twice(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    ok = (200, [json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()])
    up.script.append(ok)
    assert svc.chat_completions(s, req(CHAT, "r1"))["status"] == 200
    up.script.append(ok)
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r1"))
    assert e.value.status == 409 and e.value.code == "duplicate_request"
    # Resumed session (new Service instance, same session id, same volume): still refused.
    svc.close_session(s)
    svc2 = Service(vol, up, slot="cloud", hire_id=7, clock=lambda: t["now"])
    s2 = svc2.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    with pytest.raises(Refused) as e:
        svc2.chat_completions(s2, req(CHAT, "r1"))
    assert e.value.status == 409
    assert len(up.calls) == 1 and s.in_flight == 0 and s2.in_flight == 0


def test_non_stream_partial_body_and_oversize_finalize_unknown(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    svc.max_body_bytes = 64
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    closed = []

    def partial():
        yield b'{"choices": []'
        raise TimeoutError("stall")
    up.script.append((200, partial()))
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r1"))
    assert e.value.status == 502
    up.script.append((200, [b"x" * 65]))
    with pytest.raises(Refused) as e:
        svc.chat_completions(s, req(CHAT, "r2"))
    assert e.value.status == 502
    up.script.append((200, [b"[1,2]"]))  # valid JSON, not an object
    with pytest.raises(Refused):
        svc.chat_completions(s, req(CHAT, "r3"))
    svc.close_session(s)
    recs = latest_records(vol)
    assert all(recs[("s1", r)]["outcome"] == "upstream_error" and recs[("s1", r)]["usage"] == {"unknown": True} for r in ("r1", "r2", "r3"))
    assert s.in_flight == 0


def test_stream_eof_without_done_is_upstream_error(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    up.script.append((200, [b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n']))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True), "r1"))
    frames = b"".join(out["body"])
    assert b'"error"' in frames and b"[DONE]" not in frames
    svc.close_session(s)
    assert latest_records(vol)[("s1", "r1")]["outcome"] == "upstream_error"


def test_stream_crlf_frames_and_frame_cap(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    raw = b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\r\n\r\ndata: {"id":"x","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\r\n\r\ndata: [DONE]\r\n\r\n'
    # Split so a CR lands at a chunk boundary.
    cut = raw.find(b"\r\n\r\n") + 1
    up.script.append((200, [raw[:cut], raw[cut:]]))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True), "r1"))
    frames = b"".join(out["body"]).split(b"\n\n")
    assert sum(1 for f in frames if f.startswith(b"data: {")) == 1 and frames[-2] == b"data: [DONE]"
    s.journal.flush()
    assert latest_records(vol)[("s1", "r1")]["usage"] == {"known": {"input": 1, "output": 1}}

    svc.max_body_bytes = 32
    def huge():
        yield b"data: " + b"x" * 40
        yield b"\n\n"
    up.script.append((200, huge()))
    out = svc.chat_completions(s, req(dict(CHAT, stream=True), "r2"))
    frames = b"".join(out["body"])
    assert b'"error"' in frames and b"[DONE]" not in frames
    svc.close_session(s)
    assert latest_records(vol)[("s1", "r2")]["outcome"] == "upstream_error"


def test_non_200_closes_transport_even_when_never_pulled(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    closed = []
    def gen():
        yield b"boom"
    up.script.append((500, gen()))
    orig = up.request
    def request(target, body):
        r = orig(target, body)
        r.closer = lambda: closed.append(True)
        return r
    up.request = request
    with pytest.raises(Refused):
        svc.chat_completions(s, req(CHAT, "r1"))
    assert closed == [True]


def test_sse_frame_cap_applies_to_complete_frames(tmp_path):
    from offchain.inference import FrameTooLarge, _sse_events
    with pytest.raises(FrameTooLarge):
        list(_sse_events([b"data: " + b"x" * 100 + b"\n\n"], 10))
    assert list(_sse_events([b"data: ok\n\n"], 10)) == [b"ok"]


def test_stream_body_closed_before_first_pull_finalizes(tmp_path):
    vol, svc, up, token, record, t = make(tmp_path)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    closed = []
    def gen():
        yield b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n'
    up.script.append((200, gen()))
    orig = up.request
    def request(target, body):
        r = orig(target, body)
        r.closer = lambda: closed.append(True)
        return r
    up.request = request
    out = svc.chat_completions(s, req(dict(CHAT, stream=True), "r1"))
    body = out["body"]
    body.close()   # gateway cancelled before pulling a byte
    body.close()   # idempotent
    assert closed == [True] and s.in_flight == 0
    with pytest.raises(StopIteration):
        next(body)
    svc.close_session(s)
    rec = latest_records(vol)[("s1", "r1")]
    assert rec["outcome"] == "client_aborted" and rec["usage"] == {"unknown": True}
