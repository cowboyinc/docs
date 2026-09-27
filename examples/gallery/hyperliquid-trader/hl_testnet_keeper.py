#!/usr/bin/env python3
"""Off-chain keeper — bridge the Cowboy research actor's ideas to REAL orders on
Hyperliquid TESTNET.

The research actor stays the autonomous "brain" (news -> thesis -> ideas). This
keeper is the "hands": it reads `get_ideas` from the actor, selects + sizes them
(mirroring the paper executor's `_select_ideas` / `_on_apply` / `plan_book`
logic), and places orders via the official HL Python SDK. The signing key never
leaves this machine, and it uses an AGENT/API wallet that can trade but NOT
withdraw.

Phase 1 (this file): CRYPTO core book only — the proven SDK path.
  Equities (xyz builder dex) = phase 2 (needs builder-dex order support).
  Pre-IPO (vntl) is not on testnet, so those ideas are skipped automatically.

SETUP
-----
  pip install hyperliquid-python-sdk eth-account requests
  1. On TESTNET (app.hyperliquid.xyz/API with the testnet toggle, or via
     Exchange.approve_agent()), approve an agent/API wallet. Save its private key.
  2. Fill CONFIG below.
  3. Run. DRY_RUN=True (default) only prints the planned orders — inspect them,
     then set DRY_RUN=False to actually trade your MOCK USD.
"""

import base64
import json

# ---- CONFIG --------------------------------------------------------------- #
# The signing key is read from the ENVIRONMENT, never hardcoded here — so it
# never lives in source and can't be committed to git. Before running:
#   export HL_AGENT_KEY=0x...      # agent/API wallet key (trades, CANNOT withdraw)
#   export HL_ACCOUNT_ADDR=0x...   # your main testnet account (holds the MOCK USD)
# Use an AGENT wallet, not your main key: it's revocable and has no withdraw rights,
# so a leak of this machine can churn positions but cannot drain funds.
import os
import time

import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

AGENT_KEY = os.environ.get("HL_AGENT_KEY", "")
ACCOUNT_ADDR = os.environ.get("HL_ACCOUNT_ADDR", "")
RESEARCH_ACTOR = os.environ.get(
    "HL_RESEARCH_ACTOR", "0x1a2c5Faf365Fc24e3f81E58a64799E3c820e347B"
)
COWBOY_RPC = os.environ.get("COWBOY_RPC", "https://rpc.mesa.cowboylabs.net")

DRY_RUN = (
    os.environ.get("HL_DRY_RUN", "1") != "0"
)  # default TRUE (print only); set HL_DRY_RUN=0 to trade

MAX_POSITIONS = 6  # mirror paper max_positions (book breadth)
MIN_CONFIDENCE = 55  # mirror paper min_confidence
INCLUDE_NEUTRAL = (
    False  # False = only thesis-aligned ideas (matches the actor); True = also neutral
)
BUDGET_FRAC = 0.90  # fraction of account equity to deploy
POLL_SECONDS = 300  # rebalance cadence
SLIPPAGE = 0.01  # market-order slippage tolerance for the SDK
# Builder perp dexes to ALSO trade beyond the core crypto book. "xyz" = US
# equities (AAPL/NVDA/...) on testnet. USDC unified collateral backs these too.
# Set to [] for crypto-only. (Pre-IPO "vntl" is empty on testnet, so it's omitted.)
EQUITY_DEXES = ["xyz"]


# ---- read ideas from the Cowboy actor ------------------------------------- #
def read_actor(handler, payload=None):
    r = requests.post(
        f"{COWBOY_RPC}/actor/read",
        json={
            "actor": RESEARCH_ACTOR,
            "handler": handler,
            "payload_json": payload or {},
        },
        timeout=15,
    )
    d = r.json()
    if "result" not in d:
        raise RuntimeError(f"read {handler} failed: {d}")
    return json.loads(base64.b64decode(d["result"]))


def get_ideas():
    return read_actor("get_ideas").get("ideas", [])


# ---- Hyperliquid info reads (used for multi-dex meta/mids/positions) ------- #
def hl_info(payload):
    return requests.post(
        constants.TESTNET_API_URL + "/info", json=payload, timeout=15
    ).json()


# ---- selection + sizing (mirrors hl_paper_exec) --------------------------- #
def select_ideas(ideas, tradeable):
    """aligns (+optional neutral) & confidence>=MIN & asset tradeable on testnet,
    ranked by confidence, top MAX_POSITIONS. dedup by asset (highest conf wins)."""
    fits = {"aligns"} if not INCLUDE_NEUTRAL else {"aligns", "neutral"}
    pool, seen = [], set()
    for i in sorted(
        ideas, key=lambda x: int(x.get("confidence", 0) or 0), reverse=True
    ):
        a = str(i.get("asset", "")).upper()
        if (
            str(i.get("side", "")).lower() in ("long", "short")
            and str(i.get("thesis_fit", "")).lower() in fits
            and int(i.get("confidence", 0) or 0) >= MIN_CONFIDENCE
            and a in tradeable
            and a not in seen
        ):
            seen.add(a)
            pool.append(
                {
                    "asset": a,
                    "is_buy": i["side"].lower() == "long",
                    "confidence": int(i["confidence"]),
                }
            )
    return pool[:MAX_POSITIONS]


