#!/usr/bin/env bash
#
# V2 Liquidity Pool Demo — Cowboy AMM
# ====================================
#
# This script deploys a Uniswap V2-style constant-product pool as a
# Cowboy actor, then runs through a full lifecycle:
#
#   1. Create wallets and fund them
#   2. Create WETH + USDC tokens
#   3. Deploy the V2 pool actor
#   4. Initialize the pool (creates LP token)
#   5. Add initial liquidity
#   6. Swap tokens
#   7. Remove liquidity
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

# ── Configuration ──────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
source "$EXAMPLES_ROOT/_demo_output.sh"
COWBOY_BIN="${COWBOY_BIN:-${COWBOY:-cowboy}}"
LOCAL_DEV_KEY="$REPO_ROOT/.cowboy/keys/dev"
MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"

# Find the cowboy binary
if ! command -v "$COWBOY_BIN" &>/dev/null; then
    NODE_DIR="$WORKSPACE_ROOT/node"
    if [[ -x "${NODE_DIR}/target/release/cowboy" ]]; then
        COWBOY_BIN="${NODE_DIR}/target/release/cowboy"
    else
        echo "Error: 'cowboy' CLI not found. Build it with:"
        echo "  cargo build -p cowboy-cli --manifest-path <node>/Cargo.toml --release"
        exit 1
    fi
fi

RPC_URL="${1:-${RPC_URL:-$MESA_RPC_URL}}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
USING_SHARED_LOCAL_KEY=0

show_manual() {
    demo_print_command 1 "$*"
}

run_cowboy_cli() {
    local command_text="$1"
    shift
    demo_retry_command 1 3 1 "$command_text" "$@"
}

# Read one field out of the pool's decoded storage. `actor state` hex-decodes the
# keys and CBOR-decodes the values, so this prints native JSON: reserve_a is a
# number, lp_token a string. The execute output can't be scraped instead — event
# payloads are canonical CBOR on the wire, not text.
pool_field() {
    local key="$1"
    "$COWBOY_BIN" --indexer-url "$RPC_URL" actor state --address "$POOL_ADDR" 2>/dev/null \
      | python3 -c '
import json, sys
try:
    state = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
value = state.get(sys.argv[1])
if value is None:
    raise SystemExit(0)
# Strings bare — an lp_token has to be usable as a CLI argument; everything else
# through json.dumps so a bool prints as true, not the Python True. No apostrophes
# in here: this block is inside a single-quoted python3 -c argument.
print(value if isinstance(value, str) else json.dumps(value))
' "$key" 2>/dev/null || true
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

# Gas limits for actor operations
CYCLES=2000000
CELLS=2000000

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[ok]${NC}    $*"; }
info() { echo -e "  ${CYAN}[info]${NC}  $*"; }
warn() { echo -e "  ${YELLOW}[warn]${NC}  $*"; }
fail() { echo -e "  ${RED}[FAIL]${NC}  $*"; }

echo "============================================"
echo "  V2 Liquidity Pool Demo"
echo "============================================"
echo ""
echo "RPC:      $RPC_URL"
echo "Work dir: $WORK_DIR"
echo ""

# ── Step 1: Create wallets ─────────────────────────────────────────────

echo "── Step 1: Creating wallets ──"
echo ""

if [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -n "${COWBOY_SPAWN_LOCAL_KEY:-}" && -f "$COWBOY_SPAWN_LOCAL_KEY" ]]; then
    cp "$COWBOY_SPAWN_LOCAL_KEY" "$WORK_DIR/deployer-key"
    USING_SHARED_LOCAL_KEY=1
