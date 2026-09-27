#!/usr/bin/env bash
# One-command Hyperliquid trader demo: env -> CBSS -> actors -> live trade.
#
#   nano /tmp/hlkey.txt          # paste the HL agent private key (0x...)
#   ./tools/demo.sh              # stand everything up and open the first positions
#   ./tools/demo.sh cycle        # force another trade NOW (skips the 23-min guard)
#   ./tools/demo.sh status       # positions + P&L + chain health
#   ./tools/demo.sh down         # tear the env down
#
# ORDER OF OPERATIONS IS LOAD-BEARING. Every step below encodes a failure that cost
# hours on 2026-08-05/06/07; see memory `hl-trader-working-recipe`. In particular:
#   * the Anthropic key must be passed at env CREATION (else local llama scores
#     everything below min_score and the corpus never fills)
#   * cbssd must be >= f6224c7 (mpk-bound AAD) BEFORE the ceremony, and must never be
#     moved after it (that orphans the DKG shares, unrecoverably)
#   * outpost omits ingest.chain_id, which newer cbssd requires — proxies crashloop
#     while still reporting "registered", so the DKG silently yields no release key
#   * the leader timeout must be raised before load, and any `redeploy` wipes it
#   * RPC_GLOBAL_RATE_LIMIT_PER_SEC must be raised off its 100/s default or CBSS
#     releases and runner heartbeats 429 and the whole application starves while the
#     chain looks perfectly healthy (see step 3)
set -uo pipefail

# cal-river pins the runner built from `fix-nonce-reanchor-devnet` (devnet base +
# the heartbeat-interval and nonce-re-anchor fixes). cal-fix pins stock
# latest-devnet, which still ignores RUNNER_HEARTBEAT_INTERVAL_SECONDS.
ENV_NAME="${ENV_NAME:-cal-river}"
KEYFILE="${KEYFILE:-/tmp/hlkey.txt}"
OUTPOST="${OUTPOST:-/Users/calebmandia/Desktop/Cowboy/full-cowboy/outpost}"
NODE_SRC="${NODE_SRC:-/Users/calebmandia/Desktop/Cowboy/full-cowboy/node-hbfix}"
CLI="${CLI:-$NODE_SRC/target/release/cowboy}"
S="$(cd "$(dirname "$0")" && pwd)"
G=20000000
export AWS_PROFILE="${AWS_PROFILE:-cowboy-river}"

say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
die(){ echo "FAILED: $*" >&2; exit 1; }
ip_of(){ aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:Name,Values=river-$ENV_NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null | head -1; }
iid_of(){ aws ssm get-parameter --region us-east-1 --name "/cowboy/river/$ENV_NAME/instance-id" \
  --query Parameter.Value --output text 2>/dev/null; }
ssm(){ # $1=script text -> runs it on the box, prints stdout
  local b64 c; b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  c=$(aws ssm send-command --region us-east-1 --instance-ids "$(iid_of)" \
      --document-name AWS-RunShellScript \
      --parameters "commands=[\"echo $b64 | base64 -d | bash\"]" \
      --query 'Command.CommandId' --output text 2>/dev/null)
  sleep "${2:-25}"
  aws ssm get-command-invocation --region us-east-1 --command-id "$c" \
      --instance-id "$(iid_of)" --query 'StandardOutputContent' --output text 2>/dev/null
}
rd(){ local p="${3:-}"; [ -z "$p" ] && p='{}'
  curl -s --max-time 30 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
    --data-binary "$(python3 -c 'import json,sys;print(json.dumps({"actor":sys.argv[1],"handler":sys.argv[2],"payload_text":sys.argv[3]}))' "$1" "$2" "$p")" \
  | python3 -c "import sys,json,base64;d=json.load(sys.stdin);print(base64.b64decode(d['result']).decode() if 'result' in d else json.dumps(d))" 2>/dev/null; }
ex(){ local p="${3:-}"; [ -z "$p" ] && p='{}'
  # Manual trades use apply_ideas below, whose 295 structural accesses fit the
  # default 512 client stamp; no script hard-codes an oversized access limit.
  COWBOY_RPC_URL="$RPC" "$CLI" actor execute --actor "$1" --handler "$2" \
    --payload-text "$p" --private-key "$S/deploykey" \
    --cycles-limit $G --cells-limit $G 2>&1; }

