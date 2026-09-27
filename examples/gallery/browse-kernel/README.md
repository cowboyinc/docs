# Kernel Browse Store wrapper

A hire-gated wrapper over a Kernel cloud browser. It sells two capabilities to
registered Store Hires, both asynchronous (an acceptance record now, the bounded
page state via `get_result` later):

- **`visit`** — a stateless one-shot read. The deployed `cowboy-browse` Kernel
  app opens a browser, navigates, extracts the page, and closes it, all
  server-side, in one call.
- **`step`** — a stateful op against a **persistent, per-hire** Kernel session:
  `op` in `{open, goto, click, type, press, scroll, extract, close}`. This is how
  a real multi-step task (search, paginate, fill a form, follow a link) runs.

The Kernel API key stays in CBSS as `KERNEL_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Kernel key, the deployed `kernel-app`, and a paid
Trading Post Hire.

## Contract

Every call maps to one `POST https://api.onkernel.com/invocations` with
`Authorization: Bearer <key>`. The invocation body is
`{"app_name":"cowboy-browse","action_name":"<visit|step>","payload":"<json string>"}`
where `payload` is a JSON **string** the app `JSON.parse`s (the API rejects an
object). The response envelope is `{action_name, id, status, output}`; success
requires `status == "succeeded"` **and** the `output` string parsing to
`ok: true`. A missing/other status, a `{"code":"bad_request",...}` body, a
non-JSON/raw reply, and `output.ok != true` are all classified `status:error` —
never a false success.

### `visit`

Input: `hire_id` (int) and `url` (an `http(s)` URL; a bare domain is normalised
to `https`; `javascript:`/`file:`/`data:`/`about:` are refused). Payload string
is `{"url":"<url>"}`.

### `step`

Input: `hire_id` (int), `op`, and only the fields the op needs:

| op | fields | mutating | needs an open session |
|----|--------|----------|-----------------------|
| `open` | `stealth?`, `timeout_seconds?` (30..3600) | yes | no (creates one) |
| `goto` | `url`, `stealth?`, `timeout_seconds?` | yes | no (auto-opens if none) |
| `click` | `selector` | yes | yes |
| `type` | `selector`, `text` | yes | yes |
| `press` | `key` (default `Enter`) | yes | yes |
| `scroll` | `dy` (default `800`) | yes | yes |
| `extract` | — | no | yes |
| `close` | — | yes | yes |

**Per-hire session (the key safety property).** The caller **never** supplies a
`session_id` — it is refused as an unsupported field. The actor keeps **at most
one** session per hire, sets it on `open` (or the auto-open the Kernel app does
on the first `goto` when the hire has none), and clears it after Kernel confirms
`close`. A failed close keeps a closing binding that can only be retried or
expired. Every page op
is dispatched with that hire's **own** stored session id. Because a hire can only
ever act on the session this actor stored for it, a caller can never name another
hire's session — there is no cross-hire session-stealing hole.

**Request identity.** Mutating ops (`open/goto/click/type/press/scroll/close`)
carry an `Idempotency-Key` derived from the request id. Provider-side deduplication
depends on Kernel's invocation contract. `extract` and `visit` are read-only and
carry no key.

Both handlers return immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"step","op":"goto","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`. On
success `output.data` holds the bounded page state `{url, title, text, links,
session_id?}` for page ops (and `visit`), `{session_id}` for `open`, or
`{closed}` for `close`. Signed callers can pass `receipt_offset` to emit the
encoded result as ordered `wrapper.result_chunk` events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates (would let the
  runner paste the resolved key into an outbound field). Selectors, text, and
  keys additionally refuse control characters (incl. CR/LF).
- The target URL is public chain data. Never pass a private/signed URL whose
  possession grants access.
- The request rides `raw_body` (exact JSON, `allow_nan=False`), a `Bearer` auth
  header, an explicit `User-Agent`, and one CBSS-backed key reference. The API
  key never enters actor payloads, continuations, storage, or results; the
  continuation carries only the request id.
- **Executor text cap:** the `cowboy-browse` executor caps returned page text at
  **~4000 chars** — `get_result` returns that bounded text, **not** the whole
  page. Do not expect full-article content.
- **Transport cap:** the executor rejects a complete page result above 32,000
  bytes before it enters the invocation envelope. The actor also bounds its
  whole encoded result to 38,000 bytes. Both return an error instead of changing
  a URL or link target.
- At most 16 ordinary requests are retained globally and six per Hire. A close
  remains available at those limits, with at most one extra close receipt per
  active session and a hard global maximum of 24 retained requests. At most 8
  server-side sessions are active. Pending and completed records expire after
  bounded block windows.
- **Sessions cost money.** An abandoned session is best-effort auto-closed after
  its maximum lifetime (`session_lifetime_blocks`, ~300 blocks) on the next cleanup
  pass, and also self-expires on Kernel's own `timeout_seconds`.
- Results belong to the submitting buyer and Hire; public status is
  metadata-only. Runner callbacks use the private continuation resume path.

The independent `kernel_routes.json` fixture pins the `POST /invocations`
`visit` + `step` contract and the op set; `tests/test_kernel_parity.py` fails on drift.

## API key

Create a key at [onkernel.com](https://onkernel.com/). Store it in a file. Do
not put it in source or pass it directly on a command line. The `kernel-app/`
executor must also be deployed (`npx @onkernel/cli deploy index.ts -e
KERNEL_API_KEY=$KERNEL_API_KEY`); see `kernel-app/README.md`.

## Tests

```bash
cd examples/gallery/browse-kernel
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
uv run pytest
cowboy actor check browse

python3 tests/test_kernel_parity.py          # offline drift
python3 tests/test_kernel_parity.py --live   # probe published reference

# live differential (needs a Kernel key + deployed executor):
KERNEL_API_KEY_FILE=/secure/kernel-key.txt \
  python3 tests/test_kernel_comparison.py --page-url https://example.com
KERNEL_API_KEY_FILE=/secure/kernel-key.txt \
  python3 tests/test_kernel_comparison.py --sequence --page-url https://example.com
```

`cowboy actor check browse` needs a built `cowboy-dev-runner`. The pytest suite
is the offline boundary-contract check when that native runner is unavailable.

## Canyon

The complete Store flow requires Canyon runners, CBSS proxies, and the deployed
`kernel-app`. `demo.sh` deploys, funds, declares, hires, registers the hire, and
then runs a signed `visit` **and** a stateful `step` sequence
(`goto` auto-open -> `extract` -> optional benign `click` -> `close`):

```bash
STORE_REGISTRAR=0x... \
KERNEL_API_KEY_FILE=/secure/kernel-key.txt \
KERNEL_PAGE_URL="https://example.com" \
KERNEL_CLICK_SELECTOR="a" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

## Versioning

`visit` + `step` (acceptance record + `get_result`) is the **v2.0.0** surface.
It renames the former `browse` handler to `visit` and adds `step`, so it is a
breaking version bump over v1.0.0 and requires a redeploy plus a fresh Trading
Post declaration. Any further change to the returned shape or the supported
input fields is likewise a breaking bump.
