#!/usr/bin/env bash

set -euo pipefail

DEMO_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${EXAMPLE_DIR:-}" ]]; then
  DEMO_CALLER="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
  EXAMPLE_DIR="$(cd "$(dirname "$DEMO_CALLER")" && pwd)"
fi
SCRIPT_DIR="$EXAMPLE_DIR"
EXAMPLES_ROOT="$DEMO_LIB_DIR"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
NODE_ROOT="$WORKSPACE_ROOT/node"

source "$EXAMPLES_ROOT/_demo_output.sh"

MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
RPC_URL="${RPC_URL:-$MESA_RPC_URL}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"

DEMO_STEP_NUMBER=0
DEMO_LAST_ACTOR=""
DEMO_LAST_ACTOR_TARGET=""
DEMO_LAST_STATUS="${DEMO_LAST_STATUS:-0}"
DEMO_LAST_OUTPUT="${DEMO_LAST_OUTPUT:-}"
DEMO_KEY_SOURCE=""
ADDRESS_FILE="${ADDRESS_FILE:-}"
DEMO_RUNTIME_DIR="${DEMO_RUNTIME_DIR:-$(mktemp -d)}"
trap 'rm -rf "$DEMO_RUNTIME_DIR"' EXIT

demo_find_cowboy() {
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
  echo "cowboy CLI not found. Build it with:" >&2
  echo "  cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release" >&2
  exit 1
}

COWBOY_BIN="$(demo_find_cowboy)"

demo_is_local_rpc() {
  [[ "$RPC_URL" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ ]]
}

demo_hex_payload() {
  printf "%s" "$1" | xxd -p -c 9999
}

demo_normalize_actor_address() {
  local addr="${1:-}"
  if [[ -z "$addr" || "$addr" == 0x* ]]; then
    printf "%s\n" "$addr"
    return
  fi
  printf "0x%s\n" "$addr"
}

demo_next_salt() {
  local seed
  seed="$(printf '%s-%s' "$(basename "$SCRIPT_DIR" | tr -cd 'a-zA-Z0-9' | cut -c1-16)" "$(date +%s%N)" | cut -c1-32)"
  printf "%s" "$seed" | xxd -p -c 9999
}

demo_ensure_key() {
  if [[ -n "${PRIVATE_KEY_FILE:-}" && -f "$PRIVATE_KEY_FILE" ]]; then
    DEMO_KEY_SOURCE="provided"
    return
  fi
  if [[ "${USE_SHARED_DEMO_KEY:-0}" == "1" && -f "$REPO_ROOT/.cowboy/keys/dev" ]]; then
    PRIVATE_KEY_FILE="$REPO_ROOT/.cowboy/keys/dev"
    DEMO_KEY_SOURCE="shared"
    return
  fi
  if [[ "${USE_SHARED_DEMO_KEY:-0}" == "1" && -f "$WORKSPACE_ROOT/.cowboy/key" ]]; then
    PRIVATE_KEY_FILE="$WORKSPACE_ROOT/.cowboy/key"
    DEMO_KEY_SOURCE="shared"
    return
  fi
  if demo_is_local_rpc && [[ -n "${COWBOY_SPAWN_LOCAL_KEY:-}" && -f "$COWBOY_SPAWN_LOCAL_KEY" ]]; then
    PRIVATE_KEY_FILE="$COWBOY_SPAWN_LOCAL_KEY"
    DEMO_KEY_SOURCE="spawn-local"
    return
  fi
  PRIVATE_KEY_FILE="$DEMO_RUNTIME_DIR/demo-key"
  DEMO_KEY_SOURCE="generated"
  if [[ ! -f "$PRIVATE_KEY_FILE" ]]; then
    "$COWBOY_BIN" wallet create --output "$PRIVATE_KEY_FILE" >/dev/null
  fi
}

demo_creator_address() {
  demo_ensure_key
  "$COWBOY_BIN" wallet address --key "$PRIVATE_KEY_FILE" 2>/dev/null | tail -1
}

demo_faucet_if_available() {
  local addr="$1"
  local topups="${DEMO_FAUCET_TOPUPS:-1}"
  local i funded=0
  for ((i = 1; i <= topups; i++)); do
    if curl -fsS -X POST "$RPC_URL/faucet" \
      -H 'Content-Type: application/json' \
      -d "{\"address\":\"$addr\"}" >/dev/null 2>&1; then
      funded=1
    fi
    if [[ "$i" -lt "$topups" ]]; then
      sleep 1
    fi
  done
  [[ "$funded" == "1" ]]
}

demo_poll_account_ready() {
  local addr="$1"
  local attempts="${2:-45}"
  local delay="${3:-2}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 2 "$RPC_URL/account/$addr" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  demo_warn "Account $addr did not become queryable after faucet funding. Continuing anyway."
  return 0
}

demo_account_balance() {
  local addr="$1"
  curl -fsS --max-time 2 "$RPC_URL/account/$addr" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("balance", ""))' 2>/dev/null || true
}