# ─────────────────────────────────────────────────────────── subcommands
case "${1:-up}" in
status)
  RPC="http://$(ip_of):4000"
  echo "  chain : $(curl -s -m 20 "$RPC/height")"
  echo "  book  : $(rd "$(cat "$S/actor.txt")" get_portfolio | head -c 400)"
  echo "  pnl   : $(rd "$(cat "$S/actor.txt")" get_pnl)"
  # A frozen book with a healthy chain is THE failure signature, and it is always
  # downstream of one of these. Show them next to the book so the two are compared.
  echo "  proxies: $(curl -s -m 20 "$RPC/cbss/proxies" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("proxies",[])),"registered")' 2>/dev/null)"
  echo "  runners (storage nonce must ADVANCE; gap>=16 means stranding):"
  curl -s -m 20 "$RPC/runners" 2>/dev/null | python3 -c '
import sys,json,urllib.request
rpc = sys.argv[1]
try: body = json.load(sys.stdin)
except Exception: print("    (runner list unavailable)"); raise SystemExit
rs = body.get("runners", body if isinstance(body, list) else [])
if not rs: print("    (no runners registered — dispatch has no candidates)"); raise SystemExit
for r in rs[:10]:
    a = r.get("address") or r.get("runner") or r
    if not isinstance(a, str): continue
    a = a if a.startswith("0x") else "0x"+a
    def get(p):
        try:
            with urllib.request.urlopen(f"{rpc}{p}", timeout=10) as h: return json.load(h)
        except Exception: return {}
    st = get(f"/account/{a}").get("nonce")
    pd = get(f"/runner/{a}/heartbeat_payload").get("nonce")
    gap = (pd - st) if isinstance(pd, int) and isinstance(st, int) else "?"
    print(f"    {a[:12]} storage={st} pending={pd} gap={gap}")
' "$RPC" 2>/dev/null
  exit 0;;
cycle)
  # Force a trade NOW. `apply_ideas` is @callable_by(OWNER) @deferred and does NOT
  # consult _tick_ok, so it bypasses the ~23-min re-entry guard that bounds the
  # natural rebalance cadence. Demo-only: back-to-back calls can overlap _apply runs.
  RPC="http://$(ip_of):4000"
  T=$(cat "$S/actor.txt"); R=$(cat "$S/research.txt")
  say "forcing research synthesis"
  ex "$R" synthesize_now >/dev/null
  for i in $(seq 1 14); do sleep 15
    n=$(rd "$R" get_ideas | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('ideas',[])))" 2>/dev/null || echo 0)
    [ "${n:-0}" -gt 0 ] && break
  done
  IDEAS=$(rd "$R" get_ideas | python3 -c "import sys,json;print(json.dumps({'ideas':json.load(sys.stdin).get('ideas',[])}))")
  say "applying $(echo "$IDEAS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["ideas"]))') ideas"
  ex "$T" apply_ideas "$IDEAS" | grep -E "Status|Deferred" | head -2
  say "watching the book"
  # Report PASS/FAIL rather than exiting 0 regardless. A silent no-trade is the
  # failure mode that has cost the most time here: every upstream step reports
  # success and only an unchanged book reveals it.
  BEFORE=$(rd "$T" get_pnl | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('rebalances',0))" 2>/dev/null || echo 0)
  OPENED=0
  for i in $(seq 1 20); do sleep 30
    B=$(rd "$T" get_portfolio | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('positions',[])),json.dumps(d.get('positions',[]))[:200])" 2>/dev/null)
    echo "  $B"
    case "$B" in 0*|"") ;; *) OPENED=1; break;; esac
  done
  AFTER=$(rd "$T" get_pnl | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('rebalances',0))" 2>/dev/null || echo 0)
  if [ "$OPENED" = "1" ] || [ "${AFTER:-0}" -gt "${BEFORE:-0}" ] 2>/dev/null; then
    say "PASS — book moved (rebalances $BEFORE -> $AFTER)"; exit 0
  fi
  say "FAIL — no position and no rebalance after 10 min"
  cat >&2 <<'EOF'
  The upstream steps all report success in this case, so check, in order:
    1. CBSS releases 429-ing (THE common cause — see step 3 rate limit):
         docker logs river-<env>-cbss-1 --since 20m 2>&1 | grep -a "partial sign error"
       `429 Too Many Requests` => RPC_GLOBAL_RATE_LIMIT_PER_SEC is still at 100.
       `bincode error`         => runner/cbssd protocol skew (wrong runner build).
    2. runners dropped from dispatch (heartbeat stale > 100 blocks):
         curl -s http://<ip>:4000/cbss/proxies   # and check active runner count
    3. universe cache empty => plan_orders matches nothing and places zero orders:
         re-run `universe_now` and confirm BOTH _refresh_universe__resume legs
