#!/usr/bin/env bash
#
# Rebalancing Agent Demo
# ======================
#
# This script deploys a deterministic portfolio rebalancing actor,
# initializes its pool configuration, submits a rebalance check, and
# queries the final recommendation and stats.
#
# Prerequisites:
#   - cowboy CLI built: cargo build -p cowboy-cli --manifest-path <node>/Cargo.toml --release
#   - A running validator (mesa devnet or local)
#
# Usage:
#   ./demo.sh [RPC_URL]
#
# Default RPC: https://rpc.mesa.cowboylabs.net

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
source "$EXAMPLES_ROOT/_demo_output.sh"
COWBOY_BIN="${COWBOY_BIN:-${COWBOY:-cowboy}}"
LOCAL_DEV_KEY="$REPO_ROOT/.cowboy/keys/dev"
MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
RPC_URL="${1:-${RPC_URL:-$MESA_RPC_URL}}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
USING_SHARED_LOCAL_KEY=0

ACTOR_TARGET="rebalancer"
SALT_HEX="$(echo -n "rebalancing-$(date +%s)" | xxd -p | tr -d '\n')"
INIT_JSON='{"pool_address":"demo-pool","drift_threshold_bps":500}'
RUN_JSON='{"current_ratio":0.91,"average_ratio":1.0,"note":"demo rebalance request"}'
STATUS_JSON='{}'

CYCLES=4000000
CELLS=4000000
DEPLOY_CYCLES=10000000
DEPLOY_CELLS=10000000

show_manual() {
    demo_print_command 1 "$*"
}

hex_payload() {
    printf "%s" "$1" | xxd -p -c 9999
}

wait_for_account() {
    local addr="$1"
    local i
    for i in $(seq 1 45); do
        if curl -fsS --max-time 2 "$RPC_URL/account/$addr" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "  WARNING: account $addr did not become queryable after faucet funding"
    return 1
}

wait_for_actor() {
    local addr="$1"
    local i
    for i in $(seq 1 30); do
        if $COWBOY_BIN actor get --address "$addr" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[ok]${NC}    $*"; }
info() { echo -e "  ${CYAN}[info]${NC}  $*"; }
warn() { echo -e "  ${YELLOW}[warn]${NC}  $*"; }
fail() { echo -e "  ${RED}[FAIL]${NC}  $*"; }

if ! command -v "$COWBOY_BIN" >/dev/null 2>&1; then
    NODE_DIR="$WORKSPACE_ROOT/node"
    if [[ -x "${NODE_DIR}/target/release/cowboy" ]]; then
        COWBOY_BIN="${NODE_DIR}/target/release/cowboy"
    else
        echo "Error: 'cowboy' CLI not found. Build it with:"
        echo "  cargo build -p cowboy-cli --manifest-path <node>/Cargo.toml --release"
        exit 1
    fi
fi

echo "============================================"
echo "  Rebalancing Agent Demo"
echo "============================================"
echo ""
echo "RPC:      $RPC_URL"
echo "Work dir: $WORK_DIR"
echo ""

echo "── Step 1: Selecting wallet ──"
echo ""

if [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -f "$LOCAL_DEV_KEY" ]]; then
    cp "$LOCAL_DEV_KEY" "$WORK_DIR/deployer-key"
    USING_SHARED_LOCAL_KEY=1
else
    $COWBOY_BIN wallet create --output "$WORK_DIR/deployer-key" 2>/dev/null
fi

DEPLOYER="$($COWBOY_BIN wallet address --key "$WORK_DIR/deployer-key" 2>/dev/null)"
info "Deployer: $DEPLOYER"
echo ""

echo "── Step 2: Funding wallet via faucet ──"
echo ""

if [[ "$USING_SHARED_LOCAL_KEY" -eq 1 ]]; then
    info "Using shared localhost dev key; funding it via faucet in case it is not pre-funded"
fi
curl --max-time 5 --connect-timeout 2 -s -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\": \"$DEPLOYER\"}" >/dev/null || true
wait_for_account "$DEPLOYER" || true
ok "Wallet funded or already available"
echo ""

echo "── Step 3: Deploying the rebalancer with the portfolio config ──"
echo ""

# The rebalancer target is project-relative: the CLI discovers it from the
# nearest cowboy.toml, so the deploy runs from the example directory. Deploy
# runs init atomically and the SDK refuses a second init, so the portfolio
# config rides on the deploy via --init-payload rather than a later call.
cd "$SCRIPT_DIR"
if demo_capture_command 1 \
    "$COWBOY_BIN actor deploy $ACTOR_TARGET --salt $SALT_HEX --private-key $WORK_DIR/deployer-key --init-payload $INIT_JSON --cycles-limit $DEPLOY_CYCLES --cells-limit $DEPLOY_CELLS" \
    "$COWBOY_BIN" actor deploy "$ACTOR_TARGET" \
      --salt "$SALT_HEX" \
      --private-key "$WORK_DIR/deployer-key" \
      --init-payload "$INIT_JSON" \
      --cycles-limit "$DEPLOY_CYCLES" \
      --cells-limit "$DEPLOY_CELLS"; then
    :
else
    :
fi
DEPLOY_OUTPUT="$DEMO_LAST_OUTPUT"

ACTOR_ADDR="$(echo "$DEPLOY_OUTPUT" | awk '/Actor address:/ {print $NF}' | tail -1 | sed 's/^0x//')"
if [[ -z "$ACTOR_ADDR" ]]; then
    fail "Could not extract actor address"
    exit 1
fi

if wait_for_actor "$ACTOR_ADDR"; then
    ok "Actor deployed: 0x$ACTOR_ADDR"
else
    fail "Actor did not become queryable"
    exit 1
fi
echo ""

echo "── Step 4: Checking rebalance recommendation ──"
echo ""

if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $ACTOR_ADDR --handler check_rebalance --payload-json $RUN_JSON --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$ACTOR_ADDR" \
      --handler check_rebalance \
      --payload-json "$RUN_JSON" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
RUN_OUTPUT="$DEMO_LAST_OUTPUT"

if echo "$RUN_OUTPUT" | grep -q 'Status: String("Success")'; then
    ok "Rebalance check completed"
else
    fail "check_rebalance failed"
    exit 1
fi
echo ""

echo "── Step 5: Reading latest decision ──"
echo ""

if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $ACTOR_ADDR --handler get_last_decision --payload-json $STATUS_JSON --private-key $WORK_DIR/deployer-key --cycles-limit 1500000 --cells-limit 1500000" \
    "$COWBOY_BIN" actor execute \
      --actor "$ACTOR_ADDR" \
      --handler get_last_decision \
      --payload-json "$STATUS_JSON" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit 1500000 \
      --cells-limit 1500000; then
    :
else
    :
fi
STATUS_OUTPUT="$DEMO_LAST_OUTPUT"
echo ""

echo "── Step 6: Reading actor stats ──"
echo ""

if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $ACTOR_ADDR --handler get_stats --payload-json $STATUS_JSON --private-key $WORK_DIR/deployer-key --cycles-limit 1500000 --cells-limit 1500000" \
    "$COWBOY_BIN" actor execute \
      --actor "$ACTOR_ADDR" \
      --handler get_stats \
      --payload-json "$STATUS_JSON" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit 1500000 \
      --cells-limit 1500000; then
    :
else
    :
fi
STATS_OUTPUT="$DEMO_LAST_OUTPUT"
echo ""

echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Actor:       0x$ACTOR_ADDR"
echo "  Pool:        demo-pool"
echo "  Drift limit: 500 bps"