demo_poll_account_funded() {
  local addr="$1"
  local previous="${2:-0}"
  local attempts="${3:-45}"
  local delay="${4:-2}"
  local i current
  for ((i = 1; i <= attempts; i++)); do
    current="$(demo_account_balance "$addr")"
    if [[ -n "$current" ]] && [[ "$current" =~ ^[0-9]+$ ]] && (( current > previous )); then
      return 0
    fi
    sleep "$delay"
  done
  demo_warn "Account $addr did not receive faucet funds before the timeout."
  return 1
}

demo_prepare_creator_account() {
  local creator="$1"
  local previous_balance

  if [[ "$DEMO_KEY_SOURCE" == "spawn-local" ]]; then
    demo_poll_account_ready "$creator"
    return 0
  fi

  if [[ "$DEMO_KEY_SOURCE" == "generated" ]]; then
    previous_balance="$(demo_account_balance "$creator")"
    if [[ -z "$previous_balance" || ! "$previous_balance" =~ ^[0-9]+$ ]]; then
      previous_balance=0
    fi

    demo_emit 2 "  ${DEMO_CLR_CYAN}ℹ${DEMO_CLR_RESET} Funding generated demo wallet from $RPC_URL/faucet"
    if ! demo_faucet_if_available "$creator"; then
      demo_emit 2 "  ${DEMO_CLR_RED}✗${DEMO_CLR_RESET} No faucet was available for generated demo wallet $creator."
      demo_emit 2 "    Use --spawn-local for a no-setup local run, or set PRIVATE_KEY_FILE to a funded devnet key."
      return 1
    fi
    demo_poll_account_funded "$creator" "$previous_balance" "${DEMO_ACCOUNT_FUND_ATTEMPTS:-20}" "${DEMO_ACCOUNT_FUND_DELAY:-1}"
    return $?
  fi

  demo_poll_account_ready "$creator"
}

demo_account_nonce() {
  local addr="$1"
  curl -fsS --max-time 2 "$RPC_URL/account/$addr" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("nonce", ""))' 2>/dev/null || true
}

demo_poll_nonce_advanced() {
  local addr="$1"
  local previous="${2:-0}"
  local attempts="${3:-30}"
  local delay="${4:-2}"
  local i current
  for ((i = 1; i <= attempts; i++)); do
    current="$(demo_account_nonce "$addr")"
    if [[ -n "$current" ]] && [[ "$current" =~ ^[0-9]+$ ]] && (( current > previous )); then
      return 0
    fi
    sleep "$delay"
  done
  demo_warn "Nonce for $addr did not advance past $previous after transaction."
  return 1
}

demo_parse_step_options() {
  DEMO_OPT_WHY=""
  DEMO_OPT_EXPECT=""
  DEMO_OPT_CYCLES=""
  DEMO_OPT_CELLS=""
  DEMO_OPT_INIT=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --init)
        DEMO_OPT_INIT="${2:?--init requires a JSON payload}"
        shift 2
        ;;
      --why)
        DEMO_OPT_WHY="${2:?--why requires text}"
        shift 2
        ;;
      --expect)
        DEMO_OPT_EXPECT="${2:?--expect requires text}"
        shift 2
        ;;
      --cycles)
        DEMO_OPT_CYCLES="${2:?--cycles requires a value}"
        shift 2
        ;;
      --cells)
        DEMO_OPT_CELLS="${2:?--cells requires a value}"
        shift 2
        ;;
      *)
        echo "Unknown demo option: $1" >&2
        exit 1
        ;;
    esac
  done
}

demo_begin() {
  local title="$1"
  local intro="${2:-}"
  echo "============================================"
  echo "  $title"
  echo "============================================"
  echo ""
  [[ -n "$intro" ]] && echo "$intro" && echo ""
  echo "RPC: $RPC_URL"
  echo ""
}

demo_step() {
  local title="$1"
  local body="${2:-}"
  DEMO_STEP_NUMBER=$((DEMO_STEP_NUMBER + 1))
  echo ""
  echo "── Step $DEMO_STEP_NUMBER: $title ──"
  echo ""
  [[ -n "$body" ]] && echo "$body" && echo ""
}

