#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-4}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Timers And Automation Demo" \
  "Deploy a timer-only actor, start the scheduled loop, wait for timer ticks, and read the timer state."

demo_step "Deploy the ticker target" "The actor demonstrates scheduler-driven work without an LLM or external runner. Deploy runs init, which clears the counter before the scheduled loop starts."
ACTOR_ADDR="$(demo_deploy_actor ticker \
  --why "Deploy the timer counter actor with its state ready to start ticking." \
  --expect "The deploy output includes an actor address.")"

demo_step "Start timer loop" "The start handler schedules future timer callbacks."
demo_call "$ACTOR_ADDR" "start" '{}' \
  --why "Ask the actor to schedule timer-driven work." \
  --expect "The execution succeeds and scheduled callbacks can advance the counter."

demo_wait "Waiting specifically for timer callbacks to run and update the actor state." "$POST_RUN_SLEEP"

demo_step "Read timer state" "The state read should show timer activity after the wait."
demo_call "$ACTOR_ADDR" "get_state" '{}' \
  --why "Inspect the timer counter after scheduled callbacks." \
  --expect "The response reflects the current timer state."

demo_done
