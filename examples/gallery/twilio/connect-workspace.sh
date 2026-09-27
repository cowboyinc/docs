#!/usr/bin/env bash
# connect-workspace.sh — HIRER-side onboarding for a Twilio hire.
#
# Run AFTER hiring the twilio actor (dashboard General Store or
# `cowboy trading-post hire`). Everything here is signed by the HIRER key;
# the creator's only involvement is provisioning the per-hire state volume
# (see TWILIO_STATE_VOLUME below).
#
# Steps:
#   1. store both Twilio secrets under YOUR account (Option B two-key split):
#        TWILIO_AUTH_TOKEN  — signs inbound X-Twilio-Signature (verify-only)
#        TWILIO_API_BASIC   — base64("SKsid:secret") HTTP Basic blob (outbound)
#   2. deploy YOUR credential-proxy actor (the `relay` target,
#      actors/relay/main.py) whose
#      manifest names exactly those two keys
#   3. ACL both secrets to the proxy address (release policy)
#   4. call twilio.connect_number{twilio_number, hire_id, proxy, state_volume}
#
# Required env:
#   HIRER_PRIVATE_KEY          path to hirer's hex private key file
#   TWILIO_ACTOR               twilio actor address (0x…)
#   TWILIO_HIRE_ID             your Trading Post hire id
#   TWILIO_NUMBER              your Twilio phone number in E.164 (+1…)
#   TWILIO_ACCOUNT_SID         your Twilio Account SID (AC…)
#   TWILIO_AUTH_TOKEN_VALUE    your Twilio Auth Token (from the console)
#   TWILIO_API_KEY_SID         a scoped Standard API Key SID (SK…)
#   TWILIO_API_KEY_SECRET      that API Key's secret
#   TWILIO_STATE_VOLUME        your per-hire state volume name
#                              (provisioned by the creator; ask or see README)
# Optional env:
#   TWILIO_ARTIFACT_ID         pinned artifact of YOUR hire (default: declaration pin)
#   PROXY_SALT                 CREATE2 salt for the proxy (default 0x51ac)
#   TWILIO_API_BASE            override for testing (default https://api.twilio.com)
#   TWILIO_RPC_URL             RPC URL override

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"

PRIVATE_KEY="${HIRER_PRIVATE_KEY:?set HIRER_PRIVATE_KEY}"
TWILIO_ACTOR="${TWILIO_ACTOR:?set TWILIO_ACTOR}"
HIRE_ID="${TWILIO_HIRE_ID:?set TWILIO_HIRE_ID}"
NUMBER="${TWILIO_NUMBER:?set TWILIO_NUMBER}"
ACCOUNT_SID="${TWILIO_ACCOUNT_SID:?set TWILIO_ACCOUNT_SID}"
AUTH_TOKEN="${TWILIO_AUTH_TOKEN_VALUE:?set TWILIO_AUTH_TOKEN_VALUE}"
API_KEY_SID="${TWILIO_API_KEY_SID:?set TWILIO_API_KEY_SID}"
API_KEY_SECRET="${TWILIO_API_KEY_SECRET:?set TWILIO_API_KEY_SECRET}"
STATE_VOLUME="${TWILIO_STATE_VOLUME:?set TWILIO_STATE_VOLUME}"
PROXY_SALT="${PROXY_SALT:-0x51ac}"
API_BASE="${TWILIO_API_BASE:-https://api.twilio.com}"
RPC_URL="${TWILIO_RPC_URL:-}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

command -v cowboy >/dev/null 2>&1 || fail "cowboy CLI not on PATH"
[[ -f "$PRIVATE_KEY" ]] || fail "key file not found: $PRIVATE_KEY"
RPC_FLAG=()
if [[ -n "$RPC_URL" ]]; then
    RPC_FLAG=(--indexer-url "$RPC_URL")
    export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
fi

HIRER_ADDR="$(cowboy wallet address --key "$PRIVATE_KEY")"
info "hirer = $HIRER_ADDR ; number = $NUMBER ; hire = $HIRE_ID"

