#!/usr/bin/env python3
"""Full-state inspector for the hyperliquid-trader demo.

Usage:
  RPC=http://<ip>:4000 python3 tools/inspect.py [research|trader|all] [--json]

Reads every public view handler and prints complete contents (no truncation).
Addresses default to tools/research.txt / tools/actor.txt.
"""

import base64
import json
import os
import sys
import urllib.request

RPC = (os.environ.get("RPC") or os.environ.get("COWBOY_RPC_URL") or "").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))


def addr(f):
    try:
        return open(os.path.join(HERE, f)).read().strip()
    except OSError:
        return ""


ACTORS = {
    "research": (
        addr("research.txt"),
        [
            "get_stats",
            "get_theses",
            "get_ideas",
            "get_items",
            "get_wiki",
            "get_universe",
        ],
    ),
    "trader": (
        addr("actor.txt"),
        ["get_stats", "get_pnl", "get_portfolio", "get_scorecard"],
    ),
}


def read(a, h):
    req = urllib.request.Request(
        RPC + "/actor/read",
        data=json.dumps({"actor": a, "handler": h, "payload_text": "{}"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    d = json.load(urllib.request.urlopen(req, timeout=20))
    if "result" not in d:
        return {"error": d}
    raw = base64.b64decode(d["result"])
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw.decode("utf-8", "replace")}


def main():
    if not RPC:
        sys.exit("set RPC=http://<ip>:4000")
    which = (
        sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "all"
    )
    as_json = "--json" in sys.argv
    targets = ACTORS if which == "all" else {which: ACTORS[which]}
    out = {}
    for name, (a, handlers) in targets.items():
        if not a:
            continue
        out[name] = {"address": a}
        for h in handlers:
            try:
                out[name][h] = read(a, h)
            except Exception as e:
                out[name][h] = {"error": str(e)}
    if as_json:
        print(json.dumps(out, indent=2))
        return
    for name, sections in out.items():
        print("=" * 72)
        print(f"  {name.upper()}  {sections.pop('address')}")
        print("=" * 72)
        for h, v in sections.items():
            print(f"\n--- {h} " + "-" * (60 - len(h)))
            print(json.dumps(v, indent=2))


if __name__ == "__main__":
    main()
