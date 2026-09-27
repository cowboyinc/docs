#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
# Overridable for CI / non-sibling checkouts (COW-1346).
NODE_DIR="${NODE_DIR:-$WORKSPACE_ROOT/node}"
PEERS_FILE="$NODE_DIR/test/peers.yaml"
# Resolve the per-validator config dynamically: it is named after the
# validator pubkey and changes on every `setup generate` (COW-1346 — the
# previous hardcoded filename had drifted and broke on fresh fixtures).
resolve_base_config() {
  local found=""
  local f name
  for f in "$NODE_DIR"/test/*.yaml; do
    name="$(basename "$f" .yaml)"
    if [[ "$name" =~ ^[0-9a-f]{64}$ ]]; then
      if [[ -n "$found" ]]; then
        echo "Multiple validator configs in $NODE_DIR/test — set CONFIG_FILE explicitly." >&2
        return 1
      fi
      found="$f"
    fi
  done
  if [[ -z "$found" ]]; then
    echo "No validator config found in $NODE_DIR/test (run node/scripts/run_build.sh first)." >&2
    return 1
  fi
  printf '%s' "$found"
}
CONFIG_FILE="${CONFIG_FILE:-$(resolve_base_config)}"
RESET_STATE="${RESET_STATE:-0}"
NOFILE_LIMIT="${NOFILE_LIMIT:-8192}"
RPC_PORT="${RPC_PORT:-5000}"
CONFIG_TO_USE="$CONFIG_FILE"
TMP_DIR=""

if [[ "$RPC_PORT" != "4000" ]]; then
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  CONFIG_TO_USE="$TMP_DIR/validator.yaml"
  awk -v rpc_port="$RPC_PORT" '
    /^rpc_port:/ { print "rpc_port: " rpc_port; next }
    { print }
  ' "$CONFIG_FILE" > "$CONFIG_TO_USE"
fi

if [[ "$RESET_STATE" == "1" ]]; then
  bash "$ROOT_DIR/examples/reset_local_validator.sh"
fi

echo "Using validator config:"
echo "  $CONFIG_TO_USE"
echo "Using peers file:"
echo "  $PEERS_FILE"
echo "RPC URL:"
echo "  http://localhost:$RPC_PORT"

if command -v lsof >/dev/null 2>&1; then
  echo "Current listeners on validator ports:"
  lsof -nP -iTCP:3000 -iTCP:3001 -iTCP:"$RPC_PORT" -sTCP:LISTEN || true
fi

ulimit -n "$NOFILE_LIMIT" 2>/dev/null || true
echo "Starting local validator with nofile limit: $(ulimit -n 2>/dev/null || echo unknown)"

cd "$NODE_DIR"
cargo run --manifest-path "$NODE_DIR/Cargo.toml" --bin validator -- \
  --peers="$PEERS_FILE" \
  --config="$CONFIG_TO_USE" \
  --enable-faucet