# ── 1. store the Twilio secrets under the hirer account ──────────────────────
# TWILIO_API_BASIC is the base64 of "<API Key SID>:<API Key secret>", i.e. the
# exact HTTP Basic credential the proxy substitutes into the outbound send.
API_BASIC="$(printf '%s:%s' "$API_KEY_SID" "$API_KEY_SECRET" | base64 | tr -d '\n')"
TMP_SEC="$(mktemp)"; trap 'rm -f "$TMP_SEC"' EXIT
printf '%s' "$AUTH_TOKEN" > "$TMP_SEC"
cowboy secrets --private-key "$PRIVATE_KEY" set TWILIO_AUTH_TOKEN \
    --value-file "$TMP_SEC" "${RPC_FLAG[@]}"
printf '%s' "$API_BASIC" > "$TMP_SEC"
cowboy secrets --private-key "$PRIVATE_KEY" set TWILIO_API_BASIC \
    --value-file "$TMP_SEC" "${RPC_FLAG[@]}"
ok "secrets stored under $HIRER_ADDR"

# ── 2. deploy the credential proxy ───────────────────────────────────────────
PROXY_ADDR="$(cowboy actor address relay --creator "$HIRER_ADDR" --salt "$PROXY_SALT" \
    | python3 -c 'import re,sys; m=re.findall(r"Actor address:\s*(?:0x)?([0-9a-fA-F]{40})", sys.stdin.read()); print(("0x"+m[-1]) if m else "")')"
[[ -n "$PROXY_ADDR" ]] || fail "could not compute proxy address"
info "proxy = $PROXY_ADDR"

TWILIO_DOMAIN="$(python3 -c "from urllib.parse import urlparse; print(urlparse('$API_BASE').hostname)")"
PROXY_MANIFEST="$(mktemp)"
sed -e "s/HIRER_EOA/${HIRER_ADDR}/g" -e "s/TWILIO_API_DOMAIN/${TWILIO_DOMAIN}/g" \
    "${SCRIPT_DIR}/proxy/manifest.template.json" > "$PROXY_MANIFEST"

INIT_JSON="$(printf '{"twilio":"%s","account_sid":"%s","api_base":"%s"}' \
    "$TWILIO_ACTOR" "$ACCOUNT_SID" "$API_BASE")"
if curl -sf "${TWILIO_RPC_URL:-http://127.0.0.1:4000}/actor/$PROXY_ADDR" >/dev/null 2>&1; then
    ok "proxy already deployed"
else
    cowboy actor deploy relay \
        --manifest-json "$PROXY_MANIFEST" \
        --init-payload "$INIT_JSON" \
        --private-key "$PRIVATE_KEY" \
        --salt "$PROXY_SALT" "${RPC_FLAG[@]}"
    ok "proxy deployed"
fi
rm -f "$PROXY_MANIFEST"

# ── 3. ACL the secrets to the proxy ──────────────────────────────────────────
cowboy secrets --private-key "$PRIVATE_KEY" policy TWILIO_AUTH_TOKEN \
    --allow-actor "$PROXY_ADDR" "${RPC_FLAG[@]}"
cowboy secrets --private-key "$PRIVATE_KEY" policy TWILIO_API_BASIC \
    --allow-actor "$PROXY_ADDR" "${RPC_FLAG[@]}"
ok "release policies ACL'd to proxy"

# ── 4. bind the number on twilio ─────────────────────────────────────────────
CONNECT_JSON="$(TWILIO_NUMBER="$NUMBER" HIRE_ID="$HIRE_ID" PROXY_ADDR="$PROXY_ADDR" \
    STATE_VOLUME="$STATE_VOLUME" python3 - <<'PY'
import json, os
payload = {
    "twilio_number": os.environ["TWILIO_NUMBER"],
    "hire_id": int(os.environ["HIRE_ID"]),
    "proxy": os.environ["PROXY_ADDR"],
    "state_volume": os.environ["STATE_VOLUME"],
}
artifact = os.environ.get("TWILIO_ARTIFACT_ID", "")
if artifact:
    payload["artifact_id"] = int(artifact)
print(json.dumps(payload))
PY
)"
cowboy actor execute --actor "$TWILIO_ACTOR" --handler connect_number \
    --payload-json "$CONNECT_JSON" \
    --private-key "$PRIVATE_KEY" "${RPC_FLAG[@]}"
ok "number connected"

echo
ok "Done. Set your Twilio number's 'A message comes in' webhook to the twilio gateway"
echo "  request_url: https://<gateway-host>/twilio/sms  (must match the actor's webhook_url)"
echo "  proxy actor: $PROXY_ADDR (yours; pause anytime by revoking the secret ACLs)"
