#!/usr/bin/env bash
#
# Hyperliquid Trader demo
# =======================
# Deploys the research + paper-execution actors, wires them, drives ONE full
# cycle (ingest -> synthesize -> paper rebalance) and reads back the thesis,
# ideas, and the resulting book. Manual `*_now` handlers are used so the demo is
# observable end-to-end; for autonomous operation, init with fast
# `*_interval_blocks`, fund the actors, and call `start()` on both.
#
# Requirements: a reachable node whose runner serves http.fetch + oracle.llm
# (mesa by default), the `cowboy` CLI, and a working faucet.
#
# Usage: ./demo.sh [RPC_URL]

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RPC_URL="${1:-https://rpc.mesa.cowboylabs.net}"
export COWBOY_RPC_URL="$RPC_URL"
# Prefer the repo binary: `cowboy` on PATH is often a stale version that 400s
# deploys ("unsupported version"). Fall back to PATH only if the repo one is absent.
DEFAULT_CB="$(cd "$DIR/../../../.." && pwd)/node/target/release/cowboy"
COWBOY="${COWBOY:-$DEFAULT_CB}"
[ -x "$COWBOY" ] || COWBOY="cowboy"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# KEY_FILE=<path> reuses an existing funded key (skips the faucet wallet, whose
# freshly-credited balance can be "accepted" but not yet spendable — which drops
# the very first deploy and cascades the whole run into empty state).
KEY="${KEY_FILE:-$WORK/key}"; G=20000000
RESEARCH_CODE="$WORK/hl_research.py"

