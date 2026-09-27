# Hyperliquid Autonomous Trading System — Technical Reference

An autonomous macro trading agent implemented as three on-chain actors on Cowboy L1. It ingests financial news, forms and reality-tests macro theses, sizes and executes real perpetual-futures orders on Hyperliquid using a threshold-encrypted API key, marks its own book, and distills durable lessons from its realized results.

No server orchestrates it. The actors schedule themselves with on-chain timers and reach the outside world through runner continuations.

---

## 1. Architecture

### 1.1 Components

| Component | Implementation | Responsibility |
|---|---|---|
| Research actor | `hl_research.py` | News ingestion, thesis synthesis, validation, lesson distillation, universe discovery |
| Trader actor | `hl_live_exec.py` | Order planning, execution, position tracking, mark-to-market |
| Notifier actor | `hl_notify.py` | Telegram bridge — outbound briefs, inbound operator guidance |
| Cowboy L1 | — | Consensus, actor state, timer scheduling, job dispatch |
| CBSS | — | Threshold-encrypted custody of the exchange API key |
| Runners | — | Off-chain execution of HTTP and LLM calls on the actors' behalf |

### 1.2 Execution model

Actors are Python running inside the PVM with state committed on-chain. Two mechanisms drive them:

**Inline timers.** A handler schedules its own successor. Timers fire *during block production*, so handler wall time is charged against the block deadline.

**Continuations.** Any external call is `await runner.<thing>(...)`. The actor suspends, the call is dispatched as a job to a runner, and the actor resumes in a generated `__resume` segment when the result returns. CIP-6 caps a continuation at **8 awaits**.

The consequence that shapes everything: *timers are the scarce resource, and external I/O is asynchronous and failure-prone.*

---

## 2. Research actor

### 2.1 Loops

| Loop | Default cadence | Function |
|---|---|---|
| `ingest` | 149 blocks | Pull RSS feeds, dedupe, LLM-score for relevance, retain the best |
| `synth` | 599 blocks | Generate one thesis + up to 8 ideas from news, prices and memory |
| `validate` | 3593 blocks | Mark open theses against baseline, retire losers, archive them |
| `distill` | 7207 blocks | Compress archived theses into durable lessons |
| `discover` | 997–2003 blocks | Refresh the tradeable universe from exchange metadata |
| `brief` | 449 blocks | Emit morning/evening summaries to Telegram |

### 2.2 Ingest pipeline

1. `plan_ingest` selects a rotating batch of feeds (batch size 6) from the configured list.
2. Each feed is fetched via `runner.http`, parsed as RSS/Atom, and stripped to title + summary.
3. Items are deduplicated against a rolling `seen` set of URL keys (capped at 500).
4. Candidates go to an LLM relevance scorer with the standing mandate and asset universe as context.
5. Items scoring at or above `min_score` (7) are kept, sorted by score, truncated to `max_keep` (8), and prepended to a rolling `items` window (capped at `max_items`, 30).
6. An aggregate is appended to the **sentiment tape** — see §5.4.

### 2.3 Synthesis

Fetches current prices across all three dex groups (core, `xyz` equities, `vntl` pre-IPO), assembles a context object, and issues a single LLM call constrained to a strict JSON schema.

The context contains:

| Field | Meaning |
|---|---|
| `items` | Recent scored news |
| `assets_of_interest` | Tradeable universe, minus known-unfillable assets |
| `existing_theses` | Active thesis statements, to force differentiation |
| `mandate` | Standing operator mandate |
| `track_record` | Per-thesis outcomes |
| `traded_performance` | Realized P&L from the trader's scorecard — weighted most heavily |
| `long_term_record` | Per-asset/side aggregates with `n`, `confident`, `avg_alpha_pct` |
| `past_cases` | Prior economic contexts paired with outcomes |
| `lessons` | Distilled principles |
| `sentiment_trend` | Multi-horizon sentiment deltas |
| `unfillable` | Assets the venue repeatedly refuses |
| `operator_guidance` | Human advice from Telegram |

Output: an `economic_context` summary, one thesis (`statement`, `long[]`, `short[]`, `conviction`, `rationale`), and ideas (`asset`, `side`, `confidence`, `theme`, `rationale`).

