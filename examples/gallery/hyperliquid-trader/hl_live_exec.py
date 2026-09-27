"""Live execution actor — Research's ideas, traded for real on Hyperliquid."""

import base64
import json
import math

import hl_common
from cowboy_sdk import (
    OWNER,
    SELF,
    actor,
    callable_by,
    capture,
    deferred,
    public,
    pure,
    runner,
    runtime,
)

keccak = hl_common.keccak
packb = hl_common.packb
_f64_bits = hl_common._f64_bits
action_hash = hl_common.action_hash
hl_digest = hl_common.hl_digest
sign_headers = hl_common.sign_headers
normalize_address = hl_common.normalize_address
_parse = hl_common._parse
_json_bytes = hl_common._json_bytes
_b64_text = hl_common._b64_text
_normalize_result = hl_common._normalize_result
mark_pending = hl_common.mark_pending
tick_pending_guard = hl_common.tick_pending_guard


TESTNET_EXCHANGE = "https://api.hyperliquid-testnet.xyz/exchange"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"


SIGN_KEY_REF = "${HL_AGENT_KEY}"


def order_action(asset, is_buy, price, size, reduce_only=False, tif="Gtc"):
    """Build an HL order action."""
    return {
        "type": "order",
        "orders": [
            {
                "a": int(asset),
                "b": bool(is_buy),
                "p": str(price),
                "s": str(size),
                "r": bool(reduce_only),
                "t": {"limit": {"tif": str(tif)}},
            }
        ],
        "grouping": "na",
    }


def cancel_action(asset, oid):
    return {"type": "cancel", "cancels": [{"a": int(asset), "o": int(oid)}]}


def signed_job(action, nonce):
    """(url, headers, body) for a runner.http call that the runner will sign."""
    body = json.dumps(
        {
            "action": action,
            "nonce": nonce,
            "signature": {"r": "__SIG_R__", "s": "__SIG_S__", "v": "__SIG_V__"},
        }
    )
    url = TESTNET_EXCHANGE
    headers = sign_headers(action, nonce, is_mainnet=False, key_ref=SIGN_KEY_REF)
    return url, headers, body


def _secret_refs(cfg):
    """CBSS secret refs for the signing key."""
    acct = str(cfg.get("secret_account", "") or "")
    return [[acct, "HL_AGENT_KEY"]] if acct else ["HL_AGENT_KEY"]


_HDRS = {"Content-Type": "application/json"}


def _sender_matches(expected):
    """Match an authenticated runtime sender against a configured actor."""
    try:
        wanted = normalize_address(expected)
    except ValueError:
        return False
    return "0x" + runtime.get_sender().hex() == wanted


def _normalize(result):
    """Runner HTTP results arrive as dicts, base64 blobs, or raw text."""
    if result is None:
        return None
    # `await runner.http()` yields a cowboy_sdk RunnerResult, which is
    # deliberately NOT a dict/bytes/str — every branch below missed it and the
    # wrapper was returned unchanged. Callers then did `(meta_resp or {}).get(...)`,
    # and since RunnerResult is truthy with no `.get`, that RAISED and aborted the
    # continuation mid-chain. Same defect as hl_common's _result_text/_normalize_result.
    if not isinstance(result, (dict, bytes, bytearray, str)):
        unwrapped = getattr(result, "raw", None)
        if unwrapped is not None:
            result = unwrapped
    if isinstance(result, dict):
        if "raw" in result:
            return _normalize(result["raw"])
        for key in ("data", "text", "body"):
            if key in result:
                return _normalize(result[key])
        return result
    if isinstance(result, bytes):
        result = result.decode("utf-8", "replace")
    if isinstance(result, str):
        txt = result
        try:
            import base64 as _b64

            txt = _b64.b64decode(result).decode("utf-8", "replace")
        except Exception:
            pass
        try:
            return json.loads(txt)
        except Exception:
            try:
                return json.loads(result)
            except Exception:
                return None
    return result


def meta_query():
    """Body for the /info `meta` call — returns the perp universe."""
    return json.dumps({"type": "meta"})


def parse_universe(meta_resp):
    """{SYMBOL: {"a": <asset index>, "sz": <szDecimals>}} from /info meta."""
    out = {}
    uni = (meta_resp or {}).get("universe") or []
    for idx, item in enumerate(uni):
        name = str((item or {}).get("name", "")).upper()
        if not name:
            continue
        out[name] = {
            "a": idx,
            "sz": int((item or {}).get("szDecimals", 0) or 0),
            "venue": "core",
        }
    return out


XYZ_DEX_INDEX = 65
XYZ_DEX_BASE = 100000 + XYZ_DEX_INDEX * 10000


def dex_meta_query(dex):
    """Body for the /info `meta` call scoped to a builder dex."""
    return json.dumps({"type": "meta", "dex": dex})


def parse_dex_universe(meta_resp, base, prefix):
    """Builder-dex universe -> {SYMBOL: {"a": base+idx, "sz": szDecimals}}."""
    out = {}
    uni = (meta_resp or {}).get("universe") or []
    pfx = (prefix or "").upper() + ":"
    for idx, item in enumerate(uni):
        name = str((item or {}).get("name", "")).upper()
        if name.startswith(pfx):
            name = name[len(pfx) :]
        if not name:
            continue
        out[name] = {
            "a": base + idx,
            "sz": int((item or {}).get("szDecimals", 0) or 0),
            "venue": str(prefix or "builder").lower(),
        }
    return out


def _fmt_decimal(value, decimals):
    text = "%.*f" % (int(decimals), value)
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def fmt_size(qty, sz_decimals):
    """Round a size DOWN to the asset's szDecimals."""
    q = float(qty)
    if q <= 0:
        return "0"
    step = 10 ** int(sz_decimals)

    scaled = round(q * step, 6)
    truncated = int(scaled) / float(step)
    if truncated <= 0:
        return "0"
    return _fmt_decimal(truncated, sz_decimals)


def fmt_close_size(qty, sz_decimals):
    """Size for a reduce-only close, rounded UP to the asset's tick."""
    q = float(qty)
    if q <= 0:
        return "0"
    step = 10 ** int(sz_decimals)
    scaled = q * step
    n = int(scaled)
    if scaled - n > 1e-9:
        n += 1
    if n <= 0:
        n = 1
    out = n / float(step)
    return _fmt_decimal(out, sz_decimals)


def fmt_price(px, is_buy, sz_decimals, slip_bps=200):
    """A marketable limit price, legal under HL's tick rules."""
    raw = float(px) * (1.0 + (slip_bps / 10000.0) * (1.0 if is_buy else -1.0))
    if raw <= 0:
        return "0"
    sig5 = float("%.5g" % raw)
    max_dec = max(0, 6 - int(sz_decimals))
    q = round(sig5, max_dec)
    return _fmt_decimal(q, max_dec)


def parse_fill(resp):
    """(filled_size, avg_px, reject_reason) from an HL order response.

    `reject_reason` is None on a genuine (unfilled-but-accepted) response and a
    short string when the venue REFUSED the order. Distinguishing those two is
    the whole point: previously both returned `(0.0, None)`, so a refusal was
    indistinguishable from a zero fill everywhere downstream. On 2026-08-10 that
    hid 34 consecutive rejections across ~8.5h — the book simply showed fewer
    positions than were planned, and no surface reported anything wrong.

    TWO distinct refusal shapes, and only checking the first misses the other:
      1. `response.data.statuses[].error`  -- venue-level, e.g. no liquidity
         ("Order could not immediately match against any resting orders")
      2. top-level `status == "err"`       -- request-level, e.g. transport,
         auth, or a duplicate nonce. Note this arrives with NO `statuses` array
         at all, so any parser that only walks statuses silently sees nothing.
    """
    empty = (0.0, None, None)
    if not isinstance(resp, dict):
        return empty
    # Shape 2 first: it has no `statuses`, so walking them would report "no fill"
    # with no reason and lose the error text entirely.
    if str(resp.get("status", "")).lower() == "err":
        return 0.0, None, str(resp.get("response"))[:200]
    try:
        statuses = ((resp or {}).get("response") or {}).get("data", {}).get(
            "statuses"
        ) or []
    except Exception:
        return empty
    total, notional, reject = 0.0, 0.0, None
    for st in statuses:
        if not isinstance(st, dict):
            continue
        if "error" in st and reject is None:
            reject = str(st.get("error"))[:200]
            continue
        f = st.get("filled")
        if not isinstance(f, dict):
            continue
        try:
            sz = float(f.get("totalSz", 0) or 0)
            px = float(f.get("avgPx", 0) or 0)
        except Exception:
            continue
        if sz > 0:
            total += sz
            notional += sz * px
    if total <= 0:
        return 0.0, None, reject
    # A partially-filled-partially-rejected batch keeps BOTH facts.
    return total, notional / total, reject


def order_outcome(filled, reject_reason, expected_size):
    if reject_reason:
        return "rejected"
    if filled <= 0:
        return "unknown"
    return "partial_fill" if filled + 1e-12 < float(expected_size) else "filled"


MIN_ORDER_NOTIONAL = 10.0


def user_state_query(address, dex=""):
    """Body for a core or builder-dex clearinghouse state read."""
    q = {"type": "clearinghouseState", "user": address}
    if dex:
        q["dex"] = dex
    return json.dumps(q)


def spot_state_query(address):
    """Body for the unified-account balance/hold source of truth."""
    return json.dumps({"type": "spotClearinghouseState", "user": address})


def parse_equity(resp):
    """Account value in USD from a clearinghouseState response, or None."""
    d = _normalize(resp)
    if not isinstance(d, dict):
        return None
    try:
        return float(((d.get("marginSummary") or {}).get("accountValue")))
    except Exception:
        return None


def parse_spot_usdc(resp):
    """Unified-account USDC equity from spotClearinghouseState, or None.

    `0` is a valid, authoritative balance.  It must not be treated as a failed
    read and replaced by the configured trading budget.
    """
    d = _normalize(resp)
    if not isinstance(d, dict) or not isinstance(d.get("balances"), list):
        return None
    for bal in d.get("balances", []):
        if isinstance(bal, dict) and str(bal.get("coin", "")).upper() == "USDC":
            try:
                return float(bal.get("total", 0) or 0)
            except Exception:
                return None
    return 0.0


def parse_spot_account(resp):
    """Unified USDC total, hold, and post-maintenance availability."""
    d = _normalize(resp)
    if not isinstance(d, dict) or not isinstance(d.get("balances"), list):
        return None
    total, hold = 0.0, 0.0
    for bal in d.get("balances", []):
        if isinstance(bal, dict) and str(bal.get("coin", "")).upper() == "USDC":
            try:
                total = float(bal.get("total", 0) or 0)
                hold = float(bal.get("hold", 0) or 0)
            except Exception:
                return None
            break
    available = None
    for pair in d.get("tokenToAvailableAfterMaintenance", []) or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and int(pair[0]) == 0:
            try:
                available = float(pair[1])
            except Exception:
                pass
            break
    if available is None:
        available = max(0.0, total - hold)
    return {"total": total, "hold": hold, "available": available}


def parse_venue_positions(states, previous=None):
    """Core + builder-dex states -> the executor's canonical position book.

    Hyperliquid is authoritative for exposure.  Local thesis metadata is kept
    when a matching position already exists; venue-only positions are imported
    with conservative defaults so they can be closed instead of being ignored.
    """
    old = {str(p.get("asset", "")).upper(): p for p in (previous or [])}
    out = []
    for item in states or []:
        venue, state = (
            item
            if isinstance(item, (list, tuple)) and len(item) == 2
            else ("core", item)
        )
        d = _normalize(state)
        if not isinstance(d, dict):
            continue
        for wrapper in d.get("assetPositions", []) or []:
            pos = (wrapper or {}).get("position") if isinstance(wrapper, dict) else None
            if not isinstance(pos, dict):
                continue
            try:
                signed_qty = float(pos.get("szi", 0) or 0)
                entry = float(pos.get("entryPx", 0) or 0)
            except Exception:
                continue
            if abs(signed_qty) <= 1e-12 or entry <= 0:
                continue
            sym = str(pos.get("coin", "")).split(":")[-1].upper()
            if not sym:
                continue
            prior = old.get(sym) or {}
            qty = abs(signed_qty)
            out.append(
                {
                    "asset": sym,
                    "side": "long" if signed_qty > 0 else "short",
                    "entry_px": entry,
                    "qty": qty,
                    "notional": qty * entry,
                    "confidence": int(prior.get("confidence", 0) or 0),
                    "opened_block": int(prior.get("opened_block", 0) or 0),
                    "venue": str(venue),
                }
            )
    return out


#: Deep, continuously-quoted core perps. A marketable limit only needs to cross a
#: spread of a few basis points here, so the general slip is pure giveaway: every
#: order pays it, and on BTC/ETH that is ~2% of edge surrendered per round trip
#: for nothing. Kept deliberately short and conservative — a symbol wrongly listed
#: here gets a limit too tight to fill, which shows up immediately as a refusal
#: and is recovered by the feasibility gate rather than silently costing money.
MAJOR_SYMBOLS = ("BTC", "ETH", "SOL")


