"""Telegram chain semantics: long-poll legs, fresh-scan commands, /track,
gen fencing, scan_due handoff, cross-account refs.

Run: python3 tests/test_telegram_chain.py
"""
import json

from _bootstrap import load_actor, mock

from cowboy_sdk import codec

pw = load_actor()

CHAT = "123456789"
ADMIN = "777"


_tx = [0]


def fresh_tx():
    # cids derive from hash(tx_hash + sender + actor + method) with no counter,
    # so distinct legs of the same continuation MUST come from distinct txs —
    # true on-chain, but the mock's tx_hash is constant unless we bump it.
    _tx[0] += 1
    mock.set_context(tx_hash=_tx[0].to_bytes(32, "big"))


def call(fn, obj=None):
    fresh_tx()
    return json.loads(fn(json.dumps(obj or {}).encode("utf-8")) or b"{}")


def job_count():
    return len(mock.get_send_log())


def last_job():
    msg = codec.decode(mock.get_send_log()[-1]["payload"])
    assert msg["kind"] == "runner_job" and msg["job_type"] == "http", msg
    return msg


def resume(fn, cid, result):
    fresh_tx()  # each runner callback is its own tx on-chain
    # runner replies are canonical CBOR wire bytes (see runner._decode_reply)
    return fn(codec.encode({"cid": cid, "result": result}))


def storage():
    return pw._get_actor().storage


# --- init / track ------------------------------------------------------------ #
r = call(pw.init, {"interval_blocks": 100, "autostart": True})  # opt-in scanning
assert r["status"] == "initialized", r
r = call(pw.track, {"url": "https://shop.example/widget", "name": "widget",
                    "chat_id": CHAT})
assert r["status"] == "tracked" and r["watch_id"] == "1", r

# --- set_telegram starts the chain immediately ------------------------------- #
fresh_tx()
pw.set_telegram(json.dumps({"chat_id": ADMIN}).encode())
assert storage().get("tg_gen") == 1 and storage().get("tg_running")
assert len(mock.get_timers()) == 1, mock.get_timers()  # daily_tick
job = last_job()
kw = job["payload"]["kwargs"]
assert "/getUpdates?offset=0&timeout=25" in kw["url"], kw["url"]
assert "${TELEGRAM_BOT_TOKEN}" in kw["url"], kw["url"]
# cross-account ref: [owner, key] pair, NOT a bare string (a bare key resolves
# to the actor's own account in the node's parse_secrets and 404s the fetch)
OWNER_HEX = "0x" + "22" * 20  # mock sender/owner
assert kw["secrets"] == [[OWNER_HEX, "TELEGRAM_BOT_TOKEN"]], kw["secrets"]
leg1_cid = job["cid"]

# --- leg lands with commands: replies first, then re-arm ---------------------- #
result = {"ok": True, "result": [
    {"update_id": 41, "message": {"chat": {"id": int(CHAT)}, "text": "/watches"}},
    {"update_id": 42, "message": {"chat": {"id": int(ADMIN)}, "text": "/prices"}},
    {"update_id": 43, "message": {"chat": {"id": 555}, "text": "not a command"}},
]}
resume(pw._tg_poll__resume, leg1_cid, result)
assert storage().get("tg_offset") == 44, storage().get("tg_offset")

# /prices answers from the CACHE — no scan dispatched; the notify chain
# starts immediately with the arm-time WELCOME to the admin chat
assert storage().get("scan_pending") is not True
job = last_job()
body = json.loads(job["payload"]["kwargs"]["body"])
assert body["chat_id"] == ADMIN and "COMMANDS" in body["text"], body
resume(pw._notify_one__resume, job["cid"], {"ok": True})

job = last_job()  # then the first reply leg: /watches to the watch's chat
body = json.loads(job["payload"]["kwargs"]["body"])
assert job["payload"]["kwargs"]["url"].endswith("/sendMessage")
assert body["chat_id"] == CHAT and "widget" in body["text"], body
assert "https://shop.example/widget" in body["text"], body  # url shown per watch

resume(pw._notify_one__resume, job["cid"], {"ok": True})
job = last_job()  # second reply leg: admin /prices from the (empty) cache
body = json.loads(job["payload"]["kwargs"]["body"])
assert body["chat_id"] == ADMIN and "no prices yet" in body["text"], body

n = job_count()
resume(pw._notify_one__resume, job["cid"], {"ok": True})
assert job_count() == n + 1, "drained reply chain must re-arm the poll"
job = last_job()
assert "/getUpdates?offset=44&timeout=25" in job["payload"]["kwargs"]["url"]
leg2_cid = job["cid"]

# --- idle leg (no updates) re-arms directly ----------------------------------- #
n = job_count()
resume(pw._tg_poll__resume, leg2_cid, {"ok": True, "result": []})
assert job_count() == n + 1, "idle leg must re-arm"
leg3_cid = last_job()["cid"]

