#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Spam Detection Demo" "Deploy a spam detector, scan a suspicious message, and inspect the resulting moderation case."
demo_step "Deploy spam detector" "The actor stores a threshold and the latest scan result. The deploy runs init with the configuration in one transaction."
ACTOR_ADDR="$(demo_deploy_actor detector --init '{"spam_threshold":70}' --why "Deploy the spam detection showcase actor." --expect "An actor address is available for the scan.")"
demo_step "Scan suspicious content" "The payload is intentionally spammy so the result should be easy to recognize."
demo_call "$ACTOR_ADDR" "scan_content" '{"channel":"discord","content":"Guaranteed 100x returns. Click here now to claim your prize."}' --why "Classify one incoming message." --expect "The actor records a spam case."
demo_wait "Waiting for the scan transaction to settle before reading the result." "$POST_RUN_SLEEP"
demo_step "Read latest spam case" "This shows the last classification result."
demo_call "$ACTOR_ADDR" "get_last_case" '{}' --why "Read the latest spam case." --expect "The response includes the scanned content and decision."
demo_step "Read detection stats" "Stats summarize activity across scans."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate detector counters." --expect "The response includes scan totals."
demo_done
