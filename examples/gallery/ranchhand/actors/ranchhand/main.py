"""
Ranchhand — store-hireable, multi-tenant Slack assistant (CIP-33).

One deployed actor serves every hire (CIP-33 §2.6.4 shared-actor model).
Each hirer connects their own Slack workspace; per-workspace credentials
never touch this actor. Instead, every hire deploys its own tiny
credential-proxy actor (`proxy/slackrelay.py`) whose deploy manifest names
exactly the hirer's two CBSS secrets — so this actor's manifest carries
ZERO secrets entitlements, and tenant credentials are isolated per hire by
construction (an actor manifest can never grow keys after deploy, CIP-6
§12.1 subset rule, so a shared actor could not hold per-hirer keys even if
we wanted it to).

Message flow for one Slack event:

  Slack → gateway (CIP-15) → ranchhand.slack_http          [sync, ack-fast]
    ├─ url_verification answered inline
    ├─ SECRET-FREE prefilters (bound forged-dispatch cost; cowbot lineage)
    ├─ tenant lookup by Slack team_id (unknown team → drop)
    ├─ event filtering (mentions/DMs only, skip bot echoes)
    └─ ack 200 + relay {raw body, ts, sig} → tenant's proxy actor

  proxy.slackrelay (hirer-owned):
    1. runner.http chat.postMessage "thinking…" carrying
       verify={hirer's SLACK_SIGNING_SECRET} + secrets=[hirer's
       SLACK_BOT_TOKEN]. The runner MAC-checks the inbound webhook FIRST
       and aborts fail-closed on mismatch (CIP-24 §5.5) — a forged event
       posts nothing and never reaches the agent.
    2. reports tenant_verified {placeholder_ts} back → ranchhand

  ranchhand.tenant_answer (continuation):
    3. runner.agent over the PRIVATE artifact volume (ro) + the tenant's
       state volume (rw), with the job hire-bound via the `hire=` kwarg —
       the chain validates the hire is Active at dispatch and the runner
       re-authorizes against /trading-post/lease-authorize before
       execution and at every lease-epoch boundary. A paused/revoked hire
       gets no further compute.
    4. answer → proxy.post_answer → chat.update with the hirer's token.

Trust boundaries:
  - This actor holds no secrets and no per-tenant credentials.
  - connect_workspace binds team_id → (hire_id, proxy). The binding is
    bind-once per team_id (rebind only by the original binder or the
    owner); real authority comes from (a) the chain refusing hire-bound
    jobs for non-Active hires and (b) the proxy's MAC only passing for
    the party that actually holds that workspace's Slack app secrets.
    (An in-PVM Trading Post read surface would let this handler verify
    sender == hire.hirer on-chain; that read surface is RPC-only today —
    tracked as a CIP-33 follow-up.)
  - tenant_verified/tenant_rejected are accepted only from the tenant's
    registered proxy address (on-chain sender authentication).
"""

import hashlib
import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, deferred, public, callable_by, OWNER
from cowboy_sdk.routes import get, post
from cowboy_sdk import send as sdk_send


KEY_OWNER = "owner"
KEY_TENANTS = "tenants"            # dict: team_id -> tenant record (see connect_workspace)
KEY_DECLARATION = "declaration"    # dict: declaration_id/version/artifact_id (set post-declare)
KEY_ARTIFACT_VOLUME = "artifact_volume"
KEY_MOUNT_VOLUMES = "mount_volumes"
KEY_MODEL = "model"
KEY_PENDING = "pending"            # dict: thread_key -> request_id (single-flight per thread)
KEY_LAST_REQUEST_ID = "last_request_id"
KEY_REQUESTS = "requests"          # bounded request history for ops/debug
KEY_SEEN = "seen"                  # bounded (ts, signature, body_sha256) exact-replay dedupe
KEY_DISPATCH_COUNT = "dispatch_count"
MAX_REQUEST_HISTORY = 100
MAX_TENANTS = 1024

