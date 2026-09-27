#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
NODE_ROOT="$WORKSPACE_ROOT/node"
source "$EXAMPLES_ROOT/_demo_output.sh"
MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
RPC_URL="${RPC_URL:-$MESA_RPC_URL}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-}"
SALT_PREFIX="${SALT_PREFIX:-blackjack-$(date +%s)}"
CYCLES="${CYCLES:-5000000}"
CELLS="${CELLS:-5000000}"
DEPLOY_CYCLES="${DEPLOY_CYCLES:-5000000}"
DEPLOY_CELLS="${DEPLOY_CELLS:-10000000}"
ACTOR_GAS_CBY="${ACTOR_GAS_CBY:-25}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"

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
  echo "cowboy CLI not found. Build it with:"
  echo "  cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release"
  exit 1
}

COWBOY_BIN="$(find_cowboy)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
HOUSE_KEY="$TMP_DIR/house.key"
ALICE_KEY="$TMP_DIR/alice.key"
BOB_KEY="$TMP_DIR/bob.key"

hex_payload() {
  printf "%s" "$1" | xxd -p -c 9999
}

show_manual() {
  demo_print_command 1 "$*"
}

step_header() {
  local number="$1"
  local title="$2"
  echo
  echo "── Step $number: $title ──"
  echo ""
}

wallet_address() {
  "$COWBOY_BIN" wallet address --key "$1" 2>/dev/null | tail -1
}

ensure_key() {
  :
}

ensure_wallets() {
  "$COWBOY_BIN" wallet create --output "$HOUSE_KEY" >/dev/null
  "$COWBOY_BIN" wallet create --output "$ALICE_KEY" >/dev/null
  "$COWBOY_BIN" wallet create --output "$BOB_KEY" >/dev/null
}

faucet_if_available() {
  local addr="$1"
  curl --max-time 5 --connect-timeout 2 -fsS -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}" >/dev/null 2>&1 || true
}

poll_account_ready() {
  local addr="$1"
  local attempts="${2:-20}"
  local delay="${3:-2}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 2 "$RPC_URL/account/$addr" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  echo "Account $addr did not become queryable after faucet funding."
  exit 1
}

poll_actor_ready() {
  local addr="$1"
  local attempts="${2:-20}"
  local delay="${3:-2}"
  demo_poll_actor_ready "$COWBOY_BIN" "$addr" "$attempts" "$delay" || exit 1
}

run_tx() {
  local command_text="$1"
  shift
  demo_retry_command 1 "${DEMO_TX_RETRY_ATTEMPTS:-3}" "${DEMO_TX_RETRY_DELAY:-1}" "$command_text" "$@"
}

run_fund_actor() {
  local actor="$1"
  local amount="$2"
  run_tx \
    "$COWBOY_BIN fund-actor --rpc-url $RPC_URL --private-key $HOUSE_KEY --actor $actor --amount $amount" \
    "$COWBOY_BIN" fund-actor \
      --rpc-url "$RPC_URL" \
      --private-key "$HOUSE_KEY" \
      --actor "$actor" \
      --amount "$amount"
}

deploy_actor() {
  local target="$1"
  local salt_raw="$2"
  local init_payload="${3:-}"
  local salt_hex
  salt_hex="$(printf "%s" "$salt_raw" | xxd -p -c 9999)"
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory. Deploy runs
  # init atomically and the SDK refuses a second init, so the table config
  # rides on the deploy via --init-payload rather than a later call.
  cd "$SCRIPT_DIR"
  local -a init_args=()
  local init_display=""
  if [[ -n "$init_payload" ]]; then
    init_args=(--init-payload "$init_payload")
    init_display=" --init-payload '$init_payload'"
  fi
  demo_retry_command 2 3 1 \
    "$COWBOY_BIN actor deploy $target --salt $salt_hex --private-key $HOUSE_KEY --cycles-limit $DEPLOY_CYCLES --cells-limit $DEPLOY_CELLS$init_display" \
    "$COWBOY_BIN" actor deploy "$target" \
      --salt "$salt_hex" \
      --private-key "$HOUSE_KEY" \
      --cycles-limit "$DEPLOY_CYCLES" \
      --cells-limit "$DEPLOY_CELLS" \
      ${init_args[@]+"${init_args[@]}"}
  local output
  output="$DEMO_LAST_OUTPUT"
  local addr
  addr="$(printf "%s\n" "$output" | awk '/Actor address:/ {print $NF}' | tail -1)"
  if [[ -z "$addr" ]]; then
    echo "Failed to extract actor address for target $target" >&2
    return 1
  fi
  poll_actor_ready "$addr" || return 1
  sleep 2
  printf "%s\n" "$addr"
}

