#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLES_DIR="$ROOT_DIR/examples"
WORKSPACE_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
# Overridable for CI / non-sibling checkouts (COW-1346).
NODE_DIR="${NODE_DIR:-$WORKSPACE_ROOT/node}"
PEERS_FILE="$NODE_DIR/test/peers.yaml"
GENESIS_FILE="$NODE_DIR/test/genesis.json"

# The validator config is named after the validator's pubkey, which changes
# on every `setup generate` — a hardcoded filename breaks the first time the
# fixtures are regenerated (COW-1346: it had drifted and --spawn-local was
# failing on fresh checkouts). Resolve it dynamically: the per-validator
# config is the 64-hex-named yaml in node/test (address-keyed 0x*.yaml files
# are user keypairs, peers.yaml is the address map).
if [[ -n "${BASE_CONFIG:-}" ]]; then
  : # explicit override wins
else
  BASE_CONFIG=""
  for f in "$NODE_DIR"/test/*.yaml; do
    name="$(basename "$f" .yaml)"
    if [[ "$name" =~ ^[0-9a-f]{64}$ ]]; then
      if [[ -n "$BASE_CONFIG" ]]; then
        echo "Multiple validator configs in $NODE_DIR/test — set BASE_CONFIG explicitly." >&2
        exit 1
      fi
      BASE_CONFIG="$f"
    fi
  done
  if [[ -z "$BASE_CONFIG" ]]; then
    echo "No validator config found in $NODE_DIR/test (run node/scripts/run_build.sh first)." >&2
    exit 1
  fi
fi
NOFILE_LIMIT="${NOFILE_LIMIT:-8192}"
RUN_EXAMPLES_SWEEP_SCRIPT="${RUN_EXAMPLES_SWEEP_SCRIPT:-$EXAMPLES_DIR/test_examples_local.sh}"
TMP_DIR="$(mktemp -d)"
CONFIG_FILE="$TMP_DIR/validator.yaml"
DEMO_GENESIS_FILE="$TMP_DIR/genesis.json"
STORAGE_DIR="$TMP_DIR/storage"
VALIDATOR_LOG="$TMP_DIR/validator.log"
SPAWN_LOCAL_KEY_FILE="$TMP_DIR/spawn-local-funded.key"
VALIDATOR_PID=""
HEALTHY=0

pick_free_port() {
  python3 - <<'PY'
import socket

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

cleanup() {
  if [[ -n "$VALIDATOR_PID" ]] && kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
    kill -INT "$VALIDATOR_PID" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
      if ! kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done

    if kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
      kill -TERM "$VALIDATOR_PID" >/dev/null 2>&1 || true
      for _ in $(seq 1 5); do
        if ! kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done
    fi

    if kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
      kill -KILL "$VALIDATOR_PID" >/dev/null 2>&1 || true
    fi

    wait "$VALIDATOR_PID" >/dev/null 2>&1 || true
  fi
  # Preserve the validator log for CI artifacts before the tempdir vanishes
  # (review on COW-1346: the EXIT trap deleted it before any collect step
  # could run, so the uploaded artifact was always empty).
  if [[ -n "${SMOKE_LOG_DIR:-}" && -f "$VALIDATOR_LOG" ]]; then
    mkdir -p "$SMOKE_LOG_DIR"
    cp --backup=numbered "$VALIDATOR_LOG" "$SMOKE_LOG_DIR/" 2>/dev/null || true
  fi
  if [[ "${COWBOY_KEEP_EXAMPLE_TMP:-0}" == "1" ]]; then
    echo "Keeping temporary validator files at $TMP_DIR"
  else
    rm -rf "$TMP_DIR" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

P2P_PORT="$(pick_free_port)"
METRICS_PORT="$(pick_free_port)"
RPC_PORT="$(pick_free_port)"

while [[ "$P2P_PORT" == "$METRICS_PORT" || "$P2P_PORT" == "$RPC_PORT" || "$METRICS_PORT" == "$RPC_PORT" ]]; do
  P2P_PORT="$(pick_free_port)"
  METRICS_PORT="$(pick_free_port)"
  RPC_PORT="$(pick_free_port)"
done

write_demo_genesis() {
  local reputation_floor_x1e9="${DEMO_RUNNER_REPUTATION_FLOOR_X1E9:-50000000000}"
  python3 - "$GENESIS_FILE" "$DEMO_GENESIS_FILE" "$reputation_floor_x1e9" <<'PY'
import json
import sys

src, dst, floor_raw = sys.argv[1:4]
with open(src, "r", encoding="utf-8") as f:
    genesis = json.load(f)

floor = int(floor_raw)
genesis["reputation_config"] = {
    "half_life_blocks": 1_209_600,
    "reputation_floor_x1e9": floor,
    "reputation_ceiling_x1e9": 200_000_000_000,
    "jail_exit_floor_x1e9": max(floor, 50_000_000_000),
    "reputation_normalizer": 100,
}

with open(dst, "w", encoding="utf-8") as f:
    json.dump(genesis, f, indent=2)
    f.write("\n")
PY
}

write_demo_genesis

awk \
  -v port="$P2P_PORT" \
  -v metrics_port="$METRICS_PORT" \
  -v rpc_port="$RPC_PORT" \
  -v directory="$STORAGE_DIR" \
  -v genesis="$DEMO_GENESIS_FILE" '
  /^port:/ { print "port: " port; next }
  /^metrics_port:/ { print "metrics_port: " metrics_port; next }
  /^directory:/ { print "directory: " directory; next }
  /^indexer:/ { print "indexer: null"; next }
  /^genesis_config_path:/ { print "genesis_config_path: " genesis; next }
  /^rpc_port:/ { print "rpc_port: " rpc_port; next }
  { print }
' "$BASE_CONFIG" > "$CONFIG_FILE"

awk '/private_key:/ { print $2; exit }' "$NODE_DIR/test/0x57aD2055065eB8E41c6B52eE9F600DdAB81EA86a.yaml" > "$SPAWN_LOCAL_KEY_FILE"
chmod 600 "$SPAWN_LOCAL_KEY_FILE"

echo "Starting temporary local validator"
echo "  P2P:      $P2P_PORT"
echo "  Metrics:  $METRICS_PORT"
echo "  RPC:      http://localhost:$RPC_PORT"
echo "  Storage:  $STORAGE_DIR"
echo "  Genesis:  $DEMO_GENESIS_FILE"
echo "  Log:      $VALIDATOR_LOG"

# Prefer a prebuilt binary (CI builds release once; cargo run would silently
# kick off a full debug compile). VALIDATOR_BIN overrides; cargo run remains
# the dev-loop fallback.
if [[ -z "${VALIDATOR_BIN:-}" && -x "$NODE_DIR/target/release/validator" ]]; then
  VALIDATOR_BIN="$NODE_DIR/target/release/validator"
fi
(
  cd "$NODE_DIR"
  ulimit -n "$NOFILE_LIMIT" 2>/dev/null || true
  if [[ -n "${VALIDATOR_BIN:-}" ]]; then
    exec "$VALIDATOR_BIN" \
      --peers="$PEERS_FILE" \
      --config="$CONFIG_FILE" \
      --enable-faucet
  else
    exec cargo run --manifest-path "$NODE_DIR/Cargo.toml" --bin validator -- \
      --peers="$PEERS_FILE" \
      --config="$CONFIG_FILE" \
      --enable-faucet
  fi
) >"$VALIDATOR_LOG" 2>&1 &
VALIDATOR_PID="$!"

for _ in $(seq 1 90); do
  if ! kill -0 "$VALIDATOR_PID" >/dev/null 2>&1; then
    echo "Validator exited before becoming healthy."
    tail -n 200 "$VALIDATOR_LOG" || true
    exit 1
  fi

  HEALTH="$(curl -fsS --max-time 2 "http://localhost:$RPC_PORT/health" 2>/dev/null || true)"
  if [[ -n "$HEALTH" ]] && ! printf "%s" "$HEALTH" | grep -qiE '<!DOCTYPE html|<html'; then
    HEALTHY=1
    break
  fi

  sleep 1
done

if [[ "$HEALTHY" != "1" ]]; then
  echo "Validator did not become healthy on http://localhost:$RPC_PORT"
  tail -n 200 "$VALIDATOR_LOG" || true
  exit 1
fi

echo
echo "Validator is healthy. Running example sweep..."
echo

RPC_URL="http://localhost:$RPC_PORT" \
COWBOY_RPC_URL="http://localhost:$RPC_PORT" \
COWBOY_SPAWN_LOCAL_KEY="$SPAWN_LOCAL_KEY_FILE" \
bash "$RUN_EXAMPLES_SWEEP_SCRIPT" "$@"
