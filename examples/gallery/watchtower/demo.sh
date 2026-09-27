#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
NODE_ROOT="${NODE_DIR:-$WORKSPACE_ROOT/node}"
CBSS_ROOT="$WORKSPACE_ROOT/cbss"

MODE="offline"
for arg in "$@"; do
  case "$arg" in
    --live) MODE="live" ;;
    --offline) MODE="offline" ;;
    *)
      echo "usage: $0 [--live|--offline]" >&2
      exit 2
      ;;
  esac
done

RPC_URL="${RPC_URL:-http://localhost:4000}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
CHAIN_ID="${CHAIN_ID:-1}"
CYCLES="${CYCLES:-40000000}"
CELLS="${CELLS:-40000000}"
KEY_EPOCH_BLOCKS="${KEY_EPOCH_BLOCKS:-1000000}"
WORK_DIR="${WORK_DIR:-$(mktemp -d /tmp/cip7-watchtower-demo.XXXXXX)}"
KEEP_WORK="${KEEP_WORK:-0}"
# Devnet genesis admin (seed 0) funds freshly-created demo wallets. Devnets
# typically run with enable_faucet:false, so /faucet 404s; fund via transfer.
GENESIS_KEY_HEX="${GENESIS_KEY_HEX:-0000000000000000000000000000000000000000000000000000000000000001}"
FUND_AMOUNT_CBY="${FUND_AMOUNT_CBY:-10000000}"