# Handlers decode CBOR, so send JSON via --payload-json. init is the deploy
# boundary (decodes JSON), so it keeps raw --payload.
build_payload_args() {
  local handler="$1"
  local payload="$2"
  if [ "$handler" = "init" ] || [ "$handler" = "set_routes" ]; then
    local payload_hex
    payload_hex="$(hex_payload "$payload")"
    PAYLOAD_ARGS=(--payload "$payload_hex")
    DISPLAY_PAYLOAD="--payload $payload_hex"
  else
    PAYLOAD_ARGS=(--payload-json "$payload")
    DISPLAY_PAYLOAD="--payload-json $payload"
  fi
}

run_exec_once() {
  local key_file="$1"
  local actor="$2"
  local handler="$3"
  local payload="$4"
  local -a PAYLOAD_ARGS
  local DISPLAY_PAYLOAD
  build_payload_args "$handler" "$payload"
  demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $DISPLAY_PAYLOAD --private-key $key_file --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${PAYLOAD_ARGS[@]}" \
      --private-key "$key_file" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
  if demo_output_has_execution_error "$DEMO_LAST_OUTPUT"; then
    echo "Actor execute failed for handler '$handler' on $actor" >&2
    exit 1
  fi
}

run_exec() {
  local key_file="$1"
  local actor="$2"
  local handler="$3"
  local payload="$4"
  local -a PAYLOAD_ARGS
  local DISPLAY_PAYLOAD
  build_payload_args "$handler" "$payload"
  demo_retry_command 1 3 1 \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $DISPLAY_PAYLOAD --private-key $key_file --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${PAYLOAD_ARGS[@]}" \
      --private-key "$key_file" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
  if demo_output_has_execution_error "$DEMO_LAST_OUTPUT"; then
    echo "Actor execute failed for handler '$handler' on $actor" >&2
    exit 1
  fi
}

token_list_json() {
  "$COWBOY_BIN" token --rpc-url "$RPC_URL" list
}

find_token_by_symbol() {
  local symbol="$1"
  local owner="$2"
  token_list_json | python3 -c "
import json, sys
symbol = sys.argv[1]
owner = sys.argv[2].lower()
tokens = json.load(sys.stdin)
for token in tokens:
    if str(token.get('owner', '')).lower() == owner and token.get('symbol') == symbol:
        print(token['token_id'])
        break
" "$symbol" "$owner"
}

ensure_key
ensure_wallets
HOUSE_ADDR="$(wallet_address "$HOUSE_KEY")"
ALICE_ADDR="$(wallet_address "$ALICE_KEY")"
BOB_ADDR="$(wallet_address "$BOB_KEY")"
faucet_if_available "$HOUSE_ADDR"
faucet_if_available "$ALICE_ADDR"
faucet_if_available "$BOB_ADDR"
poll_account_ready "$HOUSE_ADDR"
poll_account_ready "$ALICE_ADDR"
poll_account_ready "$BOB_ADDR"

echo "============================================"
echo "  Blackjack Dealer Demo"
echo "============================================"
echo ""
echo "This walkthrough creates demo chip tokens, deploys a blackjack dealer,"
echo "funds the house bankroll, seats players, exercises splitting, and then"
echo "runs a natural-blackjack payout round."
echo ""
echo "RPC: $RPC_URL"
echo ""

step_header "1" "Creating a demo chip token"
run_tx \
  "$COWBOY_BIN token --private-key $HOUSE_KEY --rpc-url $RPC_URL create --name \"Blackjack Chips\" --symbol CHIPS --decimals 0 --initial-supply 100000" \
  "$COWBOY_BIN" token --private-key "$HOUSE_KEY" --rpc-url "$RPC_URL" \
    create --name "Blackjack Chips" --symbol CHIPS --decimals 0 --initial-supply 100000
sleep 3
TOKEN_ID="$(find_token_by_symbol "CHIPS" "$HOUSE_ADDR")"
if [[ -z "$TOKEN_ID" ]]; then
  echo "Could not find CHIPS token after creation." >&2
  exit 1
fi
echo "Token: $TOKEN_ID"

step_header "2" "Funding players with chips"
run_tx \
  "$COWBOY_BIN token --private-key $HOUSE_KEY --rpc-url $RPC_URL transfer --token-id $TOKEN_ID --to $ALICE_ADDR --amount 500" \
  "$COWBOY_BIN" token --private-key "$HOUSE_KEY" --rpc-url "$RPC_URL" \
    transfer --token-id "$TOKEN_ID" --to "$ALICE_ADDR" --amount 500
run_tx \
  "$COWBOY_BIN token --private-key $HOUSE_KEY --rpc-url $RPC_URL transfer --token-id $TOKEN_ID --to $BOB_ADDR --amount 500" \
  "$COWBOY_BIN" token --private-key "$HOUSE_KEY" --rpc-url "$RPC_URL" \
    transfer --token-id "$TOKEN_ID" --to "$BOB_ADDR" --amount 500
