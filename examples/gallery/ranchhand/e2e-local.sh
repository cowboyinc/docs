#!/usr/bin/env bash
# e2e-local.sh — Ranchhand end-to-end on a local devnet.
#
# Proves every NEW mechanic of the hireable Slack actor, live:
#   declare (Free+Private, real CBSS escrow) → direct hire → hirer onboarding
#   (secrets + credential proxy + ACL + connect_workspace) → synthetic Slack
#   event with a REAL v0 HMAC → gateway-shaped dispatch → proxy verify= MAC on
#   the runner → hire-bound runner.agent admitted through the PVM hire= kwarg
#   → lease-authorize allow → answer posted via the hirer's token → forged-MAC
#   rejection → revoke_hire → next event's agent job refused at admission.
#
# NOT covered locally (proven cowbot machinery, exercised on devnet instead):
# CBFS volume mounts (runner-storage FUSE) and the CIP-15 gateway hop —
# slack_http is invoked directly with the same HttpRequestEnvelope the
# gateway would submit.
#
# Requirements (branch stack):
#   node    = cbd/cip33-runner-op-hire-binding   (worktree ~/dev/cowboy/node-cip33)
#   runner  = cbd/cip33-ranchhand-e2e            (worktree ~/dev/cowboy/runner-ranchhand)
#   cbss    = cbd/cip33-cbss-authorizer          (checkout  ~/dev/cowboy/cbss)
#   cbfs    = main
# plus an E2E_ROOT dir of symlinks {node,runner,cbfs,cbss} → those checkouts
# (default: auto-created under /tmp). The cbss harness supplies the cluster:
# validator + relays + cbssd committee + runner, left alive via
# CBSS_E2E_KEEP_ALIVE=1.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Targets are project-relative: the CLI discovers them from the nearest
# cowboy.toml, so run everything from the example directory.
cd "$SCRIPT_DIR"
WORKSPACE="${COWBOY_WORKSPACE:-$HOME/dev/cowboy}"
E2E_ROOT="${E2E_ROOT:-/tmp/ranchhand-e2e-root}"
RPC_URL="http://127.0.0.1:4000"
SLACK_STUB_PORT=9877
OPENAI_STUB_PORT=9878
SLACK_STUB_LOG="/tmp/ranchhand-e2e-slack-stub.log"
OPENAI_STUB_LOG="/tmp/ranchhand-e2e-openai-stub.log"
E2E_DIR="/tmp/ranchhand-e2e"
EXPECTED_ANSWER="HOWDY-E2E-ANSWER"

log()  { echo ""; echo "▸ $1"; }
ok()   { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; tail -30 /tmp/cbss-e2e-runner.log 2>/dev/null | sed 's/^/    | /'; exit 1; }

STUB_PIDS=()
cleanup() {
    echo ""
    echo "=== ranchhand e2e cleanup ==="
    for pid in "${STUB_PIDS[@]+"${STUB_PIDS[@]}"}"; do kill "$pid" 2>/dev/null || true; done
    if [ "${RANCHHAND_E2E_KEEP_ALIVE:-0}" = "1" ]; then
        echo "  RANCHHAND_E2E_KEEP_ALIVE=1 — leaving cluster + harness (PID ${HARNESS_PID:-?}) alive."
        echo "  RPC http://127.0.0.1:4000 ; kill $HARNESS_PID + pkill -f ranchhand-e2e-root to tear down."
        return
    fi
    [ -n "${HARNESS_PID:-}" ] && kill "$HARNESS_PID" 2>/dev/null || true
    if true; then
        pkill -f "test-e2e-full-cbss"                       2>/dev/null || true
        pkill -f "$E2E_ROOT/node/target/release/validator"  2>/dev/null || true
        pkill -f "target/release/validator"                 2>/dev/null || true
        pkill -f "target/release/cbssd"                     2>/dev/null || true
        pkill -f "target/release/cbfs-node"                 2>/dev/null || true
        pkill -f "target/release/runner-node"               2>/dev/null || true
        pkill -f "cowboy-ras-write-relayer"                 2>/dev/null || true
    fi
}
trap cleanup EXIT

mkdir -p "$E2E_DIR"
rm -f "$SLACK_STUB_LOG" "$OPENAI_STUB_LOG"

# ── 0. symlink root (harness derives peer repos from its own location) ───────
if [ ! -e "$E2E_ROOT/cbss" ]; then
    mkdir -p "$E2E_ROOT"
    ln -sfn "${NODE_DIR:-$WORKSPACE/node-cip33}"        "$E2E_ROOT/node"
    ln -sfn "${RUNNER_DIR:-$WORKSPACE/runner-ranchhand}" "$E2E_ROOT/runner"
    ln -sfn "${CBFS_DIR:-$WORKSPACE/cbfs}"               "$E2E_ROOT/cbfs"
    ln -sfn "${CBSS_DIR:-$WORKSPACE/cbss}"               "$E2E_ROOT/cbss"
fi
COWBOY_BIN="${COWBOY_BIN:-$E2E_ROOT/node/target/release/cowboy}"