EOF
  exit 1;;
down)
  cd "$OUTPOST" && ./cowboy-env down "$ENV_NAME"; exit 0;;
esac

# ─────────────────────────────────────────────────────────── full stand-up
TG_TOKEN="${TG_TOKEN:-$(cat ~/.telegram_bot_token 2>/dev/null || true)}"
TG_CHAT="${TG_CHAT:-$(cat ~/.telegram_chat_id 2>/dev/null || true)}"
if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  : "${TG_WEBHOOK_SECRET:?set TG_WEBHOOK_SECRET before wiring Telegram; use the same secret_token when registering the webhook}"
fi
[ -s "$KEYFILE" ] || die "put the HL agent key in $KEYFILE first (nano $KEYFILE)"
[ -x "$CLI" ] || die "version-matched CLI missing: build it with
    cd $NODE_SRC && cargo build -p cowboy-cli --release
  (a STALE cowboy silently no-ops 'secrets set' — the tx lands and writes nothing)"

say "1. env up (Anthropic key at creation; CBSS pinned >= f6224c7)"
cd "$OUTPOST"
# Snapshot the version pin BEFORE standing up. configs/<env>.json is edited in
# place run over run, so each run silently destroys the previous run's pin: run4
# went 17.9h — 4x the ~4.6h median across every other run — and its exact build is
# now unrecoverable because run5 overwrote the file. Without a snapshot we cannot
# run the one experiment that would settle whether our changes regressed anything
# or run4 was simply a lucky draw.
RUN_TAG="${RUN_TAG:-$(date -u +%Y%m%d-%H%M)}"
if [ -f "configs/$ENV_NAME.json" ]; then
  cp "configs/$ENV_NAME.json" "configs/$ENV_NAME-$RUN_TAG.json"
  echo "  pinned config snapshot: configs/$ENV_NAME-$RUN_TAG.json"
  python3 -c "
import json;d=json.load(open('configs/$ENV_NAME.json'))
print('  versions:', json.dumps(d.get('versions',{})))" 2>/dev/null
fi
./cowboy-env up "$ENV_NAME" -e anthropic_api_key="$(cat ~/.anthropic_key_canyon)" \
  || die "env up"
IP="$(ip_of)"; RPC="http://$IP:4000"; echo "  IP=$IP"

say "2. fix cbssd config (outpost omits ingest.chain_id; newer cbssd hard-fails on it)"
ssm 'python3 - <<EOF
import json,glob
for p in sorted(glob.glob("/var/lib/docker/volumes/river-'"$ENV_NAME"'_cbss-*-data/_data/config.json")):
    d=json.load(open(p)); cid=d.get("chain",{}).get("chain_id")
    ing=d.setdefault("ingest",{})
    if cid is not None and ing.get("chain_id")!=cid:
        ing["chain_id"]=cid; json.dump(d,open(p,"w"),indent=1); print("  patched",p)
EOF
cd /opt/river && docker compose restart cbss-1 cbss-2 cbss-3 cbss-4 cbss-5 2>&1 | tail -1' 45

