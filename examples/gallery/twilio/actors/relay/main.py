"""
Twiliorelay — per-hire credential proxy for the Twilio hireable actor.

ONE of these is deployed by EACH hirer at connect time. It is the only
actor whose manifest names the hirer's Twilio secrets, so tenant
credentials are isolated per hire by construction:

  secrets.verify: ["<hirer>:TWILIO_AUTH_TOKEN"]   — signs inbound webhooks
  secrets.read:   ["<hirer>:TWILIO_API_BASIC"]    — base64("SKsid:secret")
                                                     Basic blob for outbound

Two-key split (Option B): the long-lived Auth Token verifies the inbound
X-Twilio-Signature and is NEVER substituted outbound; a scoped Standard API
Key (a base64 HTTP Basic blob) does the outbound REST send.

## Authenticated flow

Every entry point that does something sensitive is a SYNCHRONOUS `call()`
target that authenticates its caller via `runtime.get_sender()` — an async
mailbox could not (get_sender() is the receiver on async delivery). The
listed twilio actor is configured at init (`twilio`); both `relay_event` and
`post_answer` assert `sender == twilio`.

  relay_event  ← twilio.call() {N, tenant, preimage, sig, digest}
                 · assert sender == configured twilio actor
                 · schedule a FUNDED self-timer that launches verify_and_ack(N)
                   next block in THIS proxy's own frame — so the runner op's
                   submitter is this proxy (its manifest names the secrets) AND
                   it has enough access to fund the submit_job. A plain self-send
                   would give the right submitter but as a 162-access deferred
                   child that cannot fund the runner job (D36 / E1005); the timer
                   at TIMER_ACCESS_LIMIT (322) reserves one full deferred child.

  verify_and_ack (runner continuation): a NO-SECRET Twilio GET carrying ONLY
                 verify= (no TWILIO_API_BASIC — the outbound key is not
                 released here). The runner recomputes the Twilio HMAC-SHA1
                 over the relayed preimage and aborts fail-closed on mismatch
                 (CIP-24 §5.5) BEFORE anything else. On pass: record N in a
                 single-use verified set and call() twilio.tenant_verified;
                 on fail: call() twilio.tenant_rejected.

  post_answer  ← twilio.call() {N, to, from, text}
                 · assert sender == configured twilio actor
                 · assert N is in the single-use verified set (consume it)
                 · schedule a FUNDED self-timer that launches send_sms(N) next
                   block — POST Messages.json with the API key (same
                   submitter/funding reasoning as relay_event above)

The hirer owns this actor: revoke the secrets' ACL or delete the secrets to
pause the integration, no twilio/store involvement.
"""

import hashlib

import json
from cowboy_sdk import actor, runner, capture, codec, runtime, pure, deferred, public, callable_by, OWNER
from cowboy_sdk import call as sdk_call


KEY_OWNER = "owner"
KEY_TWILIO = "twilio"
KEY_API_BASE = "api_base"
KEY_ACCOUNT_SID = "account_sid"
KEY_OUTBOUND_FROM_NUMBER = "outbound_from_number"
KEY_AUTH_TOKEN_NAME = "auth_token_name"
KEY_API_BASIC_NAME = "api_basic_name"
KEY_VERIFIED = "verified"           # single-use set: request_ids whose MAC passed (small ints)
# Single-use intent records are stored PER-ITEM (relay:<N> / outbound:<N>), not as
# one aggregate dict: a relay intent carries the full Twilio preimage (up to the
# 16 KiB body cap), so a handful of concurrent large-body requests in a single
# dict would blow the 64 KiB per-KV-value hard cap. Per-item keeps each value to
# ONE intent; they are popped (deleted) on consume and are concurrency-bounded
# (only the authenticated relay_event, itself rate-limited by twilio's per-tenant
# in-flight cap, creates them).
KEY_RELAYS = "relay:"               # relay:<N> -> {preimage,sig,twilio_number} (gates run_verify)
KEY_OUTBOUND = "outbound:"          # outbound:<N> -> {to,from,text} (gates run_send)
KEY_POSTS = "posts"                 # bounded ops history (tiny {request_id,status} entries)
MAX_POST_HISTORY = 50
MAX_VERIFIED = 256                  # bound the pending verified set (small ints; << 64 KiB)

