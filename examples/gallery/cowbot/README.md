# Cowbot

Status: experimental gallery showcase. Cowbot is intentionally outside the
default gallery sweep because it depends on Slack setup, CBFS volumes,
runner-agent support, and gateway infrastructure. Use `./demo.sh` or
`local-smoke.sh` for the offline runner-agent smoke path, or `deploy.sh` plus
the gateway flow for the full operational demo.

Slack bot for the Cowboy team. Modeled on `tools/sitchbot` from `uc_plat_core`,
re-architected to run on cowboy's actor + runner + cbfs stack with no hosted
service of its own.

## How it works

```
Slack POST → Gateway (untrusted; forwards body+headers verbatim)
                  │
                  ▼
            CowbotActor.slack_http  (@runner.continuation; NO secret in-actor)
                  ├─ answer url_verification inline (pre-await: explicit envelope)
                  ├─ secret-free prefilters (ts freshness, exact-replay
                  │  dedupe, body-size cap, dispatch cap) — bound forged
                  │  cost only; they do NOT authenticate the request
                  │  (all before the first await — NO self-send hop, D36)
                  └─ runner.http chat.postMessage "Thinking…"  ◄── THE GATE
                        ├─ verify={SLACK_SIGNING_SECRET}: runner MAC-checks
                        │  the inbound webhook FIRST and aborts fail-closed
                        │  on mismatch — before any post/read/agent work
                        └─ secrets=[SLACK_BOT_TOKEN]: runner substitutes the
                           bot token into the Authorization header
                  └─ runner.agent(...)  ◄── runs the LLM
                        ├─ mounts cowboyinc (ro) + cowbot-state (rw)
                        ├─ tool-use loop: read_file/grep/ls/glob
                        └─ writes cowbot-state/.sessions/<thread_key>.json
                  └─ runner.http chat.update — posts the answer back
```

Zero plaintext secrets live in the actor (CIP-24 §5.5): a validator replays
actor execution, so the signing secret and bot token are CBSS secrets owned
by the deployer EOA and granted to the actor (`secrets.verify` /
`secrets.read`, disjointly — see `manifest.README`). Only the runner — which
can threshold-decrypt a CBSS secret — ever sees the plaintext: it MAC-verifies
the inbound webhook and substitutes the bot token on the outbound post. The
gateway is untrusted plumbing.