say "3. validator env: consensus timeouts + RPC rate limit"
# leader_timeout: the 1s default wedges under load.
#
# 2000 USED to be the hard ceiling: simplex asserts leader <= certification and
# certification was a hardcoded 2s, so anything above 2000 panicked at boot
# ("leader timeout must be less than or equal to certification timeout") and left
# the validator in a crash loop. Confirmed the hard way on 2026-08-09.
#
# Newer node builds expose COWBOY_CERTIFICATION_TIMEOUT_MS, which lifts that
# ceiling. This matters because the block cycle measured 1.57-2.32s against the
# 2000ms deadline — propose was cancelled every time, the AIMD controller halved a
# tx budget that governs only ~6% of cycle cost, and the chain re-executed one
# height ~1045 times without ever committing (the run5 livelock).
#
# We therefore try the raised pair and FALL BACK if the build does not support it.
# Never assume: an unsupported value here is not a warning, it is an unbootable
# node, and the old unbounded `until curl` wait turned that into a silent hang.
#
# RPC_GLOBAL_RATE_LIMIT_PER_SEC: THE root cause of the 2026-08-06 "healthy chain,
#   dead application" stall. The default is 100 req/s and it is GLOBAL, not per-IP.
#   cbssd queries the node on EVERY PartialSign
#   (`GET /cbss/secret/{acct}/{key_hash}?version=N`); at 100/s those 429, the proxy
#   closes the QUIC stream, and the runner reports the misleading
#   `early eof` -> `secret committee unavailable`. Runner heartbeats 429 too, so
#   runners go stale past HEARTBEAT_TIMEOUT_BLOCKS (100) and drop out of dispatch —
#   the actor pipeline dies while the chain keeps producing blocks, because
#   consensus never touches RPC.
#   10 runners polling jobs every 1s + 5 proxies + continuations do not fit in 100/s.
#   See memory `node-rpc-ratelimit-starves-cbss`.
LEADER_MS="${LEADER_MS:-5000}"; CERT_MS="${CERT_MS:-10000}"
apply_validator_env() {  # $1=leader_ms  $2=cert_ms ("" to omit the cert var)
  ssm 'python3 - <<EOF
p="/opt/river/docker-compose.yml"; s=open(p).read()
import re
anchor=chr(34)+"CIP11_QUIC_BIND=0.0.0.0:9000"+chr(34)
# Rewrite rather than skip-if-present: this runs twice on the fallback path.
for k in ["COWBOY_LEADER_TIMEOUT_MS","COWBOY_CERTIFICATION_TIMEOUT_MS"]:
    s=re.sub(", *"+chr(34)+k+"=[0-9]+"+chr(34),"",s)
want=["COWBOY_LEADER_TIMEOUT_MS='"$1"'"]
cert="'"${2:-}"'"
if cert: want.append("COWBOY_CERTIFICATION_TIMEOUT_MS="+cert)
if "RPC_GLOBAL_RATE_LIMIT_PER_SEC" not in s: want.append("RPC_GLOBAL_RATE_LIMIT_PER_SEC=5000")
if anchor in s:
    open(p,"w").write(s.replace(anchor,anchor+"".join(", "+chr(34)+a+chr(34) for a in want),1))
    print("  set:", ", ".join(want))
else:
    print("  ANCHOR MISSING — validator env NOT applied")
EOF
cd /opt/river && docker compose up -d --force-recreate validator-1 2>&1 | tail -1' 45
}
# Bounded wait. The previous `until curl; do sleep; done` had no exit and turned a
# boot crash-loop into an infinite hang with no diagnosis.
wait_for_chain() {  # $1=seconds; 0 = up, 1 = never came up
  local waited=0
  while [ "$waited" -lt "$1" ]; do
    curl -s -m 8 "$RPC/height" 2>/dev/null | grep -qE '[0-9]' && return 0
    sleep 10; waited=$((waited+10))
  done
  return 1
}
apply_validator_env "$LEADER_MS" "$CERT_MS"
if wait_for_chain 120; then
  echo "  chain: $(curl -s -m 20 "$RPC/height")  (leader=${LEADER_MS}ms cert=${CERT_MS}ms)"
