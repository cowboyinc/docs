"""Serving process for this actor's off-chain handlers, run as a CIP-10
persistent workload (the container the deploy image starts).

Two Gateway paths reach it, both carrying a base64url CBOR route context in
`x-cowboy-route-context`:

- Workload routes (CIP-15 §8.15, the deployed path): the Gateway relays the
  caller's real request (method, path, query, headers, body) to the workload
  record's serving endpoint. The context names the actor and workload and
  carries the caller auth fields; the handler is chosen by path.
- Runner routes (`Target::Runner`): the Gateway posts to `/_cowboy/route`
  with the handler name, query and allowlisted headers in the context.

Either way this process supplies what the request must never supply: actor
identity, hire slots, volume bindings, the ingress issuing key, the upstream
credential, all from one operator-written config file. Chain admission per
invocation, cbssd secret release and metering settlement are the runner
executor's job and are not done here; sessions are one request long.

    python -m offchain.host --config host.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import keys
from .http_upstream import HttpUpstream
from .inference import Refused, Service
from .manage import Hire, Request, Slot
from .management_runtime import AccountProof, VolumeBinding, build_management

log = logging.getLogger("offchain.host")

ROUTE_PATH = "/_cowboy/route"
MANAGEMENT_HANDLER = "offchain.manage.Management.handle"
MODELS_HANDLER = "offchain.inference.Service.models"
COMPLETIONS_HANDLER = "offchain.inference.Service"
MAX_CONTEXT_BYTES = 64 * 1024
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024


class ContextError(ValueError):
    pass


# ---- CBOR (the subset ciborium emits for RouteContext) ----------------------

def _cbor_decode(data: bytes):
    value, end = _cbor_item(data, 0)
    if end != len(data):
        raise ContextError("trailing bytes")
    return value


def _cbor_item(data: bytes, i: int):
    if i >= len(data):
        raise ContextError("truncated")
    major, info = data[i] >> 5, data[i] & 0x1F
    i += 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        width = 1 << (info - 24)
        if i + width > len(data):
            raise ContextError("truncated")
        n = int.from_bytes(data[i:i + width], "big")
        i += width
    elif info == 31 and major in (2, 3, 4, 5):
        return _cbor_indefinite(data, i, major)
    else:
        raise ContextError("unsupported length encoding")
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major in (2, 3):
        if i + n > len(data):
            raise ContextError("truncated")
        raw = data[i:i + n]
        return (raw.decode("utf-8") if major == 3 else bytes(raw)), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = _cbor_item(data, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(n):
            k, i = _cbor_item(data, i)
            v, i = _cbor_item(data, i)
            out[k] = v
        return out, i
    if major == 7:
        if info == 20:
            return False, i
        if info == 21:
            return True, i
        if info == 22:
            return None, i
        raise ContextError("unsupported simple value")
    raise ContextError(f"unsupported major type {major}")


def _cbor_indefinite(data: bytes, i: int, major: int):
    items = []
    while True:
        if i >= len(data):
            raise ContextError("truncated")
        if data[i] == 0xFF:
            i += 1
            break
        v, i = _cbor_item(data, i)
        items.append(v)
    if major == 2:
        return b"".join(items), i
    if major == 3:
        return "".join(items), i
    if major == 4:
        return items, i
    if len(items) % 2:
        raise ContextError("odd map")
    return dict(zip(items[0::2], items[1::2])), i


def _bytes_field(value, label: str, size: int | None = None) -> bytes | None:
    """ciborium writes `Vec<u8>` and `[u8; N]` as arrays of small ints."""
    if value is None:
        return None
    if isinstance(value, list):
        if not all(isinstance(b, int) and 0 <= b <= 255 for b in value):
            raise ContextError(f"{label} is not a byte array")
        value = bytes(value)
    if not isinstance(value, bytes):
        raise ContextError(f"{label} is not bytes")
    if size is not None and len(value) != size:
        raise ContextError(f"{label} must be {size} bytes")
    return value


@dataclass(frozen=True)
class RouteContext:
    actor: bytes
    handler: str | None  # runner routes name the handler; workload routes dispatch by path
    verb: str
    path: str
    query: str
    headers: tuple[tuple[str, str], ...]
    auth: str  # "public" | "account"
    account_sig: bytes | None
    account_expiry: int | None
    account_nonce: bytes | None
    chain_id: int
    network: str
    workload: bytes | None = None

    @property
    def raw_target(self) -> str:
        return self.path + ("?" + self.query if self.query else "")

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers if k.lower() == name), "")


def parse_route_context(encoded: str) -> RouteContext:
    if not encoded or len(encoded) > MAX_CONTEXT_BYTES:
        raise ContextError("route context missing or too large")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError as exc:
        raise ContextError("route context is not base64url") from exc
    ctx = _cbor_decode(raw)
    if not isinstance(ctx, dict):
        raise ContextError("route context is not a map")
    for name in ("verb", "path"):
        if not isinstance(ctx.get(name), str) or not ctx[name]:
            raise ContextError(f"route context {name} missing")
    handler = ctx.get("handler")
    if handler is not None and (not isinstance(handler, str) or not handler):
        raise ContextError("route context handler malformed")
    workload = _bytes_field(ctx.get("workload"), "workload", 32)
    if handler is None and workload is None:
        raise ContextError("route context names neither a handler nor a workload")
    # gateway_types::Auth is `rename_all = "lowercase"` on the wire.
    auth = ctx.get("auth", "public")
    if not isinstance(auth, str) or auth.lower() not in ("public", "account"):
        raise ContextError("unknown auth")
    headers = []
    for pair in ctx.get("headers") or []:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            raise ContextError("malformed forwarded header")
        headers.append((pair[0].lower(), pair[1]))
    query = ctx.get("query") or ""
    if not isinstance(query, str):
        raise ContextError("malformed query")
    expiry = ctx.get("account_expiry")
    if expiry is not None and (not isinstance(expiry, int) or expiry < 0):
        raise ContextError("malformed account expiry")
    chain_id, network = ctx.get("chain_id", 0), ctx.get("network", "")
    if not isinstance(chain_id, int) or not isinstance(network, str):
        raise ContextError("malformed chain binding")
    return RouteContext(
        actor=_bytes_field(ctx.get("actor_address"), "actor_address", 20),
        handler=handler, verb=ctx["verb"].upper(), path=ctx["path"], query=query,
        headers=tuple(headers), auth=auth.lower(),
        account_sig=_bytes_field(ctx.get("account_sig"), "account_sig", 65),
        account_expiry=expiry,
        account_nonce=_bytes_field(ctx.get("account_nonce"), "account_nonce"),
        chain_id=chain_id, network=network, workload=workload,
    )


def with_request(ctx: RouteContext, *, path: str, query: str, headers) -> RouteContext:
    """Workload routes relay the caller's real request: its path, query and
    headers are authoritative, the context supplies identity and auth."""
    return RouteContext(actor=ctx.actor, handler=ctx.handler, verb=ctx.verb, path=path, query=query,
                        headers=tuple((k.lower(), v) for k, v in headers), auth=ctx.auth,
                        account_sig=ctx.account_sig, account_expiry=ctx.account_expiry,
                        account_nonce=ctx.account_nonce, chain_id=ctx.chain_id, network=ctx.network,
                        workload=ctx.workload)


def handler_for_path(path: str) -> str | None:
    """Workload-route dispatch: the route table's handler mapping, by path."""
    if path == "/manage" or path.startswith("/manage/"):
        return MANAGEMENT_HANDLER
    if path == "/v1/models":
        return MODELS_HANDLER
    if path == "/v1/chat/completions":
        return COMPLETIONS_HANDLER
    return None