A new thesis snapshots the **entire universe's prices** as its `baseline` and is stored with `created_block`. Near-duplicate theses — measured by Jaccard overlap ≥ 0.75 on *both* the long and short baskets — bump the incumbent's conviction instead of consuming one of the `max_theses` (12) slots.

### 2.4 Validation and retirement

For each thesis older than `min_age_blocks` (3000):

```
thesis_return = mean over baseline assets of  direction × (current − base) / base
market_return = mean over baseline assets of            (current − base) / base
net_exposure  = (#long − #short) / (#long + #short)
alpha         = thesis_return − net_exposure × market_return
```

Retirement is on **raw return** below `min_return_pct` — a thesis that loses money is retired even if it beat the market. Learning is on **alpha**, so selection is judged independently of market drift.

### 2.5 Distillation

Archived theses (kept: 20) are compressed by an LLM into lessons of the form *market condition → what tended to work*, which feed forward into all future synthesis.

---

## 3. Trader actor

### 3.1 Loops

| Loop | Default cadence | Function |
|---|---|---|
| `rebalance` | 601 blocks | Read pushed ideas, plan the diff, execute orders |
| `mark` | 293 blocks | Refresh prices, recompute unrealized P&L |
| `universe` | 4001 blocks | Refresh asset metadata cache |

### 3.2 Configuration

| Key | Default | Meaning |
|---|---|---|
| `budget` | 1000.0 | Notional deployable |
| `max_positions` | 10 | Concurrent position cap |
| `min_confidence` | 60 | Ideas below this are ignored |
| `min_hold_blocks` | 1200 | Never close a position younger than this |
| `flip_margin` | 15 | Conviction points required to reverse a held position |
| `slip_bps` | 100 | Aggressiveness of the limit price |
| `stop_loss_pct` | 0.03 | Required per-position stop; `start()` refuses zero |
| `take_profit_pct` | 0.0 | Optional per-position take-profit |
| `max_cumulative_loss_pct` | 0.10 | Halt new exposure and reduce the book after this strategy loss |
| `max_exit_price_retries` | 3 | Immediate fresh-price retries before a visible risk halt |
| `max_poll_wait_blocks` | 950 | Guard before a stuck loop may restart |
| `secret_account` | — | CBSS account holding the signing key |

### 3.3 Rebalance sequence

1. Call `research.get_ideas`, filter by `min_confidence`.
2. Fetch current prices (2 awaits — core and equities).
3. Read the universe cache from storage (deliberately *not* fetched inline; see §6.3).
4. `plan_orders` diffs the target book against holdings, producing opens, closes and flips.
5. Each order is signed and submitted through a runner (3 awaits).
6. Responses are parsed, fills applied to the book, and execution outcomes recorded.

### 3.4 Order planning

`plan_orders` is pure — no I/O, no storage — which makes it directly testable. It applies two dampers, because synthesis re-opines every cycle and an LLM macro call on a rolling news window is not stable between runs:

- **`min_hold_blocks`** — never close a position younger than the floor.
- **`flip_margin`** — only reverse if the opposing conviction beats the held one by the margin.

Without these the book whipsaws: opened, dropped from the next idea list, closed, re-opened on the opposite side minutes later.

Every symbol that is *not* ordered is recorded with a reason: `already_held`, `not_in_universe`, `no_price`, `below_min_notional`, `size_rounds_to_zero`, `same_side_as_thesis`, `younger_than_min_hold`, `flip_edge_below_margin`, `zero_qty`, `venue_refuses`, plus `unclosable_not_in_universe` / `unclosable_no_price`.

The `unclosable_*` reasons are the ones to alarm on: they mean real exposure with no exit path. `max_positions` is enforced against reconciled venue holdings, not merely against the current idea list. A planned close can release a slot, but the staged dispatcher will not send the replacement open until the reduce-only fill confirms the position is gone. Stop exits are ordered by breach severity before the three-order execution cap is applied.

Live exits require fresh marks. Missing marks retry immediately up to
`max_exit_price_retries`; exhaustion emits `live.risk_halted` and blocks new
exposure while the regular mark loop remains active. A fresh complete mark
clears that transient halt. A cumulative-loss halt requires the owner to call
`clear_risk_halt` with `{\"confirm\": true}` after reviewing the account; that
acknowledgement records the current strategy PnL as the next drawdown baseline.

