# Steel Browser Store wrapper

A hire-gated, wallet-billed wrapper over [Steel](https://steel.dev)
(`https://api.steel.dev`) for **screenshotting and PDF-rendering a URL**. The
vendor key stays in CBSS as `STEEL_API_KEY`; the runner releases it just in time
and substitutes it into the `Steel-Api-Key` header.

This example is outside the default gallery sweep because a live run requires
CBSS proxies, runners, a Steel account, and a paid Trading Post hire.

## Handlers

| Handler | Endpoint | Key inputs | Response |
|---|---|---|---|
| `screenshot` | `POST /v1/screenshot` | `url`, optional `full_page`, `delay`, `use_proxy` | `{url}` (hosted image) |
| `pdf` | `POST /v1/pdf` | `url`, optional `delay`, `use_proxy` | `{url}` (hosted PDF) |

Administrative and observability handlers are `init`, `register_hire`,
`capability`, `get_config`, and `get_last_output`. `get_result` is the poll
handler. No public runner callback is exposed.

## Why only screenshot and pdf — the two hard constraints

**Transport cap.** The runner delivers a vendor reply back to the actor under a
hard transport cap (~40-64 KB). If a whole vendor response exceeds it the
continuation never resumes and the request parks silently while the transaction
still reports success — and client-side trimming cannot help, because it runs
*after* transport. `screenshot` and `pdf` each return a single small hosted-asset
**URL**, not the image/PDF bytes, so their whole response reliably fits.

`POST /v1/scrape` is **not exposed**: its response embeds full page content
(`html`, `markdown`, `readability`, `cleaned_html`) inline as strings, which is
unbounded and overflows the cap for any real page — so it would park. There is
no server-side reduction to lean on: the runner's `extract_path` extractor is a
broken stub that returns `{}`.

**Public consensus state.** `get_result` delivers a result by reading it out of
the actor's **public** storage, so anything a buyer needs is necessarily public
on chain. Steel's **session lifecycle** (`create/get/release`) is therefore
**not exposed**: a session response carries a live CDP `websocketUrl`/`debugUrl`,
and delivering that through `get_result` would place a live connection
credential in public consensus state for the whole retention window — the
E2B-class "live credential in public state" failure. Stripping the URLs is not
an option, because a session with its connection URL stripped is useless to the
buyer. Session-list, file archives, credentials, and captcha routes are excluded
for the same public-state / size / scope reasons.

A screenshot or PDF is different in kind: it is a **result artifact the buyer
asked for** — a rendering of a public URL the buyer chose — not a live control
channel. Its hosted URL is the deliverable, and appears in the buyer's result
exactly as intended. See the residual note on asset-URL guessability below.

## Call contract

Each handler validates and accepts synchronously, then submits one POST
`runner.http`. For example, `screenshot`:

```json
{"hire_id": 42, "url": "https://example.com", "full_page": true}
```

The immediate result is an acceptance record, not vendor data:

```json
{"ok": true, "request_id": 7, "hire_id": 42, "operation": "screenshot", "status": "pending", "poll_handler": "get_result"}
```

The registered hirer polls with both identifiers:

```json
{"request_id": 7, "hire_id": 42}
```

Once complete, `get_result` returns the request-specific envelope. The hosted
asset URL is under `output.data.url`; vendor, shape, deadline, and size failures
have `status: "error"` and an `output.error` string. A different wallet or Hire
gets only `request not found for caller and hire`. `receipt_offset` may be
supplied to emit the encoded response as ordered `wrapper.result_chunk` events
of at most 3,900 bytes — the path the Canyon test uses when the CLI cannot
display a handler's return bytes directly.

## Bounds and safety

- Caller `url` must be an `http(s)` URL under 2,048 chars, control-char free.
- All caller input is rejected if it contains `${`, so CBSS substitution cannot
  place a secret into an outbound caller-controlled value or an echoed error.
- The API key never enters actor payloads, continuations, storage, or results —
  it rides each job only as the `${STEEL_API_KEY}` reference with
  `secrets=[[account, "STEEL_API_KEY"]]`.
- Both routes are POST: the body rides as `raw_body` exact bytes so the node
  cannot re-adapt fractional JSON. There is no GET here, so no E1604 GET-body
  case exists.
- Every job sets an explicit `User-Agent` (a WAF can 403 default client agents
  before the key is checked).
- Both POSTs carry a stable `Idempotency-Key` derived from the request id so a
  runner retry of the same continuation dedups vendor-side. **Steel does not
  document server-side idempotency** (its SDK sets no idempotency header); an
  unrecognized header is ignored, so this is a best-effort mitigation, not a
  guarantee.
- The full encoded response is capped below the runner-result boundary. At most
  16 request records are retained globally, 6 per Hire, and 512 Hires may be
  registered. Each Hire is stored under its own key.
- Pending requests expire after 150 blocks; terminal results are retained for
  another 150. Late or duplicate continuations are stale.

Actor storage and continuation state are consensus-public. They hold the buyer
address, Hire metadata, request routing, and the bounded vendor result (the
hosted asset URL) — but never the resolved API key. `get_result` enforces
caller/Hire isolation at the handler boundary; it is not encryption of on-chain
data.

## Residual note: hosted asset URL guessability

The screenshot/PDF hosted URL is a rendering of a URL the buyer chose, and it
sits in the (public) result for the retention window. Whether Steel serves these
assets from a **presigned/expiring** URL or an **enumerable** public path is not
confirmed from the current docs — treat the hosted URL as potentially readable
by anyone who sees the on-chain result, and do not screenshot content the buyer
would not want an on-chain observer to retrieve. This is a vendor-behavior flag
for review, not a wrapper defect: unlike a session credential it grants no live
control, only a copy of an already buyer-selected rendering.

## Vendor access

A Steel API key is required (`https://app.steel.dev`). Screenshots and PDFs
consume vendor credits; check the account's current plan and limits before
relying on volume.

## Entitlements

Allow `http.fetch` to `api.steel.dev`, `secrets.read` for
`<deployer>:STEEL_API_KEY`, and `storage.kv`. The CBSS release policy must
separately allow-list the deployed actor for the exact secret version.

## Demo Scope

`./demo.sh` deploys the actor, stores the key from a file, declares the supported
`one-shot:cby:1.5` default term, hires it, then runs the signed Canyon flow: a
real `screenshot` and a signed `get_result` as the registered hirer. The current
node rejects PerCall jobs until CIP-8 session binding is implemented.

```bash
export RPC_URL=https://rpc.canyon.cowboylabs.net
export CBSS_INGEST_ENDPOINT=http://<cbssd>:<port>
export STEEL_API_KEY_FILE=/secure/steel-key.txt
./demo.sh
```

## Simulate Locally

The `steel` target compiles the same project entrypoint that the demo deploys.
From this directory, with `COWBOY_SDK_PATH` pointing to a Node SDK checkout:

```bash
uv run pytest
cowboy actor check steel
```

## Tests

The contract suite imports the real SDK while replacing only its PVM host. Pin
the same dependencies used by the wrapper review fixture:

```bash
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
UV_CACHE_DIR=/tmp/cowboy-uv-cache \
  uv run --python 3.13 --with cbor2==6.1.4 --with pytest -- \
  python -m pytest -q examples/gallery/steel-browser
```

The route fixture `steel_routes.json` is review-pinned. The parity guard runs
offline inside the suite and also offers an opt-in probe of Steel's published
SDK reference:

```bash
python3 examples/gallery/steel-browser/tests/test_steel_parity.py --offline
python3 examples/gallery/steel-browser/tests/test_steel_parity.py --live
```

With an authorized key, the comparison script feeds each handler's exact
composed job to Steel directly and asserts the wrapper's `output.data` equals
the direct vendor JSON (it renders one screenshot and one PDF, so it spends
credits):

```bash
STEEL_API_KEY_FILE=/secure/steel-key.txt \
COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib \
  python3 -u examples/gallery/steel-browser/tests/test_steel_comparison.py
```

After deploying and registering a paid Hire, run the adversarial Canyon test. A
separately funded `--wrong-wallet-key` also proves another identity cannot read
the result:

```bash
python3 -u examples/gallery/steel-browser/tests/test_steel_canyon.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" \
  --wrong-wallet-key /secure/other-funded-wallet.key
```