# ── 1. stubs (must exist before the harness spawns the runner) ───────────────
log "Starting Slack + OpenAI stubs"
python3 - "$SLACK_STUB_PORT" "$SLACK_STUB_LOG" <<'PY' &
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, logfile = int(sys.argv[1]), sys.argv[2]

class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(n).decode("utf-8", "replace")
        with open(logfile, "a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("authorization", ""), "body": body}) + "\n")
        if self.path.endswith("chat.postMessage"):
            resp = {"ok": True, "channel": "C0001", "ts": "1111.2222"}
        else:
            resp = {"ok": True}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):
        return

ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
PY
STUB_PIDS+=($!)

python3 - "$OPENAI_STUB_PORT" "$OPENAI_STUB_LOG" <<'PY' &
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, logfile = int(sys.argv[1]), sys.argv[2]

class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(n).decode("utf-8", "replace")
        with open(logfile, "a") as f:
            f.write(json.dumps({"path": self.path, "body": body[:2000]}) + "\n")
        resp = {
            "id": "cmpl-e2e", "object": "chat.completion", "model": "local",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "HOWDY-E2E-ANSWER"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):
        return

ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
PY
STUB_PIDS+=($!)
sleep 1
ok "stubs on :$SLACK_STUB_PORT (slack) and :$OPENAI_STUB_PORT (openai)"

# ── 2. cluster via the cbss harness (kept alive) ─────────────────────────────
log "Bringing up the local cluster (cbss harness, owner-secret scenario)"
export OPENAI_API_BASE="http://127.0.0.1:${OPENAI_STUB_PORT}/v1"
# The runner-agent "local" route reads its API key from SELF_HOSTED_LLM_KEY
# (or CBSS). The OpenAI stub ignores auth, so any non-empty value works.
export SELF_HOSTED_LLM_KEY="e2e-dummy-llm-key"
# The stubs and echo endpoints live on loopback; relax the runner's SSRF
# destination guard for this local run only.
export RUNNER_HTTP_ALLOW_PRIVATE_DESTINATIONS=1
export RUNNER_AGENT_LOCAL_MOUNTS="ranchhand-local:$E2E_DIR/agent-mounts"
mkdir -p "$E2E_DIR/agent-mounts"
# bringup mode: cluster + committee + owner AccountDKG + runner, no release
# scenario. The standalone owner-secret job is an EOA-submits-for-an-actor
# pattern #50 now forbids; ranchhand's own release path (proxy ACTOR submits
# its runner.http job, secret ACL'd to that actor) is the #50-correct proof
# and runs below.
#
# With CBSS_E2E_KEEP_ALIVE=1 the harness holds the cluster up in an infinite
# `sleep` after PASSED — it never returns. So run it in the BACKGROUND and
# proceed once it prints the hold message (cluster fully up). cleanup() kills
# the harness process group + the cluster on exit.
HARNESS_LOG="$E2E_DIR/harness-bringup.log"
CBSS_E2E_KEEP_ALIVE=1 "$E2E_ROOT/cbss/scripts/test-e2e-full-cbss.sh" local bringup \
    > "$HARNESS_LOG" 2>&1 &
HARNESS_PID=$!
log "Waiting for cluster bring-up (harness PID $HARNESS_PID; ≤10 min)…"
CLUSTER_UP=0
for _ in $(seq 1 200); do
    if grep -q "holding harness processes alive" "$HARNESS_LOG" 2>/dev/null; then CLUSTER_UP=1; break; fi
    if ! kill -0 "$HARNESS_PID" 2>/dev/null; then
        tail -25 "$HARNESS_LOG" | sed 's/^/    | /'
        fail "cbss harness exited before cluster was ready"
    fi
    sleep 3
done
[ "$CLUSTER_UP" = "1" ] || { tail -25 "$HARNESS_LOG" | sed 's/^/    | /'; fail "cluster bring-up timed out"; }
ok "cluster alive"

# Deterministic harness keys (same scheme as the harness; not secrets).
GENESIS_KEY_HEX="0000000000000000000000000000000000000000000000000000000000000001"
OWNER_KEY_HEX="0100000000000000000000000000000000000000000000000000000000000000"
CREATOR_KEY_FILE="$E2E_DIR/creator.key"; printf '%s' "$OWNER_KEY_HEX" > "$CREATOR_KEY_FILE"
HIRER_KEY_FILE="$E2E_DIR/hirer.key";     printf '%s' "$GENESIS_KEY_HEX" > "$HIRER_KEY_FILE"
CREATOR_ADDR="$("$COWBOY_BIN" wallet address --key "$CREATOR_KEY_FILE")"
HIRER_ADDR="$("$COWBOY_BIN" wallet address --key "$HIRER_KEY_FILE")"
ok "creator=$CREATOR_ADDR hirer=$HIRER_ADDR"

export COWBOY_RPC_URL="$RPC_URL"
CBSS_INGEST="http://127.0.0.1:9701"
SECRETS_ENV=(env "CBSS_INGEST_ENDPOINT=$CBSS_INGEST" "COWBOY_CHAIN_ID=9999")

