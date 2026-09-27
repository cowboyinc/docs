#!/usr/bin/env bash
# deploy.sh — creator-side Twilio actor deploy + Trading Post declaration.
#
# Idempotence follows cowbot's deploy.sh: deterministic CREATE2 address,
# skip-if-exists on volumes, re-runnable.
#
# Steps:
#   1. compute the deterministic twilio actor address
#   2. create + load the PRIVATE artifact volume ("the brain"), allow-actor
#   3. deploy twilio (manifest = storage.kv only; zero secrets)
#   4. install the CIP-15 routes table (GET /health, POST /twilio/sms)
#   5. escrow: `cowboy secrets set tradingpost/<decl>/<ver>/<artifact>`
#      produces the CBSS envelope reused as the declare's ibe-ciphertext
#   6. `cowboy trading-post declare` (Free term, Private code)
#   7. `twilio.set_declaration` records the pin for hire-bound jobs
#
# Volume writes go through the cbfs CLI (the cowboy CLI has no volume
# create/put verbs). Private volume creation needs the creator account's
# CBSS release key; this script requests the account DKG when it is absent.
#
# Required env:
#   TWILIO_PRIVATE_KEY     path to creator's hex private key file
#   TWILIO_WEBHOOK_URL     the EXACT public URL Twilio will POST to (it is the
#                          first field of the signature preimage — see README)
#   COWBOY_RAS_WRITE_RELAYER_API_KEY  relayer key — every cbfs write 401s
#                             without it (reads need nothing)
# Optional env:
#   TWILIO_SALT            CREATE2 salt (default 0x4a11)
#   TWILIO_DECL_SALT       declaration salt, 32 bytes (default
#                             0x4a12 right-padded — matches e2e-local.sh;
#                             changing padding changes the declaration id)
#   TWILIO_ARTIFACT_VOLUME volume name (default twilio-brain)
#   TWILIO_ARTIFACT_ESCROW_CBY  volume rent escrow (default 1; volumes
#                             with zero escrow expire after one epoch)
#   TWILIO_MODEL           agent model (default local)
#   TWILIO_RPC_URL         RPC URL override (falls back to COWBOY_RPC_URL)
#   CBFS_BIN                  cbfs binary (default: cbfs on PATH); MUST be
#                             wire-lockstepped with the target chain's node era
#   CBFS_STATE                cbfs state dir (default .cbfs-state next to this
#                             script). CI should use a fresh dir per run so a
#                             stale/expired delegation is never trusted.
#   CBFS_OWNER_KEY_FILE       owner wallet key for the volume DEK wrap
#                             (defaults to TWILIO_PRIVATE_KEY)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"

RPC_URL="${1:-${TWILIO_RPC_URL:-${COWBOY_RPC_URL:-}}}"
SALT="${TWILIO_SALT:-0x4a11}"
DECL_SALT="${TWILIO_DECL_SALT:-0x4a12000000000000000000000000000000000000000000000000000000000000}"
PRIVATE_KEY="${TWILIO_PRIVATE_KEY:-${COWBOY_PRIVATE_KEY:-}}"
ARTIFACT_VOLUME="${TWILIO_ARTIFACT_VOLUME:-twilio-brain}"
ARTIFACT_ESCROW_CBY="${TWILIO_ARTIFACT_ESCROW_CBY:-1}"
MODEL="${TWILIO_MODEL:-local}"
WEBHOOK_URL="${TWILIO_WEBHOOK_URL:-}"
CBFS_BIN="${CBFS_BIN:-cbfs}"
CBFS_STATE="${CBFS_STATE:-${SCRIPT_DIR}/.cbfs-state}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

command -v cowboy >/dev/null 2>&1 || fail "cowboy CLI not on PATH"
command -v "$CBFS_BIN" >/dev/null 2>&1 || fail "cbfs CLI not on PATH (set CBFS_BIN)"
[[ -f "actors/twilio/main.py" ]] || fail "actors/twilio/main.py not found"
[[ -n "$PRIVATE_KEY" && -f "$PRIVATE_KEY" ]] || fail "set TWILIO_PRIVATE_KEY to the creator key file"
[[ -n "$WEBHOOK_URL" ]] || fail "set TWILIO_WEBHOOK_URL (the exact public webhook URL Twilio signs over)"
# Remote relayers 401 unauthenticated writes; a loopback relayer (local
# devnet) needs no key, so only require one for non-local relayers.
case "${COWBOY_RAS_WRITE_RELAYER_URL:-}" in
    *127.0.0.1*|*localhost*) ;;
    *) [[ -n "${COWBOY_RAS_WRITE_RELAYER_API_KEY:-}" ]] || fail "set COWBOY_RAS_WRITE_RELAYER_API_KEY (remote cbfs writes 401 without it)" ;;
