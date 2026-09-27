# Exa Store Wrapper

The wrapper provides Exa Search, Contents, Answer, Agents, Batches, Monitors,
Websets, webhooks, events, and imports for a registered General Store Hire.
Use one deployment for **one trusted tenant**. All registered Hires share the
same Exa account and can access, change, or delete its resources through the
full API. This wrapper does not isolate Exa resources between unrelated buyers.

Both `search` and `api` return a request ID. The same buyer later calls
`get_result` with that ID and Hire to receive the result.

## Setup

Store the key as `EXA_API_KEY`. Deploy with `http.fetch` for `api.exa.ai`,
`secrets.read` for `<deployer>:EXA_API_KEY`, and `storage.kv`. Allow the Actor
in the CBSS policy, declare it on Trading Post, create a Hire, and call
`register_hire` with the finalized Hire data.

```bash
cd examples/gallery/exa-search
KEYFILE=/secure/path/wallet-key \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
EXA_API_KEY_FILE=/secure/path/exa-key.txt \
./demo.sh
```

Deploy version 2.1.0 as a **new Actor**. Its Hire and request storage differs
from earlier versions; an in-place code upgrade is not supported. Recreate
and register the Hires for the new deployment. The demo follows this fresh
setup path and does not grant `sys.upgrade`.

This example is outside the default gallery sweep. The demo writes a secret,
deploys and funds an Actor, creates a Hire, and uses Exa credits.

CBSS releases the plaintext API key to the selected Runner, which sends it to
Exa. The Runner and Exa are within the secret-handling trust boundary. Actor
state and results are public chain data; caller checks do not make them private.
This includes webhook signing secrets returned by Exa. Do not use this wrapper
for results that must stay confidential. Caller-supplied `${...}` templates are
rejected before dispatch to prevent Runner secret substitution in user fields.

## Use

`search` provides bounded search results:

```json
{"hire_id": 42, "query": "Cowboy actor network", "num_results": 5}
```

It also supports `search_type`, `category`, `include_domains`, `exclude_domains`,
`start_published_date`, `end_published_date`, and `user_location`. `capability`
returns the supported values and limits.

`api` exposes the 64 fixed routes in `API_OPERATIONS`. Pass `operation`,
`hire_id`, Exa request fields, path fields such as `id`, and GET parameters
under `query_params`. Streaming is disabled.

```json
{"hire_id": 42, "operation": "contents", "urls": ["https://example.com"], "text": true}
```

```json
{"hire_id": 42, "operation": "list_monitors", "query_params": {"limit": 10}}
```

Both handlers return acceptance, not the vendor result:

```json
{"ok": true, "request_id": 7, "status": "pending", "poll_handler": "get_result"}
```

Call `get_result` from the same buyer with the same Hire:

```json
{"hire_id": 42, "request_id": 7}
```

The response has `status: pending`, `ok`, or `error`. A pending response has
`deadline_height`. A final response has `expires_height`. Successful search
responses include `query` and `results`; API responses include `operation` and
`data`. API output above 28,000 serialized JSON bytes is returned as a bounded
`{"truncated": true, "raw_json": "..."}` value. That prefix is not complete JSON.

The Runner callback is a separate transaction. A caller must retain the request
ID and poll; the callback cannot return data to the original buyer call.

## Limits and result retention

- At most 512 Hires are registered, each in its own storage value. Registration
  is immutable; an exact retry is safe, including when capacity is full.
- `search` and `api` share 16 request slots. Separate transactions from one buyer
  can run concurrently. A second call with the same pending continuation ID is
  rejected because the SDK derives that ID from transaction, sender, Actor, and
  handler. This check covers both entrypoints.
- A pending request expires at 150 blocks after acceptance. A later request or
  result read deletes its continuation and records an error. A callback at or
  after that deadline also records an error. Cleanup is demand-driven; it does
  not cancel the Runner job.
- Completed results and errors are retained for 150 blocks after their terminal
  state is recorded. New work cannot remove them before `expires_height`.
  When all slots are protected, new requests are rejected. At or after expiry,
  a new request can reclaim a slot. Complete all reads before that height.
- Search fields are bounded before storage. The complete encoded result is
  limited to 40,000 bytes. If the response envelope exceeds that limit, the
  Actor stores a bounded error instead of an unreadable result. Request shaping reduces the risk of exceeding the
  Runner response limit, but cannot bound unknown vendor fields before receipt.
  An oversized Runner response can still produce no callback.

The Actor supplies the registered Hire pins to the Runner. Node checks the
Active Hire again before dispatch. A paused or revoked Hire can therefore
produce no callback; the request deadline supplies the terminal local error.
Node selection timeouts, the Runner HTTP timeout, and the Actor deadline are
separate controls. This Actor does not pass the unused `timeout_ms` argument.

## Buyer tools

`actors/buyer/main.py` is a local Actor example that uses SDK `call()` for both steps.
Current Trading Post Hire creation binds the Hirer to the signed transaction
sender; the Actor SDK has no Hire-creation host call. The demo therefore uses
the EOA signer as the live buyer. The Buyer Actor tests are local SDK proof.

The current CLI does not display handler return bytes. The demo obtains the
request ID from its transaction receipt and makes signed `get_result` calls
with `receipt_offset` and `receipt_length`. Each `wrapper.result_chunk` event
contains at most 4,015 data bytes within Node's 4,096-byte event limit. Each call
checks the buyer and Hire. The client checks IDs, offsets, lengths, and totals
before decoding the assembled CBOR result.

For operator diagnosis only, `BUYER=0x... ./show_results.sh <actor> <request-id>
<rpc-url>` reads transparent storage. It is not proof of authenticated delivery.

## Verification

Use Python 3.13 or later and the SDK from the Node revision under test:

```bash
export COWBOY_SDK_PATH=/path/to/node/pvm/Lib
uv run pytest
cowboy actor check exa
cowboy actor check buyer
```

These are offline checks. They cover the SDK continuation flow, caller/Hire
isolation, capacity, retention between chunk reads, deadlines, concurrent
requests, byte limits, receipt parsing, and the 64 operation mappings. They do
not prove Node VM execution, a deployment, or a live Exa result.

`tools/check_exa_parity.py` checks the pinned route fixture by default. Use `--schema`
with a downloaded schema, or `--url https://exa.ai/docs/exa-spec.json` for an
explicit network check. The fixture records the public source and its hash.

The optional live success matrix submits signed transactions and uses Exa
credits. It requires a fresh 2.1.0 deployment and registered Hire. Each case must
succeed both directly and through the Actor. Matching vendor errors fail the
matrix. It checks five representative operations, not all 64 live routes.

```bash
python3 examples/gallery/exa-search/tools/check_exa_canyon.py \
  --rpc https://rpc.canyon.cowboylabs.net --actor <address> --hire-id <id> \
  --wallet-key /secure/path/wallet-key --key-file /secure/path/exa-key.txt
```

The demo and live matrix use `tools/exa_client.py` to read the accepted request.
Receipt polling and chunk delivery share one 180-second deadline. After a
client timeout, keep the request ID and use `tools/exa_client.py --request-id <id>`
with the same connection arguments to read it again. Do not submit a replacement
request merely because the client stopped waiting.
