#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Runner LLM Demo" \
  "Deploy the canonical runner.llm lesson, submit a prompt, wait for the async callback, and read the captured response."

demo_step "Deploy the assistant target" "This actor is the dedicated curriculum example for LLM runner jobs. Deploy runs init, which records the default LLM job configuration the callback will update."
ACTOR_ADDR="$(demo_deploy_actor assistant \
  --why "Deploy the actor that calls runner.llm and stores callback state." \
  --expect "The deploy output includes an actor address.")"

demo_step "Ask the LLM" "The handler submits a prompt to runner.llm and captures enough state to correlate the later callback."
demo_call "$ACTOR_ADDR" "ask_llm" '{"prompt":"Explain Cowboy runner continuations in one short paragraph."}' \
  --why "Submit a real LLM runner job from actor code." \
  --expect "The transaction succeeds immediately while the LLM result is still pending."

demo_wait "Waiting for the LLM runner callback to write the generated response into actor state." "$POST_RUN_SLEEP"

demo_step "Read latest LLM response" "This query demonstrates result extraction after the callback has updated state."
demo_call "$ACTOR_ADDR" "get_response" '{}' \
  --why "Read the prompt, callback state, and extracted response." \
  --expect "The response contains the latest LLM result or its pending/failure state."

demo_step "Read LLM config" "The config query shows the model/job settings separately from the generated content."
demo_call "$ACTOR_ADDR" "get_config" '{}' \
  --why "Show the canonical lesson's LLM configuration." \
  --expect "The response exposes the actor's runner.llm settings."

demo_done
