#!/usr/bin/env bash
# deploy.sh — idempotent end-to-end cowbot deploy.
#
# Re-runnable: detects prior state and only does what's needed.
#   - skips actor deploy if an actor already exists at the deterministic
#     address derived from the cowbot target + creator pubkey + salt
#   - skips volume creation if the volumes already exist on chain
#   - always prints the actor address + next-step instructions
#
# Usage:
#   ./deploy.sh                                # default network from .cowboy/config.json
#   ./deploy.sh http://<rpc-host>:4000         # explicit RPC URL
#   COWBOT_SALT=0xc0w ./deploy.sh              # custom CREATE2 salt
#   COWBOT_PRIVATE_KEY=/path/key ./deploy.sh   # custom signing key
#
# Required env (no defaults):
#   COWBOT_PRIVATE_KEY or COWBOY_PRIVATE_KEY    path to deployer's hex private key
# Optional env:
#   COWBOT_SALT                                 CREATE2 salt (default: 0xc0)
#   COWBOT_KNOWLEDGE_VOLUME                     read-only volume name (default: cowboyinc)
#   COWBOT_STATE_VOLUME                         read-write volume name (default: cowbot-state)
#   COWBOT_RPC_URL                              RPC URL override (else CLI auto-discovers)
#   COWBOT_SLACK_BOT_TOKEN                      xoxb-… ; provisioned as a CBSS secret ACL'd to the actor
#   COWBOT_SLACK_SIGNING_SECRET                 Slack signing secret ; provisioned as a CBSS secret ACL'd to the actor
#                                               (both optional: if unset, set them later with `cowboy secrets set`)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COWBOT_PY="${SCRIPT_DIR}/actors/cowbot/main.py"

# CIP-9 §4.3 cross-owner private-volume mount authorization:
# the dispatcher only honors mounts of someone else's private volume when
# the actor's address is on the volume's `mount_allowed_actors` allowlist.
# Step 4 below computes the deterministic actor address from the cowbot target and
# then calls `cowboy volume allow-actor` against each volume — using
# $COWBOT_PRIVATE_KEY, which MUST be the volume owner's key (mesa wallet
# 0x2e6d152df70a0d0c9ccbf45cc0a9d5c0efb8c258 in the demo) for the
# delegation-cert auth on `/ras/volumes/{id}/mount-allowlist` to pass.
# No manifest grant is needed; the volume owner controls the list.

RPC_URL="${1:-${COWBOT_RPC_URL:-}}"
SALT="${COWBOT_SALT:-0xc0}"
PRIVATE_KEY="${COWBOT_PRIVATE_KEY:-${COWBOY_PRIVATE_KEY:-}}"
KNOWLEDGE_VOLUME="${COWBOT_KNOWLEDGE_VOLUME:-cowboyinc}"
STATE_VOLUME="${COWBOT_STATE_VOLUME:-cowbot-state}"
VOLUME_CONFIG_JSON="$(printf '{"knowledge_volume":"%s","state_volume":"%s"}' "$KNOWLEDGE_VOLUME" "$STATE_VOLUME")"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ── Step 1: prereqs ──────────────────────────────────────────────────────────
COWBOY="${COWBOY_BIN:-cowboy}"
command -v "$COWBOY" >/dev/null 2>&1 || fail "cowboy CLI not found (set COWBOY_BIN or put cowboy on PATH)"
[[ -f "$COWBOT_PY" ]] || fail "actor source not found at $COWBOT_PY"
# The CLI discovers the cowbot target from the nearest cowboy.toml, so run
# target commands from the example directory.
cd "$SCRIPT_DIR"
[[ -n "$PRIVATE_KEY" ]] || fail "set COWBOT_PRIVATE_KEY or COWBOY_PRIVATE_KEY"
[[ -f "$PRIVATE_KEY" ]] || fail "private key file not found: $PRIVATE_KEY"

