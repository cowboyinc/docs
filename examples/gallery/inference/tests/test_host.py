"""Serving host: Gateway route context in, offchain handlers out."""

import base64
import hashlib
import http.client
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from offchain.host import Host, build_host, handler_for_path, make_server, parse_route_context, ContextError
from offchain.inference import Service, UpstreamResponse
from offchain.keys import issue
from offchain.manage import Account, Hire, Management, Slot
from offchain.management_runtime import AccountProof
from offchain.volume import ConflictError, Volume, encode_json

NOW = 1789080000
ACTOR = b"a" * 20
OWNER = b"h" * 20
CATALOG = {
    "models": {"cowboy/qwen": {"upstream_model": "qwen3", "target": "http://up/v1/chat/completions", "context_tokens": 100, "max_output_tokens": 64}},
    "pricing_version": "p1",
    "limits": {"max_requests": 3, "max_concurrency": 1},
}


# ---- a ciborium-shaped encoder: byte fields as arrays of small ints ----------

def _head(major, n):
    if n < 24:
        return bytes([major << 5 | n])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * width):
            return bytes([major << 5 | info]) + n.to_bytes(width, "big")
    raise ValueError


def cbor(value):
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):  # what ciborium does for Vec<u8> / [u8; N]
        return cbor(list(value))
    if isinstance(value, str):
        raw = value.encode()
        return _head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


def context(**over):
    ctx = {"actor_address": ACTOR, "handler": "offchain.manage.Management.handle", "verb": "GET", "path": "/manage/config",
           "path_params": [], "mounts": [], "auth": "public", "chain_id": 0, "network": "", "query": "", "headers": []}
    ctx.update(over)
    return base64.urlsafe_b64encode(cbor(ctx)).rstrip(b"=").decode()


# ---- rig ---------------------------------------------------------------------

class AtomicTestVolume(Volume):
    def __init__(self):
        self.files, self.lock, self.revision = {}, threading.Lock(), 0

    def root(self):
        return str(self.revision)

    def read(self, path):
        return self.files.get(path)

    def list(self, prefix):
        return sorted(k for k in self.files if k.startswith(prefix))

    def commit(self, prev_root, writes):
        with self.lock:
            if prev_root != self.root():
                raise ConflictError()
            self.files = dict(self.files, **writes)
            self.revision += 1
            return self.root()


class FakeUpstream:
    def __init__(self):
        self.script, self.calls = [], []

    def request(self, target, body):
        self.calls.append((target, json.loads(body)))
        status, chunks = self.script.pop(0)
        return UpstreamResponse(status, {}, iter(chunks))


class CountingService(Service):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.closed = 0

    def close_session(self, session):
        self.closed += 1
        return super().close_session(session)


@pytest.fixture
def rig():
    volume = AtomicTestVolume()
    signer = Ed25519PrivateKey.generate()
    token, record = issue("cloud", "k", signer, 123, ACTOR, now=NOW)
    volume.mutate(lambda v: {record.path(): record.to_json(), "catalog.json": encode_json(CATALOG)})
    hire = Hire(OWNER, ("declaration", "version", "artifact"), "active")
    seen = {"proofs": []}

    def verify(req):
        # The host must hand the handler the Gateway's proof fields untouched.
        assert isinstance(req.proof, AccountProof)
        seen["proofs"].append((req.method, req.raw_target, req.body, req.proof))
        digest = hashlib.sha256(req.proof.sig).digest()
        return Account(OWNER, req.proof.expiry, req.proof.nonce, digest)

    management = Management(actor=ACTOR, chain_id=123, network="local",
                            slots={"cloud": Slot(7, hire.pins, "https://api.example", 3)},
                            hire_lookup=lambda _: hire, volume_for=lambda slot: volume,
                            verify_account=verify, consume_nonce=lambda key, expiry, digest: True,
                            issue_key=lambda slot, name, now: issue(slot, name, signer, 123, ACTOR, now=now), clock=lambda: NOW)
    upstream = FakeUpstream()
    service = CountingService(volume, upstream, slot="cloud", hire_id=7, clock=lambda: NOW)
    host = Host(actor=ACTOR, chain_id=123, network="local", management=management, services={"cloud": service})
    return host, service, upstream, token, seen


