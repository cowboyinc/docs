#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-8}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Async Cache Refresh Demo" \
  "Deploy a cache-backed oracle actor, request a refresh, wait for the async fetch path, and read the cached record and status."

demo_step "Deploy the cache target" "The actor demonstrates the read-through cache pattern for off-chain data. Deploy runs init, which starts the cache empty with idle status fields."
ACTOR_ADDR="$(demo_deploy_actor cache \
  --why "Deploy the oracle cache actor with its cache in a known empty state." \
  --expect "The deploy output includes an actor address.")"

demo_step "Request cache refresh" "This call asks the actor to refresh data through the async path."
demo_call "$ACTOR_ADDR" "request_refresh" '{}' \
  --why "Kick off the read-through refresh flow." \
  --expect "The actor accepts the refresh request."

demo_wait "Waiting for the oracle refresh path to settle before reading the cache." "$POST_RUN_SLEEP"

demo_step "Read cached record" "The record query shows what data the refresh path stored."
demo_call "$ACTOR_ADDR" "get_cached_record" '{}' \
  --why "Read the value served by the cache." \
  --expect "The response contains the latest cached record or a clear empty/pending state."

demo_step "Read cache status" "The status query separates metadata from the cached payload."
demo_call "$ACTOR_ADDR" "get_status" '{}' \
  --why "Inspect refresh metadata such as status and timestamps." \
  --expect "The response explains the cache's current refresh state."

demo_done
