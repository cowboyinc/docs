"""Extraction fixtures: JSON-LD, Microdata, OpenGraph, JSON walk, overrides.

Run: python3 tests/test_scraping.py  (from the example directory or anywhere)
"""
import base64
import json

from _bootstrap import load_actor, mock  # noqa: F401

pw = load_actor()


def raw_page(html):
    # what runner.http delivers for a text/html response
    return {"raw": base64.b64encode(html.encode()).decode(),
            "content_type": "text/html; charset=utf-8"}


def detect(html):
    return {r["name"]: r["price"] for r in pw._auto_detect(raw_page(html))}


passed, gaps = [], []


def check(label, cond, gap=False):
    if cond:
        passed.append(label)
    elif gap:
        gaps.append(label)
    else:
        raise AssertionError(label)


check("json-ld: Product > offers dict, string price", detect("""
<html><head><title>Shop</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Fancy Kettle",
 "offers":{"@type":"Offer","price":"79.99","priceCurrency":"USD"}}
</script></head></html>""") == {"Fancy Kettle": 79.99})

check("json-ld: offers as LIST of offers", detect("""
<title>x</title><script type="application/ld+json">
{"@type":"Product","name":"Boots",
 "offers":[{"price":"120.00"},{"price":"110.00"}]}
</script>""") == {"Boots": 120.0})  # first offer wins by design

check("json-ld: priceSpecification", detect("""
<title>x</title><script type="application/ld+json">
{"@type":"Product","name":"Lamp",
 "offers":{"priceSpecification":{"price":49.5,"priceCurrency":"EUR"}}}
</script>""") == {"Lamp": 49.5})

check("json-ld: @graph nesting + multiple products", detect("""
<title>x</title><script type="application/ld+json">
{"@graph":[{"@type":"Product","name":"A","offers":{"price":"10"}},
           {"@type":"Product","name":"B","offers":{"price":"20"}}]}
</script>""") == {"A": 10.0, "B": 20.0})

check("json-ld: comma thousands + currency text", detect("""
<title>x</title><script type="application/ld+json">
{"@type":"Product","name":"TV","offers":{"price":"1,299.00 USD"}}
</script>""") == {"TV": 1299.0})

check("json-ld: AggregateOffer lowPrice", detect("""
<title>x</title><script type="application/ld+json">
{"@type":"Product","name":"Chair",
 "offers":{"@type":"AggregateOffer","lowPrice":"89.00","highPrice":"120.00","offerCount":14}}
</script>""") == {"Chair": 89.0})

check("microdata: <meta itemprop=price content=...>", detect("""
<html><head><title>Acme Widget Page</title></head><body>
<div itemscope itemtype="https://schema.org/Product">
<meta itemprop="price" content="34.95"/>
</div></body></html>""") == {"Acme Widget Page": 34.95})

check("microdata: reversed attribute order", detect("""
<title>T</title><meta content="12.50" itemprop="price">""") == {"T": 12.5})

check("microdata: content attr on a <span>", detect("""
<title>T</title><span itemprop="price" content="55.00">$55.00</span>""")
      == {"T": 55.0})

check("microdata: price in element TEXT", detect("""
<title>T</title><span itemprop="price">$1,499.99</span>""")
      == {"T": 1499.99})

check("og: og:price:amount", detect("""
<title>OG Shop</title>
<meta property="og:price:amount" content="19.90">
<meta property="og:price:currency" content="EUR">""") == {"OG Shop": 19.9})

check("og: product:price:amount variant", detect("""
<title>OG Shop</title>
<meta property="product:price:amount" content="19.90">""")
      == {"OG Shop": 19.9})

check("priority: json-ld wins, meta ignored", detect("""
<title>T</title>
<script type="application/ld+json">{"@type":"Product","name":"P","offers":{"price":"5"}}</script>
<meta itemprop="price" content="999">""") == {"P": 5.0})

api = {"data": {"items": [
    {"name": "Widget", "price": 9.99, "stock": 3},
    {"name": "Gadget", "price": "24.50"},
]}}
got = {r["name"]: r["price"] for r in pw._auto_detect(api)}
check("json api: named items", got == {"Widget": 9.99, "Gadget": 24.5})

