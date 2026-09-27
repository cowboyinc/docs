"""
Cowbot — Slack bot that answers questions about cowboy by routing
through a runner that mounts the cowboyinc CBFS volume and runs an
open-source LLM with filesystem tool calling.

Zero plaintext secrets in the actor (CIP-24 §5.5). A validator replays
actor execution, so anything readable in the PVM is readable on chain —
the actor therefore holds NO Slack secrets. Both the inbound-webhook MAC
check and the outbound bot-token are deferred to the runner, the only
tier that can threshold-decrypt a CBSS secret:

  * SLACK_SIGNING_SECRET — owned by the deployer EOA, granted to this
    actor under the `secrets.verify` manifest entitlement. The runner
    releases it into a verify-only buffer, recomputes the Slack v0 HMAC,
    and aborts the whole job fail-closed (JobPreconditionFailed) on a
    mismatch — before any executor work, post, or read-set release.
  * SLACK_BOT_TOKEN — owned by the deployer EOA, granted under
    `secrets.read`. The runner substitutes `${SLACK_BOT_TOKEN}` into the
    Authorization header at request-construction time; the plaintext
    never touches actor state or chain.

Flow:
  Slack → gateway → actor.slack_http(envelope)   [slack_http IS a @runner.continuation]
    ├─ url_verification challenge answered inline (no secret)
    ├─ cheap SECRET-FREE prefilters (body-size cap, ts freshness,
    │  exact-replay dedupe, global count cap) — these only BOUND
    │  forged-dispatch cost; they do NOT authenticate (the attacker
    │  controls the body until the MAC passes on the runner)
    └─ then, in the SAME continuation (no self-send):
       1. runner.http chat.postMessage "Thinking…"  — carries BOTH
          secrets=[SLACK_BOT_TOKEN] AND verify={SLACK_SIGNING_SECRET, …}.
          This first op is the gate: a forged webhook fails the MAC, the
          post never happens, and the agent + CBFS work never run.
       2. runner.agent tool-use loop over the mounted CBFS volumes.
       3. runner.http chat.update (or postMessage) with the real answer —
          secrets=[SLACK_BOT_TOKEN] only (already gated by step 1).

  There is NO self-send hop. Answering in slack_http's own continuation makes
  the first runner.http a FIRST-LEVEL child of the gateway-signed tx, which
  funds it. A self-sent message would run as a deferred child whose derived
  access budget (162 units) cannot fund the nested submit_job child the first
  runner.http mints — the node refuses it (E1005) and the job never reaches the
  runner (decision D36, COW-3878). The prefilters and url_verification run in
  the pre-await segment and return their own explicit envelopes inline; once the
  handler reaches the await, the FSM initial segment returns no envelope. TODAY
  the gateway answers the caller 502 for this path (the transaction succeeded and
  the runner job runs; only the acknowledgement is wrong — COW-3895); once
  gateway PR #93 lands it answers with the route's fallback response or 2xx.

  actor.slack_http__resume (FSM-generated) updates session state in the
  cowbot-state CBFS volume under .sessions/<thread_key>.json.
"""

import hashlib
import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, public, callable_by, OWNER, Address
from cowboy_sdk.routes import get, post


KEY_OWNER = "owner"
KEY_PENDING = "pending"           # dict: thread_key -> request_id (single-flight per thread)
KEY_LAST_REQUEST_ID = "last_request_id"
KEY_REQUESTS = "requests"          # bounded request history for ops/debug
KEY_SEEN = "seen"                  # bounded list of (ts, signature, body_sha256) for exact-replay dedupe
KEY_DISPATCH_COUNT = "dispatch_count"  # monotonic count of dispatched events (coarse forged-dispatch ceiling)
KEY_KNOWLEDGE_VOLUME = "knowledge_volume"
KEY_STATE_VOLUME = "state_volume"
MAX_REQUEST_HISTORY = 100

