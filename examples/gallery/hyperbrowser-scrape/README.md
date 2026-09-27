# Hyperbrowser Scrape Store wrapper

A hire-gated wrapper over Hyperbrowser's single-URL web-scrape API. Registered
Store Hires submit a URL and read a request-specific result through
`get_result`. The Hyperbrowser API key stays in CBSS as `HYPERBROWSER_API_KEY`
and only ever rides the job as a `${HYPERBROWSER_API_KEY}` reference in an
`x-api-key` header (Hyperbrowser uses a raw api-key header, **not** Bearer).

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Hyperbrowser key, and a paid Trading Post Hire.

> ## ⚠️ Launch-Store optics decision required
>
> **Hyperbrowser markets stealth / anti-bot evasion / CAPTCHA-solving as default
> features.** A first-party Store shipping a stealth-by-default scraper reads
> badly and can enable ToS-violating scraping. This wrapper is built as a
> legitimate technical exercise and **deliberately does not expose or send any of
> those options** — no `sessionOptions` at all (`useStealth`, `useUltraStealth`,
> `useProxy`, `proxyServer/Country/State/City`, `solveCaptchas`, `solverType`,
> `adblock`, `trackers`, `annoyances` are never on the handler surface or the
> outbound body). It exposes only a straightforward fetch/scrape of a
> caller-named URL. **A human decision is still needed on whether Hyperbrowser
> belongs in a launch Store at all**, given the vendor's positioning — this
> wrapper does not settle that; it surfaces it. Do not ship without that call.

## Why the two-step shape (scrape → fetch)

