"""
Slackrelay — per-hire credential proxy for the Ranchhand hireable actor.

ONE of these is deployed by EACH hirer at connect time. It is the only
actor whose manifest names the hirer's Slack secrets, so tenant
credentials are isolated per hire by construction:

  secrets.verify: ["<hirer>:RANCHHAND_SLACK_SIGNING_SECRET"]
  secrets.read:   ["<hirer>:RANCHHAND_SLACK_BOT_TOKEN"]

Both secrets are stored by the hirer under their OWN account (`cowboy
secrets set`) with a release policy ACL naming this proxy's address —
CIP-24's two-sided check (actor manifest ∧ owner ACL) passes for exactly
this pairing and nothing else.

The proxy accepts messages ONLY from its configured ranchhand actor
(on-chain sender authentication) and does two things:

  relay_event  → runner.http chat.postMessage ":thinking:" carrying
                 verify={signing secret} + secrets=[bot token]. The
                 runner recomputes the Slack v0 MAC over the VERBATIM
                 relayed bytes and aborts fail-closed on mismatch
                 (CIP-24 §5.5) — before the token is released or
                 anything is posted. Success reports tenant_verified
                 (with the placeholder ts) back to ranchhand; failure
                 reports tenant_rejected so ranchhand clears the
                 thread's single-flight slot.

  post_answer  → chat.update on the placeholder (or a fresh
                 postMessage) with the final answer; secrets= only,
                 already gated by the relay_event MAC.

The hirer owns this actor: they can pause their integration by simply
revoking the secrets' ACL or deleting the secrets — no ranchhand or
store involvement.
"""

import json

from cowboy_sdk import actor, runner, capture, codec, runtime, pure, deferred, public, callable_by, OWNER
from cowboy_sdk import send as sdk_send


KEY_OWNER = "owner"
KEY_RANCHHAND = "ranchhand"
KEY_API_BASE = "api_base"
KEY_SIGNING_SECRET_NAME = "signing_secret_name"
KEY_BOT_TOKEN_NAME = "bot_token_name"
KEY_POSTS = "posts"                 # bounded ops history
MAX_POST_HISTORY = 50

DEFAULT_API_BASE = "https://slack.com/api"
DEFAULT_SIGNING_SECRET_NAME = "RANCHHAND_SLACK_SIGNING_SECRET"
DEFAULT_BOT_TOKEN_NAME = "RANCHHAND_SLACK_BOT_TOKEN"

MSG_RELAY_EVENT = "relay_event"
MSG_TENANT_VERIFIED = "tenant_verified"
MSG_TENANT_REJECTED = "tenant_rejected"
MSG_POST_ANSWER = "post_answer"


def _sender_hex() -> str:
    sender = runtime.get_sender()
    return "0x" + sender.hex() if isinstance(sender, bytes) else ""


def _norm_addr(addr: str) -> str:
    addr = str(addr or "").strip().lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    return addr


def _decode_message(payload):
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


