#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Compliance Screening Demo" "Deploy a sanctions screening actor, screen one entity, and inspect the stored result."
demo_step "Deploy sanctions filter" "The deploy runs init, which loads the demo sanctions lists; the actor stores the latest screening result and aggregate counters."
ACTOR_ADDR="$(demo_deploy_actor screener --why "Deploy the compliance showcase actor." --expect "An actor address is available for screening calls.")"
demo_step "Screen entity" "The sample address exercises the deterministic screening path."
demo_call "$ACTOR_ADDR" "screen_entity" '{"query":"0xdeadbeef"}' --why "Evaluate one entity against the filter." --expect "The actor records the latest screen."
demo_wait "Waiting for the screening transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest screen" "This shows the stored compliance result."
demo_call "$ACTOR_ADDR" "get_last_screen" '{}' --why "Read the latest sanctions screen." --expect "The response includes the query and decision."
demo_step "Read compliance stats" "Stats summarize screening activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate compliance counters." --expect "The response includes screen totals."
demo_done