Handlers in `actors/cowbot/main.py`:
- `slack_http(payload)` — gateway-facing entry (`POST /slack/events`) and **the
  continuation itself** (`@public @post @runner.continuation`). It answers the
  url_verification challenge inline and runs the secret-free prefilters in its
  pre-await segment (each branch returns an explicit envelope inline; on the
  normal path the empty initial return is answered **502** by today's gateway —
  the tx still succeeds and the job runs, COW-3895 — and by the route's
  fallback/2xx once gateway PR #93 lands), then — in the SAME continuation — its
  first `runner.http` carries
  `verify={SLACK_SIGNING_SECRET}`
  (the inbound-MAC gate) plus `secrets=[SLACK_BOT_TOKEN]`, runs `runner.agent`,
  and posts the answer. There is NO self-send: answering inline makes that first
  `runner.http` a first-level child of the gateway-signed tx, which funds it. A
  self-sent message would run under a deferred child whose derived access budget
  cannot fund the nested `submit_job` child, so the node refuses it (D36, COW-3878).
- `slack_http__resume(payload)` — FSM-generated; resumes after each runner await.
- `on_runner_result(payload)` — standard `runner.handle_runner_result` shim.

## Simulate Locally

The actor is a `cowboy init` project with one target, `cowbot`
(`actors/cowbot/main.py`). The offline tests drive `init`, the owner reads,
and the `/health` route through `cowboy_sdk.testing.SimulatedChain`
(`slack_http` is a runner continuation, so its awaits are not executed
offline). From this directory (with `COWBOY_SDK_PATH` pointing at a
`node/pvm/Lib` checkout):

```bash
uv run pytest
cowboy actor check cowbot
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. This calls the pure `health` route on a fresh actor and
expects the CBOR-encoded HTTP envelope:

```bash
cowboy test --target cowbot --handler health --payload 0x \
  --expect 0xa364626f6479426f6b6673746174757318c8676865616465727381826c636f6e74656e742d747970657819746578742f706c61696e3b20636861727365743d7574662d38
```

`cowboy test` and `cowboy dev` need `cowboy-dev-runner` on `PATH` or at
`COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner` in a node
checkout).

## Run On A Network

`deploy.sh` (Step 4 below) deploys the `cowbot` target with
`cowboy actor deploy cowbot`; the CLI discovers the target from this project's
`cowboy.toml`, so it runs from this directory and needs a CLI release that
matches the network you target. The full operational bring-up — Slack app,
CBFS volumes, CBSS secrets, gateway — is Steps 1-6 below.

## Prerequisites

- `cowboy` CLI authenticated to your account.
- CBFS available (devnet or mainnet) for both volumes.
- The runner network must support the `runner.agent` verb. If the selected
  network does not advertise that capability, the actor fails at
  `await runner.agent(...)`.
- The Slack bot token and signing secret are **CBSS secrets** (CIP-24 §5.5),
  owned by the deployer EOA and ACL'd to the actor — NOT env vars and NOT
  actor state. The runner threshold-decrypts them per job. The only env var on
  the runner host is the LLM inference key (`MOONSHOT_API_KEY` /
  `OPENAI_API_BASE`), which is not a CBSS secret.

## Step 1 — Create the Slack app

In <https://api.slack.com/apps>, create an app from manifest:

```yaml
display_information:
  name: Cowbot
  description: Cowboy team assistant
features:
  bot_user:
    display_name: Cowbot
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - chat:write
      - im:history
      - im:read
      - im:write
      - users:read
settings:
  event_subscriptions:
    request_url: https://<gateway-host>/<cowbot-actor-address>/slack/events
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: false   # we use HTTP webhooks, not socket mode
  token_rotation_enabled: false
```

Install to your workspace, then capture two values from the app config —
both become CBSS secrets in Step 3 (neither is ever stored in the actor):
- **Bot User OAuth Token** (`xoxb-…`) — the runner substitutes it into the
  outbound `chat.postMessage` (`secrets=[SLACK_BOT_TOKEN]`).
- **Signing Secret** — the runner MAC-verifies the inbound webhook with it
  (`verify={SLACK_SIGNING_SECRET}`); it is verify-only and never substituted.

## Step 2 — Provision CBFS volumes

```bash
# Read-only knowledge volume for the bot. It is PRIVATE and owner-key dual
# wrapped so this laptop can bulk-load it while runners still read through CBSS.
cbfs volume create --name cowboyinc --owner-key
cbfs put-many cowboyinc --from-json cowbot-files.json

# Read-write state volume — sessions, transient artifacts. Also PRIVATE.
cbfs volume create --name cowbot-state
```

The runner resolves the private CBSS key when mounting the knowledge volume,
while the owner CLI uses the owner-key wrap for local bulk loading.

Cowbot expects the system prompt at `/cbfs/cowboyinc/cowbot/system_prompt.txt`.
Copy `system_prompt.txt` from this directory to that path inside the volume.

## Step 3 — Provision the Slack secrets as CBSS secrets

Both Slack values are CBSS secrets owned by the deployer EOA and ACL'd to the
cowbot actor (CIP-24 §5.5). The actor stores neither; the runner threshold-
decrypts each per job. Set them against the deployed actor address (`deploy.sh`
does this automatically if you export `COWBOT_SLACK_BOT_TOKEN` /
`COWBOT_SLACK_SIGNING_SECRET` first — see Step 4):

```bash
# paste the value, then Ctrl-D — --value-file - avoids leaking it to shell history
cowboy secrets --private-key ./your-key.hex \
    set SLACK_BOT_TOKEN      --value-file - --actor <cowbot-actor-address>
cowboy secrets --private-key ./your-key.hex \
    set SLACK_SIGNING_SECRET --value-file - --actor <cowbot-actor-address>
```

The runner substitutes `${SLACK_BOT_TOKEN}` into the outbound `chat.postMessage`
Authorization header and uses `SLACK_SIGNING_SECRET` only to MAC-verify the
inbound webhook (verify-only; never substituted). The gateway is untrusted and
never sees either plaintext.

The LLM inference key is separate and is NOT a CBSS secret — set it on the
runner host where the agent job executes:

```bash
export MOONSHOT_API_KEY=sk-...        # or OPENAI_API_BASE for self-hosted
```

## Step 4 — Deploy the actor

Easiest: copy `.env.example` to `.env`, fill in the values, then run
the idempotent deploy script:

```bash
cp .env.example .env       # edit .env with your keys
source .env
./deploy.sh                # re-runnable; skips steps already done
```

`deploy.sh` derives the deterministic actor address from the `cowbot` target + your
creator pubkey + salt, renders the entitlement manifest (substituting the
deployer EOA into the cross-account secret refs), deploys only if the address
isn't on chain yet, and — if you exported `COWBOT_SLACK_BOT_TOKEN` /
`COWBOT_SLACK_SIGNING_SECRET` — provisions the two CBSS secrets ACL'd to the
new actor. It prints the address for the gateway.

Manual equivalent (note `--manifest-json`: the actor MUST carry the
`secrets.read` / `secrets.verify` / `http.fetch` / `storage.kv` entitlements
or the first runner op fails admission — render `manifest.json` first,
substituting `OWNER_EOA` with the deployer EOA in lowercase-no-0x form; see
`manifest.README`):

```bash
sed "s/OWNER_EOA/$(cowboy wallet address --key ./your-key.hex | sed 's/^0x//' | tr 'A-F' 'a-f')/g" \
  manifest.json > manifest.deploy.json
cowboy actor deploy cowbot \
  --salt 0xc0 \
  --manifest-json manifest.deploy.json \
  --init-payload '{"knowledge_volume":"cowboyinc","state_volume":"cowbot-state"}' \
  --private-key ./your-key.hex
```

## Step 5 — Stand up the gateway

The gateway forwards the inbound POST (body + headers, verbatim) to the actor;
it is untrusted plumbing and holds no secret. Use the gateway helper scripts
in this directory:

```bash
./gateway/bootstrap-gateway.sh
COWBOT_ACTOR=<address> ./gateway/bootstrap-gateway-mac.sh
```

Point a public HTTPS route at the gateway and paste this into the Slack app's
Event Subscriptions Request URL field:

```
https://<gateway-host>/<cowbot-actor-address>/slack/events
```

Slack sends a `url_verification` challenge; the actor answers it inline, so you
should see ✓ verified. For standing up the gateway on a host (Tailscale Funnel,
systemd unit, smoke tests), see `gateway/RUNBOOK.md`.

## Step 6 — Test

Invite Cowbot to a channel, then `@cowbot what does CIP-15 say about route
caching?`. Expect a response in-thread within ~10-30s depending on runner
backlog and Kimi K2 latency.

For DMs: open a DM with Cowbot and just ask. No `@` needed.

## Operations

- `cowboy actor execute --actor <cowbot-address> --handler get_requests --payload-json '{}'` — recent request log (owner-only)
- `cowboy actor execute --actor <cowbot-address> --handler get_pending --payload-json '{}'` — currently in-flight per-thread (owner-only)
- Events emitted: `cowbot.init`, `cowbot.answered`, `cowbot.slack_post_error`,
  `cowbot.precondition_failed` (runner `verify=` MAC rejected a forged webhook),
  `cowbot.slack_http_error` / `cowbot.agent_result_invalid` (a runner op came
  back as something other than a JSON object — the thread is released either
  way, never wedged in `get_pending`),
  `cowbot.slack_prefilter_drop` / `cowbot.slack_body_too_large` (secret-free
  prefilters bounded a stale/replayed/oversized dispatch)

## Migrating to self-hosted Kimi K2

The `model="kimi-k2"` arg is passed verbatim to `runner.agent`. To switch
models (Qwen3-Coder, DeepSeek, self-hosted), change the `DEFAULT_MODEL`
constant in `actors/cowbot/main.py` and re-deploy. The runner side handles the
routing to the right inference endpoint.

For self-hosted via llama.cpp on a workstation, see `start-llama-server.sh`
(boots Qwen3-Coder-30B-A3B Q4 with `--jinja` template handling for
proper tool-call rendering); set `OPENAI_API_BASE=http://127.0.0.1:8080/v1`
on the runner host before starting it.
