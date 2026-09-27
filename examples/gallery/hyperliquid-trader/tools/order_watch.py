#!/usr/bin/env python3
"""Sample the trader's order audit from RAW actor storage. READ ONLY.

storage["orders"] keeps only the last 50 entries and rolls, so a rejection that
is not sampled is lost — exactly how run5's plan_skips data was lost. This never
contacts the actor: it reads GET /actor/{addr}, which returns hex-encoded storage,
and decodes the CBOR value. No handler calls, no transactions, no redeploy.

Answers for run7: what fraction of orders are refused, and on which symbols.
    order_watch.py <rpc> <trader> <out.jsonl> [interval]
"""

import json
import os
import pathlib
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SDK_PATH = pathlib.Path(
    os.environ.get(
        "COWBOY_SDK_PATH", ROOT.parents[3] / "node" / "pvm" / "Lib" / "pypi_build"
    )
)
if SDK_PATH.exists():
    sys.path.insert(0, str(SDK_PATH))

from cowboy_sdk import codec


def main():
    rpc, trader, path = sys.argv[1], sys.argv[2], sys.argv[3]
    interval = int(sys.argv[4]) if len(sys.argv) > 4 else 300
    seen = set()
    while True:
        try:
            with urllib.request.urlopen(f"{rpc}/actor/{trader}", timeout=25) as r:
                st = json.load(r).get("storage", {})
            blob = None
            for k, v in st.items():
                if bytes.fromhex(k[2:]).decode("utf-8", "replace") == "orders":
                    blob = bytes.fromhex(v[2:])
                    break
            orders = codec.decode(blob) if blob else []
            fresh = []
            for o in orders or []:
                key = (o.get("sym"), o.get("height"), o.get("why"))
                if key in seen:
                    continue
                seen.add(key)
                resp = o.get("response") or {}
                err = None
                # TWO rejection shapes, both of which parse_fill maps to (0.0, None):
                #   1. statuses[].error      -> liquidity ("could not immediately match")
                #   2. top-level status:err  -> nonce collision, transport, auth, etc.
                # Only checking (1) scored a duplicate-nonce refusal as a success.
                try:
                    if isinstance(resp, dict) and resp.get("status") == "err":
                        err = str(resp.get("response"))
                except Exception:
                    pass
                try:
                    for st_ in resp["response"]["data"]["statuses"]:
                        if isinstance(st_, dict) and "error" in st_:
                            err = st_["error"]
                except Exception:
                    pass
                fresh.append(
                    {
                        "sym": o.get("sym"),
                        "why": o.get("why"),
                        "height": o.get("height"),
                        "filled": o.get("filled"),
                        "rejected": bool(err),
                        "error": (err or "")[:160],
                    }
                )
            if fresh:
                rec = {"ts": datetime.now(timezone.utc).isoformat(), "orders": fresh}
                with open(path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                rej = sum(1 for f in fresh if f["rejected"])
                print(f"{rec['ts']}  +{len(fresh)} orders, {rej} rejected", flush=True)
        except Exception as e:
            print(f"poll failed: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