cleanup() {
  if [[ "$KEEP_WORK" != "1" && "$WORK_DIR" == /tmp/cip7-watchtower-demo.* ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

find_cowboy() {
  if [[ -n "${COWBOY_BIN:-}" && -x "$COWBOY_BIN" ]]; then
    echo "$COWBOY_BIN"
  elif [[ -n "${COWBOY:-}" && -x "$COWBOY" ]]; then
    echo "$COWBOY"
  elif command -v cowboy >/dev/null 2>&1; then
    command -v cowboy
  elif [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then
    echo "$NODE_ROOT/target/release/cowboy"
  elif [[ -x "$NODE_ROOT/target/debug/cowboy" ]]; then
    echo "$NODE_ROOT/target/debug/cowboy"
  else
    echo "cowboy CLI binary not found; set COWBOY_BIN to an existing binary" >&2
    exit 1
  fi
}

find_bridge() {
  local name="$1"
  local override="$2"
  if [[ -n "$override" && -x "$override" ]]; then
    echo "$override"
  elif [[ -x "$CBSS_ROOT/target/release/$name" ]]; then
    echo "$CBSS_ROOT/target/release/$name"
  elif [[ -x "$CBSS_ROOT/target/debug/$name" ]]; then
    echo "$CBSS_ROOT/target/debug/$name"
  else
    echo "$name binary not found; set ${name//-/_}_BIN to an existing binary" >&2
    exit 1
  fi
}

COWBOY_BIN=""

require_cowboy_command() {
  local label="$1"
  shift
  if ! "$COWBOY_BIN" "$@" --help >/dev/null 2>&1; then
    echo "cowboy CLI at $COWBOY_BIN does not support: $label" >&2
    echo "Set COWBOY_BIN to a binary built from the CIP-7 integration branch." >&2
    exit 1
  fi
}

run_logged() {
  local log="$1"
  shift
  echo "+ $*"
  (cd "$WORK_DIR" && "$@") 2>&1 | tee "$log"
}

run_logged_project() {
  local log="$1"
  shift
  echo "+ $*"
  (cd "$SCRIPT_DIR" && "$@") 2>&1 | tee "$log"
}

wallet_address() {
  "$COWBOY_BIN" wallet address --key "$1" 2>/dev/null | tail -1
}

faucet_if_available() {
  local address="$1"
  # Best-effort faucet (works on devnets with enable_faucet:true).
  curl --max-time 5 --connect-timeout 2 -fsS -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$address\"}" >/dev/null 2>&1 || true
  # Fallback for faucet-disabled devnets: fund from the genesis admin key
  # via a CBY transfer (amount is denominated in CBY, converted to wei by CLI).
  if [[ -n "${GENESIS_KEY_HEX:-}" ]]; then
    printf '%s\n' "$GENESIS_KEY_HEX" > "$WORK_DIR/genesis.key"
    "$COWBOY_BIN" transfer --rpc-url "$RPC_URL" \
      --private-key "$WORK_DIR/genesis.key" \
      --to "$address" \
      --amount "$FUND_AMOUNT_CBY" >/dev/null 2>&1 || true
  fi
}

wait_account() {
  local address="$1"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "$RPC_URL/account/$address" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "account $address did not become queryable at $RPC_URL" >&2
  exit 1
}

wait_release_key() {
  local address="$1"
  for _ in $(seq 1 180); do
    if curl -fsS --max-time 3 "$RPC_URL/cbss/account-release-key/$address" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "CBSS account release key for $address did not become available" >&2
  exit 1
}

wait_delivery_ready() {
  local seal_request_id="$1"
  for _ in $(seq 1 180); do
    if curl -fsS --max-time 3 "$RPC_URL/cbss/cip7-delivery/$seal_request_id" \
      | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status", {}).get("lifecycle") == "Ready" else 1)' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "CBSS delivery $seal_request_id did not become Ready" >&2
  exit 1
}

extract_actor_address() {
  awk '/Actor address:/ {print $NF}' "$1" | tail -1
}

extract_feed_address() {
  awk '/Feed created:/ {print $NF}' "$1" | tail -1
}

extract_account_key_id() {
  python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'"account_key_id"\s*:\s*(\d+)', text)
print(match.group(1) if match else "1")
PY
}

extract_seal_request_id() {
  python3 - "$1" <<'PY'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8").read()

def to_hex(val):
    # CLI renders bytes32 as a JSON array of ints; accept that or a hex string.
    if isinstance(val, list):
        return "0x" + bytes(val).hex()
    if isinstance(val, str):
        return "0x" + val.lower().removeprefix("0x")
    return None

# Preferred: parse the KeyAccessReceipt JSON and read seal_requests_minted[0].
for obj in re.findall(r"\{.*\}", text, re.S):
    try:
        data = json.loads(obj)
    except json.JSONDecodeError:
        continue
    minted = data.get("seal_requests_minted")
    if minted:
        h = to_hex(minted[0].get("seal_request_id"))
        if h and len(h) == 66:
            print(h)
            raise SystemExit(0)

# Fallback: a bare hex string somewhere in the output.
m = re.search(r'"seal_request_id"\s*:\s*"?0?x?([0-9a-fA-F]{64})"?', text)
if not m:
    raise SystemExit("no seal_request_id in acquire-access output")
print("0x" + m.group(1).lower())
PY
}

extract_key_value() {
  local name="$1"
  local file="$2"
  awk -F= -v key="$name" '$1 == key {print $2}' "$file" | tail -1
}

count_live_proxies() {
  curl -fsS --max-time 5 "$RPC_URL/cbss/proxies" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("proxies", [])))'
}

prepare_signal() {
  python3 "$SCRIPT_DIR/tools/edgar_poller.py" --clusters > "$WORK_DIR/clusters.jsonl"
  python3 "$SCRIPT_DIR/tools/paid_consumer.py" --clusters "$WORK_DIR/clusters.jsonl" > "$WORK_DIR/signal.json"
  grep -q '"ticker": "AAPL"' "$WORK_DIR/signal.json"
  echo "BUY_SIGNAL_OK ticker=AAPL"
}

offline() {
  mkdir -p "$WORK_DIR"
  printf '0100000000000000000000000000000000000000000000000000000000000000\n' > "$WORK_DIR/publisher.key"
  prepare_signal
  python3 "$SCRIPT_DIR/tools/watchtower_crypto.py" stream-secret --output "$WORK_DIR/stream_secret.key" > "$WORK_DIR/secret.log"
  python3 "$SCRIPT_DIR/tools/watchtower_crypto.py" paid-message \
    --wallet-key-file "$WORK_DIR/publisher.key" \
    --stream-secret-file "$WORK_DIR/stream_secret.key" \
    --stream-id form4/paid \
    --chain-id "$CHAIN_ID" \
    --sequence 1 \
    --key-epoch 0 \
    --plaintext-file "$WORK_DIR/signal.json" \
    --nonce 0x242424242424242424242424242424242424242424242424 \
    --output "$WORK_DIR/paid-message.cbor" > "$WORK_DIR/paid-message.log"
  local content_key publisher_key message_hex
  content_key="$(extract_key_value content_key "$WORK_DIR/paid-message.log")"
  publisher_key="$(extract_key_value publisher_public_key "$WORK_DIR/paid-message.log")"
  message_hex="$(xxd -p -c 9999 "$WORK_DIR/paid-message.cbor")"
  python3 "$SCRIPT_DIR/tools/paid_consumer.py" \
    --message-cbor-hex "$message_hex" \
    --content-key "$content_key" \
    --publisher-key "$publisher_key" \
    | tee "$WORK_DIR/decrypted.json" >/dev/null
  grep -q '"ticker": "AAPL"' "$WORK_DIR/decrypted.json"
  echo "PAID_CRYPTO_OK fixture decrypt recovered AAPL"
  echo "offline workdir=$WORK_DIR"
}

live() {
  local PREWRAP_BIN UNSEAL_BIN
  mkdir -p "$WORK_DIR"
  COWBOY_BIN="$(find_cowboy)"
  if ! curl -fs --max-time 5 "$RPC_URL/health" >/dev/null; then
    echo "live CIP-7 demo requires a reachable node at $RPC_URL" >&2
    exit 1
  fi
  require_cowboy_command "watchtower account register-key" watchtower account register-key
  require_cowboy_command "watchtower feed fetch-key" watchtower feed 0000000000000000000000000000000000000000 fetch-key

  local proxy_count
  proxy_count="$(count_live_proxies)"
  if (( proxy_count < 5 )); then
    echo "live CIP-7 demo requires at least 5 registered CBSS proxies; found $proxy_count" >&2
    exit 1
  fi
  PREWRAP_BIN="$(find_bridge cbss-cip7-prewrap "${CBSS_CIP7_PREWRAP_BIN:-}")"
  UNSEAL_BIN="$(find_bridge cbss-cip7-unseal "${CBSS_CIP7_UNSEAL_BIN:-}")"

  echo "RPC=$RPC_URL"
  echo "CBSS proxies=$proxy_count"
  echo "workdir=$WORK_DIR"
  prepare_signal

  (cd "$WORK_DIR" && "$COWBOY_BIN" init local --rpc-url "$RPC_URL" >/dev/null)
  "$COWBOY_BIN" wallet create --output "$WORK_DIR/publisher.key" >/dev/null
  "$COWBOY_BIN" wallet create --output "$WORK_DIR/subscriber.key" >/dev/null
  local publisher subscriber
  publisher="$(wallet_address "$WORK_DIR/publisher.key")"
  subscriber="$(wallet_address "$WORK_DIR/subscriber.key")"
  # Fund + confirm serially: both transfers come from the same genesis key,
  # so the second must wait for the first to land or they collide on nonce.
  faucet_if_available "$publisher"
  wait_account "$publisher"
  faucet_if_available "$subscriber"
  wait_account "$subscriber"

  run_logged "$WORK_DIR/registry.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" init

  run_logged "$WORK_DIR/open-new.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" \
    new feed --name "form4-open" --description "fixture Form 4 signals" --stream-id form4/open --ring-buffer-capacity 16
  local open_feed
  open_feed="$(extract_feed_address "$WORK_DIR/open-new.log")"
  run_logged "$WORK_DIR/open-publish.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" \
    feed "$open_feed" publish --stream-id form4/open --kind form4_signal --data "$(cat "$WORK_DIR/signal.json")"
  run_logged "$WORK_DIR/open-consume.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/subscriber.key" \
    feed "$open_feed" consume --cursor 0 --limit 10
  grep -q '"ticker": "AAPL"' "$WORK_DIR/open-consume.log"
  echo "FREE_OK feed=$open_feed"

  python3 "$SCRIPT_DIR/tools/watchtower_crypto.py" init-payload \
    --wallet-key-file "$WORK_DIR/publisher.key" \
    --stream-id form4/paid \
    --name form4-paid \
    --description "paid fixture Form 4 signals" \
    --access-mode SUBSCRIBER_PAID \
    --ring-buffer-capacity 16 \
    --output "$WORK_DIR/paid-init.cbor" > "$WORK_DIR/paid-init.log"

  local salt paid_feed
  salt="$(printf 'form4-paid-%s' "$(date +%s)" | xxd -p -c 9999)"
  run_logged_project "$WORK_DIR/paid-deploy.log" "$COWBOY_BIN" --indexer-url "$RPC_URL" actor deploy stream \
    --salt "$salt" \
    --private-key "$WORK_DIR/publisher.key" \
    --cycles-limit "$CYCLES" \
    --cells-limit "$CELLS" \
    --init-payload "@$WORK_DIR/paid-init.cbor"
  paid_feed="$(extract_actor_address "$WORK_DIR/paid-deploy.log")"

  run_logged "$WORK_DIR/paid-config.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" \
    feed "$paid_feed" init-paid \
    --stream-id form4/paid \
    --fee-per-key-epoch-cby 1 \
    --protocol-fee-bps 0 \
    --publisher-treasury "$publisher" \
    --key-epoch-blocks "$KEY_EPOCH_BLOCKS" \
    --content-cipher XCHACHA20_POLY1305 \
    --min-purchase-epochs 1

  if ! curl -fsS --max-time 3 "$RPC_URL/cbss/account-release-key/$publisher" >/dev/null 2>&1; then
    run_logged "$WORK_DIR/dkg.log" "$COWBOY_BIN" secrets --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" request-account-dkg
  fi
  wait_release_key "$publisher"

  run_logged "$WORK_DIR/prewrap.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" \
    feed "$paid_feed" pre-wrap-epochs \
    --stream-id form4/paid \
    --from 0 \
    --to 0 \
    --chain-id "$CHAIN_ID" \
    --output register-content-keys.cbor \
    --prewrap-bin "$PREWRAP_BIN"
  local stream_secret
  stream_secret="$(awk -F': ' '/Stream secret:/ {print $2}' "$WORK_DIR/prewrap.log" | tail -1)"

  run_logged "$WORK_DIR/register-content-keys.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/publisher.key" \
    feed "$paid_feed" register-content-keys --payload @register-content-keys.cbor

  python3 "$SCRIPT_DIR/tools/watchtower_crypto.py" x25519-keypair \
    --private-output "$WORK_DIR/recipient-x25519.key" \
    --public-output "$WORK_DIR/recipient-x25519.pub" > "$WORK_DIR/recipient-x25519.log"
  local recipient_public
  recipient_public="$(extract_key_value recipient_public_key "$WORK_DIR/recipient-x25519.log")"
  run_logged "$WORK_DIR/register-account-key.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/subscriber.key" \
    account register-key --public-key "$recipient_public"
  local account_key_id
  account_key_id="$(extract_account_key_id "$WORK_DIR/register-account-key.log")"

  run_logged "$WORK_DIR/acquire-access.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/subscriber.key" \
    feed "$paid_feed" acquire-access \
    --stream-id form4/paid \
    --target-key-epoch 0 \
    --account-key-id "$account_key_id"
  local seal_request_id
  seal_request_id="$(extract_seal_request_id "$WORK_DIR/acquire-access.log")"
  wait_delivery_ready "$seal_request_id"

  run_logged "$WORK_DIR/fetch-key.log" "$COWBOY_BIN" watchtower --rpc-url "$RPC_URL" --private-key "$WORK_DIR/subscriber.key" \
    feed "$paid_feed" fetch-key \
    --seal-request-id "$seal_request_id" \
    --recipient-key-file "$WORK_DIR/recipient-x25519.key" \
    --unseal-bin "$UNSEAL_BIN"
  local content_key publisher_key message_hex
  content_key="$(extract_key_value content_key "$WORK_DIR/fetch-key.log")"
  publisher_key="$(extract_key_value publisher_public_key "$WORK_DIR/paid-init.log")"

  python3 "$SCRIPT_DIR/tools/watchtower_crypto.py" paid-message \
    --wallet-key-file "$WORK_DIR/publisher.key" \
    --stream-secret-file "$stream_secret" \
    --stream-id form4/paid \
    --chain-id "$CHAIN_ID" \
    --sequence 1 \
    --key-epoch 0 \
    --plaintext-file "$WORK_DIR/signal.json" \
    --output "$WORK_DIR/paid-message.cbor" > "$WORK_DIR/paid-message.log"
  run_logged "$WORK_DIR/paid-publish.log" "$COWBOY_BIN" --indexer-url "$RPC_URL" actor execute \
    --actor "$paid_feed" \
    --handler publish \
    --payload @paid-message.cbor \
    --private-key "$WORK_DIR/publisher.key" \
    --cycles-limit "$CYCLES" \
    --cells-limit "$CELLS"
  message_hex="$(xxd -p -c 9999 "$WORK_DIR/paid-message.cbor")"
  python3 "$SCRIPT_DIR/tools/paid_consumer.py" \
    --message-cbor-hex "$message_hex" \
    --content-key "$content_key" \
    --publisher-key "$publisher_key" \
    | tee "$WORK_DIR/paid-decrypted.json" >/dev/null
  grep -q '"ticker": "AAPL"' "$WORK_DIR/paid-decrypted.json"
  echo "PAID_OK feed=$paid_feed seal_request_id=$seal_request_id"
}

case "$MODE" in
  offline) offline ;;
  live) live ;;
esac
