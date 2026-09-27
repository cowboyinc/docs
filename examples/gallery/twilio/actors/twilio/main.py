"""
Twilio — store-hireable, multi-tenant two-way SMS assistant (CIP-33).

One deployed actor serves every hire (CIP-33 §2.6.4 shared-actor model).
Each hirer connects their own Twilio number; per-tenant credentials never
touch this actor. Instead, every hire deploys its own tiny credential-proxy
actor (`proxy/twiliorelay.py`) whose deploy manifest names exactly the
hirer's two CBSS secrets — so this actor's manifest carries ZERO secrets
entitlements, and tenant credentials are isolated per hire by construction.

Two-key split (Option B — the long-lived Auth Token is never used outbound):
  - secrets.verify: TWILIO_AUTH_TOKEN   — Twilio signs inbound webhooks with
    it (X-Twilio-Signature); the runner MAC-checks with it, fail-closed.
  - secrets.read:   TWILIO_API_BASIC    — a base64("SKsid:secret") HTTP Basic
    blob for a scoped Standard API Key, used only for the outbound REST send.

## Authenticated control flow (post-review hardening)

Every cross-actor hop is an AUTHENTICATED synchronous `call()`, not an
`@public` async mailbox — because `runtime.get_sender()` on an async
`send_message` delivery is the RECEIVING actor itself, so an async mailbox
cannot authenticate its caller. A synchronous `call()` DOES carry the caller
identity, so each callee asserts `get_sender()` before doing anything
sensitive. A `@deferred` handler reached via `call()` returns synchronously
(the auth check is atomic) while its async side effects (starting a runner
continuation) run in subsequent blocks — exactly the shape we need.

  Twilio → gateway (CIP-15) → twilio.twilio_http            [sync, ack-fast]
    · SECRET-FREE prefilters (dedupe, per-tenant rate cap, single-flight)
    · tenant lookup by the destination number `To`
    · store request N with digest = H(preimage ‖ sig), status=relayed
    · call() tenant.proxy.relay_event{N, tenant, preimage, sig, digest}
      (proxy asserts sender == this twilio actor)

  proxy.verify_and_ack (runner continuation): a NO-SECRET Twilio GET that
    carries only verify= (the runner recomputes the Twilio HMAC-SHA1 over the
    relayed preimage and aborts fail-closed on mismatch, CIP-24 §5.5 — before
    any agent compute or key release). On pass it records N single-use and
    call()s back:

  twilio.tenant_verified{N, tenant, digest}   [proxy → twilio, authenticated]
    · assert sender == tenant.proxy
    · assert request N is status=relayed AND its stored digest == digest
    · flip status → verified (single-use) and schedule a FUNDED self-timer that
      launches the hire-bound agent next block. The timer (not a self-send) is
      what lets the runner.agent job run from THIS actor (the `hire=` submitter)
      AND be funded: a self-send re-enters as a 162-access deferred child that
      cannot fund the nested submit_job (D36 / E1005), while a self-timer at
      TIMER_ACCESS_LIMIT (322) reserves one full deferred child for it.

  twilio.tenant_answer (runner continuation): runner.agent with session_id =
    the (To,From) conversation key (two-way memory), hire-bound via `hire=`.
    Then:

  proxy.post_answer{N, to, from, text}         [twilio → proxy, authenticated]
    · proxy asserts sender == this twilio actor AND N is in its single-use
      verified set (consume) → sends ONE SMS via POST Messages.json with the
      hirer's API-key Basic auth.

## Privacy boundary (stated plainly)

This is NOT a private-messaging path. The inbound signature preimage and the
outbound message body carry the correspondent's phone number and the SMS text
in cleartext through consensus/job payloads (the relay `call()`, the JobSpec
the runner executes). Treat SMS content as public-to-the-chain, exactly like
any other actor input/output. CBSS protects the Twilio CREDENTIALS, not the
message contents.
"""

import hashlib

import json
from cowboy_sdk import actor, runner, capture, codec, runtime, pure, deferred, public, callable_by, OWNER
from cowboy_sdk.routes import get, post
from cowboy_sdk import call as sdk_call


KEY_OWNER = "owner"
KEY_INFLIGHT = "inflight"          # dict: twilio_number -> count of relayed-but-unfinished reqs
KEY_DECLARATION = "declaration"    # dict: declaration_id/version/artifact_id (set post-declare)
KEY_ARTIFACT_VOLUME = "artifact_volume"
KEY_MOUNT_VOLUMES = "mount_volumes"
KEY_MODEL = "model"
KEY_WEBHOOK_URL = "webhook_url"    # exact public URL Twilio signs over (preimage prefix)
KEY_LAST_REQUEST_ID = "last_request_id"

# ── Per-item KV layout (each Cowboy actor-storage VALUE is hard-capped at
# MAX_ACTOR_KV_VALUE_BYTES = 64 KiB, fail-closed; per-actor MAX_ACTOR_KV_COUNT =
# 10_000 keys). A single aggregate dict (all tenants / all active requests / the
# whole replay list) would blow the 64 KiB value cap after only modest use — the
# replay list alone trips it at ~1k messages. So each collection is stored as
# per-item keys (value size is bounded by ONE entry, never the count) with small
# index/count values, and the replay guard is SHARDED. There is no Python-level
# prefix scan, so every collection that must be enumerated keeps an explicit
# index value (itself provably < 64 KiB). ──────────────────────────────────────
KEY_TENANT_PREFIX = "t:"          # t:<number> -> tenant record (see connect_number)
KEY_TENANT_INDEX = "tenant_index"  # list of bound numbers (count + enumeration; <= MAX_TENANTS)
KEY_ACTIVE_PREFIX = "a:"          # a:<request_id> -> live request record (holds the SMS text)
# Per-tenant active-id index — the ids a number currently has in flight, so a
# force-purge (owner admin_disconnect) can find them without a key scan. Bounded
# by MAX_TENANT_INFLIGHT, so this value stays tiny.
KEY_TENANT_ACTIVE_PREFIX = "ta:"  # ta:<number> -> [request_id, ...]
KEY_PENDING_PREFIX = "p:"         # p:<conversation_key> -> request_id (per-conversation single-flight)
# Durable POST-MAC replay guard, SHARDED by the digest's first hex char into 16
# buckets so no single value approaches the 64 KiB per-value cap, while the whole
# guard stays inside the per-actor BYTE quota (RENT_QUOTA_BASE = 1 MiB default).
# Digests are stored as the RAW 32-byte hash (not 64-char hex) to halve the
# footprint. Only MAC-VERIFIED digests enter it, so an attacker cannot flood/evict
# it. The only residual, replaying a webhook older than the window, re-sends the
# ORIGINAL signed content to the ORIGINAL correspondent — a duplicate SMS.
KEY_PROCESSED_PREFIX = "pr:"      # pr:<1 hex> -> [raw 32-byte digest, ...] (bounded per shard)
# 16 shards x 128 = ~2048 recent-digest window (covers Twilio's retry horizon at
# realistic volume); each shard value <= 128 * ~34 B, whole guard ~78 KiB.
MAX_PROCESSED_PER_SHARD = 128
# Modest tenant ceiling: worst-case live storage is ~MAX_TENANTS *
# MAX_TENANT_INFLIGHT active+pending records; kept small so the aggregate (see the
# byte-budget note below) fits the 1 MiB base quota. The 8 MiB bonded quota
# supports proportionally more.
MAX_TENANTS = 96
# Cap stored inbound text to a real SMS length (concatenated SMS <= ~1600). MAC
# is computed over the untouched preimage, so truncating the STORED copy is safe;
# it bounds both the a:<id> record and the continuation's captured copy.
MAX_TEXT_BYTES = 1600
# Global budget on the sum of live inbound-text bytes across all active requests
# (tracked in KEY_ACTIVE_BYTES, released on every finish). This is the byte-level
# analogue of the per-tenant/running COUNT caps: it stops many concurrent
# large-body messages from exhausting the per-actor storage quota (a valid write
# failing mid-flow would strand a workflow). Sized with headroom under 1 MiB for
# active-record metadata, the replay guard, tenants, and SDK continuation state.
ACTIVE_TEXT_BUDGET_BYTES = 256 * 1024
KEY_ACTIVE_BYTES = "active_bytes"  # int: sum of live inbound-text bytes
# Boundary caps on stored owner/hirer-supplied config strings so no single record
# (esp. a tenant's state_volume / the webhook URL) can approach the 64 KiB value
# cap. Rejected at the boundary rather than truncated — config must be exact.
MAX_CONFIG_STR_BYTES = 256         # volume names, model id, etc.
MAX_URL_BYTES = 512                # webhook URL (signature preimage prefix)
MAX_NUMBER_BYTES = 32              # E.164 phone number (also a tenant KV key)

