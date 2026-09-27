#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLES_DIR="$ROOT_DIR/examples"
RPC_PORT="${RPC_PORT:-5000}"
RPC_URL="${RPC_URL:-http://localhost:$RPC_PORT}"
EXAMPLES_MODE="${COWBOY_EXAMPLES_MODE:-core}"
export RPC_URL
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"

SELECTED_EXAMPLES=("$@")
SUMMARY_LABELS=()
SUMMARY_STATUSES=()
KNOWN_EXAMPLE_KEYS=(
  core/00-quickstart-hello
  core/01-state-events-queries
  core/02-senders-and-permissions
  core/03-two-actor-messaging
  core/04-multi-actor-workflow
  core/05-tokens-and-balances
  core/06-token-escrow
  core/07-timers-and-automation
  core/08-minimal-runner-continuation
  core/09-runner-llm
  core/10-async-cache-refresh
  core/11-failure-recovery
  core/12-capstone-agent
  gallery/advanced-messaging-ring
  gallery/rebalancing-agent
  gallery/liquidity-pools
  gallery/copy-trading
  gallery/spam-detection
  gallery/content-recommendations
  gallery/sentiment-analysis
  gallery/audio-transcription
  gallery/image-generation
  gallery/content-moderator
  gallery/alerts-and-schedulers
  gallery/liquidation-oracle
  gallery/dao-copilot
  gallery/compliance
  gallery/fraud-watcher
  gallery/contract-summarizer
  gallery/hn-feed
  gallery/casino-rounds
  gallery/blackjack
)

normalize_example_selector() {
  local value="$1"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  value="${value// /-}"
  value="${value#examples/}"
  echo "$value"
}

should_run_mode() {
  local track="$1"
  [[ "$EXAMPLES_MODE" == "all" || "$EXAMPLES_MODE" == "$track" ]]
}

should_run_example() {
  local key="$1"
  local track="${key%%/*}"
  local base="${key#*/}"
  local short="${base%%-*}"
  local slug="${base#*-}"
  local selector

  should_run_mode "$track" || return 1

  if [[ "${#SELECTED_EXAMPLES[@]}" -eq 0 ]]; then
    return 0
  fi

  for selector in "${SELECTED_EXAMPLES[@]}"; do
    selector="$(normalize_example_selector "$selector")"
    if [[ "$selector" == "$key" || "$selector" == "$base" || "$selector" == "$short" || "$selector" == "$slug" || "$selector" == "$track/$short" || "$selector" == "$track/$slug" ]]; then
      return 0
    fi
  done

  return 1
}

selector_matches_key() {
  local key="$1"
  local selector="$2"
  local track="${key%%/*}"
  local base="${key#*/}"
  local short="${base%%-*}"
  local slug="${base#*-}"

  [[ "$selector" == "$key" || "$selector" == "$base" || "$selector" == "$short" || "$selector" == "$slug" || "$selector" == "$track/$short" || "$selector" == "$track/$slug" ]]
}

validate_selected_examples() {
  local selector normalized key matched

  if [[ "${#SELECTED_EXAMPLES[@]}" -eq 0 ]]; then
    return 0
  fi

  for selector in "${SELECTED_EXAMPLES[@]}"; do
    normalized="$(normalize_example_selector "$selector")"
    matched=0
    for key in "${KNOWN_EXAMPLE_KEYS[@]}"; do
      should_run_mode "${key%%/*}" || continue
      if selector_matches_key "$key" "$normalized"; then
        matched=1
        break
      fi
    done
    if [[ "$matched" -ne 1 ]]; then
      echo "No examples matched mode '$EXAMPLES_MODE' and selector: $selector"
      return 1
    fi
  done
}

