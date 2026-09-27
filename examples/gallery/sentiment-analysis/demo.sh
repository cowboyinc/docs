#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Sentiment Analysis Demo" "Deploy a sentiment analyzer, submit a text batch, and read the resulting report."
demo_step "Deploy sentiment analyzer" "The actor stores topic configuration and the latest sentiment report. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor analyzer --init '{"topic":"ETH","alert_threshold":-50}' --why "Deploy the sentiment showcase actor." --expect "An actor address is available for analysis calls.")"
demo_step "Analyze sentiment batch" "The input mixes positive and negative statements so the report has something to aggregate."
demo_call "$ACTOR_ADDR" "analyze_sentiment" '{"texts":["ETH dev activity looks strong this week.","Macro conditions still look shaky.","Users are excited about cheaper L2 settlement."]}' --why "Score sentiment across a short text batch." --expect "The actor records the latest sentiment report."
demo_wait "Waiting for the analysis transaction to settle before reading the report." "$POST_RUN_SLEEP"
demo_step "Read latest sentiment report" "This shows the aggregate sentiment and any alert state."
demo_call "$ACTOR_ADDR" "get_last_report" '{}' --why "Read the latest sentiment report." --expect "The response includes the topic, score, and alert status."
demo_step "Read analyzer stats" "Stats show how many batches the actor has processed."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate analyzer counters." --expect "The response includes analysis totals."
demo_done
