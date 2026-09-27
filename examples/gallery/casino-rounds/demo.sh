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
STATE_FILE="${STATE_FILE:-${TMPDIR:-/tmp}/cowboy-casino-rounds-deploy-state.env}"
SALT_PREFIX="${SALT_PREFIX:-casino-example-$(date +%s)}"
MIN_PRICE="${MIN_PRICE:-10}"
MAX_PRICE="${MAX_PRICE:-100}"
ROUND_DURATION_BLOCKS="${ROUND_DURATION_BLOCKS:-12}"
BET_AMOUNT="${BET_AMOUNT:-100}"
BET_INTERVAL_BLOCKS="${BET_INTERVAL_BLOCKS:-4}"
BETTOR_COUNT="${BETTOR_COUNT:-3}"
AUTO_RESTART_DELAY_BLOCKS="${AUTO_RESTART_DELAY_BLOCKS:-1}"
MAX_RECENT_ROUNDS="${MAX_RECENT_ROUNDS:-32}"
MAX_DETAILED_ROUNDS="${MAX_DETAILED_ROUNDS:-1}"
CASINO_POT_TOKENS="${CASINO_POT_TOKENS:-100000}"
BETTOR_TOPUP_TOKENS="${BETTOR_TOPUP_TOKENS:-1000}"
ACTOR_GAS_CBY="${ACTOR_GAS_CBY:-50}"
CYCLES="${CYCLES:-12000000}"
CELLS="${CELLS:-12000000}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
USING_EPHEMERAL_KEY=0

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

hex_payload() {
  printf "%s" "$1" | xxd -p -c 9999
}

step_header() {
  local number="$1"
  local title="$2"
  echo
  echo "── Step $number: $title ──"
  echo ""
}

persist_var() {
  local key="$1"
  local value="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  if [[ -f "$STATE_FILE" ]]; then
    grep -v "^${key}=" "$STATE_FILE" >"$tmp_file" || true
  fi
  printf "%s='%s'\n" "$key" "$value" >>"$tmp_file"
  mv "$tmp_file" "$STATE_FILE"
}

run_capture() {
  demo_retry_command 1 "${DEMO_TX_RETRY_ATTEMPTS:-3}" "${DEMO_TX_RETRY_DELAY:-1}" "$1" "${@:2}"
}

require_success() {
  local label="$1"
  if [[ "$DEMO_LAST_STATUS" -ne 0 ]]; then
    echo "$label failed" >&2
    exit 1
  fi
}

extract_actor_address() {
  printf "%s\n" "$1" | awk '/Actor address:/ {print $NF}' | tail -1
}

extract_token_id() {
  local owner_lower
  owner_lower="$(printf "%s" "$2" | tr '[:upper:]' '[:lower:]')"
  printf "%s\n" "$1" | python3 -c 'import json,sys
text=sys.stdin.read()
start=text.find("[")
if start < 0:
    raise SystemExit(1)
items=json.loads(text[start:])
owner=(sys.argv[1] or "").lower()
symbol=sys.argv[2]
for item in items:
    if str(item.get("owner", "")).lower() == owner and item.get("symbol") == symbol:
        print(item["token_id"])
        raise SystemExit(0)
raise SystemExit(1)' "$owner_lower" "$3"
}

creator_address() {
  "$COWBOY_BIN" wallet address --key "$PRIVATE_KEY_FILE" 2>/dev/null | tail -1
}

faucet_if_available() {
  local addr="$1"
  curl --max-time 5 --connect-timeout 2 -fsS -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}" >/dev/null 2>&1 || true
}

poll_account_ready() {
  local addr="$1"
  local attempts="${2:-30}"
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

get_account_nonce() {
  local addr="$1"
  curl -fsS --max-time 3 "$RPC_URL/account/$addr" \
    | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("nonce", 0)))'
}