# --- gen fencing: a superseding set_telegram bumps the gen, so the earlier
# in-flight leg lands stale and must NOT re-arm -------------------------------- #
fresh_tx()
pw.set_telegram(json.dumps({}).encode())          # gen 2; a fresh poll leg dispatches
assert storage().get("tg_gen") == 2
n = job_count()
r = json.loads(resume(pw._tg_poll__resume, leg3_cid, {"ok": True, "result": []}))
assert r["status"] == "leg_dropped" and job_count() == n, r  # stale gen-1 leg dropped
assert storage().get("tg_offset") == 44

# --- /track and /untrack: mutations inside the poll resume -------------------- #
leg6_cid = last_job()["cid"]
resume(pw._tg_poll__resume, leg6_cid, {"ok": True, "result": [
    {"update_id": 50, "message": {"chat": {"id": int(CHAT)},
                                  "text": "/track https://api.x.dev/spot data.amount"}}]})
ws = json.loads(pw.get_watches(b"{}") or b"{}")["watches"]
assert "2" in ws and ws["2"]["owner"] == "tg:" + CHAT and ws["2"]["mode"] == "manual", ws
# with watches present, the welcome gains a "already tracking" footer
assert "already tracking 2 watch(es)" in pw._get_actor()._tg_answer(CHAT, "/help")

job = last_job()  # baseline scan: ONLY the just-tracked url
assert (job["payload"]["args"] or [""])[0] == "https://api.x.dev/spot", job["payload"]
resume(pw._scan_one__resume, job["cid"], {"data": {"amount": 12.5}})

job = last_job()  # reply scoped to the NEW watch, not the whole board
body = json.loads(job["payload"]["kwargs"]["body"])
assert body["chat_id"] == CHAT and "tracked" in body["text"] and "12.5" in body["text"], body
assert "widget" not in body["text"], body  # no full-board dump
n = job_count()
resume(pw._notify_one__resume, job["cid"], {"ok": True})
assert job_count() == n + 1, "re-arm after track reply"

leg7_cid = last_job()["cid"]
resume(pw._tg_poll__resume, leg7_cid, {"ok": True, "result": [
    {"update_id": 51, "message": {"chat": {"id": int(CHAT)}, "text": "/untrack 2"}}]})
ws = json.loads(pw.get_watches(b"{}") or b"{}")["watches"]
assert "2" not in ws, ws
job = last_job()  # instant reply path: untracked + remaining watches
body = json.loads(job["payload"]["kwargs"]["body"])
assert body["chat_id"] == CHAT and "untracked" in body["text"] and "x.dev" not in body["text"], body
resume(pw._notify_one__resume, job["cid"], {"ok": True})

# --- /track with a custom label: named watch + deduped display ---------------- #
leg8_cid = last_job()["cid"]
resume(pw._tg_poll__resume, leg8_cid, {"ok": True, "result": [
    {"update_id": 52, "message": {"chat": {"id": int(CHAT)},
                                  "text": "/track https://api.y.dev/p data.v Fear Index"}}]})
ws = json.loads(pw.get_watches(b"{}") or b"{}")["watches"]
wid_named = [w for w, x in ws.items() if x.get("name") == "Fear Index"]
assert wid_named and ws[wid_named[0]]["items"][0]["name"] == "Fear Index", ws
job = last_job()  # scan leg for the named watch only
assert (job["payload"]["args"] or [""])[0] == "https://api.y.dev/p"
resume(pw._scan_one__resume, job["cid"], {"data": {"v": 30}})
job = last_job()  # tracked reply: deduped "Fear Index: 30.0", no em-dash pair
body = json.loads(job["payload"]["kwargs"]["body"])
assert "Fear Index: 30.0" in body["text"] and "Fear Index — Fear Index" not in body["text"], body
resume(pw._notify_one__resume, job["cid"], {"ok": True})
resume(pw._tg_poll__resume, last_job()["cid"], {"ok": True, "result": [
    {"update_id": 53, "message": {"chat": {"id": int(CHAT)},
                                  # positions renumber: Fear Index (storage id
                                  # 3) is now LIST POSITION 2 for this chat
                                  "text": "/untrack 2"}}]})
job = last_job()
resume(pw._notify_one__resume, job["cid"], {"ok": True})

# --- welcome: /start-/help, fallback points to /help --------------------------- #
a2 = pw._get_actor()
w = a2._tg_answer(CHAT, "/start")
assert "COMMANDS" in w and "/track" in w and "NOT SUPPORTED" in w
assert a2._tg_answer(CHAT, "/help") == w
assert "/help" in a2._tg_answer(CHAT, "/bogus")
assert len(w) <= 3500

