#!/usr/bin/env bash
#
# price-watcher — live walkthrough on a CBSS-enabled network (canyon/mesa).
#
# Walks the full Telegram-enabled deploy, with notify_test as the checkpoint
# before the two-way chain is switched on:
#   1. throwaway wallet (or KEY_FILE) + faucet, account DKG
#   2. `cowboy secrets set TELEGRAM_BOT_TOKEN`  (token never touches the chain)
#   3. deploy the watcher target WITH a secrets.read entitlement manifest —
#      without it the runner sends the literal string ${TELEGRAM_BOT_TOKEN}
#   4. track a JSON price source, run_now, read the price back
#   5. notify_test  → a message must arrive in your Telegram chat
#   6. set_telegram → the long-poll chain starts; text the bot /prices
#
# Requires a CBSS-enabled network (account DKG + cbssd proxies + CBFS relays),
# reachable over Tailscale, and a Telegram bot:
#   TELEGRAM_BOT_TOKEN=...     (required) token from @BotFather
#   TELEGRAM_CHAT_ID=...       (required) your chat id (message @userinfobot)
#   NETWORK=canyon|mesa        selects RPC + cbssd ingest defaults (default: canyon)
#   RPC_URL=...                CBSS-enabled RPC (overrides NETWORK default)
#   CBSS_INGEST_ENDPOINT=...   a cbssd proxy ingest endpoint (host:9602)
#   COWBOY=/path/to/cowboy     cowboy CLI (else PATH / node/target/release)
#   KEY_FILE=path              reuse a funded key (skips faucet + repeat DKG)
#   KEEP_RUNNING=1             leave the poll chain running at the end
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_DIR/.." && pwd)"
NODE_ROOT="$(cd "$REPO_ROOT/.." && pwd)/node"
source "$EXAMPLES_DIR/_demo_output.sh"

# ── Config ───────────────────────────────────────────────────────────────────
NETWORK="${NETWORK:-canyon}"
case "$NETWORK" in
  canyon) NET_RPC="https://rpc.canyon.cowboylabs.net" ;;
  mesa)   NET_RPC="https://rpc.mesa.cowboylabs.net" ;;
  *) echo "FATAL: unknown NETWORK '$NETWORK' (expected: canyon | mesa)" >&2; exit 1 ;;
esac
RPC_URL="${RPC_URL:-${COWBOY_RPC_URL:-$NET_RPC}}"
INGEST="${CBSS_INGEST_ENDPOINT:?set to the network's cbssd proxy ingest endpoint (http://host:9602)}"
export COWBOY_RPC_URL="$RPC_URL"
SECRET_KEY="TELEGRAM_BOT_TOKEN"
# Default watch: a JSON API with a deterministic shape, keyless and friendly
# to cloud egress IPs (CoinGecko 429s keyless calls from AWS). The price sits
# under "amount" (no "price" in the key), so this doubles as the override-path
# demo — auto-detect would find nothing here.
WATCH_URL="${WATCH_URL:-https://api.coinbase.com/v2/prices/BTC-USD/spot}"
WATCH_ITEM="${WATCH_ITEM:-BTC-USD}"
WATCH_PATH="${WATCH_PATH:-data.amount}"

