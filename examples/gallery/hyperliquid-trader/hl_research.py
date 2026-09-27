"""Research actor (self-contained) — generate + reality-test trading theses"""

import base64
import json
import re

import hl_common
from cowboy_sdk import (
    OWNER,
    SELF,
    AddressError,
    Verify,
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

_parse = hl_common._parse
_json_bytes = hl_common._json_bytes
_b64_text = hl_common._b64_text
_result_text = hl_common._result_text
_normalize_result = hl_common._normalize_result
normalize_address = hl_common.normalize_address
mark_pending = hl_common.mark_pending
tick_pending_guard = hl_common.tick_pending_guard

HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DISCOVER_URL = "https://api.hyperliquid-testnet.xyz/info"
_INGEST_BATCH = 6
_CRYPTO_THEMES = {"CRYPTO", "COINS", "DIGITAL ASSETS", "ALTCOINS"}
_HDRS = {"Content-Type": "application/json"}

DEX_GROUPS = (("coins", ""), ("stocks", "xyz"), ("preipo", "vntl"))
DEFAULT_BRIEF_CHECK_INTERVAL_BLOCKS = 449
DEFAULT_BRIEF_UTC_OFFSET_HOURS = -4
DEFAULT_BRIEF_MORNING_HOUR = 9
DEFAULT_BRIEF_EVENING_HOUR = 17
DEFAULT_BRIEF_AUTO_DST = True


def _sender_matches(expected):
    """Match an authenticated runtime sender against a configured actor."""
    try:
        return runtime.get_sender() == to_address_bytes(str(expected or "").strip())
    except AddressError:
        return False


# Below this many observations a per-asset track record is noise, not evidence.
# Positions here are held ~20-60 min, so a single outcome is dominated by market
# drift rather than by the decision — n=1 rows must not read as established fact.
MIN_SAMPLES_FOR_CONFIDENCE = 5
SYNTH_MAX_TOKENS = 2000

SYNTH_SYSTEM_PROMPT = (
    "You are a markets research analyst that outputs ONLY machine-readable JSON. "
    "Do NOT write prose, markdown, headings, or explanation outside the JSON. "
    "You cover TWO asset classes: crypto and US equities. "
    "Choose assets from the assets_of_interest list given in the DATA below — "
    "it contains BOTH crypto and US equities; treat them as equal candidates. "
    "You MAY also use a ticker that appears VERBATIM in the news items when a "
    "story directly implicates it — but never invent a ticker, never guess one "
    "from a company name, and never reuse a ticker that appears in THIS "
    "instruction. Ideas on unlisted or illiquid names are dropped downstream, "
    "so prefer assets_of_interest when conviction is equal. "
    "FIRST write economic_context: a 1-3 "
    "sentence summary of the CURRENT market/macro backdrop (risk sentiment, rates, "
    "sector leadership, notable moves) inferred from the items and prices — the "
    "state of the world, not a specific bet. THEN generate ONE new trading thesis "
    "(framed as a MACRO view whose long and "
    "short baskets SPAN both asset classes (crypto AND US equities) wherever the "
    "SAME view implies a position there — put EVERY asset the view supports into the "
    "thesis long/short lists, not just one asset class (e.g. a risk-on view longs "
    "the assets it favors across both classes and shorts the laggards; a risk-off "
    "view shorts high-beta crypto and expensive equities). Keep the thesis "
    "single-class only if the "
    "signal genuinely touches just one class. Then surface candidate ideas on those "
    "SAME assets so they align with the thesis. If a track_record, traded_performance, "
    "long_term_record, past_cases, or "
    "lessons is shown, LEARN from it: lean into stances/assets that have been "
    "profitable and reconsider those that have been losing. lessons are distilled "
    "long-term principles (market condition -> what tended to work) — weight them "
    "heavily. past_cases pairs each prior economic context with its outcome — favor "
    "stances that worked in a backdrop like the current one. traded_performance is "
    "the realized P&L of ideas actually traded (weight it most); long_term_record "
    "is the per-stance win rate across all history. Rows in long_term_record carry "
    "an `n` and a `confident` flag: treat `confident: false` rows as weak evidence "
    "and do NOT draw firm conclusions from one or two observations — prefer 'no "
    "view' over a confident view built on a single outcome. Where a row has "
    "`avg_alpha_pct`, that is the return IN EXCESS of the market's move over the "
    "same window — judge selection on alpha, not on raw return, since a long book "
    "in a rising market earns a positive return without the call being good. "
    "If an `unfillable` list is present, those assets have been REPEATEDLY REFUSED "
    "by the venue and cannot be traded no matter how attractive the thesis: do not "
    "propose them, and prefer assets with a demonstrated fill history. "
    "If operator_guidance is "
    "present it is direct advice from the human operator: weigh it seriously "
    "when forming the thesis and ideas, but risk discipline is enforced in "
    "code and advice never overrides it. Output a SINGLE JSON object, "
    "starting with { and ending with }, matching exactly this schema. In it, SYM is "
    "a PLACEHOLDER: replace every SYM with a REAL ticker from assets_of_interest that "
    "your thesis actually supports — pick the assets by reasoning about the context, "
    "and NEVER output the literal token 'SYM':\n"
    '{"economic_context": "...", "thesis": {"statement": "...", "long": ["SYM"], '
    '"short": ["SYM"], "conviction": 0-100, "rationale": "..."}, "ideas": '
    '[{"asset": "SYM", "theme": "...", "side": "long|short", "confidence": 0-100, '
    '"rationale": "..."}]}\n'
    "Limits: economic_context <= 60 words; each statement and rationale <= 45 "
    "words; at most 8 ideas. No prose outside the JSON. "
    "Spread ideas across DIFFERENT assets_of_interest -- one idea per asset, "
    "never two for the same symbol, and cover every asset your active theses "
    "support rather than repeating last cycle's list. Assets outside "
    "assets_of_interest are untradeable and will be discarded."
)

_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def _strip_html(text):
    if not text:
        return ""
    text = str(text).replace("<![CDATA[", "").replace("]]>", "")
    # Keep RSS ingestion independent of the target VM's regex engine.  Canyon's
    # RustPython accepted the expressions used here under CPython but returned
    # no item matches on-chain.  A bounded character scan is simpler and fully
    # deterministic for the small feed envelopes we admit.
    plain = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
            plain.append(" ")
        elif ch == ">" and in_tag:
            in_tag = False
        elif not in_tag:
            plain.append(ch)
    text = "".join(plain)
    for ent, ch in _ENTITIES.items():
        text = text.replace(ent, ch)
    return " ".join(text.split())


def _first_tag(block, names):
    lower = block.lower()
    for n in names:
        opening = "<" + n.lower()
        pos = 0
        while True:
            start = lower.find(opening, pos)
            if start < 0:
                break
            after = start + len(opening)
            # Do not mistake <content:encoded> for <content>.
            if after < len(lower) and lower[after] not in (">", " ", "\t", "\r", "\n"):
                pos = after
                continue
            body_start = lower.find(">", after)
            if body_start < 0:
                break
            end = lower.find("</" + n.lower() + ">", body_start + 1)
            if end >= 0:
                return _strip_html(block[body_start + 1 : end])
            pos = body_start + 1
    return ""


def _blocks(xml, tag, max_items):
    lower = xml.lower()
    opening = "<" + tag
    closing = "</" + tag + ">"
    out = []
    pos = 0
    while len(out) < int(max_items):
        start = lower.find(opening, pos)
        if start < 0:
            break
        after = start + len(opening)
        if after < len(lower) and lower[after] not in (">", " ", "\t", "\r", "\n"):
            pos = after
            continue
        body_start = lower.find(">", after)
        end = lower.find(closing, body_start + 1) if body_start >= 0 else -1
        if body_start < 0 or end < 0:
            break
        out.append(xml[start : end + len(closing)])
        pos = end + len(closing)
    return out


def _link_href(block):
    lower = block.lower()
    pos = 0
    while True:
        start = lower.find("<link", pos)
        if start < 0:
            return ""
        after = start + 5
        if after < len(lower) and lower[after] not in (">", " ", "\t", "\r", "\n"):
            pos = after
            continue
        end = lower.find(">", after)
        if end < 0:
            return ""
        tag = block[start : end + 1]
        tag_lower = tag.lower()
        attr = tag_lower.find("href=")
        if attr >= 0:
            value_start = attr + 5
            if value_start < len(tag) and tag[value_start] in ('"', "'"):
                quote = tag[value_start]
                value_end = tag.find(quote, value_start + 1)
                if value_end >= 0:
                    return tag[value_start + 1 : value_end].strip()
        pos = end + 1


def _parse_rss(xml, max_items=15):
    if not xml:
        return []
    xml = str(xml)
    blocks = _blocks(xml, "item", max_items)
    if not blocks:
        blocks = _blocks(xml, "entry", max_items)
    out = []
    for block in blocks[: int(max_items)]:
        title = _first_tag(block, ["title"])
        url = _first_tag(block, ["link", "guid", "id"])
        if not url:
            url = _link_href(block)
        summary = _first_tag(block, ["description", "summary", "content"])
        if title or url:
            out.append({"title": title, "url": url.strip(), "summary": summary[:500]})
    return out


def _url_key(url):
    u = str(url or "").strip().lower()
    if u.startswith("http://"):
        u = u[7:]
    elif u.startswith("https://"):
        u = u[8:]
    return u.split("#", 1)[0].rstrip("/")


def plan_ingest(feeds, off, batch_max):
    n = len(feeds or [])
    if n == 0:
        return [], 0
    batch = n if n < batch_max else batch_max
    queue = [feeds[(off + i) % n] for i in range(batch)]
    return queue, (off + batch) % n


def stage_items(raw_result, cfg, seen, candidates, feed_url, height):
    entries = _parse_rss(_result_text(raw_result), cfg["max_items_per_feed"])
    seen = list(seen or [])
    seen_set = set(seen)
    cand = list(candidates or [])
    n = 0
    for e in entries:
        key = _url_key(e.get("url", ""))
        if not key or key in seen_set:
            continue
        seen_set.add(key)
        seen.append(key)
        e["feed"] = feed_url
        e["block"] = height
        cand.append(e)
        n += 1
    return seen[-int(cfg["max_seen"]) :], cand, n


def score_prompt(items, coins, mandate=""):

    lines = [
        "%d. %s: %s"
        % (i + 1, str(it.get("title", ""))[:120], str(it.get("summary", ""))[:150])
        for i, it in enumerate(items)
    ]
    m = (" Standing mandate: " + str(mandate) + ".") if mandate else ""
    return (
        "You are a relevance filter for a cross-asset MACRO trading desk covering these "
        "assets: "
        + ", ".join(coins)
        + "."
        + m
        + " Rate each news item 1-10. HIGH (7-10): directly market-moving for one of these "
        "assets, OR important macro/economy/rates/policy/risk-sentiment news that sets the "
        "market backdrop (Fed, inflation, growth, jobs, geopolitics). MEDIUM (4-6): related "
        "sector or adjacent-asset news. LOW (1-3): off-topic (sports, lifestyle, local, "
        "unrelated product launches). Return ONLY comma-separated integers in item order, "
        "e.g. 8,3,9,5 — no other text.\n\n" + "\n".join(lines)
    )


def parse_scores(result, n):
    nums = re.findall(r"\d+", _result_text(result))
    scores = [max(0, min(10, int(x))) for x in nums][:n]
    scores += [0] * (n - len(scores))
    return scores


def _days_from_civil(year, month, day):
    """Days since the Unix epoch for a Gregorian civil date."""
    year -= 1 if month <= 2 else 0
    era = year // 400
    year_of_era = year - era * 400
    month_prime = month + (-3 if month > 2 else 9)
    day_of_year = (153 * month_prime + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _civil_year(epoch_day):
    """Gregorian year containing a day counted from the Unix epoch."""
    z = int(epoch_day) + 719468
    era = z // 146097
    day_of_era = z - era * 146097
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (
        365 * year_of_era + year_of_era // 4 - year_of_era // 100
    )
    month_prime = (5 * day_of_year + 2) // 153
    month = month_prime + (3 if month_prime < 10 else -9)
    return year + (1 if month <= 2 else 0)


def _sunday_on_or_after(year, month, day):
    # 1970-01-01 was Thursday; with Sunday=0 its weekday index is 4.
    weekday = (_days_from_civil(year, month, day) + 4) % 7
    return day + ((7 - weekday) % 7)


def eastern_utc_offset_hours(ts_ms):
    """US Eastern UTC offset at a consensus timestamp (-5 EST / -4 EDT)."""
    seconds = int(ts_ms) // 1000
    year = _civil_year(seconds // 86400)
    second_sunday_march = _sunday_on_or_after(year, 3, 8)
    first_sunday_november = _sunday_on_or_after(year, 11, 1)
    # DST starts at 02:00 EST (07:00 UTC) and ends at 02:00 EDT
    # (06:00 UTC), per the current US rule.
    start = _days_from_civil(year, 3, second_sunday_march) * 86400 + 7 * 3600
    end = _days_from_civil(year, 11, first_sunday_november) * 86400 + 6 * 3600
    return -4 if start <= seconds < end else -5


def brief_slot(ts_ms, utc_offset_hours, morning_hour, evening_hour):
    """(slot_key, mode) for the consensus timestamp, or (None, None)."""
    local = int(ts_ms) // 1000 + int(utc_offset_hours) * 3600
    day = local // 86400
    hour = (local % 86400) / 3600.0
    if hour >= evening_hour:
        return ("%d-pm" % day, "evening")
    if hour >= morning_hour:
        return ("%d-am" % day, "morning")
    return (None, None)


def compose_brief(stats, theses, ideas, pnl, universe, mode="morning"):
    """Plain-text daily brief from stored state. No LLM: cheap, deterministic,."""
    lines = [
        "🤠 Good morning — plan for today"
        if mode == "morning"
        else "🤠 End-of-day recap"
    ]
    th = [t for t in (theses or []) if t.get("status") == "active"]
    if th:
        lines.append("View: " + str(th[-1].get("statement", ""))[:200])
        c = str(th[-1].get("context", ""))[:180]
        if c:
            lines.append("Context: " + c)
    if ideas:
        tops = ", ".join(
            "%s %s(%s)" % (i.get("asset"), i.get("side"), i.get("confidence"))
            for i in ideas[:5]
        )
        lines.append("Ideas: " + tops)
    if pnl:
        lines.append(
            "Book: %s open, equity $%.2f, realized $%.2f"
            % (
                pnl.get("open_positions", 0),
                float(pnl.get("equity", 0) or 0),
                float(pnl.get("realized_pnl", 0) or 0),
            )
        )
    if universe:
        lines.append("Universe (%d): %s" % (len(universe), ", ".join(universe[:12])))
    if mode == "morning":
        lines.append(
            "Any advice for today? Reply here — the advice window resets "
            "each morning and I read it before every thesis."
        )
    return "\n".join(lines)


def universe_symbols(cfg):
    out = []
    for key, _dex in DEX_GROUPS:
        for s in cfg.get(key) or []:
            u = str(s).upper()
            if u and u not in out:
                out.append(u)
    return out


def meta_ctxs_query(dex):
    return json.dumps({"type": "metaAndAssetCtxs", "dex": dex})


def discover_stocks(result, pinned, held, min_vol, cap):

    data = result
    if not isinstance(data, list):
        data = _normalize_result(data)
        if isinstance(data, dict):
            data = data.get("value")
    if not isinstance(data, list) or len(data) < 2:
        return list(pinned), 0, 0
    universe = (data[0] or {}).get("universe") or []
    ctxs = data[1] or []
    scored = []
    for u, c in zip(universe, ctxs):
        name = str((u or {}).get("name", "")).upper()
        # Builder DEX metadata namespaces symbols (for example, xyz:NVDA and
        # vntl:SPACEX); research stores the canonical asset name without it.
        if ":" in name:
            name = name.split(":", 1)[1]
        try:
            vol = float((c or {}).get("dayNtlVlm", 0) or 0)
        except Exception:
            vol = 0.0
        if name and vol >= float(min_vol):
            scored.append((vol, name))
    scored.sort(reverse=True)
    out = list(pinned)
    added = 0
    for _vol, name in scored:
        if added >= int(cap):
            break
        if name not in out:
            out.append(name)
            added += 1
    kept = 0
    for h in held:
        if h and h not in out:
            out.append(h)
            kept += 1
    return out, added, kept


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


def resolve_all(cfg, p_coins, p_stocks, p_preipo):
    return resolve_prices(
        universe_symbols(cfg), [("", p_coins), ("xyz", p_stocks), ("vntl", p_preipo)]
    )


def research_cfg(obj):
    return {
        "coins": list(obj.get("coins", ["BTC", "ETH", "SOL"])),
        "stocks": list(obj.get("stocks", [])),
        "preipo": list(obj.get("preipo", [])),
        "price_url": str(obj.get("price_url", HL_INFO)),
        "max_items": int(obj.get("max_items", 30)),
        "max_items_per_feed": int(obj.get("max_items_per_feed", 15)),
        "max_seen": int(obj.get("max_seen", 500)),
        "min_score": int(obj.get("min_score", 7)),
        "max_keep": int(obj.get("max_keep", 8)),
        "max_ideas": int(obj.get("max_ideas", 6)),
        "max_theses": int(obj.get("max_theses", 12)),
        "max_archive": int(obj.get("max_archive", 40)),
        "archive_keep": int(obj.get("archive_keep", 20)),
        "max_lessons": int(obj.get("max_lessons", 8)),
        "ingest_interval_blocks": int(obj.get("ingest_interval_blocks", 149)),
        "synth_interval_blocks": int(obj.get("synth_interval_blocks", 599)),
        "validate_interval_blocks": int(obj.get("validate_interval_blocks", 3593)),
        "distill_interval_blocks": int(obj.get("distill_interval_blocks", 7207)),
        "min_age_blocks": int(obj.get("min_age_blocks", 3000)),
        "min_return_pct": float(obj.get("min_return_pct", 0.0)),
        "max_poll_wait_blocks": int(obj.get("max_poll_wait_blocks", 300)),
        "max_poll_wait_ms": int(obj.get("max_poll_wait_ms", 1_800_000)),
        "discover_interval_blocks": int(obj.get("discover_interval_blocks", 997)),
        "brief_check_interval_blocks": int(
            obj.get("brief_check_interval_blocks", DEFAULT_BRIEF_CHECK_INTERVAL_BLOCKS)
        ),
        "brief_utc_offset_hours": int(
            obj.get("brief_utc_offset_hours", DEFAULT_BRIEF_UTC_OFFSET_HOURS)
        ),
        # New default configurations track Eastern wall time. Explicit offset
        # users retain fixed-offset semantics unless they opt back into DST.
        "brief_auto_dst": bool(
            obj.get(
                "brief_auto_dst",
                (DEFAULT_BRIEF_AUTO_DST and "brief_utc_offset_hours" not in obj),
            )
        ),
        "brief_morning_hour": int(
            obj.get("brief_morning_hour", DEFAULT_BRIEF_MORNING_HOUR)
        ),
        "brief_evening_hour": int(
            obj.get("brief_evening_hour", DEFAULT_BRIEF_EVENING_HOUR)
        ),
        "guidance_max_age_blocks": int(obj.get("guidance_max_age_blocks", 57600)),
        "discover_url": str(obj.get("discover_url", DEFAULT_DISCOVER_URL)),
        "min_discover_volume": float(obj.get("min_discover_volume", 20.0)),
        "max_discovered": int(obj.get("max_discovered", 24)),
        "num_runners": int(obj.get("num_runners", 1)),
        "threshold": int(obj.get("threshold", 1)),
    }


def stats_view(st):
    cfg = st.get("cfg", {})
    theses = st.get("theses", [])
    return {
        "running": bool(st.get("running", False)),
        "mandate": st.get("mandate", ""),
        "feeds": len(st.get("feeds", [])),
        "universe": len(universe_symbols(cfg)),
        "window": len(st.get("items", [])),
        "active_theses": len(_active_theses(theses)),
        "total_theses": len(theses),
        "ingest_runs": int(st.get("ingest_runs", 0)),
        "synth_runs": int(st.get("synth_runs", 0)),
        "validate_runs": int(st.get("validate_runs", 0)),
        "distill_runs": int(st.get("distill_runs", 0)),
        "lessons": len(st.get("lessons", [])),
    }


def wiki_view(st):
    theses = list(st.get("theses", []))
    archive = list(st.get("archive", []))
    return {
        "mandate": st.get("mandate", ""),
        "active_theses": [
            {
                "statement": t.get("statement", ""),
                "conviction": t.get("conviction", 0),
                "context": t.get("context", ""),
                "return_pct": round(float(t.get("last_return", 0) or 0) * 100, 2),
            }
            for t in _active_theses(theses)
        ],
        "long_term_record": _wiki_aggregate(archive + theses),
        "cases": _wiki_cases(archive),
        "lessons": st.get("lessons", []),
    }


def universe_view(st):
    cfg = st.get("cfg", {})
    return {
        "coins": cfg.get("coins", []),
        "stocks": cfg.get("stocks", []),
        "preipo": cfg.get("preipo", []),
        "all": universe_symbols(cfg),
    }


def synth_prompt(context):
    return (
        SYNTH_SYSTEM_PROMPT
        + "\n\nDATA:\n"
        + context
        + "\n\nNow output ONLY the JSON object described above — no prose, "
        "no markdown fences. Begin with { and end with }."
    )


def _sentiment_trend(tape, fast=6, medium=24, slow=96):
    """Tape -> multi-scale deltas. O(1) work: slices a bounded list, no scan.

    Levels are near-useless to the model — "mean score 7.4" means nothing without
    a reference. What carries information is the DERIVATIVE and the divergence
    between horizons: sentiment rising over hours while falling over a day is a
    different regime from both rising, and neither is visible in a snapshot of
    eight headlines.

    Horizons are in tape entries (ingest cycles), not blocks or wall time, so
    they stay meaningful as the chain's block rate decays — a block-denominated
    window would silently cover 2.7x less history late in a run, which is exactly
    the trap every other budget in this system fell into.
    """
    t = [e for e in (tape or []) if isinstance(e, dict) and e.get("mean") is not None]
    if len(t) < 2:
        return None

    def avg(seg):
        return round(sum(float(e["mean"]) for e in seg) / len(seg), 3) if seg else None

    now = avg(t[-1:])
    out = {"now": now, "samples": len(t)}
    for label, n in (("fast", fast), ("medium", medium), ("slow", slow)):
        if len(t) >= 2:
            seg = t[-n:]
            base = avg(seg[: max(1, len(seg) // 2)])
            head = avg(seg[max(1, len(seg) // 2) :]) or now
            if base is not None and head is not None:
                out[label] = {
                    "window": len(seg),
                    "avg": avg(seg),
                    "delta": round(head - base, 3),
                }
    # Per-theme drift over the medium horizon: which narratives are gaining.
    recent, older = t[-medium // 2 :], t[-medium : -medium // 2] or t[:1]
    themes = {}
    for src, key in ((recent, "r"), (older, "o")):
        for e in src:
            for th, v in (e.get("themes") or {}).items():
                themes.setdefault(th, {"r": [], "o": []})[key].append(float(v))
    drift = {}
    for th, d in themes.items():
        if d["r"] and d["o"]:
            drift[th] = round(sum(d["r"]) / len(d["r"]) - sum(d["o"]) / len(d["o"]), 2)
    if drift:
        out["theme_drift"] = dict(sorted(drift.items(), key=lambda kv: -abs(kv[1]))[:6])
    return out


def _context_for_synthesis(
    items,
    coins,
    active_theses,
    mandate="",
    outcomes=None,
    traded=None,
    long_term=None,
    cases=None,
    lessons=None,
    guidance=None,
    unfillable=None,
    sentiment=None,
):
    learn = (
        "the memory (track_record, traded_performance, long_term_record, past_cases, lessons) below"
        if (outcomes or traded or long_term or cases or lessons)
        else None
    )
    ctx = {
        "instruction": (
            "Generate ONE new trading thesis (distinct from existing) and surface ideas"
            + (", consistent with the standing mandate below" if mandate else "")
            + ((", learning from " + learn + ".") if learn else ".")
        ),
        "assets_of_interest": list(coins or []),
        "existing_theses": [t.get("statement", "") for t in (active_theses or [])],
        "items": [
            {
                "title": i.get("title", ""),
                "summary": str(i.get("content") or i.get("summary", ""))[:300],
            }
            for i in (items or [])[:20]
        ],
    }
    if mandate:
        ctx["mandate"] = str(mandate)
    if guidance:
        ctx["operator_guidance"] = [str(g.get("text", ""))[:300] for g in guidance[-5:]]
    if outcomes:
        ctx["track_record"] = outcomes
    if traded:
        ctx["traded_performance"] = traded
    if long_term:
        ctx["long_term_record"] = long_term
    if cases:
        ctx["past_cases"] = cases
    if lessons:
        ctx["lessons"] = lessons
    if sentiment:
        # Direction, not level. `items` alone is a point-in-time snapshot; this
        # is how sentiment is MOVING across three horizons plus which themes are
        # gaining or fading. Compact by construction (a handful of numbers), so
        # it costs essentially nothing to serialize on the inline path.
        ctx["sentiment_trend"] = sentiment
    if unfillable:
        # Assets the venue has repeatedly refused. Without this the loop has no
        # way to represent "cannot be executed": a refused order never becomes a
        # position, so it never appears in traded_performance as a win or a loss,
        # and synth re-proposes it indefinitely.
        ctx["unfillable"] = unfillable
        # Also strip them from the candidate list, so the model is not asked to
        # consider assets it has just been told are untradeable.
        bad = {str(u.get("asset", "")).upper() for u in unfillable}
        ctx["assets_of_interest"] = [
            a for a in ctx["assets_of_interest"] if str(a).upper() not in bad
        ]
    return json.dumps(ctx, sort_keys=True)


def _clean_legs(vals):
    """Uppercase, de-blank, dedupe preserving order."""
    out, seen = [], set()
    for x in vals or []:
        sym = str(x).upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _parse_synthesis(analysis, universe=None):
    """Parse the synth reply -- and NORMALIZE it, because the model does not.

    Observed in one overnight run, all stored verbatim: a short list carrying
    PLTR/AAPL/MSFT/META twice each; a thesis with BTC in long AND short; a
    conviction-0 thesis with an empty-string short; ideas naming XRP, which is
    not in the universe and therefore unpriceable at the trader -- it was
    silently skipped there every tick while blocking an idea slot here.

    Rules: legs deduped and de-blanked; a symbol claimed both long and short is
    dropped from BOTH (a contradicted direction is not a position); conviction
    clamped to [0,100] and theses under 20 rejected outright (zero-conviction
    is noise by the model's own accounting); ideas deduped per asset (highest
    confidence wins) and, when `universe` is given, restricted to it.
    """
    data = _normalize_result(analysis)
    thesis = None
    if isinstance(data, dict) and isinstance(data.get("thesis"), dict):
        t = data["thesis"]
        longs = _clean_legs(t.get("long"))
        shorts = _clean_legs(t.get("short"))
        both = set(longs) & set(shorts)
        longs = [x for x in longs if x not in both]
        shorts = [x for x in shorts if x not in both]
        conviction = max(0, min(100, int(t.get("conviction", 0) or 0)))
        thesis = {
            "statement": str(t.get("statement", ""))[:280],
            "long": longs,
            "short": shorts,
            "conviction": conviction,
            "rationale": str(t.get("rationale", ""))[:280],
            "context": str(data.get("economic_context", ""))[:400],
        }
        if conviction < 20 or not (longs or shorts):
            thesis = None
    allowed = {str(u).upper() for u in (universe or [])}
    by_asset = {}
    raw = data.get("ideas") if isinstance(data, dict) else None
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            side = str(item.get("side", "")).lower()
            if side not in ("long", "short"):
                continue
            asset = str(item.get("asset", "")).upper().strip()
            if not asset or (allowed and asset not in allowed):
                continue
            idea = {
                "asset": asset,
                "theme": str(item.get("theme", "")).lower(),
                "side": side,
                "confidence": int(item.get("confidence", 0) or 0),
                "rationale": str(item.get("rationale", ""))[:280],
            }
            prev = by_asset.get(asset)
            if prev is None or idea["confidence"] > prev["confidence"]:
                by_asset[asset] = idea
    return thesis, list(by_asset.values())


def _thesis_fit(idea, thesis):
    tags = {str(idea.get("asset", "")).upper(), str(idea.get("theme", "")).upper()}
    tags.discard("")
    side = str(idea.get("side", "")).lower()
    longs = {str(a).upper() for a in thesis.get("long", [])}
    shorts = {str(a).upper() for a in thesis.get("short", [])}
    long_bias = bool(tags & longs)
    short_bias = bool(tags & shorts)
    if (side == "long" and long_bias) or (side == "short" and short_bias):
        return "aligns"
    if (side == "long" and short_bias) or (side == "short" and long_bias):
        return "contradicts"
    return "neutral"


def _thesis_direction(thesis, coin):
    coin = str(coin).upper()
    longs = {str(a).upper() for a in thesis.get("long", [])}
    shorts = {str(a).upper() for a in thesis.get("short", [])}
    if coin in longs and coin not in shorts:
        return 1
    if coin in shorts and coin not in longs:
        return -1
    if coin in longs and coin in shorts:
        return 0
    theme_long = bool(longs & _CRYPTO_THEMES)
    theme_short = bool(shorts & _CRYPTO_THEMES)
    if theme_long and not theme_short:
        return 1
    if theme_short and not theme_long:
        return -1
    return 0


def _thesis_return(thesis, baseline, current):
    total = 0.0
    n = 0
    for coin, base in (baseline or {}).items():
        try:
            base = float(base)
        except Exception:
            continue
        if not base:
            continue
        cur = (current or {}).get(coin)
        if cur is None:
            continue
        d = _thesis_direction(thesis, coin)
        if d == 0:
            continue
        total += d * (float(cur) - base) / base
        n += 1
    return (total / n) if n else 0.0


def _market_return(baseline, current):
    """Equal-weight unsigned move across EVERY asset in the baseline snapshot.

    The baseline captures the whole universe at thesis creation, not just the
    assets the thesis took a side on, so it is a usable benchmark for free —
    no extra fetch, no extra await.
    """
    total = 0.0
    n = 0
    for coin, base in (baseline or {}).items():
        try:
            base = float(base)
        except Exception:
            continue
        if not base:
            continue
        cur = (current or {}).get(coin)
        if cur is None:
            continue
        total += (float(cur) - base) / base
        n += 1
    return (total / n) if n else 0.0


def _thesis_alpha(thesis, baseline, current):
    """Return in EXCESS of what the thesis's net exposure would earn from drift.

    Without this the loop cannot tell skill from beta: "BTC long lost 0.26%" is
    recorded as a failure even when BTC fell 0.30% and the call was actually
    good, and a long book in a rising market is recorded as a success regardless
    of selection. On 2026-08-11 the wiki was drawing confident lessons from
    exactly this raw-return signal across ~20-60 min holds, where market drift
    dominates the decision entirely.

    Net exposure in [-1, 1] scales the benchmark: a balanced long/short book has
    ~0 beta and its whole return is selection, while a one-sided book is mostly
    beta and should be judged against it.
    """
    longs = len(thesis.get("long") or [])
    shorts = len(thesis.get("short") or [])
    if (longs + shorts) == 0:
        return 0.0
    net = (longs - shorts) / float(longs + shorts)
    return _thesis_return(thesis, baseline, current) - net * _market_return(
        baseline, current
    )


def _duplicate_of(new, theses, overlap=0.75):
    """Return the active thesis `new` merely restates, or None.

    Compared on POSITIONS rather than wording: two theses that long the same
    basket and short the same basket express the same bet however differently
    they are phrased, and the LLM rephrases freely between runs. Direction
    matters — the same assets held the opposite way is a genuinely different
    (indeed opposing) view and must not be collapsed.
    """

    def sig(t):
        return (
            frozenset(str(a).upper() for a in (t.get("long") or [])),
            frozenset(str(a).upper() for a in (t.get("short") or [])),
        )

    nl, ns = sig(new)
    if not nl and not ns:
        return None
    for t in theses or []:
        if t.get("status") != "active":
            continue
        tl, ts = sig(t)

        # Jaccard on each leg, requiring BOTH to match closely.
        def j(a, b):
            if not a and not b:
                return 1.0
            u = a | b
            return (len(a & b) / len(u)) if u else 1.0

        if j(nl, tl) >= overlap and j(ns, ts) >= overlap:
            return t
    return None


def _active_theses(theses):
    return [t for t in (theses or []) if t.get("status") == "active"]


def _newest_active(theses):
    for t in theses or []:
        if t.get("status") == "active":
            return t
    return None


def _wiki_aggregate(theses):
    bucket = {}
    alpha_bucket = {}
    for t in theses or []:
        ret = t.get("final_return")
        if ret is None:
            ret = t.get("last_return")
        if ret is None:
            continue
        alp = t.get("final_alpha")
        if alp is None:
            alp = t.get("last_alpha")
        for a in t.get("long") or []:
            bucket.setdefault(str(a).upper() + "|long", []).append(float(ret))
            if alp is not None:
                alpha_bucket.setdefault(str(a).upper() + "|long", []).append(float(alp))
        for a in t.get("short") or []:
            bucket.setdefault(str(a).upper() + "|short", []).append(float(ret))
            if alp is not None:
                alpha_bucket.setdefault(str(a).upper() + "|short", []).append(
                    float(alp)
                )
    out = []
    for key, rs in bucket.items():
        asset, side = key.split("|")
        n = len(rs)
        # `confident` marks whether this row carries enough observations to be
        # worth acting on. The synthesis prompt is told to weight realized
        # performance heavily, and with n=1 or n=2 that means weighting noise
        # heavily: on 2026-08-11 the wiki held EIGHT lessons drawn from NINE
        # closed trades, several stated as confident causal rules ("regulatory
        # clarity announcements fail to sustain upside") from a single
        # observation. Surfacing n and a flag lets the model discount thin rows
        # instead of treating every row as equally established.
        row = {
            "asset": asset,
            "side": side,
            "n": n,
            "avg_return_pct": round(sum(rs) / n * 100, 2),
            "win_rate": round(sum(1 for r in rs if r > 0) / n, 2),
            "confident": n >= MIN_SAMPLES_FOR_CONFIDENCE,
        }
        als = alpha_bucket.get(key)
        if als:
            # Excess return over the benchmark: this is the column that reflects
            # the DECISION rather than the market's drift.
            row["avg_alpha_pct"] = round(sum(als) / len(als) * 100, 2)
            row["alpha_win_rate"] = round(sum(1 for a in als if a > 0) / len(als), 2)
        out.append(row)
    # Strongest evidence first so a truncated context keeps the rows that matter.
    out.sort(key=lambda r: (-r["n"], r["asset"]))
    return out


def _thesis_outcomes(theses, limit=8):
    out = []
    for t in (theses or [])[:limit]:
        ret = (
            t.get("final_return")
            if t.get("status") == "retired"
            else t.get("last_return")
        )
        if ret is None:
            continue
        out.append(
            {
                "statement": t.get("statement", ""),
                "status": t.get("status", "active"),
                "return_pct": round(float(ret) * 100, 2),
            }
        )
    return out


def _wiki_cases(archive, limit=8):

    out = []
    for t in (archive or [])[:limit]:
        ctxt = str(t.get("context", ""))
        if not ctxt:
            continue
        out.append(
            {
                "context": ctxt,
                "long": t.get("long", []),
                "short": t.get("short", []),
                "return_pct": round(float(t.get("final_return", 0) or 0) * 100, 2),
            }
        )
    return out


def distill_prompt(archive, lessons, limit=20):

    cases = [
        {
            "context": str(t.get("context", ""))[:200],
            "long": t.get("long", []),
            "short": t.get("short", []),
            "return_pct": round(float(t.get("final_return", 0) or 0) * 100, 2),
        }
        for t in (archive or [])[:40]
    ]
    payload = json.dumps(
        {"existing_lessons": lessons or [], "retired_opinions": cases}, sort_keys=True
    )
    return (
        "You maintain the long-term trading memory for a cross-asset desk. Below are "
        "retired_opinions — each a market context, the stance taken (long/short), and its "
        "realized return_pct — plus existing_lessons. COMPRESS all of it into at most "
        + str(int(limit))
        + " durable, general LESSONS. Each lesson pairs a market condition "
        "with what tended to work or fail, e.g. 'When core inflation is elevated and the Fed "
        "is dovish, defensive quality-tech longs outperformed.' Merge overlapping lessons, "
        "drop noise, keep only what generalizes. Output ONLY JSON, no prose: "
        '{"lessons": ["...", "..."]}\n\nDATA:\n' + payload
    )


def parse_lessons(result, limit=20):
    data = _normalize_result(result)
    raw = data.get("lessons") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        s = str(x).strip()[:220]
        if s:
            out.append(s)
    return out[: int(limit)]


def _verify_spec(cfg):
    n = int(cfg.get("num_runners", 1))
    if n <= 1:
        return None
    return (
        Verify.builder()
        .mode("consensus")
        .runners(n)
        .threshold(int(cfg.get("threshold", n)))
        .check(Verify.semantic_similarity(threshold=0.8))
        .build()
    )


# node 04c9f283 types/src/constants.rs:1880-1881: 160 own + 162 child.
TIMER_ACCESS_LIMIT = 322


@actor
class HyperliquidResearch:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        if self.storage.get("cfg"):
            return b"ok: already initialized"
        self.storage["__name__"] = "Hyperliquid Research"
        self.storage["__description__"] = (
            "Generate + reality-test theses across crypto, equities, pre-IPO (no custody)"
        )
        self.storage["__version__"] = "6.0.0"
        obj = _parse(payload)
        if "running" in obj:
            raise ValueError("running is controlled by the start handler")
        self.storage["cfg"] = research_cfg(obj)
        self.storage["feeds"] = list(obj.get("feeds", []))
        self.storage["mandate"] = str(obj.get("mandate", ""))
        self.storage["exec_addr"] = normalize_address(
            obj.get("exec_addr", ""), "exec_addr"
        )
        self.storage["running"] = False
        self.storage["feed_index"] = 0
        self.storage["ingest_queue"] = []
        self.storage["seen"] = []
        self.storage["items"] = []
        self.storage["theses"] = []
        self.storage["archive"] = []
        self.storage["lessons"] = []
        self.storage["next_thesis_id"] = 1
        self.storage["ideas"] = []
        self.storage["last_prices"] = {}
        self.storage["last_scorecard"] = []
        self.storage["unfillable"] = []
        self.storage["last_guidance"] = []
        self.storage["last_pnl"] = {}
        self.storage["gathered_count"] = 0
        self.storage["ingest_runs"] = 0
        self.storage["synth_runs"] = 0
        self.storage["validate_runs"] = 0
        self.storage["distill_runs"] = 0
        for k in ("ingest", "synth", "validate", "distill"):
            self.storage[k + "_pending"] = False
            self.storage[k + "_started_block"] = 0
        runtime.emit_event(
            "research.init", {"universe": len(universe_symbols(self.storage["cfg"]))}
        )
        return b"ok: initialized"

    @callable_by(OWNER, decode="raw")
    def set_feeds(self, payload):
        feeds = list(_parse(payload).get("feeds", []))
        self.storage["feeds"] = feeds
        runtime.emit_event("research.feeds_set", {"count": len(feeds)})
        return _json_bytes({"feeds": feeds})

    @callable_by(OWNER, decode="raw")
    def set_universe(self, payload):
        obj = _parse(payload)
        cfg = dict(self.storage.get("cfg", {}))
        for key, _dex in DEX_GROUPS:
            if key in obj:
                cfg[key] = list(obj.get(key) or [])
        self.storage["cfg"] = cfg

        self.storage["stocks_pinned"] = list(cfg.get("stocks") or [])
        self.storage["coins_pinned"] = list(cfg.get("coins") or [])
        syms = universe_symbols(cfg)
        runtime.emit_event("research.universe_set", {"count": len(syms)})
        return _json_bytes({"universe": syms})

    @callable_by(OWNER, decode="raw")
    def set_mandate(self, payload):
        mandate = str(_parse(payload).get("mandate", ""))[:600]
        self.storage["mandate"] = mandate
        runtime.emit_event("research.mandate_set", {"len": len(mandate)})
        return _json_bytes({"mandate": mandate})

    @callable_by(OWNER, decode="raw")
    def set_exec(self, payload):
        addr = normalize_address(_parse(payload).get("exec_addr", ""), "exec_addr")
        self.storage["exec_addr"] = addr
        self.storage["last_scorecard"] = []
        self.storage["unfillable"] = []
        self.storage["last_pnl"] = {}
        runtime.emit_event("research.exec_set", {"addr": addr})
        return _json_bytes({"exec_addr": addr})

    @callable_by(OWNER, decode="raw")
    def set_notify(self, payload):
        addr = normalize_address(_parse(payload).get("notify_addr", ""), "notify_addr")
        self.storage["notify_addr"] = addr
        self.storage["last_guidance"] = []
        runtime.emit_event("research.notify_set", {"addr": addr})
        return _json_bytes({"notify_addr": addr})

    def _cached_guidance(self):
        g = list(self.storage.get("last_guidance", []))
        cutoff = int(runtime.get_block_height()) - int(
            self.storage["cfg"].get("guidance_max_age_blocks", 57600)
        )
        return [x for x in g if int(x.get("block", 0) or 0) >= cutoff]

    def _synthesis_payload(self):
        return _json_bytes(
            {
                "scorecard": list(self.storage.get("last_scorecard", [])),
                "guidance": self._cached_guidance(),
            }
        )

    @public(decode="raw")
    @deferred
    def handle_message(self, payload):
        """Store authenticated snapshots pushed by the trader or notifier."""
        obj = _parse(payload)
        kind = str(obj.get("type", ""))
        if kind == "trader_snapshot":
            if not _sender_matches(self.storage.get("exec_addr", "")):
                return b"ignored: unknown trader"
            scorecard = list(obj.get("scorecard") or [])
            unfillable = list(obj.get("unfillable") or [])
            self.storage["last_scorecard"] = scorecard
            self.storage["unfillable"] = unfillable
            self.storage["last_pnl"] = dict(obj.get("pnl") or {})
            runtime.emit_event(
                "research.trader_snapshot",
                {
                    "scorecard": len(scorecard),
                    "unfillable": len(unfillable),
                },
            )
            return b"stored"
        if kind == "guidance_snapshot":
            if not _sender_matches(self.storage.get("notify_addr", "")):
                return b"ignored: unknown notifier"
            guidance = list(obj.get("guidance") or [])
            self.storage["last_guidance"] = guidance
            runtime.emit_event("research.guidance_snapshot", {"count": len(guidance)})
            return b"stored"
        return b"ignored: unknown message"

    def _enqueue_brief(self, text, kind="brief"):
        addr = str(self.storage.get("notify_addr", ""))
        if not addr or not text:
            return False
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "notification",
                        "text": text,
                        "kind": kind,
                    }
                ),
            )
            return True
        except Exception as e:
            runtime.emit_event("research.notify_send_failed", {"err": str(e)[:80]})
            return False

    def _push_synthesis_snapshot(self):
        addr = str(self.storage.get("exec_addr", ""))
        if not addr:
            return False
        try:
            runtime.send_message(
                addr,
                _json_bytes(
                    {
                        "type": "research_snapshot",
                        "ideas": list(self.storage.get("ideas", [])),
                        "stats": stats_view(self.storage),
                        "notify_addr": str(self.storage.get("notify_addr", "")),
                    }
                ),
            )
            return True
        except Exception as e:
            runtime.emit_event("research.exec_send_failed", {"err": str(e)[:80]})
            return False

    @callable_by(OWNER, decode="raw")
    @deferred
    def start(self, payload):
        if self.storage.get("running"):
            return b"ok: already running"
        self.storage["running"] = True
        cfg = self.storage["cfg"]
        loops = (
            ("ingest_tick", "ingest_interval_blocks", 149),
            ("synth_tick", "synth_interval_blocks", 599),
            ("validate_tick", "validate_interval_blocks", 3593),
            ("distill_tick", "distill_interval_blocks", 7207),
            ("discover_tick", "discover_interval_blocks", 2003),
            (
                "brief_tick",
                "brief_check_interval_blocks",
                DEFAULT_BRIEF_CHECK_INTERVAL_BLOCKS,
            ),
        )
        armed = []
        for handler, key, default in loops:
            interval = int(cfg.get(key, default))
            if interval > 0:
                self._schedule(handler, interval)
                armed.append(handler)
        runtime.emit_event("research.started", {"timers": armed})
        return b"ok: running"

    @callable_by(OWNER, decode="raw")
    def stop(self, payload):
        self.storage["running"] = False
        runtime.emit_event("research.stopped", {})
        return b"ok: stopped"

    @callable_by(OWNER, decode="raw")
    @deferred
    def ingest_now(self, payload):
        self._mark_pending("ingest")
        return self._kick_ingest()

    @callable_by(OWNER, decode="raw")
    @deferred
    def synthesize_now(self, payload):
        self._mark_pending("synth")
        return self._synthesize(self._synthesis_payload())

    @callable_by(OWNER, decode="raw")
    @deferred
    def validate_now(self, payload):
        self._mark_pending("validate")
        return self._validate(b"{}")

    @callable_by(OWNER, decode="raw")
    @deferred
    def discover_now(self, payload):
        self._mark_pending("discover")
        return self._discover(b"{}")

    @callable_by(OWNER, decode="raw")
    @deferred
    def distill_now(self, payload):
        if not self.storage.get("archive"):
            return b"no archive"
        self._mark_pending("distill")
        return self._distill(b"{}")

    @callable_by(SELF, decode="raw")
    @deferred
    def ingest_tick(self, payload):
        if not self._tick_ok("ingest", "ingest_interval_blocks"):
            return b"skip"
        # A raise here rolls back the `_schedule` `_tick_ok` just did, and each
        # loop only arms its own successor — so the chain would end permanently.
        # Swallow, clear the guard, emit; one bad tick costs one cycle, not the loop.
        try:
            return self._kick_ingest()
        except Exception as e:
            self.storage["ingest_pending"] = False
            runtime.emit_event(
                "research.tick_failed", {"loop": "ingest", "err": str(e)[:200]}
            )
            return b"error"

    def _kick_ingest(self):
        feeds = self.storage.get("feeds") or []
        if not feeds:
            self.storage["ingest_pending"] = False
            self.storage["ingest_queue"] = []
            runtime.emit_event("research.no_feeds", {})
            return b"no feeds"
        queue, new_index = plan_ingest(
            feeds, int(self.storage.get("feed_index", 0)), _INGEST_BATCH
        )
        self.storage["feed_index"] = new_index
        self.storage["ingest_queue"] = queue[1:]
        self.storage["candidates"] = []
        runtime.emit_event("research.ingest_kicked", {"feeds": len(queue)})
        return self._ingest_feed(_json_bytes({"feed": queue[0]}))

    @runner.continuation(timeout_blocks=800)
    async def _ingest_feed(self, payload):

        ctx = capture()
        ctx.feed = _parse(payload).get("feed", "")
        r = await runner.http(ctx.feed, method="GET", timeout_ms=8000)
        self._stage_feed(ctx.feed, r)
        queue = list(self.storage.get("ingest_queue", []))
        if queue:
            self.storage["ingest_queue"] = queue[1:]
            return self._ingest_feed(_json_bytes({"feed": queue[0]}))
        if self.storage.get("candidates"):
            return self._score_candidates(b"{}")
        return self._finalize_ingest()

    def _stage_feed(self, feed_url, result):
        cfg = self.storage["cfg"]
        height = int(runtime.get_block_height())
        decoded = _result_text(result)
        new_seen, cand, n = stage_items(
            decoded,
            cfg,
            self.storage.get("seen", []),
            self.storage.get("candidates", []),
            feed_url,
            height,
        )
        self.storage["seen"] = new_seen
        self.storage["candidates"] = cand
        runtime.emit_event(
            "research.staged",
            {
                "feed": feed_url,
                "fresh": n,
                "candidates": len(cand),
                "response_chars": len(decoded),
                "item_markers": decoded.lower().count("<item"),
            },
        )

    @runner.continuation(timeout_blocks=800)
    async def _score_candidates(self, payload):

        ctx = capture()
        ctx.cfg = self.storage.get("cfg", {})
        cand = list(self.storage.get("candidates", []))
        prompt = score_prompt(
            cand, universe_symbols(ctx.cfg), self.storage.get("mandate", "")
        )
        ctx.scores = await runner.llm(prompt, max_tokens=400)
        return self._store_scored(ctx.scores)

    def _store_scored(self, scores_result):

        cfg = self.storage["cfg"]
        cand = list(self.storage.get("candidates", []))
        scores = parse_scores(scores_result, len(cand))
        for it, sc in zip(cand, scores):
            it["score"] = sc
        kept = [
            it for it in cand if int(it.get("score", 0)) >= int(cfg.get("min_score", 7))
        ]
        kept.sort(key=lambda it: int(it.get("score", 0)), reverse=True)
        kept = kept[: int(cfg.get("max_keep", 8))]
        window = (kept + list(self.storage.get("items", [])))[: int(cfg["max_items"])]
        self.storage["items"] = window
        self.storage["gathered_count"] = int(
            self.storage.get("gathered_count", 0)
        ) + len(kept)
        self.storage["candidates"] = []
        self._append_sentiment(cand, kept)
        runtime.emit_event("research.scored", {"scored": len(cand), "kept": len(kept)})
        return self._finalize_ingest()

    def _append_sentiment(self, scored, kept):
        """Append one aggregate to the sentiment tape.

        `items` is a rolling buffer capped at `max_items` (8 in practice), so it
        is a SNAPSHOT, not a history — nothing in the system could answer "is
        sentiment improving?" because the evidence rolled out before any trend
        could be computed. Worse, it truncates by COUNT, so the window's time
        depth silently varies with news volume: busy periods retain minutes,
        quiet ones hours.

        The tape fixes that by aggregating at WRITE time. Items still roll; the
        aggregate persists. Deliberately computed here rather than scanned at
        synthesis time — `_context_for_synthesis` runs on the inline path
        between awaits, so an O(n) scan there would cost block-production time
        on every synth, and grow all run (the same latent problem `_wiki_aggregate`
        already has over `archive`).

        Multi-scale needs no extra loops: one tape at ingest cadence gives fast /
        medium / slow by slicing it, so this adds ZERO timers — the resource the
        livelock actually spends.
        """
        vals = []
        for it in scored:
            try:
                vals.append(float(it.get("score", 0) or 0))
            except Exception:
                continue
        if not vals:
            return
        by_theme = {}
        for it in scored:
            th = str(it.get("theme") or "?").upper()[:24]
            try:
                s = float(it.get("score", 0) or 0)
            except Exception:
                continue
            b = by_theme.setdefault(th, [0, 0.0])
            b[0] += 1
            b[1] += s
        entry = {
            "block": int(runtime.get_block_height()),
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
            "kept": len(kept or []),
            "themes": {
                k: round(v[1] / v[0], 2) for k, v in sorted(by_theme.items())[:8]
            },
        }
        tape = list(self.storage.get("sentiment_tape", []))
        tape.append(entry)
        # Fixed cap: bounded storage and a bounded inline cost forever.
        self.storage["sentiment_tape"] = tape[-240:]

    def _finalize_ingest(self):
        self.storage["ingest_pending"] = False
        self.storage["ingest_runs"] = int(self.storage.get("ingest_runs", 0)) + 1
        runtime.emit_event(
            "research.ingested", {"window": len(self.storage.get("items", []))}
        )
        return _json_bytes(
            {"cycle": "done", "window": len(self.storage.get("items", []))}
        )

    @callable_by(SELF, decode="raw")
    @deferred
    def synth_tick(self, payload):
        if not self._tick_ok("synth", "synth_interval_blocks"):
            return b"skip"
        if not self.storage.get("items"):
            self.storage["synth_pending"] = False
            return b"no items"
        # See ingest_tick: a raise would roll back the reschedule and end the chain.
        try:
            return self._synthesize(self._synthesis_payload())
        except Exception as e:
            self.storage["synth_pending"] = False
            runtime.emit_event(
                "research.tick_failed", {"loop": "synth", "err": str(e)[:200]}
            )
            return b"error"

    @runner.continuation(timeout_blocks=3000)
    async def _synthesize(self, payload):
        ctx = capture()
        ctx.scorecard = _parse(payload).get("scorecard", [])
        ctx.guidance = _parse(payload).get("guidance", [])
        ctx.url = self.storage.get("cfg", {})["price_url"]
        # Never carry a full allMids response across the next await.  Canyon
        # serializes ``ctx`` between continuation legs; retaining the raw core
        # response made save_cont exceed the fixed 5M cell budget before the
        # synthesis could reach the LLM.  Resolve the bounded configured
        # universe immediately and discard each raw response before yielding.
        ctx.raw_prices = await runner.http(
            ctx.url, method="POST", headers=_HDRS, body=mids_query(""), timeout_ms=5000
        )
        ctx.prices = resolve_prices(
            universe_symbols(self.storage.get("cfg", {})), [("", ctx.raw_prices)]
        )
        ctx.raw_prices = None
        ctx.raw_prices = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("xyz"),
            timeout_ms=5000,
        )
        ctx.prices.update(
            resolve_prices(
                universe_symbols(self.storage.get("cfg", {})), [("xyz", ctx.raw_prices)]
            )
        )
        ctx.raw_prices = None
        ctx.raw_prices = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("vntl"),
            timeout_ms=5000,
        )
        ctx.prices.update(
            resolve_prices(
                universe_symbols(self.storage.get("cfg", {})),
                [("vntl", ctx.raw_prices)],
            )
        )
        ctx.raw_prices = None
        cfg = self.storage.get("cfg", {})
        theses = self.storage.get("theses", [])
        archive = self.storage.get("archive", [])
        context = _context_for_synthesis(
            self.storage.get("items", []),
            universe_symbols(cfg),
            _active_theses(theses),
            self.storage.get("mandate", ""),
            _thesis_outcomes(theses),
            ctx.scorecard,
            _wiki_aggregate(archive + theses),
            _wiki_cases(archive),
            self.storage.get("lessons", []),
            ctx.guidance,
            self.storage.get("unfillable", []),
            _sentiment_trend(self.storage.get("sentiment_tape", [])),
        )

        kwargs = {
            "system_prompt": SYNTH_SYSTEM_PROMPT,
            "max_tokens": SYNTH_MAX_TOKENS,
        }
        spec = _verify_spec(cfg)
        if spec is not None:
            kwargs["verify"] = spec
        ctx.result = await runner.llm(synth_prompt(context), **kwargs)
        return self._on_synthesis_prices(ctx.prices, ctx.result)

    def _on_synthesis(self, p_coins, p_stocks, p_preipo, result):
        prices = resolve_all(self.storage["cfg"], p_coins, p_stocks, p_preipo)
        return self._on_synthesis_prices(prices, result)

    def _on_synthesis_prices(self, prices, result):
        self.storage["synth_pending"] = False
        cfg = self.storage["cfg"]
        height = int(runtime.get_block_height())
        if prices:
            self.storage["last_prices"] = prices
        thesis, ideas = _parse_synthesis(result, universe_symbols(cfg))
        if thesis and thesis.get("statement"):
            theses = list(self.storage.get("theses", []))
            # Synth is asked for a thesis "distinct from existing" every cycle,
            # but nothing enforced it: on 2026-08-11 all TWELVE active theses were
            # restatements of one view (long BTC/ETH/MSFT/AMZN/META, short the
            # micro-caps) at convictions 52-78. Twelve slots carrying one thesis
            # of information — it crowds the context, inflates the apparent
            # agreement of the book, and wastes the `max_theses` budget that
            # exists to hold COMPETING views.
            dup = _duplicate_of(thesis, theses)
            if dup is not None:
                # Refresh conviction on the incumbent rather than storing a near
                # copy, so a repeated view strengthens one entry instead of
                # occupying another slot.
                dup["conviction"] = max(
                    int(dup.get("conviction", 0) or 0),
                    int(thesis.get("conviction", 0) or 0),
                )
                dup["restated_block"] = height
                dup["restatements"] = int(dup.get("restatements", 0) or 0) + 1
                self.storage["theses"] = theses
                runtime.emit_event(
                    "research.thesis_duplicate",
                    {
                        "id": dup.get("id"),
                        "restatements": dup.get("restatements"),
                        "conviction": dup.get("conviction"),
                    },
                )
            else:
                tid = int(self.storage.get("next_thesis_id", 1))
                self.storage["next_thesis_id"] = tid + 1
                thesis["id"] = tid
                thesis["status"] = "active"
                thesis["created_block"] = height
                thesis["baseline"] = prices
                theses.insert(0, thesis)
                self.storage["theses"] = theses[: int(cfg["max_theses"])]
        current_theses = self.storage.get("theses", [])
        current = _newest_active(current_theses) or {}
        for idea in ideas:
            idea["thesis_fit"] = _thesis_fit(idea, current)
            idea["block"] = height

        parsed_ok = bool(ideas)
        if parsed_ok:
            self.storage["ideas"] = ideas[: int(cfg["max_ideas"])]
        else:
            runtime.emit_event(
                "research.synth_empty",
                {"kept_ideas": len(self.storage.get("ideas", []))},
            )
        runs = int(self.storage.get("synth_runs", 0)) + 1
        self.storage["synth_runs"] = runs
        report = {
            "run": runs,
            "generated_thesis": current.get("statement", "") if current else "",
            "active_theses": len(_active_theses(current_theses)),
            "ideas": len(ideas),
            "published": parsed_ok,
            "priced": len(prices),
        }
        runtime.emit_event("research.synthesized", report)
        self._push_synthesis_snapshot()
        return _json_bytes(report)

    @callable_by(SELF, decode="raw")
    @deferred
    def validate_tick(self, payload):
        if not self._tick_ok("validate", "validate_interval_blocks"):
            return b"skip"
        # A raise here rolls back the `_schedule` `_tick_ok` just did, and each
        # loop only arms its own successor — so the chain would end permanently.
        # Swallow, clear the guard, emit; one bad tick costs one cycle, not the loop.
        try:
            return self._validate(b"{}")
        except Exception as e:
            self.storage["validate_pending"] = False
            runtime.emit_event(
                "research.tick_failed", {"loop": "validate", "err": str(e)[:200]}
            )
            return b"error"

    @runner.continuation(timeout_blocks=2000)
    async def _validate(self, payload):
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
        ctx.p_preipo = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=mids_query("vntl"),
            timeout_ms=5000,
        )
        return self._on_validate(ctx.p_coins, ctx.p_stocks, ctx.p_preipo)

    def _on_validate(self, p_coins, p_stocks, p_preipo):
        self.storage["validate_pending"] = False
        cfg = self.storage["cfg"]
        height = int(runtime.get_block_height())
        prices = resolve_all(cfg, p_coins, p_stocks, p_preipo)
        if not prices:
            runtime.emit_event("research.validate_error", {})
            return b"no prices"
        self.storage["last_prices"] = prices
        theses = list(self.storage.get("theses", []))
        archive = list(self.storage.get("archive", []))
        retired = 0
        for t in theses:
            if t.get("status") != "active":
                continue
            if height - int(t.get("created_block", height)) < int(
                cfg["min_age_blocks"]
            ):
                continue
            ret = _thesis_return(t, t.get("baseline", {}), prices)
            t["last_return"] = ret
            # Recorded alongside the raw return, not instead of it: retirement
            # still uses raw (a thesis that loses money is retired even if it
            # beat the market), but the LEARNING signal gets the excess return.
            t["last_alpha"] = _thesis_alpha(t, t.get("baseline", {}), prices)
            t["market_return"] = _market_return(t.get("baseline", {}), prices)
            if ret < float(cfg["min_return_pct"]):
                t["status"] = "retired"
                t["retired_block"] = height
                t["final_return"] = ret
                t["final_alpha"] = t["last_alpha"]
                retired += 1
                archive.insert(
                    0,
                    {
                        "statement": t.get("statement", ""),
                        "context": t.get("context", ""),
                        "long": t.get("long", []),
                        "short": t.get("short", []),
                        "conviction": t.get("conviction", 0),
                        "final_return": ret,
                        "final_alpha": t["last_alpha"],
                        "market_return": t.get("market_return", 0.0),
                        "created_block": t.get("created_block"),
                        "retired_block": height,
                    },
                )
        self.storage["theses"] = theses
        self.storage["archive"] = archive[: int(cfg.get("max_archive", 100))]
        self.storage["validate_runs"] = int(self.storage.get("validate_runs", 0)) + 1
        report = {
            "run": self.storage["validate_runs"],
            "active": len(_active_theses(theses)),
            "retired_now": retired,
        }
        runtime.emit_event("research.validated", report)
        return _json_bytes(report)

    @callable_by(SELF, decode="raw")
    @deferred
    def brief_tick(self, payload):
        if not self.storage.get("running"):
            return b"stopped"
        cfg = self.storage.get("cfg", {})
        self._schedule(
            "brief_tick",
            int(
                cfg.get(
                    "brief_check_interval_blocks", DEFAULT_BRIEF_CHECK_INTERVAL_BLOCKS
                )
            ),
        )
        ts_ms = int(runtime.get_timestamp_ms())
        # Missing brief_auto_dst identifies an older stored config; preserve
        # its historical fixed-offset behavior during code upgrades.
        offset = (
            eastern_utc_offset_hours(ts_ms)
            if cfg.get("brief_auto_dst", False)
            else cfg.get("brief_utc_offset_hours", DEFAULT_BRIEF_UTC_OFFSET_HOURS)
        )
        key, mode = brief_slot(
            ts_ms,
            offset,
            cfg.get("brief_morning_hour", DEFAULT_BRIEF_MORNING_HOUR),
            cfg.get("brief_evening_hour", DEFAULT_BRIEF_EVENING_HOUR),
        )
        if not key or self.storage.get("last_brief_key") == key:
            return b"not due"
        self.storage["last_brief_key"] = key
        return self._brief(mode)

    @callable_by(OWNER, decode="raw")
    @deferred
    def brief_now(self, payload):
        mode = str(_parse(payload).get("mode", "morning"))
        return self._brief(mode)

    def _brief(self, mode="morning"):
        cfg = self.storage.get("cfg", {})
        pnl = dict(self.storage.get("last_pnl", {}))
        text = compose_brief(
            None,
            self.storage.get("theses", []),
            self.storage.get("ideas", []),
            pnl,
            universe_symbols(cfg),
            mode=mode,
        )

        ok = self._enqueue_brief(text, kind=("brief" if mode == "morning" else "recap"))
        runtime.emit_event(
            "research.brief", {"sent": bool(ok), "mode": mode, "chars": len(text)}
        )
        return _json_bytes({"sent": bool(ok)})

    @callable_by(SELF, decode="raw")
    @deferred
    def discover_tick(self, payload):
        if not self._tick_ok("discover", "discover_interval_blocks"):
            return b"skip"
        # A raise here rolls back the `_schedule` `_tick_ok` just did, and each
        # loop only arms its own successor — so the chain would end permanently.
        # Swallow, clear the guard, emit; one bad tick costs one cycle, not the loop.
        try:
            return self._discover(b"{}")
        except Exception as e:
            self.storage["discover_pending"] = False
            runtime.emit_event(
                "research.tick_failed", {"loop": "discover", "err": str(e)[:200]}
            )
            return b"error"

    @runner.continuation(timeout_blocks=1200)
    async def _discover(self, payload):
        ctx = capture()
        ctx.cfg = self.storage.get("cfg", {})
        ctx.url = ctx.cfg.get("discover_url", DEFAULT_DISCOVER_URL)
        ctx.xyz = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=meta_ctxs_query("xyz"),
            timeout_ms=8000,
        )

        ctx.stocks = self._screen_group(ctx.xyz, "stocks_pinned", "stocks")
        ctx.xyz = None
        ctx.core = await runner.http(
            ctx.url,
            method="POST",
            headers=_HDRS,
            body=meta_ctxs_query(""),
            timeout_ms=8000,
        )
        return self._on_discover(ctx.stocks, ctx.core)

    def _screen_group(self, resp, pinned_key, cfg_key):
        cfg = self.storage.get("cfg", {})
        pinned = list(self.storage.get(pinned_key) or cfg.get(cfg_key) or [])
        held = [
            str(p.get("asset", "")).upper()
            for p in (self.storage.get("last_scorecard") or [])
            if p.get("status") == "open"
        ]
        merged, added, kept = discover_stocks(
            resp,
            pinned,
            held,
            cfg.get("min_discover_volume", 20.0),
            cfg.get("max_discovered", 24),
        )
        self.storage[pinned_key] = pinned
        return {"merged": merged, "added": added, "kept": kept}

    def _on_discover(self, stocks, core_resp):
        self.storage["discover_pending"] = False
        cfg = dict(self.storage.get("cfg", {}))
        coins = self._screen_group(core_resp, "coins_pinned", "coins")
        cfg["stocks"] = stocks["merged"]
        cfg["coins"] = coins["merged"]
        self.storage["cfg"] = cfg
        runtime.emit_event(
            "research.universe_discovered",
            {
                "stocks": len(cfg["stocks"]),
                "coins": len(cfg["coins"]),
                "added": stocks["added"] + coins["added"],
                "held_kept": stocks["kept"] + coins["kept"],
            },
        )
        return _json_bytes({"stocks": cfg["stocks"], "coins": cfg["coins"]})

    @callable_by(SELF, decode="raw")
    @deferred
    def distill_tick(self, payload):
        if not self._tick_ok("distill", "distill_interval_blocks"):
            return b"skip"
        if not self.storage.get("archive"):
            self.storage["distill_pending"] = False
            return b"no archive"
        try:
            return self._distill(b"{}")
        except Exception as e:
            self.storage["distill_pending"] = False
            runtime.emit_event(
                "research.tick_failed", {"loop": "distill", "err": str(e)[:200]}
            )
            return b"error"

    @runner.continuation(timeout_blocks=1200)
    async def _distill(self, payload):

        ctx = capture()
        ctx.cfg = self.storage.get("cfg", {})
        prompt = distill_prompt(
            self.storage.get("archive", []),
            self.storage.get("lessons", []),
            ctx.cfg.get("max_lessons", 8),
        )

        ctx.result = await runner.llm(prompt, max_tokens=1500)
        return self._on_distill(ctx.result)

    def _on_distill(self, result):
        cfg = self.storage["cfg"]
        self.storage["distill_pending"] = False
        lessons = parse_lessons(result, int(cfg.get("max_lessons", 8)))
        if lessons:
            self.storage["lessons"] = lessons
        self.storage["archive"] = list(self.storage.get("archive", []))[
            : int(cfg.get("archive_keep", 20))
        ]
        self.storage["distill_runs"] = int(self.storage.get("distill_runs", 0)) + 1
        report = {
            "run": self.storage["distill_runs"],
            "lessons": len(self.storage.get("lessons", [])),
        }
        runtime.emit_event("research.distilled", report)
        return _json_bytes(report)

    def _mark_pending(self, loop):
        mark_pending(self.storage, runtime, loop)

    def _tick_ok(self, loop, interval_key):
        if not self.storage.get("running"):
            return False
        cfg = self.storage["cfg"]
        self._schedule(loop + "_tick", int(cfg[interval_key]))
        allowed, _height, _recovered = tick_pending_guard(
            self.storage,
            runtime,
            loop,
            "research",
            int(cfg.get("max_poll_wait_blocks", 300)),
            int(cfg.get("max_poll_wait_ms", 1_800_000)),
        )
        return allowed

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
    def get_theses(self, payload):
        obj = _parse(payload)
        theses = list(self.storage.get("theses", []))
        if obj.get("active_only"):
            theses = _active_theses(theses)
        return _json_bytes(
            {
                "theses": theses,
                "active": len(_active_theses(self.storage.get("theses", []))),
            }
        )

    @public(decode="raw")
    @pure
    def get_wiki(self, payload):
        return _json_bytes(wiki_view(self.storage))

    @public(decode="raw")
    @pure
    def get_ideas(self, payload):
        limit = int(_parse(payload).get("limit", 10))
        ideas = list(self.storage.get("ideas", []))
        return _json_bytes({"ideas": ideas[:limit], "count": len(ideas)})

    @public(decode="raw")
    @pure
    def get_items(self, payload):
        limit = int(_parse(payload).get("limit", 10))
        items = list(self.storage.get("items", []))
        return _json_bytes({"items": items[:limit], "count": len(items)})

    @public(decode="raw")
    @pure
    def get_universe(self, payload):
        return _json_bytes(universe_view(self.storage))

    @public(decode="raw")
    @pure
    def get_stats(self, payload):
        return _json_bytes(stats_view(self.storage))


def _get_actor():
    return HyperliquidResearch()


def init(payload):
    return _get_actor().init(payload)


def set_feeds(payload):
    return _get_actor().set_feeds(payload)


def set_universe(payload):
    return _get_actor().set_universe(payload)


def set_mandate(payload):
    return _get_actor().set_mandate(payload)


def set_exec(payload):
    return _get_actor().set_exec(payload)


def start(payload):
    return _get_actor().start(payload)


def stop(payload):
    return _get_actor().stop(payload)


def ingest_now(payload):
    return _get_actor().ingest_now(payload)


def synthesize_now(payload):
    return _get_actor().synthesize_now(payload)


def validate_now(payload):
    return _get_actor().validate_now(payload)


def distill_now(payload):
    return _get_actor().distill_now(payload)


def ingest_tick(payload):
    return _get_actor().ingest_tick(payload)


def synth_tick(payload):
    return _get_actor().synth_tick(payload)


def validate_tick(payload):
    return _get_actor().validate_tick(payload)


def distill_tick(payload):
    return _get_actor().distill_tick(payload)


def discover_tick(payload):
    return _get_actor().discover_tick(payload)


def discover_now(payload):
    return _get_actor().discover_now(payload)


def set_notify(payload):
    return _get_actor().set_notify(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def brief_tick(payload):
    return _get_actor().brief_tick(payload)


def brief_now(payload):
    return _get_actor().brief_now(payload)


def _discover__resume(payload):
    return _get_actor()._discover__resume(payload)


def _ingest_feed__resume(payload):
    return _get_actor()._ingest_feed__resume(payload)


def _score_candidates__resume(payload):
    return _get_actor()._score_candidates__resume(payload)


def _synthesize__resume(payload):
    return _get_actor()._synthesize__resume(payload)


def _validate__resume(payload):
    return _get_actor()._validate__resume(payload)


def _distill__resume(payload):
    return _get_actor()._distill__resume(payload)


def get_theses(payload):
    return _get_actor().get_theses(payload)


def get_wiki(payload):
    return _get_actor().get_wiki(payload)


def get_ideas(payload):
    return _get_actor().get_ideas(payload)


def get_items(payload):
    return _get_actor().get_items(payload)


def get_universe(payload):
    return _get_actor().get_universe(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