# ── Secret-free inbound prefilters (CIP-24 §5.5 "Threat model / DoS bound") ───
# These run on-chain, hold NO secret, and do NOT authenticate the request —
# the attacker controls the body until the MAC is checked on the runner. They
# only bound the cost of a forged-dispatch flood; the real ceiling is the
# actor's per-interval spend cap. Slack's own `event_id` dedupe is meaningful
# only AFTER a valid MAC, so we don't use it here.
SLACK_TS_SKEW_SECONDS = 300       # reject |now - ts| beyond this (Slack's own replay window)
MAX_SEEN_ENTRIES = 256            # bounded exact-replay set; evict oldest
MAX_DISPATCH_COUNT = 100_000      # coarse safety stop; spend cap is the true ceiling
# Secret-free body cap applied BEFORE decode/dispatch: an oversized body can't
# pass the runner's `verify=` MAC anyway (the runner rejects a verify message
# over the 64 KiB admission cap with JobPreconditionMessageTooLarge), so
# bounding it here keeps a forged-dispatch flood from ever reaching that
# release attempt. 60 KiB leaves ample headroom under that admission cap for the
# `b"v0:" + ts + b":"` framing this actor wraps around the body to build the MAC
# message (see slack_http's ctx.verify_message).
MAX_SLACK_BODY_BYTES = 60 * 1024

# Reserved KV slot consumed by the gateway's L1b routes cache (CIP-15 §6.5).
# Written verbatim (no SDK CBOR wrapping) via storage.set_raw so the gateway
# sees a plain CBOR Routes blob.
ROUTES_KEY = "__cowboy/routes"

SLACK_API_BASE = "https://slack.com/api"

DEFAULT_KNOWLEDGE_VOLUME = "cowboyinc"
DEFAULT_STATE_VOLUME = "cowbot-state"
# CBFS volumes are owned by the user that ran `cbfs volume create`, NOT by
# this actor (actors don't have wallets). We pass the deploying owner hex as
# the 3rd element of each `volumes=` tuple so the dispatcher resolves the
# volume under that account instead of the actor's address.
#
# CIP-9 §4.3 cross-owner authorization: the dispatcher only honors the
# mount when this actor's address is on the volume's
# `mount_allowed_actors` allowlist — owner-curated state inside the
# `StorageCommitment` itself, mutated via the owner-authenticated
# `cowboy volume allow-actor` CLI (Almanax #865397ad, #050899f3 — earlier
# designs that trusted an owner_hint or a self-attested manifest grant
# were rejected). `deploy.sh` calls `volume allow-actor` for both
# volumes before submitting the DeployActor tx.
DEFAULT_MODEL = "local"


def _storage_str(storage, key: str, default: str) -> str:
    value = storage.get(key, default)
    if not value:
        return default
    return str(value)


def _thread_key(channel_id: str, thread_ts: str) -> str:
    h = hashlib.sha256(f"{channel_id}:{thread_ts}".encode("utf-8")).hexdigest()
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


# ── CIP-14 HTTP envelope helpers ──────────────────────────────────────────────
# The gateway dispatches POST /<actor>/<path> by submitting a CBOR-encoded
# HttpRequestEnvelope to the actor handler that owns the route. The handler
# must return CBOR-encoded bytes that match HttpResponseEnvelope:
#   { status: u16, headers: [[k,v], …], body: bytes }

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
    """Best-effort decode of a CIP-14 HttpRequestEnvelope.

    The gateway submits CBOR-encoded envelope bytes; the PVM passes those
    bytes through unchanged to the handler. The SDK doesn't auto-decode
    them, so we do it here. Returns a dict (envelope-shaped) on success,
    or None if the payload isn't an envelope.
    """
    obj = payload
    # If we got raw bytes, try CBOR-decoding into a dict.
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
    """Return the body bytes from a decoded envelope dict."""
    body = envelope.get("body") if isinstance(envelope, dict) else None
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b""


def _envelope_headers(envelope):
    """Return a {lowercase_header_name: value} map from a decoded envelope."""
    headers = {}
    if isinstance(envelope, dict):
        for pair in envelope.get("headers") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                k, v = pair
                headers[str(k).lower()] = str(v)
    return headers


# ── Secret-free inbound prefilters (CIP-24 §5.5) ──────────────────────────────
# NOTE: the Slack HMAC is NO LONGER verified in-actor — a validator can't hold
# the signing secret, so the MAC now runs on the runner via the first
# runner.http `verify=` (see slack_http). The helpers below are SECRET-FREE
# and only bound forged-dispatch cost; none of them authenticate the request.

def _ts_is_fresh(ts: str, now: int) -> bool:
    """True if the Slack request timestamp is within ±SLACK_TS_SKEW_SECONDS.

    Mirrors Slack's own replay window. SECRET-FREE: an attacker can forge any
    `ts`, but a stale/future one is cheap to reject before dispatch. Returns
    True (skip the check) when no wall clock is available in-actor, since a
    missing clock must not silently drop legitimate traffic — the MAC on the
    runner is the real gate either way.
    """
    if now <= 0:
        # No block timestamp available (runtime.get_timestamp_ms() returned 0);
        # skip freshness rather than reject everything. The runner MAC still gates.
        return True
    try:
        event_ts = int(float(ts))
    except (TypeError, ValueError):
        return False
    return abs(now - event_ts) <= SLACK_TS_SKEW_SECONDS