# Retry a submission command on the node's transient admission races: code 2017
# ("admission should be retried" — finality advanced mid-validation) and
# nonce_too_low (a prior same-sender tx not yet finalized). Both are the node
# telling us to resubmit against fresher state. Writes combined output to $1.
retry_submit() {  # retry_submit <logfile> <cmd...>
    local logf="$1"; shift
    local attempt
    for attempt in 1 2 3 4 5; do
        if "$@" > "$logf" 2>&1; then
            if grep -q "nonce_too_low" "$logf"; then sleep 2; continue; fi
            return 0
        fi
        if grep -qE "2017|Service Unavailable|should be retried|nonce_too_low|Finality advanced" "$logf"; then
            sleep 2; continue
        fi
        return 1
    done
    return 1
}

# cw — run a cowboy command with the same transient-retry policy, ECHOING the
# final combined output so callers keep using `> logfile 2>&1` and `|| fail`.
# Wrap every MUTATING submission (the read-only get-*/address calls never hit
# the admission race and are left bare inside command substitutions).
cw() {
    local out rc attempt
    for attempt in 1 2 3 4 5; do
        out="$("$@" 2>&1)"; rc=$?
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

# Actors must hold CBY to pay for the DEFERRED txs their handlers create — the
# slack_http→proxy relay, the proxy's runner.http job submission, ranchhand's
# runner.agent submission, and the tenant_verified/post_answer callbacks are
# all actor-originated deferred txs, each charged to the originating actor.
# An unfunded actor's deferred tx is created but never included, silently
# stalling the async chain. Fund from genesis (seed-1 hirer key has balance).
fund_actor() {  # fund_actor <addr> [cby]
    local addr="$1" amt="${2:-100}"
    cw "$COWBOY_BIN" transfer --to "$addr" --amount "$amt" --private-key "$HIRER_KEY_FILE" \
        > "$E2E_DIR/fund-${addr}.log" 2>&1 \
        || { tail -3 "$E2E_DIR/fund-${addr}.log"; fail "funding $addr failed"; }
    for _ in $(seq 1 20); do
        local b
        b=$(curl -s "$RPC_URL/account/$addr" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('balance','0'))" 2>/dev/null || echo 0)
        [ "$b" != "0" ] && [ "$b" != "?" ] && return 0
        sleep 1
    done
    fail "funding $addr never landed"
}

addr_of() {  # addr_of <target> <creator> <salt>
    "$COWBOY_BIN" actor address "$1" --creator "$2" --salt "$3" \
        | python3 -c 'import re,sys; m=re.findall(r"Actor address:\s*(?:0x)?([0-9a-fA-F]{40})", sys.stdin.read()); print(("0x"+m[-1]) if m else "")'
}

# keccak256 needs pycryptodome; PEP 668 blocks a --user install, so use a
# script-local venv (created once, cached across runs).
VENV_PY="$E2E_DIR/venv/bin/python3"
if [ ! -x "$VENV_PY" ]; then
    python3 -m venv "$E2E_DIR/venv" >/dev/null
    "$E2E_DIR/venv/bin/pip" install --quiet pycryptodome >/dev/null
fi

secret_version() {  # secret_version <owner_addr> <key_name>
    OWNER="$1" KEY="$2" "$VENV_PY" - <<'PY'
import json, os, urllib.request
from Crypto.Hash import keccak
owner = os.environ["OWNER"].removeprefix("0x")
h = keccak.new(digest_bits=256); h.update(bytes.fromhex(owner)); h.update(os.environ["KEY"].encode())
url = f"{os.environ.get('COWBOY_RPC_URL','http://127.0.0.1:4000')}/cbss/secret/0x{owner}/0x{h.hexdigest()}"
d = json.load(urllib.request.urlopen(url))
print(d["version"], "0x" + h.hexdigest())
PY
}

# ── 3. creator: deploy ranchhand + declare (Free + Private) ──────────────────
log "Deploying ranchhand (mount_volumes=false for the local runner)"
RANCH_SALT="0x4a11"
RANCH_ADDR="$(addr_of ranchhand "$CREATOR_ADDR" "$RANCH_SALT")"
[ -n "$RANCH_ADDR" ] || fail "could not compute ranchhand address"
cw "$COWBOY_BIN" actor deploy ranchhand \
    --manifest-json "$SCRIPT_DIR/manifest.json" \
    --init-payload '{"mount_volumes": false, "model": "local"}' \
    --private-key "$CREATOR_KEY_FILE" \
    --salt "$RANCH_SALT" > "$E2E_DIR/ranchhand-deploy.log" 2>&1 \
    || { tail -10 "$E2E_DIR/ranchhand-deploy.log"; fail "ranchhand deploy failed"; }
sleep 2
ok "ranchhand at $RANCH_ADDR"
fund_actor "$RANCH_ADDR"
ok "ranchhand funded (deferred-tx gas)"

log "Declaring on the Trading Post (Free term, Private code, real escrow)"
# Declaration salt must be a full 32 bytes (the trading-post CLI rejects short
# salts, unlike `actor deploy` which left-pads). 0x4a12… padded to 64 hex.
DECL_SALT="0x4a12000000000000000000000000000000000000000000000000000000000000"
DECLARATION_ID="$("$COWBOY_BIN" trading-post declaration-id \
    --creator "$CREATOR_ADDR" --actor "$RANCH_ADDR" --salt "$DECL_SALT" \
    | python3 -c 'import re,sys; m=re.findall(r"Declaration ID:\s*(0x[0-9a-fA-F]{64})", sys.stdin.read()); print(m[-1] if m else "")')"
[ -n "$DECLARATION_ID" ] || fail "could not compute declaration id"
VERSION=1; ARTIFACT_ID=0
TP_LABEL="tradingpost/${DECLARATION_ID}/${VERSION}/${ARTIFACT_ID}"

python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$E2E_DIR/escrow-plain.txt"
retry_submit "$E2E_DIR/escrow.log" "${SECRETS_ENV[@]}" "$COWBOY_BIN" secrets --private-key "$CREATOR_KEY_FILE" \
    set "$TP_LABEL" --value-file "$E2E_DIR/escrow-plain.txt" \
    --cbfs-output "$E2E_DIR/escrow-envelope.json" \
    || { tail -10 "$E2E_DIR/escrow.log"; fail "escrow secret set failed"; }
sleep 2

MANIFEST_ROOT="$(TP_LABEL="$TP_LABEL" python3 -c 'import hashlib,os; print("0x"+hashlib.sha256(os.environ["TP_LABEL"].encode()).hexdigest())')"
VOLUME_ID="$(python3 -c "import secrets; print('0x'+secrets.token_hex(32))")"
# capability_schema is a VolumePath POINTER (CIP-33 §2.6.1, ≤1 KiB), not the
# inline document — the doc itself lives in the artifact volume, content-
# addressed by manifest_root. Pass a short pointer file, not the JSON.
CAP_POINTER="$E2E_DIR/cap-schema-pointer.txt"
printf 'ranchhand/capability-schema.json' > "$CAP_POINTER"
cw "$COWBOY_BIN" trading-post --private-key "$CREATOR_KEY_FILE" declare \
    --actor "$RANCH_ADDR" \
    --salt "$DECL_SALT" \
    --human-hireable \
    --manifest-root "$MANIFEST_ROOT" \
    --code-visibility private \
    --capability-schema-file "$CAP_POINTER" \
    --term free \
    --split "${CREATOR_ADDR}:10000" \
    --volume-id "$VOLUME_ID" \
    --claimed-root "$MANIFEST_ROOT" \
    --ibe-ciphertext-file "$E2E_DIR/escrow-envelope.json" > "$E2E_DIR/declare.log" 2>&1 \
    || { tail -10 "$E2E_DIR/declare.log"; fail "declare failed"; }
sleep 2
"$COWBOY_BIN" trading-post get-declaration --declaration-id "$DECLARATION_ID" >/dev/null \
    || fail "declaration not readable"
ok "declared $DECLARATION_ID"

cw "$COWBOY_BIN" actor execute --actor "$RANCH_ADDR" --handler set_declaration \
    --payload-json "{\"declaration_id\":\"$DECLARATION_ID\",\"version\":$VERSION,\"artifact_id\":$ARTIFACT_ID}" \
    --private-key "$CREATOR_KEY_FILE" > "$E2E_DIR/set-declaration.log" 2>&1 \
    || { tail -15 "$E2E_DIR/set-declaration.log" | sed 's/^/    /'; fail "set_declaration failed"; }
sleep 2
ok "pin recorded on actor"

# ── 4. hirer: hire (direct, Free) ────────────────────────────────────────────
log "Hiring (direct, Free)"
cw "$COWBOY_BIN" trading-post --private-key "$HIRER_KEY_FILE" hire \
    --declaration-id "$DECLARATION_ID" \
    --version "$VERSION" \
    --term free \
    --amount 0 > "$E2E_DIR/hire.log" 2>&1 \
    || { tail -10 "$E2E_DIR/hire.log"; fail "hire failed"; }
sleep 3
HIRE_ID=1
"$COWBOY_BIN" trading-post get-hire --hire-id "$HIRE_ID" > "$E2E_DIR/hire.json" \
    || fail "hire not readable"
ok "hire $HIRE_ID active"

# ── 5. hirer: secrets + credential proxy + ACL + connect ────────────────────
log "Hirer onboarding: secrets, proxy, ACLs, connect_workspace"
# The hirer account needs its own CBSS account release key before it can
# store secrets (the harness scenario only bootstraps the creator's).
cw "$COWBOY_BIN" secrets --private-key "$HIRER_KEY_FILE" request-account-dkg \
    > "$E2E_DIR/hirer-dkg.log" 2>&1 || { tail -5 "$E2E_DIR/hirer-dkg.log"; fail "hirer request-account-dkg failed"; }
HIRER_DKG_OK=0
for _ in $(seq 1 60); do
    if curl -sf "$RPC_URL/cbss/account-release-key/$HIRER_ADDR" >/dev/null 2>&1; then HIRER_DKG_OK=1; break; fi
    sleep 3
done
[ "$HIRER_DKG_OK" = "1" ] || fail "hirer account release key never appeared (DKG ceremony)"
ok "hirer account DKG committed"

SIGNING_SECRET="e2e-signing-secret-0123"
BOT_TOKEN="xoxb-e2e-token"
printf '%s' "$SIGNING_SECRET" > "$E2E_DIR/sig.txt"
printf '%s' "$BOT_TOKEN" > "$E2E_DIR/tok.txt"
# Sequential submits from the SAME sender must not share a nonce: the CLI reads
# the account nonce at call time, so a second submit fired before the first is
# included gets `nonce_too_low` and is silently skipped. Wait for the sender's
# on-chain nonce to advance between same-sender txs.
wait_nonce() {  # wait_nonce <addr> <min_nonce>
    for _ in $(seq 1 30); do
        local n
        n=$(curl -s "$RPC_URL/account/$1" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('nonce',0))" 2>/dev/null || echo 0)
        [ "$n" -ge "$2" ] && return 0
        sleep 1
    done
    return 1
}
HIRER_NONCE="$(curl -s "$RPC_URL/account/$HIRER_ADDR" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('nonce',0))" 2>/dev/null || echo 0)"
retry_submit "$E2E_DIR/sec1.log" "${SECRETS_ENV[@]}" "$COWBOY_BIN" secrets --private-key "$HIRER_KEY_FILE" \
    set RANCHHAND_SLACK_SIGNING_SECRET --value-file "$E2E_DIR/sig.txt" \
    || { tail -5 "$E2E_DIR/sec1.log"; fail "signing secret set failed"; }
