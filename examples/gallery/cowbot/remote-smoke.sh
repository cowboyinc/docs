#!/usr/bin/env bash
# remote-smoke.sh — same as local-smoke.sh but runs llama.cpp on shasta
# (48GB M5 Max) and tunnels port 8080 back over SSH. For when the dev box
# doesn't have enough VRAM to host the 30B model itself.
#
# Usage:
#   ./remote-smoke.sh
#   ./remote-smoke.sh "your question"
#   COWBOT_REMOTE_HOST=shasta.local ./remote-smoke.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="${COWBOT_RUNNER_DIR:-${HOME}/dev/cowboy/runner}"
REMOTE_HOST="${COWBOT_REMOTE_HOST:-shasta.local}"
REMOTE_LLAMA_BIN="${COWBOT_REMOTE_LLAMA_BIN:-\$HOME/llama.cpp/llama-b9075/llama-server}"
REMOTE_MODEL_PATH="${COWBOT_REMOTE_GGUF:-\$HOME/models/qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_HOST="127.0.0.1"
KNOWLEDGE_DIR="${COWBOT_KNOWLEDGE_DIR:-${RUNNER_DIR}/crates/runner-agent/tests/fixtures/knowledge}"
QUERY="${1:-What CIPs are described in this knowledge dir? Cite file paths.}"
REMOTE_LOG="/tmp/cowbot-llama.log"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
log()  { echo; echo -e "${CYAN}▸ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✅ $*${NC}"; }
warn() { echo -e "  ${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "  ${RED}❌ $*${NC}" >&2; exit 1; }

TUNNEL_PID=""
cleanup() {
    if [[ "${COWBOT_KEEP_LLAMA:-0}" != "1" ]]; then
        echo
        log "stopping remote llama-server"
        ssh "$REMOTE_HOST" "pkill -f 'llama-server.*--port $LLAMA_PORT' 2>/dev/null || true"
    else
        warn "leaving remote llama-server running on $REMOTE_HOST"
    fi
    if [[ -n "$TUNNEL_PID" ]]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── Step 0: prereqs ─────────────────────────────────────────────────────────
log "checking prereqs"
command -v cargo >/dev/null 2>&1 || fail "cargo not on PATH"
command -v curl >/dev/null 2>&1  || fail "curl not on PATH"
command -v ssh >/dev/null 2>&1   || fail "ssh not on PATH"
[[ -d "$RUNNER_DIR/crates/runner-agent" ]] || fail "runner-agent not found at $RUNNER_DIR/crates/runner-agent"
[[ -d "$KNOWLEDGE_DIR" ]] || fail "knowledge dir not found: $KNOWLEDGE_DIR"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" true 2>/dev/null \
    || fail "cannot ssh to $REMOTE_HOST (set up keys first)"
ssh "$REMOTE_HOST" "test -x $REMOTE_LLAMA_BIN" \
    || fail "$REMOTE_LLAMA_BIN missing or not executable on $REMOTE_HOST"
ssh "$REMOTE_HOST" "test -f $REMOTE_MODEL_PATH" \
    || fail "model missing on $REMOTE_HOST: $REMOTE_MODEL_PATH"
ok "all prereqs present"

# ── Step 1: clear local + remote port 8080 ──────────────────────────────────
log "clearing local port $LLAMA_PORT"
if lsof -tiTCP:$LLAMA_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:$LLAMA_PORT -sTCP:LISTEN || true)
    warn "local port $LLAMA_PORT in use by pid(s): $pids — killing"
    kill $pids 2>/dev/null || true
    sleep 1
fi
ok "local port $LLAMA_PORT free"

log "stopping any existing llama-server on $REMOTE_HOST"
ssh "$REMOTE_HOST" "pkill -f 'llama-server.*--port $LLAMA_PORT' 2>/dev/null || true; sleep 1"
ok "remote port $LLAMA_PORT clear"

# ── Step 2: start llama-server on remote ────────────────────────────────────
log "starting llama-server on $REMOTE_HOST (model: $REMOTE_MODEL_PATH)"
echo "  remote log: $REMOTE_HOST:$REMOTE_LOG"

ssh "$REMOTE_HOST" "nohup $REMOTE_LLAMA_BIN \
    --model $REMOTE_MODEL_PATH \
    --jinja \
    --reasoning-format none \
    --host 127.0.0.1 \
    --port $LLAMA_PORT \
    -c ${LLAMA_CTX:-32768} \
    -ngl ${LLAMA_NGL:-99} \
    --metrics \
    >$REMOTE_LOG 2>&1 </dev/null &"
sleep 1

# ── Step 3: open ssh tunnel localhost:8080 → shasta:8080 ────────────────────
log "opening SSH tunnel ${LLAMA_HOST}:${LLAMA_PORT} → ${REMOTE_HOST}:${LLAMA_PORT}"
ssh -fNL "${LLAMA_PORT}:127.0.0.1:${LLAMA_PORT}" "$REMOTE_HOST"
TUNNEL_PID=$(lsof -tiTCP:$LLAMA_PORT -sTCP:LISTEN | head -1 || true)
ok "tunnel up (local pid=$TUNNEL_PID)"

# ── Step 4: wait for /v1/models on remote ───────────────────────────────────
log "waiting for remote llama-server to load model (up to 180s)"
for i in $(seq 1 90); do
    if curl -sf "http://${LLAMA_HOST}:${LLAMA_PORT}/v1/models" >/dev/null 2>&1; then
        ok "llama-server ready (${i}×2s)"
        break
    fi
    sleep 2
done
curl -sf "http://${LLAMA_HOST}:${LLAMA_PORT}/v1/models" >/dev/null 2>&1 \
    || { ssh "$REMOTE_HOST" "tail -40 $REMOTE_LOG" | sed 's/^/    /'; fail "llama-server didn't become ready in 180s"; }

# ── Step 5: build runner-agent example ──────────────────────────────────────
log "building runner-agent cowbot_smoke example"
( cd "$RUNNER_DIR" && cargo build -p runner-agent --example cowbot_smoke 2>&1 | tail -5 )
ok "build complete"

# ── Step 6: run cowbot_smoke ────────────────────────────────────────────────
log "invoking cowbot_smoke"
echo "  query           : $QUERY"
echo "  knowledge dir   : $KNOWLEDGE_DIR"
echo "  llm endpoint    : http://${LLAMA_HOST}:${LLAMA_PORT}/v1 (tunneled to $REMOTE_HOST)"
echo "  model           : local (Qwen3-Coder-30B-A3B on $REMOTE_HOST)"
echo

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
