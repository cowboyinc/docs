# Hyperliquid live-signing — state as of 2026-07-21

## What works, end to end

An on-chain actor placed and cancelled **real Hyperliquid testnet orders**, signed by
the runner with a key released from CBSS that never existed in actor memory or in the
request that left the box. PR #182's `sign_and_strip_http_job` is exercised and correct.

Proof (testnet):
- order `56772522111` — BTC filled 0.00056 @ 65163.0
- order `56772524299` — ETH filled 0.0082 @ 1909.7
- closes filled, book flattened, nothing left resting

The digest is verified four independent ways: 89 differential tests vs the official SDK,
byte-identical output on RustPython vs CPython, identical digest computed on-chain, and a
signature byte-identical to `sign_l1_action` accepted by the live venue.

## Files

| File | State |
|---|---|
| `hl_live_exec.py` | **working live trader.** Fork of the paper trader: same strategy, real fills. Carries the inlined venue layer. |
| `hl_paper_exec.py` | **pristine simulator**, untouched. 7 tests pass. |
| `hl_research.py` | untouched |
| `tests/test_hl_digest.py` | 89 tests, all passing (imports `hl_live_exec`) |
| `tools/setup_live.sh` | reproduces a working environment end to end |

## Current state (2026-07-21)

`hl_live_exec.py` is a **fork of the paper trader**, not a mode of it. All six
strategy functions are byte-identical between the two (`_select_ideas`,
`plan_book`, `_pos_return`, `_exit_reason`, `_mark_book`, `resolve_prices`),
verified by source comparison under the PVM. `hl_paper_exec.py` is untouched
and remains a pure simulator.

Proven live on testnet, driven through the full strategy path:
  - open from research-shaped ideas, confidence-weighted, real fills
  - close via empty idea set (reduce-only, fill-gated)
  - stop-loss exit placing a real reduce-only order
  - equity-based sizing (falls back to a fixed `budget` when `account_addr` is unset)

Three bugs found only against a live venue, all fixed:
  1. `"status":"ok"` is not a fill — the per-order error is nested inside it, so
     the book recorded positions that never existed
  2. price ticks — HL needs <=5 significant figures AND <=(6 - szDecimals)
     decimals; `%.6g` produced illegal prices and every order was rejected
  3. close sizing — `int(q*step)` truncation left a dust tick behind on every
     close. Closes now round UP (`fmt_close_size`), which is safe because
     reduce-only clamps at the venue.

## BLOCKED: research needs runner PR #184

`runner.llm` jobs fail with "Unsupported job type" on every published runner
artifact, and a failed job wedges the dispatcher for the whole chain.

Cause: the runner workspace has two bins named `runner-node`. `c509360`
(2026-07-12) switched CI from the root package's bin to `crates/runner-node`'s
to fix a missing container runtime. The root bin registers an LLM executor; the
crates bin never has. River was pinned to `v0.1.3-fefd95a` — the tip of
`logan/river-cip11-runner-resilience`, which forked before that CI change and so
still shipped the root bin. Repinning river to a mainline build to pick up
PR #182's signing silently swapped LLM+MCP for Container+QUIC.

Fix: runner PR #184 (`fix/runner-llm-executor`, commit c9d8ffe) registers the
LLM executor in the shipped bin. Reviewed and passing, not yet merged.

To test before merge, dispatch a build (publishes the versioned dir only; it
cannot move the `latest` channel pointers):

    gh workflow run pipeline.yml --ref fix/runner-llm-executor -f sha=c9d8ffe

then pin `runner: "v0.1.3-c9d8ffe"` in `outpost/configs/cal.json` and rebuild.

`runner.mcp` has the identical gap and is still unfixed.

## Live-mode differences from paper (by design, worth knowing)

- sizes in coins floored to `szDecimals`, not notional-at-mid
- entry price is the venue's `avgPx`, not the mid
- an order that doesn't fill does not open a position
- **max 3 orders per rebalance** — the book converges over successive ticks
- names absent from the testnet perp universe (NVDA, pre-IPO) are skipped, not failed

