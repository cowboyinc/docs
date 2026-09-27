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
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
ACTOR_TARGET="roles"
SALT_RAW="${SALT:-authroles-$(date +%s)}"
SALT_RAW="$(printf "%s" "$SALT_RAW" | cut -c1-24)"
SALT="$(printf "%s" "$SALT_RAW" | xxd -p -c 9999)"

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

OWNER_KEY_FILE="$TMP_DIR/owner.key"
WRITER_KEY_FILE="$TMP_DIR/writer.key"
ADDRESS_FILE="$TMP_DIR/last_actor"

if [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ && -n "${COWBOY_SPAWN_LOCAL_KEY:-}" && -f "$COWBOY_SPAWN_LOCAL_KEY" ]]; then
  cp "$COWBOY_SPAWN_LOCAL_KEY" "$OWNER_KEY_FILE"
else
  "$COWBOY_BIN" wallet create --output "$OWNER_KEY_FILE" >/dev/null
fi
"$COWBOY_BIN" wallet create --output "$WRITER_KEY_FILE" >/dev/null

owner_address() {
  "$COWBOY_BIN" wallet address --key "$OWNER_KEY_FILE" 2>/dev/null | tail -1
}

writer_address() {
  "$COWBOY_BIN" wallet address --key "$WRITER_KEY_FILE" 2>/dev/null | tail -1
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

faucet_if_available() {
  local addr="$1"
  local topups="${DEMO_FAUCET_TOPUPS:-10}"
  local i
  for ((i = 1; i <= topups; i++)); do
    curl -fsS -X POST "$RPC_URL/faucet" \
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

run_exec() {
  local key_file="$1"
  local actor="$2"
  local handler="$3"
  local payload="$4"
  local cycles="${5:-3000000}"
  local cells="${6:-3000000}"
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
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $key_file --cycles-limit $cycles --cells-limit $cells" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${payload_args[@]}" \
      --private-key "$key_file" \
      --cycles-limit "$cycles" \
      --cells-limit "$cells"
}

deploy_actor() {
  local output addr
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory. Deploy runs
  # init in the deploy transaction, so the deployer becomes the owner.
  cd "$SCRIPT_DIR"
  demo_capture_command 2 \
    "$COWBOY_BIN actor deploy $ACTOR_TARGET --salt $SALT --private-key $OWNER_KEY_FILE --cycles-limit ${DEPLOY_CYCLES_LIMIT:-10000000} --cells-limit ${DEPLOY_CELLS_LIMIT:-10000000}" \
    "$COWBOY_BIN" actor deploy "$ACTOR_TARGET" \
      --salt "$SALT" \
      --private-key "$OWNER_KEY_FILE" \
      --cycles-limit "${DEPLOY_CYCLES_LIMIT:-10000000}" \
      --cells-limit "${DEPLOY_CELLS_LIMIT:-10000000}"
  output="$DEMO_LAST_OUTPUT"
  addr="$(printf "%s\n" "$output" | awk '/Actor address:/ {print $NF}' | tail -1)"
  if [[ -z "$addr" ]]; then
    echo "Failed to extract actor address for $ACTOR_TARGET" >&2
    exit 1
  fi
  printf "%s" "$addr" > "$ADDRESS_FILE"
  poll_actor_ready "$addr"
  echo "$addr"
}

OWNER_ADDR="$(owner_address)"
WRITER_ADDR="$(writer_address)"
faucet_if_available "$OWNER_ADDR"
poll_account_ready "$OWNER_ADDR"

demo_capture_command 1 \
  "$COWBOY_BIN transfer --rpc-url $RPC_URL --private-key $OWNER_KEY_FILE --to $WRITER_ADDR --amount 100" \
  "$COWBOY_BIN" transfer \
    --rpc-url "$RPC_URL" \
    --private-key "$OWNER_KEY_FILE" \
    --to "$WRITER_ADDR" \
    --amount 100
sleep 3
poll_account_ready "$WRITER_ADDR"

echo "============================================"
echo "  Senders And Permissions Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys the auth actor, which claims its owner during"
echo "deploy, grants a writer role, performs a delegated write, and then reads"
echo "back the stored entries and role map."
echo ""
echo "RPC: $RPC_URL"
echo ""

step_header "1" "Deploying auth roles actor"
ACTOR_ADDR="$(deploy_actor)"
echo "Actor: $ACTOR_ADDR"

# Reports already_initialized — the guard that keeps this from resetting the roles.
step_header "2" "Confirming ownership (deploy already ran init)"
run_exec "$OWNER_KEY_FILE" "$ACTOR_ADDR" "init" "{\"owner\":\"$OWNER_ADDR\"}"

step_header "3" "Granting writer role"
run_exec "$OWNER_KEY_FILE" "$ACTOR_ADDR" "grant_role" "{\"role\":\"writer\",\"address\":\"$WRITER_ADDR\"}"

step_header "4" "Writing entry as delegated writer"
run_exec "$WRITER_KEY_FILE" "$ACTOR_ADDR" "write_entry" '{"content":"Delegated writers can submit entries."}'

step_header "5" "Reading stored entries"
run_exec "$OWNER_KEY_FILE" "$ACTOR_ADDR" "get_entries" '{}'

step_header "6" "Reading role assignments"
run_exec "$OWNER_KEY_FILE" "$ACTOR_ADDR" "get_roles" '{}'
echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  Actor:  $ACTOR_ADDR"
echo "  Owner:  $OWNER_ADDR"
echo "  Writer: $WRITER_ADDR"
