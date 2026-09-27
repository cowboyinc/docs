"""Notifier actor — Telegram webhook ingress and bounded outbound delivery."""

import hashlib
import json

from cowboy_sdk import (
    OWNER,
    SELF,
    AddressError,
    actor,
    callable_by,
    capture,
    codec,
    deferred,
    public,
    runner,
    runtime,
    to_address_bytes,
)
from cowboy_sdk.routes import get, post

ROUTES_KEY = "__cowboy/routes"
KEY_WEBHOOK_SECRET_HASH = "webhook_secret_hash"
KEY_WEBHOOK_UNCONFIGURED_REPORTED = "webhook_unconfigured_reported"
KEY_SEEN_UPDATES = "seen_update_ids"

TG_SECRET_KEY = "TELEGRAM_BOT_TOKEN"
TG_API = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"


def _normalized_address(value):
    if isinstance(value, str):
        value = value.strip()
    try:
        return "0x" + to_address_bytes(value).hex()
    except AddressError:
        return ""


def _sender_matches(expected):
    return bool(_normalized_address(expected)) and (
        _normalized_address(runtime.get_sender()) == _normalized_address(expected)
    )


def _secret_refs(cfg):
    """Resolve the Telegram token from the deployer's CBSS account.

    Bare secret names resolve against the calling actor account.  The token is
    owned by the deployer, so the runner job must carry an explicit cross-account
    reference that matches both the manifest grant and the CBSS release policy.
    """
    account = _normalized_address((cfg or {}).get("secret_account", ""))
    if not account:
        raise ValueError("notify secret_account must be a 20-byte address")
    return [[account, TG_SECRET_KEY]]


def tg_encode(text):
    out = []
    for b in str(text).encode("utf-8"):
        c = chr(b)
        out.append(c if c in _SAFE else "%%%02X" % b)
    return "".join(out)


