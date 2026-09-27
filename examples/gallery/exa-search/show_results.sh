#!/usr/bin/env bash
# Print one request record for the expected buyer.
#
# Actor storage is transparent chain state. This operator tool checks the
# record's buyer to prevent an accidental cross-buyer read, but it does not add
# confidentiality. Buyer Actors must use the authenticated get_result handler.
#
#   BUYER=0x... ./show_results.sh <actor> <request-id> [rpc]
set -euo pipefail

ACTOR="${1:-${ACTOR:-}}"
REQUEST_ID="${2:-${REQUEST_ID:-}}"
RPC="${3:-${COWBOY_RPC_URL:-http://localhost:4000}}"
BUYER="${BUYER:-}"
[ -n "$ACTOR" ] && [ -n "$REQUEST_ID" ] && [ -n "$BUYER" ] \
  || { echo "usage: BUYER=0x... ./show_results.sh <actor> <request-id> [rpc]"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

curl -sf --max-time 25 "$RPC/actor/$ACTOR" | python3 -c '
import json,sys

request_id=sys.argv[1]
expected_buyer=sys.argv[2].lower()

from receipt_request_id import decode_cbor

doc=json.load(sys.stdin)
storage=doc.get("storage",{})
key="0x"+("request:"+request_id).encode().hex()
raw=storage.get(key)
if not raw:
    raise SystemExit("request %s is not retained" % request_id)
record=decode_cbor(bytes.fromhex(raw[2:] if raw.startswith("0x") else raw))
if str(record.get("buyer","")).lower() != expected_buyer:
    raise SystemExit("request buyer does not match BUYER")

print("request:", record.get("request_id"))
print("hire:", record.get("hire_id"))
print("status:", record.get("status","unknown"))
if record.get("status") == "pending":
    print("deadline_height:", record.get("deadline_height"))
elif record.get("status") == "error":
    print("error:", record.get("error","search failed"))
else:
    print("query:", record.get("query",""))
    for index,row in enumerate(record.get("results",[]),1):
        print()
        print("%d. %s" % (index,row.get("title") or "(no title)"))
        print("   %s" % row.get("url",""))
        snippet=(row.get("text") or "").replace("\n"," ")[:150]
        if snippet: print("   %s..." % snippet)
' "$REQUEST_ID" "$BUYER"
