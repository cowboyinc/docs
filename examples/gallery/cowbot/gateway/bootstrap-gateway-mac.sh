#!/usr/bin/env bash
# bootstrap-gateway-mac.sh — provision the Cowboy Gateway on a macOS host
# (e.g. shasta) for cowbot devnet. Single-host variant of
# bootstrap-gateway.sh — no systemd, no system user; runs under the
# current user via nohup, state lives under ~/.cowboy-gateway.
#
# What it does:
#   1. Builds the gateway binary if missing
#   2. Creates ~/.cowboy-gateway and a fresh secp256k1 keypair (idempotent)
#   3. Funds the keypair from a deployer wallet
#   4. Drops gateway.toml into place (keypair_path rewritten to absolute)
#   5. (Re)starts the gateway under nohup; logs at /tmp/cowbot-gateway.log
#   6. Prints next-step commands
#
# Required env:
#   COWBOY_ROOT           clone root, e.g. ~/dev/cowboy
#   FUNDING_PRIVATE_KEY   path to a wallet PEM that holds devnet CBY
#
# Optional env:
#   GATEWAY_RPC_URL       validator RPC (default http://127.0.0.1:4002)
#   GATEWAY_FUND_AMOUNT   CBY to seed the gateway with (default 100000000)
#   COWBOT_ACTOR          cowbot actor address — only used to print the
#                         Slack URL at the end

set -euo pipefail

# ── config ───────────────────────────────────────────────────────────

COWBOY_ROOT="${COWBOY_ROOT:?set COWBOY_ROOT to your cowboy clone root}"
FUNDING_PRIVATE_KEY="${FUNDING_PRIVATE_KEY:?set FUNDING_PRIVATE_KEY to a funded wallet PEM}"
GATEWAY_RPC_URL="${GATEWAY_RPC_URL:-http://127.0.0.1:4002}"
GATEWAY_FUND_AMOUNT="${GATEWAY_FUND_AMOUNT:-100000000}"
COWBOT_ACTOR="${COWBOT_ACTOR:-}"

GATEWAY_STATE="${GATEWAY_STATE:-$HOME/.cowboy-gateway}"
GATEWAY_BIN="$COWBOY_ROOT/gateway/target/release/gateway"
GATEWAY_KEYPAIR="$GATEWAY_STATE/keypair.json"
GATEWAY_CONFIG="$GATEWAY_STATE/gateway.toml"
GATEWAY_LOG="${GATEWAY_LOG:-/tmp/cowbot-gateway.log}"
GATEWAY_PIDFILE="$GATEWAY_STATE/gateway.pid"

ASSETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COWBOY_BIN="$COWBOY_ROOT/node/target/release/cowboy"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${CYAN}[info]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ok]${NC}    $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ -x "$COWBOY_BIN" ]] || fail "cowboy CLI not built: $COWBOY_BIN"

# ── 1. build gateway binary if missing ───────────────────────────────

if [[ ! -x "$GATEWAY_BIN" ]]; then
    info "building gateway (release)"
    ( cd "$COWBOY_ROOT/gateway" && cargo build --release --bin gateway ) \
        || fail "gateway build failed"
fi
[[ -x "$GATEWAY_BIN" ]] || fail "gateway binary still not present at $GATEWAY_BIN"

# ── 2. state dir + keypair ───────────────────────────────────────────

mkdir -p "$GATEWAY_STATE"
chmod 700 "$GATEWAY_STATE"

if [[ -f "$GATEWAY_KEYPAIR" ]]; then
    ok "gateway keypair already exists at $GATEWAY_KEYPAIR"
else
    info "generating gateway keypair"
    "$COWBOY_BIN" wallet create --output "$GATEWAY_KEYPAIR" \
        || fail "wallet create failed"
fi
chmod 600 "$GATEWAY_KEYPAIR"

GATEWAY_ADDR=$("$COWBOY_BIN" wallet address --key "$GATEWAY_KEYPAIR")
info "gateway address: $GATEWAY_ADDR"

# ── 3. fund the gateway ──────────────────────────────────────────────

CURRENT_BALANCE_LINE=$("$COWBOY_BIN" wallet balance --key "$GATEWAY_KEYPAIR" --rpc-url "$GATEWAY_RPC_URL" 2>/dev/null \
    | awk '/^Balance:/ {print; exit}')
info "current balance: ${CURRENT_BALANCE_LINE:-unknown}"

# Re-fund if balance is below half the fund amount. Transfers are nonce-
# gated by the deployer key, so re-running is safe.
NEED_FUNDING=1
BAL_CBY=$(echo "$CURRENT_BALANCE_LINE" | awk '/^Balance:/ {print $2}')
if [[ "$BAL_CBY" =~ ^[0-9]+$ ]]; then
    HALF_FUND_CBY=$(( GATEWAY_FUND_AMOUNT / 2 ))
    if (( BAL_CBY >= HALF_FUND_CBY )); then
        NEED_FUNDING=0
    fi
fi

