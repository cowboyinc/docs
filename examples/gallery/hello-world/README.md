# Hello World

The smallest store-publishable actor. No secrets, no volumes, no runner —
`init` tolerates an empty payload, so the General Store's instance-deploy
leg (`init` with `{}`) works with zero post-deploy calls. Use it to exercise
the store pipeline end to end.

| Surface | What |
|---|---|
| `GET /` | HTML greeting page (read-only) |
| `POST /hello` | Increments the counter, returns JSON |
| `set_greeting` | Owner-only chain call: `{greeting: "..."}` (CBOR) |
| `get_stats` | Public read: owner, greeting, count |

Routes are declared with `@get`/`@post` decorators; the deploy CLI lowers
them to the `__cowboy/routes` table the gateway dispatches from (CIP-15 §6).

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded default stats:

```bash
cowboy test --target hello --handler get_stats \
  --payload 0xa0 \
  --expect 0xa3656f776e657260686772656574696e676d48656c6c6f2c20776f726c64216b68656c6c6f5f636f756e7400
```

`cowboy actor check hello` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
COWBOY_PRIVATE_KEY=<deployer key file> ./deploy.sh
```

`deploy.sh` deploys the target with `cowboy actor deploy hello`. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target. Then
`curl https://<actor-address>.<gateway-domain>/` serves the page.

## Publish to the General Store (Labs path)

1. **Source volume**: put `actors/hello/main.py` in a CBFS volume and note the
   volume id + manifest root:
   ```bash
   cbfs volume create hello-world-src && cbfs put hello-world-src actors/hello/main.py
   cbfs volume manifest hello-world-src   # → sourceManifestRoot
   ```
2. **Labs publishing** (store-admin → Labs publishing → New Labs listing):
   name/category/description, the deployed actor address, the deploy salt,
   `sourceVolumeId`, `sourceManifestRoot`, visibility `public`, terms `free`.
   Prepare runs declare → grant and hands the submission to Review.
3. **Review**: claim → run checks → Approve & publish. The chain leg runs
   `register_artifact` + `attest`; the listing goes Live in the dashboard
   store, where hiring instance-deploys the declared code.