else
  # Distinguish "build lacks the cert override" from any other boot failure, so we
  # do not silently downgrade a chain that failed for an unrelated reason.
  if ssm 'docker logs river-'"$ENV_NAME"'-validator-1 2>&1 | tail -40' 30 \
       | grep -qi "less than or equal to certification"; then
    say "  !! node build lacks COWBOY_CERTIFICATION_TIMEOUT_MS — falling back to leader=2000"
    apply_validator_env 2000 ""
    wait_for_chain 120 || { say "FAIL: validator will not boot even at leader=2000"; exit 1; }
    echo "  chain: $(curl -s -m 20 "$RPC/height")  (leader=2000ms, UNRAISED — livelock risk)"
  else
    say "FAIL: validator did not come up in 120s and it is not the timeout assertion"
    ssm 'docker logs river-'"$ENV_NAME"'-validator-1 2>&1 | tail -20' 30 | sed 's/^/    /'
    exit 1
  fi
fi

say "4. ceremony (DKG -> secret -> verify). NEVER move a CBSS component after this."
RPC="$RPC" "$S/setup_live.sh" >/tmp/demo-stage1.log 2>&1
grep -qiE "release key ready|already present" /tmp/demo-stage1.log \
  || { tail -5 /tmp/demo-stage1.log; die "no account release key — are the cbss proxies healthy?"; }
CBSS_INGEST_ENDPOINT="http://$IP:9602" "$CLI" secrets --private-key "$S/deploykey" \
  --rpc-url "$RPC" set HL_AGENT_KEY --value-file "$KEYFILE" >/tmp/demo-secret.log 2>&1
DEP=$(cat "$S/deployer.txt")
for i in $(seq 1 15); do
  N=$(curl -s -m 20 "$RPC/cbss/secrets/$DEP" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("secrets",[])))' 2>/dev/null || echo 0)
  [ "${N:-0}" != "0" ] && break; sleep 8
done
[ "${N:-0}" = "0" ] && die "secret never registered (stale CLI? cbssd < f6224c7?)"
echo "  secret registered"

say "5. deploy + fund actors"
# SYNTH_BLOCKS 599 (not the 150 used while debugging): a fresh LLM macro call every
# ~4 min is not stable between runs and whipsaws the book.
#
# REBALANCE_BLOCKS must be > SYNTH_BLOCKS, and 1200 for two reasons:
#   * at 600 it sat ONE block after synth (599), so every rebalance raced the
#     synthesis it should have consumed and acted on the previous cycle's ideas
#   * `min_hold_blocks` is 1200 — a position cannot close before then, so a
#     600-block rebalance makes every OTHER cycle a structural no-op that still
#     runs the full 6-leg `_apply`, burning runner jobs and account nonces for
#     nothing. That is the load that strands the nonce counter (see the runner
#     fix in fix-nonce-reanchor-on-gap).
# Now: synth ~every 10 min feeds a rebalance ~every 21 min, two fresh syntheses
# per rebalance, and the rebalance is aligned with when a position may close.
# Telegram: setup_live.sh calls set_telegram only when BOTH TG_TOKEN and TG_CHAT are
# set, and otherwise deploys the notifier DARK with a one-line "skipping" note that is
# easy to miss. The full stand-up preflight sources them so wiring is automatic.
#   token    ~/.telegram_bot_token
#   chat id  ~/.telegram_chat_id  -- Telegram will not reveal a chat_id until the chat
#            exists, so message the bot (@HL_AdviceBot) once, then:
#     curl -s "https://api.telegram.org/bot$(cat ~/.telegram_bot_token)/getUpdates" \
#       | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"][0]["message"]["chat"]["id"])' \
#       > ~/.telegram_chat_id
[ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] \
  && echo "  telegram: wiring notifier" \
  || echo "  telegram: DARK (need ~/.telegram_chat_id — message @HL_AdviceBot, see above)"
