#!/usr/bin/env bash
# devnet-shasta.sh — boot llama-server + runner-node on shasta
# (48GB M5 Max). Runs from jefferson, SSHes into shasta to start
# both services. Runner points at jefferson's RPC over Tailscale.
#
# The runner self-registers using the seed-10 funded genesis key.
#
# Logs (on shasta):
#   /tmp/cowbot-llama.log
#   /tmp/cowbot-runner.log
#
# Stop with: ./devnet-shasta.sh stop

set -euo pipefail

JEFFERSON_TAILNET_IP="${JEFFERSON_TAILNET_IP:?set to the tailnet IP of the host running the validator}"
RPC_PORT="${RPC_PORT:-4002}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
SHASTA_HOST="${SHASTA_HOST:-shasta.local}"
MODEL_PATH='$HOME/models/qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf'
LLAMA_BIN='$HOME/llama.cpp/llama-b9075/llama-server'
RUNNER_BIN='$HOME/dev/cowboy/runner/target/release/runner-node'
RUNNER_DATA_DIR='$HOME/dev/cowboy/runner-data'

# Funded genesis seed-10 key (hex, no 0x prefix). NOT a secret — devnet only.
RUNNER_KEY_HEX="0a00000000000000000000000000000000000000000000000000000000000000"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}▸ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✅ $*${NC}"; }
fail() { echo -e "  ${RED}❌ $*${NC}" >&2; exit 1; }

if [[ "${1:-}" == "stop" ]]; then
    log "stopping shasta llama + runner"
    ssh "$SHASTA_HOST" "pkill -f 'llama-server.*--port $LLAMA_PORT' 2>/dev/null; pkill -f 'target/release/runner-node' 2>/dev/null; true"
    sleep 1
    ok "stopped"
    exit 0
fi

# ── prereqs on jefferson ────────────────────────────────────────────────────
# No Slack secrets here: the runner obtains SLACK_BOT_TOKEN via CBSS threshold
# release at job time (secrets=[...], CIP-24 §5.2), provisioned once by
# deploy.sh Step 4c — never pre-shared into the runner host's environment.

# ── verify jefferson RPC reachable from shasta ──────────────────────────────
log "verifying shasta can reach jefferson RPC over tailnet"
ssh "$SHASTA_HOST" "curl -sf http://$JEFFERSON_TAILNET_IP:$RPC_PORT/height 2>&1 | head -1" \
    | grep -q '"height"' \
    || fail "shasta can't reach http://$JEFFERSON_TAILNET_IP:$RPC_PORT/height — is jefferson devnet running?"
ok "shasta can reach chain RPC"

# ── stop any prior instances on shasta ──────────────────────────────────────
log "stopping any prior shasta processes"
ssh "$SHASTA_HOST" "pkill -f 'llama-server.*--port $LLAMA_PORT' 2>/dev/null; pkill -f 'target/release/runner-node' 2>/dev/null; sleep 1; true"
ok "shasta clean"

# ── start llama-server on shasta ────────────────────────────────────────────
log "starting llama-server on shasta"
ssh "$SHASTA_HOST" "test -x $LLAMA_BIN && test -f $MODEL_PATH" \
    || fail "llama-server bin or model missing on shasta"

ssh "$SHASTA_HOST" "nohup $LLAMA_BIN \
    --model $MODEL_PATH \
    --jinja \
    --reasoning-format none \
    --host 127.0.0.1 \
    --port $LLAMA_PORT \
    -c 32768 \
    -ngl 99 \
    --metrics \
    >/tmp/cowbot-llama.log 2>&1 </dev/null &"
sleep 1

log "waiting for llama-server (up to 180s)"
for i in $(seq 1 90); do
    if ssh "$SHASTA_HOST" "curl -sf http://127.0.0.1:$LLAMA_PORT/v1/models >/dev/null 2>&1"; then
        ok "llama-server ready (${i}×2s)"
        break
    fi
    sleep 2
done
ssh "$SHASTA_HOST" "curl -sf http://127.0.0.1:$LLAMA_PORT/v1/models >/dev/null 2>&1" \
    || fail "llama-server didn't come up; ssh shasta.local 'tail -30 /tmp/cowbot-llama.log'"

# ── seed runner key + start runner-node on shasta ───────────────────────────
log "seeding runner key + starting runner-node on shasta"
ssh "$SHASTA_HOST" "mkdir -p $RUNNER_DATA_DIR && \
    echo '{\"private_key\": \"$RUNNER_KEY_HEX\"}' > $RUNNER_DATA_DIR/runner_key.json"

# Pass env via the SSH command. Note: bash heredoc + ssh = quoting hell;
# use single-quoted body and let the remote shell expand $HOME etc.
ssh "$SHASTA_HOST" "export PATH=/opt/homebrew/bin:\$PATH && \
    nohup env \
    CHAIN_RPC_URL=http://$JEFFERSON_TAILNET_IP:$RPC_PORT \
    RUNNER_DATA_DIR=$RUNNER_DATA_DIR \
    OPENAI_API_BASE=http://127.0.0.1:$LLAMA_PORT/v1 \
    OPENAI_API_KEY=local \
    SELF_HOSTED_LLM_KEY=local \
    AGENT_LOCAL_MODEL_ID=qwen3-coder-30b-a3b \
    LLM_MODEL=qwen3-coder-30b-a3b \
    RUST_LOG=info,runner_node=info,runner_agent=info \
    $RUNNER_BIN >/tmp/cowbot-runner.log 2>&1 </dev/null &"

# ── wait for runner registration / chain link ──────────────────────────────
log "waiting for runner-node to start + register"
for i in $(seq 1 30); do
    if ssh "$SHASTA_HOST" "grep -q 'X25519 Encryption Key:' /tmp/cowbot-runner.log 2>/dev/null"; then
        X25519=$(ssh "$SHASTA_HOST" "grep 'X25519 Encryption Key:' /tmp/cowbot-runner.log | head -1 | awk '{print \$NF}'")
        ok "runner-node started, X25519 pubkey = ${X25519:0:16}..."
        echo "$X25519" > /tmp/cowbot-runner-x25519.txt
        break
    fi
    sleep 1
done
[[ -s /tmp/cowbot-runner-x25519.txt ]] || fail "runner didn't emit X25519 key in log; ssh shasta.local 'tail -30 /tmp/cowbot-runner.log'"

# ── show summary ────────────────────────────────────────────────────────────
echo
echo "shasta llama-server : http://127.0.0.1:$LLAMA_PORT/v1 (only on shasta)"
echo "shasta runner       : pointed at http://$JEFFERSON_TAILNET_IP:$RPC_PORT"
echo "runner X25519       : $(cat /tmp/cowbot-runner-x25519.txt)"
echo "logs (on shasta)    : /tmp/cowbot-llama.log, /tmp/cowbot-runner.log"
echo
echo "next: ./register-runner.sh    (idempotent, passes the X25519 to chain)"
echo "      ./deploy.sh"