def target_book(selected, equity, mids, sz_decimals):
    """confidence-weighted notionals -> sizes (rounded to szDecimals)."""
    budget = equity * BUDGET_FRAC
    total = sum(i["confidence"] for i in selected) or 1
    book = {}
    for i in selected:
        a = i["asset"]
        px = float(mids.get(a, 0) or 0)
        if px <= 0:
            continue
        notional = budget * i["confidence"] / total
        sz = round(notional / px, sz_decimals.get(a, 4))
        if sz > 0:
            book[a] = {"is_buy": i["is_buy"], "sz": sz, "px": px, "notional": notional}
    return book


def plan(target, current):
    """diff-against-target: (closes, opens). current: asset -> signed size."""
    closes, opens = [], []
    for a, cur_sz in current.items():
        want = target.get(a)
        cur_is_buy = cur_sz > 0
        if not want or want["is_buy"] != cur_is_buy:  # gone, or side flipped
            closes.append(a)
    held = {
        a for a in current if a in target and (current[a] > 0) == target[a]["is_buy"]
    }
    for a, t in target.items():
        if a not in held:  # new name (or reopened after flip)
            opens.append((a, t))
    return closes, opens


# ---- main loop ------------------------------------------------------------ #
def main():
    if not AGENT_KEY or not ACCOUNT_ADDR:
        raise SystemExit(
            "Set HL_AGENT_KEY and HL_ACCOUNT_ADDR in the environment "
            "(agent wallet key + your testnet account address). "
            "Never hardcode them; keep them out of git."
        )
    wallet = eth_account.Account.from_key(AGENT_KEY)
    # Register the builder dexes so the SDK can resolve/sign namespaced coins.
    exchange = Exchange(
        wallet,
        constants.TESTNET_API_URL,
        account_address=ACCOUNT_ADDR,
        perp_dexs=EQUITY_DEXES or None,
    )

    # Universe across the core crypto book + each builder dex. coin_of maps a bare
    # symbol -> the coin string the SDK expects: "BTC" (core) or "xyz:NVDA" (builder).
    sz_decimals, coin_of = {}, {}
    for a in hl_info({"type": "meta"})["universe"]:
        s = a["name"].upper()
        sz_decimals[s] = a["szDecimals"]
        coin_of[s] = a["name"]
    for dex in EQUITY_DEXES:
        for a in hl_info({"type": "meta", "dex": dex}).get("universe", []):
            name = a[
                "name"
            ]  # builder dex names are already namespaced, e.g. "xyz:NVDA"
            s = name.split(":")[-1].upper()  # bare symbol for matching ideas: "NVDA"
            sz_decimals[s] = a["szDecimals"]
            coin_of[s] = name  # full coin string the SDK expects: "xyz:NVDA"
    tradeable = set(coin_of)
    print(
        f"[keeper] tradeable universe: {len(tradeable)} perps (core + {EQUITY_DEXES}) | DRY_RUN={DRY_RUN}"
    )

    while True:
        try:
            # equity: unified spot USDC + any perp PnL (see note above)
            perp = hl_info({"type": "clearinghouseState", "user": ACCOUNT_ADDR})
            perp_val = float(perp.get("marginSummary", {}).get("accountValue", 0) or 0)
            spot = hl_info({"type": "spotClearinghouseState", "user": ACCOUNT_ADDR})
            usdc = next(
                (
                    float(b["total"])
                    for b in spot.get("balances", [])
                    if b.get("coin") == "USDC"
                ),
                0.0,
            )
            equity = usdc + perp_val

            # current positions across core + builder dexes -> bare symbol (best-effort per dex)
            current = {}
            for p in perp.get("assetPositions", []):
                current[p["position"]["coin"].split(":")[-1].upper()] = float(
                    p["position"]["szi"]
                )
            for dex in EQUITY_DEXES:
                try:
                    for p in hl_info(
                        {"type": "clearinghouseState", "user": ACCOUNT_ADDR, "dex": dex}
                    ).get("assetPositions", []):
                        current[p["position"]["coin"].split(":")[-1].upper()] = float(
                            p["position"]["szi"]
                        )
                except Exception:
                    pass

            # prices across core + builder dexes -> bare symbol
            mids = {
                k.split(":")[-1].upper(): float(v)
                for k, v in hl_info({"type": "allMids"}).items()
            }
            for dex in EQUITY_DEXES:
                try:
                    for k, v in hl_info({"type": "allMids", "dex": dex}).items():
                        mids[k.split(":")[-1].upper()] = float(v)
                except Exception:
                    pass

            ideas = get_ideas()
            selected = select_ideas(ideas, tradeable)
            target = target_book(selected, equity, mids, sz_decimals)
            closes, opens = plan(target, current)

            print(
                f"\n[{time.strftime('%H:%M:%S')}] equity=${equity:.2f} "
                f"ideas={len(ideas)} selected={[i['asset'] for i in selected]}"
            )
            print(f"  current: { {a: round(s, 4) for a, s in current.items()} }")
            print(f"  closes:  {closes}")
            print(
                f"  opens:   { {a: (('BUY' if t['is_buy'] else 'SELL'), t['sz'], f'~${t["notional"]:.0f}') for a, t in opens} }"
            )

            if DRY_RUN:
                print("  DRY_RUN — no orders placed.")
            else:
                for a in closes:
                    print("  close", a, "->", exchange.market_close(coin_of[a]))
                for a, t in opens:
                    print(
                        "  open",
                        a,
                        "->",
                        exchange.market_open(
                            coin_of[a], t["is_buy"], t["sz"], None, SLIPPAGE
                        ),
                    )
        except Exception as e:
            print("  [error]", repr(e))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