# ---- context decoding ----------------------------------------------------------

def test_route_context_decodes_the_gateway_shape():
    sig, nonce = bytes(range(65)), b"nonce-1"
    ctx = parse_route_context(context(verb="post", path="/manage/api/keys", query="slot=cloud", auth="account",
                                      account_sig=sig, account_expiry=NOW + 60, account_nonce=nonce, chain_id=123, network="local",
                                      headers=[["Authorization", "Bearer x"], ["content-type", "application/json"]]))
    assert (ctx.actor, ctx.verb, ctx.raw_target, ctx.auth) == (ACTOR, "POST", "/manage/api/keys?slot=cloud", "account")
    assert (ctx.account_sig, ctx.account_expiry, ctx.account_nonce, ctx.chain_id, ctx.network) == (sig, NOW + 60, nonce, 123, "local")
    assert ctx.header("authorization") == "Bearer x"


def test_workload_context_dispatches_by_path_with_the_real_request(rig):
    host, service, upstream, token, seen = rig
    wl = b"w" * 32
    base = dict(actor_address=ACTOR, verb="GET", path="/manage/config", path_params=[], auth="public", chain_id=0, network="", workload=wl)

    def wctx(**over):
        d = dict(base); d.update(over); d.pop("handler", None)
        return base64.urlsafe_b64encode(cbor(d)).rstrip(b"=").decode()

    ctx = parse_route_context(wctx())
    assert ctx.handler is None and ctx.workload == wl
    assert handler_for_path("/manage/api/keys/abc") == "offchain.manage.Management.handle"
    assert handler_for_path("/v1/models") == "offchain.inference.Service.models"
    assert handler_for_path("/v1/chat/completions") == "offchain.inference.Service"
    assert handler_for_path("/v2/other") is None
    with pytest.raises(ContextError):
        parse_route_context(base64.urlsafe_b64encode(cbor({k: v for k, v in base.items() if k != "workload"})).decode())

    server = make_server(host, "127.0.0.1:0")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # the relayed request is the caller's own: its path, query and headers count
        conn.request("GET", "/manage/config", headers={"x-cowboy-route-context": wctx()})
        resp = conn.getresponse(); assert resp.status == 200 and json.loads(resp.read())["actor"] == ACTOR.hex()
        sig, nonce = bytes([9]) * 65, b"n9"
        conn.request("POST", "/manage/api/keys?slot=cloud", body=b'{"name":"K"}', headers={
            "x-cowboy-route-context": wctx(verb="POST", path="/manage/api/keys", auth="account", account_sig=sig,
                                            account_expiry=NOW + 60, account_nonce=nonce, chain_id=123, network="local"),
            "content-type": "application/json", "content-length": "12"})
        resp = conn.getresponse(); assert resp.status == 201, resp.read()
        resp.read()
        assert seen["proofs"][-1][1] == "/manage/api/keys?slot=cloud"
        conn.request("GET", "/v1/models", headers={"x-cowboy-route-context": wctx(path="/v1/models"), "authorization": f"Bearer {token}"})
        resp = conn.getresponse(); assert resp.status == 200 and [m["id"] for m in json.loads(resp.read())["data"]] == ["cowboy/qwen"]
        conn.request("GET", "/v2/other", headers={"x-cowboy-route-context": wctx(path="/v2/other")})
        resp = conn.getresponse(); assert resp.status == 404; resp.read()
        conn.request("DELETE", "/manage/config", headers={"x-cowboy-route-context": wctx()})
        resp = conn.getresponse(); assert resp.status == 400; resp.read()  # verb mismatch with the context
        conn.request("GET", "/manage/config")
        resp = conn.getresponse(); assert resp.status == 404; resp.read()  # no context: not a gateway request
    finally:
        server.shutdown(); server.server_close()


