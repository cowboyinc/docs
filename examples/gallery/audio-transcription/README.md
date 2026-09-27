# Audio Transcription Service

> Speech-to-text transcription powered by off-chain AI

## Use Case

Audio transcription is needed for:
- Meeting recordings and podcasts
- Call center analytics
- Accessibility features
- Content indexing and search

With Cowboy, you can build a decentralized transcription service that:
- Accepts audio files via actor messages
- Routes to off-chain AI transcription runners
- Returns accurate text with timestamps
- Verifies results through consensus

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                  Audio Transcription Agent                       │
│                                                                  │
│  On transcription request:                                       │
│  1. Receive audio file reference (IPFS hash or URL)              │
│  2. Submit to transcription runners                              │
│  3. Runners use Whisper/similar models                           │
│  4. Return text with timestamps                                  │
│  5. Consensus verification (text similarity check)               │
│  6. Store result and notify requester                            │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Multiple formats**: MP3, WAV, M4A, FLAC
- **Timestamps**: Word or segment-level timing
- **Speaker diarization**: Identify different speakers
- **Language detection**: Auto-detect or specify language
- **Consensus**: Multiple runners verify accuracy

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/transcriber/main.py` - the actor
- `tests/test_transcriber.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. The runner
never answers in the simulator, so the tests cover the synchronous half of
`transcribe_audio`: the request state and the `llm` job dispatched to the runner
(visible in `mock_host.get_send_log()`), the read handlers, and the finalize
step called directly with a canned completion.

```bash
uv run pytest
```

`cowboy test` compiles the target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh actor and expects
the CBOR-encoded defaults (`default_language` en, `job_count` 0):

```bash
cowboy test --target transcriber --handler get_stats \
  --payload 0xa0 \
  --expect 0xa2696a6f625f636f756e74007064656661756c745f6c616e677561676562656e
```

`cowboy actor check transcriber` validates the target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

The demo deploys the actor, submits a deterministic sample request, and reads back the stored result.

`demo.sh` deploys the target with `cowboy actor deploy transcriber`; the deploy runs
`init`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target. It needs a healthy runner
with LLM capability.

## Demo Scope

The runnable demo uses deterministic sample payloads and stores the latest
request/result. It does not run Whisper, upload files to IPFS, produce real
timestamps, perform speaker diarization, or verify multi-runner consensus unless
that behavior is explicitly shown in `demo.sh`; treat those capabilities as
extension points.

## Caveats

- **File size limits**: Large files may timeout
- **Cost**: Transcription is compute-intensive
- **Accuracy**: Background noise affects quality
- **Languages**: Model support varies by language