# ── Secret-free inbound prefilters (CIP-24 §5.5 DoS bound; cowbot lineage) ────
SLACK_TS_SKEW_SECONDS = 300
MAX_SEEN_ENTRIES = 256
MAX_DISPATCH_COUNT = 100_000
MAX_SLACK_BODY_BYTES = 60 * 1024

# Reserved KV slot consumed by the gateway's L1b routes cache (CIP-15 §6.5).
ROUTES_KEY = "__cowboy/routes"

DEFAULT_ARTIFACT_VOLUME = "ranchhand-brain"
DEFAULT_MODEL = "local"

# Inter-actor message kinds (CBOR maps; see module docstring for the flow).
MSG_RELAY_EVENT = "relay_event"          # ranchhand -> proxy
MSG_TENANT_VERIFIED = "tenant_verified"  # proxy -> ranchhand
MSG_TENANT_REJECTED = "tenant_rejected"  # proxy -> ranchhand
MSG_POST_ANSWER = "post_answer"          # ranchhand -> proxy


def _sender_hex() -> str:
    sender = runtime.get_sender()
    return "0x" + sender.hex() if isinstance(sender, bytes) else ""


def _norm_addr(addr: str) -> str:
    addr = str(addr or "").strip().lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    return addr


def _addr_bytes(addr: str) -> bytes:
    return bytes.fromhex(_norm_addr(addr))


def _owner_arg(owner: str) -> str:
    return _norm_addr(owner)


def _storage_str(storage, key: str, default: str) -> str:
    value = storage.get(key, default)
    if not value:
        return default
    return str(value)


def _thread_key(team_id: str, channel_id: str, thread_ts: str) -> str:
    h = hashlib.sha256(f"{team_id}:{channel_id}:{thread_ts}".encode("utf-8")).hexdigest()
    return h[:32]


def _bounded(requests):
    if len(requests) <= MAX_REQUEST_HISTORY:
        return requests
    return requests[-MAX_REQUEST_HISTORY:]


def _strip_mention(text: str, bot_user_id: str | None) -> str:
    if not text:
        return ""
    if bot_user_id:
        text = text.replace(f"<@{bot_user_id}>", "")
    return text.strip()


# ── CIP-14 HTTP envelope helpers (cowbot lineage) ─────────────────────────────

def _http_resp(status, headers, body):
    """Return CBOR-encoded HttpResponseEnvelope bytes."""
    if body is None:
        body = b""
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return codec.encode({
        "status": int(status),
        "headers": list(headers or []),
        "body": body,
    })


def _decode_envelope(payload):
    """Best-effort decode of a CIP-14 HttpRequestEnvelope."""
    obj = payload
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = codec.decode(bytes(obj))
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    if "body" not in obj or "method" not in obj:
        return None
    return obj


def _envelope_body(envelope):
    body = envelope.get("body") if isinstance(envelope, dict) else None
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b""


def _envelope_headers(envelope):
    headers = {}
    if isinstance(envelope, dict):
        for pair in envelope.get("headers") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                k, v = pair
                headers[str(k).lower()] = str(v)
    return headers


# ── Secret-free inbound prefilters (CIP-24 §5.5; none of these authenticate) ──

def _ts_is_fresh(ts: str, now: int) -> bool:
    if now <= 0:
        return True
    try:
        event_ts = int(float(ts))
    except (TypeError, ValueError):
        return False
    return abs(now - event_ts) <= SLACK_TS_SKEW_SECONDS


def _body_fingerprint(ts: str, signature: str, body_bytes: bytes) -> str:
    body_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    return f"{ts}|{signature}|{body_hash}"


def _decode_message(payload):
    """Decode an inter-actor CBOR message into a dict (or {})."""
    obj = payload
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = codec.decode(bytes(obj))
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


