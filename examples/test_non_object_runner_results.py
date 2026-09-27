"""Non-object runner results must never wedge a single-flight lock.

A `RunnerResult` whose `.raw` is not a dict is a TRANSIENT condition — an HTML
error page from Slack/Telegram/Twilio, a truncated body, an agent that returned
bare text. The actors here take a single-flight lock (`KEY_PENDING[thread_key]`,
or the upstream actor's equivalent) BEFORE the first await and clear it only on
a terminal path. None of them has a timeout sweeper, so raising between those
two points strands the lock and every later message on that thread is dropped
as "already pending" — one bad HTTP body silently kills a conversation forever.

These tests drive the REAL continuations with a minimal in-process cowboy_sdk
stub whose `runner.http` / `runner.agent` / `runner.llm` are async and pop from
a queued list of `RunnerResult`s, so `await` runs the actual actor body. The
PVM's FSM splits a continuation at each await and resets plain locals; asyncio
keeps them, which is the one divergence — it can only mask a local-vs-`ctx`
bug, never a lock-safety one.

Run: python3 examples/test_non_object_runner_results.py (or under pytest)
"""

import asyncio
import importlib.util
import json
import pathlib
import pickle
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent

# ─── source-of-truth RunnerResult (mirrors node pvm/Lib/cowboy_sdk/runner.py) ──
_TEXT_KEYS = ("content", "text", "response", "data", "answer")


def extract_text(result) -> str:
    for _ in range(8):  # bounded: guard against degenerate/cyclic nesting
        if not isinstance(result, dict):
            break
        for key in _TEXT_KEYS:
            if key in result:
                result = result[key]
                break
        else:
            break
    if result is None:
        return ""
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", "replace")
    if isinstance(result, str):
        return result
    return str(result)


class RunnerResult:
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    @property
    def data(self):
        return self.raw

    @property
    def text(self) -> str:
        return extract_text(self.raw)

    def json(self):
        return json.loads(self.text)


# ─── in-process cowboy_sdk stub ───────────────────────────────────────────────
EVENTS = []      # [(name, payload)]
SENDS = []       # [(target_bytes, payload)]
CALLS = []       # [(target_bytes, method, args)]
OPS = []         # [(kind, kwargs)] — every runner op the actors issued
RESULTS = []     # queued RunnerResults, popped by each runner op


def reset(*queued):
    for sink in (EVENTS, SENDS, CALLS, OPS, RESULTS):
        sink.clear()
    RESULTS.extend(queued)


def events(name):
    return [payload for n, payload in EVENTS if n == name]


