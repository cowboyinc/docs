# Tavily Search Store wrapper

A hire-gated wrapper over Tavily's Search API. Registered Store Hires submit
bounded searches and read request-specific results through `get_result`. The
Tavily API key stays in CBSS as `TAVILY_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Tavily key, and a paid Trading Post Hire.

## Supported surface

The `search` handler maps to `POST https://api.tavily.com/search` using Bearer
authentication. It supports the current Search request surface:

- Query, search depth, chunks per source, maximum results, and topic
- Time range, start/end dates, published-date inclusion and filtering
- Generated answers, raw content, images, image descriptions, and favicons
- Included/excluded domains and include-domain filter/boost mode
- Country and language boosting, strict language filtering, exact matching,
  safe search, automatic parameters, and usage reporting
- Optional Tavily project, session, and anonymized human tracking headers

The actor caps queries at 400 characters, domains at 20 per list, and results at
10. This is a bounded subset of Tavily's larger vendor limits. Semantic date and
country validation remains Tavily's responsibility so upstream additions do not
require an actor upgrade.

Tavily Extract, Crawl, Map, Research, Usage, and Logs have distinct request,
billing, and lifecycle contracts and should be separate actors.

## Call contract

`search` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"search","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`.
Successful vendor data is under `output.data`. Signed callers can pass
`receipt_offset` to emit the encoded result as ordered `wrapper.result_chunk`
events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates.
- Requests use `raw_body`, an exact Bearer header, and one CBSS-backed key.
- The API key never enters actor payloads, continuations, storage, or results.
- Final canonical-CBOR responses are capped at 38,000 bytes. Only answer,
  content, raw-content, and image-description prose may be shortened; URLs,
  IDs, titles, and complete result rows remain exact or the request fails.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

The independent `tavily_routes.json` fixture pins the live official OpenAPI
contract. This actor claims parity for its documented bounded Search surface,
not every Tavily product.

## API key

Create a free key at [app.tavily.com](https://app.tavily.com/). Tavily's free
Researcher plan currently includes 1,000 monthly credits without a card.

Store the key in a file. Do not put it in source or pass it directly on a command
line.

## Tests

```bash
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
uv run pytest
cowboy actor check tavily
uv run python tools/check_tavily_parity.py --offline

TAVILY_API_KEY_FILE=/secure/tavily-key.txt \
  uv run python tools/live_tavily.py
```

The parity script checks the fixture against Tavily's live official OpenAPI
document; pass `--offline` to skip the network check. The comparison tool uses
one Search credit, feeds that exact vendor response through the wrapper
lifecycle, and requires exact output equality.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
TAVILY_API_KEY_FILE=/secure/tavily-key.txt \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

The demo deploys the entitled actor, stores and allow-lists the secret, creates
a Trading Post declaration and Hire, registers it, and validates Tavily output
through actor-filtered signed receipt chunks. It uses the deployer wallet as the
Store registrar because it does not accept a separate registrar signing key.