sleep 3

step_header "3" "Deploying the blackjack dealer with the table config"
DEALER_ADDR="$(deploy_actor "dealer" "$SALT_PREFIX-dealer" "{\"token_id\":\"$TOKEN_ID\",\"min_bet\":10,\"max_bet\":100,\"max_players\":2}")"
echo "Dealer actor: $DEALER_ADDR"
run_fund_actor "$DEALER_ADDR" "$ACTOR_GAS_CBY"

step_header "4" "Funding the house bankroll"
run_tx \
  "$COWBOY_BIN token --private-key $HOUSE_KEY --rpc-url $RPC_URL approve --token-id $TOKEN_ID --spender $DEALER_ADDR --amount 5000" \
  "$COWBOY_BIN" token --private-key "$HOUSE_KEY" --rpc-url "$RPC_URL" \
    approve --token-id "$TOKEN_ID" --spender "$DEALER_ADDR" --amount 5000
run_exec "$HOUSE_KEY" "$DEALER_ADDR" "fund_bankroll" '{"amount":1000}'

step_header "5" "Seating players and approving the dealer"
run_exec "$ALICE_KEY" "$DEALER_ADDR" "join_table" '{}'
run_exec "$BOB_KEY" "$DEALER_ADDR" "join_table" '{}'
run_tx \
  "$COWBOY_BIN token --private-key $ALICE_KEY --rpc-url $RPC_URL approve --token-id $TOKEN_ID --spender $DEALER_ADDR --amount 200" \
  "$COWBOY_BIN" token --private-key "$ALICE_KEY" --rpc-url "$RPC_URL" \
    approve --token-id "$TOKEN_ID" --spender "$DEALER_ADDR" --amount 200
run_tx \
  "$COWBOY_BIN token --private-key $BOB_KEY --rpc-url $RPC_URL approve --token-id $TOKEN_ID --spender $DEALER_ADDR --amount 200" \
  "$COWBOY_BIN" token --private-key "$BOB_KEY" --rpc-url "$RPC_URL" \
    approve --token-id "$TOKEN_ID" --spender "$DEALER_ADDR" --amount 200

step_header "6" "Running a split round"
run_exec "$HOUSE_KEY" "$DEALER_ADDR" "seed_next_shoe" '{"cards":["8S","5H","6D","8H","9D","9C","7S","TC","4C","2D","3C","4D"]}'
run_exec "$ALICE_KEY" "$DEALER_ADDR" "place_bet" '{"amount":20}'
run_exec "$BOB_KEY" "$DEALER_ADDR" "place_bet" '{"amount":20}'
run_exec "$HOUSE_KEY" "$DEALER_ADDR" "start_round" '{}'
run_exec "$ALICE_KEY" "$DEALER_ADDR" "act" '{"action":"split","hand_index":0}'
run_exec "$ALICE_KEY" "$DEALER_ADDR" "act" '{"action":"stand","hand_index":0}'
run_exec "$ALICE_KEY" "$DEALER_ADDR" "act" '{"action":"stand","hand_index":1}'
run_exec "$BOB_KEY" "$DEALER_ADDR" "act" '{"action":"stand","hand_index":0}'
run_exec_once "$HOUSE_KEY" "$DEALER_ADDR" "get_last_round" '{}'

step_header "7" "Running a natural blackjack round"
run_exec "$HOUSE_KEY" "$DEALER_ADDR" "seed_next_shoe" '{"cards":["AS","9H","8D","KS","7D","6C","5C","4H"]}'
run_exec "$ALICE_KEY" "$DEALER_ADDR" "place_bet" '{"amount":20}'
run_exec "$BOB_KEY" "$DEALER_ADDR" "place_bet" '{"amount":20}'
run_exec "$HOUSE_KEY" "$DEALER_ADDR" "start_round" '{}'
run_exec "$BOB_KEY" "$DEALER_ADDR" "act" '{"action":"stand","hand_index":0}'
run_exec_once "$HOUSE_KEY" "$DEALER_ADDR" "get_last_round" '{}'

step_header "8" "Reading final table and player state"
run_exec_once "$HOUSE_KEY" "$DEALER_ADDR" "get_table_state" '{}'
run_exec_once "$ALICE_KEY" "$DEALER_ADDR" "get_player_state" '{"player":"'"$ALICE_ADDR"'"}'
run_exec_once "$BOB_KEY" "$DEALER_ADDR" "get_player_state" '{"player":"'"$BOB_ADDR"'"}'

echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Dealer: $DEALER_ADDR"
echo "  Token:  $TOKEN_ID"
echo "  House:  $HOUSE_ADDR"
echo "  Alice:  $ALICE_ADDR"
echo "  Bob:    $BOB_ADDR"
