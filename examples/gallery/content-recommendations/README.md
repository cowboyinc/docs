# Content Recommendations

> "You may also like" recommendation engine powered by AI

## Use Case

Personalized recommendations drive engagement across platforms:
- E-commerce: "Customers who bought X also bought Y"
- Streaming: "Because you watched X, try Y"
- Social: "Posts you might like"

Traditional recommendation systems require:
- ML infrastructure (training, serving)
- Feature stores and embeddings
- A/B testing frameworks
- Real-time inference servers

With Cowboy, you can build a recommendation engine that:
- Uses LLMs for semantic understanding
- Learns user preferences on-chain
- Generates personalized recommendations
- Runs without ML infrastructure

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                  Content Recommender Agent                       │
│                                                                  │
│  On recommendation request:                                      │
│  1. Load user's interaction history from storage                 │
│  2. Build context prompt with preferences                        │
│  3. Submit to AI runners for recommendations                     │
│  4. Consensus: 2/3 must agree on top picks                       │
│  5. Return ranked recommendations                                │
│  6. Record interaction for learning                              │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Semantic understanding**: LLMs understand content meaning, not just patterns
- **Cold start friendly**: Works with minimal user data
- **Explainable**: AI provides reasoning for recommendations
- **Multi-domain**: Works for products, content, music, etc.
- **Privacy-preserving**: User preferences stored on-chain, not centralized

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/recommender/main.py` - the actor
- `tests/test_recommender.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`recommend_content`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded defaults (empty `catalog_name`, `request_count` 0):

```bash
cowboy test --target recommender --handler get_stats \
  --payload 0xa0 \
  --expect 0xa26c636174616c6f675f6e616d65606d726571756573745f636f756e7400
```

`cowboy actor check recommender` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor, submits a deterministic sample request, and reads back the stored result.

`demo.sh` deploys the target with `cowboy actor deploy recommender`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not call live recommendation models, run inference
servers, personalize against production user data, or verify multi-runner
consensus unless that behavior is explicitly shown in `demo.sh`; treat those
capabilities as extension points.

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `history_limit` | Interactions to consider | 50 |
| `num_runners` | AI runners for consensus | 3 |
| `diversity_weight` | Balance relevance vs novelty | 0.3 |

## Caveats

- **Catalog size**: Large catalogs require pagination
- **Latency**: AI inference adds ~2-3 seconds
- **Quality**: Depends on item metadata richness
- **Popularity bias**: May recommend popular items without diversity tuning
