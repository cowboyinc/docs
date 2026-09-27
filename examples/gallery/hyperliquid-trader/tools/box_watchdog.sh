#!/usr/bin/env bash
# Runs ON THE BOX (installed to /opt/river/box_watchdog.sh, driven by cron).
#
# Why on the box: every "did it die overnight?" ambiguity on 2026-08-08/09 came
# from the watcher AND the recovery living on a laptop that sleeps. A suspended
# laptop meant no observation and no recovery, and a 3.5h outage looked identical
# to a 3.5h gap in sampling. This has no such dependency.
#
# Recovers the two failure modes seen repeatedly:
#   1. actor timer chain dead  -> `start` re-arms all loops (does NOT touch the
#      book, unlike `init`). Only on a confirmed stall: `_schedule` ADDS a timer,
#      so re-arming a live loop stacks a self-perpetuating duplicate.
#   2. book stalled but loops alive -> force one cycle via `apply_ideas`
#      (@deferred, bypasses `_tick_ok`).
#
# It does NOT restart the validator. That clears the mempool deadlock but is too
# blunt for automation; `evict_stale` in the node should handle that within 600s.
set -uo pipefail
RPC="${RPC:-http://172.28.0.11:4000}"
STATE=/opt/river/.watchdog-state
LOG=/opt/river/watchdog.log
STALL_SECS=1080          # 18 min of an unchanged book
COOLDOWN_SECS=1500       # >= 25 min between nudges; never stack _apply runs
CLI=/opt/river/shared-bin/cowboy
KEY=/opt/river/shared-bin/deploykey

say(){ echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
[ -x "$CLI" ] || { say "no CLI at $CLI"; exit 0; }
[ -f "$KEY" ] || { say "no deploy key at $KEY"; exit 0; }
TRADER="${TRADER:-$(cat /opt/river/.trader_addr 2>/dev/null)}"
RESEARCH="${RESEARCH:-$(cat /opt/river/.research_addr 2>/dev/null)}"
[ -n "$TRADER" ] && [ -n "$RESEARCH" ] || { say "actor addrs unknown"; exit 0; }

read_pnl(){ curl -s -m 20 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
  -d "{\"actor\":\"$TRADER\",\"handler\":\"get_pnl\",\"payload_text\":\"{}\"}" 2>/dev/null \
  | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['result']).decode());print(d.get('equity'),d.get('open_positions'),d.get('rebalances'))" 2>/dev/null; }

now=$(date +%s)
sig="$(read_pnl)"
[ -z "$sig" ] && { say "book unreadable; chain down? skipping"; exit 0; }

# State layout is `since last_nudge sig...` and NOT `sig ...` — `sig` is three
# space-separated fields (equity positions rebalances), so it must come LAST for
# `read` to slurp it whole. With sig first, `since` parsed as the position count
# and `last_nudge` swallowed the rest of the line: stall was computed against a
# 3-second epoch (always "stalled"), the nudge guard became an arithmetic syntax
# error that silently never fired, and the state line grew every run. The
# watchdog looked healthy and recovered nothing. Observed 2026-08-09.
prev_sig=""; since=$now; last_nudge=0
if [ -f "$STATE" ]; then
  read -r since last_nudge prev_sig < "$STATE" 2>/dev/null || true
  # Reject a malformed/legacy state file rather than computing on garbage.
  case "$since$last_nudge" in
    *[!0-9]*|"") say "state malformed; resetting"; prev_sig=""; since=$now; last_nudge=0;;
  esac
fi
# `since` is when the CURRENT signature was first observed.
if [ "$sig" != "$prev_sig" ]; then
  [ -n "$prev_sig" ] && [ $((now-since)) -ge "$STALL_SECS" ] && say "recovered after $(( (now-since)/60 ))m -> $sig"
  since=$now
fi

stalled=$((now-since))
if [ "$stalled" -ge "$STALL_SECS" ] && [ $((now-last_nudge)) -ge "$COOLDOWN_SECS" ]; then
  h=$(curl -s -m 10 "$RPC/height" 2>/dev/null | grep -oE '[0-9]+')
  if [ -z "$h" ]; then
    say "chain unreachable; not nudging"
  else
    # DRY_RUN exercises the whole gate (stall math, cooldown, chain reachability)
    # without calling `start` — the recovery path CANNOT be safely rehearsed on a
    # healthy actor, since `_schedule` adds rather than replaces a timer.
    if [ -n "${DRY_RUN:-}" ]; then
      say "DRY_RUN: would nudge (stalled $((stalled/60))m at h=$h sig=$sig)"
      echo "$since $now $sig" > "$STATE"
      exit 0
    fi
    say "stalled $((stalled/60))m at h=$h sig=$sig -> nudging"
    for A in "$TRADER" "$RESEARCH"; do
      COWBOY_RPC_URL="$RPC" "$CLI" actor execute --actor "$A" --handler start \
        --payload-text '{}' --private-key "$KEY" \
        --cycles-limit 20000000 --cells-limit 20000000 >/dev/null 2>&1
    done
    say "  timers re-armed"
    COWBOY_RPC_URL="$RPC" "$CLI" actor execute --actor "$RESEARCH" --handler synthesize_now \
      --payload-text '{}' --private-key "$KEY" \
      --cycles-limit 20000000 --cells-limit 20000000 >/dev/null 2>&1
    sleep 90
    IDEAS=$(curl -s -m 30 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
      -d "{\"actor\":\"$RESEARCH\",\"handler\":\"get_ideas\",\"payload_text\":\"{}\"}" 2>/dev/null \
      | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['result']).decode());print(json.dumps({'ideas':d.get('ideas',[])}))" 2>/dev/null)
    if [ -n "$IDEAS" ] && [ "$IDEAS" != '{"ideas": []}' ]; then
      COWBOY_RPC_URL="$RPC" "$CLI" actor execute --actor "$TRADER" --handler apply_ideas \
        --payload-text "$IDEAS" --private-key "$KEY" \
        --cycles-limit 20000000 --cells-limit 20000000 >/dev/null 2>&1
      say "  nudge sent"
    else
      say "  no ideas; synthesis nudge only"
    fi
    last_nudge=$now
    since=$now
  fi
fi
echo "$since $last_nudge $sig" > "$STATE"