# Per-fire access budget for the funded self-timer that launches each runner op
# (verify_and_ack / send_sms). A plain self-send re-enters this proxy as a
# deferred child capped at DEFERRED_ACCESS_LIMIT = 162 (node
# types/src/constants.rs) — it cannot both do its own work AND fund the nested
# runner.http submit_job (needs REQUIRED_DEFERRED_CHILD_ACCESSES = 162), so the
# node refuses it (E1005) and the webhook is never verified/answered (D36). A
# timer fired on THIS proxy carries the same from==proxy attribution the
# secret release requires (the runner resolves verify=/secrets= against the
# submitter's manifest = this proxy), at TIMER_ACCESSES_LIMIT =
# TIMER_HANDLER_ACCESS_ALLOWANCE (160) + REQUIRED_DEFERRED_CHILD_ACCESSES (162)
# = 322, which reserves one full deferred child for the runner job.
TIMER_ACCESS_LIMIT = 322

DEFAULT_API_BASE = "https://api.twilio.com"
DEFAULT_AUTH_TOKEN_NAME = "TWILIO_AUTH_TOKEN"
DEFAULT_API_BASIC_NAME = "TWILIO_API_BASIC"

_UNRESERVED = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def _sender_hex() -> str:
    sender = runtime.get_sender()
    return "0x" + sender.hex() if isinstance(sender, bytes) else ""


def _norm_addr(addr: str) -> str:
    addr = str(addr or "").strip().lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    return addr


def _decode_args(payload):
    """Decode a synchronous call()'s CBOR args into a dict (or {})."""
    obj = payload
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = codec.decode(bytes(obj))
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


def _bytes_field(msg, key) -> bytes:
    value = msg.get(key)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return b""


def _request_digest(preimage: bytes, sig: str) -> str:
    """Byte-identical recipe to the twilio actor's — so the digest we echo on
    `tenant_verified` matches the one the actor stored for this request."""
    h = hashlib.sha256()
    h.update(preimage or b"")
    h.update(b"\x00")
    h.update((sig or "").encode("utf-8"))
    return h.hexdigest()


def _pct_encode(value: str) -> str:
    out = []
    for byte in str(value).encode("utf-8"):
        ch = chr(byte)
        if ch in _UNRESERVED:
            out.append(ch)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _form_encode(fields) -> bytes:
    """application/x-www-form-urlencoded body from an ordered list of pairs."""
    return "&".join(
        f"{_pct_encode(k)}={_pct_encode(v)}" for k, v in fields
    ).encode("utf-8")


def _outbound_form_fields(to_number: str, requested_from: str, text: str,
                          configured_from: str = ""):
    """Build a deterministic Twilio send body.

    A proxy-level sender override lets an inbound-only number keep its webhook
    while replies use the exact registered sender selected at deployment.
    """
    from_number = str(configured_from or requested_from or "").strip()
    return [("To", to_number), ("From", from_number), ("Body", text)]