def _parse(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8") if payload else "{}"
        v = json.loads(payload)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _json_bytes(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _http_resp(status, body):
    return codec.encode(
        {
            "status": int(status),
            "headers": [("content-type", "application/json")],
            "body": body if isinstance(body, bytes) else str(body).encode("utf-8"),
        }
    )


def _webhook_update(payload):
    try:
        env = (
            codec.decode(bytes(payload))
            if isinstance(payload, (bytes, bytearray))
            else payload
        )
    except Exception:
        return {}, []
    if not isinstance(env, dict):
        return {}, []
    headers = env.get("headers") or []
    body = env.get("body") or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        update = json.loads(bytes(body).decode("utf-8"))
    except Exception:
        update = {}
    return update if isinstance(update, dict) else {}, headers


def _header(headers, wanted):
    wanted = wanted.lower()
    for item in headers:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            if str(item[0]).lower() == wanted:
                return str(item[1])
    return ""


# node 04c9f283 types/src/constants.rs:1880-1881: 160 own + 162 child.
TIMER_ACCESS_LIMIT = 322


@actor
class HlNotify:
    def init(self, payload):
        if self.storage.get("cfg"):
            return b"ok: already initialized"
        obj = _parse(payload)
        runtime.assume_ownership_if_unowned()  # else every @callable_by(OWNER) reverts
        secret_account = _normalized_address(obj.get("secret_account", ""))
        if not secret_account:
            raise ValueError("init: secret_account must be a 20-byte address")
        self.storage["cfg"] = {
            "max_outbox": int(obj.get("max_outbox", 20)),
            "max_guidance": int(obj.get("max_guidance", 12)),
            "secret_account": secret_account,
        }
        self.storage["outbox"] = []
        self.storage["guidance"] = []
        self.storage[KEY_SEEN_UPDATES] = []
        self.storage["guidance_dirty"] = False
        self.storage["cached_research_stats"] = {}
        self.storage["cached_ideas"] = []
        self.storage["cached_pnl"] = {}
        self.storage["cached_portfolio"] = {}
        runtime.emit_event("notify.init", {})
        return b"ok"

    # ---- config ------------------------------------------------------------- #

    @callable_by(OWNER, decode="raw")
    @deferred
    def set_telegram(self, payload):
        obj = _parse(payload)
        self.storage["chat_id"] = str(obj.get("chat_id", ""))
        runtime.emit_event(
            "notify.telegram_set", {"chat": bool(self.storage["chat_id"])}
        )
        self._wake_webhook_drain()
        return b"ok"

    @callable_by(OWNER, decode="raw")
    @deferred
    def set_sources(self, payload):
        obj = _parse(payload)
        self.storage["research_addr"] = str(obj.get("research_addr", ""))
        self.storage["trader_addr"] = str(obj.get("trader_addr", ""))
        self.storage["cached_research_stats"] = {}
        self.storage["cached_ideas"] = []
        self.storage["cached_pnl"] = {}
        self.storage["cached_portfolio"] = {}
        self._wake_webhook_drain()
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_webhook_secret(self, payload):
        token_hash = str(_parse(payload).get("secret_hash", "")).lower()
        try:
            valid = len(token_hash) == 64 and len(bytes.fromhex(token_hash)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("set_webhook_secret: expected a SHA-256 hex digest")
        self.storage[KEY_WEBHOOK_SECRET_HASH] = token_hash
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        raw = payload if isinstance(payload, (bytes, bytearray)) else b""
        if not raw:
            raise ValueError("set_routes: empty payload")
        self.storage.set_raw(ROUTES_KEY, bytes(raw))
        return b"ok"

    # ---- inbound from other actors ----------------------------------------- #

    def _enqueue(self, payload):
        """Queue one authorized outbound line for the webhook drain."""
        obj = _parse(payload)
        text = str(obj.get("text", "") or "")[:800]
        if not text:
            return b"empty"
        box = list(self.storage.get("outbox") or [])
        box.append({"text": text, "kind": str(obj.get("kind", "") or "")})
        self.storage["outbox"] = box[-int(self.storage["cfg"]["max_outbox"]) :]
        self._arm_webhook_drain()
        return b"queued"

    @callable_by(OWNER, decode="raw")
    @deferred
    def enqueue(self, payload):
        """Let the owner queue a line; actors use authenticated messages."""
        return self._enqueue(payload)

    @callable_by(OWNER, decode="raw")
    @deferred
    def clear_guidance(self, payload):
        n = len(self.storage.get("guidance") or [])
        self.storage["guidance"] = []
        self.storage["guidance_dirty"] = True
        runtime.emit_event("notify.guidance_cleared", {"dropped": n})
        self._arm_webhook_drain()
        return _json_bytes({"cleared": n})

    @public(decode="raw")
    def get_guidance(self, payload):
        return _json_bytes({"guidance": list(self.storage.get("guidance") or [])})

    @public(decode="raw")
    @deferred
    @post("/telegram/webhook")
    def handle_webhook(self, payload):
        update, headers = _webhook_update(payload)
        expected = str(self.storage.get(KEY_WEBHOOK_SECRET_HASH, ""))
        if not expected:
            if not self.storage.get(KEY_WEBHOOK_UNCONFIGURED_REPORTED):
                self.storage[KEY_WEBHOOK_UNCONFIGURED_REPORTED] = True
                runtime.emit_event("notify.webhook_unconfigured", {})
            return _http_resp(401, b'{"ok":false}')
        got = _header(headers, "x-telegram-bot-api-secret-token")
        if hashlib.sha256(got.encode("utf-8")).hexdigest() != expected:
            return _http_resp(401, b'{"ok":false}')
        update_id = update.get("update_id")
        seen = list(self.storage.get(KEY_SEEN_UPDATES) or [])
        if update_id is not None and update_id in seen:
            return _http_resp(200, b'{"ok":true,"dedup":true}')
        if update_id is not None:
            seen.append(update_id)
            self.storage[KEY_SEEN_UPDATES] = seen[-100:]
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text", "") or "").strip()
        if text and chat_id == str(self.storage.get("chat_id", "")):
            if text.startswith("/"):
                # Telegram can execute one Bot API method directly from the
                # webhook response.  Keep command evaluation in the Cowboy
                # actor/gateway path, but avoid a second timer + runner round
                # trip just to deliver its reply.  Autonomous notifications
                # and free-form guidance still use the CBSS-backed drain.
                return _http_resp(
                    200,
                    _json_bytes(
                        {
                            "method": "sendMessage",
                            "chat_id": chat_id,
                            "text": self._answer(text)[:4096],
                        }
                    ),
                )
            self._accept_text(text)
            self._arm_webhook_drain()
        return _http_resp(200, b'{"ok":true}')

    @public(decode="raw")
    @deferred
    def handle_message(self, payload):
        obj = _parse(payload)
        kind = str(obj.get("type", ""))
        if kind == "notification":
            if not _sender_matches(self.storage.get("research_addr", "")):
                return b"ignored: unknown research"
            return self._enqueue(payload)
        if kind == "command_snapshot":
            if not _sender_matches(self.storage.get("trader_addr", "")):
                return b"ignored: unknown trader"
            self.storage["cached_research_stats"] = dict(
                obj.get("research_stats") or {}
            )
            self.storage["cached_ideas"] = list(obj.get("ideas") or [])
            self.storage["cached_pnl"] = dict(obj.get("pnl") or {})
            self.storage["cached_portfolio"] = dict(obj.get("portfolio") or {})
            return b"stored"
        # Legacy webhook self-messages may still be in flight. External text
        # must enter through the authenticated webhook.
        if not _sender_matches(runtime.get_actor_address()):
            return b"ignored: unknown text sender"
        text = str(obj.get("text", "") or "").strip()
        result = self._accept_text(text)
        if text:
            self._arm_webhook_drain()
        return result

    def _accept_text(self, text):
        if not text:
            return b"empty"
        if text.startswith("/"):
            # The webhook drain timer funds the reply job.
            box = list(self.storage.get("outbox") or [])
            box.append({"text": self._answer(text), "kind": "reply"})
            self.storage["outbox"] = box[-int(self.storage["cfg"]["max_outbox"]) :]
            return b"queued"
        guidance = list(self.storage.get("guidance") or [])
        guidance.append({"text": text[:500], "block": int(runtime.get_block_height())})
        self.storage["guidance"] = guidance[-int(self.storage["cfg"]["max_guidance"]) :]
        self.storage["guidance_dirty"] = True
        runtime.emit_event("notify.webhook_guidance", {})
        return b"stored"

    @public(decode="raw")
    @get("/telegram/health")
    def webhook_health(self, payload):
        return _http_resp(200, b'{"ok":true}')

    # The PVM requires timeout_blocks to be an integer literal at lowering time.
    @runner.continuation(timeout_blocks=150)
    async def _notify_one(self, payload):
        ctx = capture()
        obj = _parse(payload)
        queue = list(obj.get("queue") or [])
        if len(queue) != 1:
            raise ValueError("_notify_one accepts exactly one queued message")
        ctx.chat = str(self.storage.get("chat_id", ""))
        ctx.cfg = self.storage.get("cfg", {})
        ctx.secrets = _secret_refs(ctx.cfg)
        ctx.msg = queue[0]
        ctx.brief = isinstance(ctx.msg, dict) and ctx.msg.get("kind") == "brief"
        text = ctx.msg.get("text", "") if isinstance(ctx.msg, dict) else str(ctx.msg)
        ctx.r = await runner.http(
            TG_API + "/sendMessage?chat_id=" + ctx.chat + "&text=" + tg_encode(text),
            method="GET",
            secrets=ctx.secrets,
            timeout_ms=8000,
        )
        return self._after_notify(ctx.brief)

    def _after_notify(self, brief):
        if brief:
            # delivering the morning brief closes the prior advice window:
            # replies after it are the new day's guidance
            dropped = len(self.storage.get("guidance") or [])
            self.storage["guidance"] = []
            self.storage["guidance_dirty"] = True
            if dropped:
                runtime.emit_event("notify.guidance_rotated", {"dropped": dropped})
        if brief:
            self._arm_webhook_drain()
        return _json_bytes({"status": "notified"})

    # ---- webhook delivery: one child per next-block fire ------------------- #

    def _wake_webhook_drain(self):
        if self.storage.get("outbox") or self.storage.get("guidance_dirty"):
            self._arm_webhook_drain()

    def _arm_webhook_drain(self):
        if self.storage.get("webhook_drain_pending"):
            return
        self.storage["webhook_drain_pending"] = True
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            _json_bytes({"_handler": "webhook_drain_tick", "_payload": {}}),
            access_limit=TIMER_ACCESS_LIMIT,
        )  # omitted fee_payer bills the actor

    @callable_by(SELF, decode="raw")
    @deferred
    def webhook_drain_tick(self, payload):
        if not self.storage.get("webhook_drain_pending"):
            return b"idle"
        self.storage["webhook_drain_pending"] = False
        outbox = list(self.storage.get("outbox") or [])
        if outbox:
            if not self.storage.get("chat_id"):
                return b"unconfigured"  # retain work until set_telegram wakes it
            self.storage["outbox"] = outbox[1:]
            if outbox[1:] or self.storage.get("guidance_dirty"):
                self._arm_webhook_drain()
            return self._notify_one(_json_bytes({"queue": outbox[:1]}))
        if self.storage.get("guidance_dirty"):
            self._push_guidance()
        return b"drained"

    # ---- command console ---------------------------------------------------- #

    def _answer(self, cmd):
        c = cmd.strip().lower().split("@")[0]
        if c == "/status":
            st = dict(self.storage.get("cached_research_stats") or {})
            ideas = list(self.storage.get("cached_ideas") or [])
            pnl = dict(self.storage.get("cached_pnl") or {})
            return (
                "status: theses %s | ideas %s | research window %s | "
                "book %s open, equity $%s, realized $%s"
                % (
                    st.get("total_theses", "?"),
                    len(ideas),
                    st.get("window", "?"),
                    pnl.get("open_positions", "?"),
                    pnl.get("equity", "?"),
                    pnl.get("realized_pnl", "?"),
                )
            )
        if c == "/book":
            pos = (
                dict(self.storage.get("cached_portfolio") or {}).get("positions") or []
            )
            if not pos:
                return "book: empty"
            return "book:\n" + "\n".join(
                "%s %s $%.2f upnl %+.2f"
                % (
                    p.get("asset"),
                    p.get("side"),
                    float(p.get("notional", 0) or 0),
                    float(p.get("upnl", 0) or 0),
                )
                for p in pos
            )
        if c == "/ideas":
            ideas = list(self.storage.get("cached_ideas") or [])
            if not ideas:
                return "ideas: none published yet"
            return "ideas:\n\n" + "\n\n".join(
                "%s %s (%s)\n%s"
                % (
                    i.get("asset"),
                    i.get("side"),
                    i.get("confidence"),
                    str(i.get("rationale", "")),
                )
                for i in ideas[:6]
            )
        return "commands: /status /book /ideas — anything else is advice"

    def _push_guidance(self):
        addr = str(self.storage.get("research_addr", ""))
        if not addr:
            return False
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "guidance_snapshot",
                        "guidance": list(self.storage.get("guidance") or []),
                    }
                ),
            )
            self.storage["guidance_dirty"] = False
            return True
        except Exception as e:
            runtime.emit_event("notify.research_send_failed", {"err": str(e)[:80]})
            return False


_ACTOR = None


def _get_actor():
    global _ACTOR
    if _ACTOR is None:
        _ACTOR = HlNotify()
    return _ACTOR


def init(payload):
    return _get_actor().init(payload)


def set_telegram(payload):
    return _get_actor().set_telegram(payload)


def set_sources(payload):
    return _get_actor().set_sources(payload)


def set_webhook_secret(payload):
    return _get_actor().set_webhook_secret(payload)


def set_routes(payload):
    return _get_actor().set_routes(payload)


def enqueue(payload):
    return _get_actor().enqueue(payload)


def clear_guidance(payload):
    return _get_actor().clear_guidance(payload)


def get_guidance(payload):
    return _get_actor().get_guidance(payload)


def handle_webhook(payload):
    return _get_actor().handle_webhook(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def webhook_health(payload):
    return _get_actor().webhook_health(payload)


def webhook_drain_tick(payload):
    return _get_actor().webhook_drain_tick(payload)


def _notify_one__resume(payload):
    return _get_actor()._notify_one__resume(payload)