def slip_for(sym, slip_bps, slip_bps_major):
    """Per-asset-class aggressiveness.

    Widening slip does NOT rescue an empty book: on 2026-08-10 the equity perps
    were sent at 0.97-2.04% THROUGH the mark and refused 34/34, while core perps
    filled every time on the same code path in the same block. Price was never
    the differing variable there, so this exists to stop OVERPAYING on liquid
    assets, not to chase fills on illiquid ones — that is the feasibility gate's
    job.
    """
    if str(sym).upper() in MAJOR_SYMBOLS:
        return int(slip_bps_major)
    return int(slip_bps)


#: Blocks before a condemned symbol is probed again. Without a retry window this
#: check is SELF-SEALING: suppressing opens freezes `sent`, so `filled` can never
#: become non-zero and the verdict can never be revisited. Genuinely dead assets
#: simply fail again and re-condemn (cost: one order per window), while a symbol
#: refused transiently — e.g. the duplicate-nonce rejection seen 2026-08-11 — gets
#: back in. Deliberately long: retrying a dead asset is pure waste.
UNFILLABLE_RETRY_BLOCKS = 5003


def unfillable(
    sym, feasibility, min_attempts=3, height=0, retry_blocks=UNFILLABLE_RETRY_BLOCKS
):
    """True when the venue has consistently refused this symbol, and it is not
    yet due for a re-probe.

    Purely data-driven, so it needs no venue-specific knowledge and covers any
    future listing with the same problem. A symbol is only condemned after
    `min_attempts` tries with a 100% refusal rate — one bad fill must not
    blacklist an asset that normally trades. Observed 2026-08-10:
    MSFT/AMZN/META were 34/34 refused while every core-perp order filled, so the
    separation is clean.
    """
    rec = (feasibility or {}).get(sym)
    if not isinstance(rec, dict):
        return False
    sent = int(rec.get("sent", 0))
    filled = int(rec.get("filled", 0))
    if not (sent >= int(min_attempts) and filled == 0):
        return False
    last = int(rec.get("last_reject_block", 0) or 0)
    if last and int(height) - last >= int(retry_blocks):
        return False  # due for a re-probe
    return True


def plan_orders(
    target,
    held,
    universe,
    prices,
    budget,
    slip_bps=200,
    height=0,
    min_hold_blocks=0,
    flip_margin=0,
    skips=None,
    feasibility=None,
    slip_bps_major=None,
    close_absent=False,
    max_hold_blocks=0,
    max_positions=None,
):
    """Diff target book vs held -> list of orders. Pure; no I/O, no storage.

    Two dampers, because synthesis re-opines every synth_interval and a fresh LLM
    macro call on a rolling news window is not stable between runs. Without them the
    book whipsaws: an asset is opened, dropped from the next idea list, closed, then
    re-opened on the opposite side minutes later.

      min_hold_blocks — never close a position younger than this.
      flip_margin     — only reverse a position if the opposing conviction beats the
                        held one by this many points.

    Also fixes a latent no-op: previously a symbol that was BOTH held and in target on
    the OPPOSITE side hit `continue` in both loops, so the book silently kept a
    position contradicting the current thesis. Reversals are now explicit.

    `skips` — optional list; every symbol NOT acted on is appended as
    {"sym", "stage", "reason"}. Stays pure (no I/O, no storage): the caller emits.
    Every `continue` below used to be silent, which made "why didn't it trade X?"
    unanswerable after the fact — the question that cost hours on 2026-08-09 when
    BTC and GOLD were recommended at conf 68/72 and never opened.

    NOTE the close loop skips a held position whose symbol is missing from
    `universe`/`prices`. That position then cannot be closed OR flipped by any
    later run — it is stuck until the universe cache has it again. Recorded as
    `unclosable_not_in_universe` / `unclosable_no_price` because it is the most
    dangerous of these: real exposure with no exit path.
    """
    _major_slip = slip_bps if slip_bps_major is None else slip_bps_major

    def _skip(sym, stage, reason):
        if skips is not None:
            skips.append({"sym": sym, "stage": stage, "reason": reason})

    orders = []
    held_syms = {str(p.get("asset", "")).upper(): p for p in held}
    flipping = []

    for sym, pos in held_syms.items():
        u = universe.get(sym)
        px = prices.get(sym)
        if not u or px is None:
            # Held with no way to price/route an exit — stuck until this resolves.
            _skip(
                sym,
                "close",
                "unclosable_not_in_universe" if not u else "unclosable_no_price",
            )
            continue
        qty = float(pos.get("qty", 0) or 0)
        if qty <= 0:
            _skip(sym, "close", "zero_qty")
            continue
        age = int(height) - int(pos.get("opened_block", 0) or 0)
        held_long = str(pos.get("side", "")).lower() == "long"
        tgt = target.get(sym)

        if tgt is None:
            if int(max_hold_blocks) > 0 and age >= int(max_hold_blocks):
                why = "stale"
            elif not close_absent:
                _skip(sym, "close", "absent_from_cycle_hold")
                continue
            elif age < int(min_hold_blocks):
                _skip(sym, "close", "younger_than_min_hold")
                continue  # too young to churn out
            else:
                why = "close"
        else:
            if (str(tgt.get("side", "")).lower() == "long") == held_long:
                _skip(sym, "close", "same_side_as_thesis")
                continue  # same side — leave it alone
            if age < int(min_hold_blocks):
                _skip(sym, "close", "younger_than_min_hold")
                continue
            edge = int(tgt.get("confidence", 0) or 0) - int(
                pos.get("confidence", 0) or 0
            )
            if edge < int(flip_margin):
                _skip(sym, "close", "flip_edge_below_margin")
                continue  # not a strong enough reversal
            why = "flip"
            flipping.append(sym)

        orders.append(
            {
                "sym": sym,
                "a": u["a"],
                "b": (not held_long),
                "p": fmt_price(
                    px, not held_long, u["sz"], slip_for(sym, slip_bps, _major_slip)
                ),
                "s": fmt_close_size(qty, u["sz"]),
                "r": True,
                "why": why,
                "venue": str(u.get("venue", "core")),
                # Internal-only dispatch guard: an opening order may consume
                # this slot only after the reduce-only leg actually closes it.
                "_position_qty": qty,
            }
        )

    # `budget` is the capital still available for NEW exposure.  Do not dilute
    # it across targets that are already held: doing so stranded a $25 opening
    # budget as three ~$8 allocations when BTC occupied one of three target
    # slots, leaving every missing position below the venue's $10 minimum.
    # Also exclude targets that cannot currently produce an order so one dead
    # route does not starve the remaining executable candidates.
    openable = []
    for sym, tgt in target.items():
        if sym in held_syms and sym not in flipping:
            continue
        u = universe.get(sym)
        px = prices.get(sym)
        if not u or px is None or float(px) <= 0:
            continue
        if unfillable(sym, feasibility, height=height):
            continue
        openable.append(sym)
    opening_conf = (
        sum(int(target[sym].get("confidence", 0) or 0) for sym in openable) or 1
    )
    closing_syms = {str(order.get("sym", "")).upper() for order in orders}
    projected_held = max(0, len(held_syms) - len(closing_syms))
    open_slots = (
        max(0, int(max_positions) - projected_held)
        if max_positions is not None
        else len(openable)
    )
    opened_new_symbols = 0
    for sym, tgt in target.items():
        if sym in held_syms and sym not in flipping:
            _skip(sym, "open", "already_held")
            continue
        if sym not in flipping and opened_new_symbols >= open_slots:
            _skip(sym, "open", "max_positions")
            continue
        u = universe.get(sym)
        px = prices.get(sym)
        if not u or px is None or float(px) <= 0:
            _skip(sym, "open", "not_in_universe" if not u else "no_price")
            continue
        # Only gates OPENS. A held position must still be closable even if the
        # venue has been refusing new entries — refusing to plan its exit would
        # convert an unfillable symbol into a permanently stuck position, which
        # is strictly worse than the problem being avoided.
        if unfillable(sym, feasibility, height=height):
            _skip(sym, "open", "venue_refuses")
            continue
        is_buy = str(tgt.get("side", "")).lower() == "long"
        notional = float(budget) * (int(tgt.get("confidence", 0) or 0) / opening_conf)
        if notional < MIN_ORDER_NOTIONAL:
            _skip(sym, "open", "below_min_notional")
            continue
        size = fmt_size(notional / float(px), u["sz"])
        if size == "0":
            _skip(sym, "open", "size_rounds_to_zero")
            continue
        orders.append(
            {
                "sym": sym,
                "a": u["a"],
                "b": is_buy,
                "p": fmt_price(
                    px, is_buy, u["sz"], slip_for(sym, slip_bps, _major_slip)
                ),
                "s": size,
                "r": False,
                "why": "open",
                "venue": str(u.get("venue", "core")),
            }
        )
        if sym not in flipping:
            opened_new_symbols += 1
    return orders


HL_INFO = TESTNET_INFO
_HDRS = {"Content-Type": "application/json"}


def mids_query(dex):
    q = {"type": "allMids"}
    if dex:
        q["dex"] = dex
    return _json_bytes(q)


def resolve_prices(symbols, dex_results):
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


# `thesis_fit` is assigned mechanically by research (_thesis_fit), not by the
# model: an idea "aligns" only when its asset or theme string is already a member
# of the standing thesis basket, "contradicts" when it takes the opposite side of
# a basket member, and "neutral" in every other case — which is to say, for every
# asset the thesis has not already named.
#
# Gating selection on "aligns" therefore made the book a fixed point. The thesis
# basket is built from what is already held, so the only ideas that could ever be
# selected were the ones describing the current positions. Observed on river
# run8: research produced ETH/BTC/SOL (aligns, all three already held) plus
# MSFT/AMZN (neutral, discarded here) for hours. Every rebalance succeeded,
# computed target == held, and planned zero orders. The system reported perfect
# health while being structurally incapable of opening a new position, and
# `last_prices` collapsed to the three held symbols because `_apply` only ever
# resolves prices for target-union-held.
#
# "contradicts" is still refused: an idea that fights the standing thesis should
# not reach the book. "neutral" is exactly where every genuinely new position
# comes from, so it must be selectable. `min_conf` and `top_n` remain the quality
# and size gates.
_SELECTABLE_FIT = ("aligns", "neutral")


def _select_ideas(ideas, top_n, min_conf):
    pool = [
        i
        for i in (ideas or [])
        if isinstance(i, dict)
        and str(i.get("side", "")).lower() in ("long", "short")
        and str(i.get("thesis_fit", "")).lower() in _SELECTABLE_FIT
        and int(i.get("confidence", 0) or 0) >= int(min_conf)
    ]
    # Aligned ideas still win ties: sort by (fit, confidence) so a thesis-backed
    # idea outranks an equally-confident unrelated one when top_n truncates.
    pool.sort(
        key=lambda i: (
            str(i.get("thesis_fit", "")).lower() == "aligns",
            _by_confidence(i),
        ),
        reverse=True,
    )
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

    if cur_px is None:
        return None
    r = _pos_return(side, entry_px, cur_px)
    if sl and float(sl) > 0 and r <= -float(sl):
        return "stop_loss"
    if tp and float(tp) > 0 and r >= float(tp):
        return "take_profit"
    return None


def _close_from_fill(position, fill, reason, height):
    """Realize a possibly partial close from a confirmed venue fill."""
    entry = float(position.get("entry_px", 0) or 0)
    held = float(position.get("qty", 0) or 0)
    part, exit_px = fill["qty"], fill["avg_px"]
    remainder = None
    if held - part > 1e-12:
        remainder = dict(position)
        remainder["qty"] = held - part
        remainder["notional"] = (held - part) * entry
    sign = 1.0 if str(position.get("side", "")).lower() == "long" else -1.0
    pnl = sign * (float(exit_px) - entry) * float(part)
    record = dict(position)
    record["exit_px"] = exit_px
    record["pnl"] = pnl
    record["closed_qty"] = part
    record["closed_block"] = height
    record["exit_reason"] = reason
    return remainder, pnl, record


def plan_book(
    portfolio, target, prices, close_absent=False, height=0, max_hold_blocks=0
):

    keep, close = [], []
    for p in portfolio or []:
        asset = str(p.get("asset", "")).upper()
        tgt = target.get(asset)
        if tgt and str(tgt.get("side", "")).lower() == str(p.get("side", "")).lower():
            keep.append(p)
            continue
        if not tgt:
            age = int(height) - int(p.get("opened_block", 0) or 0)
            stale = int(max_hold_blocks) > 0 and age >= int(max_hold_blocks)
            if not close_absent and not stale:
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
COMMAND_SNAPSHOT_ACCESS_LIMIT = 375
ACTOR_CALL_GAS_LIMIT = 2_000_000
COMMAND_SNAPSHOT_MAX_IDEAS = 6
COMMAND_SNAPSHOT_MAX_RATIONALE_CHARS = 96
COMMAND_SNAPSHOT_MAX_POSITIONS = 8
# Research normally synthesizes every 599 blocks. Stop opening or reshaping
# positions after three missed snapshots; the independent mark/risk loop keeps
# managing the existing book.
IDEAS_MAX_AGE_BLOCKS = 1800