crypto = {"bitcoin": {"usd_price": 64123.5, "price_change_24h": -1.42}}
got = pw._auto_detect(crypto)
check("json api: negative price-ish noise is filtered out",
      any(r["price"] == 64123.5 for r in got)
      and not any(r["price"] == -1.42 for r in got))

page = {"data": {"products": [{"title": "X", "pricing": {"final": 42.0}}]}}
check("override json: dotted path with index",
      pw._extract_override({"kind": "json", "path": "data.products.0.pricing.final"},
                           page) == 42.0)
check("override json: digit DICT key (Steam appid maps)",
      pw._extract_override({"kind": "json", "path": "440.data.price_overview.final"},
                           {"440": {"data": {"price_overview": {"final": 5999}}}}) == 5999.0)
check("override json: [i] bracket form",
      pw._extract_override({"kind": "json", "path": "data.products[0].pricing.final"},
                           page) == 42.0)

html = raw_page('<div id="price" class="big">$1,234.56</div>')
check("override html: regex group 1",
      pw._extract_override({"kind": "html",
                            "pattern": r'id="price"[^>]*>\$([\d.,]+)'}, html) == 1234.56)

fng = {"name": "Fear and Greed Index", "data": [{"value": "30",
       "value_classification": "Fear"}]}
check("auto-name: path item harvests ancestor name field",
      pw.PriceWatcher()._scan_prices(
          {"mode": "manual", "max_items": 32,
           "items": [{"name": "value", "kind": "json", "path": "data.0.value",
                      "auto_name": True}]}, fng) == {"Fear and Greed Index": 30.0})
check("auto-name: explicit label wins (no auto_name flag)",
      pw.PriceWatcher()._scan_prices(
          {"mode": "manual", "max_items": 32,
           "items": [{"name": "My Label", "kind": "json",
                      "path": "data.0.value"}]}, fng) == {"My Label": 30.0})
check("auto-name: nameless doc keeps path fallback",
      pw.PriceWatcher()._scan_prices(
          {"mode": "manual", "max_items": 32,
           "items": [{"name": "amount", "kind": "json", "path": "data.amount",
                      "auto_name": True}]},
          {"data": {"amount": "64.5"}}) == {"amount": 64.5})


class FakeStore(dict):
    pass


actor = pw.PriceWatcher()
watch = {"mode": "auto", "max_items": 32}
sale_page = raw_page("""
<title>x</title><script type="application/ld+json">
[{"@type":"Product","name":"Coat","offers":{"price":"200"}},
 {"@type":"Product","name":"Coat","offers":{"price":"149.99"}}]
</script>""")
check("scan: duplicate names collapse to LOWER price (sale wins)",
      actor._scan_prices(watch, sale_page) == {"Coat": 149.99})

no_price = actor._scan_prices(watch, raw_page("<title>JS shop</title><div id='app'></div>"))
check("scan: JS-rendered page yields {} (no guessing)", no_price == {})

shopify_variants = {"product": {"title": "Straight Shooter", "variants": [
    {"title": "%s / D" % s, "price": "995.00"} for s in (8, 9, 10, 11, 12)]}}
check("scan: variant explosion collapses to one product line",
      actor._scan_prices({"mode": "auto", "name": "www.lucchese.com"},
                         shopify_variants) == {"Straight Shooter": 995.0})
two_real = {"items": [{"name": "A", "price": 5.0}, {"name": "B", "price": 5.0}]}
check("scan: a few same-priced items do NOT collapse",
      actor._scan_prices({"mode": "auto"}, two_real) == {"A": 5.0, "B": 5.0})

check("price: currency symbol wins inside prose",
      pw._to_price("Galaxy S24 Ultra $1,419.99") == 1419.99)
check("price: trailing currency code", pw._to_price("1,299.00 USD") == 1299.0)
check("price: a digit run inside a name is NOT a price",
      pw._to_price("Galaxy S24 512GB") is None)
check("price: a date is NOT a price", pw._to_price("2024-05-01") is None)
check("price: clean numbers still parse",
      pw._to_price("49.99") == 49.99 and pw._to_price("1799") == 1799.0)

doc = {"product": {"sku": "AB-1234", "price": {"amount": "49.99"}}}
check("override: empty json path yields None (not the whole doc)",
      pw._extract_override({"kind": "json", "path": ""}, doc) is None)
check("override: a malformed regex returns None, does not raise",
      pw._extract_override({"kind": "html", "pattern": "price ("}, "price 5") is None)