esac
[[ "$ARTIFACT_ESCROW_CBY" =~ ^[0-9]+(\.[0-9]{1,9})?$ && "$ARTIFACT_ESCROW_CBY" != 0 ]] \
    || fail "TWILIO_ARTIFACT_ESCROW_CBY must be a positive CBY amount (zero-escrow volumes expire after one epoch)"
export CBFS_OWNER_KEY_FILE="${CBFS_OWNER_KEY_FILE:-$PRIVATE_KEY}"

# REST base for chain reads; cowboy/cbfs flags only when an RPC was given
# (local dev discovers endpoints from .cowboy/config.json).
REST_URL="${RPC_URL:-http://127.0.0.1:4000}"
COWBOY_TOP=(cowboy)
GROUP_RPC=()
CBFS=("$CBFS_BIN" --state-dir "$CBFS_STATE")
if [[ -n "$RPC_URL" ]]; then
    COWBOY_TOP+=(--indexer-url "$RPC_URL")
    GROUP_RPC=(--rpc-url "$RPC_URL")
    CBFS+=(--rpc-url "$RPC_URL")
    export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"
fi

# Retry wrapper for MUTATING submissions (same as e2e-local.sh): the CLI reads
# the account nonce at call time, so back-to-back same-sender txs race finality
# and land as nonce_too_low; the node also surfaces transient 2017/unavailable.
cw() {
    local out rc attempt
    for attempt in 1 2 3 4 5; do
        if out="$("$@" 2>&1)"; then rc=0; else rc=$?; fi
        if [ "$rc" -eq 0 ] && ! grep -q "nonce_too_low" <<<"$out"; then
            printf '%s\n' "$out"; return 0
        fi
        if grep -qE "2017|Service Unavailable|should be retried|nonce_too_low|Finality advanced" <<<"$out"; then
            sleep 2; continue
        fi
        printf '%s\n' "$out"; return "$rc"
    done
    printf '%s\n' "$out"; return 1
}

CREATOR_ADDR="$(cowboy wallet address --key "$PRIVATE_KEY")"
info "creator = $CREATOR_ADDR"

# ── 1. deterministic actor address ───────────────────────────────────────────
ACTOR_ADDR="$(cowboy actor address twilio \
    --creator "$CREATOR_ADDR" \
    --salt "$SALT" \
    | python3 -c '
import re, sys
m = re.findall(r"Actor address:\s*(?:0x)?([0-9a-fA-F]{40})", sys.stdin.read())
print(("0x" + m[-1]) if m else "")
')"
[[ -n "$ACTOR_ADDR" ]] || fail "could not compute actor address"
info "twilio actor = $ACTOR_ADDR"

# ── 2a. cbfs write identity + CBSS release key ───────────────────────────────
# auth setup registers the delegated CBFS identity through the relayer (hence
# the API-key preflight above). `auth status` proves only that a LOCAL
# identity file exists — it validates neither on-chain registration after a
# re-genesis nor cert expiry — so register unconditionally unless the caller
# already prepared this CBFS_STATE in this run (the baseline pipeline sets
# CBFS_AUTH_PREPARED=1 after its own fresh-state setup).
if [[ "${CBFS_AUTH_PREPARED:-0}" != 1 ]]; then
    info "registering CBFS delegation identity (state: $CBFS_STATE)"
    "$CBFS_BIN" --state-dir "$CBFS_STATE" auth setup ${RPC_URL:+--rpc-url "$RPC_URL"} \
        --wallet-key "$PRIVATE_KEY" --no-passphrase
fi

# Private volume creation IBE-wraps the DEK to the creator account's CBSS
# release key and fails closed when it is absent; request the account DKG and
# poll the same endpoint cbfs reads (pattern: examples/35-cbfs-private-volume).
OWNER_LC="$(tr '[:upper:]' '[:lower:]' <<<"$CREATOR_ADDR")"
if ! curl -s "$REST_URL/cbss/account-release-key/$OWNER_LC" | grep -q mpk_g2; then
    info "requesting account-scoped CBSS DKG (release key absent)"
    cw cowboy secrets "${GROUP_RPC[@]}" --private-key "$PRIVATE_KEY" request-account-dkg \
        || fail "request-account-dkg submission failed"
    DKG_READY=0
    for _ in $(seq 1 60); do
        curl -s "$REST_URL/cbss/account-release-key/$OWNER_LC" | grep -q mpk_g2 && { DKG_READY=1; break; }
        sleep 5
    done
    [[ "$DKG_READY" == 1 ]] || fail "account release key never appeared (CBSS committee up?)"
fi
ok "CBSS release key present for creator"