find_cowboy() {
  if [[ -n "${COWBOY_BIN:-}" && -x "$COWBOY_BIN" ]]; then echo "$COWBOY_BIN"; return; fi
  if [[ -n "${COWBOY:-}" ]] && command -v "$COWBOY" >/dev/null 2>&1; then echo "$COWBOY"; return; fi
  if command -v cowboy >/dev/null 2>&1; then echo "cowboy"; return; fi
  if [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then echo "$NODE_ROOT/target/release/cowboy"; return; fi
  if [[ -x "$NODE_ROOT/target/debug/cowboy" ]]; then echo "$NODE_ROOT/target/debug/cowboy"; return; fi
  demo_fail "cowboy CLI not found. Build it: cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release"
  exit 1
}
COWBOY_BIN="$(find_cowboy)"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
die() { demo_fail "$*"; exit 1; }

hx() { printf "%s" "$1" | xxd -p -c 999999 | tr -d '\n'; }
nonce_now() { curl -s "$RPC_URL/account/$OWNER" | sed -n 's/.*"nonce":\([0-9]*\).*/\1/p'; }
settle_after() {
  local before="${1:-0}" now
  for _ in $(seq 1 40); do
    now="$(nonce_now)"
    [ "${now:-0}" -gt "$before" ] 2>/dev/null && return 0
    sleep 2
  done
  die "a transaction did not finalize (account nonce stuck at $before)"
}
# The actor's _parse() does json.loads on the raw payload bytes, so handlers
# want raw JSON-string bytes: --payload "$(hx ...)". NOT --payload-json (CBOR).
exec_actor() {
  local handler="$1" json="$2" n out
  n="$(nonce_now)"
  # capture the receipt: a reverted tx still bumps the nonce, so settle_after
  # alone cannot tell success from failure — assert Status: Success explicitly
  out="$("$COWBOY_BIN" actor execute --actor "$ACTOR" --handler "$handler" \
    --payload "$(hx "$json")" --private-key "$KEY" \
    --cycles-limit 20000000 --cells-limit 20000000 2>&1 || true)"
  settle_after "$n"
  demo_output_has_success_receipt "$out" \
    || die "handler '$handler' did not return Status: Success — $(printf '%s' "$out" | tail -1)"
}
read_actor() {
  curl -s --max-time 20 -X POST "$RPC_URL/actor/read" -H 'Content-Type: application/json' \
    -d "{\"actor\":\"$ACTOR\",\"handler\":\"$1\",\"payload_json\":${2:-{}}}" \
    | python3 -c 'import sys,json,base64
try: print(base64.b64decode(json.load(sys.stdin)["result"]).decode())
except Exception: print("{}")'
}

# ── Preflight ────────────────────────────────────────────────────────────────
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || die "TELEGRAM_BOT_TOKEN is required (create a bot with @BotFather)"
[ -n "${TELEGRAM_CHAT_ID:-}" ]  || die "TELEGRAM_CHAT_ID is required (message @userinfobot for yours, then message YOUR bot once so it may reply)"
demo_info "Network: $NETWORK"
demo_info "RPC:    $RPC_URL"
demo_info "Ingest: $INGEST"
demo_info "CLI:    $COWBOY_BIN"
"$COWBOY_BIN" status --rpc-url "$RPC_URL" >/dev/null 2>&1 || die "RPC unreachable at $RPC_URL (on Tailscale?)"
INGEST_CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$INGEST/" 2>/dev/null || echo 000)"
[ "$INGEST_CODE" != 000 ] || die "cbssd ingest endpoint unreachable at $INGEST (on Tailscale?)"

# ── 1. Wallet ────────────────────────────────────────────────────────────────
if [ -n "${KEY_FILE:-}" ]; then
  KEY="$KEY_FILE"
  OWNER="$("$COWBOY_BIN" wallet address --key "$KEY" | tr -d '[:space:]')"
  BAL="$(curl -s "$RPC_URL/account/$OWNER" | sed -n 's/.*"balance":\([0-9]*\).*/\1/p')"
  if [ "${BAL:-0}" -gt 0 ] 2>/dev/null; then
    demo_ok "reusing funded key for $(demo_shorten_address "$OWNER")"
  else
    curl -fsS -X POST "$RPC_URL/faucet" -H 'content-type: application/json' \
      -d "{\"address\":\"$OWNER\"}" >/dev/null || die "faucet request failed"
    for _ in $(seq 1 20); do
      BAL="$(curl -s "$RPC_URL/account/$OWNER" | sed -n 's/.*"balance":\([0-9]*\).*/\1/p')"
      [ "${BAL:-0}" -gt 0 ] 2>/dev/null && break
      sleep 3
    done
    [ "${BAL:-0}" -gt 0 ] 2>/dev/null || die "faucet funding never landed"
    demo_ok "reused key $(demo_shorten_address "$OWNER") funded"
  fi
else
  demo_print_command 1 "cowboy wallet create  +  POST $RPC_URL/faucet"
  KEY="$WORK/owner.key"
  "$COWBOY_BIN" wallet create --output "$KEY" >/dev/null
  OWNER="$("$COWBOY_BIN" wallet address --key "$KEY" | tr -d '[:space:]')"
  curl -fsS -X POST "$RPC_URL/faucet" -H 'content-type: application/json' \
    -d "{\"address\":\"$OWNER\"}" >/dev/null || die "faucet request failed"
  for _ in $(seq 1 20); do
    BAL="$(curl -s "$RPC_URL/account/$OWNER" | sed -n 's/.*"balance":\([0-9]*\).*/\1/p')"
    [ "${BAL:-0}" -gt 0 ] 2>/dev/null && break
    sleep 3
  done
  [ "${BAL:-0}" -gt 0 ] 2>/dev/null || die "faucet funding never landed"
  demo_ok "throwaway owner $(demo_shorten_address "$OWNER") funded"
fi

# ── 2. Account DKG (skipped if this owner already has a release key) ─────────
if curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2; then
  demo_ok "account release key already present"
else
  demo_print_command 1 "cowboy secrets request-account-dkg"
  N="$(nonce_now)"
  "$COWBOY_BIN" secrets --private-key "$KEY" --rpc-url "$RPC_URL" request-account-dkg >/dev/null
  settle_after "$N"
  READY=0
  for _ in $(seq 1 60); do
    curl -s "$RPC_URL/cbss/account-release-key/$OWNER" | grep -q mpk_g2 && { READY=1; break; }
    sleep 5
  done
  [ "$READY" = 1 ] || die "account release key never appeared (DKG did not complete)"
  demo_ok "account release key derived"
fi

# ── 3. Plant the bot token as a CBSS secret ──────────────────────────────────
demo_print_command 1 "cowboy secrets set $SECRET_KEY  (value via cbssd → CBFS; never on-chain)"
printf '%s' "$TELEGRAM_BOT_TOKEN" > "$WORK/secret.txt"
N="$(nonce_now)"
CBSS_INGEST_ENDPOINT="$INGEST" \
  "$COWBOY_BIN" secrets --private-key "$KEY" --rpc-url "$RPC_URL" \
  set "$SECRET_KEY" --value-file "$WORK/secret.txt" >/dev/null || die "secrets set failed"
settle_after "$N"
demo_ok "secret '$SECRET_KEY' set for $(demo_shorten_address "$OWNER")"

# ── 4. Deploy with the secrets.read entitlement ──────────────────────────────
# A custom manifest REPLACES the default entitlement set — declaring only
# secrets.read strips storage/timer/http rights and every handler dies with
# E1207. Ids must be in lexicographic order (the chain rejects unsorted).
cat > "$WORK/manifest.json" <<EOF
{ "name": "price-watcher", "version": "1.0.0",
  "entitlements": [
    { "id": "econ.hold_balance" },
    { "id": "http.fetch" },
    { "id": "secrets.read", "params": { "keys": ["$OWNER:$SECRET_KEY"] } },
    { "id": "storage.kv" },
    { "id": "timer.schedule" }
  ] }
EOF
SALT="0x$(head -c8 /dev/urandom | xxd -p | tr -d '\n')"
demo_print_command 1 "cowboy actor deploy --init-payload (secrets.read: $OWNER:$SECRET_KEY)"
# ATOMIC init: init is @public and first-caller-wins for ownership, so a
# --no-init deploy leaves a front-running window where anyone can land init
# first and own the actor forever. --init-payload runs init inside the deploy
# tx, from the deployer, closing that window. Explicit limits: the actor is
# ~50KB and its deploy needs >10M cells — the default budget OOGs with a
# receipt the address-grep below would mask.
# The CLI discovers the watcher target from the nearest cowboy.toml, so the
# deploy runs from the example directory.
cd "$SCRIPT_DIR"
DEPLOY="$("$COWBOY_BIN" actor deploy watcher \
  --manifest-json "$WORK/manifest.json" --salt "$SALT" \
  --init-payload '{"autostart": false, "min_drop_pct": 0}' \
  --cycles-limit 30000000 --cells-limit 30000000 \
  --private-key "$KEY" 2>&1 || true)"
ACTOR="$(printf '%s' "$DEPLOY" | sed -n 's/.*Actor address:[[:space:]]*\(0x[0-9a-fA-F]\{40\}\).*/\1/p' | head -1)"
[ -n "$ACTOR" ] || { printf '%s\n' "$DEPLOY"; die "actor deploy failed"; }
ENT=0
for _ in $(seq 1 20); do
  curl -s "$RPC_URL/actor/$ACTOR" | grep -q '"secrets.read"' && { ENT=1; break; }
  sleep 2
done
[ "$ENT" = 1 ] || die "actor deployed but secrets.read entitlement not attached (nonce race?)"
demo_ok "entitled actor $(demo_shorten_address "$ACTOR") deployed"
demo_info "actor: $ACTOR"

# Runner jobs (http fetches, Telegram sends/polls) are escrowed from the
# ACTOR's balance, not the caller's — an unfunded actor never gets its
# continuation resumed. Fund via plain `transfer` (the address space is
# unified): `fund-actor` from a version-mismatched CLI is accepted into the
# mempool but silently dropped at execution, which is invisible without the
# balance gate below. Retry on "skipped" (stale nonce while deploy settles).
demo_print_command 1 "transfer 2000 to the actor (runner jobs are paid from its balance)"
N="$(nonce_now)"
FUNDED=0
for _ in 1 2 3 4; do
  FUND_OUT="$("$COWBOY_BIN" transfer --to "$ACTOR" --amount 2000 --private-key "$KEY" 2>&1 || true)"
  case "$FUND_OUT" in *'"added":1'*) FUNDED=1; break ;; esac
  sleep 5
