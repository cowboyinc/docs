# Price Watcher

An on-chain actor that watches product pages and reports price drops. Register a
website; once the owner turns scanning on (opt-in — see `autostart`/`set_running`),
the actor re-fetches it each interval (default: daily), reads each item's current
price, and emits a `price_watcher.drop` event for anything that fell since the
last reading.

## How it works

```
 timer (daily)                    runner.http                     on-chain
 ┌───────────┐  queue of watches  ┌──────────────┐  page body   ┌──────────────┐
 │ daily_tick │ ───────────────►  │  _scan_one   │ ───────────► │  _after_scan │
 └───────────┘  one job per site  │ (continuation)│  (resume)   │  extract +   │
       ▲                          └──────────────┘              │  diff + emit │
       │ reschedule                    │ chains next site       └──────┬───────┘
       └───────────────────────────────┘                               │
                                                                 price_watcher.drop
```

(Logical flow — on node v0.0.34 timer-fired code cannot dispatch runner jobs,
so the tick only *marks* the scan due and the next live Telegram poll leg
dispatches it. In wall-clock mode, between daily summaries, off-slot wakeups run
quiet alert-only scans about every ~15 minutes.)

Each site is fetched in its own runner job; `_scan_one` fetches one, then
re-invokes itself for the next until the queue drains. A price is compared
against the item's **high-water mark** (the highest it's been since tracking
began), so a slow decline is caught even when no single scan-to-scan step is
big enough. A drop re-alerts only on a new low; a recovery to a new high resets
the reference.

## Who can do what

Registration is open — anyone can `track` a site — but each watch records its
registrant. `untrack` only removes your own watches (the actor owner can remove
any); `run_now` scans only your watches (the owner scans all); `set_running`
(pausing the daily timer) is owner-only. The **owner** is whoever called `init`
at deploy. Caps: 64 watches total, 16 per registrant.

## Reading a price

**Auto-detect (default)** — `track(url)` with no `items`. Scrapes the structured
price markup most shopping pages embed, in order of reliability:

- JSON-LD (`<script type="application/ld+json">` → Product/Offer `price`)
- Microdata (`<meta itemprop="price" content="...">`)
- OpenGraph (`<meta property="og:price:amount" content="...">`)
- a JSON API response (any key containing `price`)

Items are keyed by the product name found alongside the price. JSON-LD and
JSON APIs carry per-item names, so multi-product pages track each item
separately; microdata/OpenGraph pages collapse to ONE item named by the page
title (the lowest price on the page wins). It's a heuristic: pages that render
price via JavaScript, or use bespoke markup, yield nothing — recorded as a
`price_watcher.no_price` event, never guessed.

On a large listing page, pass `max_items` to cap how many items a watch tracks
(and therefore how many drops it can report per scan) — the first N found, up to
a hard ceiling of 32. E.g. `{ "url": "...", "max_items": 5 }`.

**Override** — when auto-detect misses, tell the actor exactly where to look:

```jsonc
// JSON API: dotted field path
{ "url": "https://api.shop/p/42", "items": [{ "name": "Widget", "path": "data.price" }] }

// HTML page: regex, price in group 1 (or whole match)
{ "url": "https://shop/widget", "items": [{ "name": "Widget", "kind": "html",
                                            "pattern": "id=\"price\"[^>]*>\\$([\\d.,]+)" }] }
```

> CSS selectors aren't reachable from actor code (the on-chain `extraction=`
> field is JSONPath-only), so HTML overrides are regex, run in-actor. **A regex
> override can only be registered by the actor owner** — the on-chain regex
> engine is unmetered, so an attacker-supplied pattern would be an unbounded-
> backtracking DoS; public/chat callers get JSON-path overrides + auto-detect.
> Auto-detect's own scans are bounded (input cap + bounded quantifiers).
>
> `runner.http` returns only the response **body** — no status code, no headers.
> A redirect (3xx) arrives as an empty body with no signal, so register the
> **final/canonical URL**, not one that redirects.

## Simulate Locally

The offline suites drive the real handlers through the SDK's mock host — no
chain, no Telegram. From this directory (with `COWBOY_SDK_PATH` pointing at a
`node/pvm/Lib` checkout):

```bash
uv run pytest
cowboy actor check watcher
```

