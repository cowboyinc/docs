#!/usr/bin/env bash
#
# Firecrawl Store wrapper demo:
#   account DKG -> store AGENTMAIL_API_KEY in CBSS -> deploy + fund the actor
#   -> declare on Trading Post -> hire -> register_hire -> gated create_inbox + send + receive.
#
# The vendor key stays in CBSS: it is released to the runner just-in-time and
# substituted into the Authorization header, never in source, chain state or logs.
# Requires a live env with runners AND cbss proxies; `cowboy dev` cannot run it.
#
# Usage:
#   STORE_REGISTRAR=0x... AGENTMAIL_API_KEY_FILE=/path/to/key RPC_URL=... \
#     CBSS_INGEST_ENDPOINT=http://<cbssd>:<port> ./demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SECRET_KEY="AGENTMAIL_API_KEY"
ACTOR_FUND_CBY="${ACTOR_FUND_CBY:-50}"
STORE_REGISTRAR="${STORE_REGISTRAR:-}"

info() { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# A stale `cowboy` on PATH breaks `secrets` transactions — prefer the built one.
COWBOY="${COWBOY:-$REPO/node/target/release/cowboy}"
[ -x "$COWBOY" ] || COWBOY="$(command -v cowboy)" || die "no cowboy CLI found"

RPC_URL="${RPC_URL:-http://localhost:4000}"
# `actor deploy`/`actor execute` take no --rpc-url; COWBOY_RPC_URL is the only
# way to point them at a remote env (else they hit localhost).
export COWBOY_RPC_URL="$RPC_URL"
KEYFILE="${KEYFILE:-$HOME/.cowboy/cal-key}"
[ -f "$KEYFILE" ] || die "no key at $KEYFILE (set KEYFILE)"

INGEST="${CBSS_INGEST_ENDPOINT:-${INGEST:-}}"
[ -n "$INGEST" ] || die "set CBSS_INGEST_ENDPOINT (the env's cbssd ingest URL)"

# --- 0. the vendor key -----------------------------------------------------
# Never pass the key inline: the CLI refuses it from a tty, and it would land
# in shell history and /proc/<pid>/cmdline either way.
if [ -n "${AGENTMAIL_API_KEY_FILE:-}" ]; then
  [ -f "$AGENTMAIL_API_KEY_FILE" ] || die "AGENTMAIL_API_KEY_FILE not found: $AGENTMAIL_API_KEY_FILE"
  cp "$AGENTMAIL_API_KEY_FILE" "$WORK/key.txt"
elif [ -n "${AGENTMAIL_API_KEY:-}" ]; then
  printf '%s' "$AGENTMAIL_API_KEY" > "$WORK/key.txt"
else
  die "set AGENTMAIL_API_KEY or AGENTMAIL_API_KEY_FILE (get one at https://console.agentmail.to (free, no card))"
fi

curl -sf "$RPC_URL/health" >/dev/null 2>&1 || curl -sf "$RPC_URL/status" >/dev/null 2>&1 \
  || die "cannot reach node at $RPC_URL"
ok "node reachable at $RPC_URL"

# `wallet address` takes --key, not --private-key (unlike every other subcommand).
OWNER="$("$COWBOY" wallet address --key "$KEYFILE" 2>/dev/null \
  | grep -oE '0x[0-9a-fA-F]{40}' | head -1 || true)"
[ -n "$OWNER" ] || die "could not resolve owner address"
[ -z "$STORE_REGISTRAR" ] || [ "$(printf %s "$STORE_REGISTRAR" | tr "[:upper:]" "[:lower:]")" = "$(printf %s "$OWNER" | tr "[:upper:]" "[:lower:]")" ] \
  || die "STORE_REGISTRAR must equal the demo wallet owner"
ok "owner $OWNER"

# --- 0b. fund the owner ----------------------------------------------------
# A fresh genesis has no account for this key, and `request-account-dkg` is a
# paid transaction — on an unfunded owner it fails and the DKG simply never
# appears, which reads as "cbssd is down" rather than "you have no money".
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

# --- 1. account DKG (the release key the runner decrypts against) ----------
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

# --- 2. deploy the entitled actor -----------------------------------------
# Deploy BEFORE planting the secret: the release policy (step 3b) must name the
# actor address, which does not exist until now. Entitlement ids MUST be sorted
# lexicographically (http.fetch < secrets.read < storage.kv).
cat > "$WORK/manifest.json" <<EOF
{ "name": "agentmail-inbox", "version": "2.0.0",
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
info "cowboy actor deploy inbox"
# Deploy with atomic init so ownership and the Store registrar are established.
DEPLOY="$("$COWBOY" actor deploy inbox \
  --manifest-json "$WORK/manifest.json" --salt "$SALT" \
  --init-payload "$INIT_PAYLOAD" \
  --private-key "$KEYFILE" 2>&1 || true)"
echo "$DEPLOY" | grep -qi "deployed successfully" \
  || { printf '%s\n' "$DEPLOY"; die "actor deploy failed"; }
# Match the LABELLED "Actor address:" — the output prints "Deployer address:"
# first, so a bare grep for the first 0x…40 yields the owner instead.
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

# --- 3. plant the vendor key in CBSS ---------------------------------------
# `secrets set` mints a NEW version each call and exiting 0 only means the tx
# reached the mempool (a stale CLI is rejected at execution with E1213). Verify
# by the tx RECEIPT, not the exit code — and by THIS tx's `cbss.secret.created`
# event, not a positional read of the secrets list: an owner holds many secrets
# and the first one listed is usually not this key.
info "cowboy secrets set $SECRET_KEY"
SET_OUT="$(CBSS_INGEST_ENDPOINT="$INGEST" \
  "$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
  set "$SECRET_KEY" --value-file "$WORK/key.txt" 2>&1)" \
  || { printf '%s\n' "$SET_OUT"; die "secrets set failed"; }
SET_TX="$(printf '%s' "$SET_OUT" | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
[ -n "$SET_TX" ] || { printf '%s\n' "$SET_OUT"; die "secrets set produced no tx hash"; }

# The `cbss.secret.created` event payload is account(20) ‖ key_hash(32) ‖
# version_be(8). Parse both from it, keyed off THIS tx — never a positional
# read of the secrets list.
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
  # A non-Success status is terminal; do not keep polling a rejected tx.
  case "$STATUS" in Failed|Reverted|Error) die "secrets set rejected at execution ($STATUS) — read: $COWBOY transaction get --hash $SET_TX" ;; esac
  sleep 3
