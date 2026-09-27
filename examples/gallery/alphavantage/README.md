# Alpha Vantage Store wrapper

A hire-gated, wallet-billed wrapper over [Alpha Vantage](https://www.alphavantage.co)
for bounded market data — quotes, compact daily time series, and company
fundamentals. The vendor key stays in CBSS as `ALPHAVANTAGE_API_KEY`.

Only functions whose whole response fits the Runner result transport cap are
exposed; larger endpoints (financial statements, news, monthly history) are
excluded — see below.

This example is outside the default gallery sweep because a live run requires
CBSS proxies, runners, an Alpha Vantage key, and a paid Trading Post hire.

## Auth is a URL query parameter (read this first)

The other Store wrappers inject the CBSS secret into an HTTP **header**. Alpha
Vantage has **no header auth** — the key is an `apikey` **URL query parameter**.
So the wrapper puts `apikey=${ALPHAVANTAGE_API_KEY}` in the request URL: the
on-chain job holds only the `${...}` reference, and the Runner substitutes the
real key just-in-time, exactly as it does for a header.

URL-position `${...}` substitution is **confirmed on Canyon**: a gated `overview`
call returned live company fundamentals — only possible if the Runner substituted
the key into the URL query — and a free hire executes the job. The on-chain job
holds only the `${...}` reference; the literal key is never stored by the actor.

## Handlers

| Handler | `function=` | Key inputs |
|---|---|---|
| `quote` | `GLOBAL_QUOTE` | `symbol` |
| `daily` | `TIME_SERIES_DAILY` | `symbol`; compact output only |
| `overview` | `OVERVIEW` | `symbol` |

Administrative and observability handlers are `init`, `register_hire`,
`capability`, `get_config`, and `get_last_output` (metadata only — never vendor
data). `get_result` is the poll handler. No public runner callback is exposed.

## Call contract

Every data handler validates and accepts the request synchronously, then submits
one bodyless `runner.http` GET to `/query`. The immediate result is an acceptance
record, not vendor data:

```json
{"ok": true, "request_id": 7, "hire_id": 42, "operation": "quote", "status": "pending", "poll_handler": "get_result"}
```

The registered hirer polls `get_result` with `{"request_id": 7, "hire_id": 42}`.
Successful vendor JSON is under `output.data`; vendor, shape, deadline, and size
failures have `status: "error"`. A different wallet or Hire gets only
`request not found for caller and hire`. `receipt_offset` emits the encoded
response as ordered `wrapper.result_chunk` events of at most 3,900 bytes.

## Bounds and safety

- Symbols are uppercased and restricted to 16 bytes and the characters used by
  real symbols, so caller input cannot inject an extra `&apikey=` or query.
- All caller input is rejected if it contains `${`, so CBSS substitution cannot
  place a secret into a caller-controlled value.
- The `apikey=${...}` reference is appended by the actor after query encoding, so
  it is never percent-encoded and never mixes with caller input.
- Bounds exist on both sides of delivery. Daily is always `compact`, and the
  supported surface (quote / overview / daily) returns small responses that sit
  below the Runner's 65,536-byte result contract. The Runner supports a
  simplified top-level `extract_path`, but these handlers fetch the full vendor
  envelope because their callback contract expects it. The actor caps its full
  encoded response at 38,000 bytes using canonical CBOR. Over-budget date maps
  keep the newest complete rows, lists keep a leading subset, and reduced
  success data carries `_cowboy.truncated`. If complete-unit reduction is not
  enough, remaining strings may be byte-shortened; if the result still cannot
  fit, the request returns an error.
- At most 16 request records are retained globally, at most 6 per Hire, and at
  most 512 Hires may be registered. Each Hire is stored under its own key.
- Pending requests expire after 150 blocks; terminal results are retained for
  another 150 blocks. Late or duplicate continuations are stale.
- Every vendor GET omits `body`, `raw_body`, `Content-Type`, and `timeout_ms`,
  and carries an explicit `User-Agent`.

Alpha Vantage signals errors, rate limits, and demo restrictions as HTTP 200
with a JSON note (`Error Message`, `Information`, or `Note`), so those keys are
treated as errors, backed by a per-function required-key check.

Actor storage and continuation state are consensus-public; they never contain
the resolved API key. `get_result` enforces caller/Hire isolation at the handler
boundary; it is not encryption of on-chain data.

## Supported-surface parity

Alpha Vantage documents more than 100 functions but does not publish an
OpenAPI document. This actor deliberately exposes a transport-safe financial
research subset; it does **not** claim whole-catalog parity. The independent
`alphavantage_routes.json` fixture pins each supported function, required result
field, extraction rule, and request-side bound against the official API
documentation.

Financial statements (`INCOME_STATEMENT`/`BALANCE_SHEET`/`CASH_FLOW` — IBM's
income envelope alone is ~122 KB), `NEWS_SENTIMENT` (~125 KB), and the full
`TIME_SERIES_MONTHLY`/weekly/daily-full histories are **excluded**: their vendor
envelopes exceed the Runner result transport cap, so the Runner cannot deliver
the body and the request **parks** (the continuation never resumes — client-side
truncation runs *after* transport, too late to help). The Runner's top-level
selector changes the callback shape and does not provide general JSONPath
reduction. Those excluded operations have not been adapted and measured with a
compatible selected-result contract, so they remain outside this wrapper.

## Vendor access

A **free** Alpha Vantage key (no credit card) is available at
[alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key);
the free tier is ~25 requests/day. The public `demo` key works only for the
documented example queries (e.g. `GLOBAL_QUOTE` for `IBM`). Excluded from this
wrapper: intraday series, FX/crypto, technical indicators, listing status, and
realtime options.

## Entitlements

Allow `http.fetch` to `www.alphavantage.co`, `secrets.read` for
`<deployer>:ALPHAVANTAGE_API_KEY`, and `storage.kv`. The CBSS release policy must
separately allow-list the deployed actor for the exact secret version.

## Run the paid Store flow

```bash
export RPC_URL=https://rpc.canyon.cowboylabs.net
export CBSS_INGEST_ENDPOINT=http://<cbssd>:<port>
export ALPHAVANTAGE_API_KEY_FILE=/secure/alphavantage-key.txt
./demo.sh   # from examples/gallery/alphavantage/
```

The demo deploys v1, stores the key, declares a `free` term, hires it, submits
`quote`, waits for that exact request record,
and performs a
signed `get_result` as the registered hirer — which is also the on-chain proof
that the URL `${...}` substitution works.

## Test

```bash
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
uv run pytest
cowboy actor check alphavantage

# drift guard (pins the /query base, apikey query auth, and function set; a
# default live demo-key probe confirms GLOBAL_QUOTE; --offline skips network)
uv run python tools/check_alphavantage_parity.py --offline

# with a key, validate the exact emitted request and compare its delivered
# vendor data (or explicitly marked bounded subset) with the actor lifecycle
ALPHAVANTAGE_API_KEY_FILE=/secure/alphavantage-key.txt \
COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib \
  uv run python tools/live_alphavantage.py

# after deploying + registering a paid Hire, the adversarial canyon differential
uv run python tools/check_alphavantage_canyon.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --key-file /secure/alphavantage-key.txt \
  --wrong-wallet-key /secure/other-funded-wallet.key
```
