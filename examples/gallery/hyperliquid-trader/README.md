# Hyperliquid Trader — autonomous cross-asset research + live execution

> **Current status:** testnet only. This example needs external services and a
> testnet account, so it is outside the default gallery sweep.

Run the paper demonstration with `./demo.sh`; use the runbook below for the live fleet.

The trader reconciles venue state before and after each order batch.

A three-actor Cowboy L1 agent that researches markets and trades its own ideas,
fully autonomously, across **crypto and US equities** (HIP-3 builder dexes),
reporting to and taking advice from a human over Telegram.

The [`ui/`](ui/) directory contains an actor-hosted, read-only dashboard that
joins bounded research and trader reads in the browser.

- **`hl_research.py`** — six timer loops: **ingest** (RSS → corpus),
  **synthesize** (`runner.llm` → a thesis + ideas from live prices + news +
  track record + operator advice), **validate** (grade theses, retire losers),
  **distill** (compress retired theses into lessons that feed back),
  **discover** (screen both dexes by traded volume → a self-widening universe),
  **brief** (9am/5pm ET Telegram digests via the notifier).
- **`hl_live_exec.py`** — consumes Research's pushed top aligned ideas, opens a
  confidence-weighted book with real IOC orders on Hyperliquid testnet
  (signed via CBSS-released key, off-wire), marks + realizes P&L, and feeds
  its scorecard back to Research.
- **`hl_notify.py`** — webhook-only Telegram bridge with a funded drain. It
  delivers briefs/recaps, answers `/status /book /ideas` from pushed caches,
  and files replies as operator guidance for the next synthesis.
- **`hl_common.py`** — shared CIP-26 library (keccak/msgpack/EIP-712 signing,
  payload + LLM-result helpers). Published once per chain; actors
  `import hl_common` and the chain pins it by code hash at deploy. Deploys ship
  bundled (`tools/bundle.py` inlines it) because timer handlers don't get pins.
- **`hl_paper_exec.py`** — the earlier no-venue paper twin (superseded by
  live exec; kept for its strategy tests).

Prices come from Hyperliquid's one `info` API, fetched per-dex and merged:
core book (coins), `xyz` (US equities). Equity books on testnet are
unattended (no asks, $0–40/day volume) — stocks only prove out on mainnet.

## Full runbook: rebuild env → deploy the fleet

Three actors (trader + research + notifier) come up from one script per stage.
The **only** step that repeats manually every rebuild is the HL key ceremony
(step 3) — CBSS secrets are per-chain and can't be automated or persisted.

**Credentials** — yours, and where they live:

| credential | stored | supplied on rebuild |
|---|---|---|
| Anthropic (LLM) key | `~/.anthropic_key_canyon` | auto — passed to `up` |
| Telegram bot token | local source file + **CBSS, per-chain** | encrypted from `~/.telegram_bot_token` during stage 2 |
| Telegram chat ID | the deploy command (a constant) | auto |
| Telegram webhook secret | your environment (`TG_WEBHOOK_SECRET`) | required when wiring Telegram |
| **HL agent key** | **CBSS, per-chain** (you hold the original) | **manual, step 3** |

### 0. Auth (once per ~8h SSO session)
```bash
aws sso login --profile cowboy-river
export AWS_PROFILE=cowboy-river
```
One-time-per-machine, if the key files don't exist yet: copy the value to the
clipboard, then `pbpaste > ~/.anthropic_key_canyon && chmod 600 ~/.anthropic_key_canyon`
(same for `~/.telegram_bot_token`). Never commit a key or put one in `configs/`.

