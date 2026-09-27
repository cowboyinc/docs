#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  exec "$SCRIPT_DIR/test_examples_parallel.sh" --help
fi

export RPC_URL="${RPC_URL:-http://localhost:4000}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
export DEMO_PARALLEL_JOBS=1

exec "$SCRIPT_DIR/test_examples_parallel.sh" \
  core/00-quickstart-hello \
  core/01-state-events-queries \
  core/02-senders-and-permissions \
  core/03-two-actor-messaging \
  core/04-multi-actor-workflow \
  core/05-tokens-and-balances \
  core/06-token-escrow \
  core/07-timers-and-automation \
  core/08-minimal-runner-continuation \
  core/09-runner-llm \
  core/10-async-cache-refresh \
  core/11-failure-recovery \
  core/12-capstone-agent \
  gallery/rebalancing-agent \
  gallery/liquidity-pools \
  gallery/copy-trading \
  gallery/spam-detection \
  gallery/content-recommendations \
  gallery/sentiment-analysis \
  gallery/audio-transcription \
  gallery/image-generation \
  gallery/content-moderator \
  gallery/alerts-and-schedulers \
  gallery/liquidation-oracle \
  gallery/dao-copilot \
  gallery/compliance \
  gallery/fraud-watcher \
  gallery/contract-summarizer \
  gallery/hn-feed \
  gallery/casino-rounds \
  gallery/blackjack
