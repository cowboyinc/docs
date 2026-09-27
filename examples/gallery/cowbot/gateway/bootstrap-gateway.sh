#!/usr/bin/env bash
# bootstrap-gateway.sh — provision the Cowboy Gateway on jefferson for
# cowbot devnet. Idempotent. Run as root (or via sudo) on jefferson.
#
# What it does:
#   1. Builds the gateway binary if it isn't already at the expected path
#   2. Creates the cowboy-gateway system user + /opt + /var/lib dirs
#   3. Generates a fresh secp256k1 keypair if one isn't present
#   4. Funds the keypair from a deployer wallet (via `cowboy transfer`)
#   5. Installs gateway.toml + the systemd unit
#   6. Enables + starts cowboy-gateway.service
#   7. Prints the next steps (Tailscale Funnel + Slack URL)
#
# Required env (matches the cowbot deploy.sh convention):
#   COWBOY_ROOT           clone root, e.g. /home/chad/dev/cowboy
#   FUNDING_PRIVATE_KEY   path to a wallet PEM that holds devnet CBY
#                         (typically the same key used by deploy.sh)
#
# Optional env:
#   GATEWAY_RPC_URL       defaults to http://127.0.0.1:4002
#   GATEWAY_FUND_AMOUNT   CBY to seed the gateway with (default 100000000 ≈ 1e8 CBY)
#   COWBOT_ACTOR          cowbot actor address — only used to print the
#                         Slack URL at the end; ./deploy.sh prints this too

set -euo pipefail

# ── config ───────────────────────────────────────────────────────────

COWBOY_ROOT="${COWBOY_ROOT:?set COWBOY_ROOT to your cowboy clone root}"
FUNDING_PRIVATE_KEY="${FUNDING_PRIVATE_KEY:?set FUNDING_PRIVATE_KEY to a funded wallet PEM}"
GATEWAY_RPC_URL="${GATEWAY_RPC_URL:-http://127.0.0.1:4002}"
GATEWAY_FUND_AMOUNT="${GATEWAY_FUND_AMOUNT:-100000000}"
COWBOT_ACTOR="${COWBOT_ACTOR:-}"

GATEWAY_USER="cowboy-gateway"
GATEWAY_HOME="/opt/cowboy-gateway"
GATEWAY_STATE="/var/lib/cowboy-gateway"
GATEWAY_BIN_SRC="$COWBOY_ROOT/gateway/target/release/gateway"
GATEWAY_BIN_DST="$GATEWAY_HOME/bin/gateway"
GATEWAY_KEYPAIR="$GATEWAY_STATE/keypair.json"
GATEWAY_CONFIG="$GATEWAY_HOME/gateway.toml"
SYSTEMD_UNIT="/etc/systemd/system/cowboy-gateway.service"

ASSETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COWBOY_BIN="$COWBOY_ROOT/node/target/release/cowboy"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${CYAN}[info]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ok]${NC}    $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run as root (sudo $0)"
[[ -x "$COWBOY_BIN" ]] || fail "cowboy CLI not built: $COWBOY_BIN"

# ── 1. build gateway binary if missing ───────────────────────────────

if [[ ! -x "$GATEWAY_BIN_SRC" ]]; then
    info "building gateway (release)"
    sudo -u "${SUDO_USER:-$(stat -c %U "$COWBOY_ROOT")}" \
        bash -c "cd '$COWBOY_ROOT/gateway' && cargo build --release --bin gateway" \
        || fail "gateway build failed"
fi
[[ -x "$GATEWAY_BIN_SRC" ]] || fail "gateway binary still not present at $GATEWAY_BIN_SRC"

# ── 2. user + dirs ───────────────────────────────────────────────────

if ! id -u "$GATEWAY_USER" >/dev/null 2>&1; then
    info "creating system user $GATEWAY_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$GATEWAY_USER"
fi

install -d -m 0755 -o "$GATEWAY_USER" -g "$GATEWAY_USER" "$GATEWAY_HOME"
install -d -m 0755 -o "$GATEWAY_USER" -g "$GATEWAY_USER" "$GATEWAY_HOME/bin"
install -d -m 0700 -o "$GATEWAY_USER" -g "$GATEWAY_USER" "$GATEWAY_STATE"

# ── 3. gateway keypair ───────────────────────────────────────────────

if [[ -f "$GATEWAY_KEYPAIR" ]]; then
    ok "gateway keypair already exists at $GATEWAY_KEYPAIR"
else
    info "generating gateway keypair"
    sudo -u "$GATEWAY_USER" "$COWBOY_BIN" wallet create --output "$GATEWAY_KEYPAIR" \
        || fail "wallet create failed"
fi
chmod 0600 "$GATEWAY_KEYPAIR"
chown "$GATEWAY_USER:$GATEWAY_USER" "$GATEWAY_KEYPAIR"