hx(){ printf "%s" "$1" | xxd -p -c 999999 | tr -d '\n'; }
addr_of(){ echo "$1" | awk '/Actor address:/ {print $NF}' | tail -1; }
nonce(){ curl -s --max-time 5 "$RPC_URL/account/${DEPLOYER#0x}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("nonce",0))' 2>/dev/null || echo 0; }
# wait for the payer nonce to actually advance (tx included). Inclusion latency
# runs tens of seconds, so poll up to ~150s, not 20 — a short window makes the
# next tx reuse a stale nonce and collide, cascading the whole run into failure.
settle(){ local n0="$1" i; for i in $(seq 1 75); do [ "$(nonce)" -gt "$n0" ] 2>/dev/null && return; sleep 2; done; }
# receipt-wait (NO --no-wait): the CLI polls the tx hash until it's included,
# which is robust to load-balanced RPC replica skew. Nonce polling is NOT — the
# nonce flaps across replicas, so settle() false-fired and cascaded the run.
# These actors' _parse() does json.loads() on the raw payload bytes, so the
# handler wants raw JSON-string bytes — which is exactly what --payload "$(hx …)"
# ships. Do NOT use --payload-json here: that sends canonical CBOR, which
# json.loads() cannot read, so _parse() returns {} and every field (feeds,
# exec_addr, research_addr, …) silently defaults to empty — the actor inits with
# default coins only, ingest sees no feeds, and paper never learns the research
# address. (The init gate still passes on the default coins, hiding the failure.)
# The push restructure keeps scripted handlers inside the 512 default stamp;
# owner-driven trades pass the read-back payload to apply_ideas below.
send(){ local a="$1" h="$2" j="$3"; "$COWBOY" actor execute --actor "$a" --handler "$h" \
  --payload "$(hx "$j")" --private-key "$KEY" --cycles-limit $G --cells-limit $G >/dev/null 2>&1; }
rd(){ curl -s --max-time 20 -X POST "$RPC_URL/actor/read" -H 'Content-Type: application/json' \
  -d "{\"actor\":\"$1\",\"handler\":\"$2\",\"payload_json\":${3:-{}}}" | python3 -c 'import sys,json,base64
try: print(base64.b64decode(json.load(sys.stdin)["result"]).decode())
except Exception: print("{}")'; }
field(){ python3 -c "import sys,json;print(json.load(sys.stdin).get('$1'))" 2>/dev/null; }
step(){ echo; echo "── $* ──"; }

step "Wallet"
if [ -n "${KEY_FILE:-}" ]; then
  DEPLOYER="$("$COWBOY" wallet address --key "$KEY" 2>/dev/null)"
  echo "  deployer (provided key): $DEPLOYER"
else
  "$COWBOY" wallet create --output "$KEY" >/dev/null 2>&1
  DEPLOYER="$("$COWBOY" wallet address --key "$KEY" 2>/dev/null)"
  for i in 1 2 3; do curl -s --max-time 8 -X POST "$RPC_URL/faucet" -H 'Content-Type: application/json' -d "{\"address\":\"$DEPLOYER\"}" >/dev/null 2>&1; sleep 2; done
  echo "  deployer (faucet): $DEPLOYER"
fi

step "Deploy actors (self-contained, <=40M gas)"
# Timer-fired handlers do not receive CIP-26 library pins. Inline hl_common so
# the README's primary demo path deploys the same self-contained research actor
# as tools/setup_live.sh instead of failing with UnresolvedImport.
python3 "$DIR/tools/bundle.py" "$DIR/hl_research.py" "$RESEARCH_CODE" \
  || { echo "failed to bundle research actor" >&2; exit 1; }
R="$(addr_of "$("$COWBOY" actor deploy --code "$RESEARCH_CODE" --no-init --salt "$(hx "res-$(date +%s)")" --private-key "$KEY" --cycles-limit $G --cells-limit $G 2>&1)")"
P="$(addr_of "$("$COWBOY" actor deploy --code "$DIR/hl_paper_exec.py" --no-init --salt "$(hx "pap-$(date +%s)")" --private-key "$KEY" --cycles-limit $G --cells-limit $G 2>&1)")"
for a in "$R" "$P"; do for i in $(seq 1 20); do "$COWBOY" actor get --address "$a" >/dev/null 2>&1 && break; sleep 2; done; done
echo "  research: $R"; echo "  paper:    $P"

step "Fund + init + wire"
# fund-actor has no receipt-wait, so it returns while its tx is still pending.
# Snapshot the deployer nonce, then block until it advances by the two fund txs
# before the init sends below. Otherwise init asks the RPC for the deployer nonce
# while the fund txs are still pending, grabs a stale value, collides, and is
# silently dropped (added=0, skipped=1) — leaving the actor un-inited and the
# universe gate below aborting the whole run.
nf="$(nonce)"
for a in "$R" "$P"; do "$COWBOY" fund-actor --actor "$a" --amount 1000 --private-key "$KEY" >/dev/null 2>&1; done
for i in $(seq 1 75); do [ "$(nonce)" -ge $((nf + 2)) ] 2>/dev/null && break; sleep 2; done
send "$R" init '{"coins":["BTC","ETH","SOL"],"stocks":["NVDA","TSLA","AAPL"],"preipo":["OPENAI","ANTHROPIC"],"feeds":["https://www.wired.com/feed/tag/ai/latest/rss","https://hnrss.org/newest?q=crypto&count=5"],"mandate":"Cross-asset macro across crypto, US equities, and pre-IPO AI names."}'
send "$P" init '{"budget":1000,"max_positions":10,"min_confidence":55}'
send "$R" set_exec "{\"exec_addr\":\"$P\"}"
send "$P" set_research "{\"research_addr\":\"$R\"}"
# gate on init FINALIZING (accepted != applied on mesa) before driving loops
u=""; for i in $(seq 1 30); do u="$(rd "$R" get_universe | field all)"; case "$u" in ""|None|"[]") sleep 4;; *) break;; esac; done
echo "  universe: $u"
case "$u" in ""|None|"[]") echo "  ABORT: init did not finalize"; exit 1;; esac

step "Ingest (runner fetches RSS)"
send "$R" ingest_now "{}"
for i in $(seq 1 25); do sleep 6; w="$(rd "$R" get_stats | field window)"; [ "${w:-0}" -gt 0 ] 2>/dev/null && { echo "  corpus: $w items"; break; }; done

