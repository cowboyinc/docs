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
SALT_PREFIX="${SALT_PREFIX:-escrow-demo-$(date +%s)}"
CYCLES="${CYCLES:-4000000}"
CELLS="${CELLS:-4000000}"
TRADE_ID="${TRADE_ID:-trade-1}"
SECOND_TRADE_ID="${SECOND_TRADE_ID:-trade-2}"
TRADE_AMOUNT="${TRADE_AMOUNT:-100}"
TOTAL_ESCROW_AMOUNT="${TOTAL_ESCROW_AMOUNT:-$((TRADE_AMOUNT * 2))}"
TOKEN_NAME="${TOKEN_NAME:-Escrow Demo Token}"
TOKEN_SYMBOL="${TOKEN_SYMBOL:-ESC}"
TOKEN_DECIMALS="${TOKEN_DECIMALS:-0}"
TOKEN_SUPPLY="${TOKEN_SUPPLY:-1000000}"
ESCROW_ROLE_LABEL="Escrow (source of truth)"
BUYER_ROLE_LABEL="Buyer proxy"
SELLER_ROLE_LABEL="Seller proxy"
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
  "$COWBOY_BIN" wallet create --output "$PRIVATE_KEY_FILE" >/dev/null
}

creator_address() {
  "$COWBOY_BIN" wallet address --key "$PRIVATE_KEY_FILE" 2>/dev/null | tail -1
}

faucet_if_available() {
  local addr="$1"
  local topups="${DEMO_FAUCET_TOPUPS:-10}"
  local i
  for ((i = 1; i <= topups; i++)); do
    curl --max-time 5 --connect-timeout 2 -fsS -X POST "$RPC_URL/faucet" \
      -H 'Content-Type: application/json' \
      -d "{\"address\":\"$addr\"}" >/dev/null 2>&1 || true
    sleep 1
  done
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

get_account_nonce() {
  local addr="$1"
  curl -fsS --max-time 2 "$RPC_URL/account/$addr" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("nonce", 0))'
}

