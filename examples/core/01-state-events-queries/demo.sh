#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-2}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "State Events Queries Demo" \
  "Deploy a counter actor, mutate stored state, emit an event, and read the final state."

demo_step "Deploy the counter target" "This actor is intentionally tiny: one state value, one mutating handler, and one query handler. The deploy runs init with a start of 2 so the later increment is easy to verify."
ACTOR_ADDR="$(demo_deploy_actor counter --init '{"start":2}' \
  --why "Deploy the counter actor that demonstrates state, events, and queries, starting the count at 2." \
  --expect "The deploy output includes an actor address and the actor stores count = 2.")"

demo_step "Increment counter" "The increment handler mutates state and emits the event this lesson is about."
demo_call "$ACTOR_ADDR" "increment" '{"amount":5}' \
  --why "Add 5 to the stored counter." \
  --expect "The result shows a successful execution and an increment event."

demo_wait "Give the node a short moment before reading the final state." "$POST_RUN_SLEEP"

demo_step "Read counter state" "The query should show that state changed from 2 to 7."
demo_call "$ACTOR_ADDR" "get_state" '{}' \
  --why "Confirm the mutation persisted." \
  --expect "The response contains the updated counter value."

demo_done