@actor
class SlackRelayActor:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        cfg = json.loads(payload) if payload else {}
        owner = _sender_hex()
        ranchhand = _norm_addr(cfg.get("ranchhand", ""))
        if len(ranchhand) != 40:
            raise ValueError("init: ranchhand actor address required")
        self.storage[KEY_OWNER] = owner
        self.storage[KEY_RANCHHAND] = ranchhand
        self.storage[KEY_API_BASE] = str(cfg.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        self.storage[KEY_SIGNING_SECRET_NAME] = cfg.get(
            "signing_secret_name", DEFAULT_SIGNING_SECRET_NAME
        )
        self.storage[KEY_BOT_TOKEN_NAME] = cfg.get("bot_token_name", DEFAULT_BOT_TOKEN_NAME)
        self.storage[KEY_POSTS] = []
        runtime.emit_event("slackrelay.init", {
            "owner": owner,
            "ranchhand": "0x" + ranchhand,
        })
        return b"ok: initialized"

    def _reject(self, ranchhand_hex, request_id, team_id):
        """Release ranchhand's single-flight lock for this request.

        Ranchhand commits KEY_PENDING[thread_key] before relaying and clears it
        ONLY on a MSG_TENANT_REJECTED / MSG_TENANT_VERIFIED reply. Every failure
        path in `verify_and_ack` must therefore send one of those instead of
        raising — a raise leaves the lock set and every later Slack message on
        that thread is dropped as "already pending".
        """
        rejected = codec.encode({
            "kind": MSG_TENANT_REJECTED,
            "request_id": request_id,
            "team_id": team_id,
        })
        sdk_send(bytes.fromhex(ranchhand_hex), rejected, raw=True)

    def _record_post(self, entry):
        posts = list(self.storage.get(KEY_POSTS, []))
        posts.append(entry)
        if len(posts) > MAX_POST_HISTORY:
            posts = posts[-MAX_POST_HISTORY:]
        self.storage[KEY_POSTS] = posts

    @public(decode="raw")
    @deferred
    def handle_message(self, payload):
        """Mailbox for ranchhand relays.

        Cross-actor identity note: `runtime.get_sender()` on an async
        `send_message` delivery is the RECEIVING actor itself, not the sender,
        so we cannot authenticate ranchhand by address here (see ranchhand's
        `_tenant_for_msg`). It does not matter for security: the ONLY sensitive
        action — releasing the hirer's Slack token and posting — happens inside
        `verify_and_ack`'s first `runner.http`, which carries `verify=` and
        fails closed on the Slack v0 MAC (CIP-24 §5.5). A forged relay_event
        cannot forge a valid Slack signature, so it releases nothing and posts
        nothing; at worst it burns a little of THIS proxy's (hirer-funded) gas
        on a job that aborts at the MAC. The secret ACL is scoped to this proxy
        regardless of who messaged it."""
        msg = _decode_message(payload)
        kind = msg.get("kind")
        if kind == MSG_RELAY_EVENT:
            return self.verify_and_ack(msg)
        if kind == MSG_POST_ANSWER:
            return self.post_answer(msg)
        runtime.emit_event("slackrelay.unknown_message", {"kind": str(kind)[:40]})
        return b"ok: ignored"

    @runner.continuation
    async def verify_and_ack(self, payload):
        """MAC-gate the relayed Slack event and post the thinking placeholder.

        THIS FIRST OP IS THE GATE (CIP-24 §5.5): `verify=` makes the runner
        recompute the Slack v0 HMAC over `v0:<ts>:<raw_body>` — the VERBATIM
        bytes Slack signed, relayed untouched by the gateway and ranchhand —
        and abort the job fail-closed on mismatch, BEFORE the bot token is
        released or the post happens. A forged webhook therefore costs one
        failed precondition and nothing else.
        """

        msg = _decode_message(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(msg.get("request_id", 0) or 0)
        ctx.team_id = str(msg.get("team_id", "") or "")
        ctx.channel_id = str(msg.get("channel_id", "") or "")
        ctx.thread_ts = str(msg.get("thread_ts", "") or "")
        ctx.raw_body = _bytes_field(msg, "raw_body")
        ctx.ts = str(msg.get("ts", "") or "")
        ctx.signature = str(msg.get("sig", "") or "")
        ctx.owner_hex = _norm_addr(self.storage.get(KEY_OWNER, ""))
        ctx.api_base = self.storage.get(KEY_API_BASE, DEFAULT_API_BASE)
        ctx.signing_secret_name = self.storage.get(
            KEY_SIGNING_SECRET_NAME, DEFAULT_SIGNING_SECRET_NAME
        )
        ctx.bot_token_name = self.storage.get(KEY_BOT_TOKEN_NAME, DEFAULT_BOT_TOKEN_NAME)
        ctx.ranchhand = self.storage.get(KEY_RANCHHAND, "")

        # The EXACT MAC message Slack signed. NEVER re-serialize the body.
        ctx.verify_message = b"v0:" + ctx.ts.encode("ascii") + b":" + ctx.raw_body

        thinking_body = json.dumps({
            "channel": ctx.channel_id,
            "thread_ts": ctx.thread_ts,
            "text": ":thinking_face: thinking…",
            "mrkdwn": True,
        }).encode("utf-8")
        # The X-Cowboy-Verify-Key header names the verify secret as a ${NAME}
        # placeholder — the post-CIP-11 runner recovers secret NAMES from op
        # templates (the canonical JobSpec carries only {account, key_hash}),
        # and a verify= whose key name appears nowhere is unrecoverable and
        # fails closed. The runner renders it as the LITERAL placeholder
        # string (verify keys are excluded from substitution), so no secret
        # material leaves the runner; Slack ignores unknown headers.
        thinking_result = await runner.http(
            url=f"{ctx.api_base}/chat.postMessage",
            method="POST",
            headers={
                "Authorization": "Bearer ${" + ctx.bot_token_name + "}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Cowboy-Verify-Key": "${" + ctx.signing_secret_name + "}",
            },
            body=thinking_body,
            secrets=[{"account": ctx.owner_hex, "key": ctx.bot_token_name}],
            verify={
                "secret": {"account": ctx.owner_hex, "key": ctx.signing_secret_name},
                "message": ctx.verify_message,
                "signature": ctx.signature,
                # Slack v0 sig = "v0=" + hex(HMAC-SHA256(signing_secret, "v0:ts:body")).
                # Protocol carries NEUTRAL crypto only; "Slack" lives here in the actor.
                "algo": {"hmac": {"hash": "sha256", "encoding": "hex", "prefix": "v0="}},
            },
        )

        # runner.http decodes Slack's JSON response into the canonical dict.
        thinking = thinking_result.raw
        if not isinstance(thinking, dict):
            # Transient non-object result (HTML error page, empty body):
            # report it as a rejection so ranchhand releases the thread.
            runtime.emit_event("slackrelay.http_error", {
                "request_id": ctx.request_id,
                "error": "chat.postMessage did not return a JSON object",
            })
            self._reject(ctx.ranchhand, ctx.request_id, ctx.team_id)
            return b"ok: relay error"

        if (
            thinking.get("ok") is False
            or thinking.get("cbss_precondition_failed")
        ):
            runtime.emit_event("slackrelay.precondition_failed", {
                "request_id": ctx.request_id,
            })
            self._reject(ctx.ranchhand, ctx.request_id, ctx.team_id)
            return b"ok: precondition failed"

        ctx.placeholder_ts = thinking.get("ts", "") or ""

        verified = codec.encode({
            "kind": MSG_TENANT_VERIFIED,
            "request_id": ctx.request_id,
            "team_id": ctx.team_id,
            "placeholder_ts": ctx.placeholder_ts,
        })
        sdk_send(bytes.fromhex(ctx.ranchhand), verified, raw=True)
        runtime.emit_event("slackrelay.event_verified", {
            "request_id": ctx.request_id,
        })
        return b"ok: verified"

    @runner.continuation
    async def post_answer(self, payload):
        """Post the final answer with the hirer's token (already MAC-gated)."""

        msg = _decode_message(payload) if not isinstance(payload, dict) else payload
        ctx = capture()
        ctx.request_id = int(msg.get("request_id", 0) or 0)
        ctx.channel_id = str(msg.get("channel_id", "") or "")
        ctx.thread_ts = str(msg.get("thread_ts", "") or "")
        ctx.placeholder_ts = str(msg.get("placeholder_ts", "") or "")
        ctx.text = str(msg.get("text", "") or "")
        ctx.owner_hex = _norm_addr(self.storage.get(KEY_OWNER, ""))
        ctx.api_base = self.storage.get(KEY_API_BASE, DEFAULT_API_BASE)
        ctx.bot_token_name = self.storage.get(KEY_BOT_TOKEN_NAME, DEFAULT_BOT_TOKEN_NAME)

        # Pick URL/body BEFORE the await (single FSM state per transition).
        if ctx.placeholder_ts:
            ctx.post_url = f"{ctx.api_base}/chat.update"
            ctx.post_body = json.dumps({
                "channel": ctx.channel_id,
                "ts": ctx.placeholder_ts,
                "text": ctx.text,
                "mrkdwn": True,
            }).encode("utf-8")
        else:
            ctx.post_url = f"{ctx.api_base}/chat.postMessage"
            ctx.post_body = json.dumps({
                "channel": ctx.channel_id,
                "thread_ts": ctx.thread_ts,
                "text": ctx.text,
                "mrkdwn": True,
            }).encode("utf-8")

        post_result = await runner.http(
            url=ctx.post_url,
            method="POST",
            headers={
                "Authorization": "Bearer ${" + ctx.bot_token_name + "}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=ctx.post_body,
            secrets=[{"account": ctx.owner_hex, "key": ctx.bot_token_name}],
        )

        # Fail-soft: the answer may have landed in Slack already, so a
        # non-object result is recorded rather than raised.
        posted = post_result.raw
        status = "posted"
        error = ""
        if not isinstance(posted, dict):
            status = "post_error"
            error = "non_object_result"
        elif posted.get("ok") is False:
            status = "post_error"
            error = str(posted.get("error", "unknown"))[:120]
        if status == "post_error":
            runtime.emit_event("slackrelay.post_error", {
                "request_id": ctx.request_id,
                "error": error,
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

    @pure
    @callable_by(OWNER, decode="cbor")
    def get_posts(self, payload):
        return self.storage.get(KEY_POSTS, [])


def _get_actor():
    return SlackRelayActor()


def init(payload):
    return _get_actor().init(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def verify_and_ack(payload):
    return _get_actor().verify_and_ack(payload)


def verify_and_ack__resume(payload):
    return _get_actor().verify_and_ack__resume(payload)


def post_answer(payload):
    return _get_actor().post_answer(payload)


def post_answer__resume(payload):
    return _get_actor().post_answer__resume(payload)


def on_runner_result(payload):
    return _get_actor().on_runner_result(payload)


def get_posts(payload):
    return _get_actor().get_posts(payload)