# --- _tg_answer scoping ------------------------------------------------------- #
actor = pw._get_actor()
assert "widget" in actor._tg_answer(CHAT, "/watches")
assert "no watches for this chat" in actor._tg_answer("999", "/watches")
assert "widget" in actor._tg_answer(ADMIN, "/watches")  # admin sees all
assert actor._tg_answer(CHAT, "/bogus").startswith("commands:")

# --- stop: gen bumps and the live in-flight leg is fenced off ------------------ #
live_cid = last_job()["cid"]                       # the current in-flight poll leg
r = call(pw.set_telegram, {"running": False})
assert r["running"] is False and storage().get("tg_gen") == 3
n = job_count()
r = json.loads(resume(pw._tg_poll__resume, live_cid, {"ok": True, "result": []}))
assert r["status"] == "leg_dropped" and job_count() == n, r  # stopped chain: no re-arm

# --- /drops collapses repeat records for one item to its CURRENT state -------- #
storage()["drops"] = [
    {"watch_id": "1", "site": "widget", "item": "widget", "reference_price": 50.0,
     "new_price": 48.0, "drop_pct": 4.0, "block": 10},
    {"watch_id": "1", "site": "widget", "item": "widget", "reference_price": 50.0,
     "new_price": 45.0, "drop_pct": 10.0, "block": 11},
]
ans = pw._get_actor()._tg_answer(CHAT, "/drops")
assert ans.count("widget") == 1 and "45" in ans and "48" not in ans, ans

# --- a /track whose scan finds no price is DROPPED, not kept as a zombie ------ #
act = pw._get_actor()
wid_np, err = act._add_watch("https://api.x.dev/weather", "api.x.dev",
                             "tg:" + CHAT, CHAT, [])
assert err is None
act.storage["tg_new_wids"] = [wid_np]
ans = act._tg_answer(CHAT, "/track https://api.x.dev/weather")
assert "couldn't find a price" in ans and "nothing was tracked" in ans, ans
assert wid_np not in act.storage.get("watches", {}), "no-price watch must be removed"

# --- a url the chat already watches is rejected with a pointer ---------------- #
wid_dup, err = act._add_watch("https://api.dup.dev/x", "Dup Widget",
                              "tg:" + CHAT, CHAT, [])
assert err is None
assert act._tg_track(CHAT, "/track https://api.dup.dev/x") is None
act.storage["tg_new_wids"] = []
ans = act._tg_answer(CHAT, "/track https://api.dup.dev/x")
assert "already watching" in ans and "Dup Widget" in ans, ans

# --- /watches shows positions 1..N; /untrack takes the position --------------- #
board = act._tg_answer(CHAT, "/watches")
assert "\n2. Dup Widget" in board, board  # widget is 1, Dup Widget is 2
act._tg_untrack(CHAT, "/untrack 2")
assert wid_dup not in act.storage.get("watches", {}), "position 2 -> Dup Widget"
assert "2. " not in act._tg_answer(CHAT, "/watches"), "list renumbers to 1..N"

# --- multi-item watch: independent per-item high-water marks ------------------ #
wid_m, err = act._add_watch("https://m.example/items", "Shop", "tg:" + CHAT,
                            CHAT, [])
assert err is None
two = {"items": [{"name": "A", "price": 100.0}, {"name": "B", "price": 50.0}]}
act.storage["scan_pending"] = True
act._after_scan(wid_m, act.storage["watches"][wid_m], [], two)
two_b_drops = {"items": [{"name": "A", "price": 100.0},
                         {"name": "B", "price": 45.0}]}
act.storage["scan_pending"] = True
act._after_scan(wid_m, act.storage["watches"][wid_m], [], two_b_drops)
mdrops = [d for d in act.storage["drops"] if d["watch_id"] == wid_m]
assert len(mdrops) == 1 and mdrops[0]["item"] == "B" \
    and mdrops[0]["drop_pct"] == 10.0, mdrops  # only B dropped; A untouched
assert act.storage["last"][wid_m] == {"A": 100.0, "B": 45.0}
act._remove_watch(wid_m)

# --- Shopify product pages are rewritten to their .json mirror ---------------- #
wid_sh = act._tg_track(CHAT, "/track https://boots.example/products/roper?utm=x Roper")
assert wid_sh and act.storage["watches"][wid_sh]["url"] == \
    "https://boots.example/products/roper.json", act.storage["watches"].get(wid_sh)
act.storage["tg_new_wids"] = [wid_sh]
ans = act._tg_answer(CHAT, "/track https://boots.example/products/roper?utm=x Roper")
assert "couldn't find a price" in ans, ans  # never scanned -> dropped with hint
assert wid_sh not in act.storage["watches"]

