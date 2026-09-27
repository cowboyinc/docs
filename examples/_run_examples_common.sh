#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLES_DIR="$ROOT_DIR/examples"
WORKSPACE_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
NODE_ROOT="$WORKSPACE_ROOT/node"
RUN_EXAMPLES_SWEEP_SCRIPT="${RUN_EXAMPLES_SWEEP_SCRIPT:-$EXAMPLES_DIR/test_examples_local.sh}"
RUN_EXAMPLES_SPAWN_SCRIPT="${RUN_EXAMPLES_SPAWN_SCRIPT:-$EXAMPLES_DIR/_run_examples_spawn_local.sh}"
MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
CANYON_RPC_URL="${CANYON_RPC_URL:-https://rpc.canyon.cowboylabs.net}"
LOCAL_RPC_URL="${LOCAL_RPC_URL:-http://localhost:${RPC_PORT:-5000}}"

print_run_examples_usage() {
  cat <<'EOF'
Usage: ./run_examples.sh [--verbose] [--spawn-local] [--server local|mesa|canyon|URL] [example selectors...]

Options:
  --verbose            Show full Cowboy CLI output for each command during the sweep.
  --spawn-local        Start a temporary local validator, then run the example sweep against it.
  --server VALUE       Target an existing validator. Accepted values: local, mesa, canyon, or a full http(s) URL.
  --gallery            Run gallery examples instead of the core curriculum.
  --all                Run both core curriculum and gallery examples.
  -h, --help           Show this help text.

Examples:
  ./run_examples.sh
  ./run_examples.sh --verbose core/08-minimal-runner-continuation
  ./run_examples.sh --gallery copy-trading
  ./run_examples.sh --all blackjack
  ./run_examples.sh --server canyon
  ./run_examples.sh --server local 10
  ./run_examples.sh --server http://localhost:4000 runner-llm
  ./run_examples.sh --spawn-local 09-runner-llm
  ./run_examples.sh --spawn-local 12-capstone-agent
EOF
}

run_examples_error() {
  echo "$*" >&2
  echo >&2
  print_run_examples_usage >&2
  exit 1
}

resolve_run_examples_server() {
  local value="$1"

  case "$value" in
    mesa)
      echo "$MESA_RPC_URL"
      ;;
    canyon)
      echo "$CANYON_RPC_URL"
      ;;
    local)
      echo "$LOCAL_RPC_URL"
      ;;
    http://*|https://*)
      echo "$value"
      ;;
    *)
      run_examples_error "Unsupported --server value: $value"
      ;;
  esac
}

run_examples_find_cowboy() {
  if [[ -n "${COWBOY_BIN:-}" && -x "$COWBOY_BIN" ]]; then
    echo "$COWBOY_BIN"
    return 0
  fi
  if command -v cowboy >/dev/null 2>&1; then
    echo "cowboy"
    return 0
  fi
  if [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then
    echo "$NODE_ROOT/target/release/cowboy"
    return 0
  fi
  echo "cowboy CLI not found. Build it with:" >&2
  echo "  cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release" >&2
  return 1
}

run_examples_wallet_address() {
  local cowboy_bin="$1"
  local key_file="$2"
  "$cowboy_bin" wallet address --key "$key_file" 2>/dev/null | tail -1
}

run_examples_account_balance() {
  local rpc_url="$1"
  local addr="$2"
  curl -fsS --max-time 2 "$rpc_url/account/$addr" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("balance", ""))' 2>/dev/null || true
}

run_examples_faucet() {
  local rpc_url="$1"
  local addr="$2"
  curl -fsS --max-time 10 --connect-timeout 3 -X POST "$rpc_url/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}" >/dev/null
}