def _install_fake_sdk():
    _ident = lambda f=None, *a, **k: (f if callable(f) else _ident)  # noqa: E731

    def _op(kind):
        async def run(*a, **k):
            OPS.append((kind, k))
            if not RESULTS:
                raise AssertionError(f"runner.{kind} called with an empty result queue")
            return RESULTS.pop(0)
        return run

    class _Address:
        def __init__(self, value):
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).hex()
            self._hex = str(value or "").lower().replace("0x", "")

        def to_hex(self, checksum=False):
            return "0x" + self._hex

    class _Codec:
        """CBOR stand-in: round-trips dicts AND bytes within the test."""
        @staticmethod
        def encode(v):
            return pickle.dumps(v)

        @staticmethod
        def decode(b):
            return pickle.loads(bytes(b))

    codec = _Codec()

    sdk = types.ModuleType("cowboy_sdk")
    sdk.actor = _ident
    sdk.pure = _ident
    sdk.deferred = _ident
    sdk.public = lambda fn=None, *, decode="raw": (fn if fn is not None else _ident)
    sdk.callable_by = lambda *a, **k: _ident
    sdk.OWNER = "OWNER"
    sdk.capture = lambda: types.SimpleNamespace()
    sdk.Address = _Address
    sdk.codec = codec
    sdk.send = lambda target, payload=None, **k: SENDS.append((bytes(target), payload))
    sdk.call = lambda target, method, args=None, **k: CALLS.append(
        (bytes(target), method, args))

    runner = types.ModuleType("cowboy_sdk.runner")
    runner.continuation = _ident
    runner.RunnerResult = RunnerResult
    runner.extract_text = extract_text
    runner.http = _op("http")
    runner.agent = _op("agent")
    runner.llm = _op("llm")
    runner.mcp = _op("mcp")
    runner.container = _op("container")
    runner.handle_runner_result = lambda *a, **k: None
    sdk.runner = runner

    runtime = types.ModuleType("cowboy_sdk.runtime")
    def emit_event(name, payload=None, *, schema=None):
        EVENTS.append((name, payload))
        return {"emit_id": len(EVENTS), "sync_outcomes": [], "deferred_subs": []}

    runtime.emit_event = emit_event
    runtime.get_sender = lambda: b"\xEE" * 20
    runtime.get_actor_address = lambda: b"\x11" * 20
    runtime.assume_ownership_if_unowned = lambda: None
    runtime.get_timestamp_ms = lambda: 0     # 0 ⇒ _ts_is_fresh short-circuits True
    runtime.get_block_height = lambda: 1
    runtime.context = lambda: {}
    # Funded self-timer that launches each runner op (D36 fix, COW-3894). Mirrors
    # the devnet SDK signature LITERALLY — node origin/devnet 22e325b9
    # pvm/Lib/cowboy_sdk/runtime.py:809-816 (devnet is the deploy target; bronco's
    # frozen pre-meter SDK lacks access_limit — COW-3899 skew, not a chain fact).
    # These non-object cases drive the continuations directly, so it is a pure
    # no-op here (it must not pollute SENDS/CALLS, which some cases assert empty).
    runtime.schedule_timer_ex = (
        lambda height, payload, fee_payer=None, gas_limit=None,
        access_limit=None, expires_at=None: b"timer-id"
    )
    sdk.runtime = runtime

    routes = types.ModuleType("cowboy_sdk.routes")
    routes.get = lambda *a, **k: _ident
    routes.post = lambda *a, **k: _ident

    sys.modules["cowboy_sdk"] = sdk
    sys.modules["cowboy_sdk.runner"] = runner
    sys.modules["cowboy_sdk.runtime"] = runtime
    sys.modules["cowboy_sdk.routes"] = routes
    sys.modules["cowboy_sdk.codec"] = codec