done
[ "$FUNDED" = 1 ] || { printf '%s\n' "$FUND_OUT"; die "funding transfer never submitted (nonce race?)"; }
settle_after "$N"
ABAL="$(curl -s "$RPC_URL/actor/$ACTOR" | sed -n 's/.*"balance":\([0-9]*\).*/\1/p')"
[ "${ABAL:-0}" -gt 0 ] 2>/dev/null || die "actor balance still zero after funding transfer"
demo_ok "actor funded (balance: $ABAL wei)"

# ── 4b. Release policy: allowlist THE ACTOR on the secret ────────────────────
# Two locks guard a cross-account secret release (CIP-24): the job's ref must
# name the owner account (the actor passes [owner, key] pairs — a bare key
# resolves to the actor's own empty account), AND the owner must allowlist the
# actor on the secret version. Without this policy the release is Unauthorized.
LISTING="$("$COWBOY_BIN" secrets list --actor "$OWNER" --rpc-url "$RPC_URL" 2>/dev/null)"
KH="$(printf '%s' "$LISTING" | grep -o '0x[0-9a-f]\{64\}' | head -1)"
VER="$(printf '%s' "$LISTING" | sed -n 's/.*latest_version=\([0-9]*\).*/\1/p' | head -1)"
[ -n "$KH" ] || die "could not read the secret's key_hash from 'secrets list'"
demo_print_command 1 "secrets policy --version ${VER:-1} --actor $ACTOR  (allowlist the actor)"
N="$(nonce_now)"
"$COWBOY_BIN" secrets --private-key "$KEY" --rpc-url "$RPC_URL" \
  policy --key-hash "$KH" --version "${VER:-1}" --actor "$ACTOR" >/dev/null 2>&1 \
  || die "secrets policy update failed"
