#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../../_demo_output.sh
source "$SCRIPT_DIR/../../_demo_output.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SECRET_KEY="EXA_API_KEY"
ACTOR_FUND_CBY="${ACTOR_FUND_CBY:-50}"
STORE_REGISTRAR="${STORE_REGISTRAR:-}"

info() { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '\033[33m  note:\033[0m %s\n' "$*"; }

COWBOY="${COWBOY:-$REPO/node/target/release/cowboy}"
[ -x "$COWBOY" ] || COWBOY="$(command -v cowboy)" || die "no cowboy CLI found"

RPC_URL="${RPC_URL:-http://localhost:4000}"
export COWBOY_RPC_URL="$RPC_URL"
KEYFILE="${KEYFILE:?set KEYFILE to the demo wallet key}"
[ -f "$KEYFILE" ] || die "no key at $KEYFILE (set KEYFILE)"

INGEST="${CBSS_INGEST_ENDPOINT:-${INGEST:-}}"
[ -n "$INGEST" ] || die "set CBSS_INGEST_ENDPOINT (the env's cbssd ingest URL)"

if [ -n "${EXA_API_KEY_FILE:-}" ]; then
  [ -f "$EXA_API_KEY_FILE" ] || die "EXA_API_KEY_FILE not found: $EXA_API_KEY_FILE"
  cp "$EXA_API_KEY_FILE" "$WORK/key.txt"
elif [ -n "${EXA_API_KEY:-}" ]; then
  printf '%s' "$EXA_API_KEY" > "$WORK/key.txt"
else
  die "set EXA_API_KEY or EXA_API_KEY_FILE (get one at https://exa.ai)"
fi

curl -sf "$RPC_URL/health" >/dev/null 2>&1 || curl -sf "$RPC_URL/status" >/dev/null 2>&1 \
  || die "cannot reach node at $RPC_URL"
ok "node reachable at $RPC_URL"

OWNER="$("$COWBOY" wallet address --key "$KEYFILE" 2>/dev/null \
  | grep -oE '0x[0-9a-fA-F]{40}' | head -1 || true)"
[ -n "$OWNER" ] || die "could not resolve owner address"
ok "owner $OWNER"

if [ -n "$STORE_REGISTRAR" ] && [ "$(printf '%s' "$STORE_REGISTRAR" | tr 'A-F' 'a-f')" != "$(printf '%s' "$OWNER" | tr 'A-F' 'a-f')" ]; then
  die "STORE_REGISTRAR must match the demo wallet owner that signs register_hire"
fi
STORE_REGISTRAR="$OWNER"

BAL="$(curl -s --max-time 15 "$RPC_URL/account/$OWNER" | grep -oE '"balance":[0-9]+' | cut -d: -f2 || true)"
if [ -z "${BAL:-}" ] || [ "${BAL:-0}" -eq 0 ] 2>/dev/null; then
  info "owner unfunded — requesting faucet"
  curl -s --max-time 30 -X POST "$RPC_URL/faucet" -H 'Content-Type: application/json' \
    -d "{\"address\":\"$OWNER\"}" >/dev/null 2>&1 || die "faucet request failed (is it enabled?)"
  for _ in $(seq 1 20); do
    BAL="$(curl -s --max-time 15 "$RPC_URL/account/$OWNER" | grep -oE '"balance":[0-9]+' | cut -d: -f2 || true)"
    [ -n "${BAL:-}" ] && [ "${BAL:-0}" -gt 0 ] 2>/dev/null && break
    sleep 3
  done
fi
[ -n "${BAL:-}" ] && [ "${BAL:-0}" -gt 0 ] 2>/dev/null || die "owner still unfunded"
ok "owner funded ($BAL)"

if ! curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2; then
  info "cowboy secrets request-account-dkg"
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" request-account-dkg >/dev/null \
    || die "request-account-dkg failed"
  for _ in $(seq 1 60); do
    curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2 && break
    sleep 5
  done
fi
curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2 \
  || die "account release key never appeared (is cbssd up?)"
ok "account release key ready"

cat > "$WORK/manifest.json" <<EOF
{ "name": "exa-search", "version": "2.1.0",
  "entitlements": [
    { "id": "http.fetch" },
    { "id": "secrets.read", "params": { "keys": ["$OWNER:$SECRET_KEY"] } },
    { "id": "storage.kv" }
  ] }
EOF

SALT="0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')"
INIT_PAYLOAD="$(STORE_REGISTRAR="$STORE_REGISTRAR" OWNER="$OWNER" python3 -c '
import json,os
print(json.dumps({"store_registrar": os.environ.get("STORE_REGISTRAR") or os.environ["OWNER"]}))
')"
info "cowboy actor deploy exa"
DEPLOY="$(cd "$SCRIPT_DIR" && "$COWBOY" actor deploy exa \
  --manifest-json "$WORK/manifest.json" --salt "$SALT" \
  --cycles-limit "${DEPLOY_CYCLES_LIMIT:-20000000}" \
  --cells-limit "${DEPLOY_CELLS_LIMIT:-40000000}" \
  --init-payload "$INIT_PAYLOAD" \
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
[ "${ENT:-0}" = 1 ] || die "entitlement secrets.read not attached on-chain"
ok "entitled actor $ACTOR deployed"

