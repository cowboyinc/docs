#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Capstone Agent Demo" \
  "Deploy a proposal-review actor, submit a proposal, generate an LLM memo, approve it, and inspect final state."

demo_step "Deploy the reviewer target" "This actor combines state, permissions, and runner.llm into one useful workflow. Deploy runs init, which records the deployer as the owner that can approve or reject proposals."
ACTOR_ADDR="$(demo_deploy_actor reviewer \
  --why "Deploy the proposal-review capstone actor with the deployer as owner and zero proposals." \
  --expect "The deploy output includes an actor address.")"

demo_step "Submit proposal" "A normal user action creates the proposal state the agent will review."
demo_call "$ACTOR_ADDR" "submit_proposal" '{"title":"Fund Runner Example Grants","body":"Allocate a small grant pool to developers building reusable Cowboy runner examples and gallery demos."}' \
  --why "Create a proposal record for the capstone workflow." \
  --expect "The actor stores proposal 1 as submitted."

demo_step "Request LLM review" "The review handler submits the proposal body to runner.llm and captures the proposal id for the callback."
demo_call "$ACTOR_ADDR" "review_with_llm" '{"proposal_id":1}' \
  --why "Generate an async review memo for proposal 1." \
  --expect "The transaction succeeds immediately while the review is pending."

demo_wait "Waiting for the runner callback to store the review memo." "$POST_RUN_SLEEP"

demo_step "Read reviewed proposal" "This read shows the submitted proposal plus the async review status and memo."
demo_call "$ACTOR_ADDR" "get_proposal" '{"proposal_id":1}' \
  --why "Inspect the proposal after the LLM callback." \
  --expect "The proposal includes review metadata."

demo_step "Approve proposal" "Owner-gated approval demonstrates permissions in the complete workflow."
demo_call "$ACTOR_ADDR" "approve" '{"proposal_id":1}' \
  --why "Finalize the proposal as the owner." \
  --expect "The actor marks proposal 1 approved."

demo_step "Read final stats" "The stats view summarizes the workflow outcome."
demo_call "$ACTOR_ADDR" "get_stats" '{}' \
  --why "Inspect aggregate proposal counts." \
  --expect "The stats include one proposal and one approval."

demo_done