demo_deploy_actor() {
  local target="$1"
  shift
  demo_parse_step_options "$@"
  demo_ensure_key

  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory.
  cd "$SCRIPT_DIR"
  local salt="${SALT:-$(demo_next_salt)}"
  # Deploy runs init atomically, and the SDK refuses a second init, so the
  # init payload rides on the deploy (--init '<json>') rather than a later call.
  local -a init_args=()
  local init_display=""
  if [[ -n "$DEMO_OPT_INIT" ]]; then
    init_args=(--init-payload "$DEMO_OPT_INIT")
    init_display=" --init-payload '$DEMO_OPT_INIT'"
  fi
  local creator previous_nonce addr output
  creator="$(demo_creator_address)"
  if ! demo_prepare_creator_account "$creator"; then
    return 1
  fi
  previous_nonce="$(demo_account_nonce "$creator")"
  if [[ -z "$previous_nonce" || ! "$previous_nonce" =~ ^[0-9]+$ ]]; then
    previous_nonce=0
  fi
  addr="$("$COWBOY_BIN" actor address "$target" --creator "$creator" --salt "$salt" 2>/dev/null \
    | awk '/Actor address:/ {print $NF}' | tail -1)"
  addr="$(demo_normalize_actor_address "$addr")"

  if demo_is_verbose; then
    demo_print_command 2 "$COWBOY_BIN actor deploy $target --salt $salt --private-key $PRIVATE_KEY_FILE --cycles-limit ${DEMO_OPT_CYCLES:-10000000} --cells-limit ${DEMO_OPT_CELLS:-10000000}$init_display"
  else
    demo_print_described_command 2 \
      "${DEMO_OPT_WHY:-Deploy the actor target to the selected Cowboy network.}" \
      "$target" \
      "deploy" \
      "${DEMO_OPT_INIT:-{\}}" \
      "${DEMO_OPT_EXPECT:-A deployed actor address for the next steps.}"
  fi

  if demo_retry_command 2 "${DEMO_TX_RETRY_ATTEMPTS:-3}" "${DEMO_TX_RETRY_DELAY:-1}" \
    "$COWBOY_BIN actor deploy $target --salt $salt --private-key $PRIVATE_KEY_FILE --cycles-limit ${DEMO_OPT_CYCLES:-10000000} --cells-limit ${DEMO_OPT_CELLS:-10000000}$init_display" \
    "$COWBOY_BIN" actor deploy "$target" \
      --salt "$salt" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "${DEMO_OPT_CYCLES:-10000000}" \
      --cells-limit "${DEMO_OPT_CELLS:-10000000}" \
      ${init_args[@]+"${init_args[@]}"}; then
    output="$DEMO_LAST_OUTPUT"
  else
    return "$DEMO_LAST_STATUS"
  fi

  if [[ -z "$addr" ]]; then
    addr="$(printf "%s\n" "$output" | awk '/Actor address:/ {print $NF}' | tail -1)"
    addr="$(demo_normalize_actor_address "$addr")"
  fi
  if [[ -n "$ADDRESS_FILE" ]]; then
    printf "%s" "$addr" > "$ADDRESS_FILE"
  fi
  DEMO_LAST_ACTOR="$addr"
  DEMO_LAST_ACTOR_TARGET="$target"
  demo_poll_nonce_advanced "$creator" "$previous_nonce" >/dev/null || true
  if ! demo_poll_actor_ready "$COWBOY_BIN" "$addr" "${DEMO_ACTOR_READY_ATTEMPTS:-20}" "${DEMO_ACTOR_READY_DELAY:-2}" >&2; then
    exit 1
  fi
  printf "%s\n" "$addr"
}

demo_call() {
  local actor
  actor="$(demo_normalize_actor_address "$1")"
  local handler="$2"
  local payload="$3"
  shift 3
  demo_parse_step_options "$@"
  demo_ensure_key

  # Handlers decode CBOR, so send JSON via --payload-json. init (decodes the
  # deploy CLI's JSON) and set_routes (verbatim CBOR blob) keep raw --payload.
  local -a payload_args
  local display_payload
  if [ "$handler" = "init" ] || [ "$handler" = "set_routes" ]; then
    local payload_hex
    payload_hex="$(demo_hex_payload "$payload")"
    payload_args=(--payload "$payload_hex")
    display_payload="--payload $payload_hex"
  else
    payload_args=(--payload-json "$payload")
    display_payload="--payload-json $payload"
  fi

  demo_capture_described_command 1 \
    "${DEMO_OPT_WHY:-Call the actor handler.}" \
    "$(demo_shorten_address "$actor")" \
    "$handler" \
    "$payload" \
    "${DEMO_OPT_EXPECT:-The actor should execute the handler successfully.}" \
    "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $PRIVATE_KEY_FILE --cycles-limit ${DEMO_OPT_CYCLES:-3000000} --cells-limit ${DEMO_OPT_CELLS:-3000000}" \
    "$COWBOY_BIN" actor execute \
      --actor "$actor" \
      --handler "$handler" \
      "${payload_args[@]}" \
      --private-key "$PRIVATE_KEY_FILE" \
      --cycles-limit "${DEMO_OPT_CYCLES:-3000000}" \
      --cells-limit "${DEMO_OPT_CELLS:-3000000}"
}

demo_wait() {
  local text="$1"
  local seconds="${2:-1}"
  demo_step "Wait" "$text"
  sleep "$seconds"
}

demo_show_status() {
  local actor
  actor="$(demo_normalize_actor_address "$1")"
  demo_parse_step_options "${@:2}"
  demo_capture_described_command 1 \
    "${DEMO_OPT_WHY:-Fetch the actor metadata from the chain.}" \
    "$(demo_shorten_address "$actor")" \
    "actor get" \
    "{}" \
    "${DEMO_OPT_EXPECT:-The actor should be queryable by address.}" \
    "$COWBOY_BIN actor get --address $actor" \
    "$COWBOY_BIN" actor get --address "$actor"
}

demo_done() {
  echo ""
  echo "============================================"
  echo "  Demo complete"
  echo "============================================"
  if [[ -n "${DEMO_LAST_ACTOR:-}" ]]; then
    echo "Actor: $DEMO_LAST_ACTOR"
  fi
}
