# Deepgram Transcription Store wrapper

A hire-gated wrapper over Deepgram's pre-recorded speech-to-text API. Registered
Store Hires submit an audio URL and read the request-specific transcript through
`get_result`. The Deepgram API key stays in CBSS as `DEEPGRAM_API_KEY`.

This example is outside the default gallery sweep because live execution needs
CBSS proxies, runners, a Deepgram key, and a paid Trading Post Hire.

## Supported surface

`transcribe` maps to `POST https://api.deepgram.com/v1/listen` using the
**`Token`** authentication scheme (`Authorization: Token <key>`, **not**
`Bearer`). It transcribes a remote audio URL:

- `url` (required) — an `http(s)` URL to remote audio, never raw audio bytes.
- `model` — one of `nova-3`, `nova-2`, `nova`, `enhanced`, `base`, `whisper`
  (default `nova-3`)
- `language` — a BCP-47 tag (optional)
- `punctuate`, `smart_format`, `diarize` — booleans (optional)

Options ride the request URL as percent-encoded query parameters.

Streaming, raw-byte upload, callback URLs, word-level timestamps,
summarization, and topic detection are not exposed in v1.

## Call contract

`transcribe` returns immediately:

```json
{"ok":true,"request_id":7,"hire_id":42,"operation":"transcribe","status":"pending","poll_handler":"get_result"}
```

The same buyer and Hire poll `get_result` with `request_id` and `hire_id`. On
success `output.data` holds the extracted `{transcript, confidence, duration,
models, request_id}`. Signed callers can pass `receipt_offset` to emit the
encoded result as ordered `wrapper.result_chunk` events.

## Bounded transcript and the long-transcript park warning

Deepgram returns the transcript **inline** — there is no hosted-result URL.

- The wrapper extracts only the transcript text plus small metadata and
  bounds the transcript to 34,000 bytes, keeping the whole encoded result
  under the 38,000-byte result budget. A truncated transcript is flagged
  `"truncated": true`.
- **Park warning:** the *whole* Deepgram reply must fit the runner transport
  cap (~40-64KB). If it exceeds the cap the continuation never resumes and
  the request **parks** until its deadline (then errors) — the submit
  transaction still reports success. **Use short clips** for on-chain calls.

## Billing and idempotency

`transcribe` is **mutating**: transcription bills. Deepgram documents no
idempotency key for this endpoint. A Runner retry or reassignment can repeat
the provider POST and may consume credits again. After an uncertain outcome,
inspect the existing request before retrying.

## Safety and bounds

- Caller data cannot contain `${...}` runner secret templates (would let the
  runner paste the live key into an outbound field).
- The audio URL and every option are public chain data. Never pass a private
  or signed URL whose possession grants access to protected audio.
- Requests use `raw_body` (exact JSON, `allow_nan=False`), a `Token` auth
  header, and one CBSS-backed key.
- The API key never enters actor payloads, continuations, storage, or
  results; the continuation carries only the request id.
- Final canonical-CBOR responses are capped at 38,000 bytes; vendor error
  text is truncated to 500 bytes and never labeled success.
- At most 16 requests are retained globally and six per Hire.
- Pending and completed records expire after bounded block windows.
- Results belong to the submitting buyer and Hire. Public status is
  metadata-only, but signed `wrapper.result_chunk` receipt events contain the
  requested transcript bytes and are public chain data.
- Runner callbacks use the private continuation resume path and exact Hire pins.

The independent `deepgram_routes.json` fixture pins Deepgram's documented
`POST /v1/listen` contract.

## API key

Create a key at [deepgram.com](https://deepgram.com/). Store the key in a file.
Do not put it in source or pass it directly on a command line.

## Tests

```bash
cd examples/gallery/deepgram
uv run pytest
cowboy actor check deepgram

python3 tests/test_deepgram_parity.py          # offline drift
python3 tests/test_deepgram_parity.py --live   # probe published reference

DEEPGRAM_API_KEY_FILE=/secure/deepgram-key.txt \
  python3 tests/test_deepgram_comparison.py \
  --url "https://dpgr.am/spacewalk.wav" --model nova-3
```

## Canyon

The complete Store flow requires Canyon runners and CBSS proxies:

```bash
STORE_REGISTRAR=0x... \
DEEPGRAM_API_KEY_FILE=/secure/deepgram-key.txt \
DEEPGRAM_URL="https://dpgr.am/spacewalk.wav" \
RPC_URL=https://rpc.canyon.cowboylabs.net \
CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> \
  ./demo.sh
```

## Versioning

The `transcribe` acceptance-record + `get_result` contract is the v1.0.0
surface. Any change to the returned shape or the supported input fields is a
breaking version bump and requires a redeploy plus a fresh Trading Post
declaration.
