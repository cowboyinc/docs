# Resend Email Store wrapper

A hire-gated wrapper over Resend's Send Email API. Registered Store Hires submit
bounded emails and read request-specific results through `get_result`. The
Resend API key stays in CBSS as `RESEND_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Resend key, a verified sender domain, and a paid
Trading Post Hire.

## Supported surface

The `send` handler maps to `POST https://api.resend.com/emails` using Bearer
authentication. It supports the current send request core:

- `from` (required) — a bare email or a friendly `Name <email>` form
- `to` (required) — one address or a list, up to 50 recipients
- `subject` (required)
- `html` and/or `text` — at least one is required
- `cc`, `bcc`, `reply_to` — each a single address or a list

Send is **mutating**. The actor attaches a stable `Idempotency-Key` derived from
the request's continuation id, so a runner retry dedups vendor-side (Resend
honors the key for 24 hours) instead of sending a second email.

Attachments, tags, custom headers, scheduling, templates, and topics are not
exposed in v1; they carry distinct validation and billing and should be added
deliberately. Retrieving, listing, updating, and cancelling emails are separate
read/lifecycle surfaces and belong in their own handlers.

## Call contract

`send` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"send","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`. On
success the vendor data is `{"id": "<email id>"}` under `output.data`. Signed
callers can pass `receipt_offset` to emit the encoded result as ordered
`wrapper.result_chunk` events.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates. This is critical
  for email: a `${RESEND_API_KEY}` in a subject or body would otherwise let the
  runner paste the live key into an outbound message.
- All inputs are public chain data. Never send passwords, tokens, or regulated
  data through this actor.
- Addresses reject CR/LF, control characters, and embedded commas
  (header-injection and recipient-list safety — one list element is one
  address, so a comma cannot smuggle extra recipients past the 50 cap).
- Requests use `raw_body`, an exact Bearer header, and one CBSS-backed key.
- The API key never enters actor payloads, continuations, storage, or results.
- Final canonical-CBOR responses are capped at 38,000 bytes; vendor error text
  is truncated to 500 bytes and never labeled success.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

The independent `resend_routes.json` fixture pins Resend's published OpenAPI
contract (spec 1.5.1). This actor claims parity for its documented bounded send
surface, not every Resend product.

## API key

Create a key at [resend.com](https://resend.com/) and verify a sending domain.
Store the key in a file. Do not put it in source or pass it directly on a
command line.

## Tests

```bash
cd examples/gallery/resend-email
uv run pytest
cowboy actor check resend

python3 tests/test_resend_parity.py          # offline drift
python3 tests/test_resend_parity.py --live   # probe published OpenAPI

RESEND_API_KEY_FILE=/secure/resend-key.txt \
  python3 tests/test_resend_comparison.py \
  --from "onboarding@resend.dev" --to "delivered@resend.dev"
```

The parity script checks the fixture against the actor; `--live` additionally
verifies Resend's published OpenAPI. The comparison script sends one real email,
feeds that exact vendor reply through the wrapper lifecycle, and requires exact
output equality.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
RESEND_API_KEY_FILE=/secure/resend-key.txt \
RESEND_FROM="you@your-verified-domain.com" \
RESEND_TO="recipient@example.com" \
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
