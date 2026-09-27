#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "DAO Copilot Demo" "Deploy a governance copilot, analyze a proposal, and inspect the governance memo."
demo_step "Deploy DAO copilot" "The actor stores DAO configuration and the latest proposal analysis. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor copilot --init '{"dao_name":"Cowboy DAO"}' --why "Deploy the DAO copilot showcase actor." --expect "An actor address is available for proposal analysis.")"
demo_step "Analyze proposal" "The sample proposal targets runner grants so the report ties back to Cowboy examples."
demo_call "$ACTOR_ADDR" "analyze_proposal" '{"title":"Fund the runner grants program","body":"Allocate 250k treasury tokens to grow the off-chain runner ecosystem over the next two quarters."}' --why "Generate a governance analysis record." --expect "The actor records the latest governance report."
demo_wait "Waiting for the proposal analysis transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest governance report" "This shows the governance memo stored by the actor."
demo_call "$ACTOR_ADDR" "get_last_report" '{}' --why "Read the latest proposal report." --expect "The response includes proposal title and analysis fields."
demo_step "Read DAO copilot stats" "Stats summarize proposal analysis activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate governance counters." --expect "The response includes analysis totals."
demo_done