info "cowboy secrets set $SECRET_KEY"
SECRET_OUT="$(CBSS_INGEST_ENDPOINT="$INGEST" \
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
  set "$SECRET_KEY" --value-file "$WORK/key.txt" 2>&1)" \
  || die "secrets set failed"
SECRET_TX="$(printf '%s' "$SECRET_OUT" | python3 "$SCRIPT_DIR/tools/receipt_request_id.py" tx-hash)" \
  || die "secrets set did not return one transaction hash"
demo_wait_for_transaction_receipt "$RPC_URL" "$SECRET_TX" "secrets set" \
  || die "secret transaction failed: $DEMO_TX_RECEIPT_JSON"
SECRET_VERSION="$(printf '%s' "$DEMO_TX_RECEIPT_JSON" \
  | python3 "$SCRIPT_DIR/tools/receipt_request_id.py" secret-version "$OWNER")" \
  || die "secret receipt did not contain one version for $OWNER"
read -r KEYHASH VER <<< "$SECRET_VERSION"
ok "secret '$SECRET_KEY' stored at version $VER"

info "cowboy secrets policy --key-hash $KEYHASH --version $VER --actor $ACTOR"
"$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
  policy --key-hash "$KEYHASH" --version "$VER" --actor "$ACTOR" 2>&1 \
  | grep -iE 'tx hash|error' || true
ACTOR_LC="$(printf '%s' "$ACTOR" | tr 'A-F' 'a-f')"
for _ in $(seq 1 20); do
  POL="$(curl -s --max-time 15 "$RPC_URL/cbss/secret/$OWNER/$KEYHASH?version=$VER" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("policy") or {}))' 2>/dev/null || echo '{}')"
  case "$POL" in *"$ACTOR_LC"*) ACL_OK=1; break ;; esac
  sleep 4
done
[ "${ACL_OK:-0}" = 1 ] \
  || die "release policy never attached to v$VER for $ACTOR (last seen: $POL) — the actor cannot read the secret"

ok "release policy set + verified on v$VER -> $ACTOR"

info "funding actor with $ACTOR_FUND_CBY CBY"
"$COWBOY" transfer --to "$ACTOR" --amount "$ACTOR_FUND_CBY" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "actor funding failed"
sleep 4
ok "actor funded"

ok "actor $ACTOR is provisioned"

info "trading-post declare"
note "declared PRIVATE with a placeholder escrow ciphertext. Public code cannot"
note "be declared through the CLI at all: it requires a non-empty --ibe-ciphertext"
note "while the chain requires an EMPTY one for public visibility (COW-3504)."
SALT="0x$(printf '%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
ROOT="0x$(printf 'manifest%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
VOL="0x$(printf 'volume%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
CAP_HEX="$(printf '%s' \
  '{"handlers":["search","api"],"slot":"search","hire_activation":{"protocol":"store-admin-v1"}}' \
  | python3 -c 'import sys;print(sys.stdin.read().encode().hex())')"

DECL_OUT="$("$COWBOY" trading-post --private-key "$KEYFILE" declare \
  --actor "$ACTOR" --salt "$SALT" \
  --human-hireable --agent-hireable \
  --manifest-root "$ROOT" --claimed-root "$ROOT" \
  --code-visibility private --ibe-ciphertext-hex deadbeef \
  --capability-schema-hex "$CAP_HEX" \
  --term free --split "$OWNER:10000" \
  --volume-id "$VOL" 2>&1)" || die "declare failed: $DECL_OUT"
DECL="$(printf '%s' "$DECL_OUT" | grep -oE 'Declaration ID: 0x[0-9a-fA-F]{64}' | head -1 | awk '{print $3}')"
[ -n "$DECL" ] || die "declare produced no declaration id: $DECL_OUT"
sleep 6
"$COWBOY" trading-post get-declaration --declaration-id "$DECL" >/dev/null 2>&1 \
  || die "declaration $DECL is not readable on chain"
ok "declared $DECL"

info "trading-post hire (free)"
HIRE_OUT="$("$COWBOY" trading-post --private-key "$KEYFILE" hire \
  --declaration-id "$DECL" --version 1 --term free --amount 0 2>&1)" \
  || die "hire failed: $HIRE_OUT"
HIRE_TX="$(printf '%s' "$HIRE_OUT" | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
[ -n "$HIRE_TX" ] || die "hire produced no tx hash: $HIRE_OUT"
sleep 6

HIRE_JSON=""
for _ in $(seq 1 15); do
  HIRE_JSON="$(curl -s --max-time 15 "$RPC_URL/transaction/$HIRE_TX/receipt" 2>/dev/null \
    | python3 -c '
