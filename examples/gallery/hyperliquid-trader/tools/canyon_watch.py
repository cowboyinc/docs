#!/usr/bin/env python3
"""Sample canyon research state to disk. READ ONLY.

Canyon is persistent, so unlike river there is no teardown deadline — but the
local record still has to be captured deliberately. run5's plan_skips data was
lost exactly this way.
"""

import base64
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

RPC, ADDR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
INTERVAL = int(sys.argv[4]) if len(sys.argv) > 4 else 600


def read(h):
    req = urllib.request.Request(
        RPC + "/actor/read",
        data=json.dumps({"actor": ADDR, "handler": h, "payload_text": "{}"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    d = json.load(urllib.request.urlopen(req, timeout=25))
    return json.loads(base64.b64decode(d["result"]))


while True:
    rec = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        rec["height"] = json.load(
            urllib.request.urlopen(RPC + "/height", timeout=15)
        ).get("height")
    except Exception:
        rec["height"] = None
    for h in ("get_stats", "get_theses", "get_wiki", "get_ideas"):
        try:
            rec[h] = read(h)
        except Exception as e:
            rec[h] = {"error": str(e)[:120]}
    try:
        rec["balance"] = json.load(
            urllib.request.urlopen(f"{RPC}/account/{ADDR}", timeout=15)
        ).get("balance")
    except Exception:
        rec["balance"] = None
    with open(OUT, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    s = rec.get("get_stats") or {}
    print(
        "%s h=%s ingest=%s synth=%s validate=%s distill=%s theses=%s lessons=%s bal=%s"
        % (
            rec["ts"][11:19],
            rec.get("height"),
            s.get("ingest_runs"),
            s.get("synth_runs"),
            s.get("validate_runs"),
            s.get("distill_runs"),
            s.get("total_theses"),
            s.get("lessons"),
            rec.get("balance"),
        ),
        flush=True,
    )
    time.sleep(INTERVAL)