check_validator_rpc() {
  local health
  health="$(curl -fsS --max-time 5 "$RPC_URL/health" 2>/dev/null || true)"

  if [[ -z "$health" ]]; then
    echo "Expected a Cowboy validator at $RPC_URL, but /health did not return a response."
    echo "Make sure the validator is running and listening on that RPC URL."
    exit 1
  fi

  if printf "%s" "$health" | grep -qiE '<!DOCTYPE html|<html'; then
    echo "Expected a Cowboy validator at $RPC_URL, but /health returned HTML instead of validator health."
    echo "Another local service is using that port. Point RPC_URL at the validator or stop the conflicting app."
    exit 1
  fi
}

run_step() {
  local label="$1"
  shift
  echo
  echo "============================================"
  echo "  $label"
  echo "============================================"
  if "$@"; then
    SUMMARY_LABELS+=("$label")
    SUMMARY_STATUSES+=("success")
    return 0
  fi

  SUMMARY_LABELS+=("$label")
  SUMMARY_STATUSES+=("failed")
  return 1
}

run_example() {
  local key="$1"
  local label="$2"
  shift 2

  if should_run_example "$key"; then
    if ! run_step "$label" "$@"; then
      echo
      echo "Example failed: $label"
      echo "Continuing to next example..."
    fi
  fi
}

