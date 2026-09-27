#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
ACTOR_TARGET="${ACTOR_TARGET:-disclosure}"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Copy Trading Demo" "Deploy a disclosure-following actor, ingest a mock trade disclosure, and inspect the suggested mirrored trade."
demo_step "Deploy copy-trading actor" "The actor stores portfolio limits and the latest mirrored trade suggestion. The deploy runs init with the portfolio limits in one transaction."
ACTOR_ADDR="$(demo_deploy_actor "$ACTOR_TARGET" --init '{"target_member":"Nancy Pelosi","broker":"paper-broker","max_position_usd":10000}' --why "Deploy the copy-trading showcase actor." --expect "An actor address is available for disclosure ingestion.")"
demo_step "Ingest disclosure" "The mock disclosure is larger than the configured max position, so sizing behavior is visible."
demo_call "$ACTOR_ADDR" "ingest_disclosure" '{"ticker":"NVDA","side":"buy","reported_size_usd":18000,"delay_days":34,"filing_text":"PTR shows a disclosed purchase in the semiconductor sector."}' --why "Convert a disclosure into a mirrored trade suggestion." --expect "The actor records the latest trade recommendation."
demo_wait "Waiting for the disclosure transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest trade" "This shows the suggested copied trade."
demo_call "$ACTOR_ADDR" "get_last_trade" '{}' --why "Read the latest mirrored trade suggestion." --expect "The response includes ticker, side, and capped position size."
demo_step "Read copy-trading stats" "Stats summarize ingested disclosures."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate copy-trading counters." --expect "The response includes disclosure totals."
demo_done
