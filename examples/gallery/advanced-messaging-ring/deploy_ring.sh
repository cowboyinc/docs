#!/usr/bin/env bash
#
# deploy_ring.sh — Deploy, configure, and test an N-actor message ring.
#
# Usage:
#   ./deploy_ring.sh [RPC_URL] [NUM_ACTORS]
#
# Defaults:
#   RPC_URL     = https://rpc.mesa.cowboylabs.net
#   NUM_ACTORS  = 5

set -euo pipefail

MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
RPC_URL="${1:-${RPC_URL:-$MESA_RPC_URL}}"
NUM_ACTORS="${2:-5}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
NODE_ROOT="$WORKSPACE_ROOT/node"
source "$EXAMPLES_ROOT/_demo_output.sh"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"
ACTOR_TARGET="ring"
SALT_PREFIX="ring-$(date +%s)"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
LOCAL_DEV_KEY="$REPO_ROOT/.cowboy/keys/dev"

# Gas limits — actor deploys need more than the default
CYCLES=2000000
CELLS=2000000

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }
show_manual() { demo_print_command 1 "$*"; }
step_header() { echo; echo "── Step $1: $2 ──"; echo; }

find_cowboy() {
    if [[ -n "${COWBOY_BIN:-}" && -x "$COWBOY_BIN" ]]; then
        echo "$COWBOY_BIN"
        return
    fi
    if [[ -n "${COWBOY:-}" ]] && command -v "$COWBOY" >/dev/null 2>&1; then
        echo "$COWBOY"
        return
    fi
    if command -v cowboy >/dev/null 2>&1; then
        echo "cowboy"
        return
    fi
    if [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then
        echo "$NODE_ROOT/target/release/cowboy"
        return
    fi
    fail "cowboy CLI not found. Build it with: cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release"
    exit 1
}

COWBOY_BIN="$(find_cowboy)"

echo "============================================"
echo "  Ring Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys a ring of actors, connects each node to the next,"
echo "starts the message loop, and then checks how far the message propagated."
echo ""
echo "RPC:       $RPC_URL"
echo "Ring size: $NUM_ACTORS"
echo ""

# ── Preflight ────────────────────────────────────────────────────────

step_header "1" "Checking local environment"
info "Actor target: $ACTOR_TARGET"
info "RPC URL:     $RPC_URL"
info "Ring size:   $NUM_ACTORS actors"
info "Salt prefix: $SALT_PREFIX"
echo

# Check node is reachable
if ! curl -sf --max-time 5 "$RPC_URL/health" > /dev/null 2>&1; then
    fail "Node at $RPC_URL is not reachable"
    exit 1
fi
ok "Node is healthy"

# Use the funded spawn-local genesis key when the wrapper provides it.
if [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -n "${COWBOY_SPAWN_LOCAL_KEY:-}" && -f "$COWBOY_SPAWN_LOCAL_KEY" ]]; then
    KEY_FILE="$COWBOY_SPAWN_LOCAL_KEY"
elif [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -f "$LOCAL_DEV_KEY" ]]; then
    KEY_FILE="$LOCAL_DEV_KEY"
else
    info "Initializing dev wallet..."
    if demo_capture_command 1 \
        "$COWBOY_BIN init dev --rpc-url $RPC_URL" \
        "$COWBOY_BIN" init dev --rpc-url "$RPC_URL"; then
        :
    else
        :
    fi
    KEY_FILE="$(pwd)/.cowboy/keys/dev"
    if [[ ! -f "$KEY_FILE" ]]; then
        fail "Expected dev key at $KEY_FILE"
        exit 1
    fi
fi

WALLET=$("$COWBOY_BIN" wallet address --key "$KEY_FILE" | awk '{print $NF}' | sed 's/^0x//')
info "Wallet: 0x$WALLET"
curl -fsS --max-time 5 --connect-timeout 2 -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"0x$WALLET\"}" >/dev/null 2>&1 || true
sleep 2
echo

