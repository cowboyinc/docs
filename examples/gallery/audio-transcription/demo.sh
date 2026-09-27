#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Audio Transcription Demo" "Deploy a transcription actor, submit an audio request, and inspect the generated transcript record."
demo_step "Deploy audio transcriber" "The deploy runs init with English as the default language, used when requests omit one; the actor stores that setting and the latest transcript."
ACTOR_ADDR="$(demo_deploy_actor transcriber --init '{"default_language":"en"}' --why "Deploy the transcription showcase actor." --expect "An actor address is available for transcription calls.")"
demo_step "Submit transcription request" "The demo URL and hints stand in for a normal audio ingestion request."
demo_call "$ACTOR_ADDR" "transcribe_audio" '{"audio_url":"https://example.com/demo.wav","language":"en","hints":"earnings call discussing AI spend"}' --why "Create a transcript job record." --expect "The actor records the latest transcript request and result."
demo_wait "Waiting for the transcription transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest transcript" "This shows the transcript record the actor stored."
demo_call "$ACTOR_ADDR" "get_last_transcript" '{}' --why "Read the latest transcript." --expect "The response includes the audio URL and transcript fields."
demo_step "Read transcription stats" "Stats summarize processed transcription requests."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate transcription counters." --expect "The response includes request totals."
demo_done