### 3.5 Order execution and signing

The exchange key is never in plaintext on-chain or in the actor. The flow:

1. The actor builds an unsigned action and a nonce, with signature fields as placeholders (`__SIG_R__`, `__SIG_S__`, `__SIG_V__`).
2. It declares a CBSS secret reference (`[secret_account, "HL_AGENT_KEY"]`).
3. The job is dispatched to a runner. The runner requests the secret; CBSS proxies each return a partial decryption; a 4-of-5 quorum reconstructs the key **inside the runner**.
4. The runner signs, substitutes the real signature, and posts to Hyperliquid.
5. Only the response returns on-chain.

Nonces come from the consensus block clock with a per-block sequence counter, so ordering is deterministic across replays.

### 3.6 Response handling

Hyperliquid signals refusal two different ways, and both must be distinguished from a genuine zero fill:

| Shape | Example | Meaning |
|---|---|---|
| `response.data.statuses[].error` | *"could not immediately match against any resting orders"* | No liquidity at the limit |
| top-level `status: "err"` | *"Invalid nonce: duplicate nonce"* | Request rejected outright — arrives with no `statuses` array |

Both are recorded per symbol in `exec_feasibility` (`sent`, `filled`, `rejected`, `last_reason`). An asset refused on 3+ attempts with zero fills is treated as **unfillable**: new opens are suppressed and it is reported to research. Closes are never suppressed — otherwise an unfillable asset becomes a permanently stuck position.

---

## 4. Infrastructure

### 4.1 CBSS

Threshold encryption across 5 proxies, 4-of-5 quorum. The key is sealed once in a ceremony and released per-signature under a policy binding it to the authorized runner. AAD binds chain id, secret id, version, wrap epoch and master public key, so a release is valid only for the chain and epoch it was issued against.

**Ceremony ordering matters:** CBSS component versions must be correct *before* the ceremony, and no component may be moved afterwards — doing so orphans the DKG shares, and re-requesting account DKG is a no-op once a release key exists.

### 4.2 Runners

Runners claim dispatched jobs, execute HTTP/LLM calls, and return results. They must heartbeat on-chain to remain in the dispatch set; going stale past `HEARTBEAT_TIMEOUT_BLOCKS` (100) removes them from job assignment.

This is a hard dependency: **if all runners drop out, every actor continuation stalls**, and the book freezes while the chain keeps producing blocks perfectly normally.

### 4.3 Venue structure

Hyperliquid exposes core perps (low asset IDs) and builder-deployed dexes (`xyz` equities, `vntl` pre-IPO, IDs in the 750xxx range). Core perps have real depth. Builder-deployed listings frequently have **no resting liquidity at all** — orders priced through the mark are still refused — which is why execution feasibility is tracked and fed back rather than assumed.

---

## 5. Learning mechanisms

### 5.1 Realized-performance feedback

`get_scorecard` returns open and closed positions with returns, plus an `unfillable` list. The trader pushes the same snapshot to Research as `traded_performance`, and the prompt instructs the model to weight it above all other evidence.

### 5.2 Alpha attribution

Raw return conflates skill with market direction. At 20–60 minute holding periods, drift dominates the decision entirely. Alpha subtracts the benchmark move scaled by net exposure:

| Scenario | Raw | Market | Alpha |
|---|---|---|---|
| Long-only, market +3%, picks +3% | +3.00% | +3.00% | **0.00%** |
| Long-only, market +3%, picks +6% | +6.00% | +3.00% | **+3.00%** |
| Long-only, market −3%, picks −1% | −1.00% | −3.50% | **+2.50%** |
| Balanced L/S, market +5% | +1.00% | +5.50% | **+1.00%** |

Row 3 is the important one: a genuinely good call that lost money. Under raw return the system learns to avoid it.

### 5.3 Confidence gating

Per-asset rows carry `n` and a `confident` flag (threshold: 5 observations). Thin rows are marked as weak evidence so the model does not build firm causal rules from single outcomes.

### 5.4 Sentiment tape

The `items` window is a rolling snapshot capped by *count*, so its time depth varies with news volume and it cannot express a trend. The tape solves this by aggregating at write time during ingest — `{block, n, mean, kept, themes{}}`, capped at 240 entries — and deriving:

