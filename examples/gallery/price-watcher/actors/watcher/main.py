"""Price-drop watcher with a two-way Telegram bridge.

JSON APIs via dotted paths, small HTML via JSON-LD/microdata/OpenGraph
auto-detect, regex overrides; every price is compared to its high-water mark,
with drop alerts, chat commands, and a daily summary over Telegram (the bot
token stays a CBSS secret). Node v0.0.34 constraints are load-bearing here —
timers can't dispatch jobs, failed jobs get no callback, results >64KiB never
deliver, the runner sends no User-Agent; see tests/ and README Limitations.
"""

import base64
import json
import re

from cowboy_sdk import actor, capture, deferred, public, pure, runner, runtime


DEFAULT_INTERVAL_BLOCKS = 86_400   # ~1 day at ~1s/block
DEFAULT_TIMEOUT_MS = 8_000
DEFAULT_MIN_DROP_PCT = 0.0
MAX_WATCHES = 64
MAX_WATCHES_PER_OWNER = 16
MAX_ITEMS_PER_WATCH = 32
MAX_AUTO_ITEMS = 32
MAX_LD_BLOCKS = 16
MAX_JSON_DEPTH = 8
MAX_DROPS_KEPT = 200
MAX_NAME_LEN = 120
MAX_POLL_WAIT_BLOCKS = 300
MAX_NOTIFY_QUEUE = 100
MAX_PATTERN_LEN = 200
MAX_PATH_LEN = 200
MAX_SCAN_TEXT = 65_536            # cap the text fed to regex (bounds backtracking)
QUIET_SCAN_GAP_BLOCKS = 750        # ~12.5 min: lets every 15-min hop scan

# The token is a CBSS secret; the runner substitutes ${TELEGRAM_BOT_TOKEN}
# into the URL at request time.
TELEGRAM_SEND_URL = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_POLL_URL = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
TELEGRAM_SECRET = "TELEGRAM_BOT_TOKEN"

TG_LONG_POLL_S = 25                # getUpdates hangs server-side up to this long
TG_HTTP_TIMEOUT_MS = 30_000        # > long poll; runner's own client cap is 60s
MAX_TG_COMMANDS = 3
MAX_TG_REPLY_LEN = 3500

# Runner's client sends no UA; WAF-fronted sites serve UA-less requests empty.
DEFAULT_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; price-watcher/1.0)"}

WELCOME = """\U0001F44B Price Watcher — on-chain price monitoring with Telegram alerts

I watch prices anywhere on the web, remember each item's highest price since
tracking began, and alert you the moment it drops below that mark.

COMMANDS
/track <url> — watch a page (price auto-detected)
/track <url> <json.path> [name] — watch a field of a JSON API
/untrack <n> — stop watching (numbers shown in /watches)
/watches — your watch list
/prices — current prices (from the latest scan)
/drops — current price drops
/help — this message

TRY THESE — one per kind, each verified working:

\u2022 Crypto API:
  /track https://api.coinbase.com/v2/prices/ETH-USD/spot data.amount Ethereum
\u2022 Stock index:
  /track https://query1.finance.yahoo.com/v8/finance/chart/SPY chart.result.0.meta.regularMarketPrice SP500
\u2022 A real store — Lucchese, Texas bootmakers since 1883 (any Shopify
  product page, pasted as-is):
  /track https://www.lucchese.com/products/straight-shooter-tobacco Lucchese Boots
\u2022 Plain HTML page (webscraper.io's live demo storefront) — the price
  is auto-detected from the page markup, no path needed:
  /track https://webscraper.io/test-sites/e-commerce/allinone/product/123
\u2022 Weather as a price:
  /track https://api.open-meteo.com/v1/forecast?latitude=40.71&longitude=-74.01&current=temperature_2m current.temperature_2m NYC Temp

WHAT WORKS
\u2022 Any JSON API — crypto, stocks, FX, game prices, weather, indexes
\u2022 Shopify stores \u2014 paste any product page; the bot reads its .json mirror
\u2022 Small HTML pages (under ~45KB): prices auto-detected from JSON-LD
  (multi-product pages too, each item named for itself), microdata, or
  OpenGraph markup. (Real merchants' pages usually exceed this — that's
  what the Shopify .json trick is for.)

NOT SUPPORTED (platform limits)
\u2022 Large pages (most big storefronts) — use the site's JSON API instead
\u2022 JavaScript-rendered prices — no browser on-chain
\u2022 Sites that block automated fetches

Prices are checked on the schedule the owner sets — a drop alert fires the moment
a price falls below its all-time-tracked high by the configured threshold, and the
owner can enable a periodic full summary of every watch, drops or not."""


# ---- generic helpers -------------------------------------------------------- #