print_summary() {
  local i
  local failed=0

  echo
  echo "============================================"
  echo "  Example Summary"
  echo "============================================"
  for ((i = 0; i < ${#SUMMARY_LABELS[@]}; i++)); do
    echo "  ${SUMMARY_LABELS[$i]}: ${SUMMARY_STATUSES[$i]}"
    if [[ "${SUMMARY_STATUSES[$i]}" == "failed" ]]; then
      failed=1
    fi
  done

  if [[ "${#SUMMARY_LABELS[@]}" -eq 0 ]]; then
    echo "  No examples matched mode '$EXAMPLES_MODE' and selectors: ${SELECTED_EXAMPLES[*]:-(none)}"
    return 1
  fi

  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi
  return 0
}

case "$EXAMPLES_MODE" in
  core|gallery|all) ;;
  *)
    echo "Unsupported COWBOY_EXAMPLES_MODE: $EXAMPLES_MODE" >&2
    exit 1
    ;;
esac

validate_selected_examples
check_validator_rpc

run_example "core/00-quickstart-hello" "Core 00 Quickstart Hello" \
  bash "$EXAMPLES_DIR/core/00-quickstart-hello/demo.sh" demo

run_example "core/01-state-events-queries" "Core 01 State Events Queries" \
  bash "$EXAMPLES_DIR/core/01-state-events-queries/demo.sh" demo

run_example "core/02-senders-and-permissions" "Core 02 Senders And Permissions" \
  bash "$EXAMPLES_DIR/core/02-senders-and-permissions/demo.sh"

run_example "core/03-two-actor-messaging" "Core 03 Two Actor Messaging" \
  bash "$EXAMPLES_DIR/core/03-two-actor-messaging/demo.sh"

run_example "core/04-multi-actor-workflow" "Core 04 Multi Actor Workflow" \
  bash "$EXAMPLES_DIR/core/04-multi-actor-workflow/demo.sh"

run_example "core/05-tokens-and-balances" "Core 05 Tokens And Balances" \
  bash "$EXAMPLES_DIR/core/05-tokens-and-balances/demo.sh" "$RPC_URL"

run_example "core/06-token-escrow" "Core 06 Token Escrow" \
  bash "$EXAMPLES_DIR/core/06-token-escrow/demo.sh"

run_example "core/07-timers-and-automation" "Core 07 Timers And Automation" \
  bash "$EXAMPLES_DIR/core/07-timers-and-automation/demo.sh" demo

run_example "core/08-minimal-runner-continuation" "Core 08 Minimal Runner Continuation" \
  bash "$EXAMPLES_DIR/core/08-minimal-runner-continuation/demo.sh" demo

run_example "core/09-runner-llm" "Core 09 Runner LLM" \
  bash "$EXAMPLES_DIR/core/09-runner-llm/demo.sh" demo

run_example "core/10-async-cache-refresh" "Core 10 Async Cache Refresh" \
  bash "$EXAMPLES_DIR/core/10-async-cache-refresh/demo.sh" demo

run_example "core/11-failure-recovery" "Core 11 Failure Recovery" \
  bash "$EXAMPLES_DIR/core/11-failure-recovery/demo.sh" demo

run_example "core/12-capstone-agent" "Core 12 Capstone Agent" \
  bash "$EXAMPLES_DIR/core/12-capstone-agent/demo.sh"

run_example "gallery/advanced-messaging-ring" "Gallery Advanced Messaging Ring" \
  bash "$EXAMPLES_DIR/gallery/advanced-messaging-ring/deploy_ring.sh" "$RPC_URL" 2

run_example "gallery/rebalancing-agent" "Gallery Rebalancing Agent" \
  bash "$EXAMPLES_DIR/gallery/rebalancing-agent/demo.sh" "$RPC_URL"

run_example "gallery/liquidity-pools" "Gallery Liquidity Pools" \
  bash "$EXAMPLES_DIR/gallery/liquidity-pools/demo.sh" "$RPC_URL"

run_example "gallery/copy-trading" "Gallery Copy Trading" \
  bash "$EXAMPLES_DIR/gallery/copy-trading/demo.sh"

run_example "gallery/spam-detection" "Gallery Spam Detection" \
  bash "$EXAMPLES_DIR/gallery/spam-detection/demo.sh"

run_example "gallery/content-recommendations" "Gallery Content Recommendations" \
  bash "$EXAMPLES_DIR/gallery/content-recommendations/demo.sh"

run_example "gallery/sentiment-analysis" "Gallery Sentiment Analysis" \
  bash "$EXAMPLES_DIR/gallery/sentiment-analysis/demo.sh"

run_example "gallery/audio-transcription" "Gallery Audio Transcription" \
  bash "$EXAMPLES_DIR/gallery/audio-transcription/demo.sh"

run_example "gallery/image-generation" "Gallery Image Generation" \
  bash "$EXAMPLES_DIR/gallery/image-generation/demo.sh"

run_example "gallery/content-moderator" "Gallery Content Moderator" \
  bash "$EXAMPLES_DIR/gallery/content-moderator/demo.sh"

run_example "gallery/alerts-and-schedulers" "Gallery Alerts And Schedulers" \
  bash "$EXAMPLES_DIR/gallery/alerts-and-schedulers/demo.sh"

run_example "gallery/liquidation-oracle" "Gallery Liquidation Oracle" \
  bash "$EXAMPLES_DIR/gallery/liquidation-oracle/demo.sh"

run_example "gallery/dao-copilot" "Gallery DAO Copilot" \
  bash "$EXAMPLES_DIR/gallery/dao-copilot/demo.sh"

run_example "gallery/compliance" "Gallery Compliance" \
  bash "$EXAMPLES_DIR/gallery/compliance/demo.sh"

run_example "gallery/fraud-watcher" "Gallery Fraud Watcher" \
  bash "$EXAMPLES_DIR/gallery/fraud-watcher/demo.sh"

run_example "gallery/contract-summarizer" "Gallery Contract Summarizer" \
  bash "$EXAMPLES_DIR/gallery/contract-summarizer/demo.sh"

run_example "gallery/hn-feed" "Gallery HN Feed" \
  bash "$EXAMPLES_DIR/gallery/hn-feed/demo.sh"

run_example "gallery/casino-rounds" "Gallery Casino Rounds" \
  bash "$EXAMPLES_DIR/gallery/casino-rounds/demo.sh"

run_example "gallery/blackjack" "Gallery Blackjack" \
  bash "$EXAMPLES_DIR/gallery/blackjack/demo.sh"

echo
echo "All selected examples have finished running."
if ! print_summary; then
  exit 1
fi