wait_nonce "$HIRER_ADDR" $((HIRER_NONCE + 1)) || fail "signing-secret tx never advanced hirer nonce"
retry_submit "$E2E_DIR/sec2.log" "${SECRETS_ENV[@]}" "$COWBOY_BIN" secrets --private-key "$HIRER_KEY_FILE" \
    set RANCHHAND_SLACK_BOT_TOKEN --value-file "$E2E_DIR/tok.txt" \
    || { tail -5 "$E2E_DIR/sec2.log"; fail "bot token set failed"; }
wait_nonce "$HIRER_ADDR" $((HIRER_NONCE + 2)) || fail "bot-token tx never advanced hirer nonce"

PROXY_SALT="0x51ac"
PROXY_ADDR="$(addr_of relay "$HIRER_ADDR" "$PROXY_SALT")"
[ -n "$PROXY_ADDR" ] || fail "could not compute proxy address"
sed -e "s/HIRER_EOA/${HIRER_ADDR}/g" -e "s/SLACK_API_DOMAIN/127.0.0.1/g" \
    "$SCRIPT_DIR/proxy/manifest.template.json" > "$E2E_DIR/proxy-manifest.json"
cw "$COWBOY_BIN" actor deploy relay \
    --manifest-json "$E2E_DIR/proxy-manifest.json" \
    --init-payload "{\"ranchhand\":\"$RANCH_ADDR\",\"api_base\":\"http://127.0.0.1:${SLACK_STUB_PORT}\"}" \
    --private-key "$HIRER_KEY_FILE" \
    --salt "$PROXY_SALT" > "$E2E_DIR/proxy-deploy.log" 2>&1 \
    || { tail -10 "$E2E_DIR/proxy-deploy.log"; fail "proxy deploy failed"; }