# ── Deploy ───────────────────────────────────────────────────────────

declare -a ACTORS

step_header "2" "Deploying ring actors"
info "Deploying $NUM_ACTORS actors..."
show_manual "$COWBOY_BIN actor deploy $ACTOR_TARGET --salt <hex-salt> --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS"
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    SALT_HEX=$(echo -n "${SALT_PREFIX}-${i}" | xxd -p | tr -d '\n')

    if demo_capture_command 1 \
        "$COWBOY_BIN actor deploy $ACTOR_TARGET --salt $SALT_HEX --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
        "$COWBOY_BIN" actor deploy "$ACTOR_TARGET" \
            --salt "$SALT_HEX" \
            --private-key "$KEY_FILE" \
            --cycles-limit "$CYCLES" \
            --cells-limit "$CELLS"; then
        :
    else
        :
    fi
    OUTPUT="$DEMO_LAST_OUTPUT"

    ADDR=$(echo "$OUTPUT" | grep "Actor address:" | head -1 | awk '{print $NF}' | sed 's/^0x//')
    if [ -z "$ADDR" ]; then
        fail "Deploy failed for actor $i"
        echo "$OUTPUT"
        exit 1
    fi

    ACTORS+=("$ADDR")
    ok "Actor $i: 0x$ADDR"
done
echo

# Wait for deploys to confirm
info "Waiting for deploys to confirm..."
sleep 8

CONFIRMED=0
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    RESULT=$("$COWBOY_BIN" actor get --address "${ACTORS[$i]}" 2>&1)
    if echo "$RESULT" | grep -q "code_hash"; then
        CONFIRMED=$((CONFIRMED + 1))
    else
        warn "Actor $i (${ACTORS[$i]}) not confirmed yet"
    fi
done

if [ "$CONFIRMED" -lt "$NUM_ACTORS" ]; then
    info "Waiting a bit longer..."
    sleep 10
    CONFIRMED=0
    for i in $(seq 0 $((NUM_ACTORS - 1))); do
        RESULT=$("$COWBOY_BIN" actor get --address "${ACTORS[$i]}" 2>&1)
        if echo "$RESULT" | grep -q "code_hash"; then
            CONFIRMED=$((CONFIRMED + 1))
        fi
    done
fi

ok "$CONFIRMED / $NUM_ACTORS actors confirmed on-chain"
if [ "$CONFIRMED" -lt "$NUM_ACTORS" ]; then
    fail "Not all actors deployed. Aborting."
    exit 1
fi
echo

# ── Configure ring ───────────────────────────────────────────────────

step_header "3" "Configuring ring topology"
info "Configuring ring topology..."
show_manual "$COWBOY_BIN actor execute --actor <actor-address> --handler configure --payload-json <json-payload> --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS"
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    NEXT=$(( (i + 1) % NUM_ACTORS ))
    PAYLOAD_JSON="{\"next_actor\":\"${ACTORS[$NEXT]}\"}"

    if demo_capture_command 1 \
        "$COWBOY_BIN actor execute --actor ${ACTORS[$i]} --handler configure --payload-json $PAYLOAD_JSON --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
        "$COWBOY_BIN" actor execute \
            --actor "${ACTORS[$i]}" \
            --handler configure \
            --payload-json "$PAYLOAD_JSON" \
            --private-key "$KEY_FILE" \
            --cycles-limit "$CYCLES" \
            --cells-limit "$CELLS"; then
        :
    else
        :
    fi
    OUTPUT="$DEMO_LAST_OUTPUT"

    if echo "$OUTPUT" | grep -q '"Success"'; then
        ok "Actor $i → Actor $NEXT"
    else
        # Might need a moment between txs
        sleep 3
        if demo_capture_command 1 \
            "$COWBOY_BIN actor execute --actor ${ACTORS[$i]} --handler configure --payload-json $PAYLOAD_JSON --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
            "$COWBOY_BIN" actor execute \
                --actor "${ACTORS[$i]}" \
                --handler configure \
                --payload-json "$PAYLOAD_JSON" \
                --private-key "$KEY_FILE" \
                --cycles-limit "$CYCLES" \
                --cells-limit "$CELLS"; then
            :
        else
            :
        fi
        OUTPUT="$DEMO_LAST_OUTPUT"
        if echo "$OUTPUT" | grep -q '"Success"'; then
            ok "Actor $i → Actor $NEXT (retry)"
        else
            fail "Configure failed for actor $i"
            echo "$OUTPUT"
            exit 1
        fi
    fi