elif [[ "${USE_SHARED_DEMO_KEY:-0}" == "1" && "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -f "$LOCAL_DEV_KEY" ]]; then
    cp "$LOCAL_DEV_KEY" "$WORK_DIR/deployer-key"
    USING_SHARED_LOCAL_KEY=1
else
    $COWBOY_BIN wallet create --output "$WORK_DIR/deployer-key" 2>/dev/null
fi
DEPLOYER=$($COWBOY_BIN wallet address --key "$WORK_DIR/deployer-key" 2>/dev/null)
info "Deployer: $DEPLOYER"

$COWBOY_BIN wallet create --output "$WORK_DIR/alice-key" 2>/dev/null
ALICE=$($COWBOY_BIN wallet address --key "$WORK_DIR/alice-key" 2>/dev/null)
info "Alice:    $ALICE"
echo ""

# Strip 0x prefix for payload use
DEPLOYER_HEX="${DEPLOYER#0x}"
ALICE_HEX="${ALICE#0x}"

# ── Step 2: Fund wallets via faucet ────────────────────────────────────

echo "── Step 2: Funding wallets via faucet ──"
echo ""

if [[ "$USING_SHARED_LOCAL_KEY" -eq 1 ]]; then
    info "Using shared localhost dev key for deployer; funding it via faucet in case it is not pre-funded"
fi

for ADDR in "$DEPLOYER" "$ALICE"; do
    for _ in $(seq 1 "${DEMO_FAUCET_TOPUPS:-10}"); do
        curl --max-time 5 --connect-timeout 2 -s -X POST "$RPC_URL/faucet" \
            -H 'Content-Type: application/json' \
            -d "{\"address\": \"$ADDR\"}" > /dev/null || true
        sleep 1
    done
    ok "Funded $ADDR or continuing with existing balance"
done
wait_for_account "$DEPLOYER" || true

if [[ "$USING_SHARED_LOCAL_KEY" -eq 1 ]]; then
    run_cowboy_cli \
        "$COWBOY_BIN transfer --rpc-url $RPC_URL --private-key $WORK_DIR/deployer-key --to $ALICE --amount 1000" \
        "$COWBOY_BIN" transfer --rpc-url "$RPC_URL" \
          --private-key "$WORK_DIR/deployer-key" \
          --to "$ALICE" \
          --amount 1000
    sleep 5
fi

echo ""

# ── Step 3: Create tokens ─────────────────────────────────────────────

echo "── Step 3: Creating CIP-20 tokens ──"
echo ""

# Create WETH token (18 decimals, 100 WETH initial supply)
info "Creating WETH (18 decimals, 100 WETH)..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL create --name \"Wrapped Ether\" --symbol WETH --decimals 18 --initial-supply 100000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      create --name "Wrapped Ether" --symbol WETH --decimals 18 \
      --initial-supply 100000000000000000000

sleep 5

# Create USDC token (6 decimals, 1M USDC initial supply)
info "Creating USDC (6 decimals, 1,000,000 USDC)..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL create --name \"USD Coin\" --symbol USDC --decimals 6 --initial-supply 1000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      create --name "USD Coin" --symbol USDC --decimals 6 \
      --initial-supply 1000000000000

sleep 5
echo ""

# ── Step 4: List tokens and get IDs ───────────────────────────────────

echo "── Step 4: Listing tokens ──"
echo ""
show_manual "$COWBOY_BIN token --rpc-url $RPC_URL list"

TOKEN_LIST=$($COWBOY_BIN token --rpc-url "$RPC_URL" list)
echo "$TOKEN_LIST" | python3 -c "
import json, sys
tokens = json.load(sys.stdin)
deployer = '${DEPLOYER}'.lower()
our_tokens = [t for t in tokens if str(t['owner']).lower() == deployer]
for t in our_tokens:
    decimals = t['decimals']
    supply = int(t['total_supply'])
    human_supply = supply / (10 ** decimals)
    print(f'  {t[\"symbol\"]:6s}  id={t[\"token_id\"][:16]}...  supply={human_supply:,.0f}  decimals={decimals}')
"

WETH_ID=$(echo "$TOKEN_LIST" | python3 -c "
import json, sys
tokens = json.load(sys.stdin)
deployer = '${DEPLOYER}'.lower()
for t in tokens:
    if str(t['owner']).lower() == deployer and t['symbol'] == 'WETH':
        print(t['token_id'])
        break
")

USDC_ID=$(echo "$TOKEN_LIST" | python3 -c "
import json, sys
tokens = json.load(sys.stdin)
deployer = '${DEPLOYER}'.lower()
for t in tokens:
    if str(t['owner']).lower() == deployer and t['symbol'] == 'USDC':
        print(t['token_id'])
        break
")

echo ""
info "WETH token_id: $WETH_ID"
info "USDC token_id: $USDC_ID"

if [[ -z "$WETH_ID" || -z "$USDC_ID" ]]; then
    fail "Could not find token IDs. Token creation may have failed."
    echo "$TOKEN_LIST" | python3 -m json.tool
    exit 1
fi
echo ""

# ── Step 5: Deploy pool actor ─────────────────────────────────────────

echo "── Step 5: Deploying V2 pool actor ──"
echo ""

for _ in $(seq 1 "${DEMO_FAUCET_TOPUPS:-10}"); do
    curl --max-time 5 --connect-timeout 2 -s -X POST "$RPC_URL/faucet" \
        -H 'Content-Type: application/json' \
        -d "{\"address\": \"$DEPLOYER\"}" > /dev/null || true
    sleep 1
done

SALT_HEX=$(echo -n "v2pool-$(date +%s)" | xxd -p | tr -d '\n')
# The pool target is project-relative: the CLI discovers it from the nearest
# cowboy.toml, so the deploy runs from the example directory.
cd "$SCRIPT_DIR"
if demo_capture_command 1 \
    "$COWBOY_BIN actor deploy pool --salt $SALT_HEX --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor deploy pool \
      --salt "$SALT_HEX" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
DEPLOY_OUTPUT="$DEMO_LAST_OUTPUT"

POOL_ADDR=$(echo "$DEPLOY_OUTPUT" | grep "Actor address:" | head -1 | awk '{print $NF}' | sed 's/^0x//')

if [[ -z "$POOL_ADDR" ]]; then
    fail "Deploy failed"
    echo "$DEPLOY_OUTPUT"
    exit 1
fi

ok "Pool actor deployed: 0x$POOL_ADDR"

sleep 8

# Confirm deployment
CONFIRM=$($COWBOY_BIN actor get --address "$POOL_ADDR" 2>&1)
if echo "$CONFIRM" | grep -q "code_hash"; then
    ok "Pool actor confirmed on-chain"
else
    warn "Pool not confirmed yet, waiting longer..."
    sleep 10
fi
echo ""

# ── Step 6: Initialize pool ───────────────────────────────────────────

echo "── Step 6: Initializing pool ──"
echo ""

INIT_PAYLOAD="{\"token_a\":\"$WETH_ID\",\"token_b\":\"$USDC_ID\",\"fee_bps\":30,\"pool_address\":\"$POOL_ADDR\"}"
if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $POOL_ADDR --handler init_pool --payload-json $INIT_PAYLOAD --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$POOL_ADDR" \
      --handler init_pool \
      --payload-json "$INIT_PAYLOAD" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
INIT_OUTPUT="$DEMO_LAST_OUTPUT"

if echo "$INIT_OUTPUT" | grep -q '"Success"'; then
    ok "Pool initialized (WETH/USDC, 30 bps fee)"
else
    fail "init_pool failed"
    echo "$INIT_OUTPUT"
    exit 1
fi

sleep 5

LP_TOKEN_ID=$(pool_field lp_token)

if [[ -n "$LP_TOKEN_ID" ]]; then
    info "LP token ID: $LP_TOKEN_ID"
else
    warn "Could not read lp_token from pool storage"
    info "Full init output:"
    echo "$INIT_OUTPUT"
fi

echo ""

# ── Step 7: Transfer tokens to Alice ──────────────────────────────────

echo "── Step 7: Transferring tokens to Alice ──"
echo ""

# Transfer 5 WETH to Alice (5 * 10^18)
info "Transferring 5 WETH to Alice..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL transfer --token-id $WETH_ID --to $ALICE --amount 5000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      transfer --token-id "$WETH_ID" --to "$ALICE" --amount 5000000000000000000

sleep 5

# Transfer 50,000 USDC to Alice (50000 * 10^6)
info "Transferring 50,000 USDC to Alice..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL transfer --token-id $USDC_ID --to $ALICE --amount 50000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      transfer --token-id "$USDC_ID" --to "$ALICE" --amount 50000000000

sleep 5
ok "Tokens transferred to Alice"
echo ""

# ── Step 8: Deployer adds initial liquidity ───────────────────────────

echo "── Step 8: Adding initial liquidity (Deployer) ──"
echo ""

# Deployer adds: 10 WETH + 25,000 USDC (implies $2,500/ETH)
WETH_LIQ=10000000000000000000    # 10 WETH (18 dec)
USDC_LIQ=25000000000             # 25,000 USDC (6 dec)

# Approve pool to spend deployer's tokens
info "Approving pool to spend WETH..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL approve --token-id $WETH_ID --spender 0x$POOL_ADDR --amount $WETH_LIQ" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      approve --token-id "$WETH_ID" --spender "0x$POOL_ADDR" --amount "$WETH_LIQ"

sleep 5

info "Approving pool to spend USDC..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL approve --token-id $USDC_ID --spender 0x$POOL_ADDR --amount $USDC_LIQ" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      approve --token-id "$USDC_ID" --spender "0x$POOL_ADDR" --amount "$USDC_LIQ"

sleep 5

# Add liquidity
ADD_LIQ_PAYLOAD="{\"sender\":\"$DEPLOYER_HEX\",\"amount_a\":$WETH_LIQ,\"amount_b\":$USDC_LIQ}"
info "Adding 10 WETH + 25,000 USDC as initial liquidity..."
if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $POOL_ADDR --handler add_liquidity --payload-json $ADD_LIQ_PAYLOAD --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$POOL_ADDR" \
      --handler add_liquidity \
      --payload-json "$ADD_LIQ_PAYLOAD" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
ADD_OUTPUT="$DEMO_LAST_OUTPUT"

if echo "$ADD_OUTPUT" | grep -q '"Success"'; then
    ok "Initial liquidity added"
else
    fail "add_liquidity failed"
    echo "$ADD_OUTPUT"
    exit 1
fi

sleep 5
echo ""

# ── Step 9: Check reserves ────────────────────────────────────────────

echo "── Step 9: Checking pool reserves ──"
echo ""

RESERVES_PAYLOAD='{}'
if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $POOL_ADDR --handler get_reserves --payload-json $RESERVES_PAYLOAD --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$POOL_ADDR" \
      --handler get_reserves \
      --payload-json "$RESERVES_PAYLOAD" \
      --private-key "$WORK_DIR/deployer-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
RESERVE_A_BEFORE="$(pool_field reserve_a)"
RESERVE_B_BEFORE="$(pool_field reserve_b)"

info "Pool reserves:"
echo "    reserve_a: ${RESERVE_A_BEFORE:-unknown}"
echo "    reserve_b: ${RESERVE_B_BEFORE:-unknown}"
echo ""

# ── Step 10: Alice swaps USDC for WETH ────────────────────────────────

echo "── Step 10: Alice swaps 1,000 USDC for WETH ──"
echo ""

USDC_SWAP=1000000000   # 1,000 USDC (6 dec)

# Alice approves pool to spend her USDC
info "Alice approving pool to spend USDC..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/alice-key --rpc-url $RPC_URL approve --token-id $USDC_ID --spender 0x$POOL_ADDR --amount $USDC_SWAP" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/alice-key" --rpc-url "$RPC_URL" \
      approve --token-id "$USDC_ID" --spender "0x$POOL_ADDR" --amount "$USDC_SWAP"

sleep 5

# Execute swap: Alice sends USDC, receives WETH
SWAP_PAYLOAD="{\"sender\":\"$ALICE_HEX\",\"token_in\":\"$USDC_ID\",\"amount_in\":$USDC_SWAP,\"min_amount_out\":0}"
info "Swapping 1,000 USDC..."
if demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $POOL_ADDR --handler swap --payload-json $SWAP_PAYLOAD --private-key $WORK_DIR/alice-key --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$POOL_ADDR" \
      --handler swap \
      --payload-json "$SWAP_PAYLOAD" \
      --private-key "$WORK_DIR/alice-key" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"; then
    :
else
    :
fi
SWAP_OUTPUT="$DEMO_LAST_OUTPUT"

if echo "$SWAP_OUTPUT" | grep -q '"Success"'; then
    ok "Swap executed"
    # Alice paid in token B, so the WETH she received is exactly how far
    # reserve_a fell.
    RESERVE_A_AFTER="$(pool_field reserve_a)"
    if [[ -n "$RESERVE_A_BEFORE" && -n "$RESERVE_A_AFTER" ]]; then
        # python3, not $(( )): an 18-decimal reserve overflows bash's signed 64-bit ints.
        echo "    WETH received: $(python3 -c \
            'import sys; print(int(sys.argv[1]) - int(sys.argv[2]))' \
            "$RESERVE_A_BEFORE" "$RESERVE_A_AFTER")"
    fi
else
    fail "swap failed"
    echo "$SWAP_OUTPUT"
    exit 1
fi

sleep 5
echo ""

# ── Step 11: Check balances after swap ────────────────────────────────

echo "── Step 11: Balances after swap ──"
echo ""

ALICE_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')
ALICE_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')
DEPLOYER_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
DEPLOYER_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')

echo "  Alice:"
echo "    WETH: $(python3 -c "print(f'{${ALICE_WETH:-0} / 10**18:.6f}')" ) WETH"
echo "    USDC: $(python3 -c "print(f'{${ALICE_USDC:-0} / 10**6:,.2f}')" ) USDC"
echo "  Deployer:"
echo "    WETH: $(python3 -c "print(f'{${DEPLOYER_WETH:-0} / 10**18:.6f}')" ) WETH"
echo "    USDC: $(python3 -c "print(f'{${DEPLOYER_USDC:-0} / 10**6:,.2f}')" ) USDC"
echo ""

# ── Step 12: Deployer removes liquidity ───────────────────────────────

echo "── Step 12: Deployer removes liquidity ──"
echo ""

if [[ -n "$LP_TOKEN_ID" ]]; then
    # Check deployer's LP balance
    DEPLOYER_LP=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$LP_TOKEN_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*' || echo "0")

    if [[ "$DEPLOYER_LP" -gt 0 ]]; then
        info "Deployer LP balance: $DEPLOYER_LP"

        # Approve pool to spend LP tokens
        info "Approving pool to spend LP tokens..."
        run_cowboy_cli \
            "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL approve --token-id $LP_TOKEN_ID --spender 0x$POOL_ADDR --amount $DEPLOYER_LP" \
            "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
              approve --token-id "$LP_TOKEN_ID" --spender "0x$POOL_ADDR" --amount "$DEPLOYER_LP"

        sleep 5

        # Remove all liquidity
        REMOVE_PAYLOAD="{\"sender\":\"$DEPLOYER_HEX\",\"lp_amount\":$DEPLOYER_LP}"
        info "Removing all liquidity..."
        if demo_capture_command 1 \
            "$COWBOY_BIN actor execute --actor $POOL_ADDR --handler remove_liquidity --payload-json $REMOVE_PAYLOAD --private-key $WORK_DIR/deployer-key --cycles-limit $CYCLES --cells-limit $CELLS" \
            "$COWBOY_BIN" actor execute \
              --actor "$POOL_ADDR" \
              --handler remove_liquidity \
              --payload-json "$REMOVE_PAYLOAD" \
              --private-key "$WORK_DIR/deployer-key" \
              --cycles-limit "$CYCLES" \
              --cells-limit "$CELLS"; then
            :
        else
            :
        fi
        REMOVE_OUTPUT="$DEMO_LAST_OUTPUT"

        if echo "$REMOVE_OUTPUT" | grep -q '"Success"'; then
            ok "Liquidity removed"
        else
            warn "remove_liquidity may have failed"
            echo "$REMOVE_OUTPUT"
        fi

        sleep 5
    else
        warn "Deployer has no LP tokens (LP balance: $DEPLOYER_LP)"
    fi
else
    warn "Skipping remove_liquidity (LP token ID not available)"
fi
echo ""

# ── Step 13: Final balances ───────────────────────────────────────────

echo "── Step 13: Final balances ──"
echo ""

DEPLOYER_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
DEPLOYER_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
ALICE_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')
ALICE_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')

echo "  Deployer:"
echo "    WETH: $(python3 -c "print(f'{${DEPLOYER_WETH:-0} / 10**18:.6f}')" ) WETH"
echo "    USDC: $(python3 -c "print(f'{${DEPLOYER_USDC:-0} / 10**6:,.2f}')" ) USDC"
echo "  Alice:"
echo "    WETH: $(python3 -c "print(f'{${ALICE_WETH:-0} / 10**18:.6f}')" ) WETH"
echo "    USDC: $(python3 -c "print(f'{${ALICE_USDC:-0} / 10**6:,.2f}')" ) USDC"
echo ""

# ── Summary ───────────────────────────────────────────────────────────

echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Pool actor:  0x$POOL_ADDR"
echo "  WETH token:  $WETH_ID"
echo "  USDC token:  $USDC_ID"
[[ -n "$LP_TOKEN_ID" ]] && echo "  LP token:    $LP_TOKEN_ID"
echo ""
echo "  Check pool logs:"
echo "    cowboy actor logs --address $POOL_ADDR"
echo ""
