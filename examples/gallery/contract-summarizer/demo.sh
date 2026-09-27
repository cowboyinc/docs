#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-12}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin "Contract Summarizer Demo" "Deploy a contract summarizer, analyze a source snippet, and inspect the generated summary."
demo_step "Deploy contract summarizer" "The deploy runs init with the profile name that identifies the risk lens for summaries; the actor stores that profile and the latest contract summary."
ACTOR_ADDR="$(demo_deploy_actor summarizer --init '{"profile_name":"evm-risk"}' --why "Deploy the contract summarizer showcase actor." --expect "An actor address is available for source analysis.")"
demo_step "Analyze contract source" "The source contains privileged mint and fee functions so the summary has concrete features to mention."
demo_call "$ACTOR_ADDR" "analyze_contract" '{"address":"0x1234","source":"function mint(address to,uint256 amount) external onlyOwner { _mint(to, amount); } function setFee(uint256 nextFee) external onlyOwner { fee = nextFee; }"}' --why "Generate a summary for one contract snippet." --expect "The actor records the latest contract summary."
demo_wait "Waiting for the analysis transaction to settle before reading state." "$POST_RUN_SLEEP"
demo_step "Read latest summary" "This shows the stored contract summary."
demo_call "$ACTOR_ADDR" "get_last_summary" '{}' --why "Read the latest contract summary." --expect "The response includes address and summary fields."
demo_step "Read summarizer stats" "Stats summarize contract analysis activity."
demo_call "$ACTOR_ADDR" "get_stats" '{}' --why "Read aggregate summarizer counters." --expect "The response includes analysis totals."
demo_done
