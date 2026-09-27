#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Content Moderation Demo" "Deploy a moderation actor, evaluate a hostile post, and inspect the moderation case."
demo_step "Deploy content moderator" "The deploy runs init with the policy name that identifies which moderation rules are being simulated; the actor stores that profile and the latest moderation decision."
ACTOR_ADDR="$(demo_deploy_actor moderator --init '{"policy_name":"community-default"}' --why "Deploy the content moderation showcase actor." --expect "An actor address is available for moderation calls.")"
demo_step "Moderate content" "The sample post is hostile enough to exercise the decision path."
demo_call "$ACTOR_ADDR" "moderate_content" '{"content_type":"post","content":"You are all idiots and should leave this community forever."}' --why "Evaluate one piece of user content." --expect "The actor records a moderation case."
demo_wait "Waiting for the moderation transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest moderation case" "This shows the latest moderation decision."
demo_call "$ACTOR_ADDR" "get_last_case" '{}' --why "Read the latest moderation case." --expect "The response includes the content type and decision."
demo_step "Read moderation stats" "Stats summarize moderation activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate moderation counters." --expect "The response includes case totals."
demo_done