## Three bugs found only by trading against a real venue

1. `"status": "ok"` is **not** a fill — the per-order error sits inside the ok envelope.
   Recorded phantom positions until `parse_fill` was added.
2. Price tick rules — HL needs ≤5 significant figures AND ≤`(6 - szDecimals)` decimals.
   `%.6g` produced illegal prices; every order was rejected. Now matches the SDK's
   `round(float(f"{px:.5g}"), 6 - szDecimals)`.
3. close sizing left dust — flooring a float qty drops a whole tick. Closes now round
   UP via `fmt_close_size`, safe because reduce-only clamps at the venue. (Rounding the
   float first was tried and still failed live; ceil is the robust fix.)

## Environment (river-cal)

Created 2026-07-21T00:55Z, **72h TTL → reaps ~2026-07-24**. Extend with
`cowboy-env extend cal --ttl <hours>`.

Working version set in `outpost/configs/cal.json` — do not change one component alone:

```json
{"node":"v0.0.34-59a2d35","runner":"v0.1.3-d4dd2b4",
 "cbssd":"v0.0.9-c226373","cbfs":"v0.1.4-c8bbbc0","gateway":"v0.1.2-495697d"}
```

The outpost fixes are committed on branch `fix/river-cbss-release` (19e3fdb):
runner env gets `CBSS_THRESHOLD_ENABLED` + `CBSS_BLOB_ENDPOINTS` (bare base URLs —
the client appends `/v1/blob/<hash>`), cbssd config gets `chain.chain_id`, and
`configs/cal.json` pins `cbss_proxies: 5` and a coherent version set.
**Not yet pushed or upstreamed.**

## Nine river/outpost defects found (none in PR #182)

Three of these each independently prevent CBSS release, so **release had never worked on
a river box**:

1. `cbss_proxies: 2` in the default config vs `DEFAULT_N = 5` — account DKG can never
   succeed; the instruction reverts *without consuming the nonce*, so it looks like a
   silent retry loop
2. `CBSS_THRESHOLD_ENABLED` never set on river runners → release path never constructed
3. `CBSS_BLOB_ENDPOINTS` never set → "no ciphertext gateway configured"
4. missing `cowboy secrets policy --actor` → cbssd refuses with "release request was not
   authorized"; the runner only sees `early eof`
5. own-account vs cross-account secret ref — the secret is owned by the deploying EOA, not
   the actor, so `secrets=["NAME"]` computes the wrong `key_hash`
6. node/runner codec pairing — a mismatched set fails with
   `Invalid("JobSpec", "unsupported version")`
7. cbssd rev must match the runner's `cbss-client` pin (`c226373`)
8. `CBFS_BLOB_ENDPOINTS` (cbssd) includes `/v1/blob`; `CBSS_BLOB_ENDPOINTS` (runner) must
   **not** — the client appends the path itself
9. **a failed runner job permanently wedges the dispatcher** — no further job is assigned
   on that chain, even to a fresh actor, even after restarting runners. One attempt per
   chain. This is why the above took six rebuilds. Unfixed; worth filing.

## Reproducing from scratch

`tools/setup_live.sh` does wallet → faucet → wait-for-5-proxies → DKG → deploy (with
`secret_account`) → **release policy** → fund → digest sanity check. Run it with
`RPC=http://<ip>:4000`, then the `secrets set` step by hand (the key must not go through
automation), then re-run with `STAGE=deploy`.

**The deployer key lives in the session scratchpad and will not survive.** If it's gone,
re-run `tools/setup_live.sh` — it makes a new wallet, which needs a fresh DKG and a fresh
`secrets set`, so have the HL agent key to hand.

## Open from the PR review

The unbound-digest item is still open and still correct: the runner signs a fully
caller-supplied digest with no recomputation from url/body and no domain separation, so it
cannot attest "I signed what I sent." Fine for devnet; bind or recompute before this signs
anything holding real funds.