# Build the indexer-url flag once; passed to every subcommand that talks to chain.
RPC_FLAG=()
if [[ -n "$RPC_URL" ]]; then
    RPC_FLAG=(--indexer-url "$RPC_URL")
    # actor deploy/execute read COWBOY_RPC_URL (they take no --rpc-url flag)
    export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
    info "RPC = $RPC_URL"
else
    info "RPC = (auto-discovered from .cowboy/config.json)"
fi

# ── Step 2: derive the deployer EOA (creator, CBSS secret + volume owner) ────
# Actors have no wallet, so the CREATE2 derivation, the manifest's
# cross-account secret refs, and the `cowboy secrets set` ACL all bind to the
# DEPLOYER's address. Manifest keys use the canonical lowercase-no-0x form
# (CIP-24 §5.5; see manifest.README).
OWNER_ADDR="$("$COWBOY" wallet address --key "$PRIVATE_KEY" 2>/dev/null | tr -d '[:space:]')"
[[ -n "$OWNER_ADDR" ]] || fail "could not derive deployer address (cowboy wallet address failed)"
OWNER_LC="$(printf '%s' "${OWNER_ADDR#0x}" | tr 'A-F' 'a-f')"
info "deployer EOA = $OWNER_ADDR (creator + secret owner)"

# ── Step 2b: compute deterministic actor address ─────────────────────────────
info "computing deterministic actor address (CREATE2)"
ACTOR_ADDR="$("$COWBOY" actor address cowbot \
    --creator "$OWNER_ADDR" \
    --salt "$SALT" 2>/dev/null | awk '/Actor address:/ {print $NF}')"

[[ -n "$ACTOR_ADDR" ]] || fail "actor address derivation returned empty (cowboy actor address failed)"
info "actor address = $ACTOR_ADDR"

# ── Step 3: deploy if not already on chain ───────────────────────────────────
EXISTING="$("$COWBOY" "${RPC_FLAG[@]}" actor get --address "$ACTOR_ADDR" 2>&1 || true)"
if echo "$EXISTING" | grep -qE 'code_hash|state_root'; then
    ok "actor already deployed; skipping deploy step"
else
    # Render the entitlement manifest: substitute the deployer EOA into the
    # cross-account secret refs (OWNER_EOA placeholder → lowercase-no-0x). Without
    # this the actor deploys with NO entitlements and the first runner op fails
    # admission/release. See manifest.README.
    MANIFEST_SRC="${SCRIPT_DIR}/manifest.json"
    MANIFEST_DEPLOY="${SCRIPT_DIR}/manifest.deploy.json"
    [[ -f "$MANIFEST_SRC" ]] || fail "manifest.json not found at $MANIFEST_SRC"
    sed "s/OWNER_EOA/${OWNER_LC}/g" "$MANIFEST_SRC" > "$MANIFEST_DEPLOY" \
        || fail "manifest render failed"
    info "rendered manifest with owner=$OWNER_LC → manifest.deploy.json"

    info "deploying the cowbot target at salt=$SALT"
    "$COWBOY" "${RPC_FLAG[@]}" actor deploy cowbot \
        --salt "$SALT" \
        --manifest-json "$MANIFEST_DEPLOY" \
        --init-payload "$VOLUME_CONFIG_JSON" \
        --private-key "$PRIVATE_KEY" \
        || fail "actor deploy failed"
    ok "actor deployed at $ACTOR_ADDR"
fi

# Existing deployments may predate configurable volume names, and operators
# often change the private knowledge volume name during bring-up. Keep actor
# storage aligned with the env every time the script runs.
info "configuring cowbot volume names"
"$COWBOY" "${RPC_FLAG[@]}" actor execute \
    --actor "$ACTOR_ADDR" \
    --handler set_volumes \
    --payload-json "$VOLUME_CONFIG_JSON" \
    --private-key "$PRIVATE_KEY" \
    >/dev/null \
    || fail "set_volumes failed"
ok "cowbot volume config set"

# ── Step 4: volume existence check ───────────────────────────────────────────
# CBFS volumes are provisioned outside this deploy script. We only detect
# existence here and prompt the operator if one is missing.
check_volume() {
    local name="$1"
    local mode="$2"
    if "$COWBOY" "${RPC_FLAG[@]}" volume info --name "$name" >/dev/null 2>&1; then
        ok "volume \"$name\" ($mode) exists on chain"
        return 0
    fi
    warn "volume \"$name\" ($mode) does NOT exist on chain"
    return 1
}

KNOWLEDGE_OK=0
STATE_OK=0
check_volume "$KNOWLEDGE_VOLUME" "ro" && KNOWLEDGE_OK=1
check_volume "$STATE_VOLUME"     "rw" && STATE_OK=1

# ── Step 4b: mount allowlist ─────────────────────────────────────────────────
# Authorize this actor to mount each existing private volume. The
# `mount-allowlist` endpoint is owner-authenticated, so $COWBOT_PRIVATE_KEY
# must belong to the volume owner. Idempotent: re-adds are no-ops because
# the chain de-dupes via BTreeSet. Skipped for volumes that don't exist
# yet — provision them first, then re-run this script.
allow_actor() {
    local vol="$1"
    info "adding $ACTOR_ADDR to mount allowlist for volume \"$vol\""
    "$COWBOY" "${RPC_FLAG[@]}" volume \
        --private-key "$PRIVATE_KEY" \
        allow-actor \
        --name "$vol" \
        --actor "$ACTOR_ADDR" \
        || fail "volume allow-actor failed for $vol"
    ok "allowlist updated for \"$vol\""
}
[[ "$KNOWLEDGE_OK" -eq 1 ]] && allow_actor "$KNOWLEDGE_VOLUME"
[[ "$STATE_OK"     -eq 1 ]] && allow_actor "$STATE_VOLUME"

# ── Step 4c: provision the two CBSS secrets, ACL'd to the new actor ───────────
# The actor holds ZERO plaintext secrets (CIP-24 §5.5). SLACK_SIGNING_SECRET
# (runner verify=) and SLACK_BOT_TOKEN (runner secrets= substitution) are CBSS
# secrets owned by the deployer EOA and released to this actor at runtime. We set
# them owned by $PRIVATE_KEY and ACL'd to $ACTOR_ADDR. Values come from env; we
# warn (not fail) if unset so the actor + manifest can still deploy without them.
#
# NOTE: the `secrets` group takes --rpc-url (not the global --indexer-url) and
# --private-key as group-level flags before `set`; the read ACL flag is --actor
# (repeatable). Values are piped via --value-file - (stdin) — the inline form is
# refused on a tty and would leak into shell history.
SECRETS_RPC_FLAG=()
[[ -n "$RPC_URL" ]] && SECRETS_RPC_FLAG=(--rpc-url "$RPC_URL")

set_cbss_secret() {
    local key="$1"
    local value="$2"
    info "setting CBSS secret $key (owner=$OWNER_ADDR, actor=$ACTOR_ADDR)"
    printf '%s' "$value" | "$COWBOY" secrets \
        "${SECRETS_RPC_FLAG[@]}" \
        --private-key "$PRIVATE_KEY" \
        set "$key" \
        --value-file - \
        --actor "$ACTOR_ADDR" \
        || fail "cowboy secrets set $key failed"
    ok "CBSS secret $key set and ACL'd to $ACTOR_ADDR"
}

if [[ -n "${COWBOT_SLACK_BOT_TOKEN:-}" && -n "${COWBOT_SLACK_SIGNING_SECRET:-}" ]]; then
    set_cbss_secret SLACK_BOT_TOKEN      "$COWBOT_SLACK_BOT_TOKEN"
    set_cbss_secret SLACK_SIGNING_SECRET "$COWBOT_SLACK_SIGNING_SECRET"
else
    warn "COWBOT_SLACK_BOT_TOKEN / COWBOT_SLACK_SIGNING_SECRET not set — skipping CBSS secret provisioning."
    warn "cowbot will NOT work until you set both secrets owned by the deployer EOA and ACL'd to the actor:"
    warn "  cowboy secrets ${SECRETS_RPC_FLAG[*]} --private-key <owner-key> set SLACK_BOT_TOKEN      --value-file - --actor $ACTOR_ADDR"
    warn "  cowboy secrets ${SECRETS_RPC_FLAG[*]} --private-key <owner-key> set SLACK_SIGNING_SECRET --value-file - --actor $ACTOR_ADDR"
fi

# ── Step 5: print operator next-steps ────────────────────────────────────────
echo
ok "─── cowbot deploy complete ───"
echo "  actor address       : $ACTOR_ADDR"
echo "  knowledge volume    : $KNOWLEDGE_VOLUME (ro)  exists=$KNOWLEDGE_OK"
echo "  state volume        : $STATE_VOLUME (rw)  exists=$STATE_OK"
echo

if [[ "$KNOWLEDGE_OK" -eq 0 || "$STATE_OK" -eq 0 ]]; then
    cat <<'EOF'
Missing CBFS volumes.
Prerequisites for the create/put commands below (all cbfs writes):
  - export COWBOY_RAS_WRITE_RELAYER_API_KEY=...   (writes 401 without it)
  - one-time identity:  cbfs --state-dir <dir> auth setup --rpc-url <rpc> \
        --wallet-key <deployer-key> --no-passphrase
  - export CBFS_OWNER_KEY_FILE=<deployer-key>     (needed by --owner-key)
  - private creates need the deployer account's CBSS release key; if
    GET <rpc>/cbss/account-release-key/<deployer> has no mpk_g2 yet, run
    `cowboy secrets --private-key <key> request-account-dkg` and wait.
Always pass --initial-escrow: volumes created with zero escrow expire
after one epoch.

Create the knowledge volume with owner-key dual wrap so this laptop can
bulk-load it while runners still read through CBSS release:

  cbfs volume create --name <knowledge-volume> --owner-key --initial-escrow 1
  cbfs put-many <knowledge-volume> --from-json cowbot-files.json

The cowbot-state volume can stay committee-only because runner.agent
writes it from the selected runner:

  cbfs volume create --name <state-volume> --initial-escrow 1
EOF
fi

cat <<EOF

To finish wiring up cowbot:

  1. CBSS secrets (Slack bot token + signing secret). If you exported
     COWBOT_SLACK_BOT_TOKEN / COWBOT_SLACK_SIGNING_SECRET this script
     already provisioned them above. Otherwise set them now — owned by the
     deployer EOA and ACL'd to this actor (the actor stores NEITHER; the
     runner threshold-decrypts them per job, CIP-24 §5.5):
         cowboy secrets ${SECRETS_RPC_FLAG[*]} --private-key "$PRIVATE_KEY" \\
             set SLACK_BOT_TOKEN      --value-file - --actor $ACTOR_ADDR   # paste xoxb-… then Ctrl-D
         cowboy secrets ${SECRETS_RPC_FLAG[*]} --private-key "$PRIVATE_KEY" \\
             set SLACK_SIGNING_SECRET --value-file - --actor $ACTOR_ADDR   # paste signing secret then Ctrl-D

  2. Inference key on the runner host(s) — the LLM key is NOT a CBSS
     secret; set it where the runner.agent job executes:
         export MOONSHOT_API_KEY=sk-...        # or OPENAI_API_BASE for self-hosted

  3. Start the gateway (separate machine or terminal). It is untrusted
     plumbing — it forwards body + headers verbatim and holds NO secret:
         ./gateway/bootstrap-gateway.sh
         COWBOT_ACTOR=$ACTOR_ADDR ./gateway/bootstrap-gateway-mac.sh

  4. Expose the gateway publicly with your preferred HTTPS ingress, then
     paste the public URL into the Slack app's Event Subscriptions
     Request URL field:
         https://<gateway-host>/$ACTOR_ADDR/slack/events
     The runner MAC-verifies the inbound webhook (SLACK_SIGNING_SECRET) and
     substitutes the bot token (SLACK_BOT_TOKEN) on the outbound post. Slack
     sends a url_verification challenge; the actor answers it inline.

  5. Smoke test:
         @cowbot what does CIP-15 say about route caching?

EOF