rec = {"site": pw._no_template("${TELEGRAM_BOT_TOKEN}"), "item": "x",
       "reference_price": 100.0, "new_price": 50.0, "drop_pct": 50.0}
check("security: ${...} in a name cannot reach an outgoing template",
      "${" not in pw._drop_message(rec))

check("security: ${ chat_id is rejected", pw._clean_chat_id("${") == "")
check("security: ${TOKEN} chat_id is rejected",
      pw._clean_chat_id("${TELEGRAM_BOT_TOKEN}") == "")
check("chat_id: real numeric ids (incl. negative groups) pass",
      pw._clean_chat_id("1234567890") == "1234567890"
      and pw._clean_chat_id("-1001234") == "-1001234")

_reject, dropped = pw._clean_items(
    [{"name": "x", "kind": "html", "pattern": "(a+)+$"}], allow_regex=False)
check("security: a non-owner regex override is dropped (not run)",
      _reject == [] and dropped == 1)
_keep, _ = pw._clean_items(
    [{"name": "B", "kind": "html", "pattern": "price_color[^0-9]*([0-9]+[.][0-9]+)"}],
    allow_regex=True)
check("regex: the owner may register a real override", len(_keep) == 1)
_slow, _ = pw._clean_items([{"name": "p", "path": "a" * 5000}], allow_regex=True)
check("path: an over-long json path is rejected", _slow == [])
# the DoS bound protects a page that PASSES the substring gate — so the crafted
# input must contain the marker, or the test measures nothing (it would run
# 20s+ with the {0,400} bound reverted)
import time as _time
_adversarial = 'itemprop="price" ' + "<" * 60000
_t0 = _time.time(); pw._auto_detect_html(_adversarial); _dt = _time.time() - _t0
check("security: bounded auto-detect on an itemprop+60k-'<' page stays fast",
      _dt < 2.0)
# pin the int guard by the RESULT of _split_path (a huge token stays a str),
# since CPython's own 4300-digit int limit hides the PVM hazard the guard exists for
check("path: a 4000-digit index token is not int()'d (stays a string)",
      isinstance(pw._split_path("x." + "9" * 4000)[1], str))

# single price or a was/now pair (lower, up to ~60% off); refuse three+ or far-apart
check("price: a was/now pair -> the LOWER (sale) price",
      pw._to_price("Now $1,419.99 (was $1,499.99)") == 1419.99)
check("price: a deep 33%-off was/now is still READ, not refused",
      pw._to_price("Was $1,499.00 Now $999.00") == 999.0)
check("price: a 50%-off was/now reads the sale price",
      pw._to_price("$200.00 $100.00") == 100.0)
check("price: $1419.9 and $1419.90 (same number) -> that price",
      pw._to_price("$1419.9 / $1419.90") == 1419.9)
for _s in (
    "Save $50 on Galaxy - now $1,419.99",
    "Free shipping over $35 — Galaxy $1,199.00",
    "Bundle: phone $999.00 + case $19.99",
    "$1,199.00 was $1,299.00 — save $100",
    "Galaxy S24 $1,199.99 or 4 payments of $299.99",
    "$1,199.99 — trade in your old phone for $400",
    "$1,199.99 | Gift card $250 included",
    "$1,199 (was $1,499) — protection plan $199",
):
    check("price: far-apart / 3+ prose refused -> None  (%s)" % _s[:30],
          pw._to_price(_s) is None)
check("auto-detect: a negative og:price is rejected",
      detect('<title>Shop</title><meta property="og:price:amount" content="-1234.00">') == {})
check("auto-detect: a negative JSON-LD price is rejected (walk_ld filter)",
      detect('<title>x</title><script type="application/ld+json">'
             '{"@type":"Product","name":"N","offers":{"price":"-9.99"}}</script>') == {})
check("auto-detect: a $0.00 (100%-off) price is KEPT",
      detect('<title>Free</title><meta property="og:price:amount" content="0.00">')
      == {"Free": 0.0})
check("override: a negative value is still allowed (temperature in winter)",
      pw._extract_override({"kind": "json", "path": "t"}, {"t": -5.0}) == -5.0)

print("PASSED (%d):" % len(passed))
for p in passed:
    print("  ok  " + p)
print("KNOWN GAPS (%d):" % len(gaps))
for g in gaps:
    print("  gap " + g)
