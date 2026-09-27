# Firecrawl Store Wrapper

A Hire-gated wrapper for Firecrawl v2. The vendor key stays in CBSS as
`FIRECRAWL_API_KEY`. The Runner inserts it into the vendor request. One vendor
account serves all Hires on this Actor.

This example is outside the default gallery sweep because it requires CBSS,
Runners, a vendor key, and a Trading Post Hire.

Version 2.0.0 uses asynchronous request acceptance and saved results. Deploy a
new Actor for this version, with `init` in the deploy transaction. It does not
migrate the previous version's storage.

## Request and result contract

| Handler | Access | Purpose |
| --- | --- | --- |
| `init` | Once | Claim ownership and set the Store registrar; the owner is the default registrar. |
| `register_hire` | Registrar | Record a verified Hire, caller, declaration, version, and artifact. Exact retries are safe. |
| `scrape` | Registered hirer | Submit `/v2/scrape`; pass all fields except `hire_id` to Firecrawl. |
| `api` | Registered hirer | Submit one of the 54 fixed routes in `openapi_routes.json`. |
| `get_result` | Request's hirer and Hire | Read the status and output for one accepted request. |
| `get_last_output`, `get_last_results` | Public | Read the latest display data. These are not request result readers. |
| `capability`, `get_config` | Public | Read the interface and configuration. |

`scrape` and `api` return an acceptance record and emit the same record as
`wrapper.request_accepted`:

```json
{"ok":true,"request_id":1,"hire_id":42,"status":"pending","poll_handler":"get_result"}
```

The Runner callback is a separate invocation. Its return value does not return
to the original buyer call. Save the accepted `request_id`, then call
`get_result` with that ID and the same `hire_id`, signed by the registered
hirer. The result contains `request_id`, `hire_id`, `operation`, `status`, and
`ok`. Pending results have a `deadline_height`. Completed results have an
`output` and `expires_height`.

For `scrape`, `output` is the Firecrawl JSON value. For `api`, `output` contains
`ok`, `operation`, and `data` (the Firecrawl JSON value), plus a short `error`
on failure. A failed request replaces the latest display output, but does not
change another request's saved result. Non-JSON Runner responses are failures.

To read data through transaction receipts, add `receipt_offset: 0`. The Actor
emits one `wrapper.result_chunk` event with `request_id`, `hire_id`, `offset`,
`total`, and CBOR bytes in `data`. Request subsequent offsets until `total`
bytes are collected, then decode the complete CBOR value. Each chunk contains
at most 3,900 data bytes and fits the Node's 4,096-byte event payload limit.
`tools/live_firecrawl.py` performs these signed reads and verifies each identity and
offset. It prints the transaction hash and accepted request ID before polling the result,
so a later connection failure does not hide the recovery ID. `--wait-seconds`
(default 300) sets one wait budget across all result reads and chunks. Reads
consume transactions and fees.

## API forwarding

The `api` payload contains `hire_id`, `operation`, optional `query_params`,
route ID fields, and the Firecrawl body fields. For example:

```json
{"hire_id":42,"operation":"map","url":"https://example.com","limit":5}
```

```json
{"hire_id":42,"operation":"developer_search","query_params":{"query":"How does C/C++ work?","repos":["firecrawl/firecrawl"]}}
```

The wrapper removes its metadata and the ID fields used in the selected path
from the body. It encodes query values separately from path IDs. GET and DELETE
requests have no body or `Content-Type` header. POST, PATCH, and PUT preserve
the vendor fields, including nested formats, actions, headers, and webhooks.
Strings with Runner secret substitution syntax (`${...}`) are refused. Request
bodies must be valid JSON; non-finite numbers are refused before dispatch.
The Actor uses a small percent encoder because PVM does not allow `urllib`.

The fixed route map covers the published v2 HTTP API. Firecrawl jobs that return
a job ID still need a later `get_*` request. Inbound webhook events, MCP, SSE,
and local CLI tools are not exposed by this Actor.

## Account and storage limits

Account-level operations can see and change the shared vendor account. This
includes job lists and cancellation, usage, monitors, browser sessions, and
threat protection. Use separate vendor accounts and Actors when separate
accounts are required. Caller checks on `get_result` do not make public chain
storage private. The latest display getters remain public.

The Actor keeps at most 16 pending or completed requests and 512 registered
Hires. Each Hire has its own storage value. Completed results remain available
for 150 blocks; capacity pressure does not remove them early. A pending request
becomes an error after 150 blocks. Cleanup runs on later submissions and result
reads. A result read checks only its own record; a submission checks all slots.
Cleanup releases expired continuations and removes completed results after
their retention period. It does not cancel vendor work or issue refunds.

