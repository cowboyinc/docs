# Anchor Browser Usage Store wrapper

A hire-gated wrapper over Anchor Browser's account API. Registered Store Hires
submit a bounded account-usage query and read request-specific results through
`get_result`. The Anchor API key stays in CBSS as `ANCHOR_API_KEY` and rides the
runner job only as a `${ANCHOR_API_KEY}` reference in the `anchor-api-key`
header.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, an Anchor Browser key, and a paid Trading Post Hire.

## Why only a usage meter, and what is excluded

Anchor Browser is a cloud-browser platform, and almost all of its surface is a
poor fit for a bounded, synchronous, on-chain wrapper. The runner-to-actor
result transport has a hard cap (~40-64 KB): if a vendor response can exceed it,
the continuation never resumes and the request parks on chain. Client-side
trimming cannot help, because it runs inside the continuation, after transport.
So this wrapper deliberately exposes only the one endpoint that returns a small,
fixed, synchronous JSON body with no session and no credential:

- `usage` maps to `GET https://api.anchorbrowser.io/v1/billing` using the
  `anchor-api-key` header. It returns the account credit/usage summary
  (`data.credits`, `data.credits_used`, `tier`, ...). Optional `granularity`
  (`day` | `week` | `month`) adds a bounded usage series over Anchor's default
  30-day window.

Everything else in Anchor's API is excluded, and why:

| Endpoint(s) | Why excluded |
| --- | --- |
| `POST /v1/tools/fetch/webpage` (Web Unlocker) | returns raw full-page `text/html` with no server-side length bound; routinely exceeds the transport cap and parks the continuation. Not JSON. |
| `POST /v1/screenshot`, `GET .../pdf`, recordings | binary / large media, unbounded and non-JSON. |
| `POST /v1/sessions`, `/v1/sessions/async`, `GET /v1/sessions/{id}`, all OS-control routes | return a live `cdp_url` WebSocket credential (public on chain) and need a persistent socket an on-chain actor cannot hold; also account-global (cross-tenant). |
| `POST /v1/perform-web-task`, `/v2/tasks/{id}/run` | agentic and long-running (return a `workflow_id` to poll); may exceed the runner synchronous window and the result is potentially unbounded. |
| Profiles, identities, applications, auth-flows | persistent account-global identity/credential state; not hire-isolated, and some routes mint identity tokens. |
| Webhooks, extensions, certificates, integrations | account-global configuration; webhook creation returns a signing secret. |
| `GET /v1/events/{name}` | blocking wait-for-event; exceeds the synchronous window. |
| `GET /v1/billing` with `hour` granularity or a custom window | unbounded usage time-series; only day/week/month over the default window is exposed. |

The honest summary: Anchor's *core* browsing capabilities are transport-cap,
socket, and credential constrained. A fetch/extract or agentic-task capability
would require a server-side runner MCP tool that creates, uses, and tears down a
session in one call and returns only a bounded result, so no page HTML or live
session token ever round-trips through the actor. That tool does not exist for
Anchor today, so this wrapper ships the safe bounded usage meter only.

## Call contract

`usage` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"usage","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`.
Successful vendor data is under `output.data`. Signed callers can pass
`receipt_offset` to emit the encoded result as ordered `wrapper.result_chunk`
events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates.
- The `usage` call is a `GET`: the runner job omits `body` and `Content-Type`
  entirely (a GET job carrying a body is rejected on chain as E1604).
- The API key never enters actor payloads, continuations, storage, or results;
  the continuation carries only the request id.
- Final canonical-CBOR responses are capped at 38,000 bytes and safely trimmed.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

The independent `anchor_routes.json` fixture pins the endpoint, auth header, and
transport bounds. `test_anchor_parity.py` fails if the actor's literals drift
from that fixture or if Anchor's public docs stop documenting `GET /v1/billing`
with the `anchor-api-key` header.

## Versioning

This is version `1.0.0`. The return contract (acceptance record + `get_result`
with vendor data under `output.data`) is the public API; changing it is a
breaking version bump that requires a redeploy and re-declaration.

## API key

Create a key in your Anchor Browser dashboard. Store it in a file. Do not put it
in source or pass it directly on a command line.

## Simulate Locally

The `anchor` target compiles the same project entrypoint that the demo deploys.
From this directory, with `COWBOY_SDK_PATH` pointing to a Node SDK checkout:

```bash
uv run pytest
cowboy actor check anchor
```

## Tests

```bash
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
python3 -m pytest -q examples/gallery/anchor-browser

python3 examples/gallery/anchor-browser/tests/test_anchor_parity.py --offline

ANCHOR_API_KEY_FILE=/secure/anchor-key.txt \
  python3 examples/gallery/anchor-browser/tests/test_anchor_comparison.py
```

The parity script checks the actor against the pinned fixture and Anchor's live
public docs; pass `--offline` to skip the network check. The comparison script
makes one real `GET /v1/billing` call, feeds that exact vendor response through
the wrapper lifecycle, and requires exact output equality.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
ANCHOR_API_KEY_FILE=/secure/anchor-key.txt \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

The demo deploys the entitled actor, stores and allow-lists the secret, creates
a Trading Post declaration and Hire, registers it, and validates Anchor usage
output through actor-filtered signed receipt chunks.