done
[ "$STATUS" = "Success" ] && [ -n "$KEYHASH" ] && [ -n "$VER" ] \
  || die "secrets set receipt never confirmed a cbss.secret.created event (tx $SET_TX)"

ok "secret '$SECRET_KEY' stored in CBSS (version $VER, key_hash ${KEYHASH:0:18}…)"

# --- 3b. release policy — allowlist the actor on the secret version --------
# This is what actually makes CBSS release TO the actor; the manifest
# entitlement only lets it ASK. Without it the runner reports `secret committee
# unavailable`. Bind the exact (key_hash, version) from the set receipt above.
info "cowboy secrets policy --key-hash $KEYHASH --version $VER --actor $ACTOR"
"$COWBOY" secrets --private-key "$KEYFILE" --rpc-url "$RPC_URL" \
  policy --key-hash "$KEYHASH" --version "$VER" --actor "$ACTOR" 2>&1 \
  | grep -iE 'tx hash|error' || true
# Poll until the on-chain ACL names THIS actor (a fixed sleep races the tx).
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

# --- 4. fund it (it pays for its own runner jobs) --------------------------
info "funding actor with $ACTOR_FUND_CBY CBY"
"$COWBOY" transfer --to "$ACTOR" --amount "$ACTOR_FUND_CBY" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "actor funding failed"
sleep 4
ok "actor funded"

ok "actor $ACTOR is provisioned"

# --- 5. declare it on Trading Post -----------------------------------------
# The node resolves the gating hire from Trading Post storage, so a real
# declaration + hire is required (register_hire alone is not enough). Declared
# `private` because the CLI cannot declare public code (COW-3504: it demands a
# non-empty ibe-ciphertext while the chain demands an empty one for public).
# `--private-key` is a GLOBAL flag on `trading-post`, before the subcommand.
info "trading-post declare"
SALT="0x$(printf '%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
ROOT="0x$(printf 'manifest%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
VOL="0x$(printf 'volume%s' "$ACTOR" | shasum -a 256 | cut -c1-64)"
CAP_HEX="$(printf '%s' \
  '{"handlers":["create_inbox","send","reply","list_messages","get_message","search"],"slot":"email-inbox","poll_handler":"get_result","send_input":{"hire_id":"int","inbox_id":"str","to":"str|list","subject":"str","text":"str"},"returns":"agentmail-async-result","output":"acceptance record; get_result returns the AgentMail response under data","result_byte_budget":38000,"hire_activation":{"protocol":"store-admin-v1"}}' \
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

# --- 6. hire it (free term) -------------------------------------------------
# The chain allocates hire_id; it cannot be chosen. Read it back from the
# TradingPostHireCreated event rather than assuming a value.
info "trading-post hire (free)"
HIRE_OUT="$("$COWBOY" trading-post --private-key "$KEYFILE" hire \
  --declaration-id "$DECL" --version 1 --term free --amount 0 2>&1)" \
  || die "hire failed: $HIRE_OUT"
# Parse the LABELLED "Tx hash:" line — a bare first-0x…64 can catch the
# declaration id or build noise the hire output echoes ahead of the tx hash.
HIRE_TX="$(printf '%s' "$HIRE_OUT" \
  | sed -n 's/.*Tx hash:[[:space:]]*\(0x[0-9a-fA-F]\{64\}\).*/\1/p' | head -1)"
[ -n "$HIRE_TX" ] || HIRE_TX="$(printf '%s' "$HIRE_OUT" \
  | grep -oE '"hashes":\["0x[0-9a-fA-F]{64}"' | grep -oE '0x[0-9a-fA-F]{64}' | head -1)"
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

# --- 7. register the hire on the actor --------------------------------------
# Store Admin does this in the real flow, with its operator key, after checking
# the finalized listing (cowboyinc/store-admin#9). Here the registrar is the
# owner, so the demo can do it directly.
info "register_hire on the actor"
"$COWBOY" actor execute --actor "$ACTOR" --handler register_hire \
  --payload-json "{\"hire_id\":$HIRE_ID,\"hirer\":\"$OWNER\",\"declaration_id\":\"$DECL\",\"version\":$HIRE_VER,\"artifact_id\":$ARTIFACT}" \
  --private-key "$KEYFILE" >/dev/null 2>&1 || die "register_hire failed"
sleep 5
ok "hire $HIRE_ID registered"

# --- 8. create an inbox, self-send, and receive -----------------------------
info "running signed Canyon AgentMail flow"
COWBOY_SDK_PATH="${COWBOY_SDK_PATH:-$REPO/node/pvm/Lib}" \
  python3 -u "$SCRIPT_DIR/tools/live_agentmail_canyon.py" \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --rpc "$RPC_URL" \
  --wallet-key "$KEYFILE" --cowboy "$COWBOY" --key-file "$AGENTMAIL_API_KEY_FILE"
ok "AgentMail inbox actor $ACTOR passed the paid flow"
