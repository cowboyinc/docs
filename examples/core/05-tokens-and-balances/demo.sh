#!/usr/bin/env bash
#
# CIP-20 Token Demo — Cowboy Token Standard
# ==========================================
#
# This script demonstrates creating and managing CIP-20 fungible tokens
# on the Cowboy blockchain using the `cowboy` CLI.
#
# CIP-20 tokens are first-class chain primitives (system instructions),
# not actor-based contracts. This means token operations are fast, cheap,
# and natively enforced by the validator.
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
    # Try the release build path
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

echo "============================================"
echo "  Tokens And Balances Demo"
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
echo "  Deployer: $DEPLOYER"

$COWBOY_BIN wallet create --output "$WORK_DIR/alice-key" 2>/dev/null
ALICE=$($COWBOY_BIN wallet address --key "$WORK_DIR/alice-key" 2>/dev/null)
echo "  Alice:    $ALICE"

$COWBOY_BIN wallet create --output "$WORK_DIR/bob-key" 2>/dev/null
BOB=$($COWBOY_BIN wallet address --key "$WORK_DIR/bob-key" 2>/dev/null)
echo "  Bob:      $BOB"
echo ""

# ── Step 2: Fund wallets via faucet ────────────────────────────────────

echo "── Step 2: Funding wallets via faucet ──"
echo ""

if [[ "$USING_SHARED_LOCAL_KEY" -eq 1 ]]; then
    echo "  Using shared localhost dev key for deployer; funding it via faucet in case it is not pre-funded"
fi

