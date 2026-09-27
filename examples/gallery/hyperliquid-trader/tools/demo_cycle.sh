#!/usr/bin/env bash
# Force one full research->trader cycle on command (for live demos).
#   RPC=http://<ip>:4000 ./tools/demo_cycle.sh
# Fires discover -> synthesize -> rebalance, printing ideas then the book.
set -uo pipefail
RPC="${RPC:-}"; [ -n "$RPC" ] || { echo "set RPC=http://<ip>:4000"; exit 1; }
export COWBOY_RPC_URL="$RPC"
S="$(cd "$(dirname "$0")" && pwd)"
R=$(cat "$S/research.txt"); T=$(cat "$S/actor.txt"); G=20000000
rd(){ curl -s --max-time 12 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
   -d "{\"actor\":\"$1\",\"handler\":\"$2\",\"payload_text\":\"{}\"}" \
   | python3 -c "import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)['result']).decode())" 2>/dev/null; }
ex(){ local payload="${3:-}"; [ -n "$payload" ] || payload='{}'
  cowboy actor execute --actor "$1" --handler "$2" --payload-text "$payload" \
   --private-key "$S/deploykey" --cycles-limit $G --cells-limit $G >/dev/null 2>&1; }

echo "1/4 discover_now ..."; ex "$R" discover_now; sleep 20
echo "2/4 synthesize_now ..."; ex "$R" synthesize_now
echo "   waiting for ideas (LLM ~1-2 min) ..."
for i in $(seq 1 12); do sleep 15
  n=$(rd "$R" get_ideas | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('ideas',[])))" 2>/dev/null||echo 0)
  [ "${n:-0}" -gt 0 ] && { echo "   $n ideas:"; rd "$R" get_ideas | python3 -c "import sys,json;[print('    ',i['asset'],i['side'],i.get('confidence'),i.get('thesis_fit')) for i in json.load(sys.stdin).get('ideas',[])[:6]]"; break; }
done
IDEAS=$(rd "$R" get_ideas | python3 -c "import sys,json;print(json.dumps({'ideas':json.load(sys.stdin).get('ideas',[])}))")
echo "3/4 apply_ideas ..."; ex "$T" apply_ideas "$IDEAS"; sleep 25
echo "4/4 book:"; rd "$T" get_portfolio | python3 -c "
import sys,json;d=json.load(sys.stdin)
for p in d.get('positions',[]): print('    ',p['asset'],p['side'],'\$%.0f'%p['notional'],'upnl %+.2f'%p['upnl'])
print('    equity \$%.2f'%d.get('equity',0)) if 'equity' in d else None"
rd "$T" get_pnl | python3 -c "import sys,json;d=json.load(sys.stdin);print('    open:',d.get('open_positions'),'| equity \$'+str(d.get('equity')),'| realized \$'+str(d.get('realized_pnl')))"
