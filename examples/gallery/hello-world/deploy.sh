#!/usr/bin/env bash
# deploy.sh — idempotent hello-world deploy (baseline-manifest compatible).
#
# The simplest per-actor deploy in the gallery: no volumes, no secrets, no
# store dependency. Computes the deterministic CREATE2 address, skips the
# deploy if the actor already exists, deploys otherwise. Safe to re-run.
#
# Usage:
#   ./deploy.sh                        # RPC from COWBOY_RPC_URL / CLI config
#   ./deploy.sh http://127.0.0.1:4000  # explicit RPC URL
#
# Env (first match wins):
#   HELLO_PRIVATE_KEY | COWBOY_PRIVATE_KEY | DEPLOYER_KEY   deployer key file
#   HELLO_SALT                                              CREATE2 salt (default 0xe110)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"
SALT="${HELLO_SALT:-0xe110}"
PRIVATE_KEY="${HELLO_PRIVATE_KEY:-${COWBOY_PRIVATE_KEY:-${DEPLOYER_KEY:-}}}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

command -v cowboy >/dev/null 2>&1 || fail "cowboy CLI not on PATH"
[[ -f "actors/hello/main.py" ]] || fail "actors/hello/main.py not found"
[[ -n "$PRIVATE_KEY" && -f "$PRIVATE_KEY" ]] || fail "set HELLO_PRIVATE_KEY / COWBOY_PRIVATE_KEY / DEPLOYER_KEY to the deployer key file"

[[ -n "${1:-}" ]] && export COWBOY_RPC_URL="$1"
RPC_URL="${COWBOY_RPC_URL:-http://127.0.0.1:4000}"
export COWBOY_RPC_URL="$RPC_URL"

CREATOR_ADDR="$(cowboy wallet address --key "$PRIVATE_KEY")"
info "creator = $CREATOR_ADDR"

ACTOR_ADDR="$(cowboy actor address hello \
    --creator "$CREATOR_ADDR" \
    --salt "$SALT" \
    | python3 -c '
import re, sys
m = re.findall(r"Actor address:\s*(?:0x)?([0-9a-fA-F]{40})", sys.stdin.read())
print(("0x" + m[-1]) if m else "")
')"
[[ -n "$ACTOR_ADDR" ]] || fail "could not compute actor address"
info "hello-world actor = $ACTOR_ADDR"

if curl -sf "${RPC_URL}/actor/${ACTOR_ADDR}" >/dev/null 2>&1; then
    ok "actor already deployed"
else
    cowboy actor deploy hello \
        --private-key "$PRIVATE_KEY" \
        --salt "$SALT"
    ok "deployed hello-world at $ACTOR_ADDR"
fi

echo ""
ok "hello-world ready — gateway serves https://${ACTOR_ADDR}.<gateway-domain>/"