# Fund-and-verify: a faucet POST can be silently dropped (rate limiting), and
# an unfunded signer later fails with skipped_reasons=["insufficient_balance"].
# Ask, then poll the account balance; stop asking once the balance is real.
fund_via_faucet() {
    local addr="$1" attempts="${DEMO_FAUCET_TOPUPS:-10}" i bal
    for ((i = 1; i <= attempts; i++)); do
        curl --max-time 5 --connect-timeout 2 -s -X POST "$RPC_URL/faucet" \
            -H 'Content-Type: application/json' \
            -d "{\"address\": \"$addr\"}" >/dev/null || true
        sleep 2
        bal=$(curl --max-time 3 -s "$RPC_URL/account/$addr" \
            | python3 -c 'import json,sys
try:
    print(int(json.load(sys.stdin).get("balance", 0)))
except Exception:
    print(0)' 2>/dev/null || echo 0)
        if [[ "$bal" =~ ^[0-9]+$ ]] && (( bal > 0 )); then
            echo "  Funded $addr"
            return 0
        fi
    done
    echo "  WARNING: $addr still unfunded after $attempts faucet attempts"
    return 1
}

for ADDR in "$DEPLOYER" "$ALICE" "$BOB"; do
    fund_via_faucet "$ADDR" || true
done
wait_for_account "$DEPLOYER" || true

if [[ "$USING_SHARED_LOCAL_KEY" -eq 1 ]]; then
    for ADDR in "$ALICE" "$BOB"; do
        run_cowboy_cli \
            "$COWBOY_BIN transfer --rpc-url $RPC_URL --private-key $WORK_DIR/deployer-key --to $ADDR --amount 1000" \
            "$COWBOY_BIN" transfer --rpc-url "$RPC_URL" \
              --private-key "$WORK_DIR/deployer-key" \
              --to "$ADDR" \
              --amount 1000
        sleep 5
    done
fi

echo ""

# ── Step 3: Create tokens ─────────────────────────────────────────────

echo "── Step 3: Creating CIP-20 tokens ──"
echo ""

# Create WETH token (18 decimals, 10 WETH initial supply)
# 10 * 10^18 = 10000000000000000000
echo "  Creating WETH (Wrapped Ether, 18 decimals, 10 WETH supply)..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL create --name \"Wrapped Ether\" --symbol WETH --decimals 18 --initial-supply 10000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      create --name "Wrapped Ether" --symbol WETH --decimals 18 \
      --initial-supply 10000000000000000000

# Wait for WETH tx to be included (nonce must advance before next tx)
echo "  Waiting for WETH tx..."
sleep 5

# Create USDC token (6 decimals, 1M USDC initial supply)
# 1_000_000 * 10^6 = 1000000000000
echo "  Creating USDC (USD Coin, 6 decimals, 1M USDC supply)..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL create --name \"USD Coin\" --symbol USDC --decimals 6 --initial-supply 1000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      create --name "USD Coin" --symbol USDC --decimals 6 \
      --initial-supply 1000000000000

echo ""

# Wait for USDC tx to be included
echo "  Waiting for USDC tx..."
sleep 5

# ── Step 4: List tokens and get IDs ───────────────────────────────────

echo ""
echo "── Step 4: Listing tokens ──"
echo ""
show_manual "$COWBOY_BIN token --rpc-url $RPC_URL list"

TOKEN_LIST=$($COWBOY_BIN token --rpc-url "$RPC_URL" list)
echo "$TOKEN_LIST" | python3 -c "
import json, sys
# token list returns a paginated envelope: {items, limit, offset, total}
tokens = json.load(sys.stdin)['items']
deployer = '${DEPLOYER}'.lower()
our_tokens = [t for t in tokens if str(t['owner']).lower() == deployer]
for t in our_tokens:
    decimals = t['decimals']
    supply = int(t['total_supply'])
    human_supply = supply / (10 ** decimals)
    print(f\"  {t['symbol']:6s}  id={t['token_id'][:16]}...  supply={human_supply:,.0f}  decimals={decimals}\")
"

# Extract token IDs
WETH_ID=$(echo "$TOKEN_LIST" | python3 -c "
import json, sys
# token list returns a paginated envelope: {items, limit, offset, total}
tokens = json.load(sys.stdin)['items']
deployer = '${DEPLOYER}'.lower()
for t in tokens:
    if str(t['owner']).lower() == deployer and t['symbol'] == 'WETH':
        print(t['token_id'])
        break
")

USDC_ID=$(echo "$TOKEN_LIST" | python3 -c "
import json, sys
# token list returns a paginated envelope: {items, limit, offset, total}
tokens = json.load(sys.stdin)['items']
deployer = '${DEPLOYER}'.lower()
for t in tokens:
    if str(t['owner']).lower() == deployer and t['symbol'] == 'USDC':
        print(t['token_id'])
        break
")

echo ""
echo "  WETH token_id: $WETH_ID"
echo "  USDC token_id: $USDC_ID"

if [[ -z "$WETH_ID" || -z "$USDC_ID" ]]; then
    echo ""
    echo "  ERROR: Could not find token IDs. Token creation may have failed."
    echo "  All tokens on chain:"
    echo "$TOKEN_LIST" | python3 -m json.tool
    exit 1
fi
echo ""

# ── Step 5: Check deployer balances ───────────────────────────────────

echo "── Step 5: Initial balances ──"
echo ""
show_manual "$COWBOY_BIN token --rpc-url $RPC_URL balance --token-id $WETH_ID --address $DEPLOYER"

WETH_BAL=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
USDC_BAL=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')

echo "  Deployer WETH: $(python3 -c "print(f'{$WETH_BAL / 10**18:.4f}')" ) WETH"
echo "  Deployer USDC: $(python3 -c "print(f'{$USDC_BAL / 10**6:,.2f}')" ) USDC"
echo ""

# ── Step 6: Transfer tokens ──────────────────────────────────────────

echo "── Step 6: Transferring tokens ──"
echo ""

# Transfer 2 WETH to Alice
echo "  Transferring 2 WETH to Alice..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL transfer --token-id $WETH_ID --to $ALICE --amount 2000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      transfer --token-id "$WETH_ID" --to "$ALICE" --amount 2000000000000000000

echo "  Waiting for WETH transfer..."
sleep 5

# Transfer 100,000 USDC to Alice
echo "  Transferring 100,000 USDC to Alice..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL transfer --token-id $USDC_ID --to $ALICE --amount 100000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      transfer --token-id "$USDC_ID" --to "$ALICE" --amount 100000000000

echo ""
echo "  Waiting for USDC transfer..."
sleep 5

# ── Step 7: Check balances after transfer ─────────────────────────────

echo ""
echo "── Step 7: Balances after transfer ──"
echo ""

DEPLOYER_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
ALICE_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')
DEPLOYER_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$DEPLOYER" 2>/dev/null | grep -o '[0-9]*')
ALICE_USDC=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$USDC_ID" --address "$ALICE" 2>/dev/null | grep -o '[0-9]*')

echo "  Deployer: $(python3 -c "print(f'{$DEPLOYER_WETH / 10**18:.4f}')" ) WETH, $(python3 -c "print(f'{$DEPLOYER_USDC / 10**6:,.2f}')" ) USDC"
echo "  Alice:    $(python3 -c "print(f'{$ALICE_WETH / 10**18:.4f}')" ) WETH, $(python3 -c "print(f'{$ALICE_USDC / 10**6:,.2f}')" ) USDC"
echo ""

# ── Step 8: Approve + TransferFrom (DEX pattern) ──────────────────────

echo "── Step 8: Approve (DEX pattern) ──"
echo ""

# Alice approves Bob to spend 1 WETH on her behalf
echo "  Alice approves Bob to spend 1 WETH..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/alice-key --rpc-url $RPC_URL approve --token-id $WETH_ID --spender $BOB --amount 1000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/alice-key" --rpc-url "$RPC_URL" \
      approve --token-id "$WETH_ID" --spender "$BOB" --amount 1000000000000000000

echo ""
echo "  Waiting for approval..."
sleep 5

echo ""

# ── Step 9: Mint additional tokens ───────────────────────────────────

echo "── Step 9: Minting additional tokens ──"
echo ""

echo "  Minting 5 WETH to Bob..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/deployer-key --rpc-url $RPC_URL mint --token-id $WETH_ID --to $BOB --amount 5000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/deployer-key" --rpc-url "$RPC_URL" \
      mint --token-id "$WETH_ID" --to "$BOB" --amount 5000000000000000000

echo ""
echo "  Waiting for mint..."
sleep 5

BOB_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$BOB" 2>/dev/null | grep -o '[0-9]*')
echo "  Bob WETH balance: $(python3 -c "print(f'{$BOB_WETH / 10**18:.4f}')" ) WETH"
echo ""

# ── Step 10: Burn tokens ─────────────────────────────────────────────

echo "── Step 10: Burning tokens ──"
echo ""

echo "  Bob burns 1 WETH..."
run_cowboy_cli \
    "$COWBOY_BIN token --private-key $WORK_DIR/bob-key --rpc-url $RPC_URL burn --token-id $WETH_ID --amount 1000000000000000000" \
    "$COWBOY_BIN" token --private-key "$WORK_DIR/bob-key" --rpc-url "$RPC_URL" \
      burn --token-id "$WETH_ID" --amount 1000000000000000000

echo ""
echo "  Waiting for burn..."
sleep 5

BOB_WETH=$($COWBOY_BIN token --rpc-url "$RPC_URL" balance --token-id "$WETH_ID" --address "$BOB" 2>/dev/null | grep -o '[0-9]*')
echo "  Bob WETH balance after burn: $(python3 -c "print(f'{$BOB_WETH / 10**18:.4f}')" ) WETH"
echo ""

# ── Step 11: Final token info ────────────────────────────────────────

echo "── Step 11: Final token state ──"
echo ""
show_manual "$COWBOY_BIN token --rpc-url $RPC_URL info --token-id $WETH_ID"

echo "  WETH token info:"
$COWBOY_BIN token --rpc-url "$RPC_URL" info --token-id "$WETH_ID" | python3 -c "
import json, sys
t = json.load(sys.stdin)
supply = int(t['total_supply']) / (10 ** t['decimals'])
print(f\"    Name:         {t['name']}\")
print(f\"    Symbol:       {t['symbol']}\")
print(f\"    Decimals:     {t['decimals']}\")
print(f\"    Total Supply: {supply:,.4f} {t['symbol']}\")
owner = str(t['owner'])
print(f\"    Owner:        {owner if owner.startswith('0x') else '0x' + owner}\")
"

echo ""
echo "  USDC token info:"
$COWBOY_BIN token --rpc-url "$RPC_URL" info --token-id "$USDC_ID" | python3 -c "
import json, sys
t = json.load(sys.stdin)
supply = int(t['total_supply']) / (10 ** t['decimals'])
print(f\"    Name:         {t['name']}\")
print(f\"    Symbol:       {t['symbol']}\")
print(f\"    Decimals:     {t['decimals']}\")
print(f\"    Total Supply: {supply:,.2f} {t['symbol']}\")
owner = str(t['owner'])
print(f\"    Owner:        {owner if owner.startswith('0x') else '0x' + owner}\")
"

echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
