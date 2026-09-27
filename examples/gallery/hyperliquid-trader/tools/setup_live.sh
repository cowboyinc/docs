#!/usr/bin/env bash
# Post-rebuild setup for the Hyperliquid signing test on a fresh river box.
#
# Order matters and was learned the hard way:
#   1. wallet + faucet
#   2. account DKG  — needs >= DEFAULT_N (5) registered CBSS proxies, else the
#      instruction reverts WITHOUT consuming the nonce (silent retry loop)
#   3. secret set   — needs the release key from (2) to encrypt to
#   4. deploy       — needs a manifest granting secrets.read <owner>:HL_AGENT_KEY,
#      and atomic init (NOT --no-init) so init() claims ownership for
#      @callable_by(OWNER) handlers
#   5. fund actor   — it pays for its own runner jobs. --amount is in CBY.
#
# Step 3 is deliberately NOT here: the key must not pass through automation.
set -uo pipefail

RPC="${RPC:-}"
[ -n "$RPC" ] || { echo "set RPC=http://<ip>:4000"; exit 1; }
S="$(cd "$(dirname "$0")" && pwd)"
export COWBOY_RPC_URL="$RPC"
CLI="${CLI:-$(command -v cowboy)}"
[ -n "$CLI" ] && [ -x "$CLI" ] || { echo "set CLI to a version-matched cowboy binary"; exit 1; }
GATEWAY_HOST_SUFFIX="${GATEWAY_HOST_SUFFIX:-}"
CBSS_INGEST="${CBSS_INGEST_ENDPOINT:-}"
case "$RPC" in
  *canyon.cowboylabs.net*)
    GATEWAY_HOST_SUFFIX="${GATEWAY_HOST_SUFFIX:-canyon.cowboylabs.net}"
    ;;
  *:4000*) CBSS_INGEST="${CBSS_INGEST:-${RPC%:4000}:9602}" ;;
esac
TG_SECRET_KEY="TELEGRAM_BOT_TOKEN"
TG_TOKEN_FILE="${TG_TOKEN_FILE:-$HOME/.telegram_bot_token}"
if [ -z "${TG_TOKEN:-}" ] && [ -s "$TG_TOKEN_FILE" ]; then
  TG_TOKEN="$(tr -d '\r\n' < "$TG_TOKEN_FILE")"
fi
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  : "${TG_WEBHOOK_SECRET:?set TG_WEBHOOK_SECRET before wiring Telegram; use the same secret_token when registering the webhook}"
fi