- **fast / medium / slow deltas** over 6 / 24 / 96 entries
- **theme drift** — which narratives are gaining or fading

Horizons are counted in *ingest cycles*, not blocks, so they remain meaningful as block rate changes. The aggregation is O(1) per ingest and adds no timers.

---

## 6. Operational constraints

### 6.1 Timer scheduling

Inline timers execute during block production. Two loops coming due in the same block stack their handler cost inside one block deadline, and a decayed chain has less headroom to absorb it.

**All cadences are prime and pairwise coprime.** Composite cadences like 1200/1800/3600 collide every 3600 blocks — three handlers in one block. Any new loop must pick a prime distinct from every existing one.

### 6.2 Block-denominated intervals

Every cadence, timeout and guard is expressed in blocks, so all of them stretch as block rate falls:

| Interval | At 0.76 blk/s | At 0.27 blk/s |
|---|---|---|
| rebalance (1201) | 26 min | 74 min |
| apply timeout (900) | 20 min | 55 min |
| poll guard (950) | 21 min | 59 min |
| universe (4001) | 88 min | 247 min |

A stuck loop therefore costs progressively more as a run ages.

### 6.3 Continuation budget

CIP-6 caps a continuation at 8 awaits, and each await is a full chain round-trip. `_apply` sat exactly at that ceiling and could not finish; moving universe refresh to its own loop brought it to 6. Near-static data belongs in storage, refreshed by a dedicated loop, not fetched inline.

Conditional awaits are unsafe: the FSM compiler counts awaits inside an `if` but does not split on them, so a conditional await passes validation and then silently never becomes a resume segment.

### 6.4 Destructive operations

`init` is the only configuration setter and it **wipes the book**. Cadences cannot be changed on a live actor — changing them means a redeploy and a fresh portfolio.

### 6.5 Failure modes

The recurring theme in development: failures are silent by default. Each of these once looked identical to success.

| Failure | Symptom | Detection |
|---|---|---|
| Refused order | Fewer positions than planned | `live.orders_rejected` |
| Refused exit | Position persists after a close | `live.exit_rejected` |
| Skipped symbol | Rebalance completes, places nothing | `live.plan_skips` |
| Dead loop | `*_started_block` frozen, no skip events | `live.tick_failed` |
| Hung continuation | `*_pending` stuck, no error | `live.continuation_lost` |
| Unclosable position | Real exposure, no exit path | `unclosable_*` skip reason |
| Runner lockout | Book freezes, chain healthy | Heartbeat rejections; jobs created, none assigned |

---

## 7. Deployment

Stand-up is one scripted sequence, and the ordering encodes hard-won failures:

1. **Provision** with the LLM API key supplied at creation — otherwise it falls back to a local model whose output never parses into scores above the threshold.
2. **Patch CBSS config** — `ingest.chain_id` must match `chain.chain_id` or the proxies crash-loop while still reporting as registered.
3. **Set consensus timeouts before load.**
4. **Run the key ceremony once**, with a version-matched CLI, and verify the secret registered. A stale CLI silently no-ops.
5. **Deploy and fund** both actors.
6. **Warm the universe cache** before any rebalance — planning against an empty universe matches nothing and places zero orders while reporting success.
7. **Drive the first trade.**

An off-box watchdog monitors both the book signature and chain height, re-arming actor timers on a stalled book and restarting the validator on a frozen height.

---

## 8. Known issues and open work

Distinct from §6.5, which describes failure *classes* and how they are detected. This is the current state of specific defects.

### 8.1 Block-rate decay — OPEN, root cause known

Block production decays roughly 0.76 → 0.27 blk/s over ~10 hours, then plateaus and holds. Profiling attributes the bulk to a fixed per-block cost of roughly 921 ms in ZSTD decompression, with about 24% of total CPU spent in `createDCtx` alone because a decompression context is constructed per read rather than reused.

Because every cadence, timeout and guard is block-denominated (§6.2), the decay stretches all of them ~2.7×. This is the root cause behind several downstream symptoms and is the highest-leverage remaining fix.