def _body_fingerprint(ts: str, signature: str, body_bytes: bytes) -> str:
    """Stable fingerprint over (ts, signature, sha256(body)).

    These are the ONLY fields fixed before the MAC is checked (§5.5), so this
    drops a byte-identical resend but is NOT forgery prevention: a modified body
    or signature is a fresh fingerprint and a fresh attempt.
    """
    body_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    return f"{ts}|{signature}|{body_hash}"


@actor
class CowbotActor:
    def init(self, payload):
        # SDK-owner sentinel — gates @callable_by(OWNER) handlers like
        # set_routes / set_volumes. Distinct from the KEY_OWNER storage entry
        # below, which records the deployer EOA: that address is the CBSS
        # secret owner the runner releases SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET
        # from (the cross-account refs in slack_http), and the volume owner.
        runtime.assume_ownership_if_unowned()
        cfg = json.loads(payload) if payload else {}
        owner = self.sender.to_hex(checksum=False)
        self.storage[KEY_OWNER] = owner
        self.storage[KEY_KNOWLEDGE_VOLUME] = cfg.get("knowledge_volume", DEFAULT_KNOWLEDGE_VOLUME)
        self.storage[KEY_STATE_VOLUME] = cfg.get("state_volume", DEFAULT_STATE_VOLUME)
        self.storage[KEY_PENDING] = {}
        self.storage[KEY_LAST_REQUEST_ID] = 0
        self.storage[KEY_REQUESTS] = []
        self.storage[KEY_SEEN] = []            # exact-replay dedupe set (slack_http prefilter)
        self.storage[KEY_DISPATCH_COUNT] = 0   # coarse forged-dispatch counter
        runtime.emit_event("cowbot.init", {
            "owner": owner,
            "knowledge_volume": self.storage[KEY_KNOWLEDGE_VOLUME],
            "state_volume": self.storage[KEY_STATE_VOLUME],
        })
        return b"ok: initialized"

    def _update_request(self, request_id, **fields):
        """KEY_PENDING is single-flight PER THREAD, so two threads can be mid-
        continuation at once and the request completing now is not necessarily
        the one appended last — hence the id lookup rather than the tail. Loops
        are illegal inside a continuation body (the FSM compiler cannot unparse
        one), hence the helper.
        """
        requests = list(self.storage.get(KEY_REQUESTS, []))
        for entry in requests:
            if entry.get("request_id") == request_id:
                entry.update(fields)
        self.storage[KEY_REQUESTS] = _bounded(requests)

    def _fail_request(self, request_id, thread_key, status, event, error=""):
        """Terminate a request that failed before the answer was posted.

        EVERY pre-answer failure path must go through here rather than raise.
        `slack_http` commits KEY_PENDING[thread_key] before its first await and
        only the success path and this helper ever clear it — there is no
        timeout sweeper — so a raise between those points wedges the thread
        permanently: every later message for it hits the "already pending for
        thread" drop and is silently discarded.
        """
        pending = dict(self.storage.get(KEY_PENDING, {}))
        if pending.get(thread_key) == request_id:
            pending.pop(thread_key, None)
        self.storage[KEY_PENDING] = pending

        self._update_request(request_id, status=status)

        detail = {"request_id": request_id, "thread_key": thread_key}
        if error:
            detail["error"] = error
        runtime.emit_event(event, detail)
        return codec.encode({
            "request_id": request_id,
            "thread_key": thread_key,
            "status": status,
        })

    # ── Gateway-facing Slack webhook (CIP-14 / CIP-15) ────────────────────────
    # slack_http IS the @runner.continuation. The route decorators (@public,
    # @post) may wrap it, but @runner.continuation must be the innermost
    # decorator (directly above `async def`), and it must NOT carry
    # @callable_by (that shadows the FSM-generated resume dispatch). The
    # secret-free prefilters and url_verification run in the pre-await segment
    # and return explicit envelopes inline; on the normal path the FSM initial
    # segment returns no envelope. TODAY the gateway answers the caller 502 for
    # that path (the tx succeeded and the runner job runs; COW-3895); after
    # gateway PR #93 it answers with the route's fallback response or 2xx. The
    # MAC gate and answer run through the runner after the first await.
    @public(decode="raw")
    @post("/slack/events")
    @runner.continuation
    async def slack_http(self, payload):
        """POST /slack/events — gateway-facing Slack webhook (runner continuation).

        Receives a CIP-14 HttpRequestEnvelope, runs cheap SECRET-FREE
        prefilters, and answers url_verification inline — all in the pre-await
        segment, each branch returning its own explicit envelope. On the normal
        path the FSM initial segment returns no envelope: TODAY the gateway
        answers the caller 502 for that path (the tx succeeded and the runner job
        runs; COW-3895), and after gateway PR #93 it answers with the route's
        fallback response or 2xx. The handler then MAC-gates and answers through
        the runner in this SAME continuation.

        There is NO self-send hop. Answering here makes the first runner.http a
        FIRST-LEVEL child of the gateway-signed tx, which funds it. A self-sent
        message would run as a deferred child whose derived access budget (162
        units) cannot fund the nested submit_job child the first runner.http
        mints — the node refuses it (E1005) and the job never reaches the runner
        (decision D36, COW-3878).

        The inbound HMAC is NOT verified on-chain — a validator can't hold the
        signing secret. Verification happens on the runner via the first
        runner.http `verify=` (CIP-24 §5.5). The prefilters below only BOUND
        forged-dispatch cost; they do NOT authenticate the request (the attacker
        controls the body until the MAC is checked on the runner). The gateway
        is untrusted: it forwards bytes and headers verbatim.
        """
        envelope = _decode_envelope(payload) or {}
        # body_bytes are the VERBATIM received request body — carried untouched
        # all the way to the runner's MAC. Never JSON-reparse/re-serialize them.
        body_bytes = _envelope_body(envelope)
        headers = _envelope_headers(envelope)
        # The header timestamp Slack signed (X-Slack-Request-Timestamp): this is
        # the value the runner's `verify=` MAC is computed over. It is NOT the
        # event `ts` (a message id used only for thread_ts) — keep them in
        # separate names so the MAC is never built over the wrong timestamp.
        sig_ts = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")

        # Secret-free body-size prefilter bounding the runner's verify-release
        # cost. Applied BEFORE decoding or answering: a body over
        # MAX_SLACK_BODY_BYTES could never pass the runner's `verify=` MAC (it
        # exceeds the 64 KiB verify-message admission cap once the `v0:<ts>:`
        # prefix is added), so rejecting it here keeps a forged-dispatch flood
        # from ever reaching that release attempt. A legitimate url_verification
        # ping is tiny, so capping above it is safe. 200 so Slack doesn't retry
        # the oversized event.
        if len(body_bytes) > MAX_SLACK_BODY_BYTES:
            runtime.emit_event("cowbot.slack_body_too_large", {"bytes": len(body_bytes)})
            return _http_resp(200, [], b"")

        try:
            slack_body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            slack_body = {}
        if not isinstance(slack_body, dict):
            # Valid JSON but not an object ([1,2], "x", 5) — not a Slack event;
            # ack inline rather than AttributeError on `.get` below.
            runtime.emit_event("cowbot.slack_prefilter_drop", {"reason": "non_object_body"})
            return _http_resp(200, [], b"")

        # URL verification — Slack's one-time setup ping. Reply inline with the
        # challenge so the app config validates. (No secret required — Slack
        # tolerates unsigned url_verification, and the actor trusts no state
        # from this request: it only echoes the challenge back.)
        if slack_body.get("type") == "url_verification":
            challenge = slack_body.get("challenge", "")
            resp_body = json.dumps({"challenge": challenge}).encode("utf-8")
            return _http_resp(
                200,
                [("content-type", "application/json; charset=utf-8")],
                resp_body,
            )

        # ── Secret-free prefilters (CIP-24 §5.5 DoS bound) ───────────────────
        # (a) Timestamp freshness — reject a stale or far-future ts (Slack's own
        # replay window). Uses deterministic block time; skips if unavailable.
        now = runtime.get_timestamp_ms() // 1000
        if not _ts_is_fresh(sig_ts, now):
            runtime.emit_event("cowbot.slack_prefilter_drop", {"reason": "stale_ts"})
            return _http_resp(200, [], b"")  # 200 so Slack doesn't retry a stale event

        # (b) Exact-replay dedupe keyed on (ts, signature, sha256(body)) — the
        # only fields fixed before the MAC (§5.5). Drops a byte-identical resend;
        # NOT forgery prevention (a modified body/sig is a fresh attempt).
        fingerprint = _body_fingerprint(sig_ts, signature, body_bytes)
        seen = list(self.storage.get(KEY_SEEN, []))
        if fingerprint in seen:
            runtime.emit_event("cowbot.slack_prefilter_drop", {"reason": "exact_replay"})
            return _http_resp(200, [], b"")
        seen.append(fingerprint)
        if len(seen) > MAX_SEEN_ENTRIES:
            seen = seen[-MAX_SEEN_ENTRIES:]  # bounded set, evict oldest
        self.storage[KEY_SEEN] = seen

        # (c) Per-thread single-flight is enforced below in this same handler
        # via KEY_PENDING: a same-thread message already in flight is dropped
        # before the runner is called (the pending entry is written and read in
        # this continuation, not a separate block).
        #
        # (d) Coarse global count cap — a simple safety stop on dispatch volume.
        # The actor's per-interval SPEND CAP is the real ceiling; this just
        # prevents an unbounded forged-dispatch loop from running indefinitely.
        dispatch_count = int(self.storage.get(KEY_DISPATCH_COUNT, 0))
        if dispatch_count >= MAX_DISPATCH_COUNT:
            runtime.emit_event("cowbot.slack_prefilter_drop", {"reason": "dispatch_cap"})
            return _http_resp(200, [], b"")
        self.storage[KEY_DISPATCH_COUNT] = dispatch_count + 1

        # The Slack event, parsed from the verbatim body above. url_verification
        # was already answered inline and never reaches this point.
        body = slack_body

        event = body.get("event") or {}
        event_type = event.get("type")

        # We only act on app_mention (channel @mention) and message.im (DM).
        # Subtype-bearing messages (edits, joins, bot messages) are ignored.
        if event_type not in ("app_mention", "message"):
            return _http_resp(200, [], b"")

        if event_type == "message":
            if event.get("subtype"):
                return _http_resp(200, [], b"")
            if event.get("channel_type") not in ("im", "mpim"):
                # Only process plain channel messages if they're a thread reply
                # we already started a session for (handled by the runner side).
                if not event.get("thread_ts"):
                    return _http_resp(200, [], b"")

        # Skip our own bot's messages to avoid loops. Use the `(... or [{}])[0]`
        # form (as bot_user_id below) so an empty `authorizations: []` is an
        # empty-dict lookup, not an IndexError.
        if event.get("bot_id") or event.get("user") == (body.get("authorizations") or [{}])[0].get("user_id"):
            return _http_resp(200, [], b"")

        channel_id = event.get("channel", "")
        event_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or event_ts
        user_id = event.get("user", "")
        bot_user_id = (body.get("authorizations") or [{}])[0].get("user_id")
        text = _strip_mention(event.get("text", ""), bot_user_id)

        if not channel_id or not text:
            return _http_resp(200, [], b"")

        thread_key = _thread_key(channel_id, thread_ts)

        pending = dict(self.storage.get(KEY_PENDING, {}))
        if thread_key in pending:
            # Already a request in flight for this thread — drop. (Slack will
            # retry on 5xx, but we 200'd at the tunnel layer, so nothing to do.)
            return _http_resp(200, [], b"")

        request_id = int(self.storage.get(KEY_LAST_REQUEST_ID, 0)) + 1
        requests = list(self.storage.get(KEY_REQUESTS, []))
        requests.append({
            "request_id": request_id,
            "thread_key": thread_key,
            "channel_id": channel_id,
            "user_id": user_id,
            "thread_ts": thread_ts,
            "status": "pending",
        })

        pending[thread_key] = request_id
        self.storage[KEY_LAST_REQUEST_ID] = request_id
        self.storage[KEY_PENDING] = pending
        self.storage[KEY_REQUESTS] = _bounded(requests)

        # Resolve the secret OWNER once: CBSS secrets are owned by the deployer
        # EOA (actors have no wallet), stored at init in KEY_OWNER. The runner
        # releases SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET from THIS account.
        owner_stored = self.storage.get(KEY_OWNER, "")
        owner_hex = Address(owner_stored).to_hex(checksum=False)[2:] if owner_stored else ""
        if not owner_hex:
            raise ValueError("cowbot secret owner is not initialized")

        ctx = capture()
        ctx.request_id = request_id
        ctx.thread_key = thread_key
        ctx.channel_id = channel_id
        ctx.thread_ts = thread_ts
        ctx.user_id = user_id
        # Locals don't survive across awaits — the FSM regenerates each
        # resume function from the original AST, so anything the second
        # segment needs (text in particular) must live on ctx.
        ctx.text = text
        ctx.owner_hex = owner_hex
        # The runner's `verify=` MAC needs these EXACT bytes. raw_body is the
        # verbatim received body; sig_ts/signature come from the Slack headers
        # (the header timestamp, NOT the event ts). Stash on ctx so they survive
        # the await (the FSM resets plain locals).
        ctx.raw_body = body_bytes
        ctx.ts = sig_ts
        ctx.signature = signature

        # The EXACT MAC message Slack signed: `v0:<ts>:<raw_body>` over the
        # verbatim body bytes. NEVER re-serialize the body — a JSON round-trip
        # changes the bytes and fails the MAC (§5.5).
        ctx.verify_message = b"v0:" + ctx.ts.encode("ascii") + b":" + ctx.raw_body

        # 1. Post a fast "Thinking…" placeholder so the user sees activity
        # before the LLM finishes. We update this same message in-place once
        # the agent answer arrives via chat.update, keyed by ts from the
        # postMessage response.
        #
        # THIS FIRST OP IS THE GATE. It carries BOTH:
        #   - secrets=[{SLACK_BOT_TOKEN}]  → ${SLACK_BOT_TOKEN} substituted by
        #     the runner into the Authorization header (read set), AND
        #   - verify={SLACK_SIGNING_SECRET, …} → the runner MAC-checks the
        #     inbound webhook FIRST and aborts the whole job fail-closed
        #     (JobPreconditionFailed) on mismatch, BEFORE releasing the bot
        #     token, posting, or running the agent (CIP-24 §5.5 fail-closed
        #     ordering). A forged webhook therefore produces no post and no
        #     agent/CBFS work.
        # Cross-account refs use the {"account", "key"} map form — the shape the
        # node dispatcher resolves to a cross-account release. (Use the map form
        # rather than a bare tuple, which the dispatcher does not resolve to a
        # cross-account release.)
        thinking_body = json.dumps({
            "channel": ctx.channel_id,
            "thread_ts": ctx.thread_ts,
            "text": ":thinking_face: thinking…",
            "mrkdwn": True,
        }).encode("utf-8")
        thinking_result = await runner.http(
            url=f"{SLACK_API_BASE}/chat.postMessage",
            method="POST",
            headers={
                "Authorization": "Bearer ${SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=thinking_body,
            secrets=[{"account": ctx.owner_hex, "key": "SLACK_BOT_TOKEN"}],
            verify={
                "secret": {"account": ctx.owner_hex, "key": "SLACK_SIGNING_SECRET"},
                "message": ctx.verify_message,
                "signature": ctx.signature,
                # Slack v0 sig = "v0=" + hex(HMAC-SHA256(signing_secret, "v0:ts:body")).
                # Protocol carries NEUTRAL crypto only; "Slack" lives here in the actor.
                "algo": {"hmac": {"hash": "sha256", "encoding": "hex", "prefix": "v0="}},
            },
        )

        # `await runner.http(...)` yields a RunnerResult wrapper, not a dict —
        # unwrap to its decoded payload (.data) BEFORE any isinstance/ok check.
        thinking = getattr(thinking_result, "data", thinking_result)
        if not isinstance(thinking, dict):
            # Slack served HTML, a proxy error page, an empty body — transient,
            # and never a raise (see _fail_request).
            return self._fail_request(
                ctx.request_id, ctx.thread_key, "http_error",
                "cowbot.slack_http_error",
                "chat.postMessage did not return a JSON object",
            )

        # Precondition gate: on a forged MAC the runner aborts the assignment
        # TERMINALLY (CIP-24 §5.5) and submits a failure envelope
        # ({ok:false, cbss_precondition_failed:true}). We catch it via ok-is-False
        # AND an explicit cbss_precondition_failed check (defense-in-depth: never
        # proceed to the agent on a verify failure).
        # Do NOT proceed to the agent, and CLEAR the pending entry so a forged
        # thread doesn't wedge future legitimate messages (this also fixes the
        # pre-existing pending-stuck bug on any first-op failure).
        if (
            thinking.get("ok") is False
            or thinking.get("cbss_precondition_failed")
        ):
            return self._fail_request(
                ctx.request_id, ctx.thread_key, "precondition_failed",
                "cowbot.precondition_failed",
            )

        # Capture the placeholder ts so chat.update targets it. Slack returns
        # {ok, channel, ts, message:{...}} — the runner-http executor parses
        # JSON responses into Value when content-type is application/json.
        ctx.placeholder_ts = thinking.get("ts", "") or ""

        # 2. Run the agent against real CBFS volume attachments. The chain
        # dispatcher converts `volumes=` kwarg → JobSpec.attachments → CapTokens
        # → runner-storage materializes the volumes locally before the agent
        # executes. Names must match volumes the actor's owner has created
        # via `cbfs volume create`.
        ctx.knowledge_volume = _storage_str(self.storage, KEY_KNOWLEDGE_VOLUME, DEFAULT_KNOWLEDGE_VOLUME)
        ctx.state_volume = _storage_str(self.storage, KEY_STATE_VOLUME, DEFAULT_STATE_VOLUME)
        volume_owner_stored = self.storage.get(KEY_OWNER, "")
        ctx.volume_owner = Address(volume_owner_stored).to_hex(checksum=False)[2:] if volume_owner_stored else ""
        if not ctx.volume_owner:
            raise ValueError("cowbot volume owner is not initialized")

        agent_result = await runner.agent(
            model=DEFAULT_MODEL,
            query=ctx.text,
            system_prompt_path=f"/cbfs/{ctx.knowledge_volume}/cowbot/system_prompt.txt",
            session_id=ctx.thread_key,
            session_dir=f"/cbfs/{ctx.state_volume}/.sessions",
            volumes=[
                [ctx.knowledge_volume, "ro", ctx.volume_owner],
                [ctx.state_volume, "rw", ctx.volume_owner],
            ],
        )

        # Variables assigned between awaits MUST live on `ctx` to survive
        # across FSM segments — plain locals get reset between resume calls.
        # runner.agent returns a structured {"answer": "<text>", ...} result.
        # Unwrap the RunnerResult wrapper to .data BEFORE the dict check; then
        # read "answer" off it rather than .text: "answer" is last in the SDK's
        # text-key walk, so .text prefers "content" when a payload carries both
        # and recurses into a nested answer .raw.get would miss. Keying on the
        # agent contract keeps which field wins here, and siblings reachable.
        agent_data = getattr(agent_result, "data", agent_result)
        if isinstance(agent_data, dict):
            ctx.answer = str(agent_data.get("answer") or "")
        else:
            # Salvage what the runner did return — .text unwraps the canonical
            # text keys and passes a bare string through — so the thread still
            # gets an answer instead of a stranded lock.
            runtime.emit_event(
                "cowbot.agent_result_invalid",
                {"request_id": ctx.request_id, "thread_key": ctx.thread_key},
            )
            ctx.answer = agent_result.text
        if not ctx.answer:
            ctx.answer = "(no response — see logs for request_id=" + str(ctx.request_id) + ")"

        # 3. Update the placeholder with the real answer. Falls back to a new
        # postMessage if we never got a ts. Pick URL/body BEFORE the await so
        # the FSM has a single state for this transition (the compiler can't
        # split an await across if/else branches without a sync wrapper that
        # breaks `await`).
        if ctx.placeholder_ts:
            ctx.post_url = f"{SLACK_API_BASE}/chat.update"
            ctx.post_body = json.dumps({
                "channel": ctx.channel_id,
                "ts": ctx.placeholder_ts,
                "text": ctx.answer,
                "mrkdwn": True,
            }).encode("utf-8")
        else:
            ctx.post_url = f"{SLACK_API_BASE}/chat.postMessage"
            ctx.post_body = json.dumps({
                "channel": ctx.channel_id,
                "thread_ts": ctx.thread_ts,
                "text": ctx.answer,
                "mrkdwn": True,
            }).encode("utf-8")
        # secrets= only (no verify=): the inbound webhook was already
        # authenticated by step 1's `verify=`, so this post is gated. The runner
        # substitutes ${SLACK_BOT_TOKEN} from the owner account's read set.
        post_result = await runner.http(
            url=ctx.post_url,
            method="POST",
            headers={
                "Authorization": "Bearer ${SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=ctx.post_body,
            secrets=[{"account": ctx.owner_hex, "key": "SLACK_BOT_TOKEN"}],
        )

        # `await runner.http(...)` yields a RunnerResult wrapper; unwrap to .data
        # before the ok check below.
        posted = getattr(post_result, "data", post_result)

        # Clear pending and update history.
        pending = dict(self.storage.get(KEY_PENDING, {}))
        if pending.get(ctx.thread_key) == ctx.request_id:
            pending.pop(ctx.thread_key, None)
        self.storage[KEY_PENDING] = pending

        self._update_request(ctx.request_id, status="answered",
                             answer_len=len(ctx.answer))

        runtime.emit_event(
            "cowbot.answered",
            {
                "request_id": ctx.request_id,
                "thread_key": ctx.thread_key,
                "channel_id": ctx.channel_id,
                "user_id": ctx.user_id,
                "answer_len": len(ctx.answer),
            },
        )

        # Surface Slack post failures as events; don't fail the continuation.
        if not isinstance(posted, dict):
            runtime.emit_event(
                "cowbot.slack_post_error",
                {"request_id": ctx.request_id, "error": "non_object_result"},
            )
        elif posted.get("ok") is False:
            runtime.emit_event(
                "cowbot.slack_post_error",
                {"request_id": ctx.request_id, "error": posted.get("error", "unknown")},
            )

        return codec.encode({"request_id": ctx.request_id, "thread_key": ctx.thread_key})

    @pure
    def on_runner_result(self, payload):
        try:
            msg = codec.decode(payload) if isinstance(payload, bytes) else payload
        except Exception:
            msg = {}
        runner.handle_runner_result(self, msg)
        return b"ok: runner result handled"

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_requests(self, payload):
        return self.storage.get(KEY_REQUESTS, [])

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_pending(self, payload):
        return self.storage.get(KEY_PENDING, {})

    # ── Gateway-facing handlers (CIP-14 / CIP-15) ─────────────────────────────

    @public(decode="raw")
    @get("/health")
    @pure
    def health(self, payload):
        """GET /health — gateway-facing liveness probe.

        Path resolution + actor dispatch only; no chain mutation.
        """
        return _http_resp(200, [("content-type", "text/plain; charset=utf-8")], b"ok")

    # NOTE: there is intentionally NO set_signing_secret handler. The Slack
    # signing secret is a CBSS secret (SLACK_SIGNING_SECRET) owned by the
    # deployer EOA and granted to this actor under `secrets.verify`; the runner
    # MAC-verifies inbound webhooks (slack_http's first runner.http `verify=`,
    # CIP-24 §5.5). A validator cannot hold the secret, so the actor never
    # stores it.

    @callable_by(OWNER, decode="cbor")
    def set_volumes(self, payload):
        """Owner-only: update the CBFS volume names cowbot mounts by owner."""
        cfg = payload or {}
        knowledge_volume = cfg.get("knowledge_volume")
        state_volume = cfg.get("state_volume")
        if knowledge_volume:
            self.storage[KEY_KNOWLEDGE_VOLUME] = str(knowledge_volume)
        if state_volume:
            self.storage[KEY_STATE_VOLUME] = str(state_volume)
        runtime.emit_event("cowbot.volumes_set", {
            "owner": self.storage.get(KEY_OWNER, ""),
            "knowledge_volume": _storage_str(self.storage, KEY_KNOWLEDGE_VOLUME, DEFAULT_KNOWLEDGE_VOLUME),
            "state_volume": _storage_str(self.storage, KEY_STATE_VOLUME, DEFAULT_STATE_VOLUME),
        })
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        """Owner-only: install the CIP-15 routes table (raw CBOR Routes bytes).

        Payload is the verbatim CBOR blob produced by tools/emit_routes.py.
        """
        routes_bytes = payload if isinstance(payload, (bytes, bytearray)) else b""
        if not routes_bytes:
            raise ValueError("set_routes: empty payload")
        self.storage.set_raw(ROUTES_KEY, bytes(routes_bytes))
        runtime.emit_event("cowbot.routes_installed", {"bytes": len(routes_bytes)})
        return b"ok"


def _get_actor():
    return CowbotActor()


def init(payload):
    return _get_actor().init(payload)


def slack_http(payload):
    return _get_actor().slack_http(payload)


def slack_http__resume(payload):
    return _get_actor().slack_http__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_requests(payload):
    return _get_actor().get_requests(payload)


def get_pending(payload):
    return _get_actor().get_pending(payload)


def health(payload):
    return _get_actor().health(payload)


def set_volumes(payload):
    return _get_actor().set_volumes(payload)


def set_routes(payload):
    return _get_actor().set_routes(payload)
