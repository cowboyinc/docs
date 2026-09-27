#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Liquidation Oracle Demo" "Deploy a liquidation oracle, evaluate account health, and inspect the liquidation case."
demo_step "Deploy liquidation oracle" "The actor stores market configuration and the latest account evaluation. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor oracle --init '{"market_name":"demo-lending"}' --why "Deploy the liquidation oracle showcase actor." --expect "An actor address is available for account checks.")"
demo_step "Evaluate account health" "The debt and collateral values exercise the liquidation threshold logic."
demo_call "$ACTOR_ADDR" "evaluate_account" '{"account":"0xabc123","collateral_value":100000,"debt_value":76000,"liquidation_threshold":0.8}' --why "Compute liquidation health for one account." --expect "The actor records the latest liquidation case."
demo_wait "Waiting for the oracle transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest oracle case" "This shows the account health decision."
demo_call "$ACTOR_ADDR" "get_last_case" '{}' --why "Read the latest liquidation case." --expect "The response includes account values and liquidation status."
demo_step "Read oracle stats" "Stats summarize account evaluations."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate oracle counters." --expect "The response includes evaluation totals."
demo_done