GATEWAY_ADDR=$("$COWBOY_BIN" wallet address --key "$GATEWAY_KEYPAIR")
info "gateway address: $GATEWAY_ADDR"

# ── 4. fund the gateway ──────────────────────────────────────────────

CURRENT_BALANCE_LINE=$("$COWBOY_BIN" wallet balance --key "$GATEWAY_KEYPAIR" --rpc-url "$GATEWAY_RPC_URL" 2>/dev/null \
    | awk '/^Balance:/ {print; exit}')
info "current balance: ${CURRENT_BALANCE_LINE:-unknown}"

# Re-fund every run if balance is below half the fund amount. Prevents the
# gateway from running dry across long-lived deploys; safe to re-run since
# transfers are nonce-gated by the deployer key, not the gateway key.
NEED_FUNDING=1
if echo "$CURRENT_BALANCE_LINE" | grep -qE '[0-9]+ CBY'; then
    BAL_INT=$(echo "$CURRENT_BALANCE_LINE" | sed -E 's/.*\(([0-9]+) wei.*/\1/')
    HALF_FUND_WEI=$(( GATEWAY_FUND_AMOUNT * 1000000000 / 2 ))
    if [[ -n "$BAL_INT" && "$BAL_INT" -ge "$HALF_FUND_WEI" ]]; then
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

# ── 5. install gateway.toml + systemd unit ───────────────────────────

install -m 0644 -o "$GATEWAY_USER" -g "$GATEWAY_USER" \
    "$ASSETS_DIR/gateway.toml" "$GATEWAY_CONFIG"
install -m 0755 -o "$GATEWAY_USER" -g "$GATEWAY_USER" \
    "$GATEWAY_BIN_SRC" "$GATEWAY_BIN_DST"
install -m 0644 -o root -g root \
    "$ASSETS_DIR/cowboy-gateway.service" "$SYSTEMD_UNIT"

systemctl daemon-reload

# ── 6. enable + (re)start ────────────────────────────────────────────

systemctl enable cowboy-gateway.service >/dev/null 2>&1 || true
systemctl restart cowboy-gateway.service
sleep 2
if ! systemctl is-active --quiet cowboy-gateway.service; then
    journalctl -u cowboy-gateway.service -n 50 --no-pager
    fail "cowboy-gateway.service failed to start"
fi
ok "cowboy-gateway.service is active"

# ── 7. next steps ────────────────────────────────────────────────────

echo
ok "─── cowboy gateway bootstrap complete ───"
echo "  binary:         $GATEWAY_BIN_DST"
echo "  config:         $GATEWAY_CONFIG"
echo "  keypair:        $GATEWAY_KEYPAIR"
echo "  gateway addr:   $GATEWAY_ADDR"
echo "  systemd unit:   $SYSTEMD_UNIT"
echo

cat <<EOF
Next steps:

  1. Expose the gateway via Tailscale Funnel:
         tailscale serve --bg https / http://127.0.0.1:8080
         tailscale funnel 443 on
     Note the public hostname from \`tailscale status --json | jq .Self.DNSName\`.

  2. Provision the Slack secrets in CBSS (one-time, owner only). The actor holds
     NO plaintext secrets — the runner threshold-decrypts them (CIP-24 §5.5).
     deploy.sh Step 4c does this from \$COWBOT_SLACK_BOT_TOKEN /
     \$COWBOT_SLACK_SIGNING_SECRET; the manual form (also RUNBOOK §3):
         printf %s '<slack-signing-secret>' | $COWBOY_BIN secrets \\
               --private-key \$COWBOT_PRIVATE_KEY \\
               set SLACK_SIGNING_SECRET --value-file - --actor \$COWBOT_ACTOR
         printf %s '<slack-bot-token>'      | $COWBOY_BIN secrets \\
               --private-key \$COWBOT_PRIVATE_KEY \\
               set SLACK_BOT_TOKEN --value-file - --actor \$COWBOT_ACTOR

  3. Smoke test the path-resolution wiring (no Slack involvement):
         curl -i https://<funnel-hostname>/0x\$(echo \$COWBOT_ACTOR | sed 's/^0x//')/health
     Expected: HTTP 200, body "ok".

  4. Set the Slack app's Event Subscription URL to:
         https://<funnel-hostname>/0x\$(echo \$COWBOT_ACTOR | sed 's/^0x//')/slack/events
     Slack will fire a url_verification challenge; the actor's slack_http
     handler (a @runner.continuation) echoes the challenge back inline, in its
     pre-await segment.

  5. Send a test message in Slack and watch:
         journalctl -u cowboy-gateway -f
         tail -f /tmp/cowbot-validator.log

  6. Once stable, retire the Python tunnel (the tunnel/ directory has already
     been removed from the repo — just stop any host still running it):
         systemctl stop cowbot-tunnel || true   # if you ran it as a service
EOF
