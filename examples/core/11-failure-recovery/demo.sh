#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-6}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Failure Recovery Demo" \
  "Deploy the async recovery example, inspect the starting status, submit a request, wait for settlement, and read the status transition."

demo_step "Deploy the recovery target" "The actor demonstrates observable state around an async request that may fail or recover."
ACTOR_ADDR="$(demo_deploy_actor recovery \
  --why "Deploy the failure recovery actor." \
  --expect "The deploy output includes an actor address.")"

demo_step "Read initial status" "This first read is intentional: there is no init mutation in this lesson."
demo_call "$ACTOR_ADDR" "get_status" '{}' \
  --why "Capture the baseline before submitting work." \
  --expect "The actor reports its initial idle or empty state."

demo_step "Submit recovery request" "The request moves the actor into the recovery workflow."
demo_call "$ACTOR_ADDR" "submit_request" '{"payload":{"job":"demo"}}' \
  --why "Start the async request whose outcome will be reflected in status." \
  --expect "The actor accepts the request and records the attempted work."

demo_wait "Waiting for the request attempt to settle before reading the final recovery state." "$POST_RUN_SLEEP"

demo_step "Read final status" "The final read shows the state transition caused by the submitted request."
demo_call "$ACTOR_ADDR" "get_status" '{}' \
  --why "Inspect whether the workflow completed, failed, or recovered." \
  --expect "The response contains the latest recovery status."

demo_done