# ── 2b. artifact volume ("the brain") ────────────────────────────────────────
# Chain-truth lookup: (owner, name) row from the RAS index. Prints
# "volume_id manifest_root used_bytes" (bare hex) or nothing when absent.
# Exit 3 = duplicate (owner, name) rows; any other nonzero exit is a
# transient fetch/parse failure (e.g. a 502 HTML body) — callers must not
# conflate the two.
volume_row() {
    curl -s "$REST_URL/ras/chain/volumes" | python3 -c '
import json, sys
owner, name = sys.argv[1].lower(), sys.argv[2]
rows = [v for v in json.load(sys.stdin)
        if v.get("owner", "").lower() == owner and v.get("volume_name") == name]
if len(rows) > 1:
    sys.exit(3)
if rows:
    v = rows[0]
    h = lambda b: "".join(f"{x:02x}" for x in b)
    print(h(v["volume_id"]), h(v["manifest_root"]),
          v.get("used_bytes") or v.get("effective_size_bytes") or 0)
' "$CREATOR_ADDR" "$ARTIFACT_VOLUME"
}

row_rc=0
ROW="$(volume_row)" || row_rc=$?
[[ "$row_rc" -eq 3 ]] && fail "multiple chain volumes match (owner, $ARTIFACT_VOLUME) — resolve manually"
[[ "$row_rc" -eq 0 ]] || fail "could not read $REST_URL/ras/chain/volumes (transient indexer error?) — re-run"
if [[ -z "$ROW" ]]; then
    info "creating private artifact volume $ARTIFACT_VOLUME (escrow $ARTIFACT_ESCROW_CBY CBY)"
    "${CBFS[@]}" volume create --name "$ARTIFACT_VOLUME" --owner-key \
        --initial-escrow "$ARTIFACT_ESCROW_CBY"
else
    ok "volume $ARTIFACT_VOLUME exists"
fi
info "loading brain content"
"${CBFS[@]}" put "$ARTIFACT_VOLUME" "${SCRIPT_DIR}/system_prompt.txt" twilio/system_prompt.txt
# The capability schema DOCUMENT lives in the artifact volume; the on-chain
# declaration stores only a short VolumePath pointer to it (CIP-33 §2.6.1).
"${CBFS[@]}" put "$ARTIFACT_VOLUME" "${SCRIPT_DIR}/capability-schema.json" twilio/capability-schema.json
cw cowboy volume "${GROUP_RPC[@]}" --private-key "$PRIVATE_KEY" \
    allow-actor --name "$ARTIFACT_VOLUME" --actor "$ACTOR_ADDR" \
    || warn "allow-actor may already be set"

# Read id + manifest root AFTER the final put (declare pins --claimed-root to
# exactly these bytes): discover the id from the (owner, name) row, then poll
# the per-id route (bare 64-hex, no 0x) until the committed writes are visible
# as nonzero used bytes.
VOLUME_ID=""
for _ in $(seq 1 12); do
    row_rc=0
    ROW="$(volume_row)" || row_rc=$?
    [[ "$row_rc" -eq 3 ]] && fail "multiple chain volumes match (owner, $ARTIFACT_VOLUME) — resolve manually"
    if [[ "$row_rc" -eq 0 && -n "$ROW" ]]; then
        VOLUME_ID="$(cut -d' ' -f1 <<<"$ROW")"
        break
    fi
    sleep 5
done
[[ -n "$VOLUME_ID" ]] || fail "volume row for $ARTIFACT_VOLUME never appeared on chain"
MANIFEST_ROOT=""
for _ in $(seq 1 12); do
    ROW="$(curl -s "$REST_URL/ras/chain/volumes/$VOLUME_ID" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(v, dict) or "manifest_root" not in v:
    sys.exit(0)
h = lambda b: "".join(f"{x:02x}" for x in b)
print(h(v["manifest_root"]), v.get("used_bytes") or v.get("effective_size_bytes") or 0)
')" || ROW=""
    if [[ -n "$ROW" ]]; then
        read -r MANIFEST_ROOT USED_BYTES <<<"$ROW"
        [[ "${USED_BYTES:-0}" != "0" ]] && break
    fi
    sleep 5
done
[[ -n "$MANIFEST_ROOT" && "${USED_BYTES:-0}" != "0" ]] \
    || fail "volume $ARTIFACT_VOLUME shows no committed bytes after puts"
VOLUME_ID="0x$VOLUME_ID"
MANIFEST_ROOT="0x$MANIFEST_ROOT"
ok "volume $VOLUME_ID committed (manifest root $MANIFEST_ROOT)"

# ── 3. deploy twilio ──────────────────────────────────────────────────────
INIT_JSON="$(printf '{"artifact_volume":"%s","model":"%s","webhook_url":"%s"}' \
    "$ARTIFACT_VOLUME" "$MODEL" "$WEBHOOK_URL")"
if curl -sf "$REST_URL/actor/$ACTOR_ADDR" >/dev/null 2>&1; then
    ok "actor already deployed"
