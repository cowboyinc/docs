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
SALT_PREFIX="${SALT_PREFIX:-multi-actor-$(date +%s)}"
CYCLES="${CYCLES:-4000000}"
CELLS="${CELLS:-4000000}"
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

deploy_actor() {
  local target="$1"
  local salt_raw="$2"
  local salt_hex
  salt_hex="$(printf "%s" "$salt_raw" | sha256sum | awk '{print $1}')"
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory.
  cd "$SCRIPT_DIR"
  demo_capture_command 2 \
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
  demo_capture_command 1 \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $PRIVATE_KEY_FILE --cycles-limit $CYCLES --cells-limit $CELLS" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${payload_args[@]}" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "$CYCLES" \
      --cells-limit "$CELLS"
}

ensure_key
CREATOR="$(creator_address)"
faucet_if_available "$CREATOR"
poll_account_ready "$CREATOR"

echo "============================================"
echo "  Multi-Actor Workflow Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys a job board, validator, and settlement ledger,"
echo "wires them together, submits work, and then reads the final settlement record."
echo ""
echo "RPC: $RPC_URL"
echo ""

step_header "1" "Deploying workflow actors"
BOARD_ADDR="$(deploy_actor "board" "$SALT_PREFIX-board")"
VALIDATOR_ADDR="$(deploy_actor "validator" "$SALT_PREFIX-validator")"
SETTLEMENT_ADDR="$(deploy_actor "ledger" "$SALT_PREFIX-settlement")"
echo "Job board:         $BOARD_ADDR"
echo "Job validator:     $VALIDATOR_ADDR"
echo "Settlement ledger: $SETTLEMENT_ADDR"

step_header "2" "Configuring actor addresses"
run_exec "$BOARD_ADDR" "configure" "{\"validator_address\":\"$VALIDATOR_ADDR\",\"settlement_address\":\"$SETTLEMENT_ADDR\"}"
run_exec "$SETTLEMENT_ADDR" "configure" "{\"board_address\":\"$BOARD_ADDR\"}"

step_header "3" "Creating a job"
run_exec "$BOARD_ADDR" "create_job" '{"title":"Summarize validator log","required_keyword":"ship","reward_units":2}'
sleep 2

step_header "4" "Submitting completed work"
run_exec "$BOARD_ADDR" "submit_work" '{"job_id":1,"work_text":"Ready to ship after validation."}'
sleep 5

step_header "5" "Reading final settlement"
run_exec "$SETTLEMENT_ADDR" "get_settlement" '{"submission_id":1}'
echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Job board:         $BOARD_ADDR"
echo "  Job validator:     $VALIDATOR_ADDR"
echo "  Settlement ledger: $SETTLEMENT_ADDR"
