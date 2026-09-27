#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-8}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Two-Actor Messaging Demo" \
  "Deploy a sender and a worker, send one deferred message between them, and read the worker's handled task state."

demo_step "Deploy the worker target" "The worker actor is the destination for deferred messages. The deploy runs init, which starts it with an empty task list."
WORKER_ADDR="$(demo_deploy_actor worker \
  --why "Deploy the actor that will receive and record task messages." \
  --expect "The deploy output includes a worker actor address.")"

demo_step "Deploy the sender target" "The sender actor accepts user calls and forwards work to the worker. The deploy runs init; the sender starts without a worker address so configuration is explicit."
SENDER_ADDR="$(demo_deploy_actor sender \
  --why "Deploy the actor that will submit deferred work to the worker." \
  --expect "The deploy output includes a sender actor address and zero sent tasks.")"

demo_step "Configure sender" "This is the composition step: the sender stores the worker address it will message."
demo_call "$SENDER_ADDR" "configure" "{\"worker_address\":\"$WORKER_ADDR\"}" \
  --why "Tell the sender where deferred task messages should go." \
  --expect "The sender stores the worker address."

demo_step "Send task" "The sender writes local sent state and schedules a deferred message to the worker."
demo_call "$SENDER_ADDR" "send_task" '{"title":"Prepare launch checklist","details":"Confirm state, events, and callback wiring."}' \
  --why "Exercise actor-to-actor deferred messaging." \
  --expect "The call succeeds immediately and creates deferred work for the worker."

demo_wait "Waiting for the deferred worker message to execute." "$POST_RUN_SLEEP"

demo_step "Read worker tasks" "The worker state confirms that the message was delivered and handled."
demo_call "$WORKER_ADDR" "get_tasks" '{}' \
  --why "Inspect the target actor after deferred delivery." \
  --expect "The response includes the handled task."

demo_step "Read sender state" "The sender state shows its own local sent counter."
demo_call "$SENDER_ADDR" "get_state" '{}' \
  --why "Inspect the source actor's local accounting." \
  --expect "The sender reports one submitted task."

demo_done
