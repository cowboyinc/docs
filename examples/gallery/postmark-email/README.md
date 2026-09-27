# Postmark Email Store wrapper

A hire-gated wrapper over Postmark's Send Email API. Registered Store Hires
submit bounded transactional emails and read request-specific results through
`get_result`. The Postmark server token stays in CBSS as
`POSTMARK_SERVER_TOKEN`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Postmark server token, a verified sender signature,
and a paid Trading Post Hire.

## Supported surface

The `send` handler maps to `POST https://api.postmarkapp.com/email`. Postmark
authenticates with an `X-Postmark-Server-Token` header — **not** Bearer — so the
key rides the job only as a `${POSTMARK_SERVER_TOKEN}` reference in that header.
The handler supports the single-message send core:

- `From` (required) — a bare email or a friendly `Name <email>` form
- `To` (required) — one address, a comma-separated string, or a list
- `Subject` (required)
- `HtmlBody` and/or `TextBody` — at least one is required
- `Cc`, `Bcc`, `ReplyTo` — each a single address, comma-separated string, or list
- `MessageStream` — optional Postmark stream id (e.g. `outbound`)

Each recipient field is validated per-address (header-injection safe) and then
joined into the single comma-separated string Postmark expects. `To + Cc + Bcc`
may not exceed 50 recipients.

Send is **mutating**, and **Postmark has no server-side idempotency** — it
ignores the `Idempotency-Key` header (which the actor still sends, harmlessly, in
case support ever lands). So a runner retry of the send **may deliver a second
email**; the actor's same-transaction cid dedup only guards duplicates within one
transaction. Mitigate buyer-side via Postmark's delivery webhooks / bounce API.

Batch send, templates, attachments, tags, custom headers, and the bounce/stats
lifecycle are not exposed in v1; they carry distinct validation and billing and
belong in their own handlers.

## Call contract

`send` returns immediately with an acceptance record:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"send","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`. On
success the vendor data is `{"MessageID": "<id>", "ErrorCode": 0, ...}` under
`output.data`. Signed callers can pass `receipt_offset` to emit the encoded
result as ordered `wrapper.result_chunk` events.

## The Postmark error gotcha

Postmark reports failures as an HTTP `200`/`422` body carrying a **nonzero
`ErrorCode`** and a `Message` (a success is `ErrorCode` 0 with a `MessageID`).
The actor therefore classifies by `ErrorCode`, never by HTTP status: a nonzero
code — even one returned alongside a `MessageID` — is surfaced as
`status:"error"` and is never labeled a false success. Non-JSON and raw replies
are errors too.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates. This is critical
  for email: a `${POSTMARK_SERVER_TOKEN}` in a subject or body would otherwise
  let the runner paste the live token into an outbound message.
- All inputs are public chain data. Never send passwords, tokens, or regulated
  data through this actor.
- Addresses reject CR/LF, control characters, and embedded commas
  (header-injection and recipient-list safety).
- Requests use `raw_body` (exact JSON, `allow_nan=False`), the bare token
  header, and one CBSS-backed key.
- The server token never enters actor payloads, continuations, storage, or
  results. The continuation carries only the `request_id`.
- Final canonical-CBOR responses are capped at 38,000 bytes; vendor error text
  is truncated to 500 bytes and never labeled success.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public `last_output` is
  metadata-only (`ok` + `operation`) and never carries vendor data.
- Runner callbacks use the private continuation resume path and exact Hire pins.

### Transport cap

Postmark's send response is a small JSON object (`{MessageID, ErrorCode,
SubmittedAt, To, Message}`, well under 1 KB), so the whole reply sits far below
the runner-result transport cap (~40–64 KB). This handler has **no parking
risk** from an oversized reply; the encoded-result budget check exists only as a
belt-and-suspenders guard.

The independent `postmark_routes.json` fixture pins Postmark's published send
contract (endpoint, token auth, required fields, and the nonzero-`ErrorCode`
failure signal). This actor claims parity for its documented bounded send
surface, not every Postmark product.

## Server token

Create a server token in a Postmark server's *API Tokens* tab and verify a
sender signature or domain. Store the token in a file. Do not put it in source
or pass it directly on a command line. For a dry run, Postmark's
`POSTMARK_API_TEST` token accepts any well-formed request and returns a canned
success without sending.

## Tests

```bash
cd examples/gallery/postmark-email
uv run pytest
cowboy actor check postmark

python3 tests/test_postmark_parity.py          # offline drift
python3 tests/test_postmark_parity.py --live   # probe published reference

POSTMARK_SERVER_TOKEN_FILE=/secure/postmark-token.txt \
  python3 tests/test_postmark_comparison.py \
  --from "you@your-verified-domain.com" --to "recipient@example.com"

# dry run against Postmark's test token (no email is sent):
python3 tests/test_postmark_comparison.py --test-token
```

The parity script checks the fixture against the actor; `--live` additionally
probes Postmark's published HTML reference for stable contract markers. The
comparison script sends one real email, feeds that exact vendor reply through
the wrapper lifecycle, and requires exact output equality.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
POSTMARK_SERVER_TOKEN_FILE=/secure/postmark-token.txt \
POSTMARK_FROM="you@your-verified-domain.com" \
POSTMARK_TO="recipient@example.com" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

The demo deploys the entitled actor, stores and allow-lists the secret, creates
a Trading Post declaration and Hire, registers it, and sends one email through
actor-filtered signed receipt chunks.

## Versioning

The `send` acceptance-record + `get_result` contract is the v1.0.0 surface. Any
change to the returned shape or the supported input fields is a breaking version
bump and requires a redeploy plus a fresh Trading Post declaration.