### 1. Build the box
```bash
cd <repo-root>/outpost
./cowboy-env down cal                                                     # skip if none up
./cowboy-env up cal -e anthropic_api_key="$(cat ~/.anthropic_key_canyon)"
./cowboy-env endpoints cal                                                # copy the host IP
export IP=<host-ip>                                                       # rest is copy-paste
```
`up` **must** carry the key or the env silently falls back to the local
llama.cpp model. `configs/cal.json` pins versions + the model
(`agent_local_model_id` → `LLM_MODEL`); currently Haiku 4.5 — Sonnet 5 needs
the runner parser fix on `fix/runner-llm-executor` (its replies lead with a
`thinking` block the shipped runner can't parse).

### 2. Stage 1 — wallet, faucet, CBSS DKG
```bash
cd <repo-root>/cowboy/examples/gallery/hyperliquid-trader
RPC=http://$IP:4000 ./tools/setup_live.sh          # stops at the key ceremony
```

### 3. The HL key — your one manual step (copy the HL agent key first)
```bash
pbpaste > /tmp/hlkey.txt
CBSS_INGEST_ENDPOINT=http://$IP:9602 cowboy secrets \
  --private-key ./tools/deploykey --rpc-url http://$IP:4000 \
  set HL_AGENT_KEY --value-file /tmp/hlkey.txt
rm -P /tmp/hlkey.txt
```
One clean attempt per chain — a failed CBSS set wedges the dispatcher. The
prereqs (5 proxies, DKG, release key) are all green by the time stage 1 stops.

### 4. Stage 2 — full fleet (trader + research + notifier + Telegram, wired)
```bash
export TG_WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
TG_TOKEN_FILE="$HOME/.telegram_bot_token" TG_CHAT=8933490887 \
  CLI=<version-matched-cowboy> STAGE=deploy \
  RPC=https://rpc.canyon.cowboylabs.net ./tools/setup_live.sh
```
Bundles both actors (CIP-26 timer safety), publishes `hl_common`, deploys +
funds all three, stores the Telegram token as a CBSS secret allowlisted to the
notifier, sets both CBSS release policies, wires every link, and starts the
loops. A missing token file or `TG_CHAT` leaves Telegram dark. The bot token is
never placed in actor storage or an actor-execute transaction.

`tools/setup_live.sh`, `tools/demo.sh`, and `tools/wire_telegram.sh` require
`TG_WEBHOOK_SECRET` when wiring Telegram; setup and wiring calculate its hash
locally and install only that hash with `set_webhook_secret` before
`set_telegram`. Keep the raw secret only for
Telegram's `setWebhook` `secret_token` parameter so requests carry the matching
`X-Telegram-Bot-Api-Secret-Token` header. An unconfigured webhook returns 401
and emits `notify.webhook_unconfigured` once; wrong or missing headers also
return 401 before storing an update. To configure an existing notifier:

```bash
COWBOY_RPC_URL="https://rpc.canyon.cowboylabs.net" cowboy actor execute \
  --actor "$(cat tools/notify.txt)" \
  --handler set_webhook_secret \
  --payload-text "$(python3 -c 'import hashlib,json,os; print(json.dumps({"secret_hash": hashlib.sha256(os.environ["TG_WEBHOOK_SECRET"].encode()).hexdigest()}))')" \
  --private-key tools/deploykey
```

### 5. Watch it
```bash
RPC=http://$IP:4000 python3 tools/watch_pipeline.py    # live dashboard
RPC=http://$IP:4000 python3 tools/inspect.py           # full untruncated state
```
Both auto-read the current actor addresses from `tools/*.txt`. Or just text the
bot `/status`, `/book`, `/ideas`; answers use the latest pushed cache.

The notifier is **webhook-only**. On Canyon the scripts lower and install its
CIP-15 routes, wait for
`https://0x<actor>.canyon.cowboylabs.net/telegram/health`, and register that
actor URL with Telegram using the configured secret. Each authenticated command or
advice update arms one actor-funded drain at the next block. A pending flag
coalesces bursts. Each fire starts one reply job or publishes one guidance
snapshot; remaining work schedules the next fire. With the default 20-message
outbox and no new input, jobs are submitted within 20 blocks and guidance by
the following block. An isolated reply job or advice snapshot starts at height
+1; actual Telegram delivery also depends on runner completion. Clear-guidance,
authenticated research notifications, and brief completion wake the same drain.
Outbound Telegram API calls resolve `${TELEGRAM_BOT_TOKEN}` inside the runner
from CBSS; the token is not readable from actor state.

There is no `getUpdates`, polling flag, notifier `start`, or watchdog fallback.
This is deliberate: Canyon's native gateway provides ingress, and a perpetual
polling actor creates unnecessary runner and deferred-transaction load.

### Teardown / stop spend
```bash
cd <repo-root>/outpost && ./cowboy-env down cal        # ttl_hours auto-reaps anyway
```

River testnet chains currently hit two node-level faults — a runner heartbeat
nonce wedge and a restart-resistant consensus grind ~6–7h after genesis (both
reported upstream). Neither is an actor bug; plan on rebuilds and keep actor
state expendable. (Local ops crutches for these are exactly that — kept off the
box's git and out of this example.)

## Notes

- **CIP-26 pins are NOT injected on timer-fired handlers** (verified live on
  node `v0.0.34-caa201d`: deploy/execute/read paths import `hl_common` fine,
  but a fired timer's handler silently no-ops and never re-arms). Deploys
  therefore ship the self-contained output of `tools/bundle.py`, which
  inlines `hl_common` in place of the import. Keep the library as the
  canonical source; never deploy the importing form until the node loads
  pins on the timer path too.
- `setup_live.sh` re-publishes `hl_common` before each deploy; identical bytes
  report "skipped", which is fine.
- Deploy/execute at `cycles`/`cells` ≤ 40M (`LANE_SYSTEM_CYCLES`), or the node
  rejects the tx.
- Views are read-only via `POST /actor/read`.
- Continuation limits: ≤ 8 awaits per continuation, 64 KiB state cap — null
  raw HTTP bodies before later awaits; set `timeout_blocks` on every
  continuation (default 0 = never expires = orphan risk).

## Access budgets

These budgets target node devnet `04c9f283` (2026-09-08). At 1,350,000 ppm,
a signed-client stamp of 512 buys 379 structural units; the User ceiling is
1,024 charged units. `/actor/read` is capped at 512 structural units. Timers
and callback envelopes are already structural; do not scale them again.

- `TIMER_ACCESS_LIMIT = 322` is **160 own + 162 child**, from node
  `types/src/constants.rs:1880-1881`. A general child reserves **160 execution
  + 2 pre-engine gate = 162** (`:2953-2954`). Default-160 timers cannot fund it.
- Runner callbacks have a fixed **152 own + 162 child = 314** tier, independent
  of the runner's stamp (`constants.rs:1744,1753-1754`,
  `execution/src/execution/transaction.rs:1068-1108`). Increasing that stamp
  cannot enlarge a resume. Own work includes the callback's own two-unit
  pre-engine gate (`storage/src/speculative.rs:3882-3905,4205`).

The earlier September 4 table reserved 160 per child and sized callbacks at
379. Correcting only those constants would turn its four callback estimates
into 308 / 302 / 304 / 294–303, leaving 6 / 12 / 10 / 11–20 units at 314.
Those are approximate historical counts, not worst-case margins. Recounting
the current bodies, the callback gate, and native hashing exposed the heavier
branches below and led to the next-block timer handoffs.

| Path | Estimated structural | Budget | Margin |
|---|---:|---:|---:|
| research `init` / `start` | 151 / 173 | 379 | 228 / 206 |
| research `ingest_now` / cached `synthesize_now` | ~294 / ~293 | 379 | ~85 / ~86 |
| research validate/discover/distill now | ~289–292 | 379 | ~87–90 |
| research `brief_now` | ~282 | 379 | ~97 |
| research ingest/synth timer | ~305 / ~305 | 322 | ~17 / ~17 |
| research validate/discover/distill timer | ~300 / ~300 / ~303 | 322 | ~22 / ~22 / ~19 |
| research brief timer | ~295 | 322 | ~27 |
| live `apply_ideas` / cached `rebalance` | ~298 / ~304 | 379 | ~81 / ~75 |
| live rebalance timer, normal / lost-work recovery | ~315 / ~317 | 322 | ~7 / ~5 |
| live mark/universe timer | ~302 / ~301 | 322 | ~20 / ~21 |
| paper cached rebalance / mark timer | ~299 / ~294 | 322 | ~23 / ~28 |
| synthesis final + ideas snapshot | 146–147 + 162 = **308–309** | 314 | **5–6** |
| trader reconcile final + feedback snapshot | 146 + 162 = **308** | 314 | **6** |
| trader mark final, no exits / three fills / mixed fills and rejections | **148 / 150 / ≤152**, no child | 314 | **≥162** |
| paper apply final + trader snapshot | 149 + 162 = **311** | 314 | **3** |
| paper mark final with exits + notifier snapshot | 150 + 162 = **312** | 314 | **2** |
| live apply final, no orders / mixed outcomes | **145 / 152**, no child | 314 | **169 / 162** |
| mark exit-dispatch final | **137–138**, no child | 314 | **176–177** |
| next-block command snapshot / feedback / reconciliation timer | **~290 / ~288 / ~284** | 322 | **~32 / ~34 / ~38** |
| next-block exit-job timer, one signed slot | **~298** | 322 | **~24** |
| apply price/plan resume + first signed slot | **≤301** including child | 314 | **≥13** |
| later signed-slot resume | **~293–295** including child | 314 | **~19–21** |
| webhook drain timer, reply + successor / guidance | 141 / 120 + 162 = **303 / 282** | 322 | **19 / 40** |
| authenticated snapshot receiver / notification with drain schedule | ~115–129 including its gate | 162 | ~33–47 |
| UI config / research / lessons / trader read | ~112 / ~400 / ~230 / ~400 | 512 | ~400 / ~112 / ~282 / ~112 |

Counting follows the September 4 evaluation's host-crossing method: 109 for
dispatch/setup and the top frame; one per state read/write, context read,
native Keccak call or unsubscribed emit; two per message send; eight per first
timer schedule (7 schedule rows + 1 TimerConfig read). The current SDK's
`continuation.py:174-224,302-337,364-402,436-450` adds six for a final resume
(load/timeout/delete/count), seven for first-leg CID/save work plus two to send,
or two to load and three to re-save a middle leg plus two to send. A callback
final therefore starts at **2 + 109 + 6 = 117 own units**. These are CPython
source tallies, **not node receipts**; retain the evaluation's ±10% uncertainty
and measure on devnet before relying on a narrow margin.

Callback derivations, counting the configured, successful push paths:

| Final leg | Own-work derivation after the change | Decision |
|---|---|---|
| research `_ingest_feed` final → next feed | 117 + 4 reads + 3 writes + 1 context + 1 emit + 7 first-leg bookkeeping + 2 send = **135** | **297/314**, 17 margin. With no next feed but candidates to score: 8 reads + 2 writes instead, **138 + 162 = 300**, 14 margin. (`hl_research.py:1125-1158`.) |
| `_on_synthesis` → `_push_synthesis_snapshot` | 117 + 19 reads + 6 writes + 1 context + 1 emit + 2 send = **146**; an empty ideas result adds 1 | Keep the child; reuse three previously repeated reads in synthesis/stats. Worst modeled total **309 ≤ 314**. |
| `_on_post_order_reconcile` → `_push_trader_snapshot` | 117 + 18 reads + 5 writes + 2 contexts + 2 emits + 2 send = **146** | Keep the child: **308 ≤ 314**. Position iteration adds no host calls. |
| `_mark_route` / `_on_mark` | No-exit final: 117 + 16 reads + 3 writes + 2 contexts + 2 emits + 8 timer = **148**. Three filled exits: 117 + 10 reads + 8 writes + 5 contexts + 2 emits + 8 timer = **150**. | Before the split these paths were **318 / 320** including a child; mixed exits can reach **322**. Schedule `command_snapshot_tick` at height + 1, with no message child on this final. |
| paper `_on_apply` → `_push_trader_snapshot` | 117 + 20 reads + 8 writes + 1 context + 1 emit + 2 send = **149** | **311/314**, 3 margin; the child requires `research_addr`. Portfolio iteration adds no host crossings. (`hl_paper_exec.py:380-454`.) |
| paper `_on_mark` → `_push_command_snapshot` | With risk exits: 117 + 22 reads + 6 writes + 1 context + 2 emits + 2 send = **150**. Without exits: **144**. | The child is sent only when `notify_addr` is wired. With exits, **312 ≤ 314**, leaving **2** units of modeled margin; without exits, **306** (8 margin). An unwired exit final uses **135**, with no child. |
| notifier `_after_notify`, single-message final | **117** for a reply. Brief rotation with cleared advice adds 3 reads + 3 writes + 1 context + 1 emit + 8 timer = **133**. | No child. A non-empty queue tail is rejected; webhook brief completion arms a drain to publish the cleared guidance window. |
| research no-candidate ingest / scored / validate / discover / distill final | **132 / 134 / 129 / 125 / 127** | No child; respectively 182 / 180 / 185 / 189 / 187 margin at 314. Configured successful arms; missing input or empty results return earlier. |
| live `_on_apply` / prices / universe final | **145–152 / 121 / 120** | No child; apply uses the follow-up timer described below. |

The rows above cover every child-funding **final**: ingest-to-feed/scoring,
synthesis, live reconciliation, and paper apply/mark.
Middle resumes that issue another HTTP/LLM job use 2 continuation reads + 3
re-save crossings + 2 send, plus their body crossings and one 162-unit child;
these are separate from the final-resume base of 117.

Timer body derivations use **110 = 109 dispatch/frame + 1 permission sender
read** (SDK `permissions.py:397-406`). The table counts code from the named
tick through its first await or direct send. `R`, `W`, `C`, `E`, and `K` cost
one each: storage read/write, context read, unsubscribed emit, and native
Keccak. `T=8` is one timer schedule, `B=7` is first-leg CID/save bookkeeping,
and `S=2` is send work; the child reservation is shown separately. Ordinary
configured arms are shown, with heavier recovery arms explicit. Stopped,
pending-work, and missing-input returns have no child and lower
cost. Updated timer totals replace the earlier rounded summary estimates.

| Timer body | Own-work derivation | Own + child / tier | Margin |
|---|---|---:|---:|
| research `ingest_tick` | 110 + 7R + 6W + 4C + E + T + B + S = **145** | 145 + 162 = **307/322** | **15** |
| research `synth_tick` | 110 + 10R + 3W + 5C + T + B + S = **145** | 145 + 162 = **307/322** | **15** |
| research lost-work recovery, worst case | ingest/synth ordinary arm + E = **146** | 146 + 162 = **308/322** | **14** |
| research `validate_tick` | 110 + 6R + 3W + 4C + T + B + S = **140** | 140 + 162 = **302/322** | **20** |
| research `discover_tick` | 110 + 6R + 3W + 4C + T + B + S = **140** | 140 + 162 = **302/322** | **20** |
| research `distill_tick` | 110 + 9R + 3W + 4C + T + B + S = **143** | 143 + 162 = **305/322** | **17** |
| research `brief_tick`, due | 110 + 8R + W + 3C + E + T + S = **133** | 133 + 162 = **295/322** | **27** |
| live `rebalance_tick` | 110 + 12R + 7W + 6C + E + T + B + S = **153** | 153 + 162 = **315/322** | **7** |
| live rebalance, lost-work recovery | 110 + 12R + 8W + 6C + 2E + T + B + S = **155** | 155 + 162 = **317/322** | **5** |
| live `mark_tick` | 110 + 6R + 3W + 4C + T + B + S = **140** | 140 + 162 = **302/322** | **20** |
| live mark, lost-work recovery | 110 + 6R + 3W + 4C + E + T + B + S = **141** | 141 + 162 = **303/322** | **19** |
| live `universe_tick` | 110 + 5R + 3W + 4C + T + B + S = **139** | 139 + 162 = **301/322** | **21** |
| live universe, lost-work recovery | 110 + 5R + 3W + 4C + E + T + B + S = **140** | 140 + 162 = **302/322** | **20** |
| paper `rebalance_tick` | 110 + 6R + W + 2C + E + T + B + S = **137** | 137 + 162 = **299/322** | **23** |
| paper `mark_tick` | 110 + 3R + 2C + T + B + S = **132** | 132 + 162 = **294/322** | **28** |
| live `command_snapshot_tick` | 110 + 16R + S = **128** | 128 + 162 = **290/322** | **32** |
| live `apply_followup_tick`, feedback | 110 + 14R + S = **126** | 126 + 162 = **288/322** | **34** |
| live `apply_followup_tick`, reconciliation | 110 + 3R + B + S = **122** | 122 + 162 = **284/322** | **38** |
| live `mark_exits_tick`, one signed slot | 110 + 4R + 2W + C + 10K + B + S = **136** | 136 + 162 = **298/322** | **24** |
| notifier `webhook_drain_tick`, reply + successor | 110 + 10R + 3W + C + T + B + S = **141** | 141 + 162 = **303/322** | **19** |
| webhook drain, last reply | 110 + 8R + 2W + B + S = **129** | 129 + 162 = **291/322** | **31** |
| webhook drain, guidance | 110 + 6R + 2W + S = **120** | 120 + 162 = **282/322** | **40** |
| webhook drain, empty | 110 + 4R + W = **115** | 115 + 0 = **115/322** | **207** |

The webhook reply-plus-more arm omits one dirty-flag read (**302/322**);
the last reply with dirty guidance is the heavier **303/322** row. A missing
bot/chat retains the outbox until configuration wakes the drain; an unwired
Research retains dirty guidance until `set_sources` or another input wakes it.
Only configured successful sends reserve the child shown above.

`clear_guidance` marks the snapshot dirty and arms the webhook drain, which
publishes the empty snapshot to Research on its next funded fire.

Adjacent execution legs needed the same treatment. `_on_apply` with both
rejected and unknown order outcomes used **153 + 162 = 315** when it started
reconciliation; its no-order feedback path used exactly **314**. Both now
schedule `apply_followup_tick`, retaining the generation fence and pending flag
until reconciliation completes. A mark that finds exits schedules
`mark_exits_tick`. Previously three signed slots were prepared before the first
await (including 28 native Keccak crossings with cold resolution); that exit
callback totaled about **342**. `_apply` and `_mark_exits` now prepare each
slot immediately before its own await, so a leg hashes and reserves only one
order. The await counts remain eight and three respectively.

Every child-producing timer uses the named 322 constant: the 11 live-fleet
loops, the two paper loops, the three live follow-ups, and the webhook drain. The rebalance
recovery fire remains a measurement target with about **five to seven units**
of modeled margin; its lost-work emit and guard reset account for the worst
branch. That timer was documented rather than further restructured.

Timer funding follows the actual caller. Natural fires are created with the
actor as sender (`storage/src/speculative.rs:5340-5352`). Next-block callback
follow-ups and the notifier drain omit `fee_payer`, explicitly selecting actor
funding even when the callback sender has runner provenance. The notifier
snapshot receiver permits authenticated actor messages; legacy text messages
require the actor itself as sender. Keep the deployed actors funded (the setup
scripts already fund them).

All recurring research, live, and paper tick entrypoints, plus the webhook
drain and live follow-ups, are SELF-only;
external callers, including the owner, cannot use a tick to fork a funded loop.
The owner starts the research and trader loops through `start` and requests
immediate work through their existing owner handlers. This permission change adds no host crossings on a
natural timer fire: the SDK already checks the sender against the actor address
before either PUBLIC or SELF permissions (`pvm/Lib/cowboy_sdk/permissions.py:397-408`).
The SELF restriction introduces no scheduling-state read. The new webhook
pending flag and scheduling work are counted separately in the drain rows.

The actor graph is push-based at its budget boundaries:

- The final synthesis callback sends current ideas, research stats, and the
  configured notifier address to the trader. The authenticated trader receiver
  stores them; rebalance handlers read that cache and never call Research. Live
  and paper execution fail closed after 1,800 blocks without a fresh snapshot,
  while their independent mark/risk loops continue managing existing positions.
- The live post-order reconciliation callback sends scorecard, unfillable, and
  P&L back to Research (the no-order follow-up timer and paper apply leg do the same).
  Research reads these cached values during synthesis and brief composition.
- The next-block command snapshot timer sends the notifier its cached research
  data plus the latest portfolio and P&L. Telegram commands read those local caches.
- The notifier pushes its bounded guidance snapshot from a webhook drain fire.
  Sending a reply leaves guidance dirty for a later drain so an invocation
  never reserves two children.
- Research sends briefs/recaps to the notifier with `send_message`; its
  authenticated receiver appends them to the same paced outbox. Direct
  `enqueue` is owner-only. Authenticated, deduplicated webhook
  commands join the outbox inline; their drain timer later sends replies.

Measure before relying on these estimates. On a devnet configured with an
access scalar of 1,350,000 ppm, measure owner entrypoints with `cowboy cost` and
inspect `accesses_used` in their receipts. Then pass
`--access-limit <measured + margin>` to `cowboy actor execute`; the chosen limit
must never exceed the 1,024 User ceiling. The one-step estimate-then-sign flow
arrives with the client SDK round and is not a flag in the node CLI. For natural
timers, read `accesses_used` from each fire receipt after scheduling at 322,
especially `rebalance_tick`.

Run the deterministic and budget guards with:

```bash
python3 -m pytest -q \
  examples/gallery/hyperliquid-trader/tests \
  examples/gallery/hyperliquid-trader/ui/tests
node --check examples/gallery/hyperliquid-trader/ui/hl-ui/app.js
```

The same Python command runs on every pull request in
`.github/workflows/hyperliquid-trader-tests.yml`, with pinned msgpack and the
official Hyperliquid SDK so the digest/signature differential tests cannot
silently skip. The workflow also builds and byte-compiles the self-contained
research and execution bundles used by the documented deployment path.

## Production notes (from the 2026-07-23 live run)

Demo-vs-real gaps, in the order they'll matter:

- **Rebalance cadence.** Every round trip pays two spread-crossings (~0.1–1%
  on testnet books), so holding periods must be long enough for the thesis's
  expected move to clear ~2× spread. Realistic tiers: ingest 15–30 min,
  synthesis 4–12 h, **rebalance daily or synthesis-triggered**, validate/
  distill days-to-weekly. Marking/stops stay FAST — fast eyes, slow hands.
  All `*_interval_blocks` are init-payload params already; add a
  `set_intervals` owner handler so cadence is tunable without redeploy.
- **Universe discovery.** Today's list is hand-curated from a one-time book
  inspection. Production: screen the venue `meta` universe mechanically
  (spread < X bps, depth > $Y, volume > 0), refreshed hourly — books go
  ask-only/bid-only as makers come and go. Then flip discovery: let research
  propose ANY symbol from the news and have execution enforce tradeability
  at the order stage (membership + liquidity check), instead of constraining
  the model's vocabulary up front.
- **Equity data sources.** RSS headlines are thin for stock selection; add
  earnings dates / filings / fundamentals via the same `runner.http` path.
- **Testnet equity liquidity is one-way.** Longs can fill (asks exist
  sporadically) but exits face empty bid sides — positions become roach
  motels. Crypto books fill at 2–6 bps. Stocks only prove out on mainnet.
- **Known infra items.** Runner heartbeat nonce runaway (45s interval
  mitigates; real fix = resync on nonce_too_high rejection). CIP-26 pins not
  injected on timer-fired handlers (deploys ship `tools/bundle.py` output
  until fixed node-side). Anthropic thinking-block parsing + LLM client
  timeout fixed on runner branch `fix/runner-llm-executor` — Sonnet 5 is
  blocked until that ships; Haiku 4.5 works with the deployed runner.