def _parse(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8") if payload else "{}"
        value = json.loads(payload)
        if isinstance(value, list):
            # a routed timer fire: the node re-serializes the envelope's
            # `_payload` as a JSON list of byte values
            value = json.loads(bytes(value))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _json_bytes(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _sender_hex():
    try:
        s = runtime.get_sender()
    except Exception:
        return ""
    if isinstance(s, (bytes, bytearray)):
        return s.hex()
    return str(s or "")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in (
            "false", "0", "no", "n", "off", "stop", "disabled", "none", "f", "")
    return bool(value)


def _as_int(value, default):
    # a bad number must not revert the tx out of a @public handler
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _b64_text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    s = str(value or "")
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return s


def _is_raw_page(page):
    return isinstance(page, dict) and "raw" in page and "content_type" in page


def _as_json(page):
    page = getattr(page, "raw", page)  # unwrap an awaited RunnerResult
    if _is_raw_page(page):
        try:
            return json.loads(_b64_text(page["raw"]))
        except Exception:
            return None
    return page


def _as_text(page):
    page = getattr(page, "raw", page)  # unwrap an awaited RunnerResult
    if _is_raw_page(page):
        return _b64_text(page["raw"])
    if isinstance(page, (dict, list)):
        return json.dumps(page)
    if isinstance(page, bytes):
        return page.decode("utf-8", "replace")
    return str(page or "")


def _no_template(s):
    return str(s or "").replace("${", "$ {")


def _clean_chat_id(value):
    # ASCII-numeric only ([0-9], not Unicode \d) — no ${...} into the send body
    s = str(value or "").strip()
    return s if re.match(r"^-?[0-9]{1,20}$", s) else ""


def _clean_name(value, fallback):
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return _no_template(s or str(fallback))[:MAX_NAME_LEN]


def _price_line(wname, iname, price):
    if iname == wname:
        return "%s: %s" % (iname, price)
    return "%s — %s: %s" % (wname, iname, price)


def _drop_message(record):
    site, item = record.get("site", ""), record.get("item", "")
    where = item if item == site else "%s — %s" % (site, item)
    # %.2f: RustPython float repr is not shortest-form (0.17 prints long)
    return "\U0001F4C9 %s: %s → %s (-%.2f%%)" % (
        where, record.get("reference_price"),
        record.get("new_price"), float(record.get("drop_pct") or 0))


def _split_path(path):
    out = []
    for part in str(path).split("."):
        for tok in part.replace("]", "").split("["):
            if tok:
                out.append(int(tok) if tok.isdigit() and len(tok) <= 15 else tok)
    return out


def _get_path(obj, path):
    segs = _split_path(path)
    if not segs:
        return None
    cur = obj
    for seg in segs:
        if isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
            cur = cur[seg]
        elif isinstance(cur, dict) and str(seg) in cur:
            cur = cur[str(seg)]
        else:
            return None
    return cur


WAS_NOW_RATIO = 2.5                # a was/now pair within this reads the lower (~60% off)
# Pattern strings, compiled at call time: the project compiler only allows
# static data in top-level assignments (re caches compiled patterns anyway).
_CLEAN_PRICE_RE = r"^-?\d[\d,]*(?:\.\d+)?$"
_CURRENCY_PRICE_RE = r"[$€£¥₹]\s*(-?\d[\d,]*(?:\.\d+)?)"
_CURRENCY_CODE_RE = r"(?i)\b(?:usd|eur|gbp|cad|aud|nzd|jpy|chf|cny|inr|mxn)\b"


def _to_price(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip()
    if not s:
        return None
    # currency-tagged numbers: one clean price, or a tight was/now pair (the LOWER
    # is the sale). Anything else — 3+ numbers, or two too far apart to be a sale —
    # is REFUSED (None), never guessed. A decoy (trade-in, gift card, add-on,
    # instalment) read as the price fires false crashes and ratchets the
    # high-water mark upward permanently; a missed read is recoverable, a wrong one
    # is not. Real product prices come from structured markup (a single value).
    try:
        vals = [float(t.replace(",", "")) for t in re.findall(_CURRENCY_PRICE_RE, s)]
    except Exception:
        vals = []
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 2:
        lo, hi = min(vals), max(vals)
        return lo if lo > 0 and hi / lo < WAS_NOW_RATIO else None
    if vals:                              # 3+ currency numbers: too ambiguous
        return None
    core = re.sub(_CURRENCY_CODE_RE, "", s).strip().lstrip("$€£¥₹").strip()
    if not re.match(_CLEAN_PRICE_RE, core):
        return None
    try:
        return float(core.replace(",", ""))
    except Exception:
        return None


def _discover_name(doc, path):
    segs = _split_path(path)
    for depth in range(len(segs), -1, -1):
        node = _get_path(doc, ".".join(str(s) for s in segs[:depth])) if depth else doc
        if isinstance(node, dict):
            for k in ("name", "title", "product"):
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    return _clean_name(v, None)
    return None


def _extract_override(item, page):
    if item.get("kind") == "html":
        try:
            m = re.search(str(item.get("pattern", "")), _as_text(page)[:MAX_SCAN_TEXT])
        except Exception:
            return None
        if not m:
            return None
        return _to_price(m.group(1) if m.groups() else m.group(0))
    try:  # a pathological path must not revert the tx
        doc = _as_json(page)
        return _to_price(_get_path(doc, item.get("path", ""))) if doc is not None else None
    except Exception:
        return None


# ---- auto-detection --------------------------------------------------------- #

def _ld_offer_price(offer):
    if isinstance(offer, list):
        for o in offer:
            p = _ld_offer_price(o)
            if p is not None:
                return p
    elif isinstance(offer, dict):
        spec = offer.get("priceSpecification")
        for v in (offer.get("price"), offer.get("lowPrice"),  # lowPrice: AggregateOffer
                  spec.get("price") if isinstance(spec, dict) else None):
            p = _to_price(v)
            if p is not None:
                return p
    return None


def _walk_ld(node, out, depth=0):
    if depth > MAX_JSON_DEPTH or len(out) >= MAX_AUTO_ITEMS:
        return
    if isinstance(node, list):
        for it in node:
            _walk_ld(it, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    price = None
    if "offers" in node:
        price = _ld_offer_price(node.get("offers"))
    if price is None and "price" in node:
        price = _to_price(node.get("price"))
    if price is not None and price >= 0:
        out.append({"name": _clean_name(node.get("name"), "price"), "price": price})
    for k, v in node.items():
        if k != "offers" and isinstance(v, (dict, list)):
            _walk_ld(v, out, depth + 1)


def _append_prices(out, raws, name):
    for raw in raws:
        p = _to_price(raw)
        if p is not None and p >= 0:
            out.append({"name": name, "price": p})


def _auto_detect_html(text):
    text = text[:MAX_SCAN_TEXT]
    out = []
    m = re.search(r"(?is)<title[^>]{0,400}>(.*?)</title>", text)
    name = _clean_name(m.group(1), "price") if m else "price"
    # 1. JSON-LD
    if "ld+json" in text:
        for block in re.findall(
            r"(?is)<script[^>]{0,400}type=[\"']application/ld\+json[\"'][^>]{0,400}>(.*?)</script>",
            text,
        )[:MAX_LD_BLOCKS]:
            try:
                _walk_ld(json.loads(block.strip()), out)
            except Exception:
                continue
    # 2. Microdata: content= on any tag (either attr order), else element text
    if not out and "itemprop" in text:
        _append_prices(out, re.findall(
            r"(?is)<[^>]{0,400}itemprop=[\"']price[\"'][^>]{0,400}content=[\"']([^\"']+)[\"']", text
        ) + re.findall(
            r"(?is)<[^>]{0,400}content=[\"']([^\"']+)[\"'][^>]{0,400}itemprop=[\"']price[\"']", text
        ) + re.findall(
            r"(?is)<[^>]{0,400}itemprop=[\"']price[\"'][^>]{0,400}>([^<]{1,40})<", text
        ), name)
    # 3. OpenGraph (og: and the product: twin), either attribute order
    if not out and "price:amount" in text:
        _append_prices(out, re.findall(
            r"(?is)<meta[^>]{0,400}property=[\"'](?:og|product):price:amount[\"'][^>]{0,400}content=[\"']([^\"']+)[\"']", text
        ) + re.findall(
            r"(?is)<meta[^>]{0,400}content=[\"']([^\"']+)[\"'][^>]{0,400}property=[\"'](?:og|product):price:amount[\"']", text
        ), name)
    return out[:MAX_AUTO_ITEMS]


def _walk_json_prices(node, name_ctx, out, depth):
    if depth > MAX_JSON_DEPTH or len(out) >= MAX_AUTO_ITEMS:
        return
    if isinstance(node, list):
        for it in node:
            _walk_json_prices(it, name_ctx, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    ctx = node.get("name") or node.get("title") or node.get("product") or name_ctx
    for k, v in node.items():
        if "price" in str(k).lower() and not isinstance(v, (dict, list)):
            p = _to_price(v)
            if p is not None and p >= 0:
                out.append({"name": _clean_name(ctx, k), "price": p})
        elif isinstance(v, (dict, list)):
            _walk_json_prices(v, ctx, out, depth + 1)


def _auto_detect(page):
    if _is_raw_page(page):
        return _auto_detect_html(_b64_text(page["raw"]))
    if isinstance(page, (dict, list)):
        out = []
        _walk_json_prices(page, None, out, 0)
        return out
    return _auto_detect_html(_as_text(page))


def _clean_items(raw_items, allow_regex=False):
    items, dropped = [], 0
    for it in (raw_items or [])[:MAX_ITEMS_PER_WATCH]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        kind = "html" if it.get("kind") == "html" or "pattern" in it else "json"
        entry = {"name": _no_template(name)[:MAX_NAME_LEN], "kind": kind,
                 "auto_name": bool(it.get("auto_name"))}
        if kind == "html":
            pattern = str(it.get("pattern", ""))
            if not allow_regex or not pattern or len(pattern) > MAX_PATTERN_LEN:
                dropped += 1
                continue
            entry["pattern"] = pattern
        else:
            path = str(it.get("path", ""))
            if not path or len(path) > MAX_PATH_LEN:
                dropped += 1
                continue
            entry["path"] = path
        items.append(entry)
    return items, dropped


# ---- telegram updates ------------------------------------------------------- #

def _tg_cmd(text):
    s = str(text).strip()
    return s.split()[0].split("@")[0].lower() if s else ""


def _norm_track_url(url):
    # Shopify product pages ship a .json data mirror — rewrite so pasting
    # the normal product page just works
    if "/products/" in url and ".json" not in url:
        return url.split("?")[0].split("#")[0].rstrip("/") + ".json"
    return url


def _tg_updates(doc, offset):
    texts = []
    offset = int(offset or 0)
    for u in (doc or {}).get("result") or []:
        if not isinstance(u, dict):
            continue
        try:
            uid = int(u.get("update_id", 0))
        except Exception:
            continue
        if uid >= offset:
            offset = uid + 1
        msg = u.get("message") or u.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text", "") or "").strip()
        if chat and text:
            texts.append((chat, text))
    return texts, offset


# ---- actor ------------------------------------------------------------------ #

@actor
class PriceWatcher:
    """Price-drop watcher with Telegram alerts, commands, and daily summary."""

    @public(decode="raw")
    def init(self, payload):
        if self.storage.get("initialized"):
            return _json_bytes({"status": "already_initialized"})
        obj = _parse(payload)
        for key, val in (("__name__", "Price Watcher"), ("__version__", "1.0.0"),
                         ("__description__", "Website price-drop watcher"),
                         ("initialized", True), ("watches", {}), ("last", {}),
                         ("ref", {}), ("drops", []), ("notify_queue", []),
                         ("next_wid", 1), ("scan_pending", False)):
            self.storage[key] = val
        self.storage["owner"] = _sender_hex()  # deployer = admin
        self.storage["cfg"] = {
            "interval_blocks": max(30, _as_int(obj.get("interval_blocks"), DEFAULT_INTERVAL_BLOCKS)),
            "timeout_ms": _as_int(obj.get("timeout_ms"), DEFAULT_TIMEOUT_MS),
            "min_drop_pct": _as_float(obj.get("min_drop_pct"), DEFAULT_MIN_DROP_PCT),
        }
        self.storage["running"] = bool(obj.get("autostart", False))  # opt-in (default off)
        if self.storage["running"]:
            self.storage["tick_gen"] = 1
            self._schedule("daily_tick", self.storage["cfg"]["interval_blocks"], 1)
        return _json_bytes({"status": "initialized", "cfg": self.storage["cfg"]})

    # ---- registration ------------------------------------------------------ #

    def _add_watch(self, url, name, owner, chat_id, items, method="GET",
                   headers=None, max_items=MAX_AUTO_ITEMS):
        watches = dict(self.storage.get("watches", {}))
        if len(watches) >= MAX_WATCHES:
            return None, "watch limit reached"
        if sum(1 for w in watches.values()
               if w.get("owner") == owner) >= MAX_WATCHES_PER_OWNER:
            return None, "per-owner watch limit reached"
        wid = str(int(self.storage.get("next_wid", 1)))
        self.storage["next_wid"] = int(wid) + 1
        watches[wid] = {"url": url, "name": name, "owner": owner,
                        "chat_id": chat_id, "method": method,
                        "headers": headers or {},
                        "mode": "manual" if items else "auto", "items": items,
                        "max_items": max_items,
                        "last_scan_block": 0, "last_ok": False}
        self.storage["watches"] = watches
        return wid, None

    @public(decode="raw")
    def track(self, payload):
        """{url, name?, chat_id?, headers?, items?: [{name, path}|{name, pattern}],
        max_items?, baseline?} — no items = auto-detect."""
        obj = _parse(payload)
        url = str(obj.get("url", "")).strip()
        if not url:
            return _json_bytes({"error": "url required"})
        items, dropped = _clean_items(
            obj.get("items"),
            allow_regex=_sender_hex() == self.storage.get("owner"))
        mi = obj.get("max_items")
        wid, err = self._add_watch(
            url, _no_template(str(obj.get("name", url)))[:MAX_NAME_LEN], _sender_hex(),
            _clean_chat_id(obj.get("chat_id")), items,
            method=str(obj.get("method", "GET")).upper(),
            headers=obj.get("headers") if isinstance(obj.get("headers"), dict) else {},
            max_items=MAX_AUTO_ITEMS if mi is None
            else max(1, min(_as_int(mi, MAX_AUTO_ITEMS), MAX_AUTO_ITEMS)))
        if err:
            return _json_bytes({"error": err})
        mode = "manual" if items else "auto"
        runtime.emit_event("price_watcher.tracked",
                           {"watch_id": wid, "url": url, "mode": mode})
        res = {"status": "tracked", "watch_id": wid, "mode": mode}
        if dropped:  # override rejected, not silently downgraded to auto
            res["dropped_items"] = dropped
        if obj.get("baseline"):
            # queue only this watch — never the scan lock/clock nor scan_due/
            # scan_summary; a public baseline reads its own watch, like run_now
            q = list(self.storage.get("baseline_due", []))
            q.append(wid)
            self.storage["baseline_due"] = q[-MAX_WATCHES:]
            res["baseline"] = "queued"
        return _json_bytes(res)

    @public(decode="raw")
    def untrack(self, payload):
        wid = str(_parse(payload).get("watch_id", ""))
        watches = self.storage.get("watches", {})
        if wid not in watches:
            return _json_bytes({"error": "unknown watch_id"})
        sender = _sender_hex()
        if watches[wid].get("owner") != sender and sender != self.storage.get("owner"):
            return _json_bytes({"error": "not your watch"})
        self._remove_watch(wid)
        return _json_bytes({"status": "untracked", "watch_id": wid})

    def _remove_watch(self, wid):
        watches = dict(self.storage.get("watches", {}))
        watches.pop(wid, None)
        self.storage["watches"] = watches
        for mapname in ("last", "ref", "strikes"):
            m = dict(self.storage.get(mapname, {}))
            m.pop(wid, None)
            self.storage[mapname] = m
        self.storage["drops"] = [d for d in self.storage.get("drops", [])
                                 if str(d.get("watch_id")) != wid]

    def _queue_notify(self, chat, text):
        notify = list(self.storage.get("notify_queue", []))
        notify.append({"chat_id": str(chat), "text": str(text)[:MAX_TG_REPLY_LEN]})
        self.storage["notify_queue"] = notify[-MAX_NOTIFY_QUEUE:]

    def _resume_poll(self, then_poll, fallback):
        # re-arm the poll chain only if this leg still owns the current gen
        if (then_poll is not None and self.storage.get("tg_running")
                and int(then_poll) == int(self.storage.get("tg_gen", 0))):
            return self._tg_poll(_json_bytes({"gen": int(then_poll)}))
        return fallback

    # ---- scanning ---------------------------------------------------------- #

    @public(decode="raw")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def run_now(self, payload):
        """Manual scan of the caller's watches (owner scans all). No summary."""
        if self.storage.get("scan_pending"):
            return _json_bytes({"status": "scan_in_progress"})
        sender = _sender_hex()
        watches = self.storage.get("watches", {})
        if sender == self.storage.get("owner"):
            queue = list(watches.keys())
        else:
            queue = [wid for wid, w in watches.items() if w.get("owner") == sender]
        if not queue:
            return _json_bytes({"status": "no_watches_for_caller"})
        # drain baseline_due only for watches this scan will actually cover — a
        # quarantined watch is filtered out by _start_scan, so leave its baseline queued
        scanned = self._scannable(queue)
        self.storage["baseline_due"] = [w for w in self.storage.get("baseline_due", [])
                                        if w not in scanned]
        return self._start_scan(queue)

    def daily_tick(self, payload):
        # node v0.0.34 timers can't dispatch jobs, so the tick only marks the
        # scan due; the next telegram poll leg dispatches it
        cfg = self.storage.get("cfg", {})
        # gen fence: a stale tick chain dies here, so set_running can't fork extras
        gen = int(_parse(payload).get("gen", 0))
        if gen != int(self.storage.get("tick_gen", 0)):
            return b"superseded"
        if not self.storage.get("running"):
            return b"stopped"
        self._schedule("daily_tick", self._next_tick_blocks(cfg), gen)
        height = int(runtime.get_block_height())
        started = int(self.storage.get("scan_started_block", 0))
        summary = True
        at = cfg.get("scan_at_min")
        if at is not None:
            # wall-clock: the on-slot wakeup summarizes, other hops scan quietly.
            # Slot window is ≤5 min late OR ≤60s early (block-time drift).
            now_s = int(runtime.get_timestamp_ms()) // 1000
            local = (now_s + int(cfg.get("tz_offset_min", 0)) * 60) % 86400
            summary = ((local - int(at) * 60) % 86400 <= 300
                       or (int(at) * 60 - local) % 86400 <= 60)
            if not summary and (height - started) < QUIET_SCAN_GAP_BLOCKS:
                return b"rescheduled"
        else:
            # interval mode: digest only every summary_every_blocks (0 = every scan)
            every = int(cfg.get("summary_every_blocks", 0))
            if every > 0:
                summary = (height - int(self.storage.get("summary_mark_block", 0))) >= every
        if self.storage.get("scan_pending") and (height - started) <= MAX_POLL_WAIT_BLOCKS:
            if summary:
                self.storage["scan_due"] = True
                self.storage["scan_summary"] = True
            runtime.emit_event("price_watcher.scan_skipped", {"height": height})
            return b"busy"
        want = bool(summary) or (bool(self.storage.get("scan_due"))
                                 and bool(self.storage.get("scan_summary")))
        self.storage["scan_due"] = True
        self.storage["scan_summary"] = want
        runtime.emit_event("price_watcher.scan_queued",
                           {"height": height, "summary": want})
        return b"scan_queued"

    def _start_scan(self, queue=None):
        watches = self.storage.get("watches", {})
        if queue is None:
            queue = list(watches.keys())
        queue = self._scannable(queue)
        if not queue:
            return _json_bytes({"status": "no_watches"})
        self.storage["scan_pending"] = True
        self.storage["scan_started_block"] = int(runtime.get_block_height())
        runtime.emit_event("price_watcher.scan_started", {"watches": len(queue)})
        return self._scan_one(_json_bytes({"queue": queue}))

    def _note_stranded_leg(self):
        inflight = self.storage.get("scan_inflight")
        if not isinstance(inflight, dict):
            return
        self.storage["scan_inflight"] = None
        wid = str(inflight.get("wid", ""))
        watches = dict(self.storage.get("watches", {}))
        if wid not in watches:
            return
        strikes = dict(self.storage.get("strikes", {}))
        strikes[wid] = int(strikes.get(wid, 0)) + 1
        self.storage["strikes"] = strikes
        threshold = 2 if int(watches[wid].get("last_scan_block", 0)) > 0 else 1
        if strikes[wid] >= threshold:
            w = dict(watches[wid])
            w["quarantined"] = True
            watches[wid] = w
            self.storage["watches"] = watches
            runtime.emit_event("price_watcher.quarantined",
                               {"watch_id": wid, "url": w.get("url", "")})
            chat = str(self.storage.get("tg_chat_id", "")) or w.get("chat_id", "")
            if chat:
                scoped = sorted(self._scoped_wids(chat), key=int)
                pos = scoped.index(wid) + 1 if wid in scoped else wid
                self._queue_notify(chat, "⚠️ '%s' keeps failing to return a usable "
                                   "price and won't be scanned. /untrack %s removes it."
                                   % (w.get("name", wid), pos))

    def _scannable(self, wids):
        watches = self.storage.get("watches", {})
        keep = [w for w in wids if w in watches and not watches[w].get("quarantined")]
        return (sorted([w for w in keep if watches[w].get("last_ok")], key=int)
                + sorted([w for w in keep if not watches[w].get("last_ok")], key=int))

    @runner.continuation
    async def _scan_one(self, payload):
        ctx = capture()
        obj = _parse(payload)
        ctx.queue = obj.get("queue", [])
        ctx.reply = obj.get("reply")
        ctx.then_poll = obj.get("then_poll")
        ctx.summary = bool(obj.get("summary"))
        ctx.wid = ctx.queue.pop(0)
        ctx.watch = self.storage.get("watches", {}).get(ctx.wid, {})
        if not ctx.watch.get("url"):
            return self._advance_scan(ctx.queue, ctx.reply, ctx.then_poll, ctx.summary)
        ctx.timeout = int(self.storage.get("cfg", {}).get("timeout_ms", DEFAULT_TIMEOUT_MS))
        ctx.headers = dict(DEFAULT_FETCH_HEADERS)
        ctx.headers.update(ctx.watch.get("headers") or {})
        self.storage["scan_inflight"] = {"wid": ctx.wid,
                                         "block": int(runtime.get_block_height())}
        ctx.page = await runner.http(
            ctx.watch.get("url", ""),
            method=ctx.watch.get("method", "GET"),
            headers=ctx.headers,
            timeout_ms=ctx.timeout,
        )
        return self._after_scan(ctx.wid, ctx.watch, ctx.queue, ctx.page,
                                ctx.reply, ctx.then_poll, ctx.summary)

    def _scan_prices(self, watch, page):
        cap = int(watch.get("max_items", MAX_AUTO_ITEMS))
        if watch.get("mode") == "manual":
            cur = {}
            for item in watch.get("items", []):
                p = _extract_override(item, page)
                if p is not None:
                    name = item["name"]
                    if item.get("auto_name"):
                        found = _discover_name(_as_json(page), item.get("path", ""))
                        if found:
                            name = found
                    cur[name] = p
        else:
            cur = {}
            for row in _auto_detect(page):
                name, price = row["name"], row["price"]
                if name not in cur or price < cur[name]:  # sale price wins
                    cur[name] = price
            if len(cur) >= 4 and len(set(cur.values())) == 1:
                # variant explosion (Shopify sizes): one product, one line
                cur = {_discover_name(_as_json(page), "product")
                       or watch.get("name", "price"): list(cur.values())[0]}
        if len(cur) > cap:
            cur = {k: cur[k] for k in list(cur)[:cap]}
        return cur

    def _after_scan(self, wid, watch, queue, page, reply=None, then_poll=None,
                    summary=False):
        height = int(runtime.get_block_height())
        self.storage["scan_inflight"] = None
        strikes = dict(self.storage.get("strikes", {}))
        if strikes.pop(wid, None) is not None:
            self.storage["strikes"] = strikes
        cfg = self.storage.get("cfg", {})
        min_drop = float(cfg.get("min_drop_pct", DEFAULT_MIN_DROP_PCT))
        cur = self._scan_prices(watch, page)

        watches = dict(self.storage.get("watches", {}))
        if wid not in watches:
            return self._advance_scan(queue, reply, then_poll, summary)
        w = dict(watches[wid])
        w["last_scan_block"] = height
        w["last_ok"] = bool(cur)
        watches[wid] = w
        self.storage["watches"] = watches

        if not cur:
            runtime.emit_event("price_watcher.no_price",
                               {"watch_id": wid, "site": watch.get("name", watch.get("url", "")),
                                "block": height})
        else:
            last = dict(self.storage.get("last", {}))
            ref = dict(self.storage.get("ref", {}))
            drops = list(self.storage.get("drops", []))
            notify = list(self.storage.get("notify_queue", []))
            chat_id = watch.get("chat_id", "")
            wref = dict(ref.get(wid, {}))
            for name, price in cur.items():
                # vs the high-water mark; alerted_below suppresses repeats until a new low
                st = dict(wref.get(name) or {})
                high = float(st.get("high", price))
                alerted = st.get("alerted_below")
                if price > high:
                    high = price
                    alerted = None
                if high > 0 and price < high:
                    pct = (high - price) / high * 100.0
                    if pct >= min_drop and (alerted is None or price < alerted):
                        record = {"watch_id": wid, "item": name,
                                  "site": watch.get("name", watch.get("url", "")),
                                  "reference_price": high, "new_price": price,
                                  "drop_pct": round(pct, 2), "block": height}
                        drops.append(record)
                        runtime.emit_event("price_watcher.drop", record)
                        if chat_id:
                            notify.append({"chat_id": chat_id, "text": _drop_message(record)})
                        alerted = price
                wref[name] = {"high": high, "alerted_below": alerted}
            # prune: a server emitting fresh item names each scan would grow this
            if len(wref) > MAX_ITEMS_PER_WATCH:
                keep = set(cur) | set(list(wref)[-MAX_ITEMS_PER_WATCH:])
                wref = {k: v for k, v in wref.items() if k in keep}
            ref[wid] = wref
            last[wid] = cur
            self.storage["last"] = last
            self.storage["ref"] = ref
            self.storage["drops"] = drops[-MAX_DROPS_KEPT:]
            self.storage["notify_queue"] = notify[-MAX_NOTIFY_QUEUE:]

        return self._advance_scan(queue, reply, then_poll, summary)

    def _advance_scan(self, queue, reply, then_poll, summary):
        if queue:
            return self._scan_one(_json_bytes({"queue": queue, "reply": reply,
                                               "then_poll": then_poll,
                                               "summary": summary}))
        return self._finalize_scan(reply, then_poll, summary)

    def _finalize_scan(self, reply=None, then_poll=None, summary=False):
        self.storage["scan_pending"] = False
        report = {"watches": len(self.storage.get("watches", {})),
                  "total_drops": len(self.storage.get("drops", [])),
                  "block": int(runtime.get_block_height())}
        runtime.emit_event("price_watcher.scan_done", report)
        # only scheduled scans summarize (opt out via summary=false in set_telegram)
        if (summary and self.storage.get("tg_chat_id")
                and self.storage.get("tg_summary", True)):
            # "new drops" counted by BLOCK — the drops list is capped/pruned
            mark = int(self.storage.get("summary_mark_block", 0))
            self.storage["summary_mark_block"] = report["block"]
            lines = []
            for wid, items in sorted(self.storage.get("last", {}).items(),
                                     key=lambda kv: int(kv[0])):
                w = self.storage.get("watches", {}).get(wid, {})
                for name, price in items.items():
                    lines.append(_price_line(w.get("name", wid), name, price))
            uniq = {}
            for d in self.storage.get("drops", []):
                if int(d.get("block", 0)) > mark:
                    uniq[(str(d.get("watch_id")), d.get("item"))] = d
            self._queue_notify(
                self.storage.get("tg_chat_id"),
                "\U0001F4CA scheduled scan done — "
                + ("%d new drop(s):" % len(uniq) if uniq else "no drops")
                + ("".join("\n" + _drop_message(d)
                           for d in list(uniq.values())[:5]))
                + ("\n" + "\n".join(lines[:15]) if lines else ""))
        # replies compose here (just-written data); drop alerts drain ahead
        notify = list(self.storage.get("notify_queue", []))
        for chat, cmd in (reply or []):
            notify.append({"chat_id": chat,
                           "text": self._tg_answer(chat, cmd)[:MAX_TG_REPLY_LEN]})
        if notify:
            self.storage["notify_queue"] = []
            return self._notify_one(_json_bytes({"queue": notify[-MAX_NOTIFY_QUEUE:],
                                                 "then_poll": then_poll}))
        return self._resume_poll(then_poll, _json_bytes(report))

    # ---- notifications ----------------------------------------------------- #

    @public(decode="raw")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def notify_test(self, payload):
        """Owner-only: send one Telegram message to verify the wiring."""
        if _sender_hex() != self.storage.get("owner"):
            return _json_bytes({"error": "owner only"})
        obj = _parse(payload)
        chat_id = _clean_chat_id(obj.get("chat_id"))
        if not chat_id:
            return _json_bytes({"error": "valid numeric chat_id required"})
        return self._notify_one(_json_bytes({"queue": [
            {"chat_id": chat_id,
             "text": str(obj.get("text", "price-watcher test \U0001F44B"))}]}))

    @runner.continuation
    async def _notify_one(self, payload):
        ctx = capture()
        obj = _parse(payload)
        ctx.queue = obj.get("queue", [])
        ctx.then_poll = obj.get("then_poll")
        ctx.msg = ctx.queue.pop(0)
        body = _json_bytes({"chat_id": _clean_chat_id(ctx.msg.get("chat_id", "")),
                            "text": _no_template(ctx.msg.get("text", "")),
                            "link_preview_options": {"is_disabled": True}})
        # secret ref is the [owner, key] pair (a bare key would resolve to the
        # actor's own account); owner allowlists this actor via `secrets policy`
        ctx.result = await runner.http(
            url=TELEGRAM_SEND_URL,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=body,
            secrets=[["0x" + str(self.storage.get("owner", "")), TELEGRAM_SECRET]],
            timeout_ms=8000,
        )
        return self._after_notify(ctx.queue, ctx.result, ctx.then_poll)

    def _after_notify(self, queue, result, then_poll=None):
        result = getattr(result, "raw", result)  # unwrap an awaited RunnerResult
        ok = isinstance(result, dict) and bool(result.get("ok"))
        runtime.emit_event("price_watcher.notified", {"ok": ok, "remaining": len(queue)})
        if queue:
            return self._notify_one(_json_bytes({"queue": queue, "then_poll": then_poll}))
        return self._resume_poll(then_poll, _json_bytes({"status": "notified"}))

    # ---- telegram two-way --------------------------------------------------- #

    @public(decode="raw")
    @deferred  # dispatching the continuation's runner job is an async side effect
    def set_telegram(self, payload):
        """Owner-only: {chat_id?: admin chat, running?, summary?} — starts the
        continuous getUpdates long-poll chain (a revival also clears any stuck
        scan lock and books a strike against the stranded watch)."""
        if _sender_hex() != self.storage.get("owner"):
            return _json_bytes({"error": "owner only"})
        obj = _parse(payload)
        if "chat_id" in obj:
            self.storage["tg_chat_id"] = _clean_chat_id(obj.get("chat_id"))
        if "summary" in obj:
            self.storage["tg_summary"] = bool(obj.get("summary"))
        run = _as_bool(obj.get("running", True))
        self.storage["tg_running"] = run
        # gen fences superseded chains so exactly one runs
        gen = int(self.storage.get("tg_gen", 0)) + 1
        self.storage["tg_gen"] = gen
        # only strike + clear a scan that is actually STUCK (>60 blocks) — a
        # healthy in-flight scan must not be quarantined by a routine revival
        started = int(self.storage.get("scan_started_block", 0))
        if (self.storage.get("scan_pending")
                and int(runtime.get_block_height()) - started > 60):
            self._note_stranded_leg()
            self.storage["scan_pending"] = False
        runtime.emit_event("price_watcher.telegram_set",
                           {"running": run, "gen": gen,
                            "admin_chat": bool(self.storage.get("tg_chat_id"))})
        if not run:
            return _json_bytes({"status": "ok", "running": False})
        chat = self.storage.get("tg_chat_id", "")
        if obj.get("chat_id") and not chat:
            return _json_bytes({"error": "chat_id must be numeric"})
        if chat and not self.storage.get("tg_welcomed"):
            self.storage["tg_welcomed"] = True
            self._queue_notify(chat, self._welcome())
        return self._tg_poll(_json_bytes({"gen": gen}))

    @runner.continuation
    async def _tg_poll(self, payload):
        ctx = capture()
        ctx.gen = int(_parse(payload).get("gen", 0))
        ctx.url = (TELEGRAM_POLL_URL
                   + "?offset=" + str(int(self.storage.get("tg_offset") or 0))
                   + "&timeout=" + str(TG_LONG_POLL_S))
        ctx.page = await runner.http(
            url=ctx.url,
            method="GET",
            secrets=[["0x" + str(self.storage.get("owner", "")), TELEGRAM_SECRET]],
            timeout_ms=TG_HTTP_TIMEOUT_MS,
        )
        return self._after_tg_poll(ctx.gen, ctx.page)

    def _after_tg_poll(self, gen, page):
        if not self.storage.get("tg_running") or gen != int(self.storage.get("tg_gen", 0)):
            return _json_bytes({"status": "leg_dropped", "gen": gen})
        doc = _as_json(page)
        texts, offset = _tg_updates(doc if isinstance(doc, dict) else {},
                                    self.storage.get("tg_offset") or 0)
        self.storage["tg_offset"] = int(offset)
        commands = [(chat, t) for chat, t in texts if t.startswith("/")][:MAX_TG_COMMANDS]
        if commands:
            runtime.emit_event("price_watcher.tg_replies",
                               {"updates": len(texts), "commands": len(commands)})
        # snapshot each chat's list before removals so batched /untrack is stable
        new_wids, untrack_ok, pre = [], {}, {}
        for chat, t in commands:
            if _tg_cmd(t) == "/untrack" and chat not in pre:
                pre[chat] = sorted(self._scoped_wids(chat), key=int)
        for chat, t in commands:
            if _tg_cmd(t) == "/track":
                wid = self._tg_track(chat, t)
                if wid:
                    new_wids.append(wid)
            elif _tg_cmd(t) == "/untrack":
                ok = self._tg_untrack(chat, t, pre.get(chat))
                untrack_ok[chat] = untrack_ok.get(chat, True) and ok
        self.storage["tg_new_wids"] = new_wids
        self.storage["tg_untrack_ok"] = untrack_ok
        # /prices /drops answer from cache. scan_due (daily tick) => fleet scan;
        # new_wids (/track) and baseline_due scan only their own watches, so no
        # public caller widens a scan past run_now's scope. This leg dispatches
        # timer-queued scans (timers can't) and breaks a stale (>60-block) one.
        watches = self.storage.get("watches", {})
        due = bool(self.storage.get("scan_due"))
        baseline = [w for w in self.storage.get("baseline_due", []) if w in watches]
        if due or new_wids or baseline:
            wids = (list(watches.keys()) if due
                    else list(dict.fromkeys(new_wids + baseline)))
            height = int(runtime.get_block_height())
            started = int(self.storage.get("scan_started_block", 0))
            stale = (height - started) > 60
            if self.storage.get("scan_pending") and stale:
                self._note_stranded_leg()
            wids = self._scannable(wids)
            if due and not wids:
                # every candidate is quarantined: consume the tick so a later
                # /track does not inherit it and widen into a fleet scan
                self.storage["scan_due"] = False
            if wids and (not self.storage.get("scan_pending") or stale):
                self.storage["scan_due"] = False
                self.storage["baseline_due"] = []
                self.storage["scan_pending"] = True
                self.storage["scan_started_block"] = height
                runtime.emit_event("price_watcher.scan_started", {"watches": len(wids)})
                return self._scan_one(_json_bytes({"queue": wids,
                                                   "reply": [[c, t] for c, t in commands],
                                                   "then_poll": gen,
                                                   "summary": due and bool(
                                                       self.storage.get("scan_summary"))}))
        notify = list(self.storage.get("notify_queue", []))
        for chat, cmd in commands:
            notify.append({"chat_id": chat,
                           "text": self._tg_answer(chat, cmd)[:MAX_TG_REPLY_LEN]})
        if notify:
            self.storage["notify_queue"] = []
            return self._notify_one(_json_bytes({"queue": notify[-MAX_NOTIFY_QUEUE:],
                                                 "then_poll": gen}))
        return self._tg_poll(_json_bytes({"gen": gen}))

    def _tg_track(self, chat, text):
        parts = str(text).split()
        url = parts[1] if len(parts) > 1 else ""
        path, label = "", ""
        if len(parts) > 2:
            if re.match(r"^[\w\[\]]+(\.[\w\[\]]+)+$", parts[2]):
                path = parts[2]
                label = " ".join(parts[3:])[:MAX_NAME_LEN]
            else:
                label = " ".join(parts[2:])[:MAX_NAME_LEN]
        label = _no_template(label)
        if not url.startswith("http"):
            return None
        url = _norm_track_url(url)
        watches = self.storage.get("watches", {})
        # dedupe against this chat's OWN watches only — the admin chat sees every
        # chat's watches, and rejecting on someone else's would send the admin to
        # /untrack a watch that isn't theirs
        for w in watches.values():
            if w.get("owner") == "tg:" + chat and w.get("url") == url:
                return None
        items = [{"name": label or path.split(".")[-1] or "price",
                  "kind": "json", "path": path,
                  "auto_name": not label}] if path else []
        wid, err = self._add_watch(
            url, label or url.split("//")[-1].split("/")[0][:MAX_NAME_LEN],
            "tg:" + chat, chat, items)
        if err:
            return None
        runtime.emit_event("price_watcher.tracked",
                           {"watch_id": wid, "url": url,
                            "mode": "manual" if items else "auto",
                            "via": "telegram"})
        return wid

    def _tg_untrack(self, chat, text, scoped=None):
        try:
            pos = int(str(text).split()[1])
        except Exception:
            return False
        if scoped is None:
            scoped = sorted(self._scoped_wids(chat), key=int)
        if not 1 <= pos <= len(scoped):
            return False
        wid = scoped[pos - 1]
        if wid not in self.storage.get("watches", {}):  # already removed this leg
            return False
        self._remove_watch(wid)
        runtime.emit_event("price_watcher.untracked",
                           {"watch_id": wid, "via": "telegram"})
        return True

    def _welcome(self):
        n = len(self.storage.get("watches", {}))
        if n:
            return (WELCOME + "\n\nThis instance is already tracking %d "
                    "watch(es) — send /watches to see them, with URLs." % n)
        return WELCOME

    def _scoped_wids(self, chat):
        # a chat sees its own watches; the admin chat sees all
        watches = self.storage.get("watches", {})
        is_admin = bool(chat) and chat == str(self.storage.get("tg_chat_id", ""))
        return [wid for wid, w in watches.items()
                if is_admin or str(w.get("chat_id", "")) == chat]

    def _tg_answer(self, chat, text):
        cmd = _tg_cmd(text)
        watches = self.storage.get("watches", {})
        scoped = {wid: watches[wid] for wid in self._scoped_wids(chat)}
        ordered = sorted(scoped.items(), key=lambda kv: int(kv[0]))
        if cmd == "/watches":
            if not scoped:
                return ("no watches for this chat — track a url with chat_id "
                        + chat + " to add one")
            lines = []
            for pos, (wid, w) in enumerate(ordered, 1):
                lines.append("%s. %s [%s] %s" % (
                    pos, w.get("name", w.get("url", "")), w.get("mode", ""),
                    "⚠️ not supported" if w.get("quarantined")
                    else ("ok" if w.get("last_ok") else "no price yet")))
                if w.get("url") and w.get("url") != w.get("name"):
                    lines.append("   " + str(w.get("url")))
            return "watches:\n" + "\n".join(lines)
        if cmd == "/prices":
            last = self.storage.get("last", {})
            lines = []
            for wid, w in ordered:
                for name, price in (last.get(wid) or {}).items():
                    lines.append(_price_line(w.get("name", w.get("url", "")),
                                             name, price))
            if not lines:
                return "no prices yet — try /watches"
            return "prices:\n" + "\n".join(lines[:20])
        if cmd == "/drops":
            drops = [d for d in self.storage.get("drops", [])
                     if str(d.get("watch_id")) in scoped]
            if not drops:
                return "no drops recorded"
            latest = {}
            for d in drops:
                latest[(str(d.get("watch_id")), d.get("item"))] = d
            return "drops:\n" + "\n".join(_drop_message(d)
                                          for d in list(latest.values())[-10:])
        if cmd == "/track":
            parts = str(text).split()
            if len(parts) < 2 or not parts[1].startswith("http"):
                return ("usage: /track <url> [json.path]\ne.g. /track "
                        "https://api.coinbase.com/v2/prices/ETH-USD/spot data.amount")
            url = _norm_track_url(parts[1])
            created = [wid for wid in (self.storage.get("tg_new_wids") or [])
                       if watches.get(wid, {}).get("owner") == "tg:" + chat
                       and watches.get(wid, {}).get("url") == url]
            if not created:
                # own watches only: `pos` is a display index the reply tells the
                # user to /untrack, so it must never point at another chat's watch
                dups = [(pos, x) for pos, (wid, x) in enumerate(ordered, 1)
                        if x.get("url") == url
                        and x.get("owner") == "tg:" + chat]
                if dups:
                    pos, x = dups[0]
                    return ("already watching this url as %s. %s — "
                            "/untrack %s to replace it"
                            % (pos, x.get("name", ""), pos))
                return "couldn't track that url — watch limit reached?"
            wid = created[-1]
            w = watches[wid]
            got = self.storage.get("last", {}).get(wid) or {}
            if not got:
                self._remove_watch(wid)
                return ("⚠️ couldn't find a price at %s — nothing was tracked. "
                        "JSON APIs usually need a path: "
                        "/track <url> <json.path> [name]"
                        % w.get("name", "that url"))
            return "tracked ✅\n" + "\n".join(
                _price_line(w.get("name", ""), n, p)
                for n, p in list(got.items())[:5])
        if cmd == "/untrack":
            if len(str(text).split()) < 2:
                return "usage: /untrack <n> — numbers are in /watches"
            if not self.storage.get("tg_untrack_ok", {}).get(chat, True):
                return "no watch at that number — /watches to see the list"
            return "untracked ✅\n" + self._tg_answer(chat, "/watches")
        if cmd in ("/start", "/help"):
            return self._welcome()
        return ("commands: /watches /prices /drops /track <url> [path] "
                "/untrack <id> — /help for examples")

    # ---- control ----------------------------------------------------------- #

    @public(decode="raw")
    def set_running(self, payload):
        """Owner-only. {"running", "interval_blocks"?} for fixed cadence, or
        {"at": "09:00", "tz_offset_min": -240} for daily wall-clock mode
        (offset vs UTC; no tz database on-chain, adjust for DST manually).
        {"summary_every_blocks"?} (interval mode only) sends the digest every N
        blocks (0 = every scan). {"min_drop_pct"?} retunes the alert threshold."""
        if _sender_hex() != self.storage.get("owner"):
            return _json_bytes({"error": "owner only"})
        obj = _parse(payload)
        cfg = dict(self.storage.get("cfg", {}))
        was_running = bool(self.storage.get("running", True))
        old_interval, old_at = cfg.get("interval_blocks"), cfg.get("scan_at_min")
        old_tz = cfg.get("tz_offset_min")
        if "min_drop_pct" in obj:
            cfg["min_drop_pct"] = _as_float(obj["min_drop_pct"], cfg.get("min_drop_pct", DEFAULT_MIN_DROP_PCT))
        if "at" in obj:
            hh, _, mm = str(obj["at"]).partition(":")
            cfg["scan_at_min"] = (_as_int(hh, 0) % 24) * 60 + (_as_int(mm or 0, 0) % 60)
            cfg["tz_offset_min"] = _as_int(obj.get("tz_offset_min"),
                                           cfg.get("tz_offset_min", 0))
            cfg.pop("summary_every_blocks", None)  # wall-clock uses a daily digest
        elif "interval_blocks" in obj:
            cfg["interval_blocks"] = max(30, _as_int(obj["interval_blocks"], DEFAULT_INTERVAL_BLOCKS))
            cfg.pop("scan_at_min", None)
        if "summary_every_blocks" in obj and cfg.get("scan_at_min") is None:
            cfg["summary_every_blocks"] = max(0, _as_int(obj["summary_every_blocks"], 0))
        self.storage["cfg"] = cfg
        run = _as_bool(obj.get("running", was_running))
        self.storage["running"] = run
        cadence_changed = (cfg.get("interval_blocks") != old_interval
                           or cfg.get("scan_at_min") != old_at
                           or cfg.get("tz_offset_min") != old_tz)
        if run and ("running" in obj or cadence_changed):
            gen = int(self.storage.get("tick_gen", 0)) + 1
            self.storage["tick_gen"] = gen
            self._schedule("daily_tick", self._next_tick_blocks(cfg), gen)
        return _json_bytes({"status": "ok", "running": run,
                            "scan_at_min": cfg.get("scan_at_min"),
                            "tz_offset_min": cfg.get("tz_offset_min"),
                            "summary_every_blocks": cfg.get("summary_every_blocks"),
                            "interval_blocks": int(cfg.get("interval_blocks",
                                                           DEFAULT_INTERVAL_BLOCKS))})

    def _next_tick_blocks(self, cfg):
        at = cfg.get("scan_at_min")
        if at is None:
            return int(cfg.get("interval_blocks", DEFAULT_INTERVAL_BLOCKS))
        now_s = int(runtime.get_timestamp_ms()) // 1000
        local = (now_s + int(cfg.get("tz_offset_min", 0)) * 60) % 86400
        delta = int(at) * 60 - local
        if delta <= 60:
            delta += 86400
        return min(delta, 900 if delta > 3600 else 300)

    def _schedule(self, handler, interval, gen=None):
        # fee_payer must be the tx sender or the scheduling actor
        body = {} if gen is None else {"gen": int(gen)}
        runtime.schedule_after(int(interval), handler, _json_bytes(body),
                               fee_payer=runtime.get_sender() or None)

    # ---- views ------------------------------------------------------------- #

    @public(decode="raw")
    @pure
    def get_watches(self, payload):
        # public getter: redact chat_id + headers (may hold caller API keys)
        pub = {}
        for wid, w in self.storage.get("watches", {}).items():
            pub[wid] = {k: v for k, v in w.items()
                        if k not in ("chat_id", "headers")}
        return _json_bytes({"watches": pub})

    @public(decode="raw")
    @pure
    def get_prices(self, payload):
        wid = str(_parse(payload).get("watch_id", ""))
        last = self.storage.get("last", {})
        if wid:
            return _json_bytes({"watch_id": wid, "prices": last.get(wid, {})})
        return _json_bytes({"prices": last})

    @public(decode="raw")
    @pure
    def get_drops(self, payload):
        limit = int(_parse(payload).get("limit", 50))
        drops = list(self.storage.get("drops", []))
        return _json_bytes({"drops": drops[-limit:]})


# ---- module-level entrypoints (PVM invokes these by name) -------------------- #

def _get_actor():
    return PriceWatcher()


def init(payload): return _get_actor().init(payload)
def track(payload): return _get_actor().track(payload)
def untrack(payload): return _get_actor().untrack(payload)
def run_now(payload): return _get_actor().run_now(payload)
def daily_tick(payload): return _get_actor().daily_tick(payload)
def set_running(payload): return _get_actor().set_running(payload)
def set_telegram(payload): return _get_actor().set_telegram(payload)
def notify_test(payload): return _get_actor().notify_test(payload)
def get_watches(payload): return _get_actor().get_watches(payload)
def get_prices(payload): return _get_actor().get_prices(payload)
def get_drops(payload): return _get_actor().get_drops(payload)
def _scan_one__resume(payload): return _get_actor()._scan_one__resume(payload)
def _notify_one__resume(payload): return _get_actor()._notify_one__resume(payload)
def _tg_poll__resume(payload): return _get_actor()._tg_poll__resume(payload)
