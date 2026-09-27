#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-8}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Minimal Runner Continuation Demo" \
  "Deploy the smallest runner continuation actor, trigger an HTTP-shaped async job, wait for the runner callback, and read the final status."

demo_step "Deploy the continuation target" "This actor keeps the code small so the continuation mechanics are the focus. Deploy runs init, which sets the status fields before the async request."
ACTOR_ADDR="$(demo_deploy_actor continuation \
  --why "Deploy the actor that will request off-chain work and resume through a callback, starting from a clean continuation state." \
  --expect "The deploy output includes an actor address.")"

demo_step "Trigger refresh" "This is the important call: the handler schedules runner work and returns before the off-chain result exists."
demo_call "$ACTOR_ADDR" "refresh" '{}' \
  --why "Submit the minimal runner continuation request." \
  --expect "The immediate transaction succeeds and the actor is waiting for a callback."

demo_wait "Waiting for the runner callback that completes the continuation and writes the final status." "$POST_RUN_SLEEP"

demo_step "Read refresh status" "The status read shows whether the callback path updated actor state after the original transaction."
demo_call "$ACTOR_ADDR" "get_status" '{}' \
  --why "Inspect the callback-updated state." \
  --expect "The response shows the latest refresh status and callback result."

demo_done
