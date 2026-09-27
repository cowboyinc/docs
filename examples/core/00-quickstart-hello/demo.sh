#!/usr/bin/env bash
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "$0")" && pwd)"
POST_RUN_SLEEP="${POST_RUN_SLEEP:-2}"
source "$EXAMPLE_DIR/../../_demo_lib.sh"

demo_begin \
  "Quickstart Hello Demo" \
  "Deploy the smallest curriculum actor with a greeting, send one message, and read the stored greeting back."

demo_step "Deploy the hello target" "Deploy creates the actor and runs init with the greeting in one transaction."
ACTOR_ADDR="$(demo_deploy_actor hello --init '{"greeting":"howdy"}' \
  --why "Deploy the minimal hello actor and store the greeting later calls will read." \
  --expect "The deploy output includes an actor address.")"

demo_step "Say hello" "This call combines submitted input with the initialized greeting."
demo_call "$ACTOR_ADDR" "say_hello" '{"name":"builder"}' \
  --why "Exercise a stateful command handler with a user-supplied name." \
  --expect "The actor emits or returns a hello message for builder."

demo_wait "Give the node a short moment to make the latest state queryable." "$POST_RUN_SLEEP"

demo_step "Read greeting" "The query confirms that the actor kept its initialized state."
demo_call "$ACTOR_ADDR" "get_greeting" '{}' \
  --why "Read the stored greeting after the command call." \
  --expect "The response still contains howdy."

demo_done