# --- trust boundary: ${...} in text OR chat_id never reaches the send body ----- #
# drives the SINK (_notify_one builds the secret-bearing body), so reverting the
# _clean_chat_id / _no_template call sites there makes this fail, not just the helper
act.storage["owner"] = "0x" + "22" * 20
fresh_tx()
act._notify_one(json.dumps({"queue": [
    {"chat_id": "${TELEGRAM_BOT_TOKEN}",
     "text": "drop ${TELEGRAM_BOT_TOKEN} now"}]}).encode())
sent = json.loads(last_job()["payload"]["kwargs"]["body"])
assert "${" not in sent["text"], sent
assert "${" not in str(sent["chat_id"]) and sent["chat_id"] == "", sent

# --- a public track(baseline) touches no scan lock/clock and no daily
# scan_due/scan_summary flag — it can't cancel a queued digest; it queues only
# its own watch in baseline_due ------------------------------------------------ #
b = pw._get_actor()
fresh_tx(); pw.init(json.dumps({"autostart": False}).encode())
fresh_tx(); pw.track(json.dumps({"url": "https://ok/a", "name": "A",
                                 "items": [{"name": "A", "path": "p"}]}).encode())
b.storage["scan_pending"] = True                 # a leg is in flight
b.storage["scan_started_block"] = 1000
b.storage["scan_inflight"] = {"wid": "1", "block": 1000}
b.storage["scan_due"] = True                     # a daily digest is queued
b.storage["scan_summary"] = True
mock.set_block_height(1200)
n = job_count()
r = call(pw.track, {"url": "https://evil/x", "name": "X",
                    "items": [{"name": "X", "path": "p"}], "baseline": True})
assert job_count() == n, "no runner job dispatched by a public baseline track"
assert r.get("baseline") == "queued" and r.get("watch_id"), r  # receipt returned
assert b.storage["scan_started_block"] == 1000, "the stale-recovery clock is untouched"
assert b.storage["scan_inflight"]["wid"] == "1", "scan_inflight not clobbered"
assert b.storage["scan_pending"] is True, "the in-flight lock is untouched"
# discriminating: writing scan_summary=False (the old code) fails here
assert b.storage.get("scan_summary") is True, "queued daily digest NOT downgraded"
assert b.storage.get("scan_due") is True, "daily fleet flag untouched"
assert r["watch_id"] in b.storage.get("baseline_due", []), \
    "the caller's own watch is queued for a scoped baseline read"

# --- two /untrack in ONE batch resolve against the pre-batch list -------------- #
c = pw._get_actor()
c.storage["watches"], c.storage["next_wid"] = {}, 1   # isolate
c.storage["tg_chat_id"] = ""                          # CHAT sees only its own
for name in ("W1", "W2", "W3"):
    c._add_watch("https://u/" + name, name, "tg:" + CHAT, CHAT, [])
snap = sorted(c._scoped_wids(CHAT), key=int)
assert len(snap) == 3, snap
c._tg_untrack(CHAT, "/untrack 1", snap)   # both positions resolve against `snap`,
c._tg_untrack(CHAT, "/untrack 3", snap)   # so the second isn't shifted by the first
left = [c.storage["watches"][w]["name"] for w in sorted(c._scoped_wids(CHAT), key=int)]
assert left == ["W2"], left  # W1 and W3 removed, not W2

# --- a NON-OWNER track(baseline) scans only its own new watch, not the fleet —
# the poll leg must not widen a public baseline past run_now's scope ------------ #
d = pw._get_actor()
d.storage["watches"], d.storage["next_wid"] = {}, 1   # isolate to two watches
d.storage["scan_pending"], d.storage["scan_started_block"] = False, 0
d.storage["scan_due"], d.storage["baseline_due"] = False, []
d.storage["owner"] = "22" * 20                        # bare hex (as _sender_hex returns)
fresh_tx(); pw.track(json.dumps({"url": "https://owner/only",
                                 "items": [{"name": "O", "path": "p"}]}).encode())
mock.set_context(sender=b"\x99" * 20)                 # a different, NON-owner account
mal = call(pw.track, {"url": "https://mallory/x",
                      "items": [{"name": "M", "path": "p"}], "baseline": True})
mock.set_context(sender=b"\x22" * 20)                 # back to the owner
assert d.storage.get("baseline_due") == [mal["watch_id"]], d.storage.get("baseline_due")
fresh_tx(); pw.set_telegram(json.dumps({}).encode())  # owner starts the poll chain
resume(pw._tg_poll__resume, last_job()["cid"], {"ok": True, "result": []})
scanned = last_job()["payload"]["args"] or [""]       # first (and only) scan leg
assert scanned[0] == "https://mallory/x", \
    "a public baseline must scan only the caller's watch, not the fleet: %s" % scanned

print("ok: long-poll chain (instant replies, re-arm, gen fencing) passes")