settle_after "$N"
demo_ok "actor allowlisted on secret version ${VER:-1}"

# init already ran atomically at deploy (see --init-payload above)

# ── 5. Track a source + scan + read the price back ───────────────────────────
demo_print_command 1 "track $WATCH_ITEM via json path '$WATCH_PATH'"
exec_actor track "{\"url\":\"$WATCH_URL\",\"name\":\"demo-watch\",\"chat_id\":\"$TELEGRAM_CHAT_ID\",\"items\":[{\"name\":\"$WATCH_ITEM\",\"path\":\"$WATCH_PATH\"}]}"
demo_print_command 1 "run_now  (runner fetches the page; resume lands on-chain)"
exec_actor run_now '{}'
PRICE=""
demo_info "waiting for the scan continuation to land…"
for _ in $(seq 1 36); do
  PRICE="$(read_actor get_prices | python3 -c "import sys,json
p=json.load(sys.stdin).get('prices',{}).get('1',{})
print(p.get('$WATCH_ITEM',''))" 2>/dev/null)"
  [ -n "$PRICE" ] && break
  sleep 5
done
if [ -z "$PRICE" ]; then
  # last_scan_block=0 -> the continuation never resumed (job never executed:
  # actor unfunded? runner down?). last_ok=false with a block -> the fetch
  # landed but extraction found nothing (blocked/rate-limited URL?).
  demo_warn "watch state: $(read_actor get_watches)"
  die "no price landed (runner up? egress to $WATCH_URL?)"