def test_route_context_rejects_malformed_input():
    for bad in ("", "!!!", context(auth="Root"), context(auth=1), context(actor_address=b"a" * 19), context(account_sig=b"s" * 64),
                context(headers=[["only-name"]]), context(handler="")):
        with pytest.raises(ContextError):
            parse_route_context(bad)
    with pytest.raises(ContextError):
        parse_route_context(base64.urlsafe_b64encode(cbor([1, 2])).decode())


# ---- dispatch ----------------------------------------------------------------

def test_public_management_route_and_handler_gating(rig):
    host, *_ = rig
    out = host.handle(parse_route_context(context()), b"")
    assert out["status"] == 200 and json.loads(out["body"])["actor"] == ACTOR.hex()
    assert host.handle(parse_route_context(context(actor_address=b"z" * 20)), b"")["status"] == 404
    assert host.handle(parse_route_context(context(handler="offchain.other")), b"")["status"] == 404
    assert host.handle(parse_route_context(context()), b"x" * (host.max_body_bytes + 1))["status"] == 413


def test_account_route_requires_the_signature_and_passes_the_proof_through(rig):
    host, _, _, _, seen = rig
    ctx = parse_route_context(context(verb="POST", path="/manage/api/keys", query="slot=cloud", auth="account"))
    assert host.handle(ctx, b"{}")["status"] == 401  # the runner self-enforces auth
    sig, nonce = bytes([7]) * 65, b"n1"
    ctx = parse_route_context(context(verb="POST", path="/manage/api/keys", query="slot=cloud", auth="account",
                                      account_sig=sig, account_expiry=NOW + 60, account_nonce=nonce, chain_id=123, network="local"))
    out = host.handle(ctx, b'{"name":"OpenRouter"}')
    assert out["status"] == 201, out
    assert seen["proofs"] == [("POST", "/manage/api/keys?slot=cloud", b'{"name":"OpenRouter"}', AccountProof(sig, NOW + 60, nonce))]


def test_models_needs_a_bearer_for_a_configured_slot(rig):
    host, _, _, token, _ = rig
    ctx = parse_route_context(context(handler="offchain.inference.Service.models", path="/v1/models", headers=[["authorization", f"Bearer {token}"]]))
    out = host.handle(ctx, b"")
    assert out["status"] == 200 and [m["id"] for m in json.loads(out["body"])["data"]] == ["cowboy/qwen"]
    assert host.handle(parse_route_context(context(handler="offchain.inference.Service.models", path="/v1/models")), b"")["status"] == 401
    other, _ = issue("other", "k", Ed25519PrivateKey.generate(), 123, ACTOR, now=NOW)
    assert host.handle(parse_route_context(context(handler="offchain.inference.Service.models", path="/v1/models", headers=[["authorization", f"Bearer {other}"]])), b"")["status"] == 401