# ── Secret-free inbound prefilter bound (CIP-24 §5.5) ─────────────────────────
MAX_SMS_BODY_BYTES = 16 * 1024     # a Twilio form webhook is small; SMS ≤ 1600 chars
# Per-tenant in-flight cap. A forged webhook to the public gateway still costs
# the tenant's proxy one (failed-MAC) verify op; this bounds how many such ops
# one tenant can have outstanding, capping the forged-webhook gas-drain. There
# is deliberately NO global lifetime dispatch cap — a never-reset counter is a
# permanent-DoS footgun; concurrency (this cap + per-conversation single-flight)
# is what bounds load.
MAX_TENANT_INFLIGHT = 8
# Global RUNNING-agent cap. The PVM limits an actor to 100 ACTIVE continuations
# (ContinuationCountLimitError). On THIS shared actor the only continuation is
# tenant_answer (the agent), started POST-MAC in run_agent — so the cap that
# matters for continuation capacity counts running agents, NOT pre-MAC relayed
# attempts. Counting pre-MAC would let forged webhooks across tenants occupy the
# budget and starve real traffic. Kept safely below 100. Rolling (frees on
# finish). The per-tenant pre-MAC inflight cap above is a SEPARATE load bound.
MAX_GLOBAL_RUNNING = 64
KEY_RUNNING = "running_count"     # global count of active tenant_answer continuations
# A runner/CBSS outage can prevent the proxy's verification callback forever.
# Keep a crashed workflow from blocking the SMS conversation for minutes:
# Canyon currently advances roughly eight blocks per ten seconds, so a later
# inbound message can quickly reclaim an abandoned request.
REQUEST_TIMEOUT_BLOCKS = 8

# Per-fire access budget for the funded self-timer that launches the hire-bound
# agent (see tenant_verified). A plain self-send re-enters this actor as a
# deferred child capped at DEFERRED_ACCESS_LIMIT = 162 (node
# types/src/constants.rs) — it cannot both do its own work AND fund the nested
# runner.agent submit_job (needs REQUIRED_DEFERRED_CHILD_ACCESSES = 162), so the
# node refuses the child (E1005) and the reply never happens (decision D36). A
# timer fired on THIS actor carries the same from==self attribution the hire
# binding requires, but at TIMER_ACCESSES_LIMIT = TIMER_HANDLER_ACCESS_ALLOWANCE
# (160) + REQUIRED_DEFERRED_CHILD_ACCESSES (162) = 322, which reserves one full
# deferred child for the runner job. Pinned by the node
# (types/src/constants.rs: TIMER_ACCESSES_LIMIT, "timer cap must preserve one
# complete deferred-child reserve").
TIMER_ACCESS_LIMIT = 322

# Reserved KV slot consumed by the gateway's L1b routes cache (CIP-15 §6.5).
ROUTES_KEY = "__cowboy/routes"

DEFAULT_ARTIFACT_VOLUME = "twilio-brain"
DEFAULT_MODEL = "local"
DEFAULT_WEBHOOK_URL = ""


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


def _self_bytes() -> bytes:
    """This actor's own 20-byte address (self-send target)."""
    a = runtime.get_actor_address()
    return a if isinstance(a, bytes) else bytes.fromhex(_norm_addr(str(a)))


def _owner_arg(owner: str) -> str:
    return _norm_addr(owner)


def _storage_str(storage, key: str, default: str) -> str:
    value = storage.get(key, default)
    if not value:
        return default
    return str(value)


def _norm_number(number: str) -> str:
    """Canonicalize a phone number for keying (E.164 kept as-is, trimmed)."""
    return str(number or "").strip()


def _conversation_key(to_number: str, from_number: str) -> str:
    h = hashlib.sha256(f"{to_number}:{from_number}".encode("utf-8")).hexdigest()
    return h[:32]


def _request_digest(preimage: bytes, sig: str) -> str:
    """Bind a request to the EXACT bytes the proxy will MAC. Stored at relay
    time and re-checked on the proxy's authenticated `tenant_verified` callback
    so the proxy cannot report a verification for content other than what this
    actor relayed for that request id."""
    h = hashlib.sha256()
    h.update(preimage or b"")
    h.update(b"\x00")
    h.update((sig or "").encode("utf-8"))
    return h.hexdigest()


def _bounded(items, cap):
    if len(items) <= cap:
        return items
    return items[-cap:]


def _digest_raw(digest: str):
    """The raw 32 bytes of a hex sha256 digest (compact replay-guard storage),
    or None if the digest is empty/malformed."""
    if not digest:
        return None
    try:
        return bytes.fromhex(digest)
    except ValueError:
        return None


def _text_bytes(text: str) -> int:
    return len((text or "").encode("utf-8"))


def _check_len(value: str, cap: int, field: str) -> str:
    """Reject an over-long stored config string at the boundary, so no single
    record can approach the 64 KiB value cap."""
    value = str(value or "")
    if len(value.encode("utf-8")) > cap:
        raise ValueError(f"{field}: exceeds {cap}-byte limit")
    return value