import sys,json,binascii
try: r=json.load(sys.stdin)
except Exception: sys.exit()
for ev in (r.get("events") or []):
    if isinstance(ev,list) and len(ev)>=3 and ev[1]=="TradingPostHireCreated":
        print(binascii.unhexlify(ev[2][2:]).decode("utf-8","replace")); break
' 2>/dev/null || true)"
  [ -n "$HIRE_JSON" ] && break
  sleep 4
done
[ -n "$HIRE_JSON" ] || die "no TradingPostHireCreated event in $HIRE_TX — the hire did not land"

HIRE_ID="$(printf '%s' "$HIRE_JSON"  | python3 -c 'import sys,json;print(json.load(sys.stdin)["hire_id"])')"
ARTIFACT="$(printf '%s' "$HIRE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["artifact_id"])')"
HIRE_VER="$(printf '%s' "$HIRE_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["declaration_version"])')"
ok "chain issued hire_id $HIRE_ID (version $HIRE_VER, artifact $ARTIFACT)"

info "register_hire on the actor"
note "the DEMO is doing Store Admin's job here. In production the Store operator"
note "key calls register_hire after verifying the finalized Hire against the live"
note "listing (cowboyinc/store-admin#9). The actor cannot trust a caller's claim"
note "to hold a Hire, which is why this step exists at all."
"$COWBOY" actor execute --actor "$ACTOR" --handler register_hire \
  --payload-json "{\"hire_id\":$HIRE_ID,\"hirer\":\"$OWNER\",\"declaration_id\":\"$DECL\",\"version\":$HIRE_VER,\"artifact_id\":$ARTIFACT}" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "register_hire failed"
sleep 5
ok "hire $HIRE_ID registered"

QUERY="${DEMO_QUERY:-best pour over coffee brewing method}"
info "search: \"$QUERY\""
SEARCH_OUT="$("$COWBOY" actor execute --actor "$ACTOR" --handler search \
  --payload-json "$(python3 -c 'import json,sys;print(json.dumps({"hire_id":int(sys.argv[1]),"query":sys.argv[2]}))' "$HIRE_ID" "$QUERY")" \
  --private-key "$KEYFILE" --no-wait 2>&1)" || die "search transaction failed: $SEARCH_OUT"
SEARCH_TX="$(printf '%s' "$SEARCH_OUT" | python3 "$SCRIPT_DIR/tools/receipt_request_id.py" tx-hash 2>/dev/null)" \
  || die "search output had no unique transaction hash: $SEARCH_OUT"

demo_wait_for_transaction_receipt "$RPC_URL" "$SEARCH_TX" "search" \
  || die "search did not produce a successful receipt: $DEMO_TX_RECEIPT_JSON"
SEARCH_RECEIPT="$DEMO_TX_RECEIPT_JSON"
REQUEST_ID="$(printf '%s' "$SEARCH_RECEIPT" | python3 "$SCRIPT_DIR/tools/receipt_request_id.py" request-id "$ACTOR" 2>/dev/null)" \
  || die "search receipt had no unique wrapper.search_accepted event from $ACTOR: $SEARCH_RECEIPT"
ok "request $REQUEST_ID accepted"

RESULT_JSON="$(python3 "$SCRIPT_DIR/tools/exa_client.py" --cowboy "$COWBOY" \
  --rpc "$RPC_URL" --actor "$ACTOR" --hire-id "$HIRE_ID" \
  --wallet-key "$KEYFILE" --request-id "$REQUEST_ID")" \
  || die "result delivery failed; retain request $REQUEST_ID and do not resubmit it"
STATUS="$(printf '%s' "$RESULT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
printf '%s' "$RESULT_JSON" | python3 -c '
import json,sys
r=json.load(sys.stdin)
print("status:", r.get("status", "error"))
if r.get("query"): print("query:", r["query"])
for i,item in enumerate(r.get("results", []), 1):
    print("%d. %s" % (i, item.get("title", "")))
    print("   %s" % item.get("url", ""))
if r.get("error"): print("error:", r["error"])
'
[ "$STATUS" = ok ] \
  || die "request $REQUEST_ID did not complete successfully; do not submit a replacement while it is pending"

echo
ok "gated request $REQUEST_ID returned live results through get_result for hire $HIRE_ID"
echo
echo "  actor:       $ACTOR"
echo "  declaration: $DECL"
echo "  hire:        $HIRE_ID"
echo "  request:     $REQUEST_ID"
echo "  re-run a query without redeploying:"
echo "    $COWBOY actor execute --actor $ACTOR --handler search \\"
echo "      --payload-json '{\"hire_id\":$HIRE_ID,\"query\":\"...\"}' --private-key \$KEYFILE"
echo "  operator-only transparent-storage diagnostic:"
echo "    BUYER=$OWNER $SCRIPT_DIR/show_results.sh $ACTOR $REQUEST_ID $RPC_URL"
echo "  inspect: $COWBOY inspect --address $ACTOR --rpc-url $RPC_URL"
echo "  events:  curl -s $RPC_URL/actor/$ACTOR/events | tail"