run_examples_poll_account_funded() {
  local rpc_url="$1"
  local addr="$2"
  local previous="${3:-0}"
  local attempts="${4:-30}"
  local delay="${5:-1}"
  local i current

  for ((i = 1; i <= attempts; i++)); do
    current="$(run_examples_account_balance "$rpc_url" "$addr")"
    if [[ -n "$current" ]] && [[ "$current" =~ ^[0-9]+$ ]] && (( current > previous )); then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

run_examples_prepare_key() {
  local rpc_url="$1"

  if [[ "${RUN_EXAMPLES_PREPARE_KEY:-1}" != "1" ]]; then
    return 0
  fi
  if [[ -n "${PRIVATE_KEY_FILE:-}" || "${USE_SHARED_DEMO_KEY:-0}" == "1" ]]; then
    return 0
  fi

  local cowboy_bin tmp_dir key_file addr previous_balance attempt
  cowboy_bin="$(run_examples_find_cowboy)"
  tmp_dir="$(mktemp -d)"
  key_file="$tmp_dir/run-examples.key"
  "$cowboy_bin" wallet create --output "$key_file" >/dev/null
  chmod 600 "$key_file"
  addr="$(run_examples_wallet_address "$cowboy_bin" "$key_file")"
  previous_balance="$(run_examples_account_balance "$rpc_url" "$addr")"
  if [[ -z "$previous_balance" || ! "$previous_balance" =~ ^[0-9]+$ ]]; then
    previous_balance=0
  fi

  echo "Preparing funded demo wallet for remote example sweep"
  echo "  RPC:     $rpc_url"
  echo "  Address: $addr"

  for attempt in 1 2 3; do
    if run_examples_faucet "$rpc_url" "$addr"; then
      if run_examples_poll_account_funded "$rpc_url" "$addr" "$previous_balance"; then
        export PRIVATE_KEY_FILE="$key_file"
        export RUN_EXAMPLES_TEMP_KEY_DIR="$tmp_dir"
        trap 'rm -rf "${RUN_EXAMPLES_TEMP_KEY_DIR:-}" >/dev/null 2>&1 || true' EXIT
        echo "  Wallet funded."
        echo
        return 0
      fi
    fi
    sleep 2
  done

  rm -rf "$tmp_dir" >/dev/null 2>&1 || true
  echo "Could not fund a generated demo wallet from $rpc_url/faucet." >&2
  echo "The devnet faucet may be unavailable or rate-limited." >&2
  echo "Use --spawn-local for a no-setup local run, or set PRIVATE_KEY_FILE to a funded devnet key." >&2
  return 1
}

run_examples_main() {
  local spawn_local=0
  local verbose=0
  local mode="core"
  local server_value=""
  local server_specified=0
  local args=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --verbose)
        verbose=1
        shift
        ;;
      --spawn-local)
        spawn_local=1
        shift
        ;;
      --gallery)
        mode="gallery"
        shift
        ;;
      --all)
        mode="all"
        shift
        ;;
      --server)
        if [[ $# -lt 2 ]]; then
          run_examples_error "--server requires a value"
        fi
        server_value="$2"
        server_specified=1
        shift 2
        ;;
      -h|--help)
        print_run_examples_usage
        return 0
        ;;
      --*)
        run_examples_error "Unknown flag: $1"
        ;;
      *)
        args+=("$1")
        shift
        ;;
    esac
  done

  if [[ "$spawn_local" == "1" && "$server_specified" == "1" ]]; then
    run_examples_error "--spawn-local cannot be combined with --server"
  fi

  if [[ "$spawn_local" == "1" ]]; then
    if [[ "$verbose" == "1" ]]; then
      export COWBOY_EXAMPLES_VERBOSE=1
    fi
    export COWBOY_EXAMPLES_MODE="$mode"
    if [[ "${#args[@]}" -gt 0 ]]; then
      bash "$RUN_EXAMPLES_SPAWN_SCRIPT" "${args[@]}"
    else
      bash "$RUN_EXAMPLES_SPAWN_SCRIPT"
    fi
    return 0
  fi

  if [[ "$server_specified" != "1" ]]; then
    server_value="mesa"
  fi

  local rpc_url
  rpc_url="$(resolve_run_examples_server "$server_value")"
  export RPC_URL="$rpc_url"
  export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
  if [[ "$verbose" == "1" ]]; then
    export COWBOY_EXAMPLES_VERBOSE=1
  fi
  export COWBOY_EXAMPLES_MODE="$mode"
  run_examples_prepare_key "$rpc_url"

  if [[ "${#args[@]}" -gt 0 ]]; then
    bash "$RUN_EXAMPLES_SWEEP_SCRIPT" "${args[@]}"
  else
    bash "$RUN_EXAMPLES_SWEEP_SCRIPT"
  fi
}
