#!/usr/bin/env bash
# connect-workspace.sh — HIRER-side onboarding for a Ranchhand hire.
#
# Run AFTER hiring ranchhand (dashboard General Store or
# `cowboy trading-post hire`). Everything here is signed by the HIRER key;
# the creator's only involvement is provisioning the per-hire state volume
# (see RANCHHAND_STATE_VOLUME below).
#
# Steps:
#   1. store both Slack app secrets under YOUR account:
#        RANCHHAND_SLACK_SIGNING_SECRET, RANCHHAND_SLACK_BOT_TOKEN
#   2. deploy YOUR credential-proxy actor (the `relay` target,
#      actors/relay/main.py) whose
#      manifest names exactly those two keys
#   3. ACL both secrets to the proxy address (release policy)
#   4. call ranchhand.connect_workspace{team_id, hire_id, proxy, state_volume}
#
# Required env:
#   HIRER_PRIVATE_KEY          path to hirer's hex private key file
#   RANCHHAND_ACTOR            ranchhand actor address (0x…)
#   RANCHHAND_HIRE_ID          your Trading Post hire id
#   SLACK_TEAM_ID              your workspace id (T…)
#   SLACK_SIGNING_SECRET       from your Slack app config
#   SLACK_BOT_TOKEN            xoxb-… from your Slack app install
#   RANCHHAND_STATE_VOLUME     your per-hire state volume name
#                              (provisioned by the creator; ask or see README)
# Optional env:
#   RANCHHAND_ARTIFACT_ID      pinned artifact of YOUR hire (default: declaration pin)
#   PROXY_SALT                 CREATE2 salt for the proxy (default 0x51ac)
#   SLACK_API_BASE             override for testing (default https://slack.com/api)
#   RANCHHAND_RPC_URL          RPC URL override

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"

PRIVATE_KEY="${HIRER_PRIVATE_KEY:?set HIRER_PRIVATE_KEY}"
RANCHHAND_ACTOR="${RANCHHAND_ACTOR:?set RANCHHAND_ACTOR}"
HIRE_ID="${RANCHHAND_HIRE_ID:?set RANCHHAND_HIRE_ID}"
TEAM_ID="${SLACK_TEAM_ID:?set SLACK_TEAM_ID}"
SIGNING_SECRET="${SLACK_SIGNING_SECRET:?set SLACK_SIGNING_SECRET}"
BOT_TOKEN="${SLACK_BOT_TOKEN:?set SLACK_BOT_TOKEN}"
STATE_VOLUME="${RANCHHAND_STATE_VOLUME:?set RANCHHAND_STATE_VOLUME}"
PROXY_SALT="${PROXY_SALT:-0x51ac}"
API_BASE="${SLACK_API_BASE:-https://slack.com/api}"
RPC_URL="${RANCHHAND_RPC_URL:-}"

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
info "hirer = $HIRER_ADDR ; team = $TEAM_ID ; hire = $HIRE_ID"

# ── 1. store the Slack secrets under the hirer account ───────────────────────
TMP_SEC="$(mktemp)"; trap 'rm -f "$TMP_SEC"' EXIT
printf '%s' "$SIGNING_SECRET" > "$TMP_SEC"
cowboy secrets --private-key "$PRIVATE_KEY" set RANCHHAND_SLACK_SIGNING_SECRET \
    --value-file "$TMP_SEC" "${RPC_FLAG[@]}"
printf '%s' "$BOT_TOKEN" > "$TMP_SEC"
cowboy secrets --private-key "$PRIVATE_KEY" set RANCHHAND_SLACK_BOT_TOKEN \
    --value-file "$TMP_SEC" "${RPC_FLAG[@]}"
ok "secrets stored under $HIRER_ADDR"

# ── 2. deploy the credential proxy ───────────────────────────────────────────
PROXY_ADDR="$(cowboy actor address relay --creator "$HIRER_ADDR" --salt "$PROXY_SALT" \
    | python3 -c 'import re,sys; m=re.findall(r"Actor address:\s*(?:0x)?([0-9a-fA-F]{40})", sys.stdin.read()); print(("0x"+m[-1]) if m else "")')"
[[ -n "$PROXY_ADDR" ]] || fail "could not compute proxy address"
info "proxy = $PROXY_ADDR"

SLACK_DOMAIN="$(python3 -c "from urllib.parse import urlparse; import sys; print(urlparse('$API_BASE').hostname)")"
PROXY_MANIFEST="$(mktemp)"
sed -e "s/HIRER_EOA/${HIRER_ADDR}/g" -e "s/SLACK_API_DOMAIN/${SLACK_DOMAIN}/g" \
    "${SCRIPT_DIR}/proxy/manifest.template.json" > "$PROXY_MANIFEST"

INIT_JSON="$(printf '{"ranchhand":"%s","api_base":"%s"}' "$RANCHHAND_ACTOR" "$API_BASE")"
if curl -sf "${RANCHHAND_RPC_URL:-http://127.0.0.1:4000}/actor/$PROXY_ADDR" >/dev/null 2>&1; then
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
cowboy secrets --private-key "$PRIVATE_KEY" policy RANCHHAND_SLACK_SIGNING_SECRET \
    --allow-actor "$PROXY_ADDR" "${RPC_FLAG[@]}"
cowboy secrets --private-key "$PRIVATE_KEY" policy RANCHHAND_SLACK_BOT_TOKEN \
    --allow-actor "$PROXY_ADDR" "${RPC_FLAG[@]}"
ok "release policies ACL'd to proxy"

# ── 4. bind the workspace on ranchhand ───────────────────────────────────────
CONNECT_JSON="$(python3 - <<PY
import json, os
payload = {
    "team_id": "$TEAM_ID",
    "hire_id": int("$HIRE_ID"),
    "proxy": "$PROXY_ADDR",
    "state_volume": "$STATE_VOLUME",
}
artifact = os.environ.get("RANCHHAND_ARTIFACT_ID", "")
if artifact:
    payload["artifact_id"] = int(artifact)
print(json.dumps(payload))
PY
)"
cowboy actor execute --actor "$RANCHHAND_ACTOR" --handler connect_workspace \
    --payload-json "$CONNECT_JSON" \
    --private-key "$PRIVATE_KEY" "${RPC_FLAG[@]}"
ok "workspace connected"

echo
ok "Done. Point your Slack app's event subscription at the ranchhand gateway"
echo "  request_url: https://<gateway-host>/slack/events"
echo "  proxy actor: $PROXY_ADDR (yours; pause anytime by revoking the secret ACLs)"
