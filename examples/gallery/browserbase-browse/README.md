# Browserbase Store wrapper

A hire-gated, wallet-billed wrapper over [Browserbase](https://browserbase.com):
fetch a page, search the web, or run an agent that browses JS-rendered pages.
The vendor key stays in CBSS as `BROWSERBASE_API_KEY`; the runner injects it
into every vendor request. POST JSON uses the Runner's `raw_body` field so
fractional JSON Schema values reach Browserbase unchanged. GET sends no body.

This example is outside the default gallery sweep because a live run requires
CBSS proxies, runners, a Browserbase key, and a Trading Post hire.

## Handlers

| Handler | Browserbase API | Shape | Notes |
|---|---|---|---|
| `fetch` | `POST /v1/fetch` | synchronous | url → `markdown` / `raw` / `json`+schema. Does NOT execute JavaScript |
| `search` | `POST /v1/search` | synchronous | query → result metadata (url, title); no page content |
| `browse` | `POST /v1/agents/runs` | async: start | starts an agent run that renders JS, returns a `run_id` |
| `browse_result` | `GET /v1/agents/runs/{id}` | async: poll | status + `result` once terminal |

Plus `init`, `register_hire`, `get_result` (the poll handler), `capability`,
`get_config`, `get_last_output`.

Each vendor call is hire-gated and asynchronous: the handler validates input and
returns an acceptance record, and the registered hirer reads the result from
`get_result`. Under the hood it is a single `runner.http` with
`${BROWSERBASE_API_KEY}` from CBSS, resumed once as a continuation. Because the
key rides every call in a header and Browserbase echoes fields like the search
query back, any caller input containing the `${NAME}` secret template is refused
before dispatch, so the runner can never paste the key into an echoed field.

## fetch vs browse

`fetch` retrieves the raw server response and converts it to markdown or
extracts structured JSON against a schema. It is fast and cheap but **does not
render JavaScript**, so a client-rendered SPA comes back as its un-hydrated
shell. For JS-rendered or interactive pages use `browse`, which runs a real
agent.

`browse` is **asynchronous**: `browse` starts a run and returns its `run_id`
immediately, and the caller polls `browse_result` until `data.done` is true.
This suits an actor caller, which polls across its own transactions rather than
blocking a continuation on a long agent run.

```json
{"hire_id": 42, "url": "https://example.com", "format": "markdown"}
```

```json
{"hire_id": 42, "query": "cowboy actor network", "num_results": 5}
```

```json
{"hire_id": 42, "task": "Open the pricing page and report the cheapest plan"}
```

```json
{"hire_id": 42, "run_id": "<run_id from browse>"}
```

## Response

A handler does **not** return the result inline. Because the Runner callback
lands after the buyer's transaction, each handler returns an **acceptance
record**, and the registered hirer reads the result from `get_result` with the
returned `request_id` — so the result reaches that buyer and is not overwritten
by an overlapping request (v2.0.0; a breaking change from the inline contract, so
a redeploy is required).

```json
{"ok": true, "request_id": 7, "hire_id": 42, "operation": "fetch", "status": "pending", "poll_handler": "get_result"}
```

Read it as the same caller and hire — `get_result` returns `status` `pending`
until the callback resolves, then `ok`/`error` with the `output`. For `fetch` and
`search`, `output.data` is the native Browserbase envelope. The complete encoded
Actor response is bounded to 38,000 bytes; large content has a `_cowboy`
truncation marker. This limit applies after the callback and does not make a
large vendor response fit the Runner transport. For `browse` and
`browse_result`, `output.data` is `{run_id, status, done, result?}`. Only `result`
is truncated; the run ID and status remain intact. `done` includes failed agent
runs, so require `status: "COMPLETED"` before treating a task as completed.

Browserbase `format=raw` still returns a JSON envelope with a `content` field.
Non-JSON gateway responses and malformed vendor replies become stored errors.
The fetched page's own `statusCode` is preserved, including 404 responses.

```json
{"request_id": 7, "hire_id": 42, "operation": "fetch", "status": "ok", "ok": true,
 "expires_height": 1500,
 "output": {"ok": true, "operation": "fetch", "data": {
   "id": "fetch-id", "statusCode": 200, "headers": {}, "content": "# Example",
   "contentType": "text/markdown", "encoding": "utf-8"}}}
```

## Excluded

Live CDP/WebSocket browser sessions (the classic Browserbase driving model)
require a persistent connection an on-chain actor cannot hold, so they are not
exposed. File downloads are excluded. The wrapper sells the synchronous REST
surface plus the async agent.

## Entitlements

Allow `http.fetch` to `api.browserbase.com`; allow `secrets.read` for
`<deployer>:BROWSERBASE_API_KEY`, and `storage.kv`. The CBSS release policy must
also allow-list the deployed actor for the current secret version.

## Simulate Locally

The offline suite drives the actor through the SDK's in-memory host; the
`browse` target compiles the way a deploy does. From this directory (with
`COWBOY_SDK_PATH` pointing at a `node/pvm/Lib` checkout):

```bash
uv run pytest
cowboy actor check browse
```

`cowboy test` compiles the target and runs one handler on the simulated PVM.
This calls `get_config` on a fresh actor and expects the CBOR-encoded reply:

```bash
cowboy test --target browse --handler get_config --payload 0x \
  --expect 0xa56676656e646f726b62726f77736572626173656a63616c6c5f636f756e74006b6c6173745f737461747573f66f73746f72655f7265676973747261726073726573706f6e73655f627974655f6c696d6974199470
```

`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Run On A Network

`demo.sh` deploys the `browse` target with `cowboy actor deploy browse`; the
CLI discovers the target from this project's `cowboy.toml`, so use a CLI
release that matches the network you target.

```bash
export RPC_URL=https://rpc.canyon.cowboylabs.net
export CBSS_INGEST_ENDPOINT=http://<cbssd>:<port>
export BROWSERBASE_API_KEY_FILE=/path/to/key.txt
./demo.sh
```

The demo requires `uv`, runs the full store flow, and ends in a gated `fetch`.
It obtains the request ID from that transaction's acceptance event and reads
the exact result through signed `get_result` receipt chunks. It does not retry
the vendor request after a polling timeout. Keep the printed request ID to
resume a read:

```bash
uv run examples/gallery/browserbase-browse/tools/browserbase_client.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" result "$REQUEST_ID"
```

Use the vendor key file input so the key does not enter shell history.

## Test

Offline (no chain, no key). The contract suite loads the actor with the real SDK
decorators against the in-memory host, so point `COWBOY_SDK_PATH` at a
`node/pvm/Lib` checkout; the drift guard is standalone:

```bash
COWBOY_SDK_PATH=/path/to/node/pvm/Lib uv run pytest
python3 examples/gallery/browserbase-browse/tests/test_browserbase_parity.py
```

`tests/test_browserbase.py` exercises the acceptance-record + `get_result` contract for
all four handlers: dispatch shape and secret custody, authenticated reads (wrong
caller / wrong hire rejected), secret-template refusal, unregistered and stolen
hires, duplicate / late / forged callbacks, oversized-reply bounding, the
request-capacity and hire caps, deadline expiry, and reversed callbacks after
Actor and SDK modules are reloaded. The client tests cover signed chunk delivery,
wrong identities, full-size chunks, exact request replay, secret declarations,
redirect refusal, and failed agent runs. The parity test is a drift
guard: it fetches Browserbase's API reference pages and fails if an endpoint the
actor calls moves, changes method, or drops the `X-BB-API-Key` auth.

Live, with a key file (direct vendor call versus the Actor's SDK job replay).
These checks spend vendor credits. Replay preserves the emitted URL, method,
headers, and body, substitutes only the declared test secret, and disables
redirects as the Runner does. It is not a Node/PVM or live-chain check.
Add `--with-agent` to start one agent run and compare its completed result with
an independent direct read of the same run:

```bash
COWBOY_SDK_PATH=/path/to/node/pvm/Lib \
  uv run examples/gallery/browserbase-browse/tests/test_browserbase_comparison.py \
  --key-file /secure/key.txt
COWBOY_SDK_PATH=/path/to/node/pvm/Lib \
  uv run examples/gallery/browserbase-browse/tests/test_browserbase_comparison.py \
  --key-file /secure/key.txt --with-agent
```

On chain, against a deployed and hired Canyon Actor, the same comparison checks
use the signed reader shared with the demo. They compare markdown fetch,
structured fetch, and search. Add `--with-agent` for the agent check:

```bash
uv run examples/gallery/browserbase-browse/tests/test_browserbase_canyon.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" --key-file /secure/key.txt
```
