#!/usr/bin/env bash
# local-smoke.sh — drive the runner-agent loop end-to-end against a
# local llama.cpp serving Qwen3-Coder-30B-A3B (or any tool-calling
# OpenAI-compatible model). No chain, no validator, no relays — just
# proves the agent crate's tool-use loop + LLM round-trip + filesystem
# sandbox tools all work together.
#
# Mirrors the pattern in cbfs-internal/test-e2e-full.sh (start LLM
# backend → run client → assert result), trimmed to just the LLM-side
# smoke. The chain-backed path is covered by deploy.sh plus the gateway flow.
#
# Usage:
#   ./local-smoke.sh
#   ./local-smoke.sh "your question"
#   COWBOT_RUNNER_DIR=/path/to/runner ./local-smoke.sh
#   COWBOT_GGUF=/path/to/model.gguf ./local-smoke.sh
#   COWBOT_KEEP_LLAMA=1 ./local-smoke.sh    # leave llama-server running on exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="${COWBOT_RUNNER_DIR:-${HOME}/dev/cowboy/runner}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
MODEL_PATH="${COWBOT_GGUF:-${HOME}/models/qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf}"
KNOWLEDGE_DIR="${COWBOT_KNOWLEDGE_DIR:-${RUNNER_DIR}/crates/runner-agent/tests/fixtures/knowledge}"
QUERY="${1:-What CIPs are described in this knowledge dir? Cite file paths.}"
LOG_FILE="/tmp/cowbot-llama.log"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
log()  { echo; echo -e "${CYAN}▸ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✅ $*${NC}"; }
warn() { echo -e "  ${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "  ${RED}❌ $*${NC}" >&2; exit 1; }

LLAMA_PID=""
cleanup() {
    if [[ -n "$LLAMA_PID" && "${COWBOT_KEEP_LLAMA:-0}" != "1" ]]; then
        echo
        log "stopping llama-server (pid=$LLAMA_PID)"
        kill "$LLAMA_PID" 2>/dev/null || true
        wait "$LLAMA_PID" 2>/dev/null || true
    elif [[ -n "$LLAMA_PID" ]]; then
        warn "leaving llama-server running (pid=$LLAMA_PID), tail logs at $LOG_FILE"
    fi
}
trap cleanup EXIT

# ── Step 0: prereqs ─────────────────────────────────────────────────────────
log "checking prereqs"
command -v llama-server >/dev/null 2>&1 || fail "llama-server not on PATH (brew install llama.cpp)"
command -v cargo >/dev/null 2>&1        || fail "cargo not on PATH"
command -v curl >/dev/null 2>&1         || fail "curl not on PATH"
[[ -f "$MODEL_PATH" ]]                  || fail "model not found: $MODEL_PATH"
[[ -d "$RUNNER_DIR/crates/runner-agent" ]] || fail "runner-agent not found at $RUNNER_DIR/crates/runner-agent (set COWBOT_RUNNER_DIR)"
[[ -d "$KNOWLEDGE_DIR" ]]               || fail "knowledge dir not found: $KNOWLEDGE_DIR"
ok "all prereqs present"

# ── Step 1: kill any stale llama-server on the same port ────────────────────
log "clearing port $LLAMA_PORT"
if lsof -tiTCP:$LLAMA_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:$LLAMA_PORT -sTCP:LISTEN || true)
    warn "port $LLAMA_PORT in use by pid(s): $pids — killing"
    kill $pids 2>/dev/null || true
    sleep 1
fi
ok "port $LLAMA_PORT free"

# ── Step 2: start llama-server in background ────────────────────────────────
log "starting llama-server (loading $MODEL_PATH)"
echo "  log: $LOG_FILE"

# Match start-llama-server.sh flags exactly. --jinja enables the model's
# real chat template (required for tool-call round-trips); --reasoning-format
# none suppresses Qwen3 thinking traces in the response.
nohup llama-server \
    --model "$MODEL_PATH" \
    --jinja \
    --reasoning-format none \
    --host "$LLAMA_HOST" \
    --port "$LLAMA_PORT" \
    -c "${LLAMA_CTX:-32768}" \
    -ngl "${LLAMA_NGL:-99}" \
    --threads "${LLAMA_THREADS:--1}" \
    --metrics \
    >"$LOG_FILE" 2>&1 &
LLAMA_PID=$!

# Wait for /v1/models to respond (model loaded). Up to ~120s on cold start
# for a 16GB GGUF on Apple silicon.
log "waiting for llama-server to load model (up to 120s)"
for i in $(seq 1 60); do
    if ! kill -0 "$LLAMA_PID" 2>/dev/null; then
        echo
        warn "llama-server died; tail of $LOG_FILE:"
        tail -30 "$LOG_FILE" | sed 's/^/    /'
        fail "llama-server exited before becoming ready"
    fi
    if curl -sf "http://${LLAMA_HOST}:${LLAMA_PORT}/v1/models" >/dev/null 2>&1; then
        ok "llama-server ready (pid=$LLAMA_PID, ${i}×2s)"
        break
    fi
    sleep 2
done
curl -sf "http://${LLAMA_HOST}:${LLAMA_PORT}/v1/models" >/dev/null 2>&1 \
    || fail "llama-server didn't become ready in 120s; see $LOG_FILE"

# ── Step 3: build runner-agent example ──────────────────────────────────────
log "building runner-agent cowbot_smoke example"
( cd "$RUNNER_DIR" && cargo build -p runner-agent --example cowbot_smoke 2>&1 | tail -5 )
ok "build complete"

# ── Step 4: run cowbot_smoke against local llama-server ─────────────────────
log "invoking cowbot_smoke"
echo "  query           : $QUERY"
echo "  knowledge dir   : $KNOWLEDGE_DIR"
echo "  llm endpoint    : http://${LLAMA_HOST}:${LLAMA_PORT}/v1"
echo "  model           : local (Qwen3-Coder-30B-A3B)"
echo

# Set up the env-override path so route_for_model("local") points at our
# llama-server. SELF_HOSTED_LLM_KEY is read for the Authorization header;
# llama-server doesn't actually check it but the agent code requires the
# var to be non-empty to construct a client.
export OPENAI_API_BASE="http://${LLAMA_HOST}:${LLAMA_PORT}/v1"
export AGENT_LOCAL_MODEL_ID="qwen3-coder-30b-a3b"
export SELF_HOSTED_LLM_KEY="local"
export RUST_LOG="${RUST_LOG:-info,runner_agent=debug}"

cd "$RUNNER_DIR"
cargo run -q -p runner-agent --example cowbot_smoke -- \
    "$QUERY" \
    "$KNOWLEDGE_DIR" \
    "local" \
    || fail "cowbot_smoke exited non-zero"

echo
ok "smoke complete"
