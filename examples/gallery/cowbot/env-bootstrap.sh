#!/usr/bin/env bash
# env-bootstrap.sh — boot validator + 3 cbfs relays on this host
# (jefferson) with RPC bound to 0.0.0.0 so shasta can reach the chain
# over Tailscale.
#
# Logs:
#   /tmp/cowbot-validator.log
#   /tmp/cowbot-cbfs-{1,2,3}.log
#
# Stop with: ./env-bootstrap.sh stop

set -euo pipefail

ROOT="${COWBOY_ROOT:-$HOME/dev/cowboy}"
NODE_DIR="$ROOT/node"
CBFS_DIR="$ROOT/cbfs"
DEVNET_DIR="$ROOT/tmp/cowboy-local-devnet"
CBFS_DATA_DIR="/tmp/cowbot-cbfs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_KEYS_DIR="$SCRIPT_DIR/relay-keys"

VALIDATOR_BIN="$NODE_DIR/target/release/validator"
COWBOY_BIN="$NODE_DIR/target/release/cowboy"
CBFS_NODE_BIN="$CBFS_DIR/target/release/cbfs-node"

VALIDATOR_PEER_ID="4ec7998347efc78363b5b689d8f78a2d644bdb83d7a4ecc5e79e2db4a5ed026d"
P2P_PORT=4000
RPC_PORT=4002
METRICS_PORT=4001

# $1 selects the cowboy network: `local` or `devnet` (alias `canyon`). $NETWORK
# feeds BOTH genesis.network (validator) and CBFS_NETWORK (cbfs nodes) so they
# never drift — a mismatch fails owner CapToken verification (the delegation-cert
# network check) and breaks public-volume writes. Default `devnet`/canyon
# matches the node's built-in DEFAULT_NETWORK and the dashboard service identity.
MODE="${1:-devnet}"
case "$MODE" in
  local)         NETWORK=local ;;
  devnet|canyon) NETWORK=canyon ;;
  *) echo "usage: $0 [local|devnet]   (default: devnet/canyon)" >&2; exit 1 ;;
esac

# Tailnet-reachable IP for jefferson — used in chain genesis so shasta-side
# runner can resolve the relays (127.0.0.1 obviously doesn't work from
# another host).
JEFFERSON_TAILNET_IP="${JEFFERSON_TAILNET_IP:?set to the tailnet IP of the host running the relays}"

# Per-relay operator wallet addresses (used as `address` in genesis and
# CBFS_RELAY_ADDRESS env var on each cbfs-node so usage reports refresh
# heartbeat / keep relays assignment-eligible on chain).
RELAY1_ADDR="000000000000000000000000000000000000000a"
RELAY2_ADDR="000000000000000000000000000000000000000b"
RELAY3_ADDR="000000000000000000000000000000000000000c"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}▸ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✅ $*${NC}"; }
fail() { echo -e "  ${RED}❌ $*${NC}" >&2; exit 1; }

if [[ "${1:-}" == "stop" ]]; then
    log "stopping validator + cbfs relays"
    pkill -f 'validator.*peers' 2>/dev/null || true
    pkill -f 'cbfs-node' 2>/dev/null || true
    sleep 1
    ok "stopped"
    exit 0
fi

# ── prereqs ─────────────────────────────────────────────────────────────────
[[ -x "$VALIDATOR_BIN" ]] || fail "validator not built: $VALIDATOR_BIN"
[[ -x "$COWBOY_BIN" ]]    || fail "cowboy CLI not built: $COWBOY_BIN"
[[ -x "$CBFS_NODE_BIN" ]] || fail "cbfs-node not built: $CBFS_NODE_BIN"
for i in 1 2 3; do
  [[ -f "$RELAY_KEYS_DIR/node$i/node_id" ]]            || fail "missing fixture $RELAY_KEYS_DIR/node$i/node_id"
  [[ -f "$RELAY_KEYS_DIR/node$i/relay_identity_key" ]] || fail "missing fixture $RELAY_KEYS_DIR/node$i/relay_identity_key"
done

ulimit -n 8192 2>/dev/null || true

# ── kill any prior procs ────────────────────────────────────────────────────
pkill -f 'validator.*peers' 2>/dev/null || true
pkill -f 'cbfs-node' 2>/dev/null || true
sleep 2