sleep 2
ok "proxy at $PROXY_ADDR"
fund_actor "$PROXY_ADDR"
ok "proxy funded (deferred-tx gas)"

for KEY in RANCHHAND_SLACK_SIGNING_SECRET RANCHHAND_SLACK_BOT_TOKEN; do
    read -r VER HASH <<<"$(secret_version "$HIRER_ADDR" "$KEY")"
    [ -n "$VER" ] || fail "could not resolve version for $KEY"
    N="$(curl -s "$RPC_URL/account/$HIRER_ADDR" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('nonce',0))" 2>/dev/null || echo 0)"
    cw "$COWBOY_BIN" secrets --private-key "$HIRER_KEY_FILE" policy \
        --key-hash "$HASH" --version "$VER" --actor "$PROXY_ADDR" > "$E2E_DIR/policy-$KEY.log" 2>&1 \
        || { tail -5 "$E2E_DIR/policy-$KEY.log"; fail "policy update failed for $KEY"; }
    wait_nonce "$HIRER_ADDR" $((N + 1)) || fail "policy tx for $KEY never advanced hirer nonce"
done
ok "release policies ACL'd to proxy"

TEAM_ID="T0001"
cw "$COWBOY_BIN" actor execute --actor "$RANCH_ADDR" --handler connect_workspace \
    --payload-json "{\"team_id\":\"$TEAM_ID\",\"hire_id\":$HIRE_ID,\"proxy\":\"$PROXY_ADDR\",\"state_volume\":\"\"}" \
    --private-key "$HIRER_KEY_FILE" > "$E2E_DIR/connect.log" 2>&1 \
    || { tail -5 "$E2E_DIR/connect.log"; fail "connect_workspace failed"; }
sleep 2
ok "workspace $TEAM_ID connected"

