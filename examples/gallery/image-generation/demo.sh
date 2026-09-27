#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Image Generation Demo" "Deploy an image generation actor, submit a prompt, and inspect the generated asset record."
demo_step "Deploy image generator" "The actor stores default style settings and the latest generated image record. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor generator --init '{"default_style":"editorial"}' --why "Deploy the image generation showcase actor." --expect "An actor address is available for generation calls.")"
demo_step "Submit image prompt" "The prompt is domain-specific so the stored record is recognizable."
demo_call "$ACTOR_ADDR" "generate_image" '{"prompt":"A futuristic cowboy validator node on a desert mesa","style":"cinematic"}' --why "Create a generated image record." --expect "The actor records the prompt, style, and generated asset metadata."
demo_wait "Waiting for the image generation transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest image record" "This shows the generated asset metadata stored by the actor."
demo_call "$ACTOR_ADDR" "get_last_image" '{}' --why "Read the latest image record." --expect "The response includes prompt, style, and asset fields."
demo_step "Read generation stats" "Stats summarize generation requests."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate generation counters." --expect "The response includes request totals."
demo_done
