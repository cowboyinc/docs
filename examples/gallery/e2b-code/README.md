# E2B Code Store wrapper

A hire-gated wrapper that runs Python, JavaScript, TypeScript, R, Java, or Bash
in an E2B sandbox. Each accepted request has its own retained result. The
registered hirer reads that result through `get_result`.

The PVM does not provide libraries such as pandas, numpy, or matplotlib. An
Actor can hire this wrapper when it needs those libraries. The E2B API key stays
in CBSS and is released to the Runner for the job.

This example is outside the default gallery sweep because it requires a
funded wallet, a Trading Post hire, CBSS proxies, and the E2B Runner tool.

## Required Runner integration

Version 2.1.0 uses one MCP call to server `e2b`, tool `execute`. The Runner tool
owns sandbox creation, execution, output limits, and cleanup. The Actor receives
only the final result. It does not receive or store the sandbox URL or access
tokens. A live sandbox token on public chain state would let other users change
the sandbox while the buyer's code runs.

Deploy and configure the E2B tool on **every eligible MCP Runner** before using
this Actor. The current scheduler selects generic MCP Runners; it does not
filter candidates by server or tool name. An older MCP Runner can accept a job
but cannot execute this tool. The Actor change alone does not install, configure,
or deploy the Runner integration.

The tool accepts these arguments:

- `api_key`: `${E2B_API_KEY}`, resolved from the deployer's CBSS secret.
- `code_hex`: the caller's UTF-8 code as hex.
- `language`: one of the supported languages.
- `timeout_seconds`: the sandbox's fallback time to live, from 5 to 600 seconds.

Hex prevents CBSS substitution inside caller code. For example,
`print('${E2B_API_KEY}')` remains that literal program; the secret is supplied
only in the separate `api_key` argument. Hex is not encryption. Caller code and
final results remain public chain data.

The tool has a 24-second total deadline, including time reserved for cleanup.
The MCP call has a 26-second timeout. The current Node adapter gives MCP jobs a
30-block dispatch window. The Actor retains a pending request for 150 blocks to
allow delivery or a terminal timeout result. The sandbox TTL does not extend
the tool's execution deadline.

## Request and result

```json
{"hire_id": 42, "code": "import pandas as pd; print(int(pd.Series([1,2,3]).sum()))"}
```

Optional `language` defaults to `python`. Optional `timeout` is the sandbox TTL
in seconds, with default 180 and maximum 600. The tool closes the sandbox after
the call; TTL is a fallback if cleanup cannot complete.

`run` returns an acceptance record, not the execution result:

```json
{"ok": true, "request_id": 7, "hire_id": 42, "operation": "run", "status": "pending", "poll_handler": "get_result"}
```

Call `get_result` as the same hirer with the accepted request ID:

```json
{"hire_id": 42, "request_id": 7}
```

The record remains `pending` until its callback or deadline. A successful result
contains the captured streams, text or JSON results, and artifact type names:

```json
{"request_id": 7, "hire_id": 42, "operation": "run", "status": "ok", "ok": true,
 "output": {"ok": true, "operation": "run", "data": {
   "stdout": "6\n", "stderr": "", "results": [], "artifacts": [], "error": null}}}
```

A program error has `status: "error"`, `output.ok: false`, and the error's
`name`, `value`, and `traceback` in `output.data.error`. A create, transport, or
cleanup failure has a fixed error message and a stage. It cannot pass as a
successful code result.

The Runner tool limits the vendor response stream to 32,000 bytes and its full
MCP result envelope to 36,976 bytes. A response that exceeds a limit returns an
output-limit error. The Actor separately checks its full encoded response against
38,000 bytes. These are application budgets, not a claim about a current 40KB
platform limit. Binary displays are represented by type names such as `png`;
their bytes do not enter the result.
Completed records remain available for 150 blocks. When all 16 request slots
are occupied, the Actor rejects new requests until records expire.

Files, persistent contexts, custom templates, GPU execution, and long-lived
sandboxes are outside this capability. Public `get_last_output` is an operator
summary only; buyers must use their request ID and `get_result`.

## Simulate Locally

The offline suite drives the actor through the SDK's in-memory host; the
`sandbox` target compiles the way a deploy does. From this directory (with
`COWBOY_SDK_PATH` pointing at a `node/pvm/Lib` checkout):

```bash
uv run pytest
cowboy actor check sandbox
```

`cowboy test` compiles the target and runs one handler on the simulated PVM.
This calls `get_config` on a fresh actor and expects the CBOR-encoded reply:

```bash
cowboy test --target sandbox --handler get_config --payload 0x \
  --expect 0xa46676656e646f72636532626a63616c6c5f636f756e74006b6c6173745f737461747573f66f73746f72655f72656769737472617260
```

`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Run On A Network

`demo.sh` deploys the `sandbox` target with `cowboy actor deploy sandbox`; the
CLI discovers the target from this project's `cowboy.toml`, so use a CLI
release that matches the network you target.

The demo provisions CBSS, deploys the Actor, creates and registers a hire, then
submits once. It takes the request ID from that transaction's acceptance event
and reads CBOR chunks through signed `get_result` calls. It checks a successful
result and a complete stdout line equal to `42`.

```bash
export RPC_URL=https://rpc.canyon.cowboylabs.net
export CBSS_INGEST_ENDPOINT=http://<cbssd>:<port>
export E2B_API_KEY_FILE=/path/to/key.txt
./demo.sh
```

The manifest needs `secrets.read` for `<deployer>:E2B_API_KEY` and `storage.kv`.
The secret's release policy must allow the deployed Actor. The current Node
has no separate MCP entitlement.

For an existing deployed Actor, `tools/live_e2b.py` handles signed submission and
result reads. `--wallet-key` is the wallet key file used by the CLI. The client
prints transaction and request IDs before waiting. If a wait times out, use the
saved request ID instead of submitting the code again.

```bash
uv run tools/live_e2b.py --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" call --payload-json '{"code":"print(6*7)"}'
uv run tools/live_e2b.py --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" result 7
```

## Checks

Offline checks use the real SDK decorators with an in-memory host. They cover
the emitted Node MCP envelope, secret-safe code encoding, callback permission,
request identity, overlap, deadline and retention behavior, receipt chunks,
and callback/read persistence across fresh processes.

```bash
COWBOY_SDK_PATH=/path/to/node/pvm/Lib uv run pytest \
  tests/test_e2b.py tests/test_live_e2b.py
```

Vendor HTTP contract and private sandbox lifecycle checks belong to the Runner
tool. This Actor no longer implements those HTTP routes or parses NDJSON.

The explicit live comparison uses the independently installed official
`e2b-code-interpreter==2.10.0` SDK. It does not import Actor helpers. Normal cases
must succeed and match fixed outputs on both paths. The error case must return
the program's `ValueError`, not an arbitrary vendor failure. This command makes
paid vendor calls and signed chain transactions:

```bash
uv run tools/live_e2b.py --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" compare --key-file "$E2B_API_KEY_FILE"
```

Offline passes do not prove that the new Runner tool is deployed or that a live
E2B request completes.
