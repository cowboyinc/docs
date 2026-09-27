#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Content Recommendation Demo" "Deploy a recommender, score a small catalog, and inspect the selected recommendation."
demo_step "Deploy recommender" "The deploy runs init with the catalog name that scopes the recommendation result; the actor keeps that configuration and the latest recommendation."
ACTOR_ADDR="$(demo_deploy_actor recommender --init '{"catalog_name":"demo-catalog"}' --why "Deploy the recommendation showcase actor." --expect "An actor address is available for recommendation calls.")"
demo_step "Generate recommendations" "The candidates cover several topics so ranking behavior is visible."
demo_call "$ACTOR_ADDR" "recommend_content" '{"profile":{"likes":["defi","ai"],"reading_time":"short"},"candidates":[{"title":"Cowboy AI Agents","tags":["ai","infra"]},{"title":"Stablecoin Risk Review","tags":["defi","risk"]},{"title":"Sports Roundup","tags":["sports"]}]}' --why "Rank candidate content for the provided profile." --expect "The actor records the winning recommendation."
demo_wait "Waiting for the recommendation transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest recommendation" "This shows the chosen item and score details."
demo_call "$ACTOR_ADDR" "get_last_recommendation" '{}' --why "Read the most recent recommendation." --expect "The response identifies the top-ranked content."
demo_step "Read recommender stats" "Stats show the number of recommendation requests handled."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate recommendation counters." --expect "The response includes request totals."
demo_done
