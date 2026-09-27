#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SECRET_TOKEN_ID="MODAL_TOKEN_ID"
SECRET_TOKEN_SECRET="MODAL_TOKEN_SECRET"
ACTOR_FUND_CBY="${ACTOR_FUND_CBY:-50}"
STORE_REGISTRAR="${STORE_REGISTRAR:-}"
HIRE_TERM="${HIRE_TERM:-one-shot:cby:1.5}"
HIRE_AMOUNT="${HIRE_AMOUNT:-1.5}"

info() { printf '\033[36m→\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

COWBOY="${COWBOY:-$REPO/.preserved-bin/cowboy}"
[ -x "$COWBOY" ] || COWBOY="$(command -v cowboy)" || die "no cowboy CLI found"
RPC_URL="${RPC_URL:-http://localhost:4000}"
export COWBOY_RPC_URL="$RPC_URL"
KEYFILE="${KEYFILE:-$HOME/.cowboy/cal-key}"
[ -f "$KEYFILE" ] || die "no wallet key at $KEYFILE"
INGEST="${CBSS_INGEST_ENDPOINT:-${INGEST:-}}"
[ -n "$INGEST" ] || die "set CBSS_INGEST_ENDPOINT"
[ -n "${MODAL_TOKEN_ID_FILE:-}" ] || die "set MODAL_TOKEN_ID_FILE"
[ -f "$MODAL_TOKEN_ID_FILE" ] || die "MODAL_TOKEN_ID_FILE not found"
[ -n "${MODAL_TOKEN_SECRET_FILE:-}" ] || die "set MODAL_TOKEN_SECRET_FILE"
[ -f "$MODAL_TOKEN_SECRET_FILE" ] || die "MODAL_TOKEN_SECRET_FILE not found"
[ -n "${MODAL_ENDPOINT_URL:-}" ] || die "set MODAL_ENDPOINT_URL to a deployed https://<ws>--<fn>.modal.run"
case "$MODAL_ENDPOINT_URL" in
  https://*.modal.run|https://*.modal.run/*) : ;;
  *) die "MODAL_ENDPOINT_URL must be an https://*.modal.run host" ;;
esac
cp "$MODAL_TOKEN_ID_FILE" "$WORK/id.txt"
cp "$MODAL_TOKEN_SECRET_FILE" "$WORK/secret.txt"

curl -sf "$RPC_URL/health" >/dev/null 2>&1 || curl -sf "$RPC_URL/status" >/dev/null 2>&1 \
  || die "cannot reach node at $RPC_URL"
OWNER="$("$COWBOY" wallet address --key "$KEYFILE" 2>/dev/null \
  | grep -oE '0x[0-9a-fA-F]{40}' | head -1 || true)"
[ -n "$OWNER" ] || die "could not resolve owner address"
[ -z "$STORE_REGISTRAR" ] || [ "$STORE_REGISTRAR" = "$OWNER" ] \
  || die "STORE_REGISTRAR must match the demo wallet owner"

BAL="$(curl -s --max-time 15 "$RPC_URL/account/$OWNER" | grep -oE '"balance":[0-9]+' | cut -d: -f2 || true)"
if [ -z "${BAL:-}" ] || [ "${BAL:-0}" -eq 0 ] 2>/dev/null; then
  curl -s --max-time 30 -X POST "$RPC_URL/faucet" -H 'Content-Type: application/json' \
    -d "{\"address\":\"$OWNER\"}" >/dev/null 2>&1 || die "faucet request failed"
  for _ in $(seq 1 20); do
    BAL="$(curl -s --max-time 15 "$RPC_URL/account/$OWNER" | grep -oE '"balance":[0-9]+' | cut -d: -f2 || true)"
    [ -n "${BAL:-}" ] && [ "${BAL:-0}" -gt 0 ] 2>/dev/null && break
    sleep 3
  done
fi
[ -n "${BAL:-}" ] && [ "${BAL:-0}" -gt 0 ] 2>/dev/null || die "owner is unfunded"

if ! curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2; then
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" request-account-dkg >/dev/null \
    || die "request-account-dkg failed"
  for _ in $(seq 1 60); do
    curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2 && break
    sleep 5
  done
fi
curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2 \
  || die "account release key never appeared"

cat > "$WORK/manifest.json" <<EOF
{"name":"modal-compute","version":"1.0.0","entitlements":[{"id":"http.fetch"},{"id":"secrets.read","params":{"keys":["$OWNER:$SECRET_TOKEN_ID","$OWNER:$SECRET_TOKEN_SECRET"]}},{"id":"storage.kv"}]}
EOF

SALT="0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')"
INIT_PAYLOAD="$(STORE_REGISTRAR="$STORE_REGISTRAR" OWNER="$OWNER" URL="$MODAL_ENDPOINT_URL" python3 -c '
import json,os
print(json.dumps({
    "store_registrar": os.environ.get("STORE_REGISTRAR") or os.environ["OWNER"],
    "endpoints": [{"label": "compute", "url": os.environ["URL"], "method": "POST"}],
}))
')"
cd "$SCRIPT_DIR"
DEPLOY="$("$COWBOY" actor deploy modal \
  --manifest-json "$WORK/manifest.json" --salt "$SALT" --init-payload "$INIT_PAYLOAD" \
  --private-key "$KEYFILE" 2>&1 || true)"
echo "$DEPLOY" | grep -qi "deployed successfully" \
  || { printf '%s\n' "$DEPLOY"; die "actor deploy failed"; }
ACTOR="$(printf '%s' "$DEPLOY" \
  | sed -n 's/.*Actor address:[[:space:]]*\(0x[0-9a-fA-F]\{40\}\).*/\1/p' | head -1)"
[ -n "$ACTOR" ] || die "could not parse actor address"
echo "$ACTOR" > "$SCRIPT_DIR/.last_actor"

for _ in $(seq 1 20); do
  curl -s "$RPC_URL/actor/$ACTOR" | grep -q '"secrets.read"' && { ENT=1; break; }
  sleep 2
done
[ "${ENT:-0}" = 1 ] || die "actor entitlements did not appear"

ACTOR_LC="$(printf '%s' "$ACTOR" | tr 'A-F' 'a-f')"

# store/confirm/policy/verify one CBSS secret; called once per token
provision_secret() {
  local key="$1" value_file="$2" set_out set_tx status keyhash ver pol
  set_out="$(CBSS_INGEST_ENDPOINT="$INGEST" \
    "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
    set "$key" --value-file "$value_file" 2>&1)" \
    || { printf '%s\n' "$set_out"; die "secrets set failed for $key"; }
  set_tx="$(printf '%s' "$set_out" | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
  [ -n "$set_tx" ] || die "secrets set produced no transaction hash for $key"
  keyhash=""; ver=""
  for _ in $(seq 1 25); do
    read -r status keyhash ver <<<"$(curl -s --max-time 15 "$RPC_URL/transaction/$set_tx/receipt" 2>/dev/null \
      | python3 -c '
import sys,json
try:r=json.load(sys.stdin)
except Exception:sys.exit()
status=r.get("status","");kh=ver=""
for ev in (r.get("events") or []):
    if isinstance(ev,list) and len(ev)>=3 and ev[1]=="cbss.secret.created":
        raw=ev[2][2:] if ev[2].startswith("0x") else ev[2]
        kh="0x"+raw[40:104];ver=str(int(raw[104:120],16));break
print(status,kh,ver)
' 2>/dev/null || echo "")"
    [ "$status" = "Success" ] && [ -n "$keyhash" ] && break
    sleep 3
  done
  [ "$status" = "Success" ] && [ -n "$keyhash" ] && [ -n "$ver" ] \
    || die "secret transaction did not confirm for $key"
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
    policy --key-hash "$keyhash" --version "$ver" --actor "$ACTOR" >/dev/null \
    || die "secret policy failed for $key"
  for _ in $(seq 1 20); do
    pol="$(curl -s --max-time 15 "$RPC_URL/cbss/secret/$OWNER/$keyhash?version=$ver" 2>/dev/null \
      | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin).get("policy") or {}))' 2>/dev/null || echo '{}')"
    case "$pol" in *"$ACTOR_LC"*) return 0 ;; esac
    sleep 4
  done
  die "secret policy did not attach to the actor for $key"
}

