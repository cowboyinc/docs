"""Service + HttpUpstream composed against a fake OpenAI-compatible endpoint."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offchain import keys
from offchain.http_upstream import HttpUpstream
from offchain.inference import Service
from offchain.journal import latest_records
from offchain.volume import LocalVolume, encode_json


class Serving(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        assert body["model"] == "qwen3-upstream"
        if body.get("stream"):
            assert body["stream_options"] == {"include_usage": True}
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            events = [
                {"id": "c1", "object": "chat.completion.chunk", "model": "qwen3-upstream", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": "qwen3-upstream", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": "qwen3-upstream", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
            ]
            for e in events:
                frame = b"data: " + json.dumps(e).encode() + b"\n\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame))
                self.wfile.flush()
            self.wfile.write(b"e\r\ndata: [DONE]\n\n\r\n0\r\n\r\n")
        else:
            out = json.dumps({"id": "c2", "object": "chat.completion", "model": "qwen3-upstream", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


@pytest.fixture(scope="module")
def serving():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Serving)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    srv.shutdown()


def test_end_to_end_over_http(tmp_path, serving):
    vol = LocalVolume(tmp_path / "vol")
    token, record = keys.issue("cloud", "k", Ed25519PrivateKey.generate(), 1, bytes(20), now=1)
    catalog = {"models": {"cowboy/qwen": {"upstream_model": "qwen3-upstream", "target": serving, "context_tokens": 4096, "max_output_tokens": 256}}, "pricing_version": "p1", "limits": {"max_requests": 10, "max_concurrency": 2}}
    vol.mutate(lambda v: {record.path(): record.to_json(), "catalog.json": encode_json(catalog)})
    svc = Service(vol, HttpUpstream(idle_timeout_s=5), slot="cloud", hire_id=1)
    s = svc.open_session({"Authorization": f"Bearer {token}"}, {"session_id": "s1"})
    body = {"model": "cowboy/qwen", "messages": [{"role": "user", "content": "hi"}]}

    out = svc.chat_completions(s, {"body": json.dumps(body).encode(), "ctx": {"request_id": "r1"}})
    obj = json.loads(out["body"])
    assert obj["model"] == "cowboy/qwen" and obj["choices"][0]["message"]["content"] == "Hello"

    out = svc.chat_completions(s, {"body": json.dumps(dict(body, stream=True, stream_options={"include_usage": True})).encode(), "ctx": {"request_id": "r2"}})
    frames = b"".join(out["body"]).split(b"\n\n")
    objs = [json.loads(f[6:]) for f in frames if f.startswith(b"data: {")]
    assert "".join(o["choices"][0]["delta"].get("content", "") for o in objs if o["choices"]) == "Hello"
    assert all(o["model"] == "cowboy/qwen" for o in objs) and objs[-1]["usage"]["prompt_tokens"] == 9
    assert frames[-2] == b"data: [DONE]"

    svc.close_session(s)
    recs = latest_records(vol)
    assert recs[("s1", "r1")]["usage"] == {"known": {"input": 9, "output": 1}}
    assert recs[("s1", "r2")]["usage"] == {"known": {"input": 9, "output": 2}}