else
    cw "${COWBOY_TOP[@]}" actor deploy twilio \
        --manifest-json "${SCRIPT_DIR}/manifest.json" \
        --init-payload "$INIT_JSON" \
        --private-key "$PRIVATE_KEY" \
        --salt "$SALT"
    ok "deployed twilio at $ACTOR_ADDR"
fi

# ── 4. routes ────────────────────────────────────────────────────────────────
python3 "${SCRIPT_DIR}/tools/emit_routes.py"
cw "${COWBOY_TOP[@]}" actor execute --actor "$ACTOR_ADDR" --handler set_routes \
    --payload "@${SCRIPT_DIR}/tools/routes.cbor" \
    --private-key "$PRIVATE_KEY"
ok "routes installed"

# ── 5. Trading Post escrow + declaration ─────────────────────────────────────
DECLARATION_ID="$(cowboy trading-post "${GROUP_RPC[@]}" declaration-id \
    --creator "$CREATOR_ADDR" --actor "$ACTOR_ADDR" --salt "$DECL_SALT" \
    | python3 -c 'import re,sys; m=re.findall(r"Declaration ID:\s*(0x[0-9a-fA-F]{64})", sys.stdin.read()); print(m[-1] if m else "")')"
[[ -n "$DECLARATION_ID" ]] || fail "could not compute declaration id"
VERSION=1
ARTIFACT_ID=0
TP_LABEL="tradingpost/${DECLARATION_ID}/${VERSION}/${ARTIFACT_ID}"
info "declaration = $DECLARATION_ID ; escrow label = $TP_LABEL"

# The escrowed plaintext is the artifact key material. v1 note: the CBSS
# envelope is REAL (threshold-encrypted to the committee, lease-gated on
# release); binding it cryptographically to the serving volume's DEK is the
# CIP-33 §4 artifact-equivalence claim, enforced socially in v1.
ESCROW_PLAINTEXT="$(mktemp)"
trap 'rm -f "$ESCROW_PLAINTEXT"' EXIT
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$ESCROW_PLAINTEXT"
ESCROW_ENVELOPE="${SCRIPT_DIR}/.twilio-escrow-envelope.json"
cw cowboy secrets "${GROUP_RPC[@]}" --private-key "$PRIVATE_KEY" set "$TP_LABEL" \
    --value-file "$ESCROW_PLAINTEXT" \
    --cbfs-output "$ESCROW_ENVELOPE"
rm -f "$ESCROW_PLAINTEXT"
ok "escrow envelope at $ESCROW_ENVELOPE"

# capability_schema is a ≤1 KiB VolumePath pointer (CIP-33 §2.6.1); the doc
# was loaded into the volume above. Store the pointer, not the JSON.
CAP_POINTER="${SCRIPT_DIR}/.cap-schema-pointer.txt"
printf 'twilio/capability-schema.json' > "$CAP_POINTER"
if cowboy trading-post "${GROUP_RPC[@]}" get-declaration --declaration-id "$DECLARATION_ID" >/dev/null 2>&1; then
    ok "declaration already exists"
else
    cw cowboy trading-post "${GROUP_RPC[@]}" --private-key "$PRIVATE_KEY" declare \
        --actor "$ACTOR_ADDR" \
        --salt "$DECL_SALT" \
        --human-hireable \
        --manifest-root "$MANIFEST_ROOT" \
        --code-visibility private \
        --capability-schema-file "$CAP_POINTER" \
        --term free \
        --split "${CREATOR_ADDR}:10000" \
        --volume-id "$VOLUME_ID" \
        --claimed-root "$MANIFEST_ROOT" \
        --ibe-ciphertext-file "$ESCROW_ENVELOPE"
    ok "declared"
fi

# ── 6. record the pin on the actor ───────────────────────────────────────────
DECL_JSON="$(printf '{"declaration_id":"%s","version":%s,"artifact_id":%s}' \
    "$DECLARATION_ID" "$VERSION" "$ARTIFACT_ID")"
cw "${COWBOY_TOP[@]}" actor execute --actor "$ACTOR_ADDR" --handler set_declaration \
    --payload-json "$DECL_JSON" \
    --private-key "$PRIVATE_KEY"
ok "declaration pin recorded on actor"

echo
ok "Twilio actor deployed."
echo "  actor          = $ACTOR_ADDR"
echo "  declaration_id = $DECLARATION_ID"
echo "  artifact vol   = $ARTIFACT_VOLUME ($VOLUME_ID)"
echo "  webhook        = POST /twilio/sms (via gateway host mapped to $ACTOR_ADDR)"
echo "  signed URL     = $WEBHOOK_URL (must match your Twilio number's webhook exactly)"
echo "Next: submit to the General Store (store-admin), or direct-hire + connect-workspace.sh"
