#!/usr/bin/env bash
# Read any actor handler without curl:  RPC=http://<ip>:4000 ./tools/read.sh <research|trader|notify|0xADDR> <handler> [json]
# e.g.  RPC=... ./tools/read.sh research get_ideas
set -uo pipefail
RPC="${RPC:-${COWBOY_RPC_URL:-}}"; [ -n "$RPC" ] || { echo "set RPC=http://<ip>:4000"; exit 1; }
S="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  research) A=$(cat "$S/research.txt");;
  trader)   A=$(cat "$S/actor.txt");;
  notify)   A=$(cat "$S/notify.txt");;
  0x*)      A="$1";;
  *) echo "usage: read.sh <research|trader|notify|0xADDR> <handler> [json]"; exit 1;;
esac
H="${2:?handler required}"; P="${3:-{}}"
curl -s --max-time 15 -X POST "$RPC/actor/read" -H 'Content-Type: application/json' \
  -d "{\"actor\":\"$A\",\"handler\":\"$H\",\"payload_text\":$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$P")}" \
  | python3 -c "import sys,json,base64;d=json.load(sys.stdin);print(json.dumps(json.loads(base64.b64decode(d['result'])),indent=2) if 'result' in d else d)"
