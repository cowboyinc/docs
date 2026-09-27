#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_demo_output.sh"

assert_eq() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $label" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    exit 1
  fi
}

hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

assert_eq "$(printf 'Transaction hash: 0x%s\n' "$hash" | demo_extract_tx_hash)" "$hash" "extract labeled tx hash"
assert_eq "$(printf 'tx 0x%s\n' "$hash" | demo_extract_tx_hash)" "$hash" "extract tx shorthand hash"
assert_eq "$(printf 'tx 0x%saa\n' "$hash" | demo_extract_tx_hash)" "" "reject hash prefix from longer hex token"
assert_eq "$(printf '{"tx_hash":"0x%s"}\n' "$hash" | demo_extract_tx_hash)" "$hash" "extract JSON tx hash"

items_json="$(printf 'noise\n{"items":[{"symbol":"A"},{"symbol":"B"}],"next_cursor":"cursor"}\n' | demo_token_list_items_json)"
python3 - "$items_json" <<'PY'
import json
import sys

items = json.loads(sys.argv[1])
if [item.get("symbol") for item in items] != ["A", "B"]:
    raise SystemExit("unexpected parsed token items")
PY

printf '{"status":"Success"}' | demo_receipt_is_success
if printf '{"status":"Failure"}' | demo_receipt_is_success; then
  echo "FAIL: rejected non-success receipt" >&2
  exit 1
fi

echo "demo output helper tests passed"