`cowboy test` compiles the `watcher` target the way a deploy does and runs one
handler on the simulated PVM. This calls `get_watches` (raw JSON payload) on a
fresh actor and expects the JSON reply bytes:

```bash
cowboy test --target watcher --handler get_watches --payload 0x7b7d \
  --expect 0x7b2277617463686573223a207b7d7d
```

`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Run On A Network

```bash
TELEGRAM_BOT_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=123456789 ./demo.sh
```

`demo.sh` deploys the `watcher` target with `cowboy actor deploy watcher`; the
CLI discovers the target from this project's `cowboy.toml`, so use a CLI
release that matches the network you target.

The demo targets a CBSS-enabled network (canyon by default, `NETWORK=mesa` to
switch) and walks the full live flow: wallet + faucet, account DKG, planting the
bot token as a CBSS secret, an entitled deploy, one real scan (`run_now` against
a JSON price API), a `notify_test` checkpoint that must land in your Telegram
chat, and finally `set_telegram` — at which point you text the bot `/prices`
and watch it answer. It needs Tailscale reachability, a token from @BotFather,
and your chat id (ask @userinfobot; also message your bot once first — bots
cannot initiate chats). Because it requires a live network and real Telegram
credentials, this example is outside the default gallery sweep.

## Deploy

The actor needs a manifest declaring every entitlement it uses — a custom
manifest **replaces** the defaults, so listing only `secrets.read` strips
storage/timer/http rights and every handler fails with E1207. Ids must be in
lexicographic order. `secrets.read` is what lets the runner substitute the
bot token — without it Telegram receives the literal string
`${TELEGRAM_BOT_TOKEN}`:

```bash
cat > manifest.json <<EOF
{ "name": "price-watcher", "version": "1.0.0",
  "entitlements": [
    { "id": "econ.hold_balance" },
    { "id": "http.fetch" },
    { "id": "secrets.read", "params": { "keys": ["<your-address>:TELEGRAM_BOT_TOKEN"] } },
    { "id": "storage.kv" },
    { "id": "timer.schedule" }
  ] }
EOF

cowboy actor deploy watcher \
  --manifest-json manifest.json \
  --salt 0x0123456789abcdef \
  --init-payload '{"interval_blocks": 86400, "min_drop_pct": 5.0}' \
  --private-key ./your-key.hex
```

(Telegram-less deploys can skip the `secrets.read` key and emit `price_watcher.drop`
events only. Note: on node v0.0.34 the timer can't dispatch scan jobs — the live
poll chain is what consumes `scan_due` — so without Telegram, drive scans with
explicit `run_now` calls; automatic scheduled scans need the poll chain.)

`init` config (all optional): `interval_blocks` (default 86400 ≈ 1 day at ~1s
blocks), `timeout_ms` (8000; node v0.0.34's runner ignores this and uses a fixed
60s HTTP timeout, so it is currently a no-op), `min_drop_pct` (0.0 = report any drop),
`autostart` (**default false** — scanning is opt-in). Turn scanning on at deploy with
`autostart:true`, or later with `set_running` — include `running:true` to start,
restart, or re-arm scanning (e.g. `{"running":true,"at":"09:00","tz_offset_min":-240}`
for quiet ~15-min scans plus a daily digest). Changing the cadence
(`interval_blocks`, `at`, `tz_offset_min`) also re-arms the timer. To adjust any
other setting on a running watcher, send just that field — adding `running:true`
to a routine tweak restarts the interval countdown and leaves the superseded timer
to fire once against the actor's balance.

> Funding note: the scan timer re-arms itself, and on node v0.0.34 a timer fire's
> fee-payer is the **actor's own account** (not the owner) — so each re-armed link,
> and every runner scan job, is paid from the actor's balance. `demo.sh` transfers a
> starting balance to the actor; keep it topped up, or scheduled scanning stops when
> it runs dry. The node emits a `timer.cancelled_insufficient_funds` event when it
> drops the timer (watch for it), then re-arm with `set_running {"running": true}`.

## Use

`$PW` is the deployed actor address. `execute` takes a **hex-encoded** payload,
not raw JSON — `demo.sh`'s `hx()` helper does the encoding; the JSON is shown
here for readability (e.g. `--payload "$(printf '%s' '{"..."}' | xxd -p -c0)"`).

```bash
# Track a site (auto-detect). Add "baseline": true to take an immediate first
# reading, so the first daily fire can already detect a drop.
cowboy actor execute --actor $PW --handler track \
  --payload '{"url": "https://shop.example/widget", "baseline": true}'