def test_completion_is_one_session_and_closes_it(rig):
    host, service, upstream, token, _ = rig
    upstream.script.append((200, [json.dumps({"id": "c1", "object": "chat.completion", "model": "qwen3",
                                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                                              "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()]))
    ctx = parse_route_context(context(handler="offchain.inference.Service", verb="POST", path="/v1/chat/completions",
                                      headers=[["authorization", f"Bearer {token}"], ["content-type", "application/json"]]))
    out = host.handle(ctx, json.dumps({"model": "cowboy/qwen", "messages": [{"role": "user", "content": "hello"}]}).encode())
    assert out["status"] == 200, out
    assert json.loads(out["body"])["choices"][0]["message"]["content"] == "hi"
    assert service.closed == 1
    # A refused request (bad key) never opens a session.
    bad = parse_route_context(context(handler="offchain.inference.Service", verb="POST", path="/v1/chat/completions"))
    assert host.handle(bad, b"{}")["status"] == 401 and service.closed == 1


def test_streaming_completion_closes_the_session_after_the_last_frame(rig):
    host, service, upstream, token, _ = rig
    frames = [b'data: {"id":"c1","object":"chat.completion.chunk","model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":"He"}}]}\n\n',
              b'data: {"id":"c1","object":"chat.completion.chunk","model":"qwen3","choices":[{"index":0,"delta":{"content":"y"},"finish_reason":"stop"}]}\n\n',
              b'data: {"id":"c1","object":"chat.completion.chunk","model":"qwen3","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n',
              b"data: [DONE]\n\n"]
    upstream.script.append((200, frames))
    ctx = parse_route_context(context(handler="offchain.inference.Service", verb="POST", path="/v1/chat/completions",
                                      headers=[["authorization", f"Bearer {token}"]]))
    out = host.handle(ctx, json.dumps({"model": "cowboy/qwen", "stream": True, "messages": [{"role": "user", "content": "hello"}]}).encode())
    assert out["status"] == 200 and not isinstance(out["body"], bytes)
    assert service.closed == 0, "the session stays open while frames are still being relayed"
    body = b"".join(out["body"])
    assert b"[DONE]" in body and service.closed == 1


# ---- HTTP surface --------------------------------------------------------------

def test_http_server_relays_bodies_and_streams_chunked(rig):
    host, _, upstream, token, _ = rig
    server = make_server(host, "127.0.0.1:0")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/_cowboy/route", body=b"", headers={"x-cowboy-route-context": context(), "content-length": "0"})
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read())["actor"] == ACTOR.hex()
        conn.request("POST", "/_cowboy/route", body=b"", headers={"x-cowboy-route-context": "bad", "content-length": "0"})
        assert conn.getresponse().status == 400 and conn.getresponse  # consume
        conn.close()

        upstream.script.append((200, [b"data: [DONE]\n\n"]))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"model": "cowboy/qwen", "stream": True, "messages": [{"role": "user", "content": "hello"}]}).encode()
        conn.request("POST", "/_cowboy/route", body=body, headers={
            "x-cowboy-route-context": context(handler="offchain.inference.Service", verb="POST", path="/v1/chat/completions",
                                              headers=[["authorization", f"Bearer {token}"]]),
            "content-length": str(len(body))})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("transfer-encoding") == "chunked"
        assert resp.getheader("content-type", "").startswith("text/event-stream")
        assert b"[DONE]" in resp.read()
        conn.request("GET", "/_cowboy/health")
        assert conn.getresponse().read() == b"ok\n"
    finally:
        server.shutdown()
        server.server_close()


# ---- configuration --------------------------------------------------------------

def _config(tmp_path):
    return {
        "listen": "127.0.0.1:0", "actor": "0x" + ACTOR.hex(), "chain_id": 9999, "network": "canyon",
        "cbfs": {"binary": "cbfs", "rpc_url": "http://127.0.0.1:4000", "timeout_s": 30, "extra_args": ["--trusted-checkpoint", str(tmp_path / "ckpt")]},
        "nonce_volume": {"name": "inference-nonces", "state_dir": str(tmp_path / "nonce"), "owner": "0x" + OWNER.hex()},
        "slots": {"cloud": {"hire_id": 7, "pins": ["d", "v", "a"], "public_base_url": "https://api.example", "active_key_cap": 8,
                            "hire": {"hirer": "0x" + OWNER.hex()},
                            "volume": {"name": "cloud-hire", "state_dir": str(tmp_path / "hire"), "owner": "0x" + OWNER.hex()},
                            "grants_state_dir": str(tmp_path / "grants")}},
        "issuing_key_hex": "11" * 32,
        "upstream": {"headers": {"authorization": "Bearer up"}},
    }


def test_build_host_from_config(tmp_path):
    host = build_host(_config(tmp_path))
    assert host.actor == ACTOR and list(host.services) == ["cloud"] and host.management.grants is not None
    volume = host.services["cloud"].volume
    assert volume.extra_args[:1] == ["--trusted-checkpoint"] and host.services["cloud"].upstream.headers == {"authorization": "Bearer up"}


def test_build_host_refuses_incomplete_config(tmp_path):
    cfg = _config(tmp_path)
    for mutate in (lambda c: c.pop("nonce_volume"), lambda c: c["slots"]["cloud"].pop("volume"),
                   lambda c: c.update(issuing_key_hex="aa"), lambda c: c["slots"]["cloud"]["volume"].update(owner="nope"),
                   lambda c: c.update(slots={})):
        broken = json.loads(json.dumps(cfg))
        mutate(broken)
        with pytest.raises(ValueError):
            build_host(broken)