Hyperbrowser scrape is **submit → poll**, and there is no synchronous mode and no
hosted/presigned result-URL option (unlike Reducto's `force_url_result`):

- `POST /api/scrape` returns only a small `{ "jobId": "..." }`.
- The result is retrieved later from a **separate, key-authenticated**
  `GET /api/scrape/{jobId}` that returns large inline page content.

So a single POST cannot deliver content (a `jobId` is useless to a buyer who does
not hold the key), and the result GET can neither be handed to the buyer as a
poll URL (it needs the key — a credential) nor reliably fit the runner transport
cap. The honest, transport-safe, credential-safe shape is therefore two hire-
gated operations, each a single `runner.http` call through one continuation:

- **`scrape`** — one `runner.http` **POST** that returns the small `job_id`.
  Inputs: `hire_id`, `url` (http/https), optional `formats` (a subset of
  `["markdown","links"]`, default `["markdown"]`). `onlyMainContent` is forced
  true. Returns `{"job_id","job_status":"submitted"}` under `output.data`.
- **`fetch`** — one `runner.http` **GET** that the ACTOR performs with the CBSS
  key (the key rides as a `${...}` reference and is never handed to the buyer),
  returning the bounded scraped content. Input: `hire_id`, `source_request_id`
  (a prior **completed** `scrape` request owned by the same buyer + Hire).
  Returns `{"job_status": "completed", "metadata", "markdown"?, "links"?}` — or
  `{"job_status": "running"}` (retryable) while the job is still in flight — under
  `output.data`.

Because `fetch` references the buyer's **own** prior `scrape` request by
`source_request_id` (already buyer + Hire gated), no raw vendor job id is ever a
caller input and there is no cross-Hire resource hole — a different Hire's
`source_request_id` is refused.

### Why this fits the transport cap

Hyperbrowser returns scrape content inline from the result endpoint. The runner
can select a top-level response field, but it cannot reshape or truncate nested
scrape content before the runner-to-actor limit. This wrapper therefore requests
only `markdown` and `links` with `onlyMainContent` enabled. After a reply reaches
the actor, exact status, metadata, and link URLs are preserved. If only the
optional markdown would exceed the actor result budget, the wrapper omits it and
returns an explicit `_cowboy.markdown_omitted` marker. If exact fields alone are
too large, the fetch returns an explicit size error.

A running job remains bound to the original buyer and Hire for 600 blocks. Polls
reuse one fetch receipt, so retries do not consume unbounded retained capacity.
The binding survives cleanup of the original submit receipt and is removed when
the provider reports a terminal status or the job lifetime expires.

### Excluded because they would park or cross a credential

- **Sessions (`/api/session*`)** — a session returns a live CDP
  `wsEndpoint`/`token` connect credential. `get_result` delivers results by
  reading them from **public consensus storage**, so that credential would be
  readable on chain (session-hijack risk) and cannot be stripped without making
  the session useless. This is the **G5 browser-session wall**: only stateless,
  credential-free, bounded one-shot operations are safe.
- **Crawl / batch scrape** — multi-page and async with no bounded delivery.
- **Extract** — schema-dependent inline result size is unbounded and can park.
- **`html` / `screenshot` formats** — overflow the transport cap / base64.
- **All stealth/proxy/CAPTCHA session options** — see the optics box above.

## Call contract

`scrape` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"scrape","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id` until
the scrape submit resolves (`output.data.job_id`), then call `fetch` with
`source_request_id` set to that scrape's `request_id` and poll `get_result` for
its content. Signed callers can pass `receipt_offset` to emit the encoded result
as ordered `wrapper.result_chunk` events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates (checked across
  `url` and `formats`). A `${HYPERBROWSER_API_KEY}` inside `url` would otherwise
  let the runner paste the live key into the outbound request or an echoed error.
- All inputs **and results** are public chain data. Scraped content lands in
  public consensus storage (gated to the buyer + Hire, expiring after a bounded
  window) — do **not** scrape private/authenticated pages.
- The continuation carries only the request id — never the key, which rides the
  job solely as a `${HYPERBROWSER_API_KEY}` reference resolved runner-side. No
  live credential is ever written to storage, the continuation, or a result.
- Requests use `raw_body` on POST, a `GET` that omits the body entirely (a GET
  carrying a body is rejected on chain as E1604), an explicit `User-Agent`, and
  one CBSS-backed key.
- Final canonical-CBOR responses are capped at 38,000 bytes; a non-JSON or
  vendor-error reply is truncated to 500 bytes and never labeled success.
- At most 16 ordinary requests are retained globally and six per Hire. A
  provider job may reserve one reusable fetch receipt, making the absolute
  bounds 32 globally and 12 per Hire while dependent fetch remains reachable.
- Pending and completed receipts expire after bounded block windows; provider
  job bindings expire after 600 blocks.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

### Billing / idempotency caveat

`scrape` is **billable** (it creates a Hyperbrowser job). Hyperbrowser exposes no
idempotency key, so the wrapper cannot dedup vendor-side: dispatch is one submit
per request (same-transaction cid dedup + a single continuation), but a
runner-level retry of the job could re-scrape and double-bill. This is documented
rather than papered over with an ignored header. `fetch` is a read-only GET and
carries no such risk.

The independent `hyperbrowser_routes.json` fixture pins the endpoints, the
`x-api-key` auth, and the transport bounds. This actor claims parity for its
documented bounded scrape surface, not every Hyperbrowser product.

## API key

Create a key at [hyperbrowser.ai](https://hyperbrowser.ai/) / the Hyperbrowser
dashboard. Store it in a file. Do not put it in source or pass it directly on a
command line.

## Tests

```bash
cd examples/gallery/hyperbrowser-scrape
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
uv run pytest
cowboy actor check scrape

uv run python tests/test_hyperbrowser_parity.py        # offline drift
uv run python tests/test_hyperbrowser_parity.py --live # probe published docs

HYPERBROWSER_API_KEY_FILE=/secure/hyperbrowser-key.txt \
  uv run python tests/test_hyperbrowser_comparison.py \
  --url "https://example.com" --formats links
```

The parity script checks the fixture against the actor; `--live` additionally
verifies Hyperbrowser's published docs still document `POST`/`GET /api/scrape`
with the `x-api-key` header. The comparison script scrapes one real URL and feeds
those exact vendor replies through the wrapper's two-phase lifecycle, requiring
exact output equality. Because the comparison bypasses transport, it does not prove that an oversized
provider reply reaches the actor; verify that boundary separately on chain.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
HYPERBROWSER_API_KEY_FILE=/secure/hyperbrowser-key.txt \
HYPERBROWSER_URL="https://example.com" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

## Demo Scope

The runnable walkthrough deploys the entitled actor, stores and allow-lists the
secret, creates a Trading Post declaration and Hire, registers it, scrapes one
URL and then fetches its bounded content, reassembling the result from
actor-filtered signed receipt chunks. It requires live Canyon infrastructure and
a Hyperbrowser key, so it is not part of the offline sweep.

## Versioning

The `scrape` + `fetch` acceptance-record + `get_result` contract is the v1.0.0
surface. Any change to the returned shape or the supported input fields is a
breaking version bump and requires a redeploy plus a fresh Trading Post
declaration.
