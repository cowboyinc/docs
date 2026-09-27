# Reducto Parse Store wrapper

A hire-gated wrapper over Reducto's document-parse API. Registered Store Hires
submit a document URL and read a request-specific result through `get_result`.
The Reducto API key stays in CBSS as `REDUCTO_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Reducto key, and a paid Trading Post Hire.

## Supported surface

The `parse` handler maps to `POST https://platform.reducto.ai/parse` using
Bearer authentication. Inputs:

- `hire_id` (required) — the registered hire submitting the request
- `document_url` (required) — an `https://`, `http://`, `reducto://`, or
  `jobid://` URL to the document (public URL, presigned S3 URL, a `reducto://`
  handle from `/upload`, or a `jobid://` handle from a prior parse)

### Transport-cap shape

A full parse result routinely exceeds the runner→actor transport cap
(~40–64KB) and would silently **park** the continuation. The runner has no
working server-side reducer, so this wrapper forces
`settings.force_url_result: true`: Reducto then returns a small **`UrlResult`**
(presigned S3 URL + `result_id`) instead of inline chunks, which always fits
the cap. The buyer fetches the full artifact from the presigned URL itself.

`get_result` returns, under `output.data`:

```json
{"result_type":"url","result_url":"https://…s3…?X-Amz-Signature=…","result_id":"…","job_id":"…","num_pages":5,"credits":5.0,"duration":1.23}
```

The result URL is an AWS S3 presigned URL (signature embedded in the query
string) and short-lived. It is public chain/result data: anyone holding the URL
can fetch the artifact without `REDUCTO_API_KEY` until the URL expires. Fetch it
promptly. This is also why sync `/parse` is used rather than `/parse_async`,
whose result requires an API-key-authenticated `GET /job/{id}` that couldn't be
handed to the buyer.

### Excluded because they would park or cross a credential

- **Inline `full` parse**: exceeds the transport cap; an unexpected `full`
  reply is classified an error, never returned inline.
- **`/parse_async`**: result needs an API-key-authenticated `GET /job/{id}`.
- **`/extract`**: unbounded, schema-dependent inline size; can park.
- **`/split`, `/pipeline`, `/upload`**: large inline content or a stateful
  ingest step outside this bounded parse surface.

## Call contract

`parse` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"parse","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`.
Signed callers can pass `receipt_offset` to emit the encoded result as ordered
`wrapper.result_chunk` events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates — otherwise the
  runner could paste the live key into the outbound request or an echoed error.
- All inputs are public chain data. The continuation carries only the request
  id — never the key, which rides the job solely as a `${REDUCTO_API_KEY}`
  reference resolved runner-side.
- Requests use `raw_body`, an exact Bearer header, an explicit `User-Agent`,
  and one CBSS-backed key. The key never enters payloads, continuations,
  storage, or results.
- Final canonical-CBOR responses are capped at 38,000 bytes; a non-JSON or
  vendor-error reply is truncated to 500 bytes and never labeled success.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

### Billing / idempotency caveat

Parse is **billable** and Reducto's `/parse` has no idempotency key, so a
runner-level retry could re-parse and double-bill; dispatch is one submit per
request (same-transaction cid dedup + a single continuation).

The independent `reducto_routes.json` fixture pins the parse endpoint, auth,
and the `settings.force_url_result` contract for this actor's bounded surface.

## API key

Create a key at [reducto.ai](https://reducto.ai/) / the Reducto dashboard. Store
it in a file. Do not put it in source or pass it directly on a command line.

## Tests

```bash
cd examples/gallery/reducto-parse
uv run pytest
cowboy actor check reducto

python3 tests/test_reducto_parity.py          # offline drift
python3 tests/test_reducto_parity.py --live   # probe published OpenAPI

REDUCTO_API_KEY_FILE=/secure/reducto-key.txt \
  python3 tests/test_reducto_comparison.py \
  --document-url "https://pdfobject.com/pdf/sample.pdf"
```

The parity script checks the fixture against the actor; `--live` additionally
verifies Reducto's published OpenAPI. The comparison script parses one real
document and feeds that exact vendor reply through the wrapper lifecycle,
requiring exact output equality.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
REDUCTO_API_KEY_FILE=/secure/reducto-key.txt \
REDUCTO_DOCUMENT_URL="https://pdfobject.com/pdf/sample.pdf" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

## Demo Scope

Deploys the entitled actor, stores and allow-lists the secret, creates a
Trading Post declaration and Hire, registers it, and parses one document,
reassembling the result from signed receipt chunks. Requires live Canyon
infrastructure and a Reducto key.

## Versioning

The `parse` acceptance-record + `get_result` contract is the v1.0.0 surface. Any
change to the returned shape or the supported input fields is a breaking version
bump and requires a redeploy plus a fresh Trading Post declaration.
