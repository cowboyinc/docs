# Content Moderator

> Community moderation with AI classification + appeal flow

## Use Case

Online communities need consistent moderation:
- Detect harassment, spam, scams, and policy violations
- Quarantine or remove harmful content
- Escalate edge cases to human review
- Track appeals and outcomes for transparency

With Cowboy, you can build an autonomous moderation agent that:
- Submits posts/comments to AI runners
- Applies deterministic policy thresholds on-chain
- Sends actions to a moderation actor or bridge
- Records decisions and appeals for auditability

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                  Content Moderator Agent                         │
│                                                                  │
│  On new content:                                                 │
│  1. Submit text to AI runners                                    │
│  2. Runners return: {labels, severity, confidence}               │
│  3. Apply policy thresholds                                      │
│  4. Action: allow / flag / remove / quarantine                   │
│  5. Record decision + allow appeal                               │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Policy-based decisions**: Deterministic thresholds on-chain
- **Multi-label classification**: harassment, hate, scams, spam, etc.
- **Appeal flow**: Users can request review
- **Audit trail**: All decisions recorded
- **Composable**: Integrates with bots, forums, or social apps

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/moderator/main.py` - the actor
- `tests/test_moderator.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`moderate_content`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `submit_appeal({"case_id": 1, "reason": "context"})`
on a fresh actor and expects the CBOR-encoded received appeal:

```bash
cowboy test --target moderator --handler submit_appeal \
  --payload 0xa266726561736f6e67636f6e7465787467636173655f696401 \
  --expect 0xa366726561736f6e67636f6e746578746673746174757368726563656976656467636173655f696401
```

`cowboy actor check moderator` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor, submits a deterministic sample request, and reads back the stored result.

`demo.sh` deploys the target with `cowboy actor deploy moderator`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not perform live stream ingestion, webhook delivery,
multi-runner consensus, or production enforcement unless that behavior is
explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Caveats

- **False positives/negatives**: AI moderation is imperfect
- **Privacy**: Content is sent to AI runners
- **Policy scope**: Define clear community rules
- **Latency**: AI evaluation adds seconds of delay
