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
PUBKEY="$(basename "$CONFIG_FILE" .yaml)"
STORAGE_DIR="$NODE_DIR/test/storage/$PUBKEY"

echo "Stopping local validator if it is running..."
pkill -INT -f "target/debug/validator --peers=$PEERS_FILE" 2>/dev/null || true
pkill -INT -f "target/release/validator --peers=$PEERS_FILE" 2>/dev/null || true
sleep 2

echo "Removing validator storage:"
echo "  $STORAGE_DIR"
rm -rf "$STORAGE_DIR"

echo "Local validator state reset."
echo "Config:"
echo "  $CONFIG_FILE"