# Loop cadences (blocks). Committed defaults are production-sane; a demo run
# can compress them via env, e.g.  REBALANCE_BLOCKS=500 SYNTH_BLOCKS=300 ...
# Do NOT over-compress: aggressive intervals flood the runners and can wedge
# the chain (InsufficientRunners). These moderate demo values were safe.
#
# Keep every value PRIME. Inline timers execute during block production, so two
# loops coming due in the same block stack their handler cost inside one block
# deadline. Coprime cadences make that coincidence rare instead of periodic —
# composite values like 1200/1800/3600 collide every 3600 blocks, three at once.
#
# `ingest` was previously absent from the init payload entirely, so it silently
# ran at the actor default of 149 — the most frequent loop in the fleet at ~17
# fires/hour. It only needs to outpace `synth`, which consumes what it produces.
# The Hyperliquid MASTER account our agent key trades for. Without this the
# actor never calls clearinghouseState and cannot tell its own storage from
# reality — which is how five generations of trader accumulated a 16-position
# book on the venue while every dashboard read a clean ~1000 equity.
HL_ACCOUNT_ADDR="${HL_ACCOUNT_ADDR:-0x4da5652c1525A895597D87D07F4A1D6f83F5e472}"
# Match the REAL account, not the aspirational 1000. Reported equity should
# mean something.
BUDGET="${BUDGET:-500}"
MAX_POSITIONS="${MAX_POSITIONS:-8}"
MIN_CONFIDENCE="${MIN_CONFIDENCE:-70}"
EQUITY_PCT="${EQUITY_PCT:-0.50}"
STOP_LOSS_PCT="${STOP_LOSS_PCT:-0.03}"
TAKE_PROFIT_PCT="${TAKE_PROFIT_PCT:-0.06}"
MAX_CUMULATIVE_LOSS_PCT="${MAX_CUMULATIVE_LOSS_PCT:-0.10}"
MAX_EXIT_PRICE_RETRIES="${MAX_EXIT_PRICE_RETRIES:-3}"
CLOSE_ABSENT="${CLOSE_ABSENT:-false}"
MAX_HOLD_BLOCKS="${MAX_HOLD_BLOCKS:-14407}"
REBALANCE_BLOCKS="${REBALANCE_BLOCKS:-2399}"
MARK_BLOCKS="${MARK_BLOCKS:-293}"
UNIVERSE_BLOCKS="${UNIVERSE_BLOCKS:-4001}"
INGEST_BLOCKS="${INGEST_BLOCKS:-419}"
SYNTH_BLOCKS="${SYNTH_BLOCKS:-599}"
DISCOVER_BLOCKS="${DISCOVER_BLOCKS:-997}"
MIN_AGE_BLOCKS="${MIN_AGE_BLOCKS:-5999}"
VALIDATE_BLOCKS="${VALIDATE_BLOCKS:-7193}"
DISTILL_BLOCKS="${DISTILL_BLOCKS:-14407}"
BRIEF_BLOCKS="${BRIEF_BLOCKS:-449}"
START_LOOPS="${START_LOOPS:-1}"
TRADER_FUND_CBY="${TRADER_FUND_CBY:-500}"
RESEARCH_FUND_CBY="${RESEARCH_FUND_CBY:-300}"
NOTIFY_FUND_CBY="${NOTIFY_FUND_CBY:-150}"
# Demo-safe mode keeps only the three research loops required to refresh ideas.
# Historical grading, lesson distillation, and scheduled briefs remain opt-in.
if [ "${LIGHTWEIGHT:-1}" = "1" ]; then
  VALIDATE_BLOCKS=0
  DISTILL_BLOCKS=0
  BRIEF_BLOCKS=0
fi
# Deploy BUNDLED artifacts: the repo files import hl_common (CIP-26), but
# timer-fired handlers never get lib pins injected, so a raw deploy's timers
# silently no-op. bundle.py inlines the library.
DIR="$(dirname "$S")"
CODE=/tmp/hl_live_exec_bundled.py
RCODE=/tmp/hl_research_bundled.py

say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
die(){ echo "FAILED: $*" >&2; exit 1; }
secret_key_hash(){
  "$CLI" secrets encrypt --account "$DEP" --key "$1" --value-file /dev/null \
    --rpc-url "$RPC" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key_hash", ""))'
}
secret_latest_version(){
  curl -s --max-time 8 "$RPC/cbss/secrets/$DEP" \
    | python3 -c 'import json,sys
want=sys.argv[1].lower().removeprefix("0x")
rows=json.load(sys.stdin).get("secrets", [])
print(next((row.get("latest_version", 0) for row in rows
            if str(row.get("key_hash", "")).lower().removeprefix("0x") == want), 0))' "$1"
}
# wait until the deployer nonce advances (tx included) — fixed sleeps race the
# mempool and cascade into nonce_too_low under load
settle(){
  local n0 n1 i
  n0="$(curl -s --max-time 6 "$RPC/account/$(cat "$S/deployer.txt" 2>/dev/null | sed 's/^0x//')" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("nonce",0))' 2>/dev/null || echo 0)"
  for i in $(seq 1 40); do sleep 3
    n1="$(curl -s --max-time 6 "$RPC/account/$(cat "$S/deployer.txt" | sed 's/^0x//')" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("nonce",0))' 2>/dev/null || echo 0)"
    [ "${n1:-0}" -gt "${n0:-0}" ] && return 0
  done
  return 1
}
xq(){ # execute + settle, loud on failure
  local out
  # Setup/start/discover all fit the default 512 client stamp. Manual trading is
  # routed through apply_ideas by demo.sh instead of hard-coding a larger limit.
  out="$("$CLI" actor execute --actor "$1" --handler "$2" --payload-text "$3"     --private-key "$S/deploykey" --cycles-limit 40000000 --cells-limit 40000000 2>&1)"
  echo "$out" | grep -qi "transaction hash" || { echo "  $2 -> $(echo "$out" | grep -i error | head -1)"; return 1; }
  settle; echo "  $2 ok"
}
xq_raw(){ # execute one handler with a raw binary payload file
  local out hex
  hex="$(xxd -p "$3" | tr -d '\n')"
  out="$("$CLI" actor execute --actor "$1" --handler "$2" --payload "$hex" \
    --private-key "$S/deploykey" --cycles-limit 40000000 --cells-limit 40000000 2>&1)"
  echo "$out" | grep -qi "transaction hash" || { echo "  $2 -> $(echo "$out" | grep -i error | head -1)"; return 1; }
  settle; echo "  $2 ok"
}

