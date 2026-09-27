#!/usr/bin/env bash
# Requires a live env with runners AND cbss proxies; `cowboy dev` cannot run it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SECRET_KEY="ALPHAVANTAGE_API_KEY"
ACTOR_FUND_CBY="${ACTOR_FUND_CBY:-50}"
STORE_REGISTRAR="${STORE_REGISTRAR:-}"

info() { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

COWBOY="${COWBOY:-$REPO/node/target/release/cowboy}"
[ -x "$COWBOY" ] || COWBOY="$(command -v cowboy)" || die "no cowboy CLI found"

RPC_URL="${RPC_URL:-http://localhost:4000}"
export COWBOY_RPC_URL="$RPC_URL"
KEYFILE="${KEYFILE:-$HOME/.cowboy/cal-key}"
[ -f "$KEYFILE" ] || die "no key at $KEYFILE (set KEYFILE)"

INGEST="${CBSS_INGEST_ENDPOINT:-${INGEST:-}}"
[ -n "$INGEST" ] || die "set CBSS_INGEST_ENDPOINT (the env's cbssd ingest URL)"

# Keep the vendor key in a file rather than command arguments.
[ -n "${ALPHAVANTAGE_API_KEY_FILE:-}" ] \
  || die "set ALPHAVANTAGE_API_KEY_FILE to an Alpha Vantage key file"
[ -f "$ALPHAVANTAGE_API_KEY_FILE" ] \
  || die "ALPHAVANTAGE_API_KEY_FILE not found: $ALPHAVANTAGE_API_KEY_FILE"
cp "$ALPHAVANTAGE_API_KEY_FILE" "$WORK/key.txt"

curl -sf "$RPC_URL/health" >/dev/null 2>&1 || curl -sf "$RPC_URL/status" >/dev/null 2>&1 \
  || die "cannot reach node at $RPC_URL"
ok "node reachable at $RPC_URL"

OWNER="$("$COWBOY" wallet address --key "$KEYFILE" 2>/dev/null \
  | grep -oE '0x[0-9a-fA-F]{40}' | head -1 || true)"
[ -n "$OWNER" ] || die "could not resolve owner address"
if [ -n "$STORE_REGISTRAR" ] && \
   [ "$(printf '%s' "$STORE_REGISTRAR" | tr 'A-F' 'a-f')" != "$(printf '%s' "$OWNER" | tr 'A-F' 'a-f')" ]; then
  die "STORE_REGISTRAR must match the deployer wallet; this demo has no separate registrar key"
fi
STORE_REGISTRAR="$OWNER"
ok "owner $OWNER"

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
{ "name": "alphavantage", "version": "1.0.0",
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
info "cowboy actor deploy alphavantage"
DEPLOY="$(cd "$SCRIPT_DIR" && "$COWBOY" actor deploy alphavantage \
  --manifest-json "$WORK/manifest.json" --salt "$SALT" \
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
SET_OUT="$(CBSS_INGEST_ENDPOINT="$INGEST" \
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
  set "$SECRET_KEY" --value-file "$WORK/key.txt" 2>&1)" \
  || { printf '%s\n' "$SET_OUT"; die "secrets set failed"; }
SET_TX="$(printf '%s' "$SET_OUT" | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
[ -n "$SET_TX" ] || { printf '%s\n' "$SET_OUT"; die "secrets set produced no tx hash"; }

KEYHASH=""; VER=""
for _ in $(seq 1 25); do
  read -r STATUS KEYHASH VER <<<"$(curl -s --max-time 15 "$RPC_URL/transaction/$SET_TX/receipt" 2>/dev/null \
    | python3 -c '
import sys,json
try: r=json.load(sys.stdin)
except Exception: sys.exit()
status=r.get("status","")
kh=ver=""
for ev in (r.get("events") or []):
    if isinstance(ev,list) and len(ev)>=3 and ev[1]=="cbss.secret.created":
        raw=ev[2][2:] if ev[2].startswith("0x") else ev[2]
        kh="0x"+raw[40:104]; ver=str(int(raw[104:120],16)); break
print(status, kh, ver)
' 2>/dev/null || echo "")"
  [ "$STATUS" = "Success" ] && [ -n "$KEYHASH" ] && break
  case "$STATUS" in Failed|Reverted|Error) die "secrets set rejected at execution ($STATUS) — read: $COWBOY transaction get --hash $SET_TX" ;; esac
  sleep 3
done
[ "$STATUS" = "Success" ] && [ -n "$KEYHASH" ] && [ -n "$VER" ] \
  || die "secrets set receipt never confirmed a cbss.secret.created event (tx $SET_TX)"
ok "secret '$SECRET_KEY' stored in CBSS (version $VER, key_hash ${KEYHASH:0:18}…)"

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
ok "actor $ACTOR is provisioned"

info "trading-post declare"
SALT="0x$(printf '%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
ROOT="0x$(printf 'manifest%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
VOL="0x$(printf 'volume%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
CAP_HEX="$(printf '%s' \
  '{"handlers":["quote","daily","overview"],"poll_handler":"get_result","slot":"market-data","symbol_input":{"hire_id":"int","symbol":"str"},"returns":"alphavantage-async-result","result_byte_budget":38000,"max_result_chunk_bytes":3900,"hire_activation":{"protocol":"store-admin-v1"}}' \
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
"$COWBOY" actor execute --actor "$ACTOR" --handler register_hire \
  --payload-json "{\"hire_id\":$HIRE_ID,\"hirer\":\"$OWNER\",\"declaration_id\":\"$DECL\",\"version\":$HIRE_VER,\"artifact_id\":$ARTIFACT}" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "register_hire failed"
