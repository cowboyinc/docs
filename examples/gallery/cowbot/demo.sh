#!/usr/bin/env bash
# demo.sh — agent-loop smoke test for the runner-agent crate.
#
# Drives AgentExecutor against a real LLM endpoint and a local fixture
# directory. No chain, no dispatcher, no actor — just verify the loop
# (LLM call → tool calls → final answer) works against your model.
#
# Usage:
#   ./demo.sh                              # default battery of queries
#   ./demo.sh "your question"              # one-off query
#   COWBOT_MODEL=qwen3-coder ./demo.sh
#   COWBOT_KNOWLEDGE_DIR=/path/to/repo ./demo.sh
#   COWBOT_SESSION_ID=alice ./demo.sh      # persistent session
#   COWBOT_RUNNER_DIR=/path/to/runner ./demo.sh
#   COWBOT_RELEASE=1 ./demo.sh             # build with --release
#
# Required env (one of these, matching COWBOT_MODEL):
#   MOONSHOT_API_KEY    for kimi-k2 (default)
#   DASHSCOPE_API_KEY   for qwen3-coder
#   DEEPSEEK_API_KEY    for deepseek-v3
#   SELF_HOSTED_LLM_KEY for self-hosted

set -euo pipefail

# Default RUNNER_DIR assumes the user's standard layout where the
# cowboyinc/runner clone lives at ~/dev/cowboy/runner. Override with
# COWBOT_RUNNER_DIR.
RUNNER_DIR="${COWBOT_RUNNER_DIR:-${HOME}/dev/cowboy/runner}"

MODEL="${COWBOT_MODEL:-kimi-k2}"
KNOWLEDGE_DIR="${COWBOT_KNOWLEDGE_DIR:-${RUNNER_DIR}/crates/runner-agent/tests/fixtures/knowledge}"
SESSION_ID="${COWBOT_SESSION_ID:-}"
BUILD_PROFILE="${COWBOT_RELEASE:+--release}"

# Map model → required env var.
case "${MODEL}" in
  kimi-k2|kimi)              REQUIRED_KEY="MOONSHOT_API_KEY" ;;
  qwen3-coder|qwen)          REQUIRED_KEY="DASHSCOPE_API_KEY" ;;
  deepseek-v3|deepseek)      REQUIRED_KEY="DEEPSEEK_API_KEY" ;;
  self-hosted|local)         REQUIRED_KEY="SELF_HOSTED_LLM_KEY" ;;
  *)                         REQUIRED_KEY="MOONSHOT_API_KEY" ;;
esac

# Local llama-server needs no real auth, but the executor refuses to start
# without a non-empty key. Default to a sentinel and point at llama-server.
if [[ "${MODEL}" == "local" || "${MODEL}" == "self-hosted" ]]; then
  : "${SELF_HOSTED_LLM_KEY:=local}"
  : "${OPENAI_API_BASE:=http://127.0.0.1:${LLAMA_PORT:-8080}/v1}"
  : "${AGENT_LOCAL_MODEL_ID:=qwen3-coder-30b-a3b}"
  export SELF_HOSTED_LLM_KEY OPENAI_API_BASE AGENT_LOCAL_MODEL_ID

  # Verify llama-server is actually up before going further.
  if ! curl -fsS "${OPENAI_API_BASE%/v1}/health" >/dev/null 2>&1; then
    echo "error: llama-server not reachable at ${OPENAI_API_BASE%/v1}/health" >&2
    echo "  start it first: ./start-llama-server.sh" >&2
    exit 2
  fi
fi

if [[ -z "${!REQUIRED_KEY:-}" ]]; then
  echo "error: ${REQUIRED_KEY} not set (required for model '${MODEL}')" >&2
  exit 2
fi

if [[ ! -d "${KNOWLEDGE_DIR}" ]]; then
  echo "error: knowledge dir does not exist: ${KNOWLEDGE_DIR}" >&2
  exit 2
fi

echo "═══════════════════════════════════════════════════════════════"
echo " cowbot smoke test"
echo "═══════════════════════════════════════════════════════════════"
echo " model         : ${MODEL}"
echo " api key env   : ${REQUIRED_KEY} (set)"
echo " knowledge dir : ${KNOWLEDGE_DIR}"
echo " session id    : ${SESSION_ID:-(none — stateless)}"
echo " build profile : ${BUILD_PROFILE:-debug}"
echo "═══════════════════════════════════════════════════════════════"

echo
echo "→ building example..."
(cd "${RUNNER_DIR}" && cargo build ${BUILD_PROFILE} -p runner-agent --example cowbot_smoke 2>&1 | tail -5)

# Determine the example binary path.
if [[ -n "${BUILD_PROFILE}" ]]; then
  BIN="${RUNNER_DIR}/target/release/examples/cowbot_smoke"
else
  BIN="${RUNNER_DIR}/target/debug/examples/cowbot_smoke"
fi

if [[ ! -x "${BIN}" ]]; then
  echo "error: example binary not found at ${BIN}" >&2
  exit 1
fi

# If a single positional query was passed, run just that.
if [[ $# -ge 1 ]]; then
  echo
  echo "→ running one-off query"
  exec "${BIN}" "$1" "${KNOWLEDGE_DIR}" "${MODEL}" "${SESSION_ID}"
fi

# Default battery: each query targets a different tool path.
QUERIES=(
  "What CIPs are in this repo? List them with their status."
  "Which file defines the runner JobType variants? Cite the path."
  "What does CIP-9 say about access modes? Quote the relevant lines."
  "Find any mentions of 'threshold' or 'IBE' across the docs."
)

PASSES=0
FAILS=0
for q in "${QUERIES[@]}"; do
  echo
  echo "─────────────────────────────────────────────────────────────"
  echo "→ ${q}"
  echo "─────────────────────────────────────────────────────────────"
  if "${BIN}" "${q}" "${KNOWLEDGE_DIR}" "${MODEL}" "${SESSION_ID}"; then
    PASSES=$((PASSES + 1))
  else
    FAILS=$((FAILS + 1))
  fi
done

echo
echo "═══════════════════════════════════════════════════════════════"
echo " summary: ${PASSES} passed, ${FAILS} failed (of ${#QUERIES[@]})"
echo "═══════════════════════════════════════════════════════════════"
[[ ${FAILS} -eq 0 ]]