def _load(relpath, name):
    # Reinstall right before every load — an actor module binds whichever stub
    # is live when it executes, and sibling test files install their own at
    # import time. Loading against theirs swaps the codec and the event sink.
    _install_fake_sdk()
    spec = importlib.util.spec_from_file_location(name, HERE / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockStorage(dict):
    def set_raw(self, key, value):
        self[key] = value


def _actor(module, cls_name):
    inst = getattr(module, cls_name).__new__(getattr(module, cls_name))
    inst.storage = MockStorage()
    return inst


def cbor(value):
    return pickle.dumps(value)


def uncbor(value):
    return pickle.loads(bytes(value))


# At import time, not in main(), so pytest collects zero-arg test functions.
C = _load("gallery/cowbot/cowbot.py", "ex_cowbot")
R = _load("gallery/ranchhand/ranchhand.py", "ex_ranchhand")
S = _load("gallery/ranchhand/proxy/slackrelay.py", "ex_slackrelay")
TG = _load("34-simple-telegram/telegram.py", "ex_telegram")
TW = _load("gallery/twilio/twilio.py", "ex_twilio")
P = _load("gallery/twilio/proxy/twiliorelay.py", "ex_twiliorelay")
D = _load("core/11-failure-recovery/recovery_demo.py", "ex_recovery_demo")
# ai_brain_actor does a plain `import empire_engine`, so its directory has to be
# on the path before the load.
sys.path.insert(0, str(HERE / "31-frontier"))
AB = _load("31-frontier/ai_brain_actor.py", "ex_ai_brain")


# ─── cowbot ───────────────────────────────────────────────────────────────────
def _cowbot_actor():
    a = _actor(C, "CowbotActor")
    a.storage[C.KEY_OWNER] = "0x" + "ee" * 20
    a.storage[C.KEY_KNOWLEDGE_VOLUME] = "kb"
    a.storage[C.KEY_STATE_VOLUME] = "state"
    a.storage[C.KEY_PENDING] = {}
    a.storage[C.KEY_LAST_REQUEST_ID] = 0
    a.storage[C.KEY_REQUESTS] = []
    return a


def _slack_http_envelope(text="<@B1> hi", ts="1.0"):
    # cowbot's slack_http IS the @runner.continuation and owns POST
    # /slack/events, so it is driven with a CIP-14 HttpRequestEnvelope (not a
    # self-dispatch envelope — the self-send hop is gone, COW-3878). The header
    # `x-slack-request-timestamp` ("1") is what the runner's MAC signs; the
    # event `ts` (ts, default "1.0") is the message id used for thread_ts only.
    body = json.dumps({
        "team_id": "T1",
        "authorizations": [{"user_id": "B1"}],
        "event": {"type": "app_mention", "channel": "C1", "ts": ts,
                  "user": "U1", "text": text},
    }).encode("utf-8")
    return cbor({
        "method": "POST", "path": "/slack/events",
        "headers": [["x-slack-request-timestamp", "1"],
                    ["x-slack-signature", "v0=deadbeef"]],
        "body": body,
    })


def test_cowbot_first_http_non_object_frees_the_thread():
    a = _cowbot_actor()
    thread_key = C._thread_key("C1", "1.0")

    reset(RunnerResult("<html>502 Bad Gateway</html>"))
    asyncio.run(a.slack_http(_slack_http_envelope()))

    assert a.storage[C.KEY_PENDING] == {}, a.storage[C.KEY_PENDING]
    assert a.storage[C.KEY_REQUESTS][-1]["status"] == "http_error"
    assert events("cowbot.slack_http_error"), EVENTS
    assert len(OPS) == 1, "must not reach runner.agent after a failed gate"

    # …and the next message on the SAME thread is accepted end to end.
    reset(RunnerResult({"ok": True, "ts": "1.5"}),
          RunnerResult({"answer": "howdy"}),
          RunnerResult({"ok": True}))
    asyncio.run(a.slack_http(_slack_http_envelope(text="<@B1> again")))
    assert a.storage[C.KEY_LAST_REQUEST_ID] == 2, "follow-up was dropped as pending"
    assert a.storage[C.KEY_REQUESTS][-1]["status"] == "answered"
    assert a.storage[C.KEY_PENDING] == {}
    assert thread_key not in a.storage[C.KEY_PENDING]
    print("ok test_cowbot_first_http_non_object_frees_the_thread")


def test_cowbot_agent_non_object_falls_back_and_frees_the_thread():
    a = _cowbot_actor()
    reset(RunnerResult({"ok": True, "ts": "1.5"}),
          RunnerResult("bare text answer"),
          RunnerResult({"ok": True}))
    asyncio.run(a.slack_http(_slack_http_envelope()))

    assert a.storage[C.KEY_PENDING] == {}, a.storage[C.KEY_PENDING]
    assert a.storage[C.KEY_REQUESTS][-1]["status"] == "answered"
    assert events("cowbot.agent_result_invalid"), EVENTS
    # The salvaged text is what got posted (RunnerResult.text passes a bare
    # string through), so the user sees a reply rather than a stuck placeholder.
    posted = json.loads(OPS[-1][1]["body"].decode("utf-8"))
    assert posted["text"] == "bare text answer", posted
    print("ok test_cowbot_agent_non_object_falls_back_and_frees_the_thread")


def test_cowbot_final_post_non_object_is_bookkeeping_only():
    a = _cowbot_actor()
    reset(RunnerResult({"ok": True, "ts": "1.5"}),
          RunnerResult({"answer": "howdy"}),
          RunnerResult("<html>500</html>"))
    out = uncbor(asyncio.run(a.slack_http(_slack_http_envelope())))

    assert out["request_id"] == 1, out
    assert a.storage[C.KEY_REQUESTS][-1]["status"] == "answered"
    assert a.storage[C.KEY_PENDING] == {}
    assert events("cowbot.slack_post_error") == [
        {"request_id": 1, "error": "non_object_result"}], EVENTS
    assert len(OPS) == 3, "a failed post must not be retried"
    print("ok test_cowbot_final_post_non_object_is_bookkeeping_only")


def test_cowbot_history_is_stamped_by_id_not_by_list_position():
    """A request that started first can finish last, so its history record is
    not the tail of the list."""
    a = _cowbot_actor()
    # Thread TA's request opened first; TB's landed while TA was awaiting.
    a.storage[C.KEY_PENDING] = {"TA": 1, "TB": 2}
    a.storage[C.KEY_REQUESTS] = [
        {"request_id": 1, "thread_key": "TA", "status": "pending"},
        {"request_id": 2, "thread_key": "TB", "status": "pending"},
    ]

    reset()
    a._fail_request(1, "TA", "http_error", "cowbot.slack_http_error")
    a._update_request(2, status="answered", answer_len=5)   # the success path

    records = {r["request_id"]: r for r in a.storage[C.KEY_REQUESTS]}
    assert records[1]["status"] == "http_error", records
    assert records[2] == {"request_id": 2, "thread_key": "TB",
                          "status": "answered", "answer_len": 5}, records
    assert a.storage[C.KEY_PENDING] == {"TB": 2}, "TA's lock outlived its request"
    print("ok test_cowbot_history_is_stamped_by_id_not_by_list_position")


# ─── ranchhand + slackrelay ───────────────────────────────────────────────────
PROXY_HEX = "ab" * 20


def _ranchhand_actor():
    a = _actor(R, "RanchhandActor")
    a.storage[R.KEY_OWNER] = "ee" * 20
    a.storage[R.KEY_TENANTS] = {"T1": {
        "hire_id": 1, "artifact_id": 0, "proxy": PROXY_HEX,
        "state_volume": "sv", "hirer": "ee" * 20, "status": "active",
    }}
    a.storage[R.KEY_DECLARATION] = {"declaration_id": "0xabc", "version": 1, "artifact_id": 0}
    a.storage[R.KEY_MOUNT_VOLUMES] = False
    a.storage[R.KEY_PENDING] = {}
    a.storage[R.KEY_LAST_REQUEST_ID] = 0
    a.storage[R.KEY_REQUESTS] = []
    a.storage[R.KEY_SEEN] = []
    a.storage[R.KEY_DISPATCH_COUNT] = 0
    return a


def _slack_envelope(text="<@B1> hi", ts="1.0"):
    body = json.dumps({
        "team_id": "T1",
        "authorizations": [{"user_id": "B1"}],
        "event": {"type": "app_mention", "channel": "C1", "ts": ts,
                  "user": "U1", "text": text},
    }).encode("utf-8")
    return cbor({
        "method": "POST", "path": "/slack/events",
        "headers": [["x-slack-request-timestamp", "1"],
                    ["x-slack-signature", "v0=deadbeef"]],
        "body": body,
    })


def test_ranchhand_agent_non_object_frees_the_thread():
    a = _ranchhand_actor()
    thread_key = R._thread_key("T1", "C1", "1.0")

    reset()
    a.slack_http(_slack_envelope())
    assert a.storage[R.KEY_PENDING] == {thread_key: 1}, a.storage[R.KEY_PENDING]

    reset(RunnerResult("bare text, not an object"))
    out = uncbor(asyncio.run(a.tenant_answer({"request_id": 1, "team_id": "T1"})))

    assert out["status"] == "agent_error", out
    assert a.storage[R.KEY_PENDING] == {}, a.storage[R.KEY_PENDING]
    assert a.storage[R.KEY_REQUESTS][-1]["status"] == "agent_error"
    assert events("ranchhand.agent_error"), EVENTS
    assert not SENDS, "no answer should be relayed for a failed agent job"

    # The thread accepts the next Slack event (distinct body ⇒ not a replay).
    a.slack_http(_slack_envelope(text="<@B1> again"))
    assert a.storage[R.KEY_LAST_REQUEST_ID] == 2, "follow-up was dropped as pending"
    assert a.storage[R.KEY_PENDING] == {thread_key: 2}
    print("ok test_ranchhand_agent_non_object_frees_the_thread")


def _slackrelay_actor():
    a = _actor(S, "SlackRelayActor")
    a.storage[S.KEY_OWNER] = "ee" * 20
    a.storage[S.KEY_RANCHHAND] = "cd" * 20
    a.storage[S.KEY_API_BASE] = S.DEFAULT_API_BASE
    a.storage[S.KEY_SIGNING_SECRET_NAME] = S.DEFAULT_SIGNING_SECRET_NAME
    a.storage[S.KEY_BOT_TOKEN_NAME] = S.DEFAULT_BOT_TOKEN_NAME
    a.storage[S.KEY_POSTS] = []
    return a


def _relay_msg():
    return {
        "kind": S.MSG_RELAY_EVENT, "request_id": 1, "team_id": "T1",
        "channel_id": "C1", "thread_ts": "1.0", "raw_body": b"{}",
        "ts": "1", "sig": "v0=deadbeef",
    }


def test_slackrelay_verify_non_object_rejects_instead_of_raising():
    a = _slackrelay_actor()
    reset(RunnerResult("<html>502 Bad Gateway</html>"))
    asyncio.run(a.verify_and_ack(_relay_msg()))

    assert events("slackrelay.http_error"), EVENTS
    assert len(SENDS) == 1, SENDS
    target, payload = SENDS[0]
    assert target == bytes.fromhex("cd" * 20)
    reply = uncbor(payload)
    # Ranchhand releases its pending lock ONLY on one of these replies.
    assert reply["kind"] == S.MSG_TENANT_REJECTED, reply
    assert reply["request_id"] == 1 and reply["team_id"] == "T1", reply
    print("ok test_slackrelay_verify_non_object_rejects_instead_of_raising")


def test_slackrelay_post_non_object_is_bookkeeping_only():
    a = _slackrelay_actor()
    reset(RunnerResult("<html>500</html>"))
    out = uncbor(asyncio.run(a.post_answer({
        "kind": S.MSG_POST_ANSWER, "request_id": 7, "channel_id": "C1",
        "thread_ts": "1.0", "placeholder_ts": "1.5", "text": "howdy",
    })))

    assert out == {"request_id": 7, "status": "post_error"}, out
    assert a.storage[S.KEY_POSTS] == [{"request_id": 7, "status": "post_error"}]
    assert events("slackrelay.post_error") == [
        {"request_id": 7, "error": "non_object_result"}], EVENTS
    assert len(OPS) == 1, "a failed post must not be retried"
    print("ok test_slackrelay_post_non_object_is_bookkeeping_only")


# ─── the ranchhand ↔ slackrelay round trip, end to end ────────────────────────
def test_relay_rejection_actually_unblocks_ranchhand():
    """The two halves wired together: a non-object result inside the proxy must
    make the NEXT Slack event on that thread reach ranchhand's agent."""
    rh = _ranchhand_actor()
    rh.storage[R.KEY_TENANTS]["T1"]["proxy"] = PROXY_HEX
    relay = _slackrelay_actor()
    relay.storage[S.KEY_RANCHHAND] = "cd" * 20
    thread_key = R._thread_key("T1", "C1", "1.0")

    reset()
    rh.slack_http(_slack_envelope())
    assert rh.storage[R.KEY_PENDING] == {thread_key: 1}

    reset(RunnerResult("<html>502</html>"))
    asyncio.run(relay.verify_and_ack(_relay_msg()))
    rejection = uncbor(SENDS[0][1])

    rh.handle_message(cbor(rejection))
    assert rh.storage[R.KEY_PENDING] == {}, rh.storage[R.KEY_PENDING]
    assert rh.storage[R.KEY_REQUESTS][-1]["status"] == "rejected"

    reset()
    rh.slack_http(_slack_envelope(text="<@B1> again"))
    assert rh.storage[R.KEY_LAST_REQUEST_ID] == 2, "thread stayed wedged"
    print("ok test_relay_rejection_actually_unblocks_ranchhand")


# ─── telegram ─────────────────────────────────────────────────────────────────
def test_telegram_non_object_send_result_is_recorded_not_raised():
    a = _actor(TG, "TelegramBridge")
    reset()
    out = uncbor(a._record_sent(4242, "howdy", RunnerResult("<html>502</html>")))

    assert out == {"ok": False, "chat_id": 4242, "status": "unknown"}, out
    record = a.storage[TG.KEY_LAST_SENT]
    assert record["status"] == "unknown" and record["ok"] is False, record
    assert record["chat_id"] == 4242 and record["text"] == "howdy"
    assert events("telegram.send_result_unknown"), EVENTS
    assert not OPS and not SENDS, "bookkeeping must not resend the message"
    print("ok test_telegram_non_object_send_result_is_recorded_not_raised")


# ─── twiliorelay ──────────────────────────────────────────────────────────────
def _twiliorelay_actor():
    a = _actor(P, "TwilioRelayActor")
    a.storage[P.KEY_OWNER] = "ee" * 20
    a.storage[P.KEY_TWILIO] = "cd" * 20
    a.storage[P.KEY_API_BASE] = P.DEFAULT_API_BASE
    a.storage[P.KEY_ACCOUNT_SID] = "ACx"
    a.storage[P.KEY_OUTBOUND_FROM_NUMBER] = "+15550000000"
    a.storage[P.KEY_AUTH_TOKEN_NAME] = P.DEFAULT_AUTH_TOKEN_NAME
    a.storage[P.KEY_API_BASIC_NAME] = P.DEFAULT_API_BASIC_NAME
    a.storage[P.KEY_VERIFIED] = []
    a.storage[P.KEY_POSTS] = []
    return a


def test_twiliorelay_send_non_object_records_send_error():
    a = _twiliorelay_actor()
    reset(RunnerResult("<html>500</html>"))
    out = uncbor(asyncio.run(a.send_sms({
        "request_id": 9, "to_number": "+15551230000",
        "from_number": "+15550000000", "text": "howdy",
    })))

    assert out == {"request_id": 9, "status": "send_error"}, out
    assert a.storage[P.KEY_POSTS] == [{"request_id": 9, "status": "send_error"}]
    assert events("twiliorelay.send_error") == [
        {"request_id": 9, "error": "non_object_result"}], EVENTS
    assert len(OPS) == 1, "a failed send must not be retried"
    print("ok test_twiliorelay_send_non_object_records_send_error")


def _twilio_sms_envelope(to, frm, sid, body="hi"):
    from urllib.parse import quote
    parts = [("AccountSid", "ACx"), ("MessageSid", sid), ("From", frm),
             ("To", to), ("Body", body), ("NumMedia", "0")]
    form = "&".join(f"{k}={quote(v)}" for k, v in parts).encode("utf-8")
    return cbor({
        "method": "POST", "path": "/twilio/sms",
        "headers": [["x-twilio-signature", "sig" + sid]], "body": form,
    })


def test_twilio_agent_non_object_frees_the_conversation():
    a = _actor(TW, "TwilioActor")
    a.init(None)
    a.storage[TW.KEY_WEBHOOK_URL] = "http://gw/twilio/sms"
    a.storage[TW.KEY_DECLARATION] = {"declaration_id": "0xabc", "version": 1, "artifact_id": 0}
    a.storage[TW.KEY_MOUNT_VOLUMES] = False
    number, correspondent = "+15551110000", "+15559990000"
    a._put_tenant(number, {
        "hire_id": 1, "artifact_id": 0, "proxy": "ab" * 20,
        "state_volume": "sv", "hirer": "ee" * 20, "status": "active",
    })
    conversation = TW._conversation_key(number, correspondent)

    reset()
    a.twilio_http(_twilio_sms_envelope(number, correspondent, "SM1"))
    assert a._pending_get(conversation) == 1, "inbound SMS did not take the slot"

    reset(RunnerResult("bare text, not an object"))
    out = uncbor(asyncio.run(a.tenant_answer(
        {"request_id": 1, "twilio_number": number})))

    assert out["status"] == "agent_error", out
    assert a._pending_get(conversation) is None, "conversation slot stayed held"
    assert a._request_by_id(1) is None, "inflight accounting was not released"
    assert events("twilio.agent_error"), EVENTS
    assert not CALLS, "no answer should be sent for a failed agent job"

    # The next SMS from the same correspondent is accepted.
    a.twilio_http(_twilio_sms_envelope(number, correspondent, "SM2", "again"))
    assert a._pending_get(conversation) == 2, "conversation stayed wedged"
    print("ok test_twilio_agent_non_object_frees_the_conversation")


def test_twiliorelay_gate_non_object_reports_rejection():
    a = _twiliorelay_actor()
    reset(RunnerResult("<html>502</html>"))
    asyncio.run(a.verify_and_ack({
        "request_id": 3, "twilio_number": "+15550000000",
        "preimage": b"https://gw/twilio/smsBodyhi", "sig": "sig",
    }))

    # twilio.py frees its per-conversation slot on this callback.
    assert CALLS and CALLS[0][1] == "tenant_rejected", CALLS
    assert CALLS[0][2]["request_id"] == 3
    assert a.storage[P.KEY_VERIFIED] == [], "an unverified gate must not mark verified"
    assert events("twiliorelay.precondition_failed") == [
        {"request_id": 3, "reason": "non_object_result"}], EVENTS
    print("ok test_twiliorelay_gate_non_object_reports_rejection")


# ─── frontier ai_brain ────────────────────────────────────────────────────────
def _brain_snapshot():
    """The smallest side snapshot `decide_llm`'s pre-await segment accepts."""
    return cbor({"turn": 1, "max_turns": 50, "height": 2, "width": 2,
                 "terrain": "...."})


def _brain_actor():
    a = _actor(AB, "AIBrain")
    a.storage["side"] = 2
    # What handle_timer stamps before submitting the job — the in-flight
    # semaphore it then skips on for STALE_PENDING_BLOCKS.
    a.storage["pending_since_block"] = 1
    return a


def test_ai_brain_non_object_llm_body_rearms_the_next_replan():
    a = _brain_actor()
    reset(RunnerResult("<html>502 Bad Gateway</html>"))
    assert asyncio.run(a.decide_llm(_brain_snapshot())) == b""

    assert a.storage["pending_since_block"] == 0, \
        "brain stays 'busy' until the watchdog expires — replans are silenced"
    assert events("empire.ai_llm_result_invalid") == [
        {"side": 2, "turn": 1, "error": "non_object_result"}], EVENTS
    assert not CALLS, "no orders may be issued from an unparsed response"
    print("ok test_ai_brain_non_object_llm_body_rearms_the_next_replan")


# Decoded JSON objects — the top-level type check passes — with no retrievable
# completion. Each must reach the lost-turn path rather than raise partway down
# the walk, which strands the semaphore exactly like a non-dict body does.
MALFORMED_LLM_BODIES = [
    {"choices": 1},                                   # not a list
    {"choices": ["bad"]},                             # entry is not an object
    {"choices": [{"message": "bad"}]},                # message is not an object
    {"choices": [{"finish_reason": "length"}]},       # no message at all
    {"choices": []},                                  # no completion
    {"error": {"message": "context length exceeded"}},
]


def test_ai_brain_malformed_llm_envelope_rearms_the_next_replan():
    for body in MALFORMED_LLM_BODIES:
        a = _brain_actor()
        reset(RunnerResult(body))
        assert asyncio.run(a.decide_llm(_brain_snapshot())) == b"", body
        assert a.storage["pending_since_block"] == 0, body
        assert events("empire.ai_llm_result_invalid") == [
            {"side": 2, "turn": 1, "error": "non_object_result"}], body
        assert not CALLS, body
    print("ok test_ai_brain_malformed_llm_envelope_rearms_the_next_replan")


def test_ai_brain_empty_completion_is_not_a_malformed_envelope():
    """A message object with no text is a well-formed answer the model just
    left blank — it takes the normal path, not the lost-turn one."""
    a = _brain_actor()
    reset(RunnerResult({"choices": [{"message": {"content": ""}}]}))
    assert asyncio.run(a.decide_llm(_brain_snapshot())) == b""

    assert a.storage["pending_since_block"] == 0
    assert not events("empire.ai_llm_result_invalid"), EVENTS
    assert events("empire.ai_llm_result") == [{
        "side": 2, "turn": 1, "orders": 0, "standing_orders": 0,
        "delivered": 0, "production_delivered": 0, "reports_consumed": 0,
        "raw_chars": 0}], EVENTS
    print("ok test_ai_brain_empty_completion_is_not_a_malformed_envelope")


# ─── recovery demo ────────────────────────────────────────────────────────────
def test_recovery_demo_non_object_lands_in_failed():
    a = _actor(D, "RecoveryDemo")
    a.storage["next_request_id"] = 1
    a.storage["requests"] = {}
    request = a._create_request({"source_url": "https://example.com/health"})

    reset()
    out = uncbor(a._finalize_request(request, RunnerResult("<html>503</html>")))
    assert out["status"] == "failed", out
    assert out["result"]["error"] == "NON_OBJECT_RESULT", out
    assert events("recovery_demo.failed"), EVENTS

    # `failed` is the state retry_request() exists to escape — still reachable.
    reset(RunnerResult({"status": "ok"}))
    retried = uncbor(asyncio.run(_coro(a.retry_request({"request_id": request["request_id"]}))))
    assert retried["status"] == "recovered", retried
    assert retried["attempt"] == 2, retried
    print("ok test_recovery_demo_non_object_lands_in_failed")


async def _coro(value):
    """retry_request returns the _run_request coroutine (continuations are
    async under this stub); await it to completion."""
    return await value


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\nall {len(tests)} non-object runner-result tests passed")


if __name__ == "__main__":
    main()