@actor
class RanchhandActor:
    def init(self, payload):
        # SDK-owner sentinel — gates @callable_by(OWNER) handlers. KEY_OWNER
        # below additionally records the deployer EOA as the CBFS volume owner
        # the agent jobs mount by.
        runtime.assume_ownership_if_unowned()
        cfg = json.loads(payload) if payload else {}
        owner = _sender_hex()
        self.storage[KEY_OWNER] = owner
        self.storage[KEY_ARTIFACT_VOLUME] = cfg.get("artifact_volume", DEFAULT_ARTIFACT_VOLUME)
        # mount_volumes=False runs the agent with an inline prompt and no CBFS
        # attachments — for environments without runner-storage FUSE (local
        # e2e). The hire-binding/lease mechanics are identical either way.
        self.storage[KEY_MOUNT_VOLUMES] = bool(cfg.get("mount_volumes", True))
        self.storage[KEY_MODEL] = cfg.get("model", DEFAULT_MODEL)
        self.storage[KEY_TENANTS] = {}
        self.storage[KEY_DECLARATION] = {}
        self.storage[KEY_PENDING] = {}
        self.storage[KEY_LAST_REQUEST_ID] = 0
        self.storage[KEY_REQUESTS] = []
        self.storage[KEY_SEEN] = []
        self.storage[KEY_DISPATCH_COUNT] = 0
        runtime.emit_event("ranchhand.init", {
            "owner": owner,
            "artifact_volume": self.storage[KEY_ARTIFACT_VOLUME],
        })
        return b"ok: initialized"

    # ── Owner configuration ──────────────────────────────────────────────────

    @callable_by(OWNER, decode="cbor")
    def set_declaration(self, payload):
        """Owner-only: record this actor's Trading Post pin after `declare`.

        The agent job's `hire=` binding must name the exact
        (declaration_id, version, artifact_id) the hires are pinned to;
        the chain rejects a binding whose pins don't match the hire.
        """
        cfg = payload or {}
        declaration_id = str(cfg.get("declaration_id", "")).strip()
        if not declaration_id:
            raise ValueError("set_declaration: declaration_id required")
        self.storage[KEY_DECLARATION] = {
            "declaration_id": declaration_id,
            "version": int(cfg.get("version", 1)),
            "artifact_id": int(cfg.get("artifact_id", 0)),
        }
        runtime.emit_event("ranchhand.declaration_set", self.storage[KEY_DECLARATION])
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        """Owner-only: install the CIP-15 routes table (raw CBOR Routes bytes)."""
        routes_bytes = payload if isinstance(payload, (bytes, bytearray)) else b""
        if not routes_bytes:
            raise ValueError("set_routes: empty payload")
        self.storage.set_raw(ROUTES_KEY, bytes(routes_bytes))
        runtime.emit_event("ranchhand.routes_installed", {"bytes": len(routes_bytes)})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def admin_disconnect(self, payload):
        """Owner-only: prune a tenant binding (squatted team_id, offboarding)."""
        cfg = payload or {}
        team_id = str(cfg.get("team_id", ""))
        tenants = dict(self.storage.get(KEY_TENANTS, {}))
        if team_id in tenants:
            tenants.pop(team_id)
            self.storage[KEY_TENANTS] = tenants
            runtime.emit_event("ranchhand.tenant_disconnected", {"team_id": team_id, "by": "owner"})
        return b"ok"

    # ── Tenant onboarding (hirer-signed) ─────────────────────────────────────

    @public(decode="cbor")
    def connect_workspace(self, payload):
        """Bind a Slack team_id to a hire and its credential-proxy actor.

        Called by the hirer (signed tx) after they have: hired this actor on
        the Trading Post, created their Slack app, stored both Slack secrets
        under their own account (ACL'd to their proxy), and deployed the
        proxy. See connect-workspace.sh.

        Binding rules (no chain lookup available in-PVM; see module
        docstring): a team_id binds once; rebinding requires the original
        binder or the owner. A squatted binding can never serve the
        workspace (the squatter's proxy fails the Slack MAC) and is
        owner-prunable via admin_disconnect.
        """
        cfg = payload or {}
        team_id = str(cfg.get("team_id", "")).strip()
        proxy = _norm_addr(cfg.get("proxy", ""))
        state_volume = str(cfg.get("state_volume", "")).strip()
        try:
            hire_id = int(cfg.get("hire_id"))
        except (TypeError, ValueError):
            raise ValueError("connect_workspace: hire_id required")
        if not team_id or not proxy:
            raise ValueError("connect_workspace: team_id and proxy required")
        if not state_volume and self.storage.get(KEY_MOUNT_VOLUMES, True):
            raise ValueError("connect_workspace: state_volume required")
        if len(proxy) != 40:
            raise ValueError("connect_workspace: proxy must be a 20-byte hex address")

        sender = _sender_hex()
        owner = self.storage.get(KEY_OWNER, "")
        tenants = dict(self.storage.get(KEY_TENANTS, {}))
        existing = tenants.get(team_id)
        if existing and existing.get("hirer") not in (sender, owner):
            raise ValueError("connect_workspace: team_id already bound by another account")
        if not existing and len(tenants) >= MAX_TENANTS:
            raise ValueError("connect_workspace: tenant capacity reached")

        # A hire pins one serving artifact (CIP-33 §2.2): artifact 0 for a
        # direct hire, the granting store's artifact for a store hire. The
        # hire-bound job must name the tenant's ACTUAL pin, so it is part of
        # the binding (defaults to the declaration-level pin when omitted).
        declaration = self.storage.get(KEY_DECLARATION, {})
        artifact_id = cfg.get("artifact_id")
        if artifact_id is None:
            artifact_id = declaration.get("artifact_id", 0)

        tenants[team_id] = {
            "hire_id": hire_id,
            "artifact_id": int(artifact_id),
            "proxy": proxy,
            "state_volume": state_volume,
            "hirer": sender,
            "status": "active",
        }
        self.storage[KEY_TENANTS] = tenants
        runtime.emit_event("ranchhand.workspace_connected", {
            "team_id": team_id,
            "hire_id": hire_id,
            "proxy": "0x" + proxy,
            "hirer": sender,
        })
        return b"ok: workspace connected"

    @public(decode="cbor")
    def disconnect_workspace(self, payload):
        """Hirer-signed: deactivate their own team binding."""
        cfg = payload or {}
        team_id = str(cfg.get("team_id", ""))
        sender = _sender_hex()
        tenants = dict(self.storage.get(KEY_TENANTS, {}))
        existing = tenants.get(team_id)
        if not existing:
            return b"ok: not bound"
        if existing.get("hirer") != sender:
            raise ValueError("disconnect_workspace: only the binding hirer may disconnect")
        tenants.pop(team_id)
        self.storage[KEY_TENANTS] = tenants
        runtime.emit_event("ranchhand.tenant_disconnected", {"team_id": team_id, "by": "hirer"})
        return b"ok"

    # ── Gateway-facing handlers (CIP-14 / CIP-15) ─────────────────────────────

    @public(decode="raw")
    @get("/health")
    @pure
    def health(self, payload):
        return _http_resp(200, [("content-type", "text/plain; charset=utf-8")], b"ok")

    @public(decode="raw")
    @deferred
    @post("/slack/events")
    def slack_http(self, payload):
        """POST /slack/events — single gateway webhook for ALL tenants.

        Sync ack-fast: prefilters, tenant lookup by team_id, event
        filtering, then relay the VERBATIM raw body + MAC headers to the
        tenant's credential proxy. The HMAC is NOT verified here — this
        actor holds no signing secret for any tenant; the proxy's first
        runner.http `verify=` is the gate (CIP-24 §5.5). Everything below
        only bounds forged-dispatch cost.
        """

        envelope = _decode_envelope(payload) or {}
        body_bytes = _envelope_body(envelope)
        headers = _envelope_headers(envelope)
        ts = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")

        if len(body_bytes) > MAX_SLACK_BODY_BYTES:
            runtime.emit_event("ranchhand.slack_body_too_large", {"bytes": len(body_bytes)})
            return _http_resp(200, [], b"")

        try:
            slack_body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            slack_body = {}

        # URL verification — Slack's one-time setup ping (echo, no secret).
        if slack_body.get("type") == "url_verification":
            challenge = slack_body.get("challenge", "")
            return _http_resp(
                200,
                [("content-type", "application/json; charset=utf-8")],
                json.dumps({"challenge": challenge}).encode("utf-8"),
            )

        # (a) Timestamp freshness (Slack's own replay window).
        now = runtime.get_timestamp_ms() // 1000
        if not _ts_is_fresh(ts, now):
            runtime.emit_event("ranchhand.slack_prefilter_drop", {"reason": "stale_ts"})
            return _http_resp(200, [], b"")

        # (b) Exact-replay dedupe on the only fields fixed before the MAC.
        fingerprint = _body_fingerprint(ts, signature, body_bytes)
        seen = list(self.storage.get(KEY_SEEN, []))
        if fingerprint in seen:
            runtime.emit_event("ranchhand.slack_prefilter_drop", {"reason": "exact_replay"})
            return _http_resp(200, [], b"")
        seen.append(fingerprint)
        if len(seen) > MAX_SEEN_ENTRIES:
            seen = seen[-MAX_SEEN_ENTRIES:]
        self.storage[KEY_SEEN] = seen

        # (c) Coarse global dispatch cap (spend cap is the true ceiling).
        dispatch_count = int(self.storage.get(KEY_DISPATCH_COUNT, 0))
        if dispatch_count >= MAX_DISPATCH_COUNT:
            runtime.emit_event("ranchhand.slack_prefilter_drop", {"reason": "dispatch_cap"})
            return _http_resp(200, [], b"")

        # (d) Tenant lookup — unknown or inactive team is a cheap drop.
        team_id = str(slack_body.get("team_id", "") or "")
        tenants = self.storage.get(KEY_TENANTS, {})
        tenant = tenants.get(team_id)
        if not tenant or tenant.get("status") != "active":
            runtime.emit_event("ranchhand.slack_prefilter_drop", {
                "reason": "unknown_team", "team_id": team_id,
            })
            return _http_resp(200, [], b"")

        # (e) Event filtering on UNVERIFIED data — bounds proxy work only;
        # a forged body that passes these still dies at the proxy's MAC.
        event = slack_body.get("event") or {}
        event_type = event.get("type")
        if event_type not in ("app_mention", "message"):
            return _http_resp(200, [], b"")
        if event_type == "message":
            if event.get("subtype"):
                return _http_resp(200, [], b"")
            if event.get("channel_type") not in ("im", "mpim") and not event.get("thread_ts"):
                return _http_resp(200, [], b"")
        bot_user_id = (slack_body.get("authorizations") or [{}])[0].get("user_id")
        if event.get("bot_id") or (event.get("user") and event.get("user") == bot_user_id):
            return _http_resp(200, [], b"")

        channel_id = event.get("channel", "")
        event_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or event_ts
        text = _strip_mention(event.get("text", ""), bot_user_id)
        if not channel_id or not text:
            return _http_resp(200, [], b"")

        # (f) Per-thread single-flight.
        thread_key = _thread_key(team_id, channel_id, thread_ts)
        pending = dict(self.storage.get(KEY_PENDING, {}))
        if thread_key in pending:
            runtime.emit_event("ranchhand.slack_prefilter_drop", {"reason": "thread_pending"})
            return _http_resp(200, [], b"")

        # Record the request; the proxy round-trip echoes request_id back.
        request_id = int(self.storage.get(KEY_LAST_REQUEST_ID, 0)) + 1
        requests = list(self.storage.get(KEY_REQUESTS, []))
        requests.append({
            "request_id": request_id,
            "team_id": team_id,
            "thread_key": thread_key,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "user_id": event.get("user", ""),
            "text": text,
            "status": "relayed",
        })
        pending[thread_key] = request_id
        self.storage[KEY_LAST_REQUEST_ID] = request_id
        self.storage[KEY_PENDING] = pending
        self.storage[KEY_REQUESTS] = _bounded(requests)
        self.storage[KEY_DISPATCH_COUNT] = dispatch_count + 1

        # Relay to the tenant's credential proxy. raw body + ts + sig MUST
        # survive verbatim — the proxy's runner `verify=` MAC is computed
        # over `v0:<ts>:<raw_body>` exactly as Slack signed it.
        relay = codec.encode({
            "kind": MSG_RELAY_EVENT,
            "request_id": request_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "raw_body": body_bytes,
            "ts": ts,
            "sig": signature,
        })
        try:
            sdk_send(_addr_bytes(tenant["proxy"]), relay, raw=True)
            runtime.emit_event("ranchhand.event_relayed", {
                "request_id": request_id,
                "team_id": team_id,
                "hire_id": tenant.get("hire_id"),
            })
        except Exception as e:
            runtime.emit_event("ranchhand.relay_failed", {"error": str(e)[:200]})

        return _http_resp(200, [], b"")

    # ── Proxy callbacks ──────────────────────────────────────────────────────

    @public(decode="raw")
    @deferred
    def handle_message(self, payload):
        """Inter-actor mailbox: tenant_verified / tenant_rejected from a proxy.

        Sender authentication: the message is honored only when the on-chain
        sender is the registered proxy for the request's team binding.
        """
        msg = _decode_message(payload)
        kind = msg.get("kind")
        if kind == MSG_TENANT_VERIFIED:
            return self._on_tenant_verified(msg)
        if kind == MSG_TENANT_REJECTED:
            return self._on_tenant_rejected(msg)
        runtime.emit_event("ranchhand.unknown_message", {"kind": str(kind)[:40]})
        return b"ok: ignored"

    def _tenant_for_msg(self, msg):
        """Resolve the tenant a proxy message refers to.

        NOTE on cross-actor identity: for an async `send_message` delivery (as
        opposed to a synchronous `call()`), `runtime.get_sender()` returns the
        RECEIVING actor itself, not the sending actor — the deferred delivery
        tx's `from` is the executor. So we CANNOT authenticate the proxy by
        sender here (the canonical async pattern, examples/core/03, likewise
        carries identity in the payload, not via get_sender). Security instead
        rests on: (a) `_on_tenant_verified` only acts on a `request_id` THIS
        actor created that is still in `relayed` state (a forged verify must
        hit a live in-flight request), and (b) the agent job it triggers is
        hire-bound — a non-Active hire yields no compute. A spurious trigger
        costs only the tenant's own hire resources.
        """
        team_id = str(msg.get("team_id", "") or "")
        tenant = self.storage.get(KEY_TENANTS, {}).get(team_id)
        if not tenant:
            return None, None
        return team_id, tenant

    def _request_by_id(self, request_id):
        for entry in self.storage.get(KEY_REQUESTS, []):
            if entry.get("request_id") == request_id:
                return dict(entry)
        return None

    def _update_request(self, request_id, **fields):
        requests = list(self.storage.get(KEY_REQUESTS, []))
        for entry in requests:
            if entry.get("request_id") == request_id:
                entry.update(fields)
        self.storage[KEY_REQUESTS] = _bounded(requests)

    def _clear_pending(self, thread_key, request_id):
        pending = dict(self.storage.get(KEY_PENDING, {}))
        if pending.get(thread_key) == request_id:
            pending.pop(thread_key, None)
        self.storage[KEY_PENDING] = pending

    def _on_tenant_rejected(self, msg):
        team_id, tenant = self._tenant_for_msg(msg)
        if not tenant:
            return b"ok: unauthenticated"
        request_id = int(msg.get("request_id", 0) or 0)
        request = self._request_by_id(request_id)
        if request:
            self._clear_pending(request.get("thread_key", ""), request_id)
            self._update_request(request_id, status="rejected")
        runtime.emit_event("ranchhand.request_rejected", {
            "request_id": request_id, "team_id": team_id,
        })
        return b"ok: rejected recorded"

    def _on_tenant_verified(self, msg):
        team_id, tenant = self._tenant_for_msg(msg)
        if not tenant:
            return b"ok: unauthenticated"
        request_id = int(msg.get("request_id", 0) or 0)
        request = self._request_by_id(request_id)
        if not request or request.get("status") != "relayed":
            runtime.emit_event("ranchhand.stale_verification", {"request_id": request_id})
            return b"ok: stale"
        declaration = self.storage.get(KEY_DECLARATION, {})
        if not declaration.get("declaration_id"):
            self._clear_pending(request.get("thread_key", ""), request_id)
            self._update_request(request_id, status="no_declaration")
            runtime.emit_event("ranchhand.no_declaration", {"request_id": request_id})
            return b"ok: no declaration"
        job = {
            "request_id": request_id,
            "team_id": team_id,
            "placeholder_ts": str(msg.get("placeholder_ts", "") or ""),
        }
        return self.tenant_answer(job)

    # ── The hire-bound agent job ─────────────────────────────────────────────

    @runner.continuation
    async def tenant_answer(self, payload):

        job = _decode_message(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(job.get("request_id", 0) or 0)
        ctx.team_id = str(job.get("team_id", "") or "")
        ctx.placeholder_ts = str(job.get("placeholder_ts", "") or "")

        tenant = self.storage.get(KEY_TENANTS, {}).get(ctx.team_id) or {}
        request = None
        for entry in self.storage.get(KEY_REQUESTS, []):
            if entry.get("request_id") == ctx.request_id:
                request = dict(entry)
        if not tenant or not request:
            return b"ok: unknown request"

        ctx.thread_key = request.get("thread_key", "")
        ctx.channel_id = request.get("channel_id", "")
        ctx.thread_ts = request.get("thread_ts", "")
        ctx.text = request.get("text", "")
        ctx.proxy = tenant.get("proxy", "")
        ctx.state_volume = tenant.get("state_volume", "")
        ctx.hire_id = int(tenant.get("hire_id", 0))
        ctx.owner_hex = _owner_arg(self.storage.get(KEY_OWNER, ""))
        ctx.artifact_volume = _storage_str(self.storage, KEY_ARTIFACT_VOLUME, DEFAULT_ARTIFACT_VOLUME)
        ctx.model = _storage_str(self.storage, KEY_MODEL, DEFAULT_MODEL)

        declaration = self.storage.get(KEY_DECLARATION, {})
        ctx.declaration_id = str(declaration.get("declaration_id", ""))
        ctx.version = int(declaration.get("version", 1))
        # The tenant's pinned artifact (recorded at connect), not the
        # declaration default — a store hire pins the store's artifact.
        ctx.artifact_id = int(tenant.get("artifact_id", declaration.get("artifact_id", 0)))

        requests = list(self.storage.get(KEY_REQUESTS, []))
        for entry in requests:
            if entry.get("request_id") == ctx.request_id:
                entry["status"] = "verified"
        self.storage[KEY_REQUESTS] = _bounded(requests)

        # The hire-bound agent job. `hire=` makes the chain validate at
        # dispatch that the hire is Active and pinned to exactly this
        # declaration/version/artifact (validate_hire_binding), and makes the
        # runner authorize against /trading-post/lease-authorize before
        # executing and at every lease-epoch boundary. Pause/revoke ⇒ no
        # further compute for this tenant. The artifact volume is the
        # PRIVATE brain (system prompt + playbooks); the state volume is the
        # tenant's own conversation memory. mount_volumes=False (local e2e,
        # no runner-storage FUSE) runs the same hire-gated job with an
        # inline prompt and runner-local session storage instead.
        agent_kwargs = {
            "session_id": ctx.thread_key,
            "hire": {
                "hire_id": ctx.hire_id,
                "declaration_id": ctx.declaration_id,
                "version": ctx.version,
                "artifact_id": ctx.artifact_id,
            },
        }
        if self.storage.get(KEY_MOUNT_VOLUMES, True):
            agent_kwargs["system_prompt_path"] = (
                f"/cbfs/{ctx.artifact_volume}/ranchhand/system_prompt.txt"
            )
            agent_kwargs["session_dir"] = f"/cbfs/{ctx.state_volume}/.sessions"
            agent_kwargs["volumes"] = [
                [ctx.artifact_volume, "ro", ctx.owner_hex],
                [ctx.state_volume, "rw", ctx.owner_hex],
            ]
        else:
            agent_kwargs["system_prompt_inline"] = (
                "You are Ranchhand, a concise Slack assistant. Answer plainly."
            )
        agent_result = await runner.agent(
            model=ctx.model,
            query=ctx.text,
            **agent_kwargs,
        )

        # runner.agent returns a structured {"answer": "<text>", ...} result.
        agent_data = agent_result.raw
        if not isinstance(agent_data, dict):
            # Terminal failure, never a raise: nothing but a clear here
            # releases the thread's KEY_PENDING lock (there is no timeout
            # sweeper), so a raise drops every later message as
            # "already pending".
            self._clear_pending(ctx.thread_key, ctx.request_id)
            self._update_request(ctx.request_id, status="agent_error")
            runtime.emit_event("ranchhand.agent_error", {
                "request_id": ctx.request_id,
                "team_id": ctx.team_id,
                "error": "runner.agent did not return a JSON object",
            })
            return codec.encode({
                "request_id": ctx.request_id,
                "status": "agent_error",
            })
        ctx.answer = str(agent_data.get("answer") or "")
        if not ctx.answer:
            ctx.answer = "(no response — request_id=" + str(ctx.request_id) + ")"

        # Hand the answer to the tenant's proxy for posting with THEIR token.
        reply = codec.encode({
            "kind": MSG_POST_ANSWER,
            "request_id": ctx.request_id,
            "channel_id": ctx.channel_id,
            "thread_ts": ctx.thread_ts,
            "placeholder_ts": ctx.placeholder_ts,
            "text": ctx.answer,
        })
        sdk_send(bytes.fromhex(ctx.proxy), reply, raw=True)

        pending = dict(self.storage.get(KEY_PENDING, {}))
        if pending.get(ctx.thread_key) == ctx.request_id:
            pending.pop(ctx.thread_key, None)
        self.storage[KEY_PENDING] = pending

        requests = list(self.storage.get(KEY_REQUESTS, []))
        for entry in requests:
            if entry.get("request_id") == ctx.request_id:
                entry["status"] = "answered"
                entry["answer_len"] = len(ctx.answer)
        self.storage[KEY_REQUESTS] = _bounded(requests)

        runtime.emit_event("ranchhand.answered", {
            "request_id": ctx.request_id,
            "team_id": ctx.team_id,
            "hire_id": ctx.hire_id,
            "answer_len": len(ctx.answer),
        })
        return codec.encode({"request_id": ctx.request_id})

    @pure
    def on_runner_result(self, payload):
        try:
            msg = codec.decode(payload) if isinstance(payload, bytes) else payload
        except Exception:
            msg = {}
        runner.handle_runner_result(self, msg)
        return b"ok: runner result handled"

    # ── Read-only ops surface ────────────────────────────────────────────────

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_tenants(self, payload):
        tenants = self.storage.get(KEY_TENANTS, {})
        redacted = {
            team: {
                "hire_id": t.get("hire_id"),
                "proxy": "0x" + str(t.get("proxy", "")),
                "status": t.get("status"),
            }
            for team, t in tenants.items()
        }
        return redacted

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_requests(self, payload):
        return self.storage.get(KEY_REQUESTS, [])

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_pending(self, payload):
        return self.storage.get(KEY_PENDING, {})


def _get_actor():
    return RanchhandActor()


def init(payload):
    return _get_actor().init(payload)


def set_declaration(payload):
    return _get_actor().set_declaration(payload)


def set_routes(payload):
    return _get_actor().set_routes(payload)


def admin_disconnect(payload):
    return _get_actor().admin_disconnect(payload)


def connect_workspace(payload):
    return _get_actor().connect_workspace(payload)


def disconnect_workspace(payload):
    return _get_actor().disconnect_workspace(payload)


def health(payload):
    return _get_actor().health(payload)


def slack_http(payload):
    return _get_actor().slack_http(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def tenant_answer(payload):
    return _get_actor().tenant_answer(payload)


def tenant_answer__resume(payload):
    return _get_actor().tenant_answer__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_tenants(payload):
    return _get_actor().get_tenants(payload)


def get_requests(payload):
    return _get_actor().get_requests(payload)


def get_pending(payload):
    return _get_actor().get_pending(payload)