# ── derive each relay's Ed25519 pubkey from its seed ────────────────────────
RELAY_PUBKEYS=$(python3 - <<PY
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
for i in (1, 2, 3):
    seed = open(f"$RELAY_KEYS_DIR/node{i}/relay_identity_key", "rb").read()
    pk = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    print(pk.hex())
PY
)
RELAY1_PUB=$(echo "$RELAY_PUBKEYS" | sed -n 1p)
RELAY2_PUB=$(echo "$RELAY_PUBKEYS" | sed -n 2p)
RELAY3_PUB=$(echo "$RELAY_PUBKEYS" | sed -n 3p)
RELAY1_NID=$(xxd -p "$RELAY_KEYS_DIR/node1/node_id" | tr -d '\n')
RELAY2_NID=$(xxd -p "$RELAY_KEYS_DIR/node2/node_id" | tr -d '\n')
RELAY3_NID=$(xxd -p "$RELAY_KEYS_DIR/node3/node_id" | tr -d '\n')

# ── write devnet config ─────────────────────────────────────────────────────
mkdir -p "$DEVNET_DIR"
rm -rf "$DEVNET_DIR/storage"

cat > "$DEVNET_DIR/peers.yaml" <<EOF
addresses:
  $VALIDATOR_PEER_ID: 127.0.0.1:$P2P_PORT
EOF

cat > "$DEVNET_DIR/$VALIDATOR_PEER_ID.yaml" <<EOF
private_key: 9d93c9d7c2facab641add386cb86600cba1a581cf265bff116f222bc38137521 # gitleaks:allow (single local validator, loopback only)
share: 004572d68fab1a57e2f7778435155fc9c3e01b7d7debef2be9a51ca1fccd8ddfad
polynomial: 000000000101aea1aba57252f92e95df87e639985589b3c5b10579a96d4ccfcb7f5c6a6e8b7f9b64ea458402798c79f4a4fbd6d5d3fd10881bfd8379fa143c11968cc9e8159ea76d06fffb7c6d6135eb6081cd59b0dd405ef56f66f4f171b4e8549c89825a81
port: $P2P_PORT
metrics_port: $METRICS_PORT
directory: $DEVNET_DIR/storage/$VALIDATOR_PEER_ID
worker_threads: 2
log_level: info
local: true
allowed_peers:
- $VALIDATOR_PEER_ID
bootstrappers: []
message_backlog: 128
mailbox_size: 128
deque_size: 128
signature_threads: 1
indexer: null
genesis_config_path: genesis.json
rpc_port: $RPC_PORT
enable_faucet: false
chain_id: null
EOF

cat > "$DEVNET_DIR/genesis.json" <<EOF
{
  "version": { "major": 1, "minor": 0, "patch": 0 },
  "total_supply": 1000000000000000000,
  "chain_id": 9999,
  "network": "${NETWORK}",
  "accounts": [
    { "seed": 0,  "balance": 499901000000000000, "nonce": 0 },
    { "public_key": "0x000000000000000000000000000000000000000a", "balance": 33000000000000, "nonce": 0 },
    { "public_key": "0x000000000000000000000000000000000000000b", "balance": 33000000000000, "nonce": 0 },
    { "public_key": "0x000000000000000000000000000000000000000c", "balance": 33000000000000, "nonce": 0 },
    { "seed": 1,  "balance": 100000000000000000, "nonce": 0 },
    { "seed": 2,  "balance": 100000000000000000, "nonce": 0 },
    { "seed": 10, "balance":  50000000000000000, "nonce": 0 },
    { "seed": 11, "balance":  50000000000000000, "nonce": 0 },
    { "seed": 12, "balance":  50000000000000000, "nonce": 0 }
  ],
  "relay_nodes": [
    { "node_id": "$RELAY1_NID", "relay_identity_pubkey": "$RELAY1_PUB", "endpoint": "$JEFFERSON_TAILNET_IP:9241", "address": "$RELAY1_ADDR", "capacity_bytes": 10737418240 },
    { "node_id": "$RELAY2_NID", "relay_identity_pubkey": "$RELAY2_PUB", "endpoint": "$JEFFERSON_TAILNET_IP:9242", "address": "$RELAY2_ADDR", "capacity_bytes": 10737418240 },
    { "node_id": "$RELAY3_NID", "relay_identity_pubkey": "$RELAY3_PUB", "endpoint": "$JEFFERSON_TAILNET_IP:9243", "address": "$RELAY3_ADDR", "capacity_bytes": 10737418240 }
  ]
}
EOF

