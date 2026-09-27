#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Alerts And Scheduler Demo" "Deploy an alert actor, run a threshold check, and inspect the recorded alert state."
demo_step "Deploy alert scheduler" "The deploy runs init with the check name, threshold, and interval; the actor stores that configuration and the latest check results."
ACTOR_ADDR="$(demo_deploy_actor scheduler --init '{"check_name":"treasury-balance","threshold":42,"interval_blocks":25}' --why "Deploy the alerts and scheduler showcase actor." --expect "An actor address is available for check calls.")"
demo_step "Run scheduled check" "The observed value is below the threshold so the check path is visible."
demo_call "$ACTOR_ADDR" "run_check" '{"observed_value":38}' --why "Evaluate one threshold observation." --expect "The actor records the latest check result."
demo_wait "Waiting for the check transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest check" "This shows the last scheduled check result."
demo_call "$ACTOR_ADDR" "get_last_check" '{}' --why "Read the latest threshold check." --expect "The response includes observed value and alert status."
demo_step "Read scheduler stats" "Stats summarize check activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate scheduler counters." --expect "The response includes check totals."
demo_done