def _truncate_text(text: str) -> str:
    """Clamp stored inbound text to a real SMS length at a UTF-8 boundary. The
    Twilio MAC is over the untouched preimage, so truncating the STORED copy is
    signature-safe and just bounds per-request + continuation storage."""
    raw = (text or "").encode("utf-8")[:MAX_TEXT_BYTES]
    return raw.decode("utf-8", "ignore")


# ── x-www-form-urlencoded parsing (Twilio webhook body; no urllib in PVM) ──────

def _pct_decode(s: str) -> str:
    """Percent-decode a urlencoded token at the byte level, then UTF-8.

    Twilio signs over the DECODED parameter values, so this must reproduce the
    exact bytes Twilio's own parser saw (`+`→space, `%XX`→byte)."""
    b = bytearray()
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "+":
            b.append(0x20)
            i += 1
        elif c == "%" and i + 2 < n:
            try:
                b.append(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                b.append(ord(c))
                i += 1
        else:
            b.append(ord(c) & 0xFF)
            i += 1
    return b.decode("utf-8", "replace")


def _parse_form(body_bytes) -> dict:
    """Parse an x-www-form-urlencoded body into {key: [values]}.

    Duplicate keys are PRESERVED as a list (not collapsed) — Twilio's
    signature covers every value of a repeated key, so collapsing to one
    value would compute the wrong MAC (e.g. media webhooks repeat keys)."""
    out = {}
    if not body_bytes:
        return out
    try:
        text = body_bytes.decode("utf-8")
    except Exception:
        return out
    for pair in text.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        out.setdefault(_pct_decode(key), []).append(_pct_decode(value))
    return out


def _first(params: dict, key: str) -> str:
    """First value for a form key (single-valued Twilio fields: From/To/…)."""
    values = params.get(key)
    return values[0] if values else ""


def _twilio_preimage(url: str, params: dict) -> bytes:
    """Canonical Twilio signature preimage (twilio-python RequestValidator):
    URL, then for each key in sorted order, each of that key's DISTINCT values
    in sorted order, appended name+value with no delimiters. Upstream uses
    `sorted(set(values))`, so repeated-identical values collapse to one
    (`Foo=a&Foo=a` → `Fooa`); distinct duplicates keep both (`Foo=b&Foo=a` →
    `FooaFoob`)."""
    parts = [url]
    for key in sorted(params.keys()):
        for value in sorted(set(params[key])):
            parts.append(key)
            parts.append(value)
    return "".join(parts).encode("utf-8")


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


def _decode_args(payload):
    """Decode a synchronous call()'s CBOR args into a dict (or {})."""
    obj = payload
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = codec.decode(bytes(obj))
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


@actor
class TwilioActor:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        cfg = json.loads(payload) if payload else {}
        owner = _sender_hex()
        self.storage[KEY_OWNER] = owner
        self.storage[KEY_ARTIFACT_VOLUME] = _check_len(
            cfg.get("artifact_volume", DEFAULT_ARTIFACT_VOLUME), MAX_CONFIG_STR_BYTES, "artifact_volume")
        self.storage[KEY_MOUNT_VOLUMES] = bool(cfg.get("mount_volumes", True))
        self.storage[KEY_MODEL] = _check_len(cfg.get("model", DEFAULT_MODEL), MAX_CONFIG_STR_BYTES, "model")
        # The EXACT public URL Twilio is configured to POST to — the first field
        # of the signature preimage, so it must match byte-for-byte.
        self.storage[KEY_WEBHOOK_URL] = _check_len(
            cfg.get("webhook_url", DEFAULT_WEBHOOK_URL), MAX_URL_BYTES, "webhook_url")
        self.storage[KEY_TENANT_INDEX] = []
        self.storage[KEY_INFLIGHT] = {}
        self.storage[KEY_DECLARATION] = {}
        self.storage[KEY_LAST_REQUEST_ID] = 0
        self.storage[KEY_RUNNING] = 0
        self.storage[KEY_ACTIVE_BYTES] = 0
        runtime.emit_event("twilio.init", {
            "owner": owner,
            "artifact_volume": self.storage[KEY_ARTIFACT_VOLUME],
        })
        return b"ok: initialized"

    # ── Owner configuration ──────────────────────────────────────────────────

    @callable_by(OWNER, decode="cbor")
    def set_declaration(self, payload):
        """Owner-only: record this actor's Trading Post pin after `declare`."""
        cfg = payload or {}
        declaration_id = _check_len(
            str(cfg.get("declaration_id", "")).strip(), MAX_CONFIG_STR_BYTES, "declaration_id")
        if not declaration_id:
            raise ValueError("set_declaration: declaration_id required")
        self.storage[KEY_DECLARATION] = {
            "declaration_id": declaration_id,
            "version": int(cfg.get("version", 1)),
            "artifact_id": int(cfg.get("artifact_id", 0)),
        }
        runtime.emit_event("twilio.declaration_set", self.storage[KEY_DECLARATION])
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def set_webhook_url(self, payload):
        """Owner-only: set the exact public URL Twilio signs over."""
        cfg = payload or {}
        url = _check_len(str(cfg.get("webhook_url", "")).strip(), MAX_URL_BYTES, "webhook_url")
        if not url:
            raise ValueError("set_webhook_url: webhook_url required")
        self.storage[KEY_WEBHOOK_URL] = url
        runtime.emit_event("twilio.webhook_url_set", {"webhook_url": url})
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        """Owner-only: install the CIP-15 routes table (raw CBOR Routes bytes)."""
        routes_bytes = payload if isinstance(payload, (bytes, bytearray)) else b""
        if not routes_bytes:
            raise ValueError("set_routes: empty payload")
        self.storage.set_raw(ROUTES_KEY, bytes(routes_bytes))
        runtime.emit_event("twilio.routes_installed", {"bytes": len(routes_bytes)})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def admin_disconnect(self, payload):
        """Owner-only: prune a tenant binding (squatted number, offboarding).

        Unlike hirer `disconnect_number`, this FORCES the prune even with
        requests in flight — a squatter can otherwise hold the binding open by
        flooding forged webhooks (which keep pre-MAC in-flight > 0 but never run
        an agent). A squatted number has no genuine running continuation, so the
        only residual is: force-pruning a legitimate tenant mid-request may let
        one already-launched continuation resume and send its ORIGINAL reply to
        the ORIGINAL correspondent after teardown. That's a deliberate,
        owner-initiated, low-harm tradeoff for guaranteed prunability."""
        cfg = payload or {}
        number = _norm_number(cfg.get("twilio_number", ""))
        if self._get_tenant(number):
            self._del_tenant(number)
            self._purge_tenant(number)
            runtime.emit_event("twilio.tenant_disconnected", {"twilio_number": number, "by": "owner"})
        return b"ok"

    # ── Tenant onboarding (hirer-signed) ─────────────────────────────────────

    @public(decode="cbor")
    def connect_number(self, payload):
        """Bind a Twilio number to a hire and its credential-proxy actor.

        Binding rules: a number binds once; rebinding requires the original
        binder or the owner. A squatted binding can never serve the number
        (the squatter's proxy fails the Twilio MAC) and is owner-prunable.
        """
        cfg = payload or {}
        # twilio_number is stored as the key `t:<number>` (and a tenant_index
        # entry), so bound it too — E.164 is <= 16 chars; MAX_NUMBER_BYTES guards
        # both the 256-byte key cap and the index size.
        twilio_number = _check_len(_norm_number(cfg.get("twilio_number", "")), MAX_NUMBER_BYTES, "twilio_number")
        proxy = _norm_addr(cfg.get("proxy", ""))
        state_volume = _check_len(str(cfg.get("state_volume", "")).strip(), MAX_CONFIG_STR_BYTES, "state_volume")
        try:
            hire_id = int(cfg.get("hire_id"))
        except (TypeError, ValueError):
            raise ValueError("connect_number: hire_id required")
        if not twilio_number or not proxy:
            raise ValueError("connect_number: twilio_number and proxy required")
        if not state_volume and self.storage.get(KEY_MOUNT_VOLUMES, True):
            raise ValueError("connect_number: state_volume required")
        if len(proxy) != 40:
            raise ValueError("connect_number: proxy must be a 20-byte hex address")

        sender = _sender_hex()
        owner = self.storage.get(KEY_OWNER, "")
        existing = self._get_tenant(twilio_number)
        if existing and existing.get("hirer") not in (sender, owner):
            raise ValueError("connect_number: number already bound by another account")
        if not existing and self._tenant_count() >= MAX_TENANTS:
            raise ValueError("connect_number: tenant capacity reached")

        declaration = self.storage.get(KEY_DECLARATION, {})
        artifact_id = cfg.get("artifact_id")
        if artifact_id is None:
            artifact_id = declaration.get("artifact_id", 0)

        # Rebind: a new binding (esp. a new proxy) would orphan any in-flight
        # request under the old binding — and its already-captured continuation
        # can't be cancelled, so it would resume against the OLD proxy and send a
        # late SMS crossing the rebind. Refuse to rebind while requests are in
        # flight; drain (or owner `admin_disconnect`) first, then rebind cleanly.
        if existing and self._inflight(twilio_number) > 0:
            raise ValueError("connect_number: requests in flight under current binding; retry after they drain")
        self._put_tenant(twilio_number, {
            "hire_id": hire_id,
            "artifact_id": int(artifact_id),
            "proxy": proxy,
            "state_volume": state_volume,
            "hirer": sender,
            "status": "active",
        })
        runtime.emit_event("twilio.number_connected", {
            "twilio_number": twilio_number,
            "hire_id": hire_id,
            "proxy": "0x" + proxy,
            "hirer": sender,
        })
        return b"ok: number connected"

    @public(decode="cbor")
    def disconnect_number(self, payload):
        """Hirer-signed: deactivate their own number binding."""
        cfg = payload or {}
        number = _norm_number(cfg.get("twilio_number", ""))
        sender = _sender_hex()
        existing = self._get_tenant(number)
        if not existing:
            return b"ok: not bound"
        if existing.get("hirer") != sender:
            raise ValueError("disconnect_number: only the binding hirer may disconnect")
        # Draining, not cancellation: a request already in flight holds a captured
        # continuation this teardown cannot cancel — if we removed the binding now,
        # that continuation would still resume and call the (now-orphaned) proxy,
        # sending a late SMS after disconnect. Require the caller to drain first;
        # in-flight requests clear in seconds. (Owner `admin_disconnect` can force
        # past this for a squatted/stuck binding.)
        if self._inflight(number) > 0:
            raise ValueError("disconnect_number: requests in flight; retry after they drain")
        self._del_tenant(number)
        self._purge_tenant(number)
        runtime.emit_event("twilio.tenant_disconnected", {"twilio_number": number, "by": "hirer"})
        return b"ok"

    # ── Per-item tenant records (t:<number> + a small index) ─────────────────

    def _get_tenant(self, number: str):
        return self.storage.get(KEY_TENANT_PREFIX + _norm_number(number))

    def _tenant_numbers(self) -> list:
        return list(self.storage.get(KEY_TENANT_INDEX, []))

    def _tenant_count(self) -> int:
        return len(self._tenant_numbers())

    def _put_tenant(self, number: str, record: dict):
        number = _norm_number(number)
        self.storage[KEY_TENANT_PREFIX + number] = record
        index = self._tenant_numbers()
        if number not in index:
            index.append(number)
            self.storage[KEY_TENANT_INDEX] = index

    def _del_tenant(self, number: str):
        number = _norm_number(number)
        key = KEY_TENANT_PREFIX + number
        if key in self.storage:
            del self.storage[key]
        self.storage[KEY_TENANT_INDEX] = [n for n in self._tenant_numbers() if n != number]

    # ── Per-item request records (a:<id>) + per-tenant active-id index (ta:) ──

    def _request_by_id(self, request_id):
        entry = self.storage.get(KEY_ACTIVE_PREFIX + str(request_id))
        return dict(entry) if entry else None

    def _put_request(self, request_id, record: dict):
        self.storage[KEY_ACTIVE_PREFIX + str(request_id)] = record

    def _update_request(self, request_id, **fields):
        key = KEY_ACTIVE_PREFIX + str(request_id)
        entry = self.storage.get(key)
        if entry:
            entry.update(fields)
            self.storage[key] = entry

    def _del_request(self, request_id):
        key = KEY_ACTIVE_PREFIX + str(request_id)
        if key in self.storage:
            del self.storage[key]

    def _ta_ids(self, number: str) -> list:
        return list(self.storage.get(KEY_TENANT_ACTIVE_PREFIX + _norm_number(number), []))

    def _ta_add(self, number: str, request_id):
        key = KEY_TENANT_ACTIVE_PREFIX + _norm_number(number)
        ids = list(self.storage.get(key, []))
        if request_id not in ids:
            ids.append(request_id)
            self.storage[key] = ids

    def _ta_remove(self, number: str, request_id):
        key = KEY_TENANT_ACTIVE_PREFIX + _norm_number(number)
        ids = [x for x in self.storage.get(key, []) if x != request_id]
        if ids:
            self.storage[key] = ids
        elif key in self.storage:
            del self.storage[key]

    # ── Per-conversation single-flight (p:<conversation_key>) ────────────────

    def _pending_get(self, conversation_key: str):
        return self.storage.get(KEY_PENDING_PREFIX + conversation_key)

    def _pending_set(self, conversation_key: str, request_id):
        self.storage[KEY_PENDING_PREFIX + conversation_key] = request_id

    def _pending_del(self, conversation_key: str, request_id):
        key = KEY_PENDING_PREFIX + conversation_key
        if self.storage.get(key) == request_id:
            del self.storage[key]

    # ── Sharded durable replay guard (pr:<2 hex>) ────────────────────────────

    def _is_processed(self, digest: str) -> bool:
        raw = _digest_raw(digest)
        if raw is None:
            return False
        return raw in self.storage.get(KEY_PROCESSED_PREFIX + digest[:1], [])

    def _mark_processed(self, digest: str):
        """Record a MAC-verified digest (as its RAW 32 bytes) in the digest's
        replay shard. Bounded per shard, but only MAC-verified entries enter it,
        so an attacker can't evict it (they can't produce a MAC-verified digest)."""
        raw = _digest_raw(digest)
        if raw is None:
            return
        key = KEY_PROCESSED_PREFIX + digest[:1]
        shard = list(self.storage.get(key, []))
        if raw not in shard:
            shard.append(raw)
        self.storage[key] = _bounded(shard, MAX_PROCESSED_PER_SHARD)

    # ── in-flight accounting (per-tenant gas-drain cap) + running count ──────

    def _inflight(self, number: str) -> int:
        return int(self.storage.get(KEY_INFLIGHT, {}).get(number, 0))

    def _active_bytes(self) -> int:
        return int(self.storage.get(KEY_ACTIVE_BYTES, 0))

    def _active_bytes_add(self, delta: int):
        """Track the sum of live inbound-text bytes so the global text budget can
        cap total storage pressure (released on every finish)."""
        self.storage[KEY_ACTIVE_BYTES] = max(0, self._active_bytes() + int(delta))

    def _running(self) -> int:
        return int(self.storage.get(KEY_RUNNING, 0))

    def _running_add(self, delta: int):
        """Adjust the global count of RESERVED tenant_answer continuations. This
        is the number that must stay under the PVM's 100-continuation limit — it
        counts only POST-MAC reserved/running agents, not pre-MAC relayed
        attempts. Reserved synchronously in tenant_verified; released on every
        terminal path."""
        self.storage[KEY_RUNNING] = max(0, self._running() + int(delta))

    def _inflight_add(self, number: str, delta: int):
        counts = dict(self.storage.get(KEY_INFLIGHT, {}))
        new = max(0, int(counts.get(number, 0)) + delta)
        if new == 0:
            counts.pop(number, None)
        else:
            counts[number] = new
        self.storage[KEY_INFLIGHT] = counts

    def _purge_tenant(self, number: str):
        """Force-drop all in-flight state for a number (owner admin_disconnect).
        Hirer disconnect/rebind already refuse while in-flight > 0, so this only
        does real work on an owner force-purge. A running request holds a live
        continuation the purge cannot cancel (it may still resume and send its
        original reply) — but its slots and reservation are released here so they
        don't strand. Enumerates via the per-tenant active-id index (no key scan)."""
        number = _norm_number(number)
        for request_id in self._ta_ids(number):
            entry = self._request_by_id(request_id)
            self._del_request(request_id)
            if entry:
                # Release a held continuation reservation (verified or running).
                if entry.get("status") in ("verified", "running"):
                    self._running_add(-1)
                self._active_bytes_add(-_text_bytes(entry.get("text", "")))
                self._pending_del(entry.get("conversation_key", ""), request_id)
                runtime.emit_event("twilio.request_purged", {
                    "request_id": request_id, "twilio_number": number,
                })
        key = KEY_TENANT_ACTIVE_PREFIX + number
        if key in self.storage:
            del self.storage[key]
        counts = dict(self.storage.get(KEY_INFLIGHT, {}))
        counts.pop(number, None)
        self.storage[KEY_INFLIGHT] = counts

    # ── Gateway-facing handlers (CIP-14 / CIP-15) ─────────────────────────────

    @public(decode="raw")
    @get("/health")
    @pure
    def health(self, payload):
        return _http_resp(200, [("content-type", "text/plain; charset=utf-8")], b"ok")

    @public(decode="raw")
    @deferred
    @post("/twilio/sms")
    def twilio_http(self, payload):
        """POST /twilio/sms — single gateway webhook for ALL tenants.

        Ack-fast: prefilters, tenant lookup by `To`, then AUTHENTICATED
        `call()` to the tenant's proxy (which asserts sender == this actor).
        The HMAC is NOT verified here — this actor holds no Auth Token; the
        proxy's runner `verify=` is the gate. The 200 body is empty TwiML so
        Twilio sends no auto-reply.
        """
        empty_twiml = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        twiml_headers = [("content-type", "application/xml; charset=utf-8")]

        envelope = _decode_envelope(payload) or {}
        body_bytes = _envelope_body(envelope)
        headers = _envelope_headers(envelope)
        signature = headers.get("x-twilio-signature", "")

        if len(body_bytes) > MAX_SMS_BODY_BYTES:
            runtime.emit_event("twilio.body_too_large", {"bytes": len(body_bytes)})
            return _http_resp(200, twiml_headers, empty_twiml)

        params = _parse_form(body_bytes)
        message_sid = _first(params, "MessageSid")
        to_number = _norm_number(_first(params, "To"))
        from_number = _norm_number(_first(params, "From"))
        text = _truncate_text(_first(params, "Body").strip())

        # (a) Require Twilio's per-message id (used only for logging now — the
        # AUTHORITATIVE anti-replay guard is the post-MAC digest set in
        # tenant_verified; a pre-MAC MessageSid ring is attacker-evictable).
        if not message_sid:
            runtime.emit_event("twilio.prefilter_drop", {"reason": "no_message_sid"})
            return _http_resp(200, twiml_headers, empty_twiml)

        # (b) Tenant lookup by the destination number — unknown/inactive drops.
        tenant = self._get_tenant(to_number)
        if not tenant or tenant.get("status") != "active":
            runtime.emit_event("twilio.prefilter_drop", {
                "reason": "unknown_number", "to": to_number,
            })
            return _http_resp(200, twiml_headers, empty_twiml)

        # (c) Require a real correspondent + message.
        if not from_number or not text:
            return _http_resp(200, twiml_headers, empty_twiml)

        # (d) Per-tenant pre-MAC load cap (gas-drain bound). This is a LOAD cap,
        # NOT the continuation-capacity guard — it counts relayed-but-unfinished
        # attempts (which may be forged and never MAC-pass). The PVM
        # 100-continuation limit is enforced post-MAC in run_agent, so a flood of
        # forged webhooks across tenants can't consume continuation budget or
        # starve legitimate traffic here.
        if self._inflight(to_number) >= MAX_TENANT_INFLIGHT:
            runtime.emit_event("twilio.prefilter_drop", {"reason": "tenant_inflight_cap", "to": to_number})
            return _http_resp(200, twiml_headers, empty_twiml)

        # (d2) Global active-text BYTE budget. The count caps above bound how many
        # requests are live; this bounds their aggregate text so a burst of
        # large-body messages can't exhaust the per-actor storage quota (a write
        # failing mid-flow would strand a workflow). Released on every finish.
        text_bytes = _text_bytes(text)
        if self._active_bytes() + text_bytes > ACTIVE_TEXT_BUDGET_BYTES:
            runtime.emit_event("twilio.prefilter_drop", {"reason": "active_bytes_budget", "to": to_number})
            return _http_resp(200, twiml_headers, empty_twiml)

        # (e) Per-conversation single-flight ((To,From) is the SMS "thread").
        conversation_key = _conversation_key(to_number, from_number)
        pending_id = self._pending_get(conversation_key)
        if pending_id is not None:
            pending = self._request_by_id(pending_id)
            created_height = int((pending or {}).get("created_height", 0) or 0)
            current_height = int(runtime.get_block_height())
            stale = (
                pending is None
                or created_height <= 0  # backward-compatible recovery
                or current_height - created_height >= REQUEST_TIMEOUT_BLOCKS
            )
            if not stale:
                runtime.emit_event("twilio.prefilter_drop", {"reason": "conversation_pending"})
                return _http_resp(200, twiml_headers, empty_twiml)
            if pending is None:
                self._pending_del(conversation_key, pending_id)
            else:
                self._finish_request(pending_id, conversation_key, to_number, "timed_out")
            runtime.emit_event("twilio.stale_request_reclaimed", {
                "request_id": pending_id,
                "twilio_number": to_number,
            })

        # Build the EXACT preimage Twilio signed + the request digest.
        webhook_url = _storage_str(self.storage, KEY_WEBHOOK_URL, DEFAULT_WEBHOOK_URL)
        preimage = _twilio_preimage(webhook_url, params)
        digest = _request_digest(preimage, signature)

        # (f) Cheap best-effort replay drop against the DURABLE post-MAC digest
        # shard: a webhook already MAC-processed is dropped here without a relay.
        # This is only an optimization — the authoritative check is in
        # tenant_verified — but it means a genuine Twilio retry costs nothing.
        if self._is_processed(digest):
            runtime.emit_event("twilio.prefilter_drop", {"reason": "already_processed"})
            return _http_resp(200, twiml_headers, empty_twiml)

        request_id = int(self.storage.get(KEY_LAST_REQUEST_ID, 0)) + 1
        self._put_request(request_id, {
            "request_id": request_id,
            "twilio_number": to_number,
            "conversation_key": conversation_key,
            "from_number": from_number,
            "message_sid": message_sid,
            "text": text,
            "digest": digest,
            "status": "relayed",
            "created_height": int(runtime.get_block_height()),
        })
        self._pending_set(conversation_key, request_id)
        self._ta_add(to_number, request_id)
        self.storage[KEY_LAST_REQUEST_ID] = request_id
        self._inflight_add(to_number, +1)
        self._active_bytes_add(text_bytes)

        # AUTHENTICATED relay: a synchronous call() so the proxy can assert
        # get_sender() == this twilio actor (an async send could not be
        # authenticated). The proxy schedules its verify continuation as its
        # own deferred work and returns immediately.
        relay = {
            "request_id": request_id,
            "twilio_number": to_number,
            "preimage": preimage,
            "sig": signature,
            "digest": digest,
        }
        try:
            sdk_call(_addr_bytes(tenant["proxy"]), "relay_event", relay)
            runtime.emit_event("twilio.event_relayed", {
                "request_id": request_id,
                "twilio_number": to_number,
                "hire_id": tenant.get("hire_id"),
            })
        except Exception as e:
            # The proxy call failed (down / rejected) — roll back so the
            # conversation isn't stuck pending and in-flight isn't leaked.
            self._finish_request(request_id, conversation_key, to_number, "relay_failed")
            runtime.emit_event("twilio.relay_failed", {"error": str(e)[:200]})

        return _http_resp(200, twiml_headers, empty_twiml)

    # ── Authenticated proxy callbacks (synchronous call() targets) ───────────

    def _tenant_and_auth(self, twilio_number: str):
        """Resolve the tenant and require the caller to be its registered
        proxy. Returns the tenant dict on success, else None. This is the
        authentication that the async-mailbox design could not do."""
        tenant = self._get_tenant(twilio_number)
        if not tenant:
            return None
        if _norm_addr(_sender_hex()) != _norm_addr(tenant.get("proxy", "")):
            runtime.emit_event("twilio.callback_unauthorized", {
                "twilio_number": twilio_number,
                "sender": _sender_hex(),
            })
            return None
        return tenant

    def _finish_request(self, request_id, conversation_key, twilio_number, status):
        # IDEMPOTENT: all accounting is tied to the per-item record still existing.
        # inflight/active-bytes/reservation were incremented exactly once, when the
        # record was created; a second (stale/late) finish for an already-removed
        # request must be a no-op or it would double-release a slot and wrongly
        # free capacity belonging to another live request of the same tenant.
        # There is no completed-history KV blob (it would blow the value cap under
        # load) — the terminal state is emitted as an event, the audit trail.
        entry = self._request_by_id(request_id)
        if not entry:
            return
        self._pending_del(conversation_key, request_id)
        self._del_request(request_id)
        # Release the continuation reservation iff this request had reserved one
        # (tenant_verified reserves at `verified`; run_agent moves it to
        # `running`). Pre-reservation states (relayed) never reserved.
        if entry.get("status") in ("verified", "running"):
            self._running_add(-1)
        self._active_bytes_add(-_text_bytes(entry.get("text", "")))
        self._ta_remove(twilio_number, request_id)
        self._inflight_add(twilio_number, -1)
        runtime.emit_event("twilio.request_finished", {
            "request_id": request_id, "twilio_number": twilio_number, "status": status,
        })

    @public(decode="raw")
    @deferred
    def tenant_verified(self, payload):
        """Proxy → twilio, AUTHENTICATED. The proxy calls this only after its
        runner `verify=` MAC passed. We assert the caller is the tenant's
        registered proxy, that request N is still `relayed`, and that the
        proxy verified the SAME content we relayed (digest match), then flip
        the request single-use and launch the hire-bound agent.
        """
        msg = _decode_args(payload)
        twilio_number = _norm_number(msg.get("twilio_number", ""))
        tenant = self._tenant_and_auth(twilio_number)
        if not tenant:
            return codec.encode({"r": "err: unauthorized"})
        request_id = int(msg.get("request_id", 0) or 0)
        request = self._request_by_id(request_id)
        if not request or request.get("status") != "relayed":
            runtime.emit_event("twilio.stale_verification", {"request_id": request_id})
            return codec.encode({"r": "ok: stale"})
        # Bind the request to the caller's tenant: request N must belong to the
        # same number this (authenticated) proxy serves — a proxy cannot verify
        # another tenant's request even if it learned the id.
        if _norm_number(request.get("twilio_number", "")) != twilio_number:
            runtime.emit_event("twilio.cross_tenant_verification", {"request_id": request_id})
            return codec.encode({"r": "err: tenant mismatch"})
        if str(msg.get("digest", "")) != str(request.get("digest", "")):
            runtime.emit_event("twilio.digest_mismatch", {"request_id": request_id})
            return codec.encode({"r": "err: digest mismatch"})
        # AUTHORITATIVE anti-replay: this is the post-MAC, authenticated point.
        # If this exact webhook (digest) was already processed, a replay reached
        # here with a fresh request_id — refuse it. The processed shard holds only
        # MAC-verified digests, so an attacker cannot flood/evict it.
        digest = str(request.get("digest", ""))
        conversation_key = request.get("conversation_key", "")
        if self._is_processed(digest):
            self._finish_request(request_id, conversation_key, twilio_number, "replay")
            runtime.emit_event("twilio.replay_rejected", {"request_id": request_id})
            return codec.encode({"r": "err: replay"})
        # Continuation-capacity RESERVATION. tenant_answer opens a continuation on
        # this shared actor and the PVM caps an actor at 100 active continuations,
        # so we must not admit more than MAX_GLOBAL_RUNNING (< 100). Reserve HERE,
        # synchronously, BEFORE returning ok: because actor handlers run serially,
        # each tenant_verified sees the prior reservation — no race between the
        # ok:verified return and the (async) run_agent increment. Over-cap returns
        # a NON-accepted verdict the proxy consumes (releasing its verified marker
        # via the same path as the other non-ok verdicts), and we do NOT mark the
        # digest processed, so a genuine Twilio retry can still succeed once
        # capacity frees. Released on every terminal path (finish / purge).
        if self._running() >= MAX_GLOBAL_RUNNING:
            self._finish_request(request_id, conversation_key, twilio_number, "over_capacity")
            runtime.emit_event("twilio.over_capacity", {"request_id": request_id,
                                                        "running": self._running()})
            return codec.encode({"r": "err: over capacity"})
        self._mark_processed(digest)
        declaration = self.storage.get(KEY_DECLARATION, {})
        if not declaration.get("declaration_id"):
            self._finish_request(request_id, conversation_key, twilio_number, "no_declaration")
            runtime.emit_event("twilio.no_declaration", {"request_id": request_id})
            return codec.encode({"r": "ok: no declaration"})
        # Reserve the continuation slot and flip out of `relayed` (single-use, so a
        # replayed tenant_verified is rejected by the status check above).
        self._running_add(+1)
        self._update_request(request_id, status="verified")
        # Launch the agent from THIS twilio actor's OWN top-level frame — the
        # runner job's submitter is the enclosing frame's actor (it governs the
        # `hire=` authority), and this handler runs inside the proxy's call() tx
        # (submitter would be the proxy). We re-enter twilio via a funded
        # self-timer rather than a self-send: both give from==twilio, but a
        # self-send re-enters as a 162-access deferred child that cannot fund the
        # nested runner.agent submit_job (D36 / E1005), whereas a timer fired on
        # self at TIMER_ACCESS_LIMIT reserves one full deferred child for it. It
        # fires next block into `handle_timer` (the default timer handler), which
        # routes by `kind`; run_agent gates on status==verified. Scheduling is
        # cheap here (the 322-access budget is charged at fire time, not now).
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            codec.encode({"kind": "run_agent", "request_id": request_id}),
            access_limit=TIMER_ACCESS_LIMIT,
        )
        return codec.encode({"r": "ok: verified"})

    @public(decode="raw")
    @deferred
    def handle_timer(self, payload):
        """Timer-fire router (the node's DEFAULT_TIMER_HANDLER, "handle_timer").
        `tenant_verified` schedules a funded self-timer carrying
        `{kind: run_agent}`; on fire this routes by `kind` to the gated launcher.
        The only sensitive action (the hire-bound agent) is gated on request
        state that only the authenticated `tenant_verified` sets, so a stray fire
        (or a plain external send that lands here) can trigger nothing."""
        msg = _decode_args(payload)
        if msg.get("kind") == "run_agent":
            return self.run_agent(msg)
        runtime.emit_event("twilio.unknown_message", {"kind": str(msg.get("kind"))[:40]})
        return codec.encode({"r": "ignored"})

    @public(decode="raw")
    @deferred
    def run_agent(self, payload):
        """Timer-triggered (via handle_timer): launch the hire-bound agent in
        THIS twilio actor's own funded timer frame (correct submitter for hire
        validation). Gated on request status `verified`, which only the
        authenticated tenant_verified sets."""
        msg = _decode_args(payload)
        request_id = int(msg.get("request_id", 0) or 0)
        request = self._request_by_id(request_id)
        if not request or request.get("status") != "verified":
            return codec.encode({"r": "not verified"})
        # Continuation capacity was already RESERVED synchronously in
        # tenant_verified (which serialized against every other verify), so we
        # neither re-check the cap nor increment here — just transition the
        # already-reserved request verified→running (both states hold the
        # reservation; _finish_request releases it). Single-use: flipping out of
        # `verified` makes a repeated run_agent a no-op.
        self._update_request(request_id, status="running")
        self.tenant_answer({
            "request_id": request_id,
            "twilio_number": _norm_number(request.get("twilio_number", "")),
        })
        return codec.encode({"r": "running"})

    @public(decode="raw")
    @deferred
    def tenant_rejected(self, payload):
        """Proxy → twilio, AUTHENTICATED. MAC failed at the runner; clear the
        conversation's single-flight slot and release the in-flight count."""
        msg = _decode_args(payload)
        twilio_number = _norm_number(msg.get("twilio_number", ""))
        tenant = self._tenant_and_auth(twilio_number)
        if not tenant:
            return codec.encode({"r": "err: unauthorized"})
        request_id = int(msg.get("request_id", 0) or 0)
        request = self._request_by_id(request_id)
        # Bind request↔tenant (mirror tenant_verified): a proxy authenticated for
        # its own number cannot reject/cancel another tenant's in-flight request.
        if (
            request
            and request.get("status") == "relayed"
            and _norm_number(request.get("twilio_number", "")) == twilio_number
        ):
            self._finish_request(request_id, request.get("conversation_key", ""),
                                 twilio_number, "rejected")
        runtime.emit_event("twilio.request_rejected", {
            "request_id": request_id, "twilio_number": twilio_number,
        })
        return codec.encode({"r": "ok: rejected recorded"})

    # ── The hire-bound agent job ─────────────────────────────────────────────

    @runner.continuation
    async def tenant_answer(self, payload):

        job = _decode_args(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(job.get("request_id", 0) or 0)
        ctx.twilio_number = _norm_number(job.get("twilio_number", ""))

        tenant = self._get_tenant(ctx.twilio_number) or {}
        request = self._request_by_id(ctx.request_id)
        if not tenant or not request:
            return b"ok: unknown request"

        ctx.conversation_key = request.get("conversation_key", "")
        ctx.from_number = request.get("from_number", "")
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
        ctx.artifact_id = int(tenant.get("artifact_id", declaration.get("artifact_id", 0)))

        # The hire-bound agent job. `hire=` makes the chain validate the hire is
        # Active + pinned at dispatch and the runner re-authorize each lease
        # epoch. session_id = the (To,From) conversation key so the runner keeps
        # this correspondent's prior turns (two-way workflow memory).
        agent_kwargs = {
            "session_id": ctx.conversation_key,
            "hire": {
                "hire_id": ctx.hire_id,
                "declaration_id": ctx.declaration_id,
                "version": ctx.version,
                "artifact_id": ctx.artifact_id,
            },
        }
        if self.storage.get(KEY_MOUNT_VOLUMES, True):
            agent_kwargs["system_prompt_path"] = (
                f"{ctx.artifact_volume}/twilio/system_prompt.txt"
            )
            agent_kwargs["session_dir"] = f"{ctx.state_volume}/.sessions"
            agent_kwargs["volumes"] = [
                [ctx.artifact_volume, "ro", ctx.owner_hex],
                [ctx.state_volume, "rw", ctx.owner_hex],
            ]
        else:
            agent_kwargs["system_prompt_inline"] = (
                "You are a concise SMS assistant. Keep replies under 320 "
                "characters and answer plainly."
            )
        agent_result = await runner.agent(
            model=ctx.model,
            query=ctx.text,
            **agent_kwargs,
        )

        # runner.agent returns a structured {"answer": "<text>", ...} result.
        # `await runner.agent(...)` yields a RunnerResult wrapper, not a dict —
        # unwrap to its decoded payload (.data) BEFORE the dict check. Use
        # getattr(..., "data", ...) rather than .raw so a value that is already a
        # bare dict (not a wrapper) passes through instead of AttributeError-ing.
        agent_data = getattr(agent_result, "data", agent_result)
        if not isinstance(agent_data, dict):
            # Terminal failure, never a raise: _finish_request is the ONLY
            # release for this conversation's single-flight slot, its inflight
            # accounting and its continuation-capacity reservation — a raise
            # holds all three until the stale-request sweeper reclaims them.
            self._finish_request(ctx.request_id, ctx.conversation_key,
                                 ctx.twilio_number, "agent_error")
            runtime.emit_event("twilio.agent_error", {
                "request_id": ctx.request_id,
                "twilio_number": ctx.twilio_number,
                "error": "runner.agent did not return a JSON object",
            })
            return codec.encode({
                "request_id": ctx.request_id,
                "status": "agent_error",
            })
        ctx.answer = str(agent_data.get("answer") or "")
        if not ctx.answer:
            ctx.answer = "(no response — request_id=" + str(ctx.request_id) + ")"

        # AUTHENTICATED send: synchronous call() so the proxy asserts the caller
        # is this twilio actor before releasing the outbound API key. To = the
        # correspondent, From = the tenant's own Twilio number.
        reply = {
            "request_id": ctx.request_id,
            "to_number": ctx.from_number,
            "from_number": ctx.twilio_number,
            "text": ctx.answer,
        }
        try:
            sdk_call(bytes.fromhex(ctx.proxy), "post_answer", reply)
            status = "answered"
        except Exception as e:
            status = "post_failed"
            runtime.emit_event("twilio.post_failed", {
                "request_id": ctx.request_id, "error": str(e)[:200],
            })

        self._finish_request(ctx.request_id, ctx.conversation_key, ctx.twilio_number, status)
        runtime.emit_event("twilio.answered", {
            "request_id": ctx.request_id,
            "twilio_number": ctx.twilio_number,
            "hire_id": ctx.hire_id,
            "answer_len": len(ctx.answer),
            "status": status,
        })
        return codec.encode({"request_id": ctx.request_id, "status": status})

    @pure
    def on_runner_result(self, payload):
        try:
            msg = codec.decode(payload) if isinstance(payload, bytes) else payload
        except Exception:
            msg = {}
        runner.handle_runner_result(self, msg)
        return b"ok: runner result handled"

    # ── Read-only ops surface (owner-only: request ids must not be public) ───

    @callable_by(OWNER, decode="cbor")
    def get_tenants(self, payload):
        redacted = {}
        for number in self._tenant_numbers():
            t = self._get_tenant(number) or {}
            redacted[number] = {
                "hire_id": t.get("hire_id"),
                "proxy": "0x" + str(t.get("proxy", "")),
                "status": t.get("status"),
            }
        return redacted

    @callable_by(OWNER, decode="cbor")
    def get_requests(self, payload):
        # No history KV blob (dropped for the 64 KiB value cap — the audit trail
        # is the emitted twilio.request_finished events). Enumerate live requests
        # via the per-tenant active-id indexes (no key scan available).
        active = []
        for number in self._tenant_numbers():
            for request_id in self._ta_ids(number):
                entry = self._request_by_id(request_id)
                if entry:
                    active.append(entry)
        return {"active": active}

    @callable_by(OWNER, decode="cbor")
    def get_pending(self, payload):
        pending = {}
        for number in self._tenant_numbers():
            for request_id in self._ta_ids(number):
                entry = self._request_by_id(request_id)
                if entry:
                    pending[entry.get("conversation_key", "")] = request_id
        return pending


def _get_actor():
    return TwilioActor()


def init(payload):
    return _get_actor().init(payload)


def set_declaration(payload):
    return _get_actor().set_declaration(payload)


def set_webhook_url(payload):
    return _get_actor().set_webhook_url(payload)


def set_routes(payload):
    return _get_actor().set_routes(payload)


def admin_disconnect(payload):
    return _get_actor().admin_disconnect(payload)


def connect_number(payload):
    return _get_actor().connect_number(payload)


def disconnect_number(payload):
    return _get_actor().disconnect_number(payload)


def health(payload):
    return _get_actor().health(payload)


def twilio_http(payload):
    return _get_actor().twilio_http(payload)


def tenant_verified(payload):
    return _get_actor().tenant_verified(payload)


def handle_timer(payload):
    return _get_actor().handle_timer(payload)


def run_agent(payload):
    return _get_actor().run_agent(payload)


def tenant_rejected(payload):
    return _get_actor().tenant_rejected(payload)


# NOTE: the initial continuation handler `tenant_answer` is deliberately NOT
# exported at module level — `@actor` skips permission wrapping for
# continuations, so exporting it would let an attacker dispatch the hire-bound
# agent directly, bypassing tenant_verified's auth + the run_agent gate. It is
# only started via `self.tenant_answer()` from run_agent; resume routes through
# on_runner_result → handle_runner_result → the instance `__resume`.


def _resume_is_self() -> bool:
    """A genuine continuation resume is a runner-result callback tx synthesized
    with from == this actor (transaction.rs). `@actor` leaves resume methods
    permission-unwrapped, so this module-level export force-gates on sender ==
    self — an EOA or other actor forging a `__resume` (e.g. injecting a fake
    agent answer) is denied."""
    return _norm_addr(_sender_hex()) == _self_bytes().hex()


def tenant_answer__resume(payload):
    if not _resume_is_self():
        runtime.emit_event("twilio.resume_denied", {"handler": "tenant_answer__resume"})
        return codec.encode({"r": "denied"})
    return _get_actor().tenant_answer__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_tenants(payload):
    return _get_actor().get_tenants(payload)


def get_requests(payload):
    return _get_actor().get_requests(payload)


def get_pending(payload):
    return _get_actor().get_pending(payload)
