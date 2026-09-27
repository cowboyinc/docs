#!/usr/bin/env python3
"""Live send-and-receive round-trip for the AgentMail wrapper.

Creates one inbox, self-sends a uniquely-tagged message through the wrapper,
polls list_messages until it arrives (the receive path), fetches its body, and
searches for it. A direct list on the same inbox confirms the wrapper's output
matches AgentMail's. The wrapper path drives the actor with the real SDK: it
submits the handler, replays the exact job the actor composed against the live
vendor (substituting the CBSS key placeholder), resumes the continuation, and
reads the shaped result back from get_result as the registered hirer. Uses one
of the 3 free inboxes.

    AGENTMAIL_API_KEY_FILE=/secure/key.txt python3 -u live_agentmail_comparison.py
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SDK = Path(os.environ.get("COWBOY_SDK_PATH", HERE.parents[3] / "node/pvm/Lib")).resolve()
# Load the SDK by explicit spec, NOT via sys.path: that Lib carries a RustPython
# threading.py shim that shadows stdlib and breaks a standalone CPython run.
_spec = importlib.util.spec_from_file_location(
    "pvm_host", SDK / "cowboy_sdk/tests/mock_pvm_host.py")
host_mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_mock)
sys.modules["pvm_host"] = host_mock
_pkg = SDK / "cowboy_sdk"
_spec = importlib.util.spec_from_file_location(
    "cowboy_sdk", _pkg / "__init__.py", submodule_search_locations=[str(_pkg)])
_sdk = importlib.util.module_from_spec(_spec)
sys.modules["cowboy_sdk"] = _sdk
_spec.loader.exec_module(_sdk)
sys.path.insert(0, str(PROJECT))

import actors.inbox.main as am  # noqa: E402
from cowboy_sdk import codec  # noqa: E402

DEFAULT_KEY_FILE = os.environ.get("AGENTMAIL_API_KEY_FILE", "/private/tmp/agentmail-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


def http(url, method="GET", body=None, key=None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + key}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def _fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = am.AgentMailInboxWrapper()
    actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    actor.register_hire(codec.encode({
        "hire_id": 42, "hirer": "0x" + BUYER.hex(),
        "declaration_id": "0x" + "55" * 32, "version": 3, "artifact_id": 7}))
    return actor


def _live_reply(job, key):
    kw = job["payload"]["kwargs"]
    headers = dict(kw.get("headers") or {})
    if headers.get("Authorization") == "Bearer ${%s}" % am.SECRET_KEY:
        headers["Authorization"] = "Bearer " + key
    # The actor emits POST payloads as `raw_body` (verbatim bytes), not `body`;
    # read raw_body first and fall back to body for any older job shape.
    raw = kw.get("raw_body")
    if raw is None:
        raw = kw.get("body")
    body = json.loads(raw) if raw else None
    _, reply = http(kw["url"], kw.get("method", "GET"), body, key)
    return reply


def wrapped(key, handler, payload):
    actor = _fresh_actor()
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode(dict(payload, hire_id=42))))
    assert accepted["ok"] and accepted["status"] == "pending", accepted
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    host_mock.set_context(sender=ACTOR)
    am._run_request__resume(codec.encode(
        {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": _live_reply(job, key)}))
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode(
        {"request_id": accepted["request_id"], "hire_id": 42})))
    return result["output"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    marker = "cowboy-store-%d" % int(time.time())

    out = wrapped(key, "create_inbox", {})
    assert out["ok"] is True and out["data"].get("inbox_id"), out
    inbox = out["data"]["inbox_id"]
    print("PASS create_inbox  %s" % inbox)

    out = wrapped(key, "send", {"inbox_id": inbox, "to": inbox,
                                "subject": marker, "text": "body for " + marker})
    assert out["ok"] is True and out["data"].get("message_id"), out
    print("PASS send          message_id=%s (self-addressed)" % out["data"]["message_id"])

    got = None
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(6)
        out = wrapped(key, "list_messages", {"inbox_id": inbox, "limit": 20})
        assert out["ok"] is True, out
        for m in out["data"].get("messages", []):
            if m.get("subject") == marker:
                got = m
                break
        if got:
            break
    assert got, "self-sent message never appeared in the inbox within 120s"
    print("PASS list_messages received the message (message_id=%s)" % got.get("message_id"))

    _, direct = http(am.INBOXES + "/" + inbox + "/messages?limit=20", "GET", None, key)
    w = wrapped(key, "list_messages", {"inbox_id": inbox, "limit": 20})
    wid = [m.get("message_id") for m in w["data"]["messages"]]
    did = [m.get("message_id") for m in direct["messages"]]
    assert wid == did and wid, "wrapper/direct message ids differ: %r vs %r" % (wid, did)
    print("PASS list==direct  %d message ids match" % len(wid))

    out = wrapped(key, "get_message", {"inbox_id": inbox, "message_id": got["message_id"]})
    assert out["ok"] is True and marker in (out["data"].get("text") or ""), out
    print("PASS get_message   body contains the marker")

    out = wrapped(key, "search", {"inbox_id": inbox, "q": marker})
    assert out["ok"] is True and out["data"].get("messages"), out
    print("PASS search        found the message by marker")

    print("PASS: AgentMail wrapper send-and-receive round-trip")


if __name__ == "__main__":
    main()
