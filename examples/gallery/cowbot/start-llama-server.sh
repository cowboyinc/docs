#!/usr/bin/env bash
# start-llama-server.sh — launch llama-server with the Qwen3-Coder GGUF and
# the right flags for OpenAI-compatible tool calling.
#
# Pair with demo.sh by running this in one terminal and
#   COWBOT_MODEL=local ./demo.sh
# in another.

set -euo pipefail

MODEL_PATH="${COWBOT_GGUF:-${HOME}/models/qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf}"
PORT="${LLAMA_PORT:-8080}"
HOST="${LLAMA_HOST:-127.0.0.1}"
CTX="${LLAMA_CTX:-32768}"
NGL="${LLAMA_NGL:-99}"
THREADS="${LLAMA_THREADS:--1}"

if [[ ! -f "${MODEL_PATH}" ]]; then
  echo "error: model not found: ${MODEL_PATH}" >&2
  echo "set COWBOT_GGUF or wait for the download to finish." >&2
  exit 2
fi

if ! command -v llama-server >/dev/null; then
  echo "error: llama-server not on PATH (brew install llama.cpp)" >&2
  exit 2
fi

echo "═══════════════════════════════════════════════════════════════"
echo " llama-server"
echo "═══════════════════════════════════════════════════════════════"
echo " model    : ${MODEL_PATH}"
echo " endpoint : http://${HOST}:${PORT}/v1/chat/completions"
echo " context  : ${CTX} tokens"
echo " gpu lyrs : ${NGL} (Metal on Apple silicon)"
echo "═══════════════════════════════════════════════════════════════"
echo
echo "Pair with: COWBOT_MODEL=local ./demo.sh"
echo

# Flags:
#   --jinja                use the model's actual Jinja chat template
#                          (required for proper tool-calling round-trips)
#   --reasoning-format none disables Qwen3 thinking traces in the response
#   -c                     context window
#   -ngl                   layers offloaded to GPU (Metal)
#   --metrics              expose /metrics for observability
exec llama-server \
  --model "${MODEL_PATH}" \
  --jinja \
  --reasoning-format none \
  --host "${HOST}" \
  --port "${PORT}" \
  -c "${CTX}" \
  -ngl "${NGL}" \
  --threads "${THREADS}" \
  --metrics