fi
demo_ok "on-chain price for $WATCH_ITEM: $PRICE"

# ── 6. notify_test — the wiring checkpoint ───────────────────────────────────
demo_print_command 1 "notify_test  (deploy → CBSS release → runner egress → sendMessage)"
exec_actor notify_test "{\"chat_id\":\"$TELEGRAM_CHAT_ID\",\"text\":\"price-watcher demo: wiring OK — $WATCH_ITEM is $PRICE\"}"
demo_info "a Telegram message should arrive in chat $TELEGRAM_CHAT_ID within ~15s"
sleep 15
# HARD GATE: do not start the poll chain until the send provably worked. A
# chain whose secret can't be released dispatches an unfulfillable job over
# and over — and every failed attempt slashes a runner's reputation, until
# the whole fleet is below the dispatch threshold and the ecosystem is
# wedged for ALL jobs. Set NOTIFY_OK=1 to skip the prompt.
if [ "${NOTIFY_OK:-0}" != 1 ]; then
  printf "Did the Telegram message arrive? [y/N] "
  read -r REPLY
  case "$REPLY" in
    y|Y|yes|YES) ;;
    *) die "aborting before set_telegram — check the runner logs for 'secret fetch failed' (was the secret ingested via CBSS_INGEST_ENDPOINT? is the manifest's secrets.read key right?)" ;;
  esac
fi

# ── 7. Two-way: start the long-poll chain ────────────────────────────────────
demo_print_command 1 "set_telegram  (starts the continuous getUpdates chain)"
exec_actor set_telegram "{\"chat_id\":\"$TELEGRAM_CHAT_ID\"}"
demo_info "the chain is live — text your bot now:  /watches   /prices   /drops"
demo_info "replies land a few blocks after you hit send; you have 90 seconds…"
sleep 90
read_actor get_watches | python3 -c 'import sys,json
for wid,w in sorted(json.load(sys.stdin).get("watches",{}).items()):
    print("  watch %s: %s ok=%s" % (wid, w.get("name"), w.get("last_ok")))'

if [ "${KEEP_RUNNING:-0}" = 1 ]; then
  demo_ok "leaving the poll chain running (owner key controls it)"
else
  demo_print_command 1 "set_telegram running:false  (stop the chain; KEEP_RUNNING=1 to skip)"
  exec_actor set_telegram '{"running": false}'
  demo_ok "poll chain stopped"
fi

demo_ok "done — actor $ACTOR on $NETWORK (owner $(demo_shorten_address "$OWNER"))"
demo_info "re-enable chat:   set_telegram '{\"chat_id\":\"$TELEGRAM_CHAT_ID\"}'"
demo_info "scheduled scans:  set_running '{\"running\": true}'  (opt-in; wall-clock: add \"at\":\"09:00\")"