# ---- dispatch ----------------------------------------------------------------

def _openai_error(status: int, message: str, type_: str, code: str | None = None) -> dict:
    return {"status": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps({"error": {"message": message, "type": type_, "code": code}}).encode()}


class Host:
    """Executor stand-in: routes a decoded context to the handler it names."""

    def __init__(self, *, actor: bytes, chain_id: int, network: str, management, services: dict[str, Service],
                 max_body_bytes: int = DEFAULT_MAX_BODY_BYTES):
        self.actor, self.chain_id, self.network = actor, chain_id, network
        self.management, self.services = management, dict(services)
        self.max_body_bytes = max_body_bytes

    def handle(self, ctx: RouteContext, body: bytes) -> dict:
        if ctx.actor != self.actor:
            return _openai_error(404, "unknown actor", "invalid_request_error")
        if len(body) > self.max_body_bytes:
            return _openai_error(413, "request body too large", "invalid_request_error")
        # The Gateway gates presence of the signature; the runner enforces it
        # too so a request that reaches this port directly cannot skip auth.
        if ctx.auth == "account" and ctx.account_sig is None:
            return _openai_error(401, "account signature required", "invalid_request_error")
        handler = ctx.handler if ctx.handler is not None else handler_for_path(ctx.path)
        if handler == MANAGEMENT_HANDLER:
            return self._management(ctx, body)
        if handler == MODELS_HANDLER:
            return self._models(ctx)
        if handler == COMPLETIONS_HANDLER:
            return self._completion(ctx, body)
        return _openai_error(404, "unknown handler", "invalid_request_error")

    def _management(self, ctx: RouteContext, body: bytes) -> dict:
        proof = None
        if ctx.account_sig is not None:
            if ctx.account_expiry is None or ctx.account_nonce is None:
                return _openai_error(401, "incomplete account proof", "invalid_request_error")
            proof = AccountProof(ctx.account_sig, ctx.account_expiry, ctx.account_nonce)
        # chain_id/network in the context are the caller's binding (zero and
        # empty on public routes); the handler checks its own deployment
        # identity, so the executor's values go in.
        req = Request(ctx.verb, ctx.raw_target, body, self.actor, self.chain_id, self.network,
                      MANAGEMENT_HANDLER, proof)
        return self.management.handle(req)

    def _service_for(self, ctx: RouteContext) -> Service | Refused:
        auth = ctx.header("authorization")
        parsed = keys.parse(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if parsed is None or parsed.slot not in self.services:
            return Refused(401, "invalid or revoked API key", "invalid_request_error", "invalid_api_key")
        return self.services[parsed.slot]

    def _models(self, ctx: RouteContext) -> dict:
        service = self._service_for(ctx)
        if isinstance(service, Refused):
            return _openai_error(service.status, service.message, service.type_, service.code)
        try:
            listing = service.models(dict(ctx.headers))
        except Refused as err:
            return _openai_error(err.status, err.message, err.type_, err.code)
        return {"status": 200, "headers": {"content-type": "application/json", "cache-control": "no-store"},
                "body": json.dumps(listing).encode()}

    def _completion(self, ctx: RouteContext, body: bytes) -> dict:
        service = self._service_for(ctx)
        if isinstance(service, Refused):
            return _openai_error(service.status, service.message, service.type_, service.code)
        headers = dict(ctx.headers)
        try:
            session = service.open_session(headers, {"session_id": uuid.uuid4().hex})
        except Refused as err:
            return _openai_error(err.status, err.message, err.type_, err.code)
        try:
            out = service.chat_completions(session, {"ctx": {"request_id": uuid.uuid4().hex}, "body": body})
        except Refused as err:
            service.close_session(session)
            return _openai_error(err.status, err.message, err.type_, err.code)
        if isinstance(out.get("body"), (bytes, bytearray)):
            service.close_session(session)
            return out
        # Streaming: close the session once the relay finishes or the client goes away.
        inner = out["body"]

        def closing():
            try:
                yield from inner
            finally:
                close = getattr(inner, "close", None)
                if close is not None:
                    close()
                service.close_session(session)

        return {**out, "body": closing()}


# ---- configuration ------------------------------------------------------------

def _addr(value, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise ValueError(f"{label} must be a 0x-prefixed 20-byte hex address")
    return bytes.fromhex(value[2:])


def _binding(cfg: dict, cbfs: dict, label: str) -> VolumeBinding:
    if not isinstance(cfg, dict) or set(cfg) - {"name", "state_dir", "owner"} or not {"name", "state_dir", "owner"} <= set(cfg):
        raise ValueError(f"{label} needs exactly name, state_dir, owner")
    return VolumeBinding(cfg["name"], cfg["state_dir"], _addr(cfg["owner"], f"{label}.owner"),
                         binary=cbfs.get("binary", "cbfs"), rpc_url=cbfs.get("rpc_url"),
                         timeout_s=float(cbfs.get("timeout_s", 60)), extra_args=tuple(cbfs.get("extra_args", ())))


def build_host(config: dict, *, clock=None) -> Host:
    """Executor-supplied configuration only. Every key is explicit; nothing is
    read from the request path."""
    required = {"listen", "actor", "chain_id", "network", "cbfs", "nonce_volume", "slots", "issuing_key_hex", "upstream"}
    if not isinstance(config, dict) or not required <= set(config):
        raise ValueError(f"host config needs {sorted(required)}")
    actor = _addr(config["actor"], "actor")
    chain_id, network = config["chain_id"], config["network"]
    if not isinstance(chain_id, int) or chain_id <= 0 or not isinstance(network, str) or not network:
        raise ValueError("chain_id must be a positive int and network a nonempty string")
    cbfs = config["cbfs"]
    if not isinstance(cbfs, dict):
        raise ValueError("cbfs must be a map")
    seed = bytes.fromhex(config["issuing_key_hex"])
    if len(seed) != 32:
        raise ValueError("issuing_key_hex must be a 32-byte ed25519 seed")
    issuing = Ed25519PrivateKey.from_private_bytes(seed)
    upstream_cfg = config["upstream"]
    if not isinstance(upstream_cfg, dict):
        raise ValueError("upstream must be a map")
    upstream = HttpUpstream(headers=dict(upstream_cfg.get("headers") or {}),
                            connect_timeout_s=float(upstream_cfg.get("connect_timeout_s", 10)),
                            idle_timeout_s=float(upstream_cfg.get("idle_timeout_s", 120)),
                            max_connection_s=float(upstream_cfg.get("max_connection_s", 600)))

    slots: dict[str, Slot] = {}
    hires: dict[int, Hire] = {}
    volumes: dict[int, VolumeBinding] = {}
    grants: dict[int, VolumeBinding] = {}
    for name, slot in config["slots"].items():
        needed = {"hire_id", "pins", "public_base_url", "active_key_cap", "hire", "volume"}
        if not isinstance(slot, dict) or not needed <= set(slot):
            raise ValueError(f"slot {name!r} needs {sorted(needed)}")
        pins = slot["pins"]
        if not (isinstance(pins, list) and len(pins) == 3 and all(isinstance(p, str) for p in pins)):
            raise ValueError(f"slot {name!r}: pins must be three strings")
        hire_id = slot["hire_id"]
        if not isinstance(hire_id, int) or hire_id < 0 or hire_id in hires:
            raise ValueError(f"slot {name!r}: hire_id must be a unique non-negative int")
        slots[name] = Slot(hire_id, tuple(pins), slot["public_base_url"], int(slot["active_key_cap"]))
        hire = slot["hire"]
        hires[hire_id] = Hire(_addr(hire["hirer"], f"slot {name!r} hirer"), tuple(pins), hire.get("status", "active"))
        volumes[hire_id] = _binding(slot["volume"], cbfs, f"slot {name!r} volume")
        if "grants_state_dir" in slot:
            grants[hire_id] = VolumeBinding(volumes[hire_id].name, slot["grants_state_dir"], volumes[hire_id].owner,
                                            binary=volumes[hire_id].binary, rpc_url=volumes[hire_id].rpc_url,
                                            timeout_s=volumes[hire_id].timeout_s, extra_args=volumes[hire_id].extra_args)
    if not slots:
        raise ValueError("at least one slot is required")
    nonce = _binding(config["nonce_volume"], cbfs, "nonce_volume")
    kwargs = {} if clock is None else {"clock": clock}
    management = build_management(
        actor=actor, chain_id=chain_id, network=network, slots=slots,
        hire_lookup=hires.get, volumes=volumes, nonce_volume=nonce,
        issue_key=lambda slot, name, now: keys.issue(slot, name, issuing, chain_id, actor, now=now),
        grant_bindings=grants or None, **kwargs)
    services = {name: Service(volumes[slot.hire_id].volume(), upstream, slot=name, hire_id=slot.hire_id, **kwargs)
                for name, slot in slots.items()}
    return Host(actor=actor, chain_id=chain_id, network=network, management=management, services=services,
                max_body_bytes=int(config.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)))


# ---- HTTP ----------------------------------------------------------------------

def make_server(host: Host, listen: str) -> ThreadingHTTPServer:
    bind, _, port = listen.rpartition(":")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no request lines with targets/tokens in logs
            pass

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def do_PUT(self):
            self._route()

        def do_PATCH(self):
            self._route()

        def do_DELETE(self):
            self._route()

        def _route(self):
            encoded = self.headers.get("x-cowboy-route-context")
            if encoded is None:
                if self.command == "GET" and self.path == "/_cowboy/health":
                    return self._respond({"status": 200, "headers": {"content-type": "text/plain"}, "body": b"ok\n"})
                return self._respond(_openai_error(404, "not found", "invalid_request_error"))
            try:
                ctx = parse_route_context(encoded)
            except ContextError as exc:
                return self._respond(_openai_error(400, f"bad route context: {exc}", "invalid_request_error"))
            runner_route = self.command == "POST" and self.path == ROUTE_PATH and ctx.handler is not None
            if not runner_route:
                # Workload relay: the caller's request itself arrived here.
                path, _, query = self.path.partition("?")
                ctx = with_request(ctx, path=path, query=query, headers=self.headers.items())
                if ctx.verb != self.command:
                    return self._respond(_openai_error(400, "route context verb mismatch", "invalid_request_error"))
            length = self.headers.get("content-length")
            try:
                n = int(length) if length is not None else 0
            except ValueError:
                return self._respond(_openai_error(400, "bad content-length", "invalid_request_error"))
            if n < 0:
                return self._respond(_openai_error(400, "bad content-length", "invalid_request_error"))
            if n > host.max_body_bytes:
                return self._respond(_openai_error(413, "request body too large", "invalid_request_error"))
            body = self.rfile.read(n) if n else b""
            try:
                out = host.handle(ctx, body)
            except Exception:  # noqa: BLE001 - the executor boundary; never leak internals
                log.exception("handler failure handler=%s path=%s", ctx.handler, ctx.path)
                out = _openai_error(500, "internal error", "server_error")
            self._respond(out)

        def _respond(self, out: dict):
            body = out.get("body", b"")
            headers = {k.lower(): v for k, v in (out.get("headers") or {}).items()}
            self.send_response(int(out.get("status", 200)))
            for k, v in headers.items():
                if k not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            if isinstance(body, (bytes, bytearray)):
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                for chunk in body:
                    if not chunk:
                        continue
                    self.wfile.write(b"%x\r\n" % len(chunk) + bytes(chunk) + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                close = getattr(body, "close", None)
                if close is not None:
                    close()
                self.close_connection = True

    server = ThreadingHTTPServer((bind or "127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="inference actor serving host")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = json.loads(args.config.read_text())
    host = build_host(config)
    server = make_server(host, config["listen"])
    log.info("serving host listening on %s for actor 0x%s (%d slots)", config["listen"], host.actor.hex(), len(host.services))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