These block limits are cleanup thresholds. They do not set an HTTP timeout.
The Runner controls the HTTP timeout and Node selection/retry windows.

The combined request body and route/query size is limited to 28,000 bytes. The full
encoded `get_result` response is limited to 38,000 bytes. The JSON limiter first
shortens strings, then trims list tails, and adds `_cowboy` truncation metadata.
If the full CBOR response still exceeds the limit, the result becomes a bounded
error. The saved result, latest display copy, and Hire capacity fit the base
1 MiB storage allocation; each storage value stays below 65,536 bytes.

Actor-side limiting only applies after a result reaches the callback. A large
vendor response can exceed an upstream transport limit first. Use small format
lists and vendor limits. A timeout result does not prove that Firecrawl stopped
its work; inspect the existing request or vendor job before submitting again.

## Simulate Locally

The `scrape` target compiles the way a deploy does. From this directory (with
`COWBOY_SDK_PATH` pointing at a `node/pvm/Lib` checkout):

```bash
uv run pytest
cowboy actor check scrape
```

`cowboy test` compiles the target and runs one handler on the simulated PVM.
This calls `get_config` on a fresh actor and expects the CBOR-encoded reply:

```bash
cowboy test --target scrape --handler get_config --payload 0x \
  --expect 0xa56676656e646f726966697265637261776c6a63616c6c5f636f756e74006b6c6173745f737461747573f66f73746f72655f7265676973747261726073726573706f6e73655f627974655f6c696d6974199470
```

`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Offline checks

Use Python 3.13 or later and a current Node SDK checkout. The test suite imports
the actual SDK decorators and runs dispatch and callbacks through its mock host.
It checks route wiring against an independent pinned fixture, forwarding,
authorization, failure handling, concurrent results, expiry, and storage/event
limits. A source check keeps direct Actor imports within the supported modules.
Client tests use independent CBOR encoding and expected vendor fields, and also
read chunks produced by the Actor.

From the Cowboy repository root:

```bash
COWBOY_SDK_PATH=/path/to/node/pvm/Lib uv run pytest
python3 examples/gallery/firecrawl-scrape/tools/check_openapi.py
```

The second command checks the pinned routes against the current
[Firecrawl OpenAPI document](https://docs.firecrawl.dev/api-reference/v2-openapi.json).
Use `--schema /path/to/v2-openapi.json` for a downloaded schema. The fixture
records the source date and digest. Review and update it when routes change.
These checks do not prove VM execution, CBSS delivery, or a live network result.

## Run On A Network

Run `./demo.sh` from this directory, or use the path below. It deploys the
`scrape` target with `cowboy actor deploy scrape`; the CLI discovers the target
from this project's `cowboy.toml`, so use a CLI release that matches the
network you target.
The live demo requires a compatible `cowboy` CLI, `uv`, Python, a funded wallet,
a Node with Runners and CBSS proxies, and a Firecrawl key. `cowboy dev` alone is
not sufficient. The demo deploys and funds an Actor, stores a new secret version,
sets its release policy, declares and hires the Actor on Trading Post, registers
the Hire, and reads the exact results of `scrape` and `api` map calls. It uses
the owner as registrar. It is a CLI demonstration, not a Store purchase test.

```bash
KEYFILE=/secure/wallet.key \
FIRECRAWL_API_KEY_FILE=/secure/firecrawl.key \
RPC_URL=http://validator:4000 \
CBSS_INGEST_ENDPOINT=http://cbssd:8080 \
  examples/gallery/firecrawl-scrape/demo.sh
```

The manifest allows `http.fetch`, `secrets.read` for the owner's named key, and
`storage.kv`. The secret release policy must also name the deployed Actor.

For an existing deployed and registered Actor:

```bash
uv run examples/gallery/firecrawl-scrape/tools/live_firecrawl.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key /secure/wallet.key \
  call --payload-json '{"operation":"map","url":"https://example.com","limit":5}'
```

Replace `call --payload-json ...` with `result 1` to recover request 1 without
submitting the vendor call again. Replace it with
`compare --key-file /secure/firecrawl.key` for paid direct-versus-chain checks.
Use a key for the same vendor account as the Actor. Normal scrape, map, and
credit-usage cases must succeed; only a missing crawl case expects an error.
Invalid credentials cannot pass the normal cases. Live checks spend wallet
funds and vendor credits. Keep keys in files, outside the repository.