wait_for_sender_nonce() {
  local sender="$1"
  local previous_nonce="$2"
  local label="$3"
  if [[ "${COWBOY_EXAMPLES_SKIP_NONCE_WAIT:-0}" == "1" ]]; then
    return 0
  fi
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

poll_actor_ready() {
  local addr="$1"
  local attempts="${2:-20}"
  local delay="${3:-2}"
  local i output
  for ((i = 1; i <= attempts; i++)); do
    output="$("$COWBOY_BIN" actor get --address "$addr" 2>&1 || true)"
    if printf "%s" "$output" | grep -Eq 'Actor Information:|code_hash|Code hash'; then
      return 0
    fi
    sleep "$delay"
  done
  echo "Actor $addr did not become queryable after deployment."
  exit 1
}

hex_payload() {
  printf "%s" "$1" | xxd -p -c 9999
}

short_address() {
  demo_shorten_address "$1"
}

step_header() {
  local number="$1"
  local title="$2"
  echo
  echo "── Step $number: $title ──"
  echo ""
}

print_step_context() {
  local line_one="$1"
  local line_two="${2:-}"
  echo "$line_one"
  [[ -n "$line_two" ]] && echo "$line_two"
  echo ""
}

role_display() {
  local label="$1"
  local addr="$2"
  printf '%s (%s)' "$label" "$(short_address "$addr")"
}

deploy_actor() {
  local target="$1"
  local salt_raw="$2"
  local action="${3:-Deploy $target}"
  local actor_label="${4:-$target}"
  local expect="${5:-Actor becomes available on-chain}"
  local salt_hex
  salt_hex="$(printf "%s" "$salt_raw" | xxd -p -c 9999)"
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory.
  cd "$SCRIPT_DIR"
  demo_capture_described_command 2 \
    "$action" \
    "$actor_label" \
    "deploy" \
    "{\"target\":\"$target\",\"salt\":\"$salt_raw\"}" \
    "$expect" \
    "$COWBOY_BIN actor deploy $target --salt $salt_hex --private-key $PRIVATE_KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor deploy "$target" \
      --salt "$salt_hex" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
  local output
  output="$DEMO_LAST_OUTPUT"
  local addr
  addr="$(printf "%s\n" "$output" | awk '/Actor address:/ {print $NF}' | tail -1)"
  if [[ -z "$addr" ]]; then
    echo "Failed to extract actor address for $target" >&2
    return 1
  fi
  poll_actor_ready "$addr" || return 1
  printf "%s\n" "$addr"
}

run_exec() {
  local actor="$1"
  local handler="$2"
  local payload="$3"
  local action="${4:-Call $handler}"
  local actor_label="${5:-$actor}"
  local expect="${6:-Transaction succeeds}"
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
  demo_capture_described_command 1 \
    "$action" \
    "$actor_label" \
    "$handler" \
    "$payload" \
    "$expect" \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $PRIVATE_KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${payload_args[@]}" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
}

run_token_create() {
  demo_capture_described_command 1 \
    "Create a demo CIP-20 token" \
    "Creator wallet" \
    "token create" \
    "{\"name\":\"$TOKEN_NAME\",\"symbol\":\"$TOKEN_SYMBOL\",\"decimals\":$TOKEN_DECIMALS,\"initial_supply\":$TOKEN_SUPPLY}" \
    "Creator receives a fungible token that can be transferred into actor custody" \
    "$COWBOY_BIN token --private-key $PRIVATE_KEY_FILE create --name \"$TOKEN_NAME\" --symbol \"$TOKEN_SYMBOL\" --decimals $TOKEN_DECIMALS --initial-supply $TOKEN_SUPPLY" \
    "$COWBOY_BIN" token --private-key "$PRIVATE_KEY_FILE" create \
      --name "$TOKEN_NAME" \
      --symbol "$TOKEN_SYMBOL" \
      --decimals "$TOKEN_DECIMALS" \
      --initial-supply "$TOKEN_SUPPLY"
}

run_token_list() {
  demo_capture_described_command 1 \
    "List tokens to find the created token ID" \
    "Token registry" \
    "token list" \
    "{\"owner\":\"$CREATOR\",\"symbol\":\"$TOKEN_SYMBOL\"}" \
    "Token list should include the demo token owned by the creator wallet" \
    "$COWBOY_BIN token list" \
    "$COWBOY_BIN" token list
}

extract_token_id() {
  local output="$1"
  local owner_lower
  owner_lower="$(printf "%s" "$2" | tr '[:upper:]' '[:lower:]')"
  printf "%s\n" "$output" | python3 -c 'import json,sys
text=sys.stdin.read()
# token list returns a paginated envelope: {items, limit, offset, total}
start=text.find("{")
if start < 0:
    raise SystemExit(1)
items=json.loads(text[start:])["items"]
owner=(sys.argv[1] or "").lower()
symbol=sys.argv[2]
for item in items:
    if str(item.get("owner", "")).lower() == owner and item.get("symbol") == symbol:
        print(item["token_id"])
        raise SystemExit(0)
raise SystemExit(1)' "$owner_lower" "$3"
}

run_token_transfer() {
  local to="$1"
  local amount="$2"
  local action="$3"
  local expect="$4"
  demo_capture_described_command 1 \
    "$action" \
    "Creator wallet" \
    "token transfer" \
    "{\"token_id\":\"$TOKEN_ID\",\"to\":\"$to\",\"amount\":$amount}" \
    "$expect" \
    "$COWBOY_BIN token --private-key $PRIVATE_KEY_FILE transfer --token-id $TOKEN_ID --to $to --amount $amount" \
    "$COWBOY_BIN" token --private-key "$PRIVATE_KEY_FILE" transfer \
      --token-id "$TOKEN_ID" \
      --to "$to" \
      --amount "$amount"
}

read_balance() {
  local address="$1"
  local actor_label="$2"
  local action="$3"
  local expect="$4"
  demo_capture_described_command 1 \
    "$action" \
    "$actor_label" \
    "token balance" \
    "{\"token_id\":\"$TOKEN_ID\",\"address\":\"$address\"}" \
    "$expect" \
    "$COWBOY_BIN token balance --token-id $TOKEN_ID --address $address" \
    "$COWBOY_BIN" token balance --token-id "$TOKEN_ID" --address "$address"
  BALANCE_VALUE="$(printf "%s\n" "$DEMO_LAST_OUTPUT" | awk -F': ' '/Balance:/ {print $2}' | tail -1)"
}

print_balance_card() {
  local label="$1"
  local amount="$2"
  echo "Balance checkpoint: $label -> ${amount:-unknown}"
}

print_all_balances() {
  read_balance "$BUYER_ADDR" "$(role_display "$BUYER_ROLE_LABEL" "$BUYER_ADDR")" "Read buyer actor balance" "Buyer actor balance should reflect escrowed or refunded funds"
  print_balance_card "$BUYER_ROLE_LABEL" "$BALANCE_VALUE"
  read_balance "$ESCROW_ADDR" "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" "Read escrow actor balance" "Escrow actor balance should match funds currently held in custody"
  print_balance_card "$ESCROW_ROLE_LABEL" "$BALANCE_VALUE"
  read_balance "$SELLER_ADDR" "$(role_display "$SELLER_ROLE_LABEL" "$SELLER_ADDR")" "Read seller actor balance" "Seller actor balance should show released proceeds after successful delivery"
  print_balance_card "$SELLER_ROLE_LABEL" "$BALANCE_VALUE"
}

extract_trade_event_json() {
  local output="$1"
  python3 - "$output" <<'PY'
import json
import re
import sys

text = sys.argv[1]

def coerce_value(raw):
    value = raw.strip().strip('"')
    if value.isdigit():
        return int(value)
    return value

for line in text.splitlines():
    match = re.search(r'- get_trade:\s*(\{.*\})\s*$', line)
    if not match:
        continue
    candidate = match.group(1)
    try:
        trade = json.loads(candidate)
    except Exception:
        inner = candidate.strip()[1:-1].strip()
        trade = {}
        if not inner:
            continue
        try:
            for part in inner.split(","):
                key, value = part.split(":", 1)
                trade[key.strip().strip('"')] = coerce_value(value)
        except Exception:
            continue
    print(json.dumps(trade))
    raise SystemExit(0)
raise SystemExit(1)
PY
}

print_trade_checkpoint() {
  local trade_json="$1"
  python3 - "$trade_json" <<'PY'
import json
import re
import sys

trade = json.loads(sys.argv[1])

def shorten(value):
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{16,}", value):
        return value[:10] + "..." + value[-8:]
    return value

print(
    "Escrow checkpoint: "
    f"state={trade.get('state', 'UNKNOWN')} "
    f"trade_id={trade.get('trade_id', '')} "
    f"amount={trade.get('amount', '')} "
    f"buyer={shorten(trade.get('buyer', ''))} "
    f"seller={shorten(trade.get('seller', ''))}"
)
PY
}

read_trade_state() {
  local trade_id="$1"
  local action="$2"
  local expect="$3"
  run_exec \
    "$ESCROW_ADDR" \
    "get_trade" \
    "{\"trade_id\":\"$trade_id\"}" \
    "$action" \
    "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
    "$expect"

  local trade_json
  if ! trade_json="$(extract_trade_event_json "$DEMO_LAST_OUTPUT")"; then
    demo_warn "Unable to decode get_trade event for $trade_id."
    return 1
  fi

  LAST_TRADE_JSON="$trade_json"
  return 0
}

print_trade_card() {
  local trade_json="$1"
  python3 - "$trade_json" <<'PY'
import json
import re
import sys

trade = json.loads(sys.argv[1])

def shorten(value):
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{16,}", value):
        return value[:10] + "..." + value[-8:]
    return value

print("Final escrow record:")
print(f"  Trade ID: {trade.get('trade_id', '')}")
print(f"  State:    {trade.get('state', 'UNKNOWN')}")
print(f"  Amount:   {trade.get('amount', '')}")
print(f"  Buyer:    {shorten(trade.get('buyer', ''))}")
print(f"  Seller:   {shorten(trade.get('seller', ''))}")
PY
}

print_actor_map() {
  echo "Actor map:"
  echo "  $ESCROW_ROLE_LABEL: $(short_address "$ESCROW_ADDR")"
  echo "    full: $ESCROW_ADDR"
  echo "  $BUYER_ROLE_LABEL:  $(short_address "$BUYER_ADDR")"
  echo "    full: $BUYER_ADDR"
  echo "  $SELLER_ROLE_LABEL: $(short_address "$SELLER_ADDR")"
  echo "    full: $SELLER_ADDR"
}

ensure_key
CREATOR="$(creator_address)"
faucet_if_available "$CREATOR"
poll_account_ready "$CREATOR"

echo "============================================"
echo "  Token Escrow Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys buyer, seller, and escrow actors, creates a demo"
echo "CIP-20 token, funds the buyer actor, approves escrow spending, and then"
echo "proves custody with both a release flow and a refund flow."
echo ""
echo "RPC:             $RPC_URL"
echo "Release trade:   $TRADE_ID"
echo "Cancel trade:    $SECOND_TRADE_ID"
echo "Trade amount:    $TRADE_AMOUNT"
echo "Token funding:   $TOTAL_ESCROW_AMOUNT"
echo ""

step_header "1" "Create a demo CIP-20 token"
print_step_context \
  "This example uses CIP-20 only, not native CBY, so token custody is explicit and inspectable." \
  "The creator wallet will mint the demo supply, then fund the buyer actor."
PREVIOUS_NONCE="$(get_account_nonce "$CREATOR" 2>/dev/null || echo 0)"
run_token_create
wait_for_sender_nonce "$CREATOR" "$PREVIOUS_NONCE" "token create"
run_token_list
TOKEN_ID="$(extract_token_id "$DEMO_LAST_OUTPUT" "$CREATOR" "$TOKEN_SYMBOL")"
if [[ -z "$TOKEN_ID" ]]; then
  echo "Failed to find token $TOKEN_SYMBOL owned by $CREATOR in token list." >&2
  exit 1
fi
echo "Token ID: $TOKEN_ID"

step_header "2" "Deploy the escrow roles"
print_step_context \
  "Deploy one escrow custodian actor plus buyer and seller helper actors." \
  "The escrow actor will be the only address that actually holds locked funds."
ESCROW_ADDR="$(deploy_actor "escrow" "$SALT_PREFIX-escrow" "Deploy authoritative escrow actor" "$ESCROW_ROLE_LABEL" "Escrow actor is ready to hold CIP-20 funds")"
BUYER_ADDR="$(deploy_actor "buyer" "$SALT_PREFIX-buyer" "Deploy buyer helper actor" "$BUYER_ROLE_LABEL" "Buyer proxy can approve and deposit into escrow")"
SELLER_ADDR="$(deploy_actor "seller" "$SALT_PREFIX-seller" "Deploy seller helper actor" "$SELLER_ROLE_LABEL" "Seller proxy can lock and release escrowed funds")"
print_actor_map

step_header "3" "Configure escrow and helper actors"
print_step_context \
  "Escrow needs the token ID, and the buyer actor needs both the escrow address and token ID." \
  "Seller only needs the escrow address because it never approves or funds tokens directly."
run_exec \
  "$ESCROW_ADDR" \
  "configure" \
  "{\"token_id\":\"$TOKEN_ID\"}" \
  "Configure escrow with the demo token" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow stores the token ID it will custody"
run_exec \
  "$BUYER_ADDR" \
  "configure" \
  "{\"escrow_address\":\"$ESCROW_ADDR\",\"token_id\":\"$TOKEN_ID\"}" \
  "Configure buyer proxy with escrow and token" \
  "$(role_display "$BUYER_ROLE_LABEL" "$BUYER_ADDR")" \
  "Buyer proxy stores the escrow address and token ID"
run_exec \
  "$SELLER_ADDR" \
  "configure" \
  "{\"escrow_address\":\"$ESCROW_ADDR\"}" \
  "Configure seller proxy with escrow address" \
  "$(role_display "$SELLER_ROLE_LABEL" "$SELLER_ADDR")" \
  "Seller proxy stores the escrow address"

step_header "4" "Fund the buyer actor and approve escrow"
print_step_context \
  "The creator wallet transfers tokens into the buyer actor, making the actor the economic principal in the demo." \
  "Then the buyer actor approves escrow to pull exactly the amount needed for two trades."
PREVIOUS_NONCE="$(get_account_nonce "$CREATOR" 2>/dev/null || echo 0)"
run_token_transfer \
  "$BUYER_ADDR" \
  "$TOTAL_ESCROW_AMOUNT" \
  "Transfer demo tokens into the buyer actor" \
  "Buyer actor receives spendable CIP-20 balance"
wait_for_sender_nonce "$CREATOR" "$PREVIOUS_NONCE" "token transfer"
run_exec \
  "$BUYER_ADDR" \
  "approve_escrow" \
  "{\"amount\":$TOTAL_ESCROW_AMOUNT}" \
  "Buyer actor approves escrow to pull funds" \
  "$(role_display "$BUYER_ROLE_LABEL" "$BUYER_ADDR")" \
  "Escrow can now transfer approved tokens into custody"
print_all_balances

step_header "5" "Open the release trade"
print_step_context \
  "The buyer actor has approved escrow, then the escrow actor records and pulls from that buyer actor with token_transfer_from." \
  "DEPOSITED now means funds are actually locked in escrow, not merely recorded in storage."
run_exec \
  "$ESCROW_ADDR" \
  "deposit" \
  "{\"trade_id\":\"$TRADE_ID\",\"buyer\":\"$BUYER_ADDR\",\"seller\":\"$SELLER_ADDR\",\"amount\":$TRADE_AMOUNT}" \
  "Escrow pulls approved buyer actor funds for the release flow" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow should custody the buyer actor's tokens and create a DEPOSITED trade"
if read_trade_state "$TRADE_ID" "Read escrow after buyer deposit" "Checkpoint should show DEPOSITED"; then
  print_trade_checkpoint "$LAST_TRADE_JSON"
fi
print_all_balances

step_header "6" "Seller locks the release trade"
print_step_context \
  "The escrow actor verifies the seller actor identity recorded on the trade before locking." \
  "Locking should change only trade state; the escrow actor should still hold the same token balance."
run_exec \
  "$ESCROW_ADDR" \
  "lock" \
  "{\"trade_id\":\"$TRADE_ID\",\"seller\":\"$SELLER_ADDR\"}" \
  "Escrow locks the release trade for the seller actor" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow moves the trade from DEPOSITED to LOCKED"
if read_trade_state "$TRADE_ID" "Read escrow after seller lock" "Checkpoint should show LOCKED"; then
  print_trade_checkpoint "$LAST_TRADE_JSON"
fi
print_all_balances

step_header "7" "Release funds to the seller actor"
print_step_context \
  "The escrow actor releases only after the trade is locked for the recorded seller actor." \
  "Escrow should transfer tokens out of custody into the seller actor and mark the trade RELEASED."
run_exec \
  "$ESCROW_ADDR" \
  "release" \
  "{\"trade_id\":\"$TRADE_ID\",\"seller\":\"$SELLER_ADDR\"}" \
  "Escrow releases funds to the seller actor" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow transfers funds to the seller actor and marks the trade RELEASED"
if read_trade_state "$TRADE_ID" "Read escrow after release" "Checkpoint should show RELEASED"; then
  print_trade_checkpoint "$LAST_TRADE_JSON"
  print_trade_card "$LAST_TRADE_JSON"
fi
print_all_balances

step_header "8" "Open a second trade and cancel it"
print_step_context \
  "A second deposit shows the refund path: escrow pulls approved buyer actor funds, then cancels before lock." \
  "Escrow should return funds to the buyer actor and mark the trade CANCELLED."
run_exec \
  "$ESCROW_ADDR" \
  "deposit" \
  "{\"trade_id\":\"$SECOND_TRADE_ID\",\"buyer\":\"$BUYER_ADDR\",\"seller\":\"$SELLER_ADDR\",\"amount\":$TRADE_AMOUNT}" \
  "Escrow pulls approved buyer actor funds for the refund flow" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow should custody another DEPOSITED trade for the cancel scenario"
run_exec \
  "$ESCROW_ADDR" \
  "cancel" \
  "{\"trade_id\":\"$SECOND_TRADE_ID\"}" \
  "Escrow cancels the second trade and refunds the buyer actor" \
  "$(role_display "$ESCROW_ROLE_LABEL" "$ESCROW_ADDR")" \
  "Escrow refunds the buyer actor and marks the trade CANCELLED"
if read_trade_state "$SECOND_TRADE_ID" "Read escrow after cancel" "Checkpoint should show CANCELLED"; then
  print_trade_checkpoint "$LAST_TRADE_JSON"
fi
print_all_balances

echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Token ID:             $TOKEN_ID"
echo "  $ESCROW_ROLE_LABEL: $ESCROW_ADDR"
echo "  $BUYER_ROLE_LABEL:  $BUYER_ADDR"
echo "  $SELLER_ROLE_LABEL: $SELLER_ADDR"
