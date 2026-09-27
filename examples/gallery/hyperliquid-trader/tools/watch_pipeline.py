#!/usr/bin/env python3
"""Live view of the research -> trader pipeline.

Polls both actors' read-only handlers and renders one screen: what research
currently believes (thesis, ideas, lessons) next to what the trader actually
did about it (book, P&L, scorecard). The point is the seam between them — an
idea appearing on the left and a position appearing on the right a few blocks
later is the whole demo.

Reads only. Every handler used is @public and non-mutating, so this cannot
perturb the actors it watches (no nonce consumed, no storage written).

    RPC=http://<ip>:4000 RESEARCH=0x.. TRADER=0x.. python3 watch_pipeline.py
    # optional: INTERVAL=10
"""

import base64
import json
import os
import sys
import time
import urllib.request


def _addr(fname):
    try:
        return (
            open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fname))
            .read()
            .strip()
        )
    except OSError:
        return ""


RPC = os.environ.get("RPC", "").rstrip("/")
# env overrides win; otherwise follow the current fleet via tools/*.txt so the
# watcher survives redeploys without re-aiming
RESEARCH = os.environ.get("RESEARCH", "") or _addr("research.txt")
TRADER = os.environ.get("TRADER", "") or _addr("actor.txt")
INTERVAL = int(os.environ.get("INTERVAL", "10"))

if not (RPC and RESEARCH and TRADER):
    sys.exit("set RPC, RESEARCH and TRADER env vars")

BOLD, DIM, GREEN, RED, CYAN, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[31m",
    "\033[36m",
    "\033[0m",
)


def read(actor, handler, payload="{}"):
    """Call a @public handler via /actor/read. Returns {} on any failure —
    a transient RPC hiccup should blank a panel, never kill the watcher."""
    body = json.dumps(
        {"actor": actor, "handler": handler, "payload_text": payload}
    ).encode()
    req = urllib.request.Request(
        RPC + "/actor/read", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.load(r)
        return json.loads(base64.b64decode(out["result"]))
    except Exception:
        return {}


def money(x):
    try:
        v = round(float(x), 2)
    except Exception:
        return "-"
    if v == 0:
        v = 0.0
    c = GREEN if v > 0 else (RED if v < 0 else "")
    return f"{c}{v:+.2f}{RESET}" if c else f"{v:.2f}"


def trunc(s, n):
    s = str(s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render():
    stats = read(RESEARCH, "get_stats")
    theses = read(RESEARCH, "get_theses").get("theses", [])
    ideas = read(RESEARCH, "get_ideas").get("ideas", [])
    wiki = read(RESEARCH, "get_wiki")
    port = read(TRADER, "get_portfolio")
    pnl = read(TRADER, "get_pnl")
    score = read(TRADER, "get_scorecard").get("scorecard", [])

    out = []
    out.append(
        f"{BOLD}research -> trader{RESET}   "
        f"{DIM}{time.strftime('%H:%M:%S')}  refresh {INTERVAL}s  ctrl-c to stop{RESET}"
    )
    out.append("")

    # ---- research ---------------------------------------------------------
    out.append(
        f"{BOLD}{CYAN}RESEARCH{RESET}  "
        f"{DIM}ingest {stats.get('ingest_runs', 0)} · "
        f"synth {stats.get('synth_runs', 0)} · "
        f"validate {stats.get('validate_runs', 0)} · "
        f"distill {stats.get('distill_runs', 0)} · "
        f"corpus {stats.get('window', 0)}{RESET}"
    )
    if theses:
        t = theses[0]
        out.append(f"  thesis  {trunc(t.get('statement'), 88)}")
        out.append(
            f"          {DIM}conviction {t.get('conviction', '-')} · "
            f"long {','.join(t.get('long', [])) or '-'} · "
            f"short {','.join(t.get('short', [])) or '-'} · "
            f"{t.get('status', '')}{RESET}"
        )
    else:
        out.append(f"  thesis  {DIM}(none yet — run synthesize_now){RESET}")

    out.append(f"  ideas   {len(ideas)}")
    for i in ideas[:5]:
        side = i.get("side", "")
        col = GREEN if side == "long" else RED
        out.append(
            f"    {col}{side:<5}{RESET} {i.get('asset', ''):<6} "
            f"conf {i.get('confidence', 0):<3} "
            f"{DIM}{i.get('thesis_fit', '')}  {trunc(i.get('rationale'), 46)}{RESET}"
        )

    lessons = wiki.get("lessons") or []
    if lessons:
        out.append(f"  lessons {len(lessons)}")
        for l in lessons[:2]:
            out.append(
                f"    {DIM}{trunc(l if isinstance(l, str) else l.get('text', l), 84)}{RESET}"
            )

    # ---- trader -----------------------------------------------------------
    out.append("")
    out.append(
        f"{BOLD}{CYAN}TRADER{RESET}    "
        f"{DIM}rebalances {pnl.get('rebalances', 0)} · "
        f"closed {pnl.get('closed', 0)} · "
        f"win rate {pnl.get('win_rate', 0)}{RESET}"
    )
    out.append(
        f"  equity  {money(pnl.get('equity'))}   "
        f"realized {money(pnl.get('realized_pnl'))}   "
        f"unrealized {money(pnl.get('unrealized_pnl'))}"
    )

    positions = port.get("positions", [])
    if positions:
        out.append(f"  book    {len(positions)} position(s)")
        for p in positions:
            side = p.get("side", "")
            col = GREEN if side == "long" else RED
            out.append(
                f"    {col}{side:<5}{RESET} {p.get('asset', ''):<6} "
                f"qty {str(p.get('qty', '-')):<10} "
                f"entry {str(p.get('entry_px', '-')):<10} "
                f"mark {str(p.get('mark_px', '-')):<10} "
                f"upnl {money(p.get('upnl', 0))}"
            )
    else:
        out.append(f"  book    {DIM}flat{RESET}")

    closed = [s for s in score if s.get("status") == "closed"]
    if closed:
        out.append(f"  closed  {len(closed)}")
        for s in closed[:3]:
            out.append(
                f"    {DIM}{s.get('asset', ''):<6} {s.get('side', ''):<5} "
                f"{float(s.get('return_pct', 0)):+.2f}%{RESET}"
            )

    sys.stdout.write("\033[H\033[J" + "\n".join(out) + "\n")
    sys.stdout.flush()


def main():
    try:
        while True:
            render()
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        sys.stdout.write("\n")


main()