# Cadences are PRIME on purpose — the actors' own defaults (601, 293, 4001, 149,
# 599, 3593, 7207, 997, 449) are all prime or near-prime so that no two of the nine
# timer loops come due in the same block except every lcm(a,b) blocks, which is
# enormous when they share no factors.
#
# This script previously overrode four of them with round numbers — 1200, 1800,
# 3600, 250 — and 3600 is an exact multiple of BOTH 1200 and 1800. So at every
# multiple of 3600 the rebalance, validate and distill timers all fell due
# together: three inline handlers at ~600ms each inside a 2s block deadline.
# Inline timers execute during block production, so a collision directly threatens
# the deadline (see memory `inline-timer-block-livelock`).
#
# Values below are the nearest primes to the intended cadences, so timing is
# unchanged in practice but the collisions stop being periodic.
# Constraint retained: REBALANCE must exceed SYNTH so a rebalance never races the
# synthesis that feeds it.
# Only `rebalance` is compressed for demo velocity — it is the loop that produces
# visible trades. The research loops are left at (or near) their committed
# defaults: compressing them costs inline-timer budget every block and buys
# nothing watchable. Notably `discover` was being forced 997 -> 250, four times
# more frequent, and `ingest` was absent from the payload entirely so it ran at
# the actor default of 149. Together those two were ~27 of ~50 timer fires/hour.
#
# This matters beyond scheduling: when blocks slow, due timers ACCUMULATE and the
# next block executes all of them, which slows it further. Coprime intervals stop
# periodic collisions but do nothing for that spiral — only a lower fire rate does.
STAGE=deploy RPC="$RPC" REBALANCE_BLOCKS=1201 INGEST_BLOCKS=419 SYNTH_BLOCKS=599 \
  DISCOVER_BLOCKS=997 MIN_AGE_BLOCKS=1499 VALIDATE_BLOCKS=1801 DISTILL_BLOCKS=3607 \
  TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" \
  TG_WEBHOOK_SECRET="${TG_WEBHOOK_SECRET:-}" \
  "$S/setup_live.sh" >/tmp/demo-fleet.log 2>&1
FLEET_RC=$?
T=$(cat "$S/actor.txt"); R=$(cat "$S/research.txt")
# setup_live.sh dies loudly on a failed deploy, but its exit code was ignored here
# and actor.txt/research.txt keep the PREVIOUS env's addresses on failure. On
# 2026-08-08 the research actor failed with E1210 (a NameError at import), the
# stale address stayed in research.txt, and the run sailed on to "applying 0 ideas"
# against an actor that did not exist on this chain. Fail here instead, and prove
# both actors are real before continuing.
if [ "$FLEET_RC" != "0" ]; then
  tail -6 /tmp/demo-fleet.log >&2
  die "fleet deploy failed (rc=$FLEET_RC) — see /tmp/demo-fleet.log"
fi
for pair in "$T:trader" "$R:research"; do
  addr="${pair%:*}"; name="${pair##*:}"
  curl -s -m 20 "$RPC/account/$addr" 2>/dev/null | grep -q '"address"' \
    || die "$name actor $addr does not exist on this chain — stale ${name}.txt from a previous env?"
done
echo "  trader=$T  research=$R  (both verified on chain)"

say "6. warm the universe cache (must precede any rebalance — _apply reads it from
      storage; empty => plan_orders matches nothing and places zero orders silently)"
ex "$T" universe_now | grep -E "Status|Deferred" | head -2
for i in $(seq 1 12); do sleep 30
  L=$(aws logs filter-log-events --region us-east-1 \
      --log-group-name "/cowboy/river/$ENV_NAME/validator-1" \
      --start-time $(( ($(date +%s) - 900) * 1000 )) --filter-pattern JobSubmit \
      --max-items 60 --query 'events[].message' --output text 2>/dev/null \
    | python3 -c "
import sys,re
n=0
for m in re.finditer(r'job_spec: \[([0-9, ]+)\]', sys.stdin.read()):
    if b'_refresh_universe__resume' in bytes(int(x) for x in m.group(1).split(',')): n+=1
print(n)" 2>/dev/null || echo 0)
  echo "  universe legs=$L"
  [ "${L:-0}" -ge 2 ] && break
done

say "7. first trade"
# MUST be "$S/demo.sh", not "$0": step 1 does `cd "$OUTPOST"` and never returns,
# so a relative $0 (./tools/demo.sh) no longer resolves from there and exec dies
# with 126 -- the stand-up looks green but the first trade never fires.
exec "$S/demo.sh" cycle