wait_for_sender_nonce() {
  local sender="$1"
  local previous_nonce="$2"
  local label="$3"
  local current_nonce
  local i
  for ((i = 1; i <= 20; i++)); do
    current_nonce="$(get_account_nonce "$sender" 2>/dev/null || echo "$previous_nonce")"
    if [[ "$current_nonce" =~ ^[0-9]+$ ]] && (( current_nonce > previous_nonce )); then
      return 0
    fi
    sleep 1
  done
  echo "Nonce for $sender did not advance after $label." >&2
  exit 1
}

run_with_nonce_wait() {
  local label="$1"
  shift
  local previous_nonce
  previous_nonce="$(get_account_nonce "$CREATOR" 2>/dev/null || echo 0)"
  "$@"
  require_success "$label"
  wait_for_sender_nonce "$CREATOR" "$previous_nonce" "$label"
}

deploy_actor() {
  local target="$1"
  local salt_raw="$2"
  local salt_hex
  salt_hex="$(printf "%s" "$salt_raw" | sha256sum | awk '{print $1}')"
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory.
  cd "$SCRIPT_DIR"
  demo_retry_command 2 3 1 \
    "$COWBOY_BIN actor deploy $target --salt $salt_hex --private-key $PRIVATE_KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor deploy "$target" \
      --salt "$salt_hex" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
  local output
  output="$DEMO_LAST_OUTPUT"
  local addr
  addr="$(extract_actor_address "$output")"
  if [[ -z "$addr" ]]; then
    echo "Failed to extract actor address for target $target" >&2
    return 1
  fi
  poll_actor_ready "$addr" || return 1
  printf "%s\n" "$addr"
}

run_exec() {
  local actor="$1"
  local handler="$2"
  local payload="$3"
  # Handlers decode CBOR, so send JSON via --payload-json. init is the deploy
  # boundary (decodes JSON), so it keeps raw --payload.
  local -a payload_args
  local display_payload
  if [ "$handler" = "init" ] || [ "$handler" = "set_routes" ]; then
    local payload_hex
    payload_hex="$(hex_payload "$payload")"
    payload_args=(--payload "$payload_hex")
    display_payload="--payload $payload_hex"
  else
    payload_args=(--payload-json "$payload")
    display_payload="--payload-json $payload"
  fi
  run_capture \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $PRIVATE_KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${payload_args[@]}" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
}

fund_actor() {
  local actor="$1"
  local amount="$2"
  run_capture \
    "$COWBOY_BIN fund-actor --rpc-url $RPC_URL --private-key $PRIVATE_KEY_FILE --actor $actor --amount $amount" \
    "$COWBOY_BIN" fund-actor \
      --rpc-url "$RPC_URL" \
      --private-key "$PRIVATE_KEY_FILE" \
      --actor "$actor" \
      --amount "$amount"
}

token_create() {
  local symbol="$1"
  local name="$2"
  run_capture \
    "$COWBOY_BIN token --private-key $PRIVATE_KEY_FILE --rpc-url $RPC_URL create --name \"$name\" --symbol $symbol --decimals 6 --initial-supply 0" \
    "$COWBOY_BIN" token --private-key "$PRIVATE_KEY_FILE" --rpc-url "$RPC_URL" \
      create --name "$name" --symbol "$symbol" --decimals 6 --initial-supply 0
}

token_list() {
  demo_capture_command 1 \
    "$COWBOY_BIN token --rpc-url $RPC_URL list" \
    "$COWBOY_BIN" token --rpc-url "$RPC_URL" list
}

token_mint() {
  local token_id="$1"
  local to="$2"
  local amount="$3"
  run_capture \
    "$COWBOY_BIN token --private-key $PRIVATE_KEY_FILE --rpc-url $RPC_URL mint --token-id $token_id --to $to --amount $amount" \
    "$COWBOY_BIN" token --private-key "$PRIVATE_KEY_FILE" --rpc-url "$RPC_URL" \
      mint --token-id "$token_id" --to "$to" --amount "$amount"
}