ok "devnet config regenerated at $DEVNET_DIR"

# ── start validator (RPC bound 0.0.0.0 so shasta can hit it) ────────────────
log "starting validator (RPC on 0.0.0.0:$RPC_PORT)"
RPC_BIND_ADDRESS=0.0.0.0 \
RPC_GLOBAL_RATE_LIMIT_PER_SEC=10000 \
"$VALIDATOR_BIN" \
    --peers="$DEVNET_DIR/peers.yaml" \
    --config="$DEVNET_DIR/$VALIDATOR_PEER_ID.yaml" \
    > /tmp/cowbot-validator.log 2>&1 &
VAL_PID=$!

# wait for RPC ready
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$RPC_PORT/height" >/dev/null 2>&1; then
        HEIGHT=$(curl -s "http://127.0.0.1:$RPC_PORT/height" | python3 -c "import json,sys; print(json.load(sys.stdin)['height'])")
        ok "validator running pid=$VAL_PID at block $HEIGHT"
        break
    fi
    kill -0 "$VAL_PID" 2>/dev/null || fail "validator died, see /tmp/cowbot-validator.log"
    sleep 2
done
curl -sf "http://127.0.0.1:$RPC_PORT/height" >/dev/null 2>&1 || fail "validator RPC didn't come up in 60s"

# ── start 3 cbfs relays ─────────────────────────────────────────────────────
log "starting 3 cbfs relays on 9241-9243"
rm -rf "$CBFS_DATA_DIR"
mkdir -p "$CBFS_DATA_DIR"

for i in 1 2 3; do
    port=$((9240 + i))
    data_dir="$CBFS_DATA_DIR/node${i}"
    config="$CBFS_DATA_DIR/node${i}.toml"
    mkdir -p "$data_dir"

    # Seed identity from fixture so chain RAS recognizes this node.
    cp "$RELAY_KEYS_DIR/node$i/node_id"            "$data_dir/node_id"
    cp "$RELAY_KEYS_DIR/node$i/relay_identity_key" "$data_dir/relay_identity_key"
    chmod 600 "$data_dir/relay_identity_key"

    peers=""
    for j in 1 2 3; do
        [ $j -eq $i ] && continue
        [ -n "$peers" ] && peers="$peers, "
        peers="${peers}\"127.0.0.1:$((9240 + j))\""
    done

    cat > "$config" <<EOF
listen_addr = "0.0.0.0:${port}"
data_dir = "${data_dir}"
gc_interval_secs = 15
orphan_shard_ttl_secs = 30
seed_peers = [$peers]
EOF

    addr_var="RELAY${i}_ADDR"
    relay_addr="${!addr_var}"
    CBFS_COWBOY_RPC_URL="http://127.0.0.1:$RPC_PORT" \
    CBFS_CHAIN_ID=9999 \
    CBFS_NETWORK="$NETWORK" \
    CBFS_RELAY_ADDRESS="0x$relay_addr" \
    RUST_LOG=info \
    "$CBFS_NODE_BIN" --config "$config" > "/tmp/cowbot-cbfs-${i}.log" 2>&1 &
done

sleep 2
for i in 1 2 3; do
    lsof -i ":$((9240 + i))" >/dev/null 2>&1 || fail "cbfs node $i didn't start (see /tmp/cowbot-cbfs-${i}.log)"
done
ok "3 cbfs relays running on ports 9241-9243"

# ── show summary ────────────────────────────────────────────────────────────
echo
echo "validator   : http://127.0.0.1:$RPC_PORT  (also reachable as http://$JEFFERSON_TAILNET_IP:$RPC_PORT over tailnet)"
echo "cbfs relays : 127.0.0.1:9241, 127.0.0.1:9242, 127.0.0.1:9243"
echo "                node_ids: $RELAY1_NID, $RELAY2_NID, $RELAY3_NID"
echo "                operators: 0x$RELAY1_ADDR, 0x$RELAY2_ADDR, 0x$RELAY3_ADDR"
echo "logs        : /tmp/cowbot-validator.log, /tmp/cowbot-cbfs-{1,2,3}.log"
echo
echo "next: ./devnet-shasta.sh   (run from jefferson; SSHes into shasta)"
echo "      ./register-runner.sh"
