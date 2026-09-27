# Image Generation Service

> Stable Diffusion image generation via off-chain AI runners

## Use Case

AI image generation powers:
- NFT artwork creation
- Marketing asset generation
- Game asset creation
- Personalized content

With Cowboy, you can build a decentralized image generation service that:
- Accepts text prompts and generation parameters
- Routes to off-chain SD/SDXL runners
- Returns generated images (stored on IPFS)
- Provides on-chain provenance and payment

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                  Image Generation Agent                          │
│                                                                  │
│  On generation request:                                          │
│  1. Receive prompt and parameters                                │
│  2. Submit to image generation runners                           │
│  3. Runners use SD/SDXL models                                   │
│  4. Upload result to IPFS                                        │
│  5. Return image hash and metadata                               │
│  6. Record provenance on-chain                                   │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Multiple models**: SD 1.5, SDXL, custom fine-tunes
- **Customizable**: Size, steps, guidance, seed
- **IPFS storage**: Decentralized image hosting
- **Provenance**: On-chain generation records
- **Batch support**: Generate multiple variations

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/generator/main.py` - the actor
- `tests/test_generator.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

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
cowboy test --target generator --handler get_stats \
  --payload 0xa0 \
  --expect 0xa2696a6f625f636f756e74006d64656661756c745f7374796c6569656469746f7269616c
```

`cowboy actor check generator` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the target with `cowboy actor deploy generator`, submits a
deterministic sample request, and reads back the stored result. A project
deploy checks the node's actor runtime profile, so use the CLI release that
matches the network you target.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not generate images, upload assets to IPFS, deliver
production moderation, or charge for real model usage unless that behavior is
explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Caveats

- **Generation time**: 10-60 seconds per image
- **Content policy**: Runners may filter inappropriate content
- **Consistency**: Different runners may produce slightly different results
- **Cost**: GPU compute is expensive