# ── 6. the happy path: signed synthetic Slack event ─────────────────────────
log "Sending signed Slack event (valid MAC)"
send_slack_event() {  # send_slack_event <text> <thread_suffix> <sig_override?>
    local text="$1" suffix="$2" sig_override="${3:-}"
    TS="$(date +%s)"
    BODY="$(TEAM_ID="$TEAM_ID" TEXT="$text" SUFFIX="$suffix" python3 - <<'PY'
import json, os
print(json.dumps({
    "token": "e2e", "team_id": os.environ["TEAM_ID"], "type": "event_callback",
    "event": {"type": "app_mention", "user": "U0001",
              "text": "<@U0BOT> " + os.environ["TEXT"],
              "channel": "C0001", "ts": "1751000000.00" + os.environ["SUFFIX"]},
    "authorizations": [{"user_id": "U0BOT"}],
}, separators=(",", ":")))
PY
)"
    local sig
    if [ -n "$sig_override" ]; then
        sig="$sig_override"
    else
        sig="$(TS="$TS" BODY="$BODY" SECRET="$SIGNING_SECRET" python3 - <<'PY'
import hashlib, hmac, os
mac = hmac.new(os.environ["SECRET"].encode(), f"v0:{os.environ['TS']}:{os.environ['BODY']}".encode(), hashlib.sha256)
print("v0=" + mac.hexdigest())
PY
)"
    fi
    # The CIP-14 HttpRequestEnvelope is raw CBOR bytes. `--payload @file` is
    # restricted to the CWD, so hex-encode the CBOR and pass it via --payload
    # (hex → raw bytes → the handler's _decode_envelope).
    local envelope_hex
    envelope_hex="$(TS="$TS" BODY="$BODY" SIG="$sig" python3 - <<'PY'
import os
import cbor2
env = {
    "method": "POST",
    "path": "/slack/events",
    "headers": [["content-type", "application/json"],
                ["x-slack-request-timestamp", os.environ["TS"]],
                ["x-slack-signature", os.environ["SIG"]]],
    "body": os.environ["BODY"].encode(),
}
print("0x" + cbor2.dumps(env).hex())
PY
)"
    cw "$COWBOY_BIN" actor execute --actor "$RANCH_ADDR" --handler slack_http \
        --payload "$envelope_hex" \
        --private-key "$HIRER_KEY_FILE" > "$E2E_DIR/event-send.log" 2>&1 \
        || { tail -5 "$E2E_DIR/event-send.log"; fail "slack_http execute failed"; }
}

stub_count() { grep -c "$1" "$SLACK_STUB_LOG" 2>/dev/null || echo 0; }

send_slack_event "What is Cowboy?" "0100"
DEADLINE=$((SECONDS + 120))
ANSWERED=0
while [ $SECONDS -lt $DEADLINE ]; do
    if grep -q "$EXPECTED_ANSWER" "$SLACK_STUB_LOG" 2>/dev/null; then ANSWERED=1; break; fi
    sleep 3
done
[ "$ANSWERED" = "1" ] || fail "answer never reached the Slack stub (happy path)"
grep -q "chat.postMessage" "$SLACK_STUB_LOG" || fail "thinking placeholder was never posted"
grep -q "Bearer $BOT_TOKEN" "$SLACK_STUB_LOG" || fail "hirer bot token was not substituted into the post"
ok "hire-bound agent answer posted via the hirer's token"

# ── 6b. General Store listing + a second tenant hired THROUGH the store ─────
# The protocol store flow (register-store → grant → store artifact → attest →
# grant-routed hire pinning the store's artifact). The store-admin product
# service drives exactly these calls in production (see impl-general-store.md);
# here the store operator drives them directly.
log "Store leg: register store, grant, artifact, attest"
STORE_KEY_HEX="0200000000000000000000000000000000000000000000000000000000000000"
STORE_KEY_FILE="$E2E_DIR/store.key"; printf '%s' "$STORE_KEY_HEX" > "$STORE_KEY_FILE"
STORE_ADDR="$("$COWBOY_BIN" wallet address --key "$STORE_KEY_FILE")"
# Trading-post ops (register-store/grant/register-artifact/attest) carry a much
# higher gas ceiling than actor deferred txs, so 100 CBY is refused at admission
# (insufficient_balance) — fund the store generously. fund_actor polls until the
# balance lands (a bare sleep raced the transfer).
fund_actor "$STORE_ADDR" 5000

cw "$COWBOY_BIN" trading-post --private-key "$STORE_KEY_FILE" register-store \
    --metadata "ranchhand-e2e-store" > "$E2E_DIR/register-store.log" 2>&1 \
    || { tail -5 "$E2E_DIR/register-store.log"; fail "register-store failed"; }
sleep 2
STORE_ID=1
"$COWBOY_BIN" trading-post get-store --store-id "$STORE_ID" >/dev/null || fail "store not readable"

cw "$COWBOY_BIN" trading-post --private-key "$CREATOR_KEY_FILE" grant \
    --declaration-id "$DECLARATION_ID" \
    --store-id "$STORE_ID" \
    --split "${CREATOR_ADDR}:8000" \
    --split "${STORE_ADDR}:2000" \
    --lease-epoch-blocks 200 > "$E2E_DIR/grant.log" 2>&1 \
    || { tail -5 "$E2E_DIR/grant.log"; fail "grant failed"; }