@actor
class LiveExecution:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        if self.storage.get("cfg"):
            return b"ok: already initialized"
        self.storage["__name__"] = "HL Live Execution"
        self.storage["__description__"] = (
            "Trade Research's ideas live on Hyperliquid with a CBSS-held key"
        )
        self.storage["__version__"] = "3.0.8"
        obj = _parse(payload)
        if "running" in obj:
            raise ValueError("running is controlled by the start handler")
        requested_price_url = str(obj.get("price_url", TESTNET_INFO))
        if requested_price_url != TESTNET_INFO:
            raise ValueError(
                "price_url must be the fixed Hyperliquid testnet info endpoint"
            )
        self.storage["cfg"] = {
            # Prices, metadata, account state, signatures, and orders must all
            # refer to the same venue.  This actor signs testnet orders.
            "price_url": TESTNET_INFO,
            # Actor-to-actor messages execute in Canyon's fixed 100k deferred
            # lane. A full research snapshot can exceed that budget, so a
            # runner callback supplies the larger execution lane in which the
            # trader synchronously reads the research actor's pure snapshot.
            "research_pull": bool(obj.get("research_pull", False)),
            "budget": float(obj.get("budget", 1000.0)),
            "max_positions": int(obj.get("max_positions", 10)),
            "min_confidence": int(obj.get("min_confidence", 60)),
            "mark_interval_blocks": int(obj.get("mark_interval_blocks", 293)),
            # Dampers against synthesis whipsaw (see plan_orders). ~1200 blocks is
            # roughly 25-30 min at observed block rates — longer than one rebalance
            # cycle, so a position survives at least one fresh LLM opinion.
            "min_hold_blocks": int(obj.get("min_hold_blocks", 1200)),
            # A single synthesis omitting an asset is not an exit signal. Keep
            # the position unless research explicitly reverses it or it reaches
            # this maximum age; the independent risk loop still enforces SL/TP.
            "close_absent": bool(obj.get("close_absent", False)),
            "max_hold_blocks": int(obj.get("max_hold_blocks", 14407)),
            "flip_margin": int(obj.get("flip_margin", 15)),
            # near-static metadata: refresh rarely, and off-phase from the other loops
            "universe_interval_blocks": int(obj.get("universe_interval_blocks", 4001)),
            "rebalance_interval_blocks": int(obj.get("rebalance_interval_blocks", 601)),
            # MUST be >= the largest `timeout_blocks` on a continuation this
            # guards (_apply 900, _mark 800), because `_tick_ok` uses THIS value
            # — not timeout_blocks — to decide whether a loop is still in flight.
            # At 300 against a 900-block continuation the guard expired first, so
            # a second _apply launched on top of the first still-running one, and
            # a third at 600: a sequential retry storm turned concurrent.
            "max_poll_wait_blocks": int(obj.get("max_poll_wait_blocks", 7000)),
            # Wall-clock recovery bound. Block-denominated guards stretch as the
            # chain decays (7000 blocks = ~26 min fresh, ~7.2 h at 0.27 blk/s),
            # so this is the bound that actually releases a lost continuation.
            "max_poll_wait_ms": int(obj.get("max_poll_wait_ms", 1_800_000)),
            "secret_account": str(obj.get("secret_account", "") or ""),
            "slip_bps": int(obj.get("slip_bps", 100)),
            # Tighter tier for deep core perps. Defaults to `slip_bps`, so an
            # actor deployed without it behaves exactly as before.
            "slip_bps_major": int(obj.get("slip_bps_major", obj.get("slip_bps", 100))),
            "account_addr": normalize_address(
                obj.get("account_addr", ""), "account_addr"
            ),
            "equity_pct": float(obj.get("equity_pct", 0.80)),
            "stop_loss_pct": float(obj.get("stop_loss_pct", 0.03)),
            "take_profit_pct": float(obj.get("take_profit_pct", 0.0)),
            "max_cumulative_loss_pct": float(
                obj.get("max_cumulative_loss_pct", 0.10)
            ),
            "max_exit_price_retries": int(obj.get("max_exit_price_retries", 3)),
        }
        self.storage["research_addr"] = normalize_address(
            obj.get("research_addr", ""), "research_addr"
        )
        self.storage["portfolio"] = []
        self.storage["history"] = []
        self.storage["last_prices"] = {}
        self.storage["realized_pnl"] = 0.0
        self.storage["closed_count"] = 0
        self.storage["win_count"] = 0
        self.storage["rebalances"] = 0
        self.storage["latest_ideas"] = []
        self.storage["ideas_snapshot_block"] = 0
        self.storage["cached_research_stats"] = {}
        notify_addr = str(obj.get("notify_addr", "") or "")
        self.storage["notify_addr"] = (
            normalize_address(notify_addr, "notify_addr") if notify_addr else ""
        )
        # A deploy-time boolean must not claim that timers were armed. Starting
        # is a separate owner action with an observable result.
        self.storage["running"] = False
        self.storage["apply_pending"] = False
        self.storage["post_reconcile_pending"] = False
        self.storage["mark_price_retry_symbols"] = []
        self.storage["mark_price_retry_attempts"] = 0
        self.storage["risk_halted"] = False
        self.storage["risk_halt_reason"] = ""
        self.storage["cumulative_loss_baseline_pnl"] = 0.0
        self.storage["pending_apply"] = {}
        self.storage["apply_generation"] = 0
        self.storage["nonce_block_ms"] = 0
        self.storage["nonce_seq"] = 0
        runtime.emit_event("live.init", {"budget": self.storage["cfg"]["budget"]})
        return b"ok: initialized"

    def _commit_prices(self, prices):
        """Merge fresh prices into last_prices and return the merged map."""
        merged = dict(self.storage.get("last_prices", {}))
        if prices:
            merged.update(prices)
            self.storage["last_prices"] = merged
        return merged

    def _record_account_state(self, spot_resp, core_resp, xyz_resp):
        """Reconcile unified equity and actual venue exposure before planning."""
        height = int(runtime.get_block_height())
        if not self.storage.get("cfg", {}).get("account_addr"):
            runtime.emit_event(
                "live.venue_reconcile_failed",
                {
                    "reason": "no_account",
                },
            )
            return False
        account = parse_spot_account(spot_resp)
        core_doc = _normalize(core_resp)
        xyz_doc = _normalize(xyz_resp)
        core_valid = isinstance(core_doc, dict) and isinstance(
            core_doc.get("assetPositions"), list
        )
        xyz_valid = isinstance(xyz_doc, dict) and isinstance(
            xyz_doc.get("assetPositions"), list
        )
        if account is None or not core_valid or not xyz_valid:
            runtime.emit_event(
                "live.venue_reconcile_failed",
                {
                    "spot_valid": account is not None,
                    "core_valid": core_valid,
                    "xyz_valid": xyz_valid,
                },
            )
            return False
        eq = account.get("total")
        self.storage["last_equity"] = eq
        positions = parse_venue_positions(
            [("core", core_doc), ("xyz", xyz_doc)], self.storage.get("portfolio", [])
        )
        # A replacement actor inherits the venue book but has no local open
        # timestamp. Treat discovery as the start of its holding window rather
        # than immediately declaring every inherited position stale.
        for p in positions:
            if int(p.get("opened_block", 0) or 0) <= 0:
                p["opened_block"] = height
        self.storage["portfolio"] = positions
        counts = {"core": 0, "xyz": 0}
        for p in positions:
            venue = str(p.get("venue", "core"))
            counts[venue] = int(counts.get(venue, 0)) + 1
        self.storage["account_state"] = {
            "mode": "unified",
            "network": "testnet",
            "equity": eq,
            "spot_usdc": eq,
            "core_equity": parse_equity(core_doc),
            "xyz_equity": parse_equity(xyz_doc),
            "hold": account.get("hold"),
            "available": account.get("available"),
            "positions_by_venue": counts,
            "reconciled_block": height,
        }
        runtime.emit_event(
            "live.venue_reconciled",
            {
                "positions": len(positions),
                "equity": eq,
                "equity_valid": eq is not None,
            },
        )
        return True

    def _target_from(self, selected):
        """Ideas -> {SYMBOL: {side, confidence}}. Shared by paper and live so the."""
        target = {}
        for i in selected:
            asset = str(i.get("asset", "")).upper()
            if asset and asset not in target:
                target[asset] = {
                    "side": str(i.get("side", "")).lower(),
                    "confidence": int(i.get("confidence", 0) or 0),
                }
        return target

    def _combined_universe(self, core_meta, xyz_meta):
        """Core perp universe + the xyz builder dex (equities/commodities),."""
        uni = parse_dex_universe(_normalize(xyz_meta), XYZ_DEX_BASE, "xyz")
        uni.update(parse_universe(_normalize(core_meta)))
        return uni

    def _live_plan(self, selected, prices, universe):
        """Venue orders for this rebalance, or [] in paper mode."""
        cfg = self.storage.get("cfg", {})
        if not universe:
            return []
        held = []
        for p in self.storage.get("portfolio", []):
            held.append(
                {
                    "asset": str(p.get("asset", "")).upper(),
                    "side": str(p.get("side", "")).lower(),
                    "qty": float(p.get("qty", 0) or 0),
                    # needed by the min-hold / flip-margin dampers
                    "opened_block": int(p.get("opened_block", 0) or 0),
                    "confidence": int(p.get("confidence", 0) or 0),
                }
            )
        # The budget caps TOTAL venue exposure, not merely this cycle's new
        # orders.  A reconciled legacy position above the cap must consume all
        # opening capacity while remaining fully eligible for reduce-only exits.
        gross_held = 0.0
        for p in self.storage.get("portfolio", []):
            sym = str(p.get("asset", "")).upper()
            mark = prices.get(sym)
            px = float(mark if mark is not None else p.get("entry_px", 0) or 0)
            gross_held += abs(float(p.get("qty", 0) or 0) * px)
        opening_budget = max(0.0, self._deployable(cfg) - gross_held)
        skips = []
        planned = plan_orders(
            self._target_from(selected),
            held,
            universe,
            prices,
            opening_budget,
            int(cfg.get("slip_bps", 200)),
            int(runtime.get_block_height()),
            int(cfg.get("min_hold_blocks", 1200)),
            int(cfg.get("flip_margin", 15)),
            skips,
            self.storage.get("exec_feasibility", {}),
            int(cfg.get("slip_bps_major", cfg.get("slip_bps", 100))),
            bool(cfg.get("close_absent", False)),
            int(cfg.get("max_hold_blocks", 14407)),
            int(cfg.get("max_positions", 10)),
        )
        # Emit WHY nothing was ordered. A rebalance that plans zero orders is
        # indistinguishable from a broken one without this — it completes, bumps
        # the counter, and places nothing. `unclosable_*` is the one to alarm on:
        # it means real exposure with no exit path.
        if skips:
            stuck = [k for k in skips if k["reason"].startswith("unclosable")]
            runtime.emit_event(
                "live.plan_skips",
                {
                    "planned": len(planned),
                    "skipped": len(skips),
                    "reasons": skips[:12],
                    "unclosable": len(stuck),
                },
            )
        return planned[:3]

    def _deployable(self, cfg):
        """Notional to deploy, with ``budget`` enforced as a hard ceiling.

        Venue reconciliation makes spot equity authoritative, but it must not
        silently replace the operator's explicit risk budget.  The previous
        implementation did exactly that: a $50 actor attached to a $900 account
        could plan roughly $720 of orders at the default 80% equity fraction.
        """
        budget = max(0.0, float(cfg.get("budget", 1000.0)))
        eq = self.storage.get("last_equity")
        if eq is None:
            return budget
        equity_pct = max(0.0, min(1.0, float(cfg.get("equity_pct", 0.80))))
        return min(budget, max(0.0, float(eq)) * equity_pct)

    def _slot(self, plan, i, info_url, info_body):
        """(url, headers, body) for order slot `i`; a plain /info read if unused."""
        if i >= len(plan):
            return info_url, _HDRS, info_body
        o = plan[i]
        action = order_action(
            o["a"], o["b"], o["p"], o["s"], reduce_only=o["r"], tif="Ioc"
        )
        return signed_job(action, self._next_nonce())

    def _next_nonce(self):
        """Reserve a strictly-increasing millisecond nonce.

        Store the actual last value, not a block-clock base plus a separate
        sequence.  Continuations can resume in distinct blocks that expose the
        same consensus millisecond; persisting only the base made two staged
        orders reuse that millisecond and Hyperliquid rejected the second one.
        """
        now = int(runtime.get_timestamp_ms())
        last = int(self.storage.get("nonce_block_ms", 0))
        nonce = max(now, last + 1)
        self.storage["nonce_block_ms"] = nonce
        self.storage["nonce_seq"] = 0  # retained for state compatibility
        return nonce

    @callable_by(OWNER, decode="raw")
    def set_research(self, payload):
        addr = normalize_address(
            _parse(payload).get("research_addr", ""), "research_addr"
        )
        self.storage["research_addr"] = addr
        self.storage["latest_ideas"] = []
        self.storage["ideas_snapshot_block"] = 0
        self.storage["cached_research_stats"] = {}
        runtime.emit_event("live.research_set", {"addr": addr})
        return _json_bytes({"research_addr": addr})

    @callable_by(OWNER, decode="raw")
    @deferred
    def set_notify(self, payload):
        """Bind the command notifier independently of research push delivery."""
        addr = normalize_address(_parse(payload).get("notify_addr", ""), "notify_addr")
        self.storage["notify_addr"] = addr
        self._schedule_command_snapshot()
        runtime.emit_event("live.notify_set", {"addr": addr})
        return _json_bytes({"notify_addr": addr})

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
        notify_addr = str(obj.get("notify_addr", "") or "")
        if notify_addr:
            self.storage["notify_addr"] = normalize_address(notify_addr, "notify_addr")
        runtime.emit_event("live.ideas_received", {"count": len(ideas)})
        return b"stored"

    @callable_by(OWNER, decode="raw")
    @deferred
    def apply_ideas(self, payload):
        blocked = self._order_entry_blocked()
        if blocked:
            return blocked
        self._begin_apply()
        return self._apply(payload)

    @callable_by(OWNER, SELF, decode="raw")
    @deferred
    def rebalance(self, payload):
        blocked = self._order_entry_blocked()
        if blocked:
            return blocked
        return self._do_rebalance()

    def _configuration_error(self):
        cfg = self.storage.get("cfg", {})
        if not cfg.get("account_addr"):
            return b"unconfigured: account_addr required"
        budget = float(cfg.get("budget", 0.0))
        if not math.isfinite(budget) or budget <= 0:
            return b"unconfigured: budget must be finite and greater than zero"
        stop_loss = float(cfg.get("stop_loss_pct", 0.0))
        if not math.isfinite(stop_loss) or stop_loss <= 0:
            return b"unconfigured: stop_loss_pct must be greater than zero"
        max_loss = float(cfg.get("max_cumulative_loss_pct", 0.0))
        if not math.isfinite(max_loss) or not 0 < max_loss <= 1:
            return b"unconfigured: max_cumulative_loss_pct must be in (0, 1]"
        return None

    def _risk_halt_error(self):
        if not self.storage.get("risk_halted"):
            return None
        reason = str(self.storage.get("risk_halt_reason", "risk control"))
        return ("risk halted: " + reason).encode("utf-8")

    def _abort_apply(self, stage):
        """Release staged entry work without disturbing the mark/exit loop."""
        self.storage["apply_pending"] = False
        self.storage["post_reconcile_pending"] = False
        self.storage["pending_apply"] = {}
        runtime.emit_event("live.apply_aborted", {"stage": str(stage)})

    def _order_entry_blocked(self):
        error = self._configuration_error()
        if error:
            return error
        if not self.storage.get("running"):
            return b"stopped: start required"
        halted = self._risk_halt_error()
        if halted:
            return halted
        if self.storage.get("apply_pending") or self.storage.get(
            "post_reconcile_pending"
        ):
            return b"busy: order work pending"
        return None

    def _begin_apply(self):
        self.storage["apply_generation"] = (
            int(self.storage.get("apply_generation", 0)) + 1
        )
        mark_pending(self.storage, runtime, "apply")

    @callable_by(SELF, decode="raw")
    @deferred
    def rebalance_tick(self, payload):
        height = self._tick_ok(
            "rebalance_tick", "rebalance_interval_blocks", "apply", True
        )
        if height is False:
            return b"skip"
        # A raise here would roll the whole tx back INCLUDING the `_schedule` that
        # `_tick_ok` just did — and since each loop only ever arms its own successor,
        # that ends the chain permanently. Observed three times (2026-08-07/08/09):
        # `*_started_block` frozen, guard long expired, and NO `live.skipped` event,
        # i.e. `_tick_ok` was never re-entered. Swallow, clear the guard so the loop
        # is not also left `*_pending`, and emit so the next occurrence is diagnosable
        # instead of silent. One bad tick should cost one cycle, not the loop.
        try:
            return self._do_rebalance(height)
        except Exception as e:
            self.storage["apply_pending"] = False
            runtime.emit_event(
                "live.tick_failed", {"loop": "apply", "err": str(e)[:200]}
            )
            return b"error"

    def _tick_ok(self, handler, interval_key, flag, return_height=False):
        """Re-arm the timer, then report whether this loop may start work."""
        if not self.storage.get("running"):
            return False
        cfg = self.storage["cfg"]
        self._schedule(handler, int(cfg[interval_key]))
        guard = int(cfg.get("max_poll_wait_blocks", 7000))
        guard_ms = int(cfg.get("max_poll_wait_ms", 1_800_000))
        allowed, height, recovered = tick_pending_guard(
            self.storage, runtime, flag, "live", guard, guard_ms
        )
        if not allowed:
            return False
        if recovered and flag == "apply":
            self.storage["post_reconcile_pending"] = False
        return height if return_height else True

    def _do_rebalance(self, current_height=None):
        # `_tick_ok` has ALREADY set rebalance_pending before we get here, and it
        # is only cleared when the rebalance completes. Both early returns below
        # must clear it themselves or the loop self-blocks for
        # max_poll_wait_blocks (950 ~= 26 min) with nothing in flight.
        #
        # This fires in practice: the rebalance_tick timer fires at actor startup
        # BEFORE `set_research` has wired the research address, so the very first
        # tick takes the `no addr` path and wedges the loop for 26 minutes —
        # every later rebalance_tick returns "skip" while _apply has never run.
        error = self._configuration_error()
        if error:
            self.storage["apply_pending"] = False
            return error
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        addr = str(self.storage.get("research_addr", ""))
        if not addr:
            self.storage["apply_pending"] = False
            runtime.emit_event("live.no_research", {})
            return b"no research addr"
        # Prefer a fresh pull over the message cache. Canyon grants a runner
        # callback enough budget to decode and store the snapshot; its 100k
        # cross-actor message lane does not (observed as out_of_cycles on a
        # six-idea research_snapshot). Split the actual apply onto a fresh
        # timer after the pull so the callback never has to reserve both the
        # decoded state and the three-leg reconciliation child.
        if self.storage.get("cfg", {}).get("research_pull"):
            self._begin_apply()
            return self._pull_research(b"{}")
        snapshot_block = int(self.storage.get("ideas_snapshot_block", 0) or 0)
        if snapshot_block <= 0:
            self.storage["apply_pending"] = False
            runtime.emit_event("live.no_cached_ideas", {})
            return b"no cached ideas"
        height = (
            int(runtime.get_block_height())
            if current_height is None
            else int(current_height)
        )
        if height - snapshot_block > IDEAS_MAX_AGE_BLOCKS:
            self.storage["apply_pending"] = False
            runtime.emit_event(
                "live.stale_cached_ideas",
                {
                    "snapshot_block": snapshot_block,
                },
            )
            return b"stale cached ideas"
        ideas = list(self.storage.get("latest_ideas", []))
        runtime.emit_event("live.pulled_ideas", {"count": len(ideas)})
        self._begin_apply()
        return self._apply(_json_bytes({"ideas": ideas}))

    @runner.continuation(timeout_blocks=2000)
    async def _pull_research(self, payload):
        ctx = capture()
        # Canyon runners cannot hairpin into the chain RPC/gateway, but a
        # callback from an ordinary external request has the 5M execution lane
        # needed for a bounded pure call into the research actor. The response
        # is intentionally not retained or decoded; this account-state request
        # is also a cheap liveness check for the venue we are about to trade.
        ctx.trigger = await runner.http(
            TESTNET_INFO,
            method="POST",
            headers=_HDRS,
            body=spot_state_query(self.storage.get("cfg", {}).get("account_addr", "")),
            timeout_ms=5000,
        )
        return self._on_research_pull()

    def _on_research_pull(self):
        addr = str(self.storage.get("research_addr", ""))
        try:
            snapshot = _parse(
                runtime.call_actor(addr, "get_ideas", b"{}", ACTOR_CALL_GAS_LIMIT)
            )
        except Exception as e:
            self.storage["apply_pending"] = False
            runtime.emit_event(
                "live.research_pull_error",
                {
                    "error": str(e)[:120],
                },
            )
            return b"research read failed"
        ideas = list(snapshot.get("ideas") or [])
        if not isinstance(snapshot, dict) or not ideas:
            self.storage["apply_pending"] = False
            runtime.emit_event(
                "live.research_pull_error",
                {
                    "decoded": bool(snapshot),
                    "ideas": len(ideas),
                },
            )
            return b"no research ideas"
        height = int(runtime.get_block_height())
        self.storage["latest_ideas"] = ideas
        self.storage["ideas_snapshot_block"] = height
        generation = int(self.storage.get("apply_generation", 0))
        self._schedule_followup(
            "rebalance_apply_tick",
            {
                "generation": generation,
            },
        )
        # A nested actor read consumes roughly one Canyon access frame. Ideas
        # and stats together exceed the callback's fixed access budget, so pull
        # the compact counters in their own actor-funded timer transaction.
        self._schedule_followup("research_stats_tick", {})
        runtime.emit_event("live.ideas_pulled", {"count": len(ideas)})
        return b"ideas scheduled"

    @callable_by(SELF, decode="raw")
    @deferred
    def research_stats_tick(self, payload):
        return self._pull_research_stats(b"{}")

    @runner.continuation(timeout_blocks=2000)
    async def _pull_research_stats(self, payload):
        ctx = capture()
        ctx.trigger = await runner.http(
            TESTNET_INFO,
            method="POST",
            headers=_HDRS,
            body=spot_state_query(self.storage.get("cfg", {}).get("account_addr", "")),
            timeout_ms=5000,
        )
        return self._on_research_stats_pull()

    def _on_research_stats_pull(self):
        addr = str(self.storage.get("research_addr", ""))
        try:
            stats = _parse(runtime.call_actor(addr, "get_stats", b"{}", 1_000_000))
            if not isinstance(stats, dict):
                raise ValueError("research stats are not an object")
        except Exception as e:
            runtime.emit_event(
                "live.research_stats_pull_error",
                {
                    "error": str(e)[:120],
                },
            )
            return b"research stats read failed"
        self.storage["cached_research_stats"] = dict(stats)
        if self.storage.get("notify_addr"):
            self._schedule_command_snapshot()
        runtime.emit_event(
            "live.research_stats_pulled",
            {
                "theses": int(stats.get("total_theses", 0) or 0),
            },
        )
        return b"research stats stored"

    @callable_by(SELF, decode="raw")
    @deferred
    def rebalance_apply_tick(self, payload):
        obj = _parse(payload)
        generation = int(obj.get("generation", 0) or 0)
        if not self.storage.get("apply_pending") or generation != int(
            self.storage.get("apply_generation", -1)
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        return self._apply(
            _json_bytes(
                {
                    "ideas": list(self.storage.get("latest_ideas", [])),
                }
            )
        )

    def _portfolio_snapshot(self):
        unreal, marked = _mark_book(
            self.storage.get("portfolio", []), self.storage.get("last_prices", {})
        )
        return {
            "positions": marked,
            "unrealized_pnl": unreal,
            "last_prices": self.storage.get("last_prices", {}),
            "account_state": self.storage.get("account_state", {}),
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
        live_equity = self.storage.get("last_equity")
        strategy_pnl = realized + unreal
        equity = (
            float(live_equity)
            if live_equity is not None
            else budget + realized + unreal
        )
        return {
            "budget": budget,
            "realized_pnl": realized,
            "unrealized_pnl": unreal,
            "equity": equity,
            "equity_source": "venue" if live_equity is not None else "estimated",
            "strategy_equity": budget + strategy_pnl,
            "return_pct": (
                strategy_pnl / budget
                if math.isfinite(budget) and budget > 0
                else 0.0
            ),
            "return_basis": "strategy_pnl_over_budget",
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
        # Orders that never filled must still reach the research learning loop.
        unfilled = []
        for sym, rec in (self.storage.get("exec_feasibility", {}) or {}).items():
            if not isinstance(rec, dict):
                continue
            sent, filled = int(rec.get("sent", 0)), int(rec.get("filled", 0))
            if sent and filled == 0:
                unfilled.append(
                    {
                        "asset": sym,
                        "status": "unfillable",
                        "attempts": sent,
                        "reason": str(rec.get("last_reason") or "")[:120],
                    }
                )
        return {"scorecard": out, "unfillable": unfilled}

    def _push_trader_snapshot(self):
        addr = str(self.storage.get("research_addr", ""))
        if not addr:
            return False
        scorecard = self._scorecard_snapshot()
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "trader_snapshot",
                        "scorecard": scorecard["scorecard"],
                        "unfillable": scorecard["unfillable"],
                        "pnl": self._pnl_snapshot(),
                    }
                ),
            )
            return True
        except Exception as e:
            runtime.emit_event("live.research_send_failed", {"err": str(e)[:80]})
            return False

    def _push_command_snapshot(self):
        addr = str(self.storage.get("notify_addr", ""))
        if not addr:
            return False
        try:
            # Keep the direct actor-call payload bounded so the notifier can
            # decode and store it within the call budget. Commands only need
            # this compact projection of the research and venue state.
            raw_stats = dict(self.storage.get("cached_research_stats", {}))
            stats = {
                key: raw_stats[key]
                for key in ("total_theses", "active_theses", "window")
                if key in raw_stats
            }
            ideas = [
                {
                    "asset": item.get("asset"),
                    "side": item.get("side"),
                    "confidence": item.get("confidence"),
                    "rationale": str(item.get("rationale", ""))[
                        :COMMAND_SNAPSHOT_MAX_RATIONALE_CHARS
                    ],
                }
                for item in list(self.storage.get("latest_ideas", []))[
                    :COMMAND_SNAPSHOT_MAX_IDEAS
                ]
            ]
            raw_pnl = self._pnl_snapshot()
            pnl = {
                key: raw_pnl[key]
                for key in (
                    "open_positions",
                    "equity",
                    "realized_pnl",
                    "unrealized_pnl",
                )
                if key in raw_pnl
            }
            raw_book = self._portfolio_snapshot()
            positions = [
                {
                    "asset": item.get("asset"),
                    "side": item.get("side"),
                    "notional": item.get("notional"),
                    "upnl": item.get("upnl"),
                }
                for item in list(raw_book.get("positions", []))[
                    :COMMAND_SNAPSHOT_MAX_POSITIONS
                ]
            ]
            message = _json_bytes(
                {
                    "type": "command_snapshot",
                    "research_stats": stats,
                    "ideas": ideas,
                    "pnl": pnl,
                    "portfolio": {"positions": positions},
                }
            )
            runtime.call_actor(addr, "handle_message", message, ACTOR_CALL_GAS_LIMIT)
            runtime.emit_event(
                "live.command_snapshot_queued",
                {
                    "bytes": len(message),
                    "ideas": len(ideas),
                    "positions": len(positions),
                },
            )
            return True
        except Exception as e:
            runtime.emit_event("live.notify_send_failed", {"err": str(e)[:80]})
            return False

    @runner.continuation(timeout_blocks=6000)
    async def _apply_monolithic_unused(self, payload):
        ctx = capture()
        ctx.generation = int(self.storage.get("apply_generation", 0))
        ctx.cfg = self.storage.get("cfg", {})
        ctx.selected = _select_ideas(
            _parse(payload).get("ideas", []),
            ctx.cfg.get("max_positions", 3),
            ctx.cfg.get("min_confidence", 60),
        )
        ctx.url = ctx.cfg["price_url"]
        ctx.secrets = _secret_refs(ctx.cfg)
        ctx.info_url = TESTNET_INFO
        # Asset metadata is near-static, but fetching it inline cost TWO awaits
        # in every loop that needed it. CIP-6 caps a continuation at 8 awaits and
        # `_apply` sat exactly AT that ceiling; each await is a full chain
        # round-trip, so measured live at ~0.6 blk/s its eight legs needed ~42 min
        # against a 900-block (~23 min) timeout. It could never finish: it timed
        # out mid-chain, restarted from leg one, and never opened a position.
        #
        # `universe` is now refreshed by its own `universe_tick` continuation and
        # read from storage here. Account reconciliation now uses the two freed
        # slots plus one formerly-wasted state slot, keeping `_apply` at the hard
        # limit of eight awaits while covering spot, core perps, and xyz perps.
        #
        # Deliberately NOT cached with an inline `if ...: await`: the FSM compiler
        # COUNTS awaits inside an `if` (`_iter_top_level_awaits` recurses) but does
        # NOT split on them (`_try_extract_await` handles only Assign/Expr at top
        # level), so a conditional await passes validation and then silently never
        # becomes a resume segment.
        ctx.universe = self.storage.get("universe") or {}

        ctx.account = ctx.cfg.get("account_addr")
        ctx.spot_body = (
            spot_state_query(ctx.account) if ctx.account else mids_query("vntl")
        )
        ctx.core_body = user_state_query(ctx.account) if ctx.account else meta_query()
        ctx.xyz_body = (
            user_state_query(ctx.account, "xyz")
            if ctx.account
            else dex_meta_query("xyz")
        )
        ctx.spot_state = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=ctx.spot_body,
            timeout_ms=5000,
        )
        ctx.core_state = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=ctx.core_body,
            timeout_ms=5000,
        )
        ctx.xyz_state = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=ctx.xyz_body,
            timeout_ms=5000,
        )
        ctx.reconciled = bool(ctx.account) and self._record_account_state(
            ctx.spot_state, ctx.core_state, ctx.xyz_state
        )
        ctx.spot_state = None
        ctx.core_state = None
        ctx.xyz_state = None

        # Fetch prices AFTER reconciliation so a position that existed only at
        # the venue (and was absent from actor storage) is priced and closable in
        # this same rebalance rather than one cycle later.
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
        ctx.prices = resolve_prices(
            list(self._target_from(ctx.selected).keys())
            + [
                str(p.get("asset", "")).upper()
                for p in self.storage.get("portfolio", [])
            ],
            [("", ctx.p_coins), ("xyz", ctx.p_stocks)],
        )
        ctx.p_coins = None
        ctx.p_stocks = None
        ctx.plan = (
            self._live_plan(ctx.selected, ctx.prices, ctx.universe)
            if ctx.reconciled
            else []
        )
        ctx.u0, ctx.h0, ctx.b0 = self._slot(ctx.plan, 0, ctx.info_url, meta_query())
        ctx.r0 = await runner.http(
            ctx.u0,
            method="POST",
            headers=ctx.h0,
            body=ctx.b0,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        # Hash and nonce one order per leg; three signed slots in one resume
        # exhaust the fixed callback budget before it can reserve its child.
        ctx.u1, ctx.h1, ctx.b1 = self._slot(ctx.plan, 1, ctx.info_url, meta_query())
        ctx.r1 = await runner.http(
            ctx.u1,
            method="POST",
            headers=ctx.h1,
            body=ctx.b1,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        ctx.u2, ctx.h2, ctx.b2 = self._slot(ctx.plan, 2, ctx.info_url, meta_query())
        ctx.r2 = await runner.http(
            ctx.u2,
            method="POST",
            headers=ctx.h2,
            body=ctx.b2,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        return self._on_apply(
            ctx.selected, ctx.prices, ctx.plan, ctx.r0, ctx.r1, ctx.r2
        )

    @runner.continuation(timeout_blocks=3000)
    async def _apply(self, payload):
        """Reconcile venue state, then continue planning on a fresh timer.

        The former eight-await continuation exceeded Canyon's 5M callback-cycle
        budget before it reached the signed-order legs.  Account reads, price
        reads, and each order now run as bounded independent stages.
        """
        ctx = capture()
        ctx.generation = int(self.storage.get("apply_generation", 0))
        ctx.cfg = self.storage.get("cfg", {})
        ctx.selected = _select_ideas(
            _parse(payload).get("ideas", []),
            ctx.cfg.get("max_positions", 3),
            ctx.cfg.get("min_confidence", 60),
        )
        ctx.info_url = TESTNET_INFO
        ctx.account = ctx.cfg.get("account_addr")
        ctx.spot = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=spot_state_query(ctx.account),
            timeout_ms=5000,
        )
        ctx.core = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=user_state_query(ctx.account),
            timeout_ms=5000,
        )
        ctx.xyz = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=user_state_query(ctx.account, "xyz"),
            timeout_ms=5000,
        )
        return self._on_apply_state(
            ctx.generation, ctx.selected, ctx.account, ctx.spot, ctx.core, ctx.xyz
        )

    def _on_apply_state(self, generation, selected, account, spot, core, xyz):
        if (
            not self.storage.get("apply_pending")
            or int(generation) != int(self.storage.get("apply_generation", -1))
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        reconciled = bool(account) and self._record_account_state(spot, core, xyz)
        if not reconciled:
            self.storage["apply_pending"] = False
            self.storage["pending_apply"] = {}
            runtime.emit_event("live.apply_aborted", {"stage": "reconcile"})
            return b"reconcile failed"
        self.storage["pending_apply"] = {
            "generation": int(generation),
            "stage": "prices",
            "selected": list(selected or []),
        }
        self._schedule_followup("apply_prices_tick", {"generation": int(generation)})
        runtime.emit_event(
            "live.apply_staged",
            {
                "stage": "prices",
                "generation": int(generation),
            },
        )
        return b"prices scheduled"

    @callable_by(SELF, decode="raw")
    @deferred
    def apply_prices_tick(self, payload):
        obj = _parse(payload)
        state = dict(self.storage.get("pending_apply") or {})
        generation = int(obj.get("generation", 0) or 0)
        if (
            not self.storage.get("apply_pending")
            or generation != int(state.get("generation", -1))
            or generation != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "prices"
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        return self._apply_prices(_json_bytes({"generation": generation}))

    @runner.continuation(timeout_blocks=2000)
    async def _apply_prices(self, payload):
        ctx = capture()
        ctx.generation = int(_parse(payload).get("generation", 0) or 0)
        ctx.url = self.storage.get("cfg", {})["price_url"]
        ctx.coins = await runner.http(
            ctx.url, method="POST", headers=_HDRS, body=mids_query(""), timeout_ms=5000
        )
        return self._on_apply_core_prices(ctx.generation, ctx.coins)

    def _on_apply_core_prices(self, generation, coins):
        state = dict(self.storage.get("pending_apply") or {})
        if (
            int(generation) != int(state.get("generation", -1))
            or int(generation) != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "prices"
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        selected = list(state.get("selected") or [])
        symbols = list(self._target_from(selected).keys()) + [
            str(p.get("asset", "")).upper() for p in self.storage.get("portfolio", [])
        ]
        state["core_prices"] = resolve_prices(symbols, [("", coins)])
        state["stage"] = "stock_prices"
        self.storage["pending_apply"] = state
        self._schedule_followup(
            "apply_stocks_tick",
            {
                "generation": int(generation),
            },
        )
        runtime.emit_event(
            "live.apply_staged",
            {
                "stage": "stock_prices",
                "generation": int(generation),
            },
        )
        return b"stock prices scheduled"

    @callable_by(SELF, decode="raw")
    @deferred
    def apply_stocks_tick(self, payload):
        obj = _parse(payload)
        state = dict(self.storage.get("pending_apply") or {})
        generation = int(obj.get("generation", 0) or 0)
        if (
            not self.storage.get("apply_pending")
            or generation != int(state.get("generation", -1))
            or generation != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "stock_prices"
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        return self._apply_stocks(_json_bytes({"generation": generation}))

    @runner.continuation(timeout_blocks=2000)
    async def _apply_stocks(self, payload):
        ctx = capture()
        ctx.generation = int(_parse(payload).get("generation", 0) or 0)
        ctx.url = self.storage.get("cfg", {})["price_url"]
        ctx.stocks = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("xyz"),
            timeout_ms=5000,
        )
        return self._on_apply_prices(ctx.generation, ctx.stocks)

    def _on_apply_prices(self, generation, stocks):
        state = dict(self.storage.get("pending_apply") or {})
        if (
            int(generation) != int(state.get("generation", -1))
            or int(generation) != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "stock_prices"
        ):
            return b"superseded"
        halted = self._risk_halt_error()
        if halted:
            self._abort_apply("risk_halted")
            return halted
        selected = list(state.get("selected") or [])
        symbols = list(self._target_from(selected).keys()) + [
            str(p.get("asset", "")).upper() for p in self.storage.get("portfolio", [])
        ]
        prices = dict(state.get("core_prices") or {})
        prices.update(resolve_prices(symbols, [("xyz", stocks)]))
        if not prices:
            self.storage["apply_pending"] = False
            self.storage["pending_apply"] = {}
            runtime.emit_event("live.price_error", {})
            return b"no prices"
        plan = self._live_plan(selected, prices, self.storage.get("universe") or {})
        if not plan:
            self.storage["pending_apply"] = {}
            return self._on_apply(selected, prices, [])
        self.storage["pending_apply"] = {
            "generation": int(generation),
            "stage": "orders",
            "selected": selected,
            "prices": prices,
            "plan": plan,
            "results": [],
        }
        self._schedule_followup(
            "apply_order_tick",
            {
                "generation": int(generation),
                "index": 0,
            },
        )
        runtime.emit_event(
            "live.apply_staged",
            {
                "stage": "orders",
                "generation": int(generation),
                "orders": len(plan),
            },
        )
        return b"orders scheduled"

    @callable_by(SELF, decode="raw")
    @deferred
    def apply_order_tick(self, payload):
        obj = _parse(payload)
        state = dict(self.storage.get("pending_apply") or {})
        generation = int(obj.get("generation", 0) or 0)
        index = int(obj.get("index", 0) or 0)
        if (
            not self.storage.get("apply_pending")
            or generation != int(state.get("generation", -1))
            or generation != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "orders"
            or index != len(state.get("results") or [])
        ):
            return b"superseded"
        plan = list(state.get("plan") or [])
        halted = self._risk_halt_error()
        if halted and index < len(plan) and not plan[index].get("r"):
            # All reduce-only legs precede opening legs. Finalize their results
            # so PnL is not lost, but never dispatch new exposure while halted.
            executed_plan = plan[:index]
            results = list(state.get("results") or [])
            selected = list(state.get("selected") or [])
            prices = dict(state.get("prices") or {})
            self.storage["pending_apply"] = {}
            return self._on_apply(selected, prices, executed_plan, *results)
        if index < len(plan) and not plan[index].get("r"):
            projected = {
                str(position.get("asset", "")).upper()
                for position in self.storage.get("portfolio", [])
            }
            results = list(state.get("results") or [])
            for prior, result in zip(plan[:index], results):
                filled, _avg, _why = parse_fill(result) if isinstance(result, dict) else (0.0, None, None)
                symbol = str(prior.get("sym", "")).upper()
                if prior.get("r"):
                    held_qty = float(prior.get("_position_qty", prior.get("s", 0)) or 0)
                    if filled + 1e-12 >= held_qty:
                        projected.discard(symbol)
                elif filled > 0:
                    projected.add(symbol)
            order_symbol = str(plan[index].get("sym", "")).upper()
            max_positions = int(self.storage.get("cfg", {}).get("max_positions", 10))
            if order_symbol in projected or len(projected) >= max_positions:
                skipped = plan.pop(index)
                state["plan"] = plan
                self.storage["pending_apply"] = state
                runtime.emit_event(
                    "live.order_skipped",
                    {
                        "sym": skipped.get("sym", ""),
                        "reason": "max_positions_waiting_for_close",
                    },
                )
                if index < len(plan):
                    self._schedule_followup(
                        "apply_order_tick",
                        {"generation": generation, "index": index},
                    )
                    return b"order skipped"
                selected = list(state.get("selected") or [])
                prices = dict(state.get("prices") or {})
                self.storage["pending_apply"] = {}
                return self._on_apply(selected, prices, plan, *results)
        # Allocate immediately before dispatch from this timer transaction,
        # where the durable counter write commits before the runner suspends.
        # Reserving base+index while the plan was built left later orders using
        # stale millisecond nonces; the shared agent-wallet nonce tracker could
        # consume those values before the later timers reached Hyperliquid.
        nonce = self._next_nonce()
        return self._apply_order(
            _json_bytes(
                {
                    "generation": generation,
                    "index": index,
                    "nonce": nonce,
                }
            )
        )

    @runner.continuation(timeout_blocks=2000)
    async def _apply_order(self, payload):
        ctx = capture()
        obj = _parse(payload)
        ctx.generation = int(obj.get("generation", 0) or 0)
        ctx.index = int(obj.get("index", 0) or 0)
        ctx.nonce = int(obj.get("nonce", 0) or 0) or self._next_nonce()
        state = dict(self.storage.get("pending_apply") or {})
        ctx.order = list(state.get("plan") or [])[ctx.index]
        ctx.url, ctx.headers, ctx.body = signed_job(
            order_action(
                ctx.order["a"],
                ctx.order["b"],
                ctx.order["p"],
                ctx.order["s"],
                reduce_only=ctx.order["r"],
                tif="Ioc",
            ),
            ctx.nonce,
        )
        ctx.result = await runner.http(
            ctx.url,
            method="POST",
            headers=ctx.headers,
            body=ctx.body,
            secrets=_secret_refs(self.storage.get("cfg", {})),
            timeout_ms=10000,
        )
        return self._on_apply_order(ctx.generation, ctx.index, ctx.result)

    def _on_apply_order(self, generation, index, result):
        state = dict(self.storage.get("pending_apply") or {})
        results = list(state.get("results") or [])
        if (
            int(generation) != int(state.get("generation", -1))
            or int(generation) != int(self.storage.get("apply_generation", -1))
            or state.get("stage") != "orders"
            or int(index) != len(results)
        ):
            return b"superseded"
        results.append(_normalize(result))
        plan = list(state.get("plan") or [])
        next_index = int(index) + 1
        if next_index < len(plan):
            state["results"] = results
            self.storage["pending_apply"] = state
            self._schedule_followup(
                "apply_order_tick",
                {
                    "generation": int(generation),
                    "index": next_index,
                },
            )
            return b"next order scheduled"
        selected = list(state.get("selected") or [])
        prices = dict(state.get("prices") or {})
        self.storage["pending_apply"] = {}
        return self._on_apply(selected, prices, plan, *results)

    def _on_apply(self, selected, prices, plan=None, *results):
        cfg = self.storage["cfg"]
        height = int(runtime.get_block_height())
        portfolio = list(self.storage.get("portfolio", []))

        target = {}
        for i in selected:
            asset = str(i.get("asset", "")).upper()
            if asset and asset not in target:
                target[asset] = {
                    "side": str(i.get("side", "")).lower(),
                    "confidence": int(i.get("confidence", 0) or 0),
                }
        held = [str(p.get("asset", "")).upper() for p in portfolio]

        if not prices:
            self.storage["apply_pending"] = False
            self.storage["pending_apply"] = {}
            runtime.emit_event("live.price_error", {})
            return b"no prices"

        keep, close = plan_book(
            portfolio,
            target,
            prices,
            bool(cfg.get("close_absent", False)),
            height,
            int(cfg.get("max_hold_blocks", 14407)),
        )

        live_fills = {}
        if plan:
            audit = list(self.storage.get("orders", []))
            # Per-symbol execution record. This is what research reads back via
            # get_scorecard: without it a symbol the venue always refuses is
            # INVISIBLE to the learning loop (it never enters `portfolio`, so it
            # is never a winner or a loser), and synth keeps proposing it forever.
            # Observed 2026-08-10: MSFT/AMZN/META refused 34/34 while research
            # went on recommending them for 8.5h.
            feas = dict(self.storage.get("exec_feasibility", {}))
            rejects = []
            unknowns = []
            for o, res in zip(plan, results):
                r = _normalize(res)
                sz, avg, why_no = (
                    parse_fill(r) if isinstance(r, dict) else (0.0, None, None)
                )
                outcome = order_outcome(sz, why_no, o["s"])
                audit.append(
                    {
                        "sym": o["sym"],
                        "why": o["why"],
                        "sent": {"p": o["p"], "s": o["s"]},
                        "filled": sz,
                        "avg_px": avg,
                        "outcome": outcome,
                        "rejected": bool(why_no),
                        "reject_reason": why_no,
                        "response": r if isinstance(r, (dict, str)) else str(r),
                        "height": height,
                    }
                )
                rec = feas.get(o["sym"]) or {
                    "sent": 0,
                    "filled": 0,
                    "rejected": 0,
                    "last_reason": None,
                }
                rec["sent"] = int(rec.get("sent", 0)) + 1
                rec["last_block"] = height
                if sz > 0:
                    rec["filled"] = int(rec.get("filled", 0)) + 1
                    live_fills[(o["sym"], bool(o["r"]))] = {
                        "qty": sz,
                        "avg_px": avg,
                        "reduce_only": bool(o["r"]),
                        "venue": str(o.get("venue", "core")),
                    }
                if why_no:
                    rec["rejected"] = int(rec.get("rejected", 0)) + 1
                    rec["last_reason"] = why_no[:120]
                    rec["last_reject_block"] = height
                    rejects.append(
                        {"sym": o["sym"], "why": o["why"], "reason": why_no[:120]}
                    )
                if outcome == "unknown":
                    unknowns.append({"sym": o["sym"], "why": o["why"]})
                feas[o["sym"]] = rec
            self.storage["orders"] = audit[-50:]
            # Bounded: one entry per symbol ever ordered would otherwise grow
            # monotonically in on-chain storage as the universe rotates. Keep the
            # most recently touched, which is what the gate and the research
            # feedback both care about.
            if len(feas) > 120:
                ranked = sorted(
                    feas.items(),
                    key=lambda kv: int((kv[1] or {}).get("last_block", 0) or 0),
                    reverse=True,
                )
                feas = dict(ranked[:120])
            self.storage["exec_feasibility"] = feas
            # Alarm surface: a refusal must announce itself. Everything about this
            # failure was silent until someone eyeballed the book.
            if rejects:
                runtime.emit_event(
                    "live.orders_rejected",
                    {
                        "count": len(rejects),
                        "planned": len(plan),
                        "detail": rejects[:8],
                        "height": height,
                    },
                )
            if unknowns:
                runtime.emit_event(
                    "live.orders_unknown",
                    {
                        "count": len(unknowns),
                        "planned": len(plan),
                        "detail": unknowns[:8],
                        "height": height,
                    },
                )

        realized = float(self.storage.get("realized_pnl", 0.0))
        closed = int(self.storage.get("closed_count", 0))
        wins = int(self.storage.get("win_count", 0))
        history = list(self.storage.get("history", []))
        for p, _cur in close:
            # A close is only real when the reduce-only order filled. Dropping
            # an unfilled close would leave venue exposure missing from the book.
            f = live_fills.get((str(p.get("asset", "")).upper(), True))
            if not f or not f.get("reduce_only"):
                keep.append(p)
                continue
            remainder, pnl, rec = _close_from_fill(p, f, "rebalance", height)
            if remainder:
                keep.append(remainder)
            realized += pnl
            closed += 1
            if pnl > 0:
                wins += 1
            history.insert(0, rec)

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
            f = live_fills.get((asset, False))
            if not f or f.get("reduce_only"):
                continue
            entry_px, qty = f["avg_px"], f["qty"]
            keep.append(
                {
                    "asset": asset,
                    "side": tgt["side"],
                    "entry_px": entry_px,
                    "qty": qty,
                    "notional": qty * entry_px,
                    "confidence": tgt["confidence"],
                    "opened_block": height,
                    "venue": str(f.get("venue", "core")),
                }
            )
            opened += 1

        self.storage["portfolio"] = keep
        self.storage["history"] = history[:50]
        # MERGE, never assign. `prices` here covers only target-union-held (see
        # _apply), so assigning it discarded every other symbol the wide refresh
        # had cached and left the map able to shrink but never grow — river run8
        # sat at exactly {BTC, ETH, SOL}. Every other writer (_on_prices,
        # _mark_route, _on_mark) already merges; this was the lone exception.
        self._commit_prices(prices)
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
        runtime.emit_event("live.rebalanced", report)
        reconcile = bool(plan and cfg.get("account_addr"))
        if reconcile:
            self.storage["post_reconcile_pending"] = True
            report["apply_generation"] = int(self.storage.get("apply_generation", 0))
        else:
            self.storage["apply_pending"] = False
        # Mixed order outcomes can spend the entire callback own-work allowance.
        # Keep reconciliation and feedback children on a fresh timer budget.
        self._schedule_followup(
            "apply_followup_tick",
            {
                "reconcile": reconcile,
                "report": report,
            },
        )
        return _json_bytes(report)

    @callable_by(SELF, decode="raw")
    @deferred
    def apply_followup_tick(self, payload):
        obj = _parse(payload)
        if obj.get("reconcile"):
            report = dict(obj.get("report") or {})
            if int(report.get("apply_generation", 0)) != int(
                self.storage.get("apply_generation", 0)
            ) or not self.storage.get("post_reconcile_pending"):
                return b"superseded"
            return self._reconcile_after_orders(_json_bytes(report))
        return b"sent" if self._push_trader_snapshot() else b"not sent"

    @runner.continuation(timeout_blocks=3000)
    async def _reconcile_after_orders(self, payload):
        """Read the authoritative venue state after a submitted order batch."""
        ctx = capture()
        ctx.report = payload
        ctx.cfg = self.storage.get("cfg", {})
        ctx.account = ctx.cfg.get("account_addr")
        ctx.info_url = TESTNET_INFO
        ctx.spot = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=spot_state_query(ctx.account),
            timeout_ms=5000,
        )
        ctx.core = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=user_state_query(ctx.account),
            timeout_ms=5000,
        )
        ctx.xyz = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=user_state_query(ctx.account, "xyz"),
            timeout_ms=5000,
        )
        return self._on_post_order_reconcile(ctx.report, ctx.spot, ctx.core, ctx.xyz)

    def _on_post_order_reconcile(self, report, spot, core, xyz):
        result = _parse(report)
        generation = int(result.pop("apply_generation", 0) or 0)
        current_generation = int(self.storage.get("apply_generation", 0) or 0)
        if generation != current_generation:
            runtime.emit_event(
                "live.reconcile_superseded",
                {
                    "generation": generation,
                    "current_generation": current_generation,
                },
            )
            result["venue_reconciled"] = False
            return _json_bytes(result)
        self.storage["post_reconcile_pending"] = False
        self.storage["apply_pending"] = False
        reconciled = self._record_account_state(spot, core, xyz)
        result["venue_reconciled"] = reconciled
        if reconciled:
            runtime.emit_event(
                "live.orders_reconciled",
                {
                    "positions": len(self.storage.get("portfolio", [])),
                    "height": int(runtime.get_block_height()),
                },
            )
        # Pull-mode fleets cannot consume stateful cross-actor messages inside
        # Canyon's fixed 100k message lane (the authenticated snapshot handler
        # exhausts it).  The trader already pulls research through a runner
        # callback; do not emit a doomed reverse push after every rebalance.
        # Legacy push-mode deployments retain their feedback behavior.
        if not self.storage.get("cfg", {}).get("research_pull"):
            self._push_trader_snapshot()
        return _json_bytes(result)

    @callable_by(OWNER, decode="raw")
    @deferred
    def mark_now(self, payload):
        return self._mark(b"{}")

    @callable_by(SELF, decode="raw")
    @deferred
    def mark_tick(self, payload):
        if not self._tick_ok("mark_tick", "mark_interval_blocks", "mark"):
            return b"skip"
        try:
            return self._mark(b"{}")
        except Exception as e:
            self.storage["mark_pending"] = False
            runtime.emit_event(
                "live.tick_failed", {"loop": "mark", "err": str(e)[:200]}
            )
            return b"error"

    @callable_by(SELF, decode="raw")
    @deferred
    def mark_price_retry_tick(self, payload):
        """Retry one incomplete held-position price fetch without re-arming."""
        if not self.storage.get("running"):
            return b"stopped"
        if self.storage.get("mark_pending"):
            runtime.emit_event(
                "live.exit_price_retry_skipped",
                {
                    "reason": "mark_pending",
                },
            )
            return b"busy"
        self.storage["mark_pending"] = True
        self.storage["mark_started_block"] = int(runtime.get_block_height())
        self.storage["mark_started_ms"] = int(runtime.get_timestamp_ms())
        try:
            return self._mark(b"{}")
        except Exception as e:
            self.storage["mark_pending"] = False
            runtime.emit_event(
                "live.tick_failed",
                {
                    "loop": "mark_price_retry",
                    "err": str(e)[:200],
                },
            )
            return b"error"

    @callable_by(SELF, decode="raw")
    @deferred
    def universe_tick(self, payload):
        """Refresh the near-static asset universe on its own slow cadence.

        Split out of `_apply`/`_mark` so neither pays two chain round-trips for
        metadata that barely changes. Runs rarely; both loops read the cached
        result from storage.
        """
        if not self._tick_ok("universe_tick", "universe_interval_blocks", "universe"):
            return b"skip"
        try:
            return self._refresh_universe(b"{}")
        except Exception as e:
            self.storage["universe_pending"] = False
            runtime.emit_event(
                "live.tick_failed", {"loop": "universe", "err": str(e)[:200]}
            )
            return b"error"

    @callable_by(OWNER, decode="raw")
    @deferred
    def universe_now(self, payload):
        return self._refresh_universe(b"{}")

    @callable_by(OWNER, SELF, decode="raw")
    @deferred
    def prices_now(self, payload):
        """Refresh last_prices ONLY. Two awaits, deliberately.

        `_mark` fetches prices in its first two awaits but commits `last_prices`
        only after its full 7-await chain returns — so where long continuations
        cannot complete, the prices are fetched and then discarded, and
        `plan_orders` skips every symbol as `no_price`.

        Observed 2026-08-12 on canyon: continuations of <=3 awaits complete,
        >=4 never return (`_refresh_universe` 2 and `_validate` 3 work; `_mark`
        7 and `_apply` 8 hang). This mirrors `_refresh_universe`'s shape so
        prices land independently of the rest of the mark cycle.
        """
        return self._refresh_prices(payload)

    @runner.continuation(timeout_blocks=2000)
    async def _refresh_prices(self, payload):
        ctx = capture()
        ctx.url = self.storage.get("cfg", {})["price_url"]
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
        return self._on_prices(ctx.p_coins, ctx.p_stocks)

    def _on_prices(self, p_coins, p_stocks):
        syms = list((self.storage.get("universe") or {}).keys())
        prices = resolve_prices(syms, [("", p_coins), ("xyz", p_stocks)])
        self._commit_prices(prices)
        runtime.emit_event("live.prices", {"symbols": len(prices)})
        return _json_bytes({"symbols": len(prices)})

    @runner.continuation(timeout_blocks=2000)
    async def _refresh_universe(self, payload):
        ctx = capture()
        ctx.info_url = TESTNET_INFO
        ctx.meta = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=meta_query(),
            timeout_ms=5000,
        )
        ctx.xyz = await runner.http(
            ctx.info_url,
            method="POST",
            headers=_HDRS,
            body=dex_meta_query("xyz"),
            timeout_ms=5000,
        )
        return self._on_universe(ctx.meta, ctx.xyz)

    def _on_universe(self, meta, xyz):
        self.storage["universe_pending"] = False
        uni = self._combined_universe(meta, xyz)
        # Keep the previous universe on a bad/empty fetch: `_apply` cannot place
        # an order for an asset missing from it, so clobbering with {} would
        # silently disable trading until the next refresh.
        if uni:
            self.storage["universe"] = uni
        runtime.emit_event("live.universe", {"assets": len(uni)})
        return _json_bytes({"assets": len(uni)})

    # 7 sequential `runner.http` awaits, each a full chain round-trip (submit ->
    # assign -> fetch -> result -> resume). 150 blocks gave ~21 per round-trip,
    # tighter even than the 200 that made `_apply` restart-storm: it expired
    # mid-chain and restarted from leg one, re-issuing 7 fresh jobs, and that
    # load kept the chain slow enough that the next attempt also expired.
    @runner.continuation(timeout_blocks=2000)
    async def _mark(self, payload):
        """Price leg ONLY — two awaits.

        `_mark` used to be one 5-await chain: two price fetches then three exit
        order slots. `_slot` substitutes a plain /info read for an unused slot,
        so those three awaits fired on EVERY tick even with nothing to exit —
        and `last_prices` was only committed at the end, so a stall in those
        pointless calls discarded prices that had already been fetched. With an
        empty price cache `plan_orders` skips every symbol as `no_price`, and
        the trader goes silent while looking healthy (run8, 2026-08-12: only 3
        symbols priced for ~73 min while a valid DOGE idea was dropped).

        Splitting on the exit decision — rather than an inline `if ...: await`,
        which the FSM compiler counts but does not split on — means the common
        no-exit path finishes in two awaits and prices always land.
        """
        ctx = capture()
        ctx.url = self.storage.get("cfg", {})["price_url"]
        # Marking only needs prices for the bounded live book (at most
        # max_positions), not hundreds of raw allMids entries.  Keeping the raw
        # core result in ``ctx`` across the xyz await exhausted Canyon's fixed
        # callback budget.  Resolve held symbols immediately, then discard the
        # response before the next continuation leg is serialized.
        ctx.held = [
            str(p.get("asset", "")).upper() for p in self.storage.get("portfolio", [])
        ]
        ctx.raw_prices = await runner.http(
            ctx.url, method="POST", headers=_HDRS, body=mids_query(""), timeout_ms=5000
        )
        ctx.prices = resolve_prices(ctx.held, [("", ctx.raw_prices)])
        ctx.raw_prices = None
        ctx.raw_prices = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("xyz"),
            timeout_ms=5000,
        )
        ctx.prices.update(resolve_prices(ctx.held, [("xyz", ctx.raw_prices)]))
        ctx.raw_prices = None
        return self._mark_route_prices(ctx.prices)

    def _mark_route(self, p_coins, p_stocks):
        """Commit prices, then dispatch the exit leg only if there is one."""
        pairs = [("", p_coins), ("xyz", p_stocks)]
        wide = resolve_prices(list((self.storage.get("universe") or {}).keys()), pairs)
        if wide:
            self._commit_prices(wide)
            runtime.emit_event("live.prices", {"symbols": len(wide), "src": "mark"})
        held = [
            str(p.get("asset", "")).upper() for p in self.storage.get("portfolio", [])
        ]
        mprices = resolve_prices(held, pairs)
        return self._dispatch_mark(mprices)

    def _mark_route_prices(self, prices):
        """Commit an already-bounded held-position price map."""
        if prices:
            self._commit_prices(prices)
            runtime.emit_event(
                "live.prices",
                {
                    "symbols": len(prices),
                    "src": "mark",
                },
            )
        return self._dispatch_mark(prices)

    def _set_risk_halt(self, reason, detail=None):
        """Block new exposure while preserving the mark/exit safety loop."""
        already = bool(self.storage.get("risk_halted"))
        previous = str(self.storage.get("risk_halt_reason", ""))
        # Missing market data must not downgrade a cumulative-loss halt that
        # requires explicit owner acknowledgement.
        if previous == "cumulative_loss" and reason != "cumulative_loss":
            return
        self.storage["risk_halted"] = True
        self.storage["risk_halt_reason"] = str(reason)
        if not already or previous != str(reason):
            runtime.emit_event(
                "live.risk_halted",
                {
                    "reason": str(reason),
                    "detail": detail or {},
                },
            )

    def _clear_price_risk_halt(self):
        if self.storage.get("risk_halt_reason") != "exit_price_unavailable":
            return
        self.storage["risk_halted"] = False
        self.storage["risk_halt_reason"] = ""
        runtime.emit_event("live.risk_recovered", {"reason": "prices_fresh"})

    def _strategy_pnl(self, prices):
        """Return realized plus fresh-mark unrealized PnL, or None if incomplete."""
        portfolio = list(self.storage.get("portfolio", []))
        held = [str(p.get("asset", "")).upper() for p in portfolio]
        if any((prices or {}).get(sym) is None for sym in held):
            return None
        unreal, _marked = _mark_book(portfolio, prices or {})
        return float(self.storage.get("realized_pnl", 0.0)) + unreal

    def _cumulative_loss(self, prices):
        """Return drawdown since the last owner-acknowledged loss baseline."""
        strategy_pnl = self._strategy_pnl(prices)
        if strategy_pnl is None:
            return None
        budget = float(self.storage.get("cfg", {}).get("budget", 0.0))
        if budget <= 0:
            return None
        baseline = float(self.storage.get("cumulative_loss_baseline_pnl", 0.0) or 0.0)
        return max(0.0, (baseline - strategy_pnl) / budget)

    def _dispatch_mark(self, mprices):
        universe = self.storage.get("universe") or {}
        cfg = self.storage.get("cfg", {})
        risk_enabled = (
            float(cfg.get("stop_loss_pct", 0.0)) > 0
            or float(cfg.get("take_profit_pct", 0.0)) > 0
        )
        held = [
            str(p.get("asset", "")).upper() for p in self.storage.get("portfolio", [])
        ]
        missing = (
            [sym for sym in held if mprices.get(sym) is None]
            if risk_enabled and universe
            else []
        )
        retry_attempts = int(self.storage.get("mark_price_retry_attempts", 0) or 0)
        max_retries = max(0, int(cfg.get("max_exit_price_retries", 3)))
        if missing:
            retry_attempts += 1
        else:
            retry_attempts = 0
            self._clear_price_risk_halt()
        self.storage["mark_price_retry_attempts"] = retry_attempts
        retry_symbols = missing if missing and retry_attempts <= max_retries else []
        self.storage["mark_price_retry_symbols"] = retry_symbols
        if missing:
            runtime.emit_event(
                "live.exit_price_missing",
                {
                    "count": len(missing),
                    "symbols": missing[:8],
                    "attempt": retry_attempts,
                    "max_retries": max_retries,
                    "retry_scheduled": bool(retry_symbols),
                },
            )
            if not retry_symbols:
                self._set_risk_halt(
                    "exit_price_unavailable",
                    {"symbols": missing[:8], "attempts": retry_attempts},
                )
        else:
            loss = self._cumulative_loss(mprices)
            max_loss = float(cfg.get("max_cumulative_loss_pct", 0.10))
            if loss is not None and loss >= max_loss:
                self._set_risk_halt(
                    "cumulative_loss",
                    {"loss_pct": loss, "limit_pct": max_loss},
                )
        exits = self._exit_plan(mprices, universe)
        if not exits:
            return self._on_mark(mprices)
        if len(exits) > 3:
            runtime.emit_event(
                "live.exit_deferred",
                {
                    "planned": len(exits),
                    "submitted": 3,
                    "deferred": [o.get("sym", "") for o in exits[3:11]],
                },
            )
            exits = exits[:3]
        self.storage["pending_exits"] = exits
        self.storage["pending_exit_prices"] = mprices
        self._schedule_followup("mark_exits_tick", {})
        return b"exits scheduled"

    @callable_by(SELF, decode="raw")
    @deferred
    def mark_exits_tick(self, payload):
        return self._mark_exits(b"{}")

    @runner.continuation(timeout_blocks=3000)
    async def _mark_exits(self, payload):
        """Exit leg — three signed slots, only reached when exits exist."""
        ctx = capture()
        ctx.secrets = _secret_refs(self.storage.get("cfg", {}))
        ctx.info_url = TESTNET_INFO
        ctx.exits = list(self.storage.get("pending_exits", []))
        ctx.mprices = dict(self.storage.get("pending_exit_prices", {}))
        ctx.u0, ctx.h0, ctx.b0 = self._slot(ctx.exits, 0, ctx.info_url, meta_query())
        ctx.r0 = await runner.http(
            ctx.u0,
            method="POST",
            headers=ctx.h0,
            body=ctx.b0,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        ctx.u1, ctx.h1, ctx.b1 = self._slot(ctx.exits, 1, ctx.info_url, meta_query())
        ctx.r1 = await runner.http(
            ctx.u1,
            method="POST",
            headers=ctx.h1,
            body=ctx.b1,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        ctx.u2, ctx.h2, ctx.b2 = self._slot(ctx.exits, 2, ctx.info_url, meta_query())
        ctx.r2 = await runner.http(
            ctx.u2,
            method="POST",
            headers=ctx.h2,
            body=ctx.b2,
            secrets=ctx.secrets,
            timeout_ms=10000,
        )
        return self._on_mark(ctx.mprices, ctx.exits, ctx.r0, ctx.r1, ctx.r2)

    def _exit_plan(self, prices, universe):
        """Build reduce-only orders for positions breaching configured exits."""
        cfg = self.storage.get("cfg", {})
        sl = float(cfg.get("stop_loss_pct", 0.0))
        tp = float(cfg.get("take_profit_pct", 0.0))
        force_close = self.storage.get("risk_halt_reason") == "cumulative_loss"
        if sl <= 0 and tp <= 0 and not force_close:
            return []
        if not universe:
            return []

        orders = []
        for position in list(self.storage.get("portfolio", [])):
            sym = str(position.get("asset", "")).upper()
            cur = (prices or {}).get(sym)
            reason = (
                "cumulative_loss"
                if force_close and cur is not None
                else _exit_reason(
                    position.get("side"), position.get("entry_px"), cur, sl, tp
                )
            )
            if not reason:
                continue
            metadata = universe.get(sym)
            qty = float(position.get("qty", 0) or 0)
            if not metadata or cur is None or qty <= 0:
                continue
            was_long = str(position.get("side", "")).lower() == "long"
            size = fmt_close_size(qty, metadata["sz"])
            if size == "0":
                continue
            performance = _pos_return(
                position.get("side"), position.get("entry_px"), cur
            )
            orders.append(
                {
                    "sym": sym,
                    "a": metadata["a"],
                    "b": not was_long,
                    "p": fmt_price(
                        cur, not was_long, metadata["sz"], int(cfg.get("slip_bps", 200))
                    ),
                    "s": size,
                    "r": True,
                    "why": reason,
                    "venue": str(metadata.get("venue", "core")),
                    "_exit_priority": (
                        3
                        if reason == "cumulative_loss"
                        else 2
                        if reason == "stop_loss"
                        else 1,
                        abs(performance),
                    ),
                }
            )
        orders.sort(key=lambda order: order["_exit_priority"], reverse=True)
        for order in orders:
            order.pop("_exit_priority", None)
        return orders

    def _on_mark(self, prices, exits=None, *results):

        self.storage["mark_pending"] = False
        cfg = self.storage["cfg"]
        portfolio = list(self.storage.get("portfolio", []))
        held = [str(p.get("asset", "")).upper() for p in portfolio]

        prices = self._commit_prices(prices)

        exit_fills = {}
        if exits:
            audit = list(self.storage.get("orders", []))
            stuck = []
            for o, res in zip(exits, results):
                r = _normalize(res)
                sz, avg, why_no = (
                    parse_fill(r) if isinstance(r, dict) else (0.0, None, None)
                )
                audit.append(
                    {
                        "sym": o["sym"],
                        "why": o["why"],
                        "venue": str(o.get("venue", "core")),
                        "sent": {"p": o["p"], "s": o["s"]},
                        "filled": sz,
                        "avg_px": avg,
                        "rejected": bool(why_no),
                        "reject_reason": why_no,
                        "response": r if isinstance(r, (dict, str)) else str(r),
                        "height": int(runtime.get_block_height()),
                    }
                )
                if sz > 0:
                    exit_fills[o["sym"]] = {"qty": sz, "avg_px": avg}
                elif why_no:
                    stuck.append({"sym": o["sym"], "reason": why_no[:120]})
            # A refused EXIT is materially worse than a refused entry: real
            # exposure we decided to close and could not. On 2026-08-10 a BTC
            # close was refused for a duplicate nonce and the position stayed on
            # the book with nothing reporting it. Alarm on this one specifically.
            if stuck:
                runtime.emit_event(
                    "live.exit_rejected",
                    {
                        "count": len(stuck),
                        "detail": stuck[:8],
                        "height": int(runtime.get_block_height()),
                    },
                )
            self.storage["orders"] = audit[-50:]
        portfolio = self._enforce_exits(
            portfolio,
            prices,
            float(cfg.get("stop_loss_pct", 0.0)),
            float(cfg.get("take_profit_pct", 0.0)),
            exit_fills,
        )
        unreal, _m = _mark_book(portfolio, prices)
        runtime.emit_event(
            "live.marked", {"unrealized_pnl": unreal, "positions": len(portfolio)}
        )
        retry_symbols = list(self.storage.get("mark_price_retry_symbols", []))
        self.storage["mark_price_retry_symbols"] = []
        if retry_symbols:
            self._schedule_followup(
                "mark_price_retry_tick",
                {
                    "symbols": retry_symbols,
                },
            )
        elif self.storage.get("notify_addr"):
            # A mark final cannot also afford a nested notifier call. Publish
            # on the next block in a dedicated 512-access timer transaction.
            self._schedule_command_snapshot()
        return _json_bytes({"unrealized_pnl": unreal, "positions": len(portfolio)})

    @callable_by(SELF, decode="raw")
    @deferred
    def command_snapshot_tick(self, payload):
        return b"sent" if self._push_command_snapshot() else b"not sent"

    def _schedule_followup(self, handler, payload):
        # Omitted fee_payer bills this actor, regardless of callback sender.
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            _json_bytes({"_handler": handler, "_payload": payload}),
            access_limit=TIMER_ACCESS_LIMIT,
        )

    def _schedule_command_snapshot(self):
        # The notifier mutates four cache keys in its nested frame, so this
        # needs more access headroom than the usual one-read follow-up. Canyon's
        # default timer entitlement caps requested access at 375.
        runtime.schedule_timer_ex(
            int(runtime.get_block_height()) + 1,
            _json_bytes({"_handler": "command_snapshot_tick", "_payload": {}}),
            access_limit=COMMAND_SNAPSHOT_ACCESS_LIMIT,
        )

    def _enforce_exits(self, portfolio, prices, sl, tp, exit_fills=None):

        force_close = self.storage.get("risk_halt_reason") == "cumulative_loss"
        if sl <= 0 and tp <= 0 and not force_close:
            return portfolio
        height = int(runtime.get_block_height())
        realized = float(self.storage.get("realized_pnl", 0.0))
        closed = int(self.storage.get("closed_count", 0))
        wins = int(self.storage.get("win_count", 0))
        history = list(self.storage.get("history", []))
        kept, exits = [], 0
        for p in portfolio:
            cur = prices.get(str(p.get("asset", "")).upper())
            reason = (
                "cumulative_loss"
                if self.storage.get("risk_halt_reason") == "cumulative_loss"
                and cur is not None
                else _exit_reason(p.get("side"), p.get("entry_px"), cur, sl, tp)
            )
            if not reason:
                kept.append(p)
                continue
            # A close is only real when the reduce-only order filled. Dropping
            # an unfilled close would leave venue exposure missing from the book.
            f = (exit_fills or {}).get(str(p.get("asset", "")).upper())
            if not f:
                kept.append(p)
                continue
            remainder, pnl, rec = _close_from_fill(p, f, reason, height)
            if remainder:
                kept.append(remainder)
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
                "live.exits", {"closed": exits, "realized_pnl": realized}
            )
        return kept

    @callable_by(OWNER, decode="raw")
    @deferred
    def start(self, payload):
        if self.storage.get("running"):
            return b"ok: already running"
        error = self._configuration_error()
        if error:
            return error
        self.storage["running"] = True
        cfg = self.storage["cfg"]
        self._schedule("rebalance_tick", int(cfg["rebalance_interval_blocks"]))
        self._schedule("mark_tick", int(cfg["mark_interval_blocks"]))
        self._schedule("universe_tick", int(cfg.get("universe_interval_blocks", 4001)))
        runtime.emit_event("live.started", {})
        return b"ok: running"

    @callable_by(OWNER, decode="raw")
    def stop(self, payload):
        self.storage["running"] = False
        runtime.emit_event("live.stopped", {})
        return b"ok: stopped"

    @callable_by(OWNER, decode="raw")
    def clear_risk_halt(self, payload):
        """Explicit owner acknowledgement required after a cumulative-loss halt."""
        if _parse(payload).get("confirm") is not True:
            return b"confirmation required"
        previous = str(self.storage.get("risk_halt_reason", ""))
        if previous == "cumulative_loss":
            baseline = self._strategy_pnl(self.storage.get("last_prices", {}))
            if baseline is None:
                return b"fresh prices required"
            self.storage["cumulative_loss_baseline_pnl"] = float(baseline)
        self.storage["risk_halted"] = False
        self.storage["risk_halt_reason"] = ""
        self.storage["mark_price_retry_attempts"] = 0
        runtime.emit_event("live.risk_halt_cleared", {"previous_reason": previous})
        return b"ok: risk halt cleared"

    def _schedule(self, handler, interval):
        height = int(runtime.get_block_height()) + int(interval)
        payload = _json_bytes({"_handler": handler})
        # The SDK provides the basic-scheduler fallback for mock hosts.
        runtime.schedule_timer_ex(
            height,
            payload,
            fee_payer=runtime.get_sender(),
            access_limit=TIMER_ACCESS_LIMIT,
        )

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
                "risk_halted": bool(self.storage.get("risk_halted", False)),
                "risk_halt_reason": str(self.storage.get("risk_halt_reason", "")),
                "account_state": self.storage.get("account_state", {}),
                "apply_pending": bool(self.storage.get("apply_pending", False)),
                "post_reconcile_pending": bool(
                    self.storage.get("post_reconcile_pending", False)
                ),
            }
        )

    @public(decode="raw")
    @pure
    def get_account_state(self, payload):
        return _json_bytes(self.storage.get("account_state", {}))

    @public(decode="raw")
    def preview(self, payload):
        """Digest for an action, computed on-chain, sending nothing."""
        obj = _parse(payload)
        cfg = self.storage.get("cfg", {})
        action = order_action(
            int(obj.get("asset", 3)),
            bool(obj.get("is_buy", True)),
            str(obj.get("price", "1.0")),
            str(obj.get("size", "0.001")),
            reduce_only=False,
            tif="Gtc",
        )
        nonce = int(obj.get("nonce") or runtime.get_timestamp_ms())
        digest = hl_digest(action, nonce, is_mainnet=False)
        return _json_bytes(
            {
                "action": action,
                "nonce": nonce,
                "digest": "0x" + digest.hex(),
                "timestamp_ms": int(runtime.get_timestamp_ms()),
            }
        )