A first attempt reused the decompression context per thread but changed decode semantics: the fast path sized its buffer from `get_frame_content_size` and called `decompress`, both of which see only the *first* frame, where the `decode_all` it replaced consumes every concatenated frame. Multi-frame blobs were silently truncated (39 bytes returned as 19). A follow-up patch fixed that specific divergence, but `cold_start_bootstrap` still fails and was never diagnosed, because the tree cannot be built on macOS.

The likely correct shape is to reuse the context *and* retain `decode_all`'s multi-frame semantics — upstream deliberately moved to `decode_all`, which suggests they hit the same issue. Next step is one CI run on Linux to read the actual failure.

### 8.2 Runner heartbeat lockout — FIXED, awaiting deploy

The most damaging defect in practice: it stops trading entirely while the chain looks perfectly healthy.

Mechanism: transactions wedge in the mempool, pushing `mempool_next` beyond `storage + MAX_FUTURE_NONCE_GAP`. Admission then requires `nonce >= max(storage_next, mempool_next)` *and* `nonce < floor + max_backlog` — bounds that have crossed, so **no nonce is admissible**. Runners cannot heartbeat, go stale past `HEARTBEAT_TIMEOUT_BLOCKS`, and drop out of job dispatch. Jobs are created and assigned to nobody, so every actor continuation starves and the book freezes while blocks keep being produced normally.

A nonce clamp capping the advertised heartbeat nonce at `storage + 31` converted the rejection from `nonce_too_high` to `nonce_too_low` without resolving the deadlock.

Age-based mempool eviction was written to break it, and did not fire — 0 evictions across 3 hours with ~95 transactions wedged. **Root cause:** eviction measured transaction age from `received_at`, which is cleared when a transaction is popped for a proposal and re-stamped as *now* by the cancelled-proposal restore path. A wedged mempool cancels proposals every few seconds, so every restore reset the age of exactly the transactions the eviction existed to remove. Circular and self-sustaining.

Fixed by measuring age from a `first_seen` timestamp preserved across pop/restore cycles. Covered by a regression test that fails against the previous behaviour.

### 8.3 Builder-dex illiquidity — FIXED, awaiting deploy

Builder-deployed listings (asset IDs in the 750xxx range) have no resting liquidity. Observed 34 consecutive refusals on equity symbols while every core-perp order filled — including orders priced 1–3% *through* the mark, which rules out limit-price aggressiveness as the cause.

The compounding problem was that research had no way to represent "cannot be executed": a refused order never becomes a position, so it never appears as a winner or a loser, and synthesis re-proposed the same assets indefinitely. During one 8.5-hour window every rebalance planned only unfillable equities and the system opened nothing while reporting success.

Fixed by tracking per-symbol execution feasibility, suppressing opens (never closes) on repeated refusal, and feeding an `unfillable` list back into synthesis.

### 8.4 Consensus livelock — FIXED, deployed

Block cycle cost (~2.1 s) exceeded the leader timeout (2000 ms), so proposals were cancelled every round. The adaptive controller responded by halving the transaction budget — but that budget governs only ~6% of cycle cost, so it drove to its floor while the deadline was still missed, and the chain re-executed a single height ~1045 times without committing.

The documented mitigation is to raise the leader timeout, but it was capped by a hardcoded 2 s certification timeout that made any larger value panic at boot. Fixed by making the certification timeout configurable and clamping the pair rather than aborting. Validated over a 20-hour run with zero deadline misses.

### 8.5 Learning quality — OPEN

Lessons were being drawn at roughly one per closed trade (8 lessons from 9 trades), several stated as confident causal rules from single observations. Confidence gating and alpha attribution (§5.2, §5.3) address the measurement, but the underlying issue is horizon: at 20–60 minute holds the outcome signal is dominated by noise, so more data does not straightforwardly produce better rules. Lengthening holding periods is the likely structural fix.

---

## 9. Status

- Longest continuous run: **20+ hours** of chain uptime.
- Complete round trip demonstrated: news → LLM scoring → thesis → idea → sized order → CBSS-released key → signed exchange order → on-chain position → live marking → realized P&L → distilled lesson.
- The learning loop demonstrably changes behaviour: it identified its micro-cap short exposure as structurally unprofitable from its own realized losses and stopped taking it.
- Per-trade profitability is **not** yet statistically distinguishable from noise at current sample sizes. Trade cadence, execution reliability, and learning quality are the active areas of work.