if [[ "$NEED_FUNDING" -eq 1 ]]; then
    info "funding gateway with $GATEWAY_FUND_AMOUNT CBY from $FUNDING_PRIVATE_KEY"
    "$COWBOY_BIN" transfer \
        --rpc-url "$GATEWAY_RPC_URL" \
        --private-key "$FUNDING_PRIVATE_KEY" \
        --to "$GATEWAY_ADDR" \
        --amount "$GATEWAY_FUND_AMOUNT" \
        || fail "transfer failed"
    ok "funded"
else
    ok "gateway balance sufficient; skipping transfer"
fi

# ── 4. install gateway.toml (keypair_path rewritten absolute) ────────

# `sed -i ''` is BSD-flavor (macOS). Linux users should use the
# bootstrap-gateway.sh variant; that one drives systemd.
cp "$ASSETS_DIR/gateway.toml" "$GATEWAY_CONFIG"
sed -i '' "s|/var/lib/cowboy-gateway/keypair.json|$GATEWAY_KEYPAIR|" "$GATEWAY_CONFIG"

# Also rewrite the RPC URL in case the caller is on a non-default port.
sed -i '' "s|rpc_url = \"http://127.0.0.1:4002\"|rpc_url = \"$GATEWAY_RPC_URL\"|" "$GATEWAY_CONFIG"

ok "gateway.toml installed at $GATEWAY_CONFIG"

# ── 5. (re)start the gateway ─────────────────────────────────────────

# Stop any prior instance — same pidfile or same binary running.
if [[ -f "$GATEWAY_PIDFILE" ]]; then
    OLD_PID=$(cat "$GATEWAY_PIDFILE" 2>/dev/null || true)
    if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        info "stopping prior gateway (pid $OLD_PID)"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
fi
pkill -f "$GATEWAY_BIN.*--config $GATEWAY_CONFIG" 2>/dev/null || true
sleep 1

info "starting gateway under nohup"
nohup "$GATEWAY_BIN" --config "$GATEWAY_CONFIG" \
    >"$GATEWAY_LOG" 2>&1 </dev/null &
echo $! > "$GATEWAY_PIDFILE"
NEW_PID=$(cat "$GATEWAY_PIDFILE")

# Health check.
for _ in $(seq 1 20); do
    if curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        ok "gateway is responding on 127.0.0.1:8080 (pid $NEW_PID)"
        break
    fi
    sleep 0.5
done
if ! curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    tail -30 "$GATEWAY_LOG" >&2
    fail "gateway didn't respond on /healthz; see $GATEWAY_LOG"
fi

# ── 6. next steps ────────────────────────────────────────────────────

echo
ok "─── cowboy gateway bootstrap complete (macOS / nohup) ───"
echo "  binary:         $GATEWAY_BIN"
echo "  config:         $GATEWAY_CONFIG"
echo "  keypair:        $GATEWAY_KEYPAIR"
echo "  gateway addr:   $GATEWAY_ADDR"
echo "  pid file:       $GATEWAY_PIDFILE  (pid $NEW_PID)"
echo "  log:            $GATEWAY_LOG"
echo
cat <<EOF
Next steps:

  1. Expose the gateway via Tailscale Funnel (run on this host):
         tailscale serve --bg https / http://127.0.0.1:8080
         tailscale funnel 443 on
         tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.\$//'

  2. Provision the Slack secrets in CBSS (one-time, owner only). The actor holds
     NO plaintext secrets — the runner threshold-decrypts them (CIP-24 §5.5).
     deploy.sh Step 4c does this from \$COWBOT_SLACK_BOT_TOKEN /
     \$COWBOT_SLACK_SIGNING_SECRET; the manual form (also RUNBOOK §3):
         printf %s '<slack-signing-secret>' | $COWBOY_BIN secrets \\
               --rpc-url $GATEWAY_RPC_URL --private-key \$COWBOT_PRIVATE_KEY \\
               set SLACK_SIGNING_SECRET --value-file - --actor \$COWBOT_ACTOR
         printf %s '<slack-bot-token>'      | $COWBOY_BIN secrets \\
               --rpc-url $GATEWAY_RPC_URL --private-key \$COWBOT_PRIVATE_KEY \\
               set SLACK_BOT_TOKEN --value-file - --actor \$COWBOT_ACTOR

  3. Smoke test path resolution (no Slack, no gas):
         curl -i https://<funnel-hostname>/0x\$(echo \$COWBOT_ACTOR | sed 's/^0x//')/health
     Expected: HTTP 200, body "ok".

  4. Point the Slack app's Event Subscription URL at:
         https://<funnel-hostname>/0x\$(echo \$COWBOT_ACTOR | sed 's/^0x//')/slack/events

  5. Tail logs while you smoke-test:
         tail -f $GATEWAY_LOG /tmp/cowbot-validator.log /tmp/cowbot-runner.log

  6. Stop the gateway:
         kill \$(cat $GATEWAY_PIDFILE)
EOF