sleep 2
GRANT_ID="$("$COWBOY_BIN" trading-post get-grant --store-id "$STORE_ID" --declaration-id "$DECLARATION_ID" \
    | python3 -c 'import json,sys,re; t=sys.stdin.read(); i=t.find("{"); print(json.loads(t[i:]).get("grant_id", 1))' 2>/dev/null || echo 1)"

# Store escrows ITS artifact (expected artifact id 1) under its own account.
STORE_ARTIFACT_ID=1
STORE_TP_LABEL="tradingpost/${DECLARATION_ID}/${VERSION}/${STORE_ARTIFACT_ID}"
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$E2E_DIR/store-escrow-plain.txt"
retry_submit "$E2E_DIR/store-escrow.log" "${SECRETS_ENV[@]}" "$COWBOY_BIN" secrets --private-key "$STORE_KEY_FILE" \
    set "$STORE_TP_LABEL" --value-file "$E2E_DIR/store-escrow-plain.txt" \
    --cbfs-output "$E2E_DIR/store-escrow-envelope.json" \
    || { tail -5 "$E2E_DIR/store-escrow.log"; fail "store escrow failed"; }
sleep 2
cw "$COWBOY_BIN" trading-post --private-key "$STORE_KEY_FILE" register-artifact \
    --store-id "$STORE_ID" \
    --declaration-id "$DECLARATION_ID" \
    --version "$VERSION" \
    --volume-id "$VOLUME_ID" \
    --claimed-root "$MANIFEST_ROOT" \
    --ibe-ciphertext-file "$E2E_DIR/store-escrow-envelope.json" > "$E2E_DIR/register-artifact.log" 2>&1 \
    || { tail -5 "$E2E_DIR/register-artifact.log"; fail "register-artifact failed"; }
sleep 2
"$COWBOY_BIN" trading-post get-store-artifacts --store-id "$STORE_ID" > "$E2E_DIR/store-artifacts.json" \
    || fail "store artifacts not readable"
grep -q "\"artifact_id\": *$STORE_ARTIFACT_ID" "$E2E_DIR/store-artifacts.json" \
    || fail "store artifact id != expected $STORE_ARTIFACT_ID (escrow label would mismatch)"

cw "$COWBOY_BIN" trading-post --private-key "$STORE_KEY_FILE" attest \
    --manifest-root "$MANIFEST_ROOT" \
    --verdict approved \
    --policy-uri "ranchhand-e2e://approved" > "$E2E_DIR/attest.log" 2>&1 \
    || { tail -5 "$E2E_DIR/attest.log"; fail "attest failed"; }
sleep 2
ok "listed: store $STORE_ID, grant $GRANT_ID, artifact $STORE_ARTIFACT_ID, attested"

log "Store-routed hire for a second workspace (T0002)"
cw "$COWBOY_BIN" trading-post --private-key "$HIRER_KEY_FILE" hire \
    --declaration-id "$DECLARATION_ID" \
    --version "$VERSION" \
    --grant-id "$GRANT_ID" \
    --term free \
    --amount 0 > "$E2E_DIR/hire2.log" 2>&1 \
    || { tail -5 "$E2E_DIR/hire2.log"; fail "store-routed hire failed"; }
sleep 3
HIRE2_ID=2
"$COWBOY_BIN" trading-post get-hire --hire-id "$HIRE2_ID" > "$E2E_DIR/hire2.json" \
    || fail "hire $HIRE2_ID not readable"
grep -q "\"artifact_id\": *$STORE_ARTIFACT_ID" "$E2E_DIR/hire2.json" \
    || fail "store hire did not pin the store's artifact"

PROXY2_SALT="0x51ad"
PROXY2_ADDR="$(addr_of relay "$HIRER_ADDR" "$PROXY2_SALT")"
cw "$COWBOY_BIN" actor deploy relay \
    --manifest-json "$E2E_DIR/proxy-manifest.json" \
    --init-payload "{\"ranchhand\":\"$RANCH_ADDR\",\"api_base\":\"http://127.0.0.1:${SLACK_STUB_PORT}\"}" \
    --private-key "$HIRER_KEY_FILE" \
    --salt "$PROXY2_SALT" > "$E2E_DIR/proxy2-deploy.log" 2>&1 \
    || { tail -5 "$E2E_DIR/proxy2-deploy.log"; fail "proxy2 deploy failed"; }
sleep 2
fund_actor "$PROXY2_ADDR"
# Policy update REPLACES the actor list — keep proxy 1 while adding proxy 2.
for KEY in RANCHHAND_SLACK_SIGNING_SECRET RANCHHAND_SLACK_BOT_TOKEN; do
    read -r VER HASH <<<"$(secret_version "$HIRER_ADDR" "$KEY")"
    N="$(curl -s "$RPC_URL/account/$HIRER_ADDR" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('nonce',0))" 2>/dev/null || echo 0)"
    cw "$COWBOY_BIN" secrets --private-key "$HIRER_KEY_FILE" policy \
        --key-hash "$HASH" --version "$VER" --actor "$PROXY_ADDR" --actor "$PROXY2_ADDR" \
        > "$E2E_DIR/policy2-$KEY.log" 2>&1 \
        || { tail -5 "$E2E_DIR/policy2-$KEY.log"; fail "policy update (proxy2) failed for $KEY"; }
    wait_nonce "$HIRER_ADDR" $((N + 1)) || fail "policy2 tx for $KEY never advanced hirer nonce"