step "Synthesize (multi-dex prices + runner.llm -> thesis)"
b="$(rd "$R" get_stats | field synth_runs)"; send "$R" synthesize_now "{}"
for i in $(seq 1 40); do sleep 6; n="$(rd "$R" get_stats | field synth_runs)"; [ "${n:-0}" -gt "${b:-0}" ] 2>/dev/null && break; done
rd "$R" get_theses '{"active_only":true}' | python3 -c 'import sys,json
t=json.load(sys.stdin).get("theses") or []
if t: x=t[0]; print("  thesis:",x.get("statement","")[:160]); print("   long",x.get("long"),"short",x.get("short"),"conviction",x.get("conviction"))
else: print("  (no thesis)")'
IDEAS="$(rd "$R" get_ideas | python3 -c 'import sys,json
d=json.load(sys.stdin); print(json.dumps({"ideas":d.get("ideas",[])}))')"
printf '%s' "$IDEAS" | python3 -c 'import sys,json
print("  ideas:", [(i.get("asset"),i.get("side"),i.get("confidence")) for i in json.load(sys.stdin).get("ideas",[])])'

step "Paper rebalance #1 (apply ideas, open book at real prices)"
b="$(rd "$P" get_stats | field rebalances)"; send "$P" apply_ideas "$IDEAS"
for i in $(seq 1 40); do sleep 6; n="$(rd "$P" get_stats | field rebalances)"; [ "${n:-0}" -gt "${b:-0}" ] 2>/dev/null && break; done
rd "$P" get_portfolio > "$WORK/book1.json"
python3 -c 'import sys,json
for p in json.load(open(sys.argv[1])).get("positions",[]): print("  ",p.get("asset"),p.get("side"),"notional",round(p.get("notional",0),1),"@",p.get("entry_px"))' "$WORK/book1.json"

step "Paper rebalance #2 — ideas unchanged, so held names must NOT churn"
b="$(rd "$P" get_stats | field rebalances)"; send "$P" apply_ideas "$IDEAS"
for i in $(seq 1 40); do sleep 6; n="$(rd "$P" get_stats | field rebalances)"; [ "${n:-0}" -gt "${b:-0}" ] 2>/dev/null && break; done
rd "$P" get_portfolio > "$WORK/book2.json"
python3 -c 'import sys,json
b={p["asset"]:p for p in json.load(open(sys.argv[1])).get("positions",[])}
a={p["asset"]:p for p in json.load(open(sys.argv[2])).get("positions",[])}
same=[k for k in b if k in a and b[k].get("entry_px")==a[k].get("entry_px") and b[k].get("opened_block")==a[k].get("opened_block")]
print("  book #1:",sorted(b)); print("  book #2:",sorted(a))
print("  held with UNCHANGED entry+clock:",sorted(same))
print("  => ", "NO CHURN (diff-against-target works)" if same and set(same)==set(b)&set(a) else "book rotated")' "$WORK/book1.json" "$WORK/book2.json"

step "Mark-to-market + feedback (scorecard -> research)"
send "$P" mark_now "{}"; sleep 8
rd "$P" get_scorecard | python3 -c 'import sys,json
sc=json.load(sys.stdin).get("scorecard",[])
print("  scorecard:", [(s.get("asset"),s.get("status"),s.get("return_pct")) for s in sc])'
rd "$P" get_pnl | python3 -c 'import sys,json
d=json.load(sys.stdin); print("  pnl: equity",round(d.get("equity",0),2),"realized",round(d.get("realized_pnl",0),2),"unreal",round(d.get("unrealized_pnl",0),2),"open",d.get("open_positions"))'

step "Second synthesis — research folds the traded scorecard into context"
b="$(rd "$R" get_stats | field synth_runs)"; send "$R" synthesize_now "{}"
for i in $(seq 1 40); do sleep 6; n="$(rd "$R" get_stats | field synth_runs)"; [ "${n:-0}" -gt "${b:-0}" ] 2>/dev/null && break; done
echo "  synth_runs: $(rd "$R" get_stats | field synth_runs) (scorecard now weighted in the prompt)"
rd "$R" get_theses '{"active_only":true}' | python3 -c 'import sys,json
t=json.load(sys.stdin).get("theses") or []
print("  latest thesis:", (t[0].get("statement","")[:150]) if t else "(none)")'
echo
echo "Done. research=$R paper=$P"
