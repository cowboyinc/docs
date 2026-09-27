# LlamaParse Store wrapper

A hire-gated wrapper over LlamaParse's document-parse API (LlamaIndex Cloud).
Registered Store Hires submit a document URL, then retrieve a request-specific
result through `get_result`. The LlamaParse API key stays in CBSS as
`LLAMAPARSE_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a LlamaParse key, and a paid Trading Post Hire.

## Supported surface

LlamaParse is an asynchronous submit->poll API, so the wrapper exposes two
hire-gated handlers, each a single Runner call that returns an acceptance
record:

- `parse` — `POST https://api.cloud.llamaindex.ai/api/v2/parse` (Bearer auth).
  Inputs: `hire_id` (required) and `document_url` (required; an `https://` or
  `http://` URL to the document). It submits the job and its `get_result`
  returns `{job_id, vendor_status}`.
- `fetch_result` — `GET https://api.cloud.llamaindex.ai/api/v2/parse/{job_id}?expand=markdown_content_metadata`.
  Inputs: `hire_id` and `request_id` (the *parse* submission's request id). It
  retrieves the finished job and its `get_result` returns
  `{ready, result_type, result_url, size_bytes}`.

### Why this shape fits the transport cap

A parsed document's full markdown/text/json routinely exceeds the runner→actor
transport cap (~40–64KB). If the whole result crossed that cap the
`@runner.continuation` would never resume and the request would silently
**park** (the submit transaction still reports success, but `get_result` stays
`pending` until its deadline, then errors). The runner cannot reduce the body
server-side (its `extract_path` JSONPath extractor is a stub returning `{}`), and
client-side bounding runs *inside* the continuation — after transport — so it
never sees an oversized reply.

So the wrapper never asks LlamaParse for the inline result. `fetch_result` uses
`expand=markdown_content_metadata`, which returns a small **presigned S3
download URL** (plus `size_bytes` / `exists`) **instead of** the markdown itself.
The reply the runner must deliver is therefore always metadata + a URL and
reliably fits the cap. The buyer downloads the full parsed markdown from the
presigned URL itself. This is the same lesson as the Reducto wrapper's
`force_url_result`.

### The result URL is a public bearer capability

The returned URL is an **AWS S3 presigned URL**: possession of its embedded
query signature grants temporary download access without `LLAMAPARSE_API_KEY`.
The Actor stores that bearer capability in public consensus state, so this
wrapper is suitable only for documents safe for public delivery. The
wrapper additionally refuses to return any result URL whose host is the
key-authenticated result host `api.cloud.llamaindex.ai` — only the presigned
download URL drawn from the response is returned. Fetch it promptly; presigned
URLs expire (treat the TTL as on the order of an hour).

### Excluded because they would park or cross a credential

- **Inline v2 results** (`expand=markdown`, `text`, `items`): inline content
  routinely exceeds the transport cap and parks. Only the `*_content_metadata`
  presigned-URL expand is used.
- **v1 inline result endpoints** (`GET /api/v1/parsing/job/{id}/result/markdown|json|text`):
  return large inline content that parks. v1 has no presigned URL for the primary
  markdown result, so it is not used.
- **File upload / multipart** (`/api/v1/parsing/upload` file body): only remote
  `document_url`s are accepted (a buyer holds no key to upload directly).
- **Webhooks, batch, and job-listing endpoints**: out of this bounded parse
  surface.

## Call contract

`parse` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"parse","status":"pending","poll_handler":"get_result"}
```

The buyer polls `get_result(request_id, hire_id)` until it shows
`output.data.job_id`. Then the buyer calls `fetch_result(hire_id, request_id)`
with that *parse* request id; that returns its own acceptance record and its own
`request_id`. The buyer polls `get_result` for the fetch request:

- `{"output":{"data":{"ready":false,"vendor_status":"PENDING"}}}` — the job is
  still parsing; call `fetch_result` again after a short wait.
- `{"output":{"data":{"ready":true,"result_type":"markdown","result_url":"https://…s3…?X-Amz-Signature=…","size_bytes":45678}}}`
  — download the markdown from `result_url`.

Referencing the *parse request id* (not a raw job id) is what binds the fetch to
the submitting buyer and hire: `fetch_result` verifies the submit request's
`buyer == sender` and `hire_id`, so one hire can never fetch another hire's job.
Signed callers can pass `receipt_offset` to `get_result` to emit the encoded
result as ordered `wrapper.result_chunk` events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates. A
  `${LLAMAPARSE_API_KEY}` inside `document_url` would otherwise let the runner
  paste the live key into the outbound request or an echoed error.
- All inputs are public chain data. The continuation carries only the request id
  — never the key, which rides the job solely as a `${LLAMAPARSE_API_KEY}`
  reference resolved runner-side.
- The submit uses `raw_body`, an exact Bearer header, an explicit `User-Agent`,
  and one CBSS-backed key. The result GET omits the body entirely (a GET carrying
  a body is rejected by the node as **E1604**). The key never enters actor
  payloads, continuations, storage, or results.
- Final canonical-CBOR responses are capped at 38,000 bytes; a non-JSON or
  vendor-error reply is truncated to 500 bytes and never labeled success.
- At most 16 ordinary requests are retained globally and six per Hire. One
  reusable result fetch per active parse makes the absolute receipt bounds 32
  globally and 12 per Hire.
- Request receipts expire after bounded windows. The compact buyer/Hire/job
  binding survives nonterminal work for at most 7,200 blocks.
- Results belong to the submitting buyer and Hire; public status is metadata-only
  (no result URL, job id, or parsed content in `get_last_output`).
- Runner callbacks use the private continuation resume path and exact Hire pins.

### Billing / idempotency caveat

Parse is **billable** (consumes LlamaParse credits). LlamaParse v2 `/parse`
exposes no verified idempotency key, so the wrapper cannot dedup vendor-side:
dispatch is one submit per request (same-transaction cid dedup + a single
continuation), but a runner-level retry of the job could re-submit and
double-bill. This is documented rather than papered over with an ignored header.
If v2 is later confirmed to honor an `Idempotency-Key`, add one keyed to the
request id.

The independent `llamaparse_routes.json` fixture pins the endpoints, auth, the
`source_url` submit field, and the `expand=markdown_content_metadata`
presigned-result contract. This actor claims parity for its documented bounded
parse surface, not every LlamaParse product.

### Contract items still to verify against the live API

The v2 REST contract was pinned from LlamaParse's published docs; the v2 parse
endpoints are not fully present in the public `/api/openapi.json` (v1 is). Before
promoting past local evidence, confirm live: (1) `POST /api/v2/parse` accepts a
remote `source_url`; (2) the exact `expand` param and the presigned-URL response
path (`result_content_metadata.md.presigned_url`); (3) the parse-job status
vocabulary. `test_llamaparse_parity.py --live` and `test_llamaparse_comparison.py`
(both require a key) are the checks for this.

## API key

Create a key at [cloud.llamaindex.ai](https://cloud.llamaindex.ai/) (it starts
with `llx-`). Store it in a file. Do not put it in source or pass it directly on
a command line.

## Tests

```bash
cd examples/gallery/llamaparse
export COWBOY_SDK_PATH=/path/to/full-cowboy/node/pvm/Lib
uv run pytest
cowboy actor check parse

