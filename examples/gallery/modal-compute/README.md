# Modal Compute Store wrapper

A hire-gated wrapper over invocation of **already-deployed Modal web endpoints**
(`https://<workspace>--<label>.modal.run`) protected by Modal Proxy Auth Tokens.
Registered Store Hires invoke a registrar-approved endpoint with a bounded JSON
payload and read request-specific results through `get_result`. The Modal token
pair stays in CBSS as `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Modal token pair, a deployed endpoint, and a paid
Trading Post Hire.

## Supported surface

The `invoke` handler makes one synchronous request to a registered
`*.modal.run` endpoint, sending the Modal Proxy Auth headers `Modal-Key` /
`Modal-Secret`. It supports:

- `endpoint` (required) — the **label** of a registrar-registered endpoint
- `payload` (optional) — a bounded JSON object; POST sends it as the body,
  GET appends bounded query params

- Buyers select a registered **label**, never a URL — only the Store
  registrar can register endpoints, and every URL is validated to a
  `*.modal.run` host, so the token pair can never be steered to an attacker
  host.
- Only pre-deployed endpoints are wrapped, not Modal's Sandboxes /
  deploy-then-invoke: that flow produces unbounded output and has no single
  bounded credential-free HTTP call.
- `invoke` is treated as **mutating**; a stable `Idempotency-Key` (from the
  request's continuation id) lets a well-behaved endpoint dedup a runner
  retry — Modal's proxy itself does not dedup (residual retry risk).

## Call contract

`invoke` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"invoke","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`. On
success the endpoint's JSON reply is placed verbatim under `output.data`. Signed
callers can pass `receipt_offset` to emit the encoded result as ordered
`wrapper.result_chunk` events.

`runner.http` exposes only the response body, not the HTTP status, so an empty
or non-object reply carries no success signal and is classified as an error —
never a false `ok`.

Application JSON remains opaque data: fields such as `raw`, `raw_text`,
`error`, and `message` are returned verbatim. The exact two-field object
`{raw: string, content_type: string}` is reserved for the runner's transport
envelope and is classified as an error, including when `content_type` says
`application/json`. Add any application field to make a JSON object distinct.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates (would let the
  runner paste the live token into an outbound request).
- All inputs are public chain data. Never send passwords, tokens, or regulated
  data through this actor.
- POST uses `raw_body` (exact bytes); GET omits the body entirely (avoids the
  node's E1604 job-schema rejection).
- The token pair never enters actor payloads, continuations, storage, or results.
- Final canonical-CBOR responses are capped at 38,000 bytes; a reply larger than
  the runner transport cap never arrives and the request parks instead (so only
  bounded endpoints should be registered). Vendor error text is truncated to 500
  bytes and never labeled success.
- At most 16 requests are retained globally and six per Hire; at most 16 endpoints
  may be registered.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire; public status is metadata-only.
- Runner callbacks use the private continuation resume path and exact Hire pins.

The independent `modal_routes.json` fixture pins Modal's proxy-auth invoke
contract (host suffix, auth headers, method set).

## Tokens

Create a Proxy Auth Token in the Modal dashboard (a token-id / token-secret
pair). Store each in a file. Do not put them in source or pass them directly on
a command line.

## Tests

```bash
cd examples/gallery/modal-compute
uv run pytest
cowboy actor check modal

python3 tests/test_modal_parity.py          # offline drift
python3 tests/test_modal_parity.py --live   # probe published docs

MODAL_TOKEN_ID_FILE=/secure/modal-id.txt \
MODAL_TOKEN_SECRET_FILE=/secure/modal-secret.txt \
  python3 tests/test_modal_comparison.py \
  --url https://your-workspace--your-fn.modal.run
```

`test_modal_parity.py` checks the fixture against the actor (`--live` also
probes Modal's docs); `test_modal_comparison.py` invokes one real endpoint and
diffs the wrapper output against the direct reply.

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
MODAL_ENDPOINT_URL=https://your-workspace--your-fn.modal.run \
MODAL_TOKEN_ID_FILE=/secure/modal-id.txt \
MODAL_TOKEN_SECRET_FILE=/secure/modal-secret.txt \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

The demo deploys the entitled actor, stores and allow-lists the token pair,
registers one `*.modal.run` endpoint, creates a Trading Post declaration and
Hire, registers it, and invokes the endpoint through actor-filtered signed
receipt chunks.

## Versioning

The `invoke` acceptance-record + `get_result` contract is the v1.0.0 surface. Any
change to the returned shape or the supported input fields is a breaking version
bump and requires a redeploy plus a fresh Trading Post declaration.
