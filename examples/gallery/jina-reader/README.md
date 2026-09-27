# Jina Store Wrapper

This actor lets another hired actor use Jina Reader, Search, embeddings,
reranking, classification, segmentation, DeepSearch, and catalog APIs. Calls
are wallet-billed while the Jina API key remains in CBSS.

This example is **outside the default gallery sweep** because live execution
needs a Jina API key (held in CBSS) and a paid Trading Post hire.

## Setup

Store the vendor key as `JINA_API_KEY` under the deployer account. Deploy with:

- `http.fetch` for `r.jina.ai`, `s.jina.ai`, `api.jina.ai`, and
  `deepsearch.jina.ai`
- `secrets.read` for `<deployer>:JINA_API_KEY`
- `storage.kv`

Allow the deployed actor in the CBSS release policy, declare it on Trading
Post, create a hire, and call `register_hire` with the finalized hire data.

## Use

Every vendor operation requires `hire_id`. JSON handlers accept the regular
Jina request body plus `hire_id`; the wrapper removes it before calling Jina.

Vendor data is **not** returned by the handler call. Each handler starts the
Runner request and returns an acceptance record; the registered hirer then reads
the result from `get_result` with the returned `request_id`. This is because the
Runner callback arrives after the buyer's transaction, so a single shared output
could neither reach that buyer nor survive a second overlapping request.

```json
{"hire_id": 42, "url": "https://example.com", "engine": "direct"}
```

```json
{"hire_id": 42, "query": "latest Cowboy actor network news"}
```

A handler returns `{"ok": true, "request_id": N, "status": "pending",
"poll_handler": "get_result"}`. Read the result as the same caller and hire:

```json
{"hire_id": 42, "request_id": 7}
```

`get_result` returns `status` `pending` until the callback lands, then `ok` with
the shaped `output`, or `error`. A request is refused unless the caller is the
registered hirer and the `hire_id` matches, and results expire after a bounded
retention window. API JSON results, including model names and complete embedding
vectors, are returned exactly or fail with an explicit size error. Handlers:

- `read`, `read_post`, `search`, `search_post`
- `embeddings`, `rerank`, `classify`, `train_classifier`, `segment`
- `deepsearch`, `catalog`
- `get_result` (poll for a request's result)

## Test

Offline (no chain, no key). From this directory, point `COWBOY_SDK_PATH` at a
`node/pvm/Lib` checkout:

```bash
COWBOY_SDK_PATH=/path/to/node/pvm/Lib uv run pytest
cowboy actor check jina
```

`test_jina.py` covers dispatch, authenticated `get_result`, the secret-template
guard, hire/capacity caps, callback hygiene, response bounding, and the live
client's chunk reassembly. `tools/check_jina_parity.py` is the route drift guard:

```bash
uv run python tools/check_jina_parity.py
uv run python tools/check_jina_parity.py --url https://api.jina.ai/openapi.json
```

The full Canyon flow (deploy → declare → hire → signed call) runs end to end via
`./demo.sh`.

On chain, against a deployed and hired Canyon actor, `tools/live_jina.py` submits a
request and reads the result back through signed receipt chunks:

```bash
uv run examples/gallery/jina-reader/tools/live_jina.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" --wallet-key /secure/wallet.key \
  call --handler read --payload-json '{"url":"https://example.com"}'

uv run examples/gallery/jina-reader/tools/live_jina.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" --wallet-key /secure/wallet.key \
  compare --key-file /secure/jina-key.txt
```
