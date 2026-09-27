"""Execution actor (paper mode, self-contained) — run Research's top ideas as a
tracked paper book. No custody, no venue: reads real prices and *simulates*
fills, so it's the guarded execution gate with the signer swapped for a recorder
(US-legal, since nothing is traded).

Self-contained (no library import): the timer-fire path doesn't inject CIP-26
libraries, so keeping all logic in-module is what lets the rebalance/mark timer
loops fire autonomously. Prices span every dex Research trades — crypto, US
equities (`xyz`), pre-IPO (`vntl`). Deploy/execute at cycles/cells <= 40M.

To go live, swap the "record fill at market" step for a real signer + venue call.
"""

import json

from cowboy_sdk import (
    OWNER,
    SELF,
    AddressError,
    actor,
    callable_by,
    capture,
    deferred,
    public,
    pure,
    runner,
    runtime,
    to_address_bytes,
)

HL_INFO = "https://api.hyperliquid.xyz/info"
_HDRS = {"Content-Type": "application/json"}


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


def _parse(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8") if payload else "{}"
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _json_bytes(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _b64_text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    s = str(value or "")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    out = bytearray()
    acc = 0
    bits = 0
    valid = 0
    for ch in s:
        if ch == "=":
            break
        value6 = alphabet.find(ch)
        if value6 < 0:
            if ch in (" ", "\t", "\r", "\n"):
                continue
            return s
        valid += 1
        acc = (acc << 6) | value6
        bits += 6
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 255)
            acc = acc & ((1 << bits) - 1) if bits else 0
    if not valid:
        return "" if not s else s
    return bytes(out).decode("utf-8", "replace")


def _normalize_result(result):
    """Decode an HTTP runner result into a dict, or return an empty dict."""
    result = getattr(result, "raw", result)
    if isinstance(result, dict) and "raw" in result:
        result = _b64_text(result["raw"])
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return {}
    return result if isinstance(result, dict) else {}


def mids_query(dex):
    q = {"type": "allMids"}
    if dex:
        q["dex"] = dex
    return _json_bytes(q)


def resolve_prices(symbols, dex_results):
    # Price a flat symbol list against several dex allMids responses. Builder
    # dexs namespace keys as `dex:SYMBOL`, the core book keys are bare.
    norm = []
    for dex, res in dex_results or []:
        data = _normalize_result(res)
        mids = data.get("mids", data) if isinstance(data, dict) else {}
        norm.append((dex, mids if isinstance(mids, dict) else {}))
    out = {}
    for s in symbols or []:
        sym = str(s).upper()
        for dex, mids in norm:
            v = (
                mids.get(dex + ":" + sym)
                if dex
                else (mids.get(sym) or mids.get(str(s)))
            )
            if v is not None:
                try:
                    out[sym] = float(v)
                    break
                except Exception:
                    pass
    return out


def _by_confidence(idea):
    return int(idea.get("confidence", 0) or 0)


def _select_ideas(ideas, top_n, min_conf):
    pool = [
        i
        for i in (ideas or [])
        if isinstance(i, dict)
        and str(i.get("side", "")).lower() in ("long", "short")
        and str(i.get("thesis_fit", "")).lower() == "aligns"
        and int(i.get("confidence", 0) or 0) >= int(min_conf)
    ]
    pool.sort(key=_by_confidence, reverse=True)
    return pool[: int(top_n)]


def _pos_return(side, entry_px, cur_px):
    entry = float(entry_px or 0)
    if not entry or cur_px is None:
        return 0.0
    sign = 1.0 if side == "long" else -1.0
    return sign * (float(cur_px) - entry) / entry


def _mark_book(portfolio, prices):
    unreal = 0.0
    marked = []
    for p in portfolio or []:
        cur = prices.get(str(p.get("asset", "")).upper())
        r = _pos_return(p.get("side"), p.get("entry_px"), cur)
        pnl = r * float(p.get("notional", 0) or 0)
        unreal += pnl
        m = dict(p)
        m["mark_px"] = cur
        m["ret_pct"] = r
        m["upnl"] = pnl
        marked.append(m)
    return unreal, marked


def _exit_reason(side, entry_px, cur_px, sl, tp):
    # Risk control: "stop_loss" / "take_profit" / None for a position at its mark.
    # sl/tp are fractions (0.10 = 10%); 0 disables that side.
    if cur_px is None:
        return None
    r = _pos_return(side, entry_px, cur_px)
    if sl and float(sl) > 0 and r <= -float(sl):
        return "stop_loss"
    if tp and float(tp) > 0 and r >= float(tp):
        return "take_profit"
    return None


def _close_at_mark(position, cur, reason, height):
    """Realize a full paper close at the mark price."""
    pnl = _pos_return(position.get("side"), position.get("entry_px"), cur) * float(
        position.get("notional", 0) or 0
    )
    record = dict(position)
    record["exit_px"] = cur
    record["pnl"] = pnl
    record["closed_block"] = height
    record["exit_reason"] = reason
    return pnl, record


def plan_book(portfolio, target, prices):
    # Diff the held book against the target set (asset -> {side, confidence}).
    # Returns (keep, close): keep = held positions still wanted (same asset+side,
    # returned untouched so their entry price + clock survive); close = (position,
    # cur_px) pairs rotating out, to be realized. A held name we can't price is
    # kept (we can't close a position without a mark). This is what stops the
    # book from churning identical names when the idea set hasn't changed.
    keep, close = [], []
    for p in portfolio or []:
        asset = str(p.get("asset", "")).upper()
        tgt = target.get(asset)
        if tgt and str(tgt.get("side", "")).lower() == str(p.get("side", "")).lower():
            keep.append(p)
            continue
        cur = prices.get(asset)
        if cur is None:
            keep.append(p)
            continue
        close.append((p, cur))
    return keep, close


# node 04c9f283 types/src/constants.rs:1880-1881: 160 own + 162 child.
TIMER_ACCESS_LIMIT = 322
IDEAS_MAX_AGE_BLOCKS = 1800


@actor
class PaperExecution:
    @callable_by(OWNER, decode="raw")
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        self.storage["__name__"] = "Paper Execution"
        self.storage["__description__"] = (
            "Paper-trade Research's ideas across crypto/equities/pre-IPO (no custody)"
        )
        self.storage["__version__"] = "3.0.0"
        obj = _parse(payload)
        self.storage["cfg"] = {
            "price_url": str(obj.get("price_url", HL_INFO)),
            "budget": float(obj.get("budget", 1000.0)),
            "max_positions": int(obj.get("max_positions", 10)),
            "min_confidence": int(obj.get("min_confidence", 60)),
            # De-harmonized to primes coprime with research's cadences (149/599/
            # 3593/7207): rebalance no longer shares the 600 beat with synth, so
            # it never reads get_ideas mid-synthesis. mark no longer divides it.
            "mark_interval_blocks": int(obj.get("mark_interval_blocks", 293)),
            "rebalance_interval_blocks": int(obj.get("rebalance_interval_blocks", 601)),
            "stop_loss_pct": float(
                obj.get("stop_loss_pct", 0.0)
            ),  # close if down >= this (0 = off)
            "take_profit_pct": float(
                obj.get("take_profit_pct", 0.0)
            ),  # close if up >= this (0 = off)
        }
        self.storage["research_addr"] = str(obj.get("research_addr", ""))
        self.storage["portfolio"] = []
        self.storage["history"] = []
        self.storage["last_prices"] = {}
        self.storage["realized_pnl"] = 0.0
        self.storage["closed_count"] = 0
        self.storage["win_count"] = 0
        self.storage["rebalances"] = 0
        self.storage["running"] = bool(obj.get("running", False))
        self.storage["apply_pending"] = False
        self.storage["latest_ideas"] = []
        self.storage["ideas_snapshot_block"] = 0
        self.storage["cached_research_stats"] = {}
        self.storage["notify_addr"] = ""
        runtime.emit_event("paper.init", {"budget": self.storage["cfg"]["budget"]})
        return b"ok: initialized"

    def _commit_prices(self, prices):
        """Merge fresh prices into last_prices and return the merged map."""
        merged = dict(self.storage.get("last_prices", {}))
        if prices:
            merged.update(prices)
            self.storage["last_prices"] = merged
        return merged

    # ---- rebalance to the top ideas ------------------------------------- #

    @callable_by(OWNER, decode="raw")
    def set_research(self, payload):
        addr = str(_parse(payload).get("research_addr", ""))
        self.storage["research_addr"] = addr
        self.storage["latest_ideas"] = []
        self.storage["ideas_snapshot_block"] = 0
        self.storage["cached_research_stats"] = {}
        self.storage["notify_addr"] = ""
        runtime.emit_event("paper.research_set", {"addr": addr})
        return _json_bytes({"research_addr": addr})

    @public(decode="raw")
    @deferred
    def handle_message(self, payload):
        """Receive the latest synthesis from the configured research actor."""
        obj = _parse(payload)
        if str(obj.get("type", "")) != "research_snapshot":
            return b"ignored: unknown message"
        if not _sender_matches(self.storage.get("research_addr", "")):
            return b"ignored: unknown research"
        ideas = list(obj.get("ideas") or [])
        self.storage["latest_ideas"] = ideas
        self.storage["ideas_snapshot_block"] = int(runtime.get_block_height())
        self.storage["cached_research_stats"] = dict(obj.get("stats") or {})
        self.storage["notify_addr"] = _normalized_address(obj.get("notify_addr", ""))
        runtime.emit_event("paper.ideas_received", {"count": len(ideas)})
        return b"stored"

    @callable_by(OWNER, decode="raw")
    @deferred
    def apply_ideas(self, payload):
        self.storage["apply_pending"] = True
        return self._apply(payload)

    @callable_by(OWNER, SELF, decode="raw")
    @deferred
    def rebalance(self, payload):
        return self._do_rebalance()

    @callable_by(SELF, decode="raw")
    @deferred
    def rebalance_tick(self, payload):
        if not self.storage.get("running"):
            return b"skip"
        height = self._schedule(
            "rebalance_tick", int(self.storage["cfg"]["rebalance_interval_blocks"])
        )
        return self._do_rebalance(height)

    def _do_rebalance(self, current_height=None):
        addr = str(self.storage.get("research_addr", ""))
        if not addr:
            runtime.emit_event("paper.no_research", {})
            return b"no research addr"
        snapshot_block = int(self.storage.get("ideas_snapshot_block", 0) or 0)
        if snapshot_block <= 0:
            runtime.emit_event("paper.no_cached_ideas", {})
            return b"no cached ideas"
        height = (
            int(runtime.get_block_height())
            if current_height is None
            else int(current_height)
        )
        if height - snapshot_block > IDEAS_MAX_AGE_BLOCKS:
            runtime.emit_event(
                "paper.stale_cached_ideas",
                {
                    "snapshot_block": snapshot_block,
                },
            )
            return b"stale cached ideas"
        ideas = list(self.storage.get("latest_ideas", []))
        runtime.emit_event("paper.pulled_ideas", {"count": len(ideas)})
        self.storage["apply_pending"] = True
        return self._apply(_json_bytes({"ideas": ideas}))

    def _portfolio_snapshot(self):
        unreal, marked = _mark_book(
            self.storage.get("portfolio", []), self.storage.get("last_prices", {})
        )
        return {
            "positions": marked,
            "unrealized_pnl": unreal,
            "last_prices": self.storage.get("last_prices", {}),
        }

    def _pnl_snapshot(self):
        cfg = self.storage.get("cfg", {})
        budget = float(cfg.get("budget", 0.0))
        realized = float(self.storage.get("realized_pnl", 0.0))
        unreal, _m = _mark_book(
            self.storage.get("portfolio", []), self.storage.get("last_prices", {})
        )
        closed = int(self.storage.get("closed_count", 0))
        wins = int(self.storage.get("win_count", 0))
        return {
            "budget": budget,
            "realized_pnl": realized,
            "unrealized_pnl": unreal,
            "equity": budget + realized + unreal,
            "return_pct": ((realized + unreal) / budget) if budget else 0.0,
            "open_positions": len(self.storage.get("portfolio", [])),
            "closed": closed,
            "win_rate": (wins / closed) if closed else 0.0,
            "rebalances": int(self.storage.get("rebalances", 0)),
        }

    def _scorecard_snapshot(self):
        prices = self.storage.get("last_prices", {})
        out = []
        for p in self.storage.get("portfolio", []):
            cur = prices.get(str(p.get("asset", "")).upper())
            out.append(
                {
                    "asset": p.get("asset"),
                    "side": p.get("side"),
                    "status": "open",
                    "return_pct": round(
                        _pos_return(p.get("side"), p.get("entry_px"), cur) * 100, 2
                    ),
                }
            )
        for h in self.storage.get("history", [])[:8]:
            r = _pos_return(h.get("side"), h.get("entry_px"), h.get("exit_px"))
            out.append(
                {
                    "asset": h.get("asset"),
                    "side": h.get("side"),
                    "status": "closed",
                    "return_pct": round(r * 100, 2),
                }
            )
        return {"scorecard": out}

    def _push_trader_snapshot(self):
        addr = str(self.storage.get("research_addr", ""))
        if not addr:
            return False
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "trader_snapshot",
                        "scorecard": self._scorecard_snapshot()["scorecard"],
                        "unfillable": [],
                        "pnl": self._pnl_snapshot(),
                    }
                ),
            )
            return True
        except Exception as e:
            runtime.emit_event("paper.research_send_failed", {"err": str(e)[:80]})
            return False

    def _push_command_snapshot(self):
        addr = str(self.storage.get("notify_addr", ""))
        if not addr:
            return False
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "command_snapshot",
                        "research_stats": dict(
                            self.storage.get("cached_research_stats", {})
                        ),
                        "ideas": list(self.storage.get("latest_ideas", [])),
                        "pnl": self._pnl_snapshot(),
                        "portfolio": self._portfolio_snapshot(),
                    }
                ),
            )
            return True
        except Exception as e:
            runtime.emit_event("paper.notify_send_failed", {"err": str(e)[:80]})
            return False

    @runner.continuation
    async def _apply(self, payload):
        ctx = capture()
        ctx.cfg = self.storage.get("cfg", {})
        ctx.selected = _select_ideas(
            _parse(payload).get("ideas", []),
            ctx.cfg.get("max_positions", 3),
            ctx.cfg.get("min_confidence", 60),
        )
        ctx.url = ctx.cfg["price_url"]  # captured: reused across the awaits below
        ctx.p_coins = await runner.http(
            ctx.url, method="POST", headers=_HDRS, body=mids_query(""), timeout_ms=5000
        )
        ctx.p_stocks = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("xyz"),
            timeout_ms=5000,
        )
        ctx.p_preipo = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("vntl"),
            timeout_ms=5000,
        )
        return self._on_apply(ctx.selected, ctx.p_coins, ctx.p_stocks, ctx.p_preipo)

    def _on_apply(self, selected, p_coins, p_stocks, p_preipo):
        self.storage["apply_pending"] = False
        cfg = self.storage["cfg"]
        height = int(runtime.get_block_height())
        portfolio = list(self.storage.get("portfolio", []))
        # target book: dedup the selected ideas by asset (highest-confidence wins,
        # since _select_ideas already ranked them by confidence).
        target = {}
        for i in selected:
            asset = str(i.get("asset", "")).upper()
            if asset and asset not in target:
                target[asset] = {
                    "side": str(i.get("side", "")).lower(),
                    "confidence": int(i.get("confidence", 0) or 0),
                }
        held = [str(p.get("asset", "")).upper() for p in portfolio]
        prices = resolve_prices(
            list(target.keys()) + held,
            [("", p_coins), ("xyz", p_stocks), ("vntl", p_preipo)],
        )
        if not prices:
            runtime.emit_event("paper.price_error", {})
            return b"no prices"

        # 1) diff-against-target: keep names still wanted (untouched — entry price
        #    and clock preserved), realize only the ones rotating out.
        keep, close = plan_book(portfolio, target, prices)
        realized = float(self.storage.get("realized_pnl", 0.0))
        closed = int(self.storage.get("closed_count", 0))
        wins = int(self.storage.get("win_count", 0))
        history = list(self.storage.get("history", []))
        for p, cur in close:
            pnl, rec = _close_at_mark(p, cur, "rebalance", height)
            realized += pnl
            closed += 1
            if pnl > 0:
                wins += 1
            history.insert(0, rec)

        # 2) open target names we're not already holding, confidence-weighted.
        #    Held names keep their original size (no resize — avoids partial-fill
        #    P&L accounting); when the idea set is unchanged this loop is a no-op.
        held_now = {str(p.get("asset", "")).upper() for p in keep}
        budget = float(cfg["budget"])
        total_conf = sum(t["confidence"] for t in target.values()) or 1
        opened = 0
        for asset, tgt in target.items():
            if asset in held_now:
                continue
            cur = prices.get(asset)
            if cur is None:
                continue
            keep.append(
                {
                    "asset": asset,
                    "side": tgt["side"],
                    "entry_px": cur,
                    "notional": budget * (tgt["confidence"] / total_conf),
                    "confidence": tgt["confidence"],
                    "opened_block": height,
                }
            )
            opened += 1

        self.storage["portfolio"] = keep
        self.storage["history"] = history[:50]
        self.storage["last_prices"] = prices
        self.storage["realized_pnl"] = realized
        self.storage["closed_count"] = closed
        self.storage["win_count"] = wins
        self.storage["rebalances"] = int(self.storage.get("rebalances", 0)) + 1
        report = {
            "positions": len(keep),
            "opened": opened,
            "closed": len(close),
            "realized_pnl": realized,
            "rebalances": self.storage["rebalances"],
        }
        runtime.emit_event("paper.rebalanced", report)
        self._push_trader_snapshot()
        return _json_bytes(report)

    # ---- mark-to-market ------------------------------------------------- #

    @callable_by(OWNER, decode="raw")
    @deferred
    def mark_now(self, payload):
        return self._mark(b"{}")

    @callable_by(SELF, decode="raw")
    @deferred
    def mark_tick(self, payload):
        if not self.storage.get("running"):
            return b"skip"
        self._schedule("mark_tick", int(self.storage["cfg"]["mark_interval_blocks"]))
        return self._mark(b"{}")

    @runner.continuation
    async def _mark(self, payload):
        ctx = capture()
        ctx.cfg = self.storage.get("cfg", {})
        ctx.url = ctx.cfg["price_url"]
        ctx.p_coins = await runner.http(
            ctx.url, method="POST", headers=_HDRS, body=mids_query(""), timeout_ms=5000
        )
        ctx.p_stocks = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("xyz"),
            timeout_ms=5000,
        )
        ctx.p_preipo = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("vntl"),
            timeout_ms=5000,
        )
        return self._on_mark(ctx.p_coins, ctx.p_stocks, ctx.p_preipo)

    def _on_mark(self, p_coins, p_stocks, p_preipo):
        cfg = self.storage["cfg"]
        portfolio = list(self.storage.get("portfolio", []))
        held = [str(p.get("asset", "")).upper() for p in portfolio]
        prices = resolve_prices(
            held, [("", p_coins), ("xyz", p_stocks), ("vntl", p_preipo)]
        )
        prices = self._commit_prices(prices)
        # risk controls: close positions that breach their stop-loss / take-profit
        portfolio = self._enforce_exits(
            portfolio,
            prices,
            float(cfg.get("stop_loss_pct", 0.0)),
            float(cfg.get("take_profit_pct", 0.0)),
        )
        unreal, _m = _mark_book(portfolio, prices)
        runtime.emit_event(
            "paper.marked", {"unrealized_pnl": unreal, "positions": len(portfolio)}
        )
        self._push_command_snapshot()
        return _json_bytes({"unrealized_pnl": unreal, "positions": len(portfolio)})

    def _enforce_exits(self, portfolio, prices, sl, tp):
        # Close any position hitting its stop-loss or take-profit — realize the
        # P&L into history now, rather than waiting for the next rebalance.
        if sl <= 0 and tp <= 0:
            return portfolio
        height = int(runtime.get_block_height())
        realized = float(self.storage.get("realized_pnl", 0.0))
        closed = int(self.storage.get("closed_count", 0))
        wins = int(self.storage.get("win_count", 0))
        history = list(self.storage.get("history", []))
        kept, exits = [], 0
        for p in portfolio:
            cur = prices.get(str(p.get("asset", "")).upper())
            reason = _exit_reason(p.get("side"), p.get("entry_px"), cur, sl, tp)
            if not reason:
                kept.append(p)
                continue
            pnl, rec = _close_at_mark(p, cur, reason, height)
            realized += pnl
            closed += 1
            if pnl > 0:
                wins += 1
            history.insert(0, rec)
            exits += 1
        if exits:
            self.storage["portfolio"] = kept
            self.storage["history"] = history[:50]
            self.storage["realized_pnl"] = realized
            self.storage["closed_count"] = closed
            self.storage["win_count"] = wins
            runtime.emit_event(
                "paper.exits", {"closed": exits, "realized_pnl": realized}
            )
        return kept

    @callable_by(OWNER, decode="raw")
    @deferred
    def start(self, payload):
        self.storage["running"] = True
        cfg = self.storage["cfg"]
        self._schedule("rebalance_tick", int(cfg["rebalance_interval_blocks"]))
        self._schedule("mark_tick", int(cfg["mark_interval_blocks"]))
        runtime.emit_event("paper.started", {})
        return b"ok: running"

    @callable_by(OWNER, decode="raw")
    def stop(self, payload):
        self.storage["running"] = False
        runtime.emit_event("paper.stopped", {})
        return b"ok: stopped"

    def _schedule(self, handler, interval):
        now = int(runtime.get_block_height())
        height = now + int(interval)
        payload = _json_bytes({"_handler": handler})
        # The SDK provides the basic-scheduler fallback for mock hosts.
        runtime.schedule_timer_ex(
            height,
            payload,
            fee_payer=runtime.get_sender(),
            access_limit=TIMER_ACCESS_LIMIT,
        )
        return now

    # ---- views ---------------------------------------------------------- #

    @public(decode="raw")
    @pure
    def get_portfolio(self, payload):
        return _json_bytes(self._portfolio_snapshot())

    @public(decode="raw")
    @pure
    def get_pnl(self, payload):
        return _json_bytes(self._pnl_snapshot())

    @public(decode="raw")
    @pure
    def get_scorecard(self, payload):
        return _json_bytes(self._scorecard_snapshot())

    @public(decode="raw")
    @pure
    def get_stats(self, payload):
        return _json_bytes(
            {
                "running": bool(self.storage.get("running", False)),
                "open_positions": len(self.storage.get("portfolio", [])),
                "rebalances": int(self.storage.get("rebalances", 0)),
                "realized_pnl": float(self.storage.get("realized_pnl", 0.0)),
            }
        )


# Module-level entrypoints — the PVM dispatches to top-level functions by name.


def _get_actor():
    return PaperExecution()


def init(payload):
    return _get_actor().init(payload)


def set_research(payload):
    return _get_actor().set_research(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def apply_ideas(payload):
    return _get_actor().apply_ideas(payload)


def rebalance(payload):
    return _get_actor().rebalance(payload)


def rebalance_tick(payload):
    return _get_actor().rebalance_tick(payload)


def mark_now(payload):
    return _get_actor().mark_now(payload)


def mark_tick(payload):
    return _get_actor().mark_tick(payload)


def start(payload):
    return _get_actor().start(payload)


def stop(payload):
    return _get_actor().stop(payload)


def _apply__resume(payload):
    return _get_actor()._apply__resume(payload)


def _mark__resume(payload):
    return _get_actor()._mark__resume(payload)


def get_portfolio(payload):
    return _get_actor().get_portfolio(payload)


def get_pnl(payload):
    return _get_actor().get_pnl(payload)


def get_scorecard(payload):
    return _get_actor().get_scorecard(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