uv run python tests/test_llamaparse_parity.py          # offline drift
uv run python tests/test_llamaparse_parity.py --live   # probe published OpenAPI

LLAMAPARSE_API_KEY_FILE=/secure/llamaparse-key.txt \
  uv run python tests/test_llamaparse_comparison.py \
  --document-url "https://pdfobject.com/pdf/sample.pdf"
```

The parity script checks the fixture against the actor; `--live` additionally
probes LlamaParse's published OpenAPI. The comparison script parses one real
document and feeds those exact vendor replies through the wrapper lifecycle,
requiring exact output equality. Because the wrapper fetches a presigned URL the
vendor reply is small, so the comparison is representative of the on-chain reply
size (an inline markdown result would exceed the transport cap and never resume).

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
LLAMAPARSE_API_KEY_FILE=/secure/llamaparse-key.txt \
LLAMAPARSE_DOCUMENT_URL="https://pdfobject.com/pdf/sample.pdf" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

## Demo Scope

The runnable walkthrough deploys the entitled actor, stores and allow-lists the
secret, creates a Trading Post declaration and Hire, registers it, submits one
document, polls the job to completion, and reassembles the result from
actor-filtered signed receipt chunks. It requires live Canyon infrastructure and
a LlamaParse key, so it is not part of the offline sweep.

## Versioning

The `parse`/`fetch_result` acceptance-record + `get_result` contract is the
v1.0.0 surface. Any change to the returned shapes or the supported input fields
is a breaking version bump and requires a redeploy plus a fresh Trading Post
declaration.