cowboy actor execute --actor $PW --handler run_now    --payload '{}'   # scan now (testing)
cowboy actor execute --actor $PW --handler get_prices --payload '{}'   # latest price per item
cowboy actor execute --actor $PW --handler get_drops  --payload '{"limit": 20}'
cowboy actor execute --actor $PW --handler get_watches --payload '{}'
cowboy actor execute --actor $PW --handler untrack    --payload '{"watch_id": "1"}'
cowboy actor execute --actor $PW --handler set_running --payload '{"running": false}'  # pause timer

# Wall-clock mode: anchor the daily summary scan to a local time. The actor
# wakes in capped hops (~15 min) to stay on target, and every off-slot wakeup
# runs a QUIET scan — drop alerts fire around the clock, but only the on-slot
# scan sends the full summary. Quiet scans keep ~12.5 min spacing from any scan.
cowboy actor execute --actor $PW --handler set_running \
  --payload '{"running": true, "at": "09:00", "tz_offset_min": -240}'

# Decoupled digest: scan every ~3 min (live 📉 alerts); summarize ~every 6-9 min.
# summary_every_blocks is a floor evaluated at scan-finalize, so it rounds UP to a
# later tick — further out with more watches or slower fetches (a few → ~9 min).
cowboy actor execute --actor $PW --handler set_running \
  --payload '{"running": true, "interval_blocks": 180, "summary_every_blocks": 300}'
```

> A drop is only detectable on the **second** reading of an item — the first is
> just a baseline. Use `"baseline": true` on `track` (or one `run_now`) to seed it.

## Telegram notifications

Pass a `chat_id` when you `track` a site and drops on that watch are pushed to
Telegram — a POST to the Bot API on its own continuation chain after the scan:

```bash
cowboy actor execute --actor $PW --handler track \
  --payload '{"url": "https://shop.example/widget", "chat_id": "123456789"}'
```

The bot token is a **CBSS secret**, never stored on-chain — provision it under the
name `TELEGRAM_BOT_TOKEN`; the runner substitutes `${TELEGRAM_BOT_TOKEN}` into the
send URL at request time. Verify the wiring without waiting for a drop:

```bash
cowboy actor execute --actor $PW --handler notify_test --payload '{"chat_id": "123456789"}'  # owner only
```

No `chat_id` on a watch → no Telegram, just the on-chain `price_watcher.drop` event.

### Two-way: chat commands, near-real-time

Like the hyperliquid-trader's notifier the actor also *listens* — but where
`hl_notify` polls on an N-block timer (worst case minutes of lag), here an
owner-only `set_telegram` starts a **continuous long-poll chain**: each leg
calls `getUpdates?timeout=25`, which hangs on Telegram's servers and returns
the instant a message arrives. A landed leg answers any commands (through the
same notify chain the drop alerts use) and immediately re-arms the next leg,
so a reply reaches the chat a few blocks (~seconds) after the user hits send:

```bash
cowboy actor execute --actor $PW --handler set_telegram \
  --payload '{"chat_id": "123456789"}'          # owner only; starts the chain

cowboy actor execute --actor $PW --handler set_telegram \
  --payload '{"running": false}'                # stop listening