provision_secret "$SECRET_TOKEN_ID" "$WORK/id.txt"
provision_secret "$SECRET_TOKEN_SECRET" "$WORK/secret.txt"

"$COWBOY" transfer --to "$ACTOR" --amount "$ACTOR_FUND_CBY" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "actor funding failed"
sleep 4

SALT="0x$(printf '%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
ROOT="0x$(printf 'manifest%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
VOL="0x$(printf 'volume%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
CAP_HEX="$(printf '%s' \
  '{"handlers":["invoke"],"poll_handler":"get_result","slot":"compute-invoke","returns":"modal-invoke-async-result","hire_activation":{"protocol":"store-admin-v1"}}' \
  | python3 -c 'import sys;print(sys.stdin.read().encode().hex())')"
DECL_OUT="$("$COWBOY" trading-post --private-key "$KEYFILE" declare \
  --actor "$ACTOR" --salt "$SALT" --human-hireable --agent-hireable \
  --manifest-root "$ROOT" --claimed-root "$ROOT" --code-visibility private \
  --ibe-ciphertext-hex deadbeef --capability-schema-hex "$CAP_HEX" \
  --term "$HIRE_TERM" --split "$OWNER:10000" --volume-id "$VOL" 2>&1)" \
  || die "declare failed: $DECL_OUT"
DECL="$(printf '%s' "$DECL_OUT" | grep -oE 'Declaration ID: 0x[0-9a-fA-F]{64}' | head -1 | awk '{print $3}')"
[ -n "$DECL" ] || die "declare produced no declaration id"
sleep 6

HIRE_OUT="$("$COWBOY" trading-post --private-key "$KEYFILE" hire \
  --declaration-id "$DECL" --version 1 --term "$HIRE_TERM" --amount "$HIRE_AMOUNT" 2>&1)" \
  || die "hire failed: $HIRE_OUT"
HIRE_TX="$(printf '%s' "$HIRE_OUT" | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
[ -n "$HIRE_TX" ] || die "hire produced no transaction hash"
sleep 6

HIRE_JSON=""
for _ in $(seq 1 15); do
  HIRE_JSON="$(curl -s --max-time 15 "$RPC_URL/transaction/$HIRE_TX/receipt" 2>/dev/null \
    | python3 -c '
import sys,json,binascii
try:r=json.load(sys.stdin)
except Exception:sys.exit()
for ev in (r.get("events") or []):
    if isinstance(ev,list) and len(ev)>=3 and ev[1]=="TradingPostHireCreated":
        print(binascii.unhexlify(ev[2][2:]).decode("utf-8","replace"));break
' 2>/dev/null || true)"
  [ -n "$HIRE_JSON" ] && break
  sleep 4
done
[ -n "$HIRE_JSON" ] || die "hire event did not appear"
HIRE_ID="$(printf '%s' "$HIRE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hire_id"])')"
ARTIFACT="$(printf '%s' "$HIRE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["artifact_id"])')"
HIRE_VER="$(printf '%s' "$HIRE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["declaration_version"])')"

"$COWBOY" actor execute --actor "$ACTOR" --handler register_hire \
  --payload-json "{\"hire_id\":$HIRE_ID,\"hirer\":\"$OWNER\",\"declaration_id\":\"$DECL\",\"version\":$HIRE_VER,\"artifact_id\":$ARTIFACT}" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "register_hire failed"
sleep 5

info "running signed Canyon invoke"
COWBOY_SDK_PATH="${COWBOY_SDK_PATH:-$REPO/node/pvm/Lib}" python3 -u "$SCRIPT_DIR/tests/test_modal_canyon.py" \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" --cowboy "$COWBOY" --label compute
ok "Modal compute-invoke actor $ACTOR passed the paid Canyon flow"