def _get_actor():
    return LiveExecution()


def init(payload):
    return _get_actor().init(payload)


def set_research(payload):
    return _get_actor().set_research(payload)


def set_notify(payload):
    return _get_actor().set_notify(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def apply_ideas(payload):
    return _get_actor().apply_ideas(payload)


def rebalance(payload):
    return _get_actor().rebalance(payload)


def rebalance_tick(payload):
    return _get_actor().rebalance_tick(payload)


def rebalance_apply_tick(payload):
    return _get_actor().rebalance_apply_tick(payload)


def research_stats_tick(payload):
    return _get_actor().research_stats_tick(payload)


def apply_prices_tick(payload):
    return _get_actor().apply_prices_tick(payload)


def apply_stocks_tick(payload):
    return _get_actor().apply_stocks_tick(payload)


def apply_order_tick(payload):
    return _get_actor().apply_order_tick(payload)


def mark_now(payload):
    return _get_actor().mark_now(payload)


def mark_tick(payload):
    return _get_actor().mark_tick(payload)


def mark_price_retry_tick(payload):
    return _get_actor().mark_price_retry_tick(payload)


def universe_tick(payload):
    return _get_actor().universe_tick(payload)


def universe_now(payload):
    return _get_actor().universe_now(payload)


def prices_now(payload):
    return _get_actor().prices_now(payload)


def _refresh_prices__resume(payload):
    return _get_actor()._refresh_prices__resume(payload)


def start(payload):
    return _get_actor().start(payload)


def stop(payload):
    return _get_actor().stop(payload)


def clear_risk_halt(payload):
    return _get_actor().clear_risk_halt(payload)


def _apply__resume(payload):
    return _get_actor()._apply__resume(payload)


def _pull_research__resume(payload):
    return _get_actor()._pull_research__resume(payload)


def _pull_research_stats__resume(payload):
    return _get_actor()._pull_research_stats__resume(payload)


def _apply_monolithic_unused__resume(payload):
    return _get_actor()._apply_monolithic_unused__resume(payload)


def _apply_prices__resume(payload):
    return _get_actor()._apply_prices__resume(payload)


def _apply_stocks__resume(payload):
    return _get_actor()._apply_stocks__resume(payload)


def _apply_order__resume(payload):
    return _get_actor()._apply_order__resume(payload)


def apply_followup_tick(payload):
    return _get_actor().apply_followup_tick(payload)


def _mark__resume(payload):
    return _get_actor()._mark__resume(payload)


def command_snapshot_tick(payload):
    return _get_actor().command_snapshot_tick(payload)


def _mark_exits__resume(payload):
    return _get_actor()._mark_exits__resume(payload)


def mark_exits_tick(payload):
    return _get_actor().mark_exits_tick(payload)


def _reconcile_after_orders__resume(payload):
    return _get_actor()._reconcile_after_orders__resume(payload)


# Every @runner.continuation needs its generated __resume exported at module
# level — the chain invokes the callback BY NAME. Without this the job result
# comes back, the callback is scheduled, and there is no function to receive
# it: leg 1 dispatches, leg 2 never does, and the continuation dies silently.
def _refresh_universe__resume(payload):
    return _get_actor()._refresh_universe__resume(payload)


def get_portfolio(payload):
    return _get_actor().get_portfolio(payload)


def get_pnl(payload):
    return _get_actor().get_pnl(payload)


def get_scorecard(payload):
    return _get_actor().get_scorecard(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)


def get_account_state(payload):
    return _get_actor().get_account_state(payload)


def preview(payload):
    return _get_actor().preview(payload)
