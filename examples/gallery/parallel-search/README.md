# Parallel Search Store wrapper

A hire-gated wrapper over Parallel's GA Search API. Registered Store Hires submit
bounded searches and read request-specific results through `get_result`. The
Parallel API key stays in CBSS as `PARALLEL_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Parallel key, and a paid Trading Post Hire.

## Supported surface

The `search` handler maps to `POST https://api.parallel.ai/v1/search` with the
API key in the `x-api-key` header. It supports:

- `search_queries` (required, one to five strings)
- `objective`
- `mode`: `turbo`, `fast`, `basic`, or `advanced`
- `max_results`
- `max_chars_total`
- `max_chars_per_result`
- `session_id`
- `client_model`

The actor constructs `advanced_settings.max_results` and
`advanced_settings.excerpt_settings.max_chars_per_result` from the bounded
inputs above. It does not accept unrestricted caller-supplied
`advanced_settings`.

The wrapper deliberately excludes Extract, Task, Monitor, FindAll, Chat, and
Responses. Those products have distinct request, billing, and lifecycle
contracts and should be separate actors.

## Call contract

`search` returns an acceptance record immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"search","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`.
Successful vendor data is returned under `output.data`. Signed callers can pass
`receipt_offset` to emit the encoded result as ordered `wrapper.result_chunk`
events.

## Safety and bounds

- Caller data cannot contain `${...}` secret templates.
- Requests use `raw_body`, exact JSON headers, and one CBSS-backed `x-api-key`.
- Search queries are capped at five and 200 characters each.
- Results are capped at five, total excerpts at 8,000 characters, and each
  result excerpt budget at 2,000 characters.
- The request-side bounds target less than 60 KB before the Runner's 65,536-byte
  result limit. Final canonical-CBOR responses are capped at 38,000 bytes. If a
  reply must be reduced, only excerpts are shortened and complete trailing
  results can be removed; identifiers and URLs stay exact. A reply that still
  cannot fit returns an explicit error.
- At most 16 requests are retained globally and six per Hire. Requests and
  completed results expire after bounded block windows.
- Results are owned by the submitting buyer and Hire. Public observability
  exposes metadata only.

The independent `parallel_routes.json` fixture pins the GA OpenAPI contract and
its `x-api-key` auth. The parity check fails if the live OpenAPI changes the
`POST /v1/search` method, drops or renames that auth, or drifts the request or
response schema. This actor claims parity only for its bounded Search surface,
not every Parallel API.

## Transport cap

The runner delivers the vendor reply to the actor over a hard transport cap
(~40-64 KB, the "40 KB cliff"). If a whole vendor response exceeds it the
continuation never resumes and the request parks until its deadline, then
errors; client-side bounding cannot help because `_limited_response` runs *after*
transport. The single `search` handler is the only response path, and it is kept
well under the cap by request-side bounds: at most five queries, five results,
and 8,000 total excerpt characters, which produce responses on the order of a
few KB up to roughly 15 KB. No handler here is expected to exceed the cap. The
excluded Parallel products (Task, FindAll, Extract, Chat, Responses) can return
much larger, unbounded payloads and are deliberately kept out for that reason.
No server-side field extraction is used: the runner's `extract_path` JSONPath
extractor is a stub that returns `{}`, so reduction must happen through the
request-side bounds above.

## API key

Create and manage keys at [platform.parallel.ai](https://platform.parallel.ai/).
Parallel currently provides eligible organizations with recurring monthly free
credits; check [current pricing](https://parallel.ai/pricing).

Store the key in a file. Do not place it in source or pass it directly on a
command line.

## Tests

From this directory, with `COWBOY_SDK_PATH` pointing at a current
`node/pvm/Lib` checkout:

```bash
uv run pytest
cowboy actor check search

uv run python tests/test_parallel_parity.py --offline

PARALLEL_API_KEY_FILE=/secure/parallel-key.txt \
  uv run python tests/test_parallel_comparison.py
```

The parity script checks the pinned fixture against Parallel's live OpenAPI;
pass `--offline` to skip that network check. The comparison script makes one
billable Search request, feeds that exact response through the wrapper
lifecycle, and requires exact output equality.

## Canyon

The complete paid Store flow requires a Canyon environment with runners and
CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
PARALLEL_API_KEY_FILE=/secure/parallel-key.txt \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

The demo deploys the entitled actor, stores and allow-lists the secret, creates
a Trading Post declaration and Hire, registers it, and validates the search
result through actor-filtered signed receipt chunks.

The Hire term defaults to `one-shot:cby:1.5` (override with `HIRE_TERM` /
`HIRE_AMOUNT`). PerCall (`per-call:cby:N`) is the intended production term but
is currently unimplemented on the node — the runner fail-closes every PerCall
lease (`PerCallSessionBindingUnimplemented`, pending the CIP-8 session binding),
so a PerCall hire deploys and hires cleanly yet every job under it is silently
denied and no result is ever written. Live verification must therefore use a
supported term (`free`, `one-shot:cby:N`, or `subscription:cby:N:BLOCKS`).