say "1. wallet + faucet"
if [ ! -s "$S/deploykey" ]; then
  "$CLI" wallet create --output "$S/deploykey" >/dev/null 2>&1
else
  echo "reusing existing deployer key"
fi
DEP="$("$CLI" wallet address --key "$S/deploykey" 2>/dev/null | tail -1)"
echo "$DEP" > "$S/deployer.txt"
echo "deployer: $DEP"
for _ in 1 2 3; do
  curl -s --max-time 8 -X POST "$RPC/faucet" -H 'Content-Type: application/json' \
    -d "{\"address\":\"$DEP\"}" >/dev/null 2>&1
  sleep 2
done
BAL="$(curl -s --max-time 8 "$RPC/account/${DEP#0x}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("balance",0))' 2>/dev/null)"
[ "${BAL:-0}" != "0" ] || die "faucet did not fund $DEP"
echo "balance: $BAL"

say "2. wait for >= 5 eligible CBSS proxies"
for i in $(seq 1 60); do
  N="$(curl -s --max-time 8 "$RPC/cbss/proxies" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("proxies",[])))' 2>/dev/null || echo 0)"
  echo "  proxies=$N"
  [ "${N:-0}" -ge 5 ] && break
  sleep 10
done
[ "${N:-0}" -ge 5 ] || die "only $N proxies; DEFAULT_N=5 required for account DKG"

say "3. account DKG"
# IDEMPOTENT: `STAGE=deploy` re-runs steps 1-3, and a second `request-account-dkg`
# ROTATES the account release key. Any secret set between stage 1 and stage 2 was
# encrypted to the old key, so rotating orphans it and step 4b then dies with
# "no secret registered for <deployer>". Skip the request when a key already
# exists — that makes the documented stage1 -> secret -> STAGE=deploy flow work.
if curl -s --max-time 8 "$RPC/cbss/account-release-key/$DEP" | grep -q mpk_g2; then
  echo "release key already present — skipping DKG (re-running it would rotate the key)"
else
  "$CLI" secrets --private-key "$S/deploykey" --rpc-url "$RPC" request-account-dkg 2>&1 | grep -iE "tx hash|error" || true
  for i in $(seq 1 40); do
    curl -s --max-time 8 "$RPC/cbss/account-release-key/$DEP" | grep -q mpk_g2 && break
    sleep 10
  done
  curl -s --max-time 8 "$RPC/cbss/account-release-key/$DEP" | grep -q mpk_g2 \
    || die "no account release key (check proxy count / nonce did not advance)"
  echo "release key ready"
fi

say "NEXT (manual, key must not go through automation):"
cat <<EOF
  nano /tmp/hlkey.txt        # paste the agent private key, Ctrl+O, Enter, Ctrl+X
  CBSS_INGEST_ENDPOINT=$CBSS_INGEST \\
  $CLI secrets --private-key $S/deploykey --rpc-url $RPC \\
    set HL_AGENT_KEY --value-file /tmp/hlkey.txt
  rm -P /tmp/hlkey.txt
Then re-run this script with STAGE=deploy to continue.
EOF

[ "${STAGE:-}" = "deploy" ] || exit 0

say "4. deploy entitled actor (atomic init)"
# Entitlements MUST be sorted lexicographically by id.
cat > "$S/manifest.json" <<EOF
{ "name": "hl-live-exec", "version": "0.1.0",
  "entitlements": [
    { "id": "http.fetch" },
    { "id": "secrets.read", "params": { "keys": ["$DEP:HL_AGENT_KEY"] } },
    { "id": "storage.kv" },
    { "id": "timer.schedule" }
  ] }
EOF
cat > "$S/notify_manifest.json" <<EOF
{ "name": "hl-notify", "version": "0.1.0",
  "entitlements": [
    { "id": "http.fetch" },
    { "id": "secrets.read", "params": { "keys": ["$DEP:$TG_SECRET_KEY"] } },
    { "id": "storage.kv" },
    { "id": "timer.schedule" }
  ] }
EOF
python3 "$S/bundle.py" "$DIR/hl_live_exec.py" "$CODE"   || die "bundle trader"
python3 "$S/bundle.py" "$DIR/hl_research.py"  "$RCODE"  || die "bundle research"
SALT="0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')"
# CIP-26: the actors `import hl_common`; deploy-time pin resolution looks the
# name up in THIS deployer's library catalog, so it must be published (or
# re-published after edits) before any actor deploy. "skipped" on the publish
# output means the identical bytes are already cataloged — fine.
"$CLI" lib publish --name hl_common --code "$(dirname "$S")/hl_common.py" \
  --private-key "$S/deploykey" --rpc-url "$RPC" 2>&1 | grep -iE "added|skipped|error" || true
settle || true
# 40M: the actor carries the inlined venue layer (digest/msgpack/EIP-712), so
# the default max-affordable ceiling is not enough — 10M fails with OutOfGas at
# ~12.2M cells.
OUT="$("$CLI" actor deploy --code "$CODE" --manifest-json "$S/manifest.json" \
  --cycles-limit 40000000 --cells-limit 40000000 \
  --salt "$SALT" \
  --init-payload "{\"timeout_ms\": 10000, \"secret_account\": \"$DEP\", \"research_pull\": true, \"mark_interval_blocks\": $MARK_BLOCKS, \"universe_interval_blocks\": $UNIVERSE_BLOCKS, \"rebalance_interval_blocks\": $REBALANCE_BLOCKS, \"account_addr\": \"$HL_ACCOUNT_ADDR\", \"budget\": $BUDGET, \"max_positions\": $MAX_POSITIONS, \"min_confidence\": $MIN_CONFIDENCE, \"equity_pct\": $EQUITY_PCT, \"stop_loss_pct\": $STOP_LOSS_PCT, \"take_profit_pct\": $TAKE_PROFIT_PCT, \"max_cumulative_loss_pct\": $MAX_CUMULATIVE_LOSS_PCT, \"max_exit_price_retries\": $MAX_EXIT_PRICE_RETRIES, \"close_absent\": $CLOSE_ABSENT, \"max_hold_blocks\": $MAX_HOLD_BLOCKS}" \
  --private-key "$S/deploykey" 2>&1)"
echo "$OUT" | grep -qi "deployed successfully" || { echo "$OUT"; die "deploy failed"; }
A="$(printf '%s' "$OUT" | sed -n 's/.*Actor address:[[:space:]]*\(0x[0-9a-fA-F]\{40\}\).*/\1/p' | head -1)"
echo "$A" > "$S/actor.txt"
echo "actor: $A"
curl -s --max-time 8 "$RPC/actor/$A" | grep -q '"secrets.read"' || die "entitlement not attached"

say "4b. CBSS release policy — allowlist the actor on the secret version"
# The manifest entitlement lets the ACTOR ask; this policy is what makes CBSS
# release TO it. Without it cbssd refuses with "release request was not
# authorized" and the runner sees `early eof` on every committee member.
# Resolve the hash from the scoped plaintext name. A deployer carries both the
# Hyperliquid and Telegram secrets, so selecting by list position or creation
# time can silently attach policy to the wrong credential.
KH="${KEY_HASH:-$(secret_key_hash HL_AGENT_KEY)}"
[ -n "$KH" ] || die "no secret registered for $DEP — run the secrets set step first"
PV="${POLICY_VERSION:-$(secret_latest_version "$KH")}"
[ "${PV:-0}" -gt 0 ] 2>/dev/null || die "HL_AGENT_KEY has no active version"
"$CLI" secrets --private-key "$S/deploykey" --rpc-url "$RPC" \
  policy --key-hash "$KH" --version "$PV" --actor "$A" 2>&1 | grep -iE "tx hash|error" || true
sleep 10
echo "policy set: $KH version $PV -> $A"

say "4c. deploy research actor (stretched grading horizons)"
# rebalance hourly is trader-side; here: theses get ~2.5h before grading
# (min_age), validate every ~3h, distill every ~6h. Odd numbers avoid timer
# collisions. All overridable in this payload.
ROUT="$("$CLI" actor deploy --code "$RCODE" --manifest-json "$S/research_manifest.json" \
  --cycles-limit 40000000 --cells-limit 40000000 \
  --salt "0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')" \
  --init-payload "{\"ingest_interval_blocks\": $INGEST_BLOCKS, \"synth_interval_blocks\": $SYNTH_BLOCKS, \"discover_interval_blocks\": $DISCOVER_BLOCKS, \"validate_interval_blocks\": $VALIDATE_BLOCKS, \"min_age_blocks\": $MIN_AGE_BLOCKS, \"distill_interval_blocks\": $DISTILL_BLOCKS, \"brief_check_interval_blocks\": $BRIEF_BLOCKS}" \
  --private-key "$S/deploykey" 2>&1)"
echo "$ROUT" | grep -qi "deployed successfully" || { echo "$ROUT"; die "research deploy failed"; }
RA="$(printf '%s' "$ROUT" | sed -n 's/.*Actor address:[[:space:]]*\(0x[0-9a-fA-F]\{40\}\).*/\1/p' | head -1)"
echo "$RA" > "$S/research.txt"
echo "research: $RA"

say "5. fund actor (CBY)"
"$CLI" transfer --to "$A" --amount "$TRADER_FUND_CBY" --private-key "$S/deploykey" >/dev/null 2>&1
sleep 6
"$CLI" transfer --to "$RA" --amount "$RESEARCH_FUND_CBY" --private-key "$S/deploykey" >/dev/null 2>&1
sleep 8
echo "actor account: $(curl -s --max-time 8 "$RPC/account/${A#0x}" | head -c 120)"

say "6. sanity: digest on-chain must match the SDK"
curl -s --max-time 20 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
  -d "{\"actor\":\"$A\",\"handler\":\"preview\",\"payload_text\":\"{\\\"asset\\\":3,\\\"is_buy\\\":true,\\\"price\\\":\\\"1.0\\\",\\\"size\\\":\\\"0.001\\\",\\\"nonce\\\":1700000000000}\"}" \
 | python3 -c "
import sys,json,base64
d=json.load(sys.stdin); r=json.loads(base64.b64decode(d['result']))
want='0x61caa23b29f231d308406a3f7a6058e1b97c8a036d8e6e7454b565d2ebac9141'
print('digest:', r['digest'])
print('MATCH :', r['digest']==want)
"
say "7. notifier actor (Telegram bridge)"
# The manifest references the CBSS token by name. The plaintext is submitted
# only after deployment so the secret policy can allowlist this exact actor.
# It is never placed in actor storage or an actor-execute payload.
NOUT="$("$CLI" actor deploy --code "$DIR/hl_notify.py" --manifest-json "$S/notify_manifest.json" \
  --cycles-limit 40000000 --cells-limit 40000000 \
  --salt "0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')" \
  --init-payload "{\"secret_account\":\"$DEP\"}" \
  --private-key "$S/deploykey" 2>&1)"
echo "$NOUT" | grep -qi "deployed successfully" || { echo "$NOUT" | tail -3; die "notifier deploy failed"; }
NA="$(printf '%s' "$NOUT" | sed -n 's/.*Actor address:[[:space:]]*\(0x[0-9a-fA-F]\{40\}\).*/\1/p' | head -1)"
echo "$NA" > "$S/notify.txt"; echo "notifier: $NA"; settle || true
curl -s --max-time 8 "$RPC/actor/$NA" | grep -q '"secrets.read"' \
  || die "notifier secrets.read entitlement not attached"
"$CLI" transfer --to "$NA" --amount "$NOTIFY_FUND_CBY" --private-key "$S/deploykey" >/dev/null 2>&1; settle || true
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  [ -n "$CBSS_INGEST" ] || die "set CBSS_INGEST_ENDPOINT to store the Telegram token"
  TG_SECRET_FILE="$(mktemp)" || die "could not create Telegram secret file"
  chmod 600 "$TG_SECRET_FILE"
  printf '%s' "$TG_TOKEN" > "$TG_SECRET_FILE"
  CBSS_INGEST_ENDPOINT="$CBSS_INGEST" "$CLI" secrets \
    --private-key "$S/deploykey" --rpc-url "$RPC" \
    set "$TG_SECRET_KEY" --value-file "$TG_SECRET_FILE" --actor "$NA" \
    >/dev/null 2>&1 || { rm -f "$TG_SECRET_FILE"; die "Telegram CBSS secret setup failed"; }
  rm -f "$TG_SECRET_FILE"
  echo "  Telegram token stored in CBSS and allowlisted to notifier"
fi
python3 "$S/lower_notify_routes.py" >/dev/null || die "notifier route lowering failed"
xq_raw "$NA" set_routes "$DIR/notify_routes.cbor" >/dev/null || die "notifier route install failed"
GATEWAY_HOST="$(printf '%s' "$NA" | tr 'A-F' 'a-f').${GATEWAY_HOST_SUFFIX}"
if [ -n "$GATEWAY_HOST_SUFFIX" ]; then
  for _ in $(seq 1 20); do
    GATEWAY_CODE="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' \
      "https://$GATEWAY_HOST/telegram/health" || true)"
    [ "$GATEWAY_CODE" = "200" ] && break
    sleep 3
  done
  [ "$GATEWAY_CODE" = "200" ] || die "gateway health returned ${GATEWAY_CODE:-000} for $GATEWAY_HOST"
  echo "  gateway health ok: https://$GATEWAY_HOST/telegram/health"
fi
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  WEBHOOK_PAYLOAD="$(TG_WEBHOOK_SECRET="$TG_WEBHOOK_SECRET" python3 -c 'import hashlib,json,os; print(json.dumps({"secret_hash": hashlib.sha256(os.environ["TG_WEBHOOK_SECRET"].encode()).hexdigest()}))')"
  xq "$NA" set_webhook_secret "$WEBHOOK_PAYLOAD" >/dev/null || die "webhook secret setup failed"
  xq "$NA" set_telegram "{\"chat_id\":\"$TG_CHAT\"}" >/dev/null && echo "  set_telegram ok"
else
  echo "  TG_TOKEN/TG_CHAT unset — skipping telegram config"
fi
xq "$NA" set_sources "{\"research_addr\":\"$RA\",\"trader_addr\":\"$A\"}"
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  [ -n "$GATEWAY_HOST_SUFFIX" ] || die "set GATEWAY_HOST_SUFFIX for webhook delivery"
  WEBHOOK_URL="https://$GATEWAY_HOST/telegram/webhook"
  WEBHOOK_RESULT="$(curl -fsS "https://api.telegram.org/bot${TG_TOKEN}/setWebhook" \
    -d "url=$WEBHOOK_URL" -d "secret_token=$TG_WEBHOOK_SECRET")" \
    || die "Telegram setWebhook failed"
  printf '%s' "$WEBHOOK_RESULT" | python3 -c 'import json,sys; assert json.load(sys.stdin).get("ok") is True' \
    || die "Telegram rejected setWebhook"
  echo "  webhook registered: $WEBHOOK_URL"
else
  echo "  Telegram credentials unset; route installed but bot not registered"
fi

say "8. wire the fleet"
# Raw XML HTTP results are base64-wrapped by the runner. Keep each source well
# below the 65,536-byte result contract so the callback remains admissible even
# after that 4/3 expansion. Cointelegraph and Decrypt regularly exceed the safe
# envelope; these bounded publisher feeds cover crypto, AI, and liquid markets.
xq "$RA" set_feeds '{"feeds":["https://www.theblock.co/rss.xml","https://techcrunch.com/category/artificial-intelligence/feed/","https://feeds.content.dowjones.io/public/rss/mw_topstories"]}'
xq "$RA" set_universe '{"coins":["BTC","ETH","SOL"],"stocks":["MSFT","AMZN","META"]}'
xq "$RA" set_mandate '{"mandate":"Cross-asset macro across major crypto perps and liquid US tech equities."}'
xq "$RA" set_exec "{\"exec_addr\":\"$A\"}"
xq "$RA" set_notify "{\"notify_addr\":\"$NA\"}"
xq "$A" set_research "{\"research_addr\":\"$RA\"}"
xq "$A" set_notify "{\"notify_addr\":\"$NA\"}"
if [ "$START_LOOPS" = "1" ]; then
  xq "$RA" start '{}'
  xq "$A" start '{}'
  xq "$RA" discover_now '{}'
  say "fleet live: research=$RA trader=$A notifier=$NA"
else
  say "fleet deployed but stopped: research=$RA trader=$A notifier=$NA"
fi