done
echo

step_header "4" "Funding actors for deferred execution"
info "Funding actors for deferred message execution..."
show_manual "$COWBOY_BIN fund-actor --rpc-url $RPC_URL --private-key $KEY_FILE --actor <actor-address> --amount 20"
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    "$COWBOY_BIN" fund-actor --rpc-url "$RPC_URL" --private-key "$KEY_FILE" --actor "0x${ACTORS[$i]}" --amount 20 >/dev/null
    ok "Funded actor $i"
done
sleep 5
echo

# ── Start the ring ───────────────────────────────────────────────────

MSG="hello ring from $SALT_PREFIX"
PAYLOAD_JSON="{\"msg\":\"$MSG\"}"

step_header "5" "Starting the ring"
info "Starting ring on actor 0..."
show_manual "$COWBOY_BIN actor execute --actor ${ACTORS[0]} --handler start_ring --payload-json $PAYLOAD_JSON --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS"
if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor ${ACTORS[0]} --handler start_ring --payload-json $PAYLOAD_JSON --private-key $KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
        --actor "${ACTORS[0]}" \
        --handler start_ring \
        --payload-json "$PAYLOAD_JSON" \
        --private-key "$KEY_FILE" \
        --cycles-limit "$CYCLES" \
        --cells-limit "$CELLS"; then
    :
else
    :
fi
START_OUTPUT="$DEMO_LAST_OUTPUT"

if echo "$START_OUTPUT" | grep -q '"Success"'; then
    ok "Ring started"
else
    fail "start_ring failed"
    echo "$START_OUTPUT"
    exit 1
fi
echo

# ── Observe propagation ─────────────────────────────────────────────

step_header "6" "Observing propagation"
info "Waiting for message to propagate around the ring..."
sleep 15

HOPS_SEEN=0
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    LOGS=$("$COWBOY_BIN" actor logs --rpc-url "$RPC_URL" --address "${ACTORS[$i]}" 2>&1)
    HOP_COUNT=$(echo "$LOGS" | grep -c "ring_hop\|got_message\|forwarding" || true)
    if [ "$HOP_COUNT" -gt 0 ]; then
        ok "Actor $i: $HOP_COUNT hop events"
        HOPS_SEEN=$((HOPS_SEEN + 1))
    else
        warn "Actor $i: no hop events yet"
    fi
done
echo

# ── Summary ──────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${CYAN}Ring Demo Summary${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ring size:      $NUM_ACTORS actors"
echo "  Actors reached: $HOPS_SEEN / $NUM_ACTORS"
echo "  Message:        \"$MSG\""
echo

if [ "$HOPS_SEEN" -ge "$NUM_ACTORS" ]; then
    ok "Message reached all actors in the ring"
elif [ "$HOPS_SEEN" -gt 0 ]; then
    warn "Message reached $HOPS_SEEN actors (may still be propagating — check logs)"
else
    fail "No hops observed. The ring did not propagate."
fi

echo
echo "Actor addresses:"
for i in $(seq 0 $((NUM_ACTORS - 1))); do
    echo "  [$i] 0x${ACTORS[$i]}"
done
echo
echo "Check logs:"
echo "  $COWBOY_BIN actor logs --address ${ACTORS[0]}"
