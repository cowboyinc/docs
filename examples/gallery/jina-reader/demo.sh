#!/usr/bin/env bash
#
# Jina Store wrapper — local verification demo.
#
# Runs the checks a laptop can actually prove: target compilation, the real-SDK
# contract suite, and the route drift guard. It does NOT deploy or make
# a live vendor call — that needs a funded wallet, runners, CBSS, and a hire, and
# is driven by tools/live_jina.py (see the on-chain flow printed at the end).
#
# Usage:
#   COWBOY_SDK_PATH=/path/to/node/pvm/Lib ./demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

info() { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }

command -v uv >/dev/null || { echo "uv is required" >&2; exit 1; }

command -v cowboy >/dev/null || { echo "cowboy is required" >&2; exit 1; }

info "compile the jina target"
cowboy actor check jina
ok "target is valid"

info "real-SDK contract suite + route drift guard (pinned toolchain)"
uv run pytest
ok "offline suite green"

info "route pin vs the current Jina OpenAPI"
uv run python tools/check_jina_parity.py --url https://api.jina.ai/openapi.json

cat <<'NEXT'

Local verification done. The on-chain flow (needs a funded wallet, runners, CBSS,
a Trading Post hire, and a JINA_API_KEY in CBSS) is driven by tools/live_jina.py:

  uv run tools/live_jina.py --actor "$ACTOR" --hire-id "$HIRE_ID" \
    --rpc "$RPC_URL" --wallet-key /secure/wallet.key \
    call --handler read --payload-json '{"url":"https://example.com"}'

  uv run tools/live_jina.py --actor "$ACTOR" --hire-id "$HIRE_ID" \
    --rpc "$RPC_URL" --wallet-key /secure/wallet.key \
    compare --key-file /secure/jina-key.txt      # paid direct-vs-chain checks
NEXT