done
TEAM2_ID="T0002"
cw "$COWBOY_BIN" actor execute --actor "$RANCH_ADDR" --handler connect_workspace \
    --payload-json "{\"team_id\":\"$TEAM2_ID\",\"hire_id\":$HIRE2_ID,\"proxy\":\"$PROXY2_ADDR\",\"state_volume\":\"\",\"artifact_id\":$STORE_ARTIFACT_ID}" \
    --private-key "$HIRER_KEY_FILE" > "$E2E_DIR/connect2.log" 2>&1 \
    || { tail -5 "$E2E_DIR/connect2.log"; fail "connect_workspace (T0002) failed"; }
sleep 2

UPDATES_BEFORE_T2="$(stub_count "$EXPECTED_ANSWER")"
TEAM_ID_SAVED="$TEAM_ID"; TEAM_ID="$TEAM2_ID"
send_slack_event "Howdy from the store tenant" "0400"
TEAM_ID="$TEAM_ID_SAVED"
DEADLINE=$((SECONDS + 120)); T2_ANSWERED=0
while [ $SECONDS -lt $DEADLINE ]; do
    if [ "$(stub_count "$EXPECTED_ANSWER")" -gt "$UPDATES_BEFORE_T2" ]; then T2_ANSWERED=1; break; fi
    sleep 3
done
[ "$T2_ANSWERED" = "1" ] || fail "store-routed tenant never got an answer"
ok "second tenant (hired through the store, pinned to the store artifact) answered"

# wait_hire_status <hire_id> <status> — poll get-hire until the revoke is
# FINALIZED, so the test event's hire-bound agent JobSubmit is validated
# against the revoked state (a bare sleep raced the ~6-block async chain).
wait_hire_status() {
    for _ in $(seq 1 30); do
        local st
        st=$("$COWBOY_BIN" trading-post get-hire --hire-id "$1" 2>/dev/null \
            | python3 -c "import json,sys,re; t=sys.stdin.read(); i=t.find('{'); print(json.loads(t[i:]).get('status','')) if i>=0 else print('')" 2>/dev/null || echo "")
        [ "$st" = "$2" ] && return 0
        sleep 1
    done
    return 1
}

log "Store-side revoke of hire $HIRE2_ID"
cw "$COWBOY_BIN" trading-post --private-key "$STORE_KEY_FILE" revoke --hire-id "$HIRE2_ID" \
    > "$E2E_DIR/revoke2.log" 2>&1 || { tail -5 "$E2E_DIR/revoke2.log"; fail "store revoke failed"; }
wait_hire_status "$HIRE2_ID" "Revoked" || fail "hire $HIRE2_ID never reached Revoked"
UPDATES_BEFORE_T2R="$(stub_count "$EXPECTED_ANSWER")"
TEAM_ID="$TEAM2_ID"
send_slack_event "Still with us?" "0500"
TEAM_ID="$TEAM_ID_SAVED"
sleep 30
[ "$(stub_count "$EXPECTED_ANSWER")" = "$UPDATES_BEFORE_T2R" ] \
    || fail "store-revoked hire still produced an answer"
ok "store-revoked tenant refused (MAC ok, no compute)"

# ── 7. forged MAC is rejected before any post ────────────────────────────────
log "Sending forged event (bad MAC)"
POSTS_BEFORE="$(stub_count chat.postMessage)"
send_slack_event "Steal the herd" "0200" "v0=$(printf 'ab%.0s' {1..32})"
sleep 20
POSTS_AFTER="$(stub_count chat.postMessage)"
[ "$POSTS_AFTER" = "$POSTS_BEFORE" ] || fail "forged event produced a Slack post"
grep -q "precondition" /tmp/cbss-e2e-runner.log \
    || fail "runner log shows no precondition failure for the forged event"
ok "forged MAC rejected fail-closed (no post, terminal precondition)"

# ── 8. revoke: the next event gets no compute ────────────────────────────────
log "Revoking hire $HIRE_ID and re-sending a valid event"
cw "$COWBOY_BIN" trading-post --private-key "$CREATOR_KEY_FILE" revoke --hire-id "$HIRE_ID" \
    > "$E2E_DIR/revoke.log" 2>&1 || { tail -5 "$E2E_DIR/revoke.log"; fail "revoke failed"; }
wait_hire_status "$HIRE_ID" "Revoked" || fail "hire $HIRE_ID never reached Revoked"

UPDATES_BEFORE="$(stub_count "$EXPECTED_ANSWER")"
send_slack_event "Are you still there?" "0300"
sleep 30
UPDATES_AFTER="$(stub_count "$EXPECTED_ANSWER")"
[ "$UPDATES_AFTER" = "$UPDATES_BEFORE" ] \
    || fail "revoked hire still produced an agent answer"
ok "revoked hire: MAC still verifies, but the hire-bound agent job is refused"

echo ""
ok "RANCHHAND E2E PASSED"
echo "  declaration = $DECLARATION_ID"
echo "  hire        = $HIRE_ID (revoked at the end, by design)"
echo "  stub log    = $SLACK_STUB_LOG"