ensure_key() {
  if [[ -n "$PRIVATE_KEY_FILE" && -f "$PRIVATE_KEY_FILE" ]]; then
    return
  fi
  if [[ "${USE_SHARED_DEMO_KEY:-0}" == "1" && -f "$REPO_ROOT/.cowboy/keys/dev" ]]; then
    PRIVATE_KEY_FILE="$REPO_ROOT/.cowboy/keys/dev"
    return
  fi
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  PRIVATE_KEY_FILE="$TMP_DIR/demo-key"
  USING_EPHEMERAL_KEY=1
  "$COWBOY_BIN" wallet create --output "$PRIVATE_KEY_FILE" >/dev/null
}

rm -f "$STATE_FILE"
ensure_key
CREATOR="$(creator_address)"
faucet_if_available "$CREATOR"
poll_account_ready "$CREATOR"
persist_var "RPC_URL" "$RPC_URL"
persist_var "CREATOR_ADDR" "$CREATOR"
if [[ "$USING_EPHEMERAL_KEY" != "1" ]]; then
  persist_var "PRIVATE_KEY_FILE" "$PRIVATE_KEY_FILE"
fi

echo "============================================"
echo "  Casino Rounds Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys a feed actor, a token-backed casino, and several"
echo "automated bettor actors, then leaves the system running for watch_rounds.py."
echo ""
echo "RPC: $RPC_URL"
echo "State file: $STATE_FILE"
echo ""

step_header "1" "Creating a casino token"
TOKEN_SYMBOL="${TOKEN_SYMBOL:-CAS$(date +%H%M%S)}"
TOKEN_NAME="${TOKEN_NAME:-Casino Demo ${SALT_PREFIX}}"
run_with_nonce_wait "token create" token_create "$TOKEN_SYMBOL" "$TOKEN_NAME"
token_list
require_success "token list"
TOKEN_ID="$(extract_token_id "$DEMO_LAST_OUTPUT" "$CREATOR" "$TOKEN_SYMBOL")"
if [[ -z "$TOKEN_ID" ]]; then
  echo "Failed to find token $TOKEN_SYMBOL owned by $CREATOR in token list." >&2
  exit 1
fi
persist_var "TOKEN_ID" "$TOKEN_ID"
persist_var "TOKEN_SYMBOL" "$TOKEN_SYMBOL"

step_header "2" "Deploying actors"
FEED_ADDR="$(deploy_actor "feed" "$SALT_PREFIX-feed")"
CASINO_ADDR="$(deploy_actor "casino" "$SALT_PREFIX-casino")"
persist_var "FEED_ADDR" "$FEED_ADDR"
persist_var "CASINO_ADDR" "$CASINO_ADDR"
echo "Feed actor:   $FEED_ADDR"
echo "Casino actor: $CASINO_ADDR"

declare -a BETTOR_ADDRS=()
for i in $(seq 1 "$BETTOR_COUNT"); do
  addr="$(deploy_actor "bettor" "$SALT_PREFIX-bettor-$i")"
  BETTOR_ADDRS+=("$addr")
  persist_var "BETTOR_ADDR_$i" "$addr"
  echo "Bettor $i:     $addr"
done

step_header "3" "Funding gas and token balances"
run_with_nonce_wait "fund feed actor" fund_actor "$FEED_ADDR" "$ACTOR_GAS_CBY"
run_with_nonce_wait "fund casino actor" fund_actor "$CASINO_ADDR" "$ACTOR_GAS_CBY"
for i in $(seq 1 "$BETTOR_COUNT"); do
  run_with_nonce_wait "fund bettor actor $i" fund_actor "${BETTOR_ADDRS[$((i - 1))]}" "$ACTOR_GAS_CBY"
done
run_with_nonce_wait "mint casino pot" token_mint "$TOKEN_ID" "$CASINO_ADDR" "$CASINO_POT_TOKENS"
for i in $(seq 1 "$BETTOR_COUNT"); do
  run_with_nonce_wait "mint bettor $i balance" token_mint "$TOKEN_ID" "${BETTOR_ADDRS[$((i - 1))]}" "$BETTOR_TOPUP_TOKENS"