sleep 5
ok "hire $HIRE_ID registered"

# A successful overview proves URL-position secret substitution.
SYMBOL="$(printf '%s' "${DEMO_SYMBOL:-IBM}" | tr 'a-z' 'A-Z')"
CODEC_PY="${COWBOY_SDK_CODEC:-${COWBOY_SDK_PATH:-$REPO/node/pvm/Lib}/cowboy_sdk/codec.py}"
[ -f "$CODEC_PY" ] || die "SDK codec not found at $CODEC_PY (set COWBOY_SDK_CODEC to a cowboy_sdk/codec.py)"

read_request() {
  curl -s --max-time 15 "$RPC_URL/actor/$ACTOR" 2>/dev/null | \
    REQUEST_KEY="request:$1" CODEC_PY="$CODEC_PY" python3 -c '
import importlib.util,json,os,sys
try:
    state=json.load(sys.stdin)
    raw=state.get("storage",{}).get("0x"+os.environ["REQUEST_KEY"].encode().hex())
    if not raw: raise SystemExit()
    spec=importlib.util.spec_from_file_location("alphavantage_codec",os.environ["CODEC_PY"])
    codec=importlib.util.module_from_spec(spec); spec.loader.exec_module(codec)
    value=codec.decode(bytes.fromhex(raw[2:] if raw.startswith("0x") else raw))
    print(json.dumps(value,separators=(",",":")))
except Exception:
    pass
' 2>/dev/null || true
}

info "overview: $SYMBOL"
SUBMIT_OUT="$("$COWBOY" actor execute --actor "$ACTOR" --handler overview \
  --payload-json "{\"hire_id\":$HIRE_ID,\"symbol\":\"$SYMBOL\"}" \
  --private-key "$KEYFILE" --receipt-timeout-seconds 120 2>&1)" \
  || die "overview transaction failed: $SUBMIT_OUT"
# CLI prints the receipt inline (no outer tx hash); parse request_accepted from it.
REQUEST_ID="$(printf '%s' "$SUBMIT_OUT" | CODEC_PY="$CODEC_PY" python3 -c '
import importlib.util,os,re,sys
out=sys.stdin.read()
if not re.search(r"Status:\s*(?:String\()?\"?Success", out): raise SystemExit()
spec=importlib.util.spec_from_file_location("av_codec",os.environ["CODEC_PY"])
codec=importlib.util.module_from_spec(spec); spec.loader.exec_module(codec)
for topic,data in re.findall(r"^\s*-\s+([\w.]+):\s*(0x[0-9a-fA-F]+)\s*$", out, re.MULTILINE):
    if topic=="wrapper.request_accepted":
        acc=codec.decode(bytes.fromhex(data[2:]))
        if acc.get("operation")=="overview" and acc.get("status")=="pending":
            print(acc["request_id"]); break
' 2>/dev/null || true)"
[ -n "$REQUEST_ID" ] \
  || die "overview submit did not emit wrapper.request_accepted: $(printf '%s' "$SUBMIT_OUT" | tail -4)"
ok "request $REQUEST_ID accepted"

REQUEST_JSON=""
for _ in $(seq 1 60); do
  REQUEST_JSON="$(read_request "$REQUEST_ID")"
  REQUEST_STATUS="$(printf '%s' "$REQUEST_JSON" | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("status", ""))' 2>/dev/null || true)"
  case "$REQUEST_STATUS" in ok|error) break ;; esac
  sleep 4
done
[ -n "$REQUEST_JSON" ] || die "request:$REQUEST_ID never appeared in actor storage"

SIGNED_JSON="$(COWBOY_SDK_PATH="${COWBOY_SDK_PATH:-$REPO/node/pvm/Lib}" python3 -u \
  "$SCRIPT_DIR/tools/check_alphavantage_canyon.py" \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --request-id "$REQUEST_ID" \
  --rpc "$RPC_URL" --wallet-key "$KEYFILE" --cowboy "$COWBOY")" \
  || die "signed get_result failed for request $REQUEST_ID"

printf '%s' "$SIGNED_JSON" | EXPECT_SYMBOL="$SYMBOL" EXPECT_HIRE="$HIRE_ID" EXPECT_REQUEST="$REQUEST_ID" python3 -c '
import json,os,sys
record=json.load(sys.stdin)
if record.get("request_id") != int(os.environ["EXPECT_REQUEST"]): raise SystemExit("wrong request record")
if record.get("hire_id") != int(os.environ["EXPECT_HIRE"]): raise SystemExit("wrong Hire in request record")
if record.get("operation") != "overview": raise SystemExit("wrong operation in request record")
if record.get("status") != "ok": raise SystemExit("overview failed — URL ${...} substitution not applied? "+str(record.get("output",{}).get("error")))
output=record.get("output") or {}
if output.get("ok") is not True: raise SystemExit("masked or malformed success")
data=output.get("data") or {}
if data.get("Symbol") != os.environ["EXPECT_SYMBOL"]: raise SystemExit("vendor result is for the wrong symbol")
print("    URL-substituted key worked: overview for "+data["Symbol"]+" ("+str(data.get("Name"))+")")
' || die "signed result for request $REQUEST_ID did not contain the expected successful result"

echo
ok "paid, gated overview returned live fundamentals through hire $HIRE_ID — URL \${...} substitution confirmed"
echo
echo "  actor:       $ACTOR"
echo "  declaration: $DECL"
echo "  hire:        $HIRE_ID"
echo "  other handlers: quote, daily"
echo "    $COWBOY actor execute --actor $ACTOR --handler overview \\"
echo "      --payload-json '{\"hire_id\":$HIRE_ID,\"symbol\":\"NVDA\"}' --private-key \$KEYFILE"
