#!/usr/bin/env python3
"""Compare a live Canyon AgentMail actor with independent direct calls.

Creates an inbox and sends a self-addressed message on chain, then confirms the
message is receivable both directly and through the actor's list_messages. The wrapper emits an acceptance event containing the exact request id. This
client parses that event, waits for that exact record to leave `pending`, and
requires the matching operation result.

    python3 -u live_agentmail_canyon.py --actor 0x... --hire-id N --key-file /secure/key.txt
"""
import argparse
import json
import os
import re
import subprocess
import time

import live_agentmail_comparison as cmp
from live_agentmail_comparison import http

codec = cmp.codec


def rpc_json(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def state(args):
    return rpc_json(args.rpc.rstrip("/") + "/actor/" + args.actor)


def value(snapshot, key, default=None):
    raw = snapshot.get("storage", {}).get(key)
    return default if raw is None else codec.decode(bytes.fromhex(raw[2:]))


def request_record(args, request_id):
    key = "0x" + ("request:" + str(request_id)).encode().hex()
    return value(state(args), key, {})


def canyon(args, handler, payload):
    env = dict(os.environ, COWBOY_RPC_URL=args.rpc)
    completed = subprocess.run([args.cowboy, "actor", "execute", "--actor", args.actor,
                    "--handler", handler, "--payload-json", json.dumps(dict(payload, hire_id=args.hire_id)),
                    "--private-key", args.wallet_key, "--receipt-timeout-seconds", "180"],
                   check=True, capture_output=True, text=True, env=env)
    output = completed.stdout + "\n" + completed.stderr
    events = re.findall(r"^\s*-\s+([\w.]+):\s*(0x[0-9a-fA-F]+)\s*$", output, re.MULTILINE)
    accepted = [codec.decode(bytes.fromhex(data[2:])) for topic, data in events
                if topic == "wrapper.request_accepted"]
    assert len(accepted) == 1 and accepted[0].get("operation") == handler
    request_id = accepted[0]["request_id"]
    deadline = time.time() + 240
    while time.time() < deadline:
        record = request_record(args, request_id)
        if isinstance(record, dict) and record.get("status") in ("ok", "error"):
            result = record.get("output") or {}
            assert record.get("request_id") == request_id
            assert result.get("operation") == handler
            return result
        time.sleep(4)
    raise TimeoutError("Canyon %s timed out" % handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True)
    parser.add_argument("--hire-id", required=True, type=int)
    parser.add_argument("--key-file", default=os.environ.get("AGENTMAIL_API_KEY_FILE",
                                                             "/private/tmp/agentmail-key.txt"))
    parser.add_argument("--rpc", default="https://rpc.canyon.cowboylabs.net")
    parser.add_argument("--wallet-key", default=os.path.expanduser("~/.cowboy/cal-key"))
    parser.add_argument("--cowboy", default="/Users/calebmandia/Desktop/Cowboy/bin/cowboy")
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    marker = "cowboy-canyon-%d" % int(time.time())

    out = canyon(args, "create_inbox", {})
    assert out["ok"] is True and out["data"].get("inbox_id"), out
    inbox = out["data"]["inbox_id"]
    print("PASS create_inbox  Canyon created %s" % inbox)

    out = canyon(args, "send", {"inbox_id": inbox, "to": inbox, "subject": marker,
                                "text": "canyon body " + marker})
    assert out["ok"] is True and out["data"].get("message_id"), out
    print("PASS send          Canyon sent message_id=%s" % out["data"]["message_id"])

    got = None
    deadline = time.time() + 180
    while time.time() < deadline:
        out = canyon(args, "list_messages", {"inbox_id": inbox, "limit": 20})
        assert out["ok"] is True, out
        if any(m.get("subject") == marker for m in out["data"].get("messages", [])):
            got = True
            break
        time.sleep(6)
    assert got, "self-sent message not received on chain within 180s"
    print("PASS list_messages Canyon received the message")

    _, direct = http(cmp.am.INBOXES + "/" + inbox + "/messages?limit=20", "GET", None, key)
    assert any(m.get("subject") == marker for m in direct.get("messages", [])), "direct list missing it"
    print("PASS direct        the message is visible via a direct AgentMail call too")

    print("PASS: Canyon AgentMail send-and-receive matrix")


if __name__ == "__main__":
    main()