@actor
class TwilioRelayActor:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        cfg = json.loads(payload) if payload else {}
        owner = _sender_hex()
        twilio = _norm_addr(cfg.get("twilio", ""))
        if len(twilio) != 40:
            raise ValueError("init: twilio actor address required")
        account_sid = str(cfg.get("account_sid", "")).strip()
        if not account_sid:
            raise ValueError("init: account_sid required")
        outbound_from_number = str(cfg.get("outbound_from_number", "")).strip()
        if outbound_from_number and (
            not outbound_from_number.startswith("+")
            or not outbound_from_number[1:].isdigit()
            or not 8 <= len(outbound_from_number[1:]) <= 15
        ):
            raise ValueError("init: outbound_from_number must be E.164")
        self.storage[KEY_OWNER] = owner
        self.storage[KEY_TWILIO] = twilio
        self.storage[KEY_API_BASE] = str(cfg.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        self.storage[KEY_ACCOUNT_SID] = account_sid
        self.storage[KEY_OUTBOUND_FROM_NUMBER] = outbound_from_number
        self.storage[KEY_AUTH_TOKEN_NAME] = cfg.get("auth_token_name", DEFAULT_AUTH_TOKEN_NAME)
        self.storage[KEY_API_BASIC_NAME] = cfg.get("api_basic_name", DEFAULT_API_BASIC_NAME)
        self.storage[KEY_VERIFIED] = []
        self.storage[KEY_POSTS] = []
        runtime.emit_event("twiliorelay.init", {
            "owner": owner,
            "twilio": "0x" + twilio,
        })
        return b"ok: initialized"

    def _require_twilio(self) -> bool:
        """The caller must be the configured listed twilio actor."""
        expected = _norm_addr(self.storage.get(KEY_TWILIO, ""))
        if _norm_addr(_sender_hex()) != expected:
            runtime.emit_event("twiliorelay.unauthorized", {"sender": _sender_hex()})
            return False
        return True

    def _verified_add(self, request_id: int):
        ids = list(self.storage.get(KEY_VERIFIED, []))
        if request_id not in ids:
            ids.append(request_id)
        if len(ids) > MAX_VERIFIED:
            ids = ids[-MAX_VERIFIED:]
        self.storage[KEY_VERIFIED] = ids

    def _verified_consume(self, request_id: int) -> bool:
        """Single-use: return True and remove the id iff it was present."""
        ids = list(self.storage.get(KEY_VERIFIED, []))
        if request_id not in ids:
            return False
        ids = [i for i in ids if i != request_id]
        self.storage[KEY_VERIFIED] = ids
        return True

    def _intent_put(self, prefix: str, request_id: int, value: dict):
        # Per-item: one KV value per intent (bounded by the intent, never a count).
        self.storage[prefix + str(request_id)] = value

    def _intent_pop(self, prefix: str, request_id: int):
        # Single-use: read then delete so a consumed intent can't be re-launched.
        key = prefix + str(request_id)
        value = self.storage.get(key)
        if key in self.storage:
            del self.storage[key]
        return value

    # ── Authenticated entry points (synchronous call() targets) ──────────────
    #
    # WHY the sync call() + funded self-timer split: a runner op's submitter is
    # the enclosing TOP-LEVEL frame's actor (it governs the verify=/secrets=
    # owning account). If this proxy launched the verify/send op directly inside
    # a call()-reached handler (which runs within twilio's tx), the op's
    # submitter would be twilio — and the runner would resolve the verify/read
    # secrets against twilio's manifest (which has none), failing "secret_ref not
    # resolvable". So we AUTHENTICATE here (get_sender() is real under call()),
    # record a single-use KV intent, and schedule a FUNDED self-timer: the timer
    # fires on THIS proxy (submitter == this proxy, correct) with enough access
    # to fund the runner submit_job. A plain self-send would also re-enter the
    # proxy with the right submitter, but as a 162-access deferred child that
    # cannot fund the nested submit_job (D36 / E1005); the timer at
    # TIMER_ACCESS_LIMIT reserves one full deferred child for it.

    @public(decode="raw")
    @deferred
    def relay_event(self, payload):
        """twilio → proxy, AUTHENTICATED. Record the relay intent and schedule a
        funded self-timer so the MAC-gate op runs in THIS proxy's own frame
        (correct submitter) with enough access to fund the runner job (D36)."""
        if not self._require_twilio():
            return codec.encode({"r": "err: unauthorized"})
        msg = _decode_args(payload)
        request_id = int(msg.get("request_id", 0) or 0)
        self._intent_put(KEY_RELAYS, request_id, {
            "twilio_number": str(msg.get("twilio_number", "") or ""),
            "preimage": _bytes_field(msg, "preimage"),
            "sig": str(msg.get("sig", "") or ""),
        })
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            codec.encode({"kind": "run_verify", "request_id": request_id}),
            access_limit=TIMER_ACCESS_LIMIT,
        )
        return codec.encode({"r": "ok: relayed"})

    @public(decode="raw")
    @deferred
    def handle_timer(self, payload):
        """Timer-fire router (the node's DEFAULT_TIMER_HANDLER, "handle_timer").
        relay_event / post_answer each schedule a funded self-timer carrying a
        `kind`; on fire this routes by `kind` to the gated launcher. Each
        launcher consumes a single-use KV intent that only an authenticated entry
        point (relay_event / post_answer) creates, so a stray fire (or a plain
        external send that lands here) launches nothing."""
        msg = _decode_args(payload)
        kind = msg.get("kind")
        if kind == "run_verify":
            return self.run_verify(msg)
        if kind == "run_send":
            return self.run_send(msg)
        runtime.emit_event("twiliorelay.unknown_message", {"kind": str(kind)[:40]})
        return codec.encode({"r": "ignored"})

    @public(decode="raw")
    @deferred
    def run_verify(self, payload):
        """Timer-triggered (via handle_timer): launch verify_and_ack in THIS
        proxy's own funded timer frame. Gated by a single-use relay intent (only
        the authenticated relay_event creates one), so a fire/send with no
        matching intent is ignored — bounds the forged-webhook gas drain and
        blocks direct triggering."""
        msg = _decode_args(payload)
        request_id = int(msg.get("request_id", 0) or 0)
        intent = self._intent_pop(KEY_RELAYS, request_id)
        if not intent:
            return codec.encode({"r": "no intent"})
        self.verify_and_ack({
            "request_id": request_id,
            "twilio_number": str(intent.get("twilio_number", "") or ""),
            "preimage": _bytes_field(intent, "preimage"),
            "sig": str(intent.get("sig", "") or ""),
        })
        return codec.encode({"r": "verifying"})

    @runner.continuation
    async def verify_and_ack(self, payload):
        """MAC-gate the relayed Twilio webhook (CIP-24 §5.5).

        THE GATE: `verify=` makes the runner recompute the Twilio HMAC-SHA1
        signature over the relayed preimage — `url + sorted(params)`, exactly
        what Twilio signed — and abort fail-closed on mismatch BEFORE anything.
        The op is a NO-SECRET Twilio GET: it carries verify= only, releasing NO
        outbound key (the API key's first release stays the final Messages POST,
        so recent-message content never lands in this job's result/on-chain).
        """

        msg = _decode_args(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(msg.get("request_id", 0) or 0)
        ctx.twilio_number = str(msg.get("twilio_number", "") or "")
        ctx.preimage = _bytes_field(msg, "preimage")
        ctx.signature = str(msg.get("sig", "") or "")
        ctx.digest = _request_digest(ctx.preimage, ctx.signature)
        ctx.owner_hex = _norm_addr(self.storage.get(KEY_OWNER, ""))
        ctx.api_base = self.storage.get(KEY_API_BASE, DEFAULT_API_BASE)
        ctx.auth_token_name = self.storage.get(KEY_AUTH_TOKEN_NAME, DEFAULT_AUTH_TOKEN_NAME)
        ctx.twilio = self.storage.get(KEY_TWILIO, "")

        # No-secret carrier: a bare GET to the Twilio API root. The verify=
        # precondition is checked by the runner BEFORE the request executes, so
        # the GET's own auth/status is irrelevant — it exists only to carry the
        # MAC gate. The X-Cowboy-Verify-Key header names the verify secret so
        # the post-CIP-11 runner can recover its key name (rendered as the
        # literal placeholder; verify keys are never substituted).
        gate_result = await runner.http(
            url=f"{ctx.api_base}/2010-04-01",
            method="GET",
            headers={
                "X-Cowboy-Verify-Key": "${" + ctx.auth_token_name + "}",
            },
            verify={
                "secret": {"account": ctx.owner_hex, "key": ctx.auth_token_name},
                "message": ctx.preimage,
                "signature": ctx.signature,
                # Twilio's X-Twilio-Signature is base64(HMAC-SHA1(auth_token,
                # preimage)) with no scheme prefix. The protocol carries only
                # NEUTRAL crypto primitives — "Twilio" lives here, in the actor.
                "algo": {"hmac": {"hash": "sha1", "encoding": "base64"}},
            },
        )

        # runner.http decodes the response into the canonical dict; the MAC
        # precondition-failure envelope ({cbss_precondition_failed: true}) is a
        # dict too. `await runner.http(...)` yields a RunnerResult wrapper, not a
        # dict — unwrap to .data BEFORE the isinstance/precondition check (getattr
        # so a value already a bare dict passes through instead of AttributeError).
        gate = getattr(gate_result, "data", gate_result)
        if not isinstance(gate, dict) or gate.get("cbss_precondition_failed"):
            # A non-object result is treated as a MAC failure: the webhook was
            # never confirmed authentic. Either way we must reply, not raise —
            # twilio.py's per-conversation slot is released only by a
            # tenant_rejected / tenant_verified callback.
            runtime.emit_event("twiliorelay.precondition_failed", {
                "request_id": ctx.request_id,
                "reason": "mac" if isinstance(gate, dict) else "non_object_result",
            })
            try:
                sdk_call(bytes.fromhex(ctx.twilio), "tenant_rejected", {
                    "request_id": ctx.request_id,
                    "twilio_number": ctx.twilio_number,
                })
            except Exception as e:
                runtime.emit_event("twiliorelay.callback_failed", {
                    "kind": "tenant_rejected", "error": str(e)[:160],
                })
            return b"ok: precondition failed"

        # MAC passed → mark single-use and report verified (authenticated call).
        self._verified_add(ctx.request_id)
        try:
            result = sdk_call(bytes.fromhex(ctx.twilio), "tenant_verified", {
                "request_id": ctx.request_id,
                "twilio_number": ctx.twilio_number,
                "digest": ctx.digest,
            })
        except Exception as e:
            # Couldn't notify — drop the marker so a stale post_answer can't
            # later fire against it, and so the verified set doesn't leak.
            self._verified_consume(ctx.request_id)
            runtime.emit_event("twiliorelay.callback_failed", {
                "kind": "tenant_verified", "error": str(e)[:160],
            })
            return b"ok: verified (notify failed)"
        # Twilio only proceeds to an agent+send when it returns "ok: verified".
        # On ANY other verdict (stale / replay / tenant mismatch / digest
        # mismatch / no declaration) it will never call post_answer, so RELEASE
        # the single-use marker now — otherwise a rejected verification would
        # leak a live-auth entry in the bounded verified set (attacker could
        # accumulate these via replays to evict genuine markers).
        # `call()` decodes the reply, so a non-dict here is a malformed verdict,
        # not an undecoded one — leave it empty and release the marker below.
        verdict = ""
        if isinstance(result, dict):
            verdict = str(result.get("r", ""))
        if verdict != "ok: verified":
            self._verified_consume(ctx.request_id)
            runtime.emit_event("twiliorelay.verification_declined", {
                "request_id": ctx.request_id, "verdict": verdict[:40],
            })
            return b"ok: verification declined"
        runtime.emit_event("twiliorelay.event_verified", {"request_id": ctx.request_id})
        return b"ok: verified"

    @public(decode="raw")
    @deferred
    def post_answer(self, payload):
        """twilio → proxy, AUTHENTICATED. Only the configured twilio actor may
        request a send, and only for a request THIS proxy actually verified
        (single-use). Schedules the outbound send on a funded self-timer."""
        if not self._require_twilio():
            return codec.encode({"r": "err: unauthorized"})
        msg = _decode_args(payload)
        request_id = int(msg.get("request_id", 0) or 0)
        if not self._verified_consume(request_id):
            # Not a request we verified (or already consumed) → refuse. This is
            # what stops a forged/replayed send from releasing the API key.
            runtime.emit_event("twiliorelay.unverified_send", {"request_id": request_id})
            return codec.encode({"r": "err: not verified"})
        # Record the outbound intent + schedule a funded self-timer so the send
        # op runs in THIS proxy's own frame (correct submitter for the
        # secrets.read release) with enough access to fund the runner job (D36).
        self._intent_put(KEY_OUTBOUND, request_id, {
            "to_number": str(msg.get("to_number", "") or ""),
            "from_number": str(msg.get("from_number", "") or ""),
            "text": str(msg.get("text", "") or ""),
        })
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            codec.encode({"kind": "run_send", "request_id": request_id}),
            access_limit=TIMER_ACCESS_LIMIT,
        )
        return codec.encode({"r": "ok: accepted"})

    @public(decode="raw")
    @deferred
    def run_send(self, payload):
        """Timer-triggered (via handle_timer): launch send_sms in THIS proxy's
        own funded timer frame. Gated by a single-use outbound intent that only
        the authenticated (and verified-consumed) post_answer creates."""
        msg = _decode_args(payload)
        request_id = int(msg.get("request_id", 0) or 0)
        intent = self._intent_pop(KEY_OUTBOUND, request_id)
        if not intent:
            return codec.encode({"r": "no intent"})
        self.send_sms({
            "request_id": request_id,
            "to_number": str(intent.get("to_number", "") or ""),
            "from_number": str(intent.get("from_number", "") or ""),
            "text": str(intent.get("text", "") or ""),
        })
        return codec.encode({"r": "sending"})

    @runner.continuation
    async def send_sms(self, payload):
        """Send the answer SMS with the hirer's API key (already MAC-gated and
        single-use-consumed by post_answer)."""

        msg = _decode_args(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(msg.get("request_id", 0) or 0)
        ctx.to_number = str(msg.get("to_number", "") or "")
        ctx.from_number = str(msg.get("from_number", "") or "")
        ctx.text = str(msg.get("text", "") or "")
        ctx.owner_hex = _norm_addr(self.storage.get(KEY_OWNER, ""))
        ctx.api_base = self.storage.get(KEY_API_BASE, DEFAULT_API_BASE)
        ctx.account_sid = self.storage.get(KEY_ACCOUNT_SID, "")
        ctx.configured_from_number = self.storage.get(KEY_OUTBOUND_FROM_NUMBER, "")
        ctx.api_basic_name = self.storage.get(KEY_API_BASIC_NAME, DEFAULT_API_BASIC_NAME)

        ctx.post_url = f"{ctx.api_base}/2010-04-01/Accounts/{ctx.account_sid}/Messages.json"
        # raw_body (not body): Twilio's REST API is form-encoded, not JSON — the
        # node's JSON-only `body` path would reject it. Sent byte-for-byte.
        ctx.post_body = _form_encode(_outbound_form_fields(
            ctx.to_number,
            ctx.from_number,
            ctx.text,
            ctx.configured_from_number,
        ))

        post_result = await runner.http(
            url=ctx.post_url,
            method="POST",
            headers={
                "Authorization": "Basic ${" + ctx.api_basic_name + "}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            raw_body=ctx.post_body,
            secrets=[{"account": ctx.owner_hex, "key": ctx.api_basic_name}],
        )

        # Twilio returns JSON: a successful create has a message `sid` (SM…);
        # an error has a numeric `code` + `message` and NO sid. Check the body
        # (the actor-visible result `data`) — the HTTP status lives only in the
        # source attestation, which is not surfaced here.
        # Fail-soft: this runs AFTER the POST, so the SMS may already be out
        # the door — record the outcome rather than raising. `await
        # runner.http(...)` yields a RunnerResult wrapper, not a dict — unwrap to
        # .data BEFORE the isinstance check (getattr so a value already a bare
        # dict passes through instead of AttributeError).
        posted = getattr(post_result, "data", post_result)
        status = "sent"
        detail = ""
        if not isinstance(posted, dict):
            status = "send_error"
            detail = "non_object_result"
        elif posted.get("cbss_precondition_failed"):
            status = "send_error"
            detail = "precondition_failed"
        elif not posted.get("sid"):
            status = "send_error"
            detail = str(posted.get("code", posted.get("message", "unknown")))[:120]
        if status == "send_error":
            runtime.emit_event("twiliorelay.send_error", {
                "request_id": ctx.request_id, "error": detail or "no_sid",
            })

        posts = list(self.storage.get(KEY_POSTS, []))
        posts.append({"request_id": ctx.request_id, "status": status})
        if len(posts) > MAX_POST_HISTORY:
            posts = posts[-MAX_POST_HISTORY:]
        self.storage[KEY_POSTS] = posts
        return codec.encode({"request_id": ctx.request_id, "status": status})

    @pure
    def on_runner_result(self, payload):
        try:
            msg = codec.decode(payload) if isinstance(payload, bytes) else payload
        except Exception:
            msg = {}
        runner.handle_runner_result(self, msg)
        return b"ok: runner result handled"

    @callable_by(OWNER, decode="cbor")
    def get_posts(self, payload):
        return self.storage.get(KEY_POSTS, [])


def _get_actor():
    return TwilioRelayActor()


def init(payload):
    return _get_actor().init(payload)


def relay_event(payload):
    return _get_actor().relay_event(payload)


def handle_timer(payload):
    return _get_actor().handle_timer(payload)


def run_verify(payload):
    return _get_actor().run_verify(payload)


def run_send(payload):
    return _get_actor().run_send(payload)


# NOTE: the initial continuation handlers `verify_and_ack` and `send_sms` are
# deliberately NOT exported at module level. `@actor` skips permission wrapping
# for continuations, so exporting them would make them directly dispatchable —
# bypassing relay_event/post_answer's sender+intent gates and releasing the
# hirer's key for attacker-chosen content. They are only ever started via
# `self.<cont>()` from the gated launchers (run_verify/run_send); resume routes
# through on_runner_result → handle_runner_result → the instance `__resume`.


def _resume_is_self() -> bool:
    """A genuine continuation resume is a runner-result callback tx synthesized
    with from == this actor (transaction.rs). `@actor` leaves resume methods
    permission-unwrapped, so these module-level exports force-gate on
    sender == self — a forged `__resume` (e.g. faking a send result) is denied."""
    a = runtime.get_actor_address()
    self_hex = (a.hex() if isinstance(a, bytes) else _norm_addr(str(a)))
    return _norm_addr(_sender_hex()) == self_hex


def verify_and_ack__resume(payload):
    if not _resume_is_self():
        runtime.emit_event("twiliorelay.resume_denied", {"handler": "verify_and_ack__resume"})
        return codec.encode({"r": "denied"})
    return _get_actor().verify_and_ack__resume(payload)


def post_answer(payload):
    return _get_actor().post_answer(payload)


def send_sms__resume(payload):
    if not _resume_is_self():
        runtime.emit_event("twiliorelay.resume_denied", {"handler": "send_sms__resume"})
        return codec.encode({"r": "denied"})
    return _get_actor().send_sms__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_posts(payload):
    return _get_actor().get_posts(payload)