```

- `/watches` — your registered watches and whether the last scan found a price (instant)
- `/prices` — answers **instantly from the cache**. In wall-clock mode the
  quiet scans keep it at most ~15 minutes stale; in fixed-interval mode it is as
  stale as `interval_blocks`, so scan first with `run_now` if you
  need it fresh. Drop alerts push the moment any scan sees one, regardless.
- `/drops` — instant too: each item's current drop from its tracked high
- `/track <url> [json.path]` — register a watch **from chat**: the poll resume is
  an on-chain execution, so the write happens right there, owned by your chat id
  (`tg:<chat>`). With a path it's a json override; without, auto-detect. Only the
  new watch is baseline-scanned in the same pass, so the confirmation quotes its
  price within seconds. A url the chat already watches is rejected with a pointer
  to the existing watch, and a track whose first scan finds no price is dropped
  rather than kept as a dead watch
- `/untrack <n>` — remove a watch by its `/watches` list number; the list
  renumbers 1..N after removals (positions are per-chat display numbers —
  storage ids underneath stay stable). The admin chat can remove any watch;
  wallet-registered watches can also be managed from the CLI by real id
- anything else — a short command list (instant)

A chat sees only the watches registered with its own `chat_id`; the `chat_id`
passed to `set_telegram` is the **admin chat** and sees everything. Unlike the
hyperliquid notifier (which keeps `bot_token` in actor storage), the token never
touches the chain here — the poll URL uses the same `${TELEGRAM_BOT_TOKEN}` CBSS
secret as sends.

Reliability: a generation counter fences off superseded legs, so re-calling
`set_telegram` restarts the loop instead of forking a second one — and that
re-call is also how a dead chain is revived, since node v0.0.34 timers can't
dispatch a runner job and so on-chain auto-recovery isn't possible. Idle cost is
one runner job per ~25s (~3.5k/day) — that's the price of instant replies; raise
`TG_LONG_POLL_S` toward the runner's 60s HTTP ceiling to thin it out.

> Telegram allows one `getUpdates` consumer per bot token: don't point another
> poller (or a webhook) at the same bot, or the two will steal each other's
> updates. The first leg after enabling drains any backlog; at most 3 commands
> are answered per leg.

## Events

- `price_watcher.tracked` — a site was registered `{watch_id, url, mode}`
- `price_watcher.drop` — a price fell below its high-water mark `{watch_id, site, item, reference_price, new_price, drop_pct, block}`
- `price_watcher.no_price` — a scan found no price on a site `{watch_id, site, block}`
- `price_watcher.notified` — a Telegram send completed `{ok, remaining}`
- `price_watcher.telegram_set` — poll chain (re)configured `{running, gen, admin_chat}`
- `price_watcher.tg_replies` — a poll leg answered commands `{updates, commands}`
- `price_watcher.scan_started` / `price_watcher.scan_queued` / `price_watcher.scan_skipped`
  / `price_watcher.scan_done` — scan lifecycle (queued/skipped are the wall-clock
  timer marking or deferring a scan)
- `price_watcher.quarantined` — a watch was excluded after repeated stranded
  fetches `{watch_id, url}`
- `price_watcher.untracked` — a watch was removed `{watch_id, via}`

## Files

- `cowboy.toml` — project manifest; the `watcher` target is discovered from
  `actors/watcher/main.py`
- `actors/watcher/main.py` — the actor (auto-detect + override, wall-clock
  scheduler with quiet scans, drop detection, Telegram chain)
- `demo.sh` — live walkthrough on canyon/mesa (secrets, entitled deploy, scan, notify_test, chat)
- `tests/` — offline suites that drive the real handlers through the SDK's mock
  host (no chain): extraction fixtures + the price parser and injection guards
  (`test_scraping.py`), and the full Telegram chain semantics — instant replies,
  re-arm, gen fencing, unauth `track(baseline)` scoping (`test_telegram_chain.py`).
  Run `uv run pytest`, or one file at a time with `python3 tests/test_<name>.py`.
  They load the real on-chain SDK + mock host from `$COWBOY_SDK_PATH` (falling
  back to the sibling `node` checkout, the same one `demo.sh` uses) — no
  stubbing, so the actual continuation FSM runs; they skip with a clear message
  if neither is present. They target the current SDK contract: runner replies
  are CBOR wire bytes and awaited results arrive as `RunnerResult` wrappers.

## Limitations

- **Response-size ceiling (node v0.0.34): roughly ~45KB of raw HTML.** Runner
  results ride a message payload bounded twice: `MAX_INLINE_BLOB_BYTES`
  (64 KiB, COW-1253 admission cap — its own docs say larger payloads must go
  through CBFS by hash) and `MAX_MESSAGE_PAYLOAD_SIZE` (128 KiB wire-decode
  bound). Base64 + envelope overhead puts the practical page ceiling near
  45KB. JSON APIs are unaffected. Larger pages fetch fine but their result tx is rejected
  at admission — and on current builds that *strands the scan continuation*
  (no failure callback) and can nonce-wedge the executing runner. The actor
  defends itself: a stranded fetch earns a strike, and a watch that has never
  succeeded is **quarantined on its first strike** (proven watches get two),
  excluded from future scans, flagged ⚠️ in `/watches`, with one alert to the
  admin chat. Until the node ships result truncation or CBFS spill, track
  JSON APIs and small pages; real store pages (typically 100KB–3MB) will
  quarantine themselves.

- Heuristic scraping: no JavaScript execution, so JS-rendered prices need an
  override or won't work.
- No CAPTCHA / cookie / login support — `runner.http` exposes no response
  headers, so there's no session to carry.
- Multi-currency isn't normalized; a price is compared to its own history, so a
  currency change on the page would read as a spurious jump.
- Shopify variant collapse: while every variant shares one price, the watch
  shows a single line named for the product. If a variant later diverges (a
  per-size sale), items re-key to variant names and their drop history restarts
  — the first divergence itself won't alert.
- Prices: `_to_price` is a **best-effort heuristic, not a reliable extractor.** It
  reads a single clean currency value, or a `was $Y / now $X` pair within ~2.5×
  (`WAS_NOW_RATIO`, the lower = the sale); it **refuses** three-or-more numbers or a
  wider pair, which filters the big decoys (gift card, trade-in, instalment). Two
  limits are real and worth stating plainly:
  - it **won't read a was/now crash steeper than ~60% in one string** — a
    `WAS_NOW_RATIO` trade-off, since a wider ratio would admit more decoys; and
  - it **can be fooled by a lone high value** — a bare `Was $1,499`, `MSRP $…`, or a
    `compare_at_price` reads as a price, and because the high-water mark only ratchets
    up, one such read **poisons the baseline** and makes the next real price look like
    a drop. (Contrary to what an earlier revision of this file claimed, only the
    *two-number* branch takes the lower — single values are trusted as-is.)

  So for anything that matters, **point a `path`/`pattern` override at a clean price
  field** — that is the reliable path; free-prose auto-detection is a convenience, not
  a guarantee. `min_drop_pct` filters the noise floor of near-decoy false drops.
  Negative auto-detected prices are dropped, `$0.00` (100%-off) is kept; `path`/`pattern`
  overrides are trusted as-is (a sub-zero temperature is kept).
- **Unauthenticated denial-of-service via a stranded leg (node v0.0.34) — the
  sharpest limit, disclosed deliberately.** `track` is `@public`, so **any account**
  can queue a black-hole URL with `track({"baseline": true})`, and the next poll leg
  fetches it. On this build a runner job that never returns *strands* its
  continuation: the node emits no failure callback, timers can't dispatch, and
  continuations carry no enforced timeout (there is no chain-side `timeout_blocks`).
  Because only one leg runs at a time, the poll leg *is* the leg that strands — so
  the poll chain's own >60-block stale breaker can never re-arm itself. The effect:
  **a single unauthenticated call deterministically and repeatably halts all
  scanning, drop alerts, `/prices`, and the daily digest** until the **owner**
  manually re-runs `set_telegram` (which clears the lock and strikes/quarantines the
  stranded watch). The quarantine breaker only auto-recovers a strand a *still-live*
  poll leg later observes, and unproven URLs are scanned last so a strand lands after
  the proven watches — mitigations, not a fix. A real fix needs SDK/runtime support
  for continuation expiry or failure callbacks; it is not solvable inside an example.
  **Do not deploy this to an untrusted user base as-is.** Note there is **no built-in
  caller gating**: `track` is unconditionally `@public` and the bot answers any chat,
  so the mitigations are external — keep the actor/bot off untrusted callers, or wait
  for the upstream continuation-expiry fix. (Adding an owner-only `track` flag would
  only cover the `@public` path, not the chat path, so it is left out rather than
  implying a guard it can't fully give.)
- SDK line: this example targets node v0.0.34's SDK (verified against
  `stg-v0.0.34-18ecb33`), where `await runner.http(...)` yields a dict. Devnet's
  newer SDK wraps it in a `RunnerResult` and uses CBOR reply encoding; porting the
  whole gallery to that contract is tracked upstream, not in this example.

  **The failure on a newer node is silent, not loud.** `_is_raw_page`/`_as_json`
  simply fall through a `RunnerResult`, so a JSON-path watch records no price and
  looks identical to a genuine `no_price`; `_after_tg_poll`'s `isinstance(doc, dict)`
  fails the same way, so the command loop goes dead with no error. If you see empty
  prices and an unresponsive bot after a node bump, this is why — check your node
  version first. `tests/test_telegram_chain.py` surfaces it as
  `CodecError: CBOR: trailing bytes`.
