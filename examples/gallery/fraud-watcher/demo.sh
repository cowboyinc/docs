#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Fraud Watcher Demo" "Deploy a fraud watcher, analyze a suspicious transaction, and inspect the resulting case."
demo_step "Deploy fraud watcher" "The actor stores a risk threshold and the latest transaction analysis. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor watcher --init '{"risk_threshold":70}' --why "Deploy the fraud watcher showcase actor." --expect "An actor address is available for transaction analysis.")"
demo_step "Analyze suspicious transaction" "The sample context describes rapid withdrawals to exercise the risk path."
demo_call "$ACTOR_ADDR" "analyze_transaction" '{"from_addr":"0x1111","to_addr":"0x2222","amount":9800,"context":"three rapid withdrawals in one hour"}' --why "Score one transaction for fraud risk." --expect "The actor records a fraud case."
demo_wait "Waiting for the analysis transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest fraud case" "This shows the stored risk decision."
demo_call "$ACTOR_ADDR" "get_last_case" '{}' --why "Read the latest fraud case." --expect "The response includes transaction details and risk score."
demo_step "Read fraud stats" "Stats summarize fraud analysis activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate fraud counters." --expect "The response includes analysis totals."
demo_done