done

step_header "4" "Configuring feed, casino, and bettors"
run_with_nonce_wait "configure feed" run_exec "$FEED_ADDR" "configure" "{\"min_price\":$MIN_PRICE,\"max_price\":$MAX_PRICE}"
run_with_nonce_wait "configure casino" run_exec "$CASINO_ADDR" "configure" "{\"self_actor\":\"$CASINO_ADDR\",\"feed_actor\":\"$FEED_ADDR\",\"token_id\":\"$TOKEN_ID\",\"round_duration_blocks\":$ROUND_DURATION_BLOCKS,\"fee_bps\":0,\"auto_restart_delay_blocks\":$AUTO_RESTART_DELAY_BLOCKS,\"max_recent_rounds\":$MAX_RECENT_ROUNDS,\"max_detailed_rounds\":$MAX_DETAILED_ROUNDS}"
for i in $(seq 1 "$BETTOR_COUNT"); do
  run_with_nonce_wait "configure bettor $i" run_exec "${BETTOR_ADDRS[$((i - 1))]}" "configure" "{\"self_actor\":\"${BETTOR_ADDRS[$((i - 1))]}\",\"casino_actor\":\"$CASINO_ADDR\",\"token_id\":\"$TOKEN_ID\",\"min_guess\":$MIN_PRICE,\"max_guess\":$MAX_PRICE,\"bet_interval_blocks\":$BET_INTERVAL_BLOCKS,\"bet_amount\":$BET_AMOUNT}"
done

step_header "5" "Starting automated rounds"
run_with_nonce_wait "start casino auto" run_exec "$CASINO_ADDR" "start_auto" "{\"auto_restart_delay_blocks\":$AUTO_RESTART_DELAY_BLOCKS}"
for i in $(seq 1 "$BETTOR_COUNT"); do
  run_with_nonce_wait "start bettor $i" run_exec "${BETTOR_ADDRS[$((i - 1))]}" "start" '{}'
done
run_with_nonce_wait "generate initial feed price" run_exec "$FEED_ADDR" "generate_price" '{}'

persist_var "MIN_PRICE" "$MIN_PRICE"
persist_var "MAX_PRICE" "$MAX_PRICE"
persist_var "ROUND_DURATION_BLOCKS" "$ROUND_DURATION_BLOCKS"
persist_var "BET_AMOUNT" "$BET_AMOUNT"
persist_var "BET_INTERVAL_BLOCKS" "$BET_INTERVAL_BLOCKS"
persist_var "BETTOR_COUNT" "$BETTOR_COUNT"
persist_var "MAX_RECENT_ROUNDS" "$MAX_RECENT_ROUNDS"
persist_var "MAX_DETAILED_ROUNDS" "$MAX_DETAILED_ROUNDS"

echo
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Token ID:      $TOKEN_ID"
echo "  Feed actor:    $FEED_ADDR"
echo "  Casino actor:  $CASINO_ADDR"
for i in $(seq 1 "$BETTOR_COUNT"); do
  echo "  Bettor $i:     ${BETTOR_ADDRS[$((i - 1))]}"
done
echo ""
echo "Watch the deployed rounds:"
echo "  python3 $SCRIPT_DIR/tools/watch_rounds.py --state-file $STATE_FILE --show-balances"
echo ""
echo "Inspect current state:"
echo "  $COWBOY_BIN actor get --address $CASINO_ADDR"
if [[ "$USING_EPHEMERAL_KEY" != "1" ]]; then
  echo "  $COWBOY_BIN actor execute --actor $CASINO_ADDR --handler get_state --payload-json '{}' --private-key $PRIVATE_KEY_FILE"
  echo "  $COWBOY_BIN actor execute --actor $CASINO_ADDR --handler inspect_round --payload-json '{}' --private-key $PRIVATE_KEY_FILE"
else
  echo "  Re-run with PRIVATE_KEY_FILE=<path> or USE_SHARED_DEMO_KEY=1 if you want signed inspect commands after the demo exits."
fi
