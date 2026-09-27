#!/usr/bin/env bash

set -euo pipefail

demo_is_verbose() {
  [[ "${COWBOY_EXAMPLES_VERBOSE:-0}" == "1" ]]
}

demo_use_color() {
  [[ -t 1 || -t 2 ]]
}

if demo_use_color; then
  DEMO_CLR_RESET=$'\033[0m'
  DEMO_CLR_CYAN=$'\033[0;36m'
  DEMO_CLR_GREEN=$'\033[0;32m'
  DEMO_CLR_YELLOW=$'\033[1;33m'
  DEMO_CLR_RED=$'\033[0;31m'
  DEMO_CLR_BOLD=$'\033[1m'
else
  DEMO_CLR_RESET=''
  DEMO_CLR_CYAN=''
  DEMO_CLR_GREEN=''
  DEMO_CLR_YELLOW=''
  DEMO_CLR_RED=''
  DEMO_CLR_BOLD=''
fi

demo_emit() {
  local fd="$1"
  shift
  printf '%b\n' "$*" >&"$fd"
}

demo_info() {
  demo_emit 1 "  ${DEMO_CLR_CYAN}ℹ${DEMO_CLR_RESET} $*"
}

demo_ok() {
  demo_emit 1 "  ${DEMO_CLR_GREEN}✓${DEMO_CLR_RESET} $*"
}

demo_warn() {
  demo_emit 1 "  ${DEMO_CLR_YELLOW}⚠${DEMO_CLR_RESET} $*"
}

demo_fail() {
  demo_emit 1 "  ${DEMO_CLR_RED}✗${DEMO_CLR_RESET} $*"
}

demo_print_command() {
  local fd="${1:-1}"
  shift
  local command_text="$*"
  demo_emit "$fd" ""
  demo_emit "$fd" "  ${DEMO_CLR_CYAN}▶ Running${DEMO_CLR_RESET}"
  demo_emit "$fd" "    $command_text"
}

demo_shorten_address() {
  local value="${1:-}"
  if [[ "$value" =~ ^0x[0-9a-fA-F]{16,}$ ]]; then
    printf '%s...%s\n' "${value:0:10}" "${value: -8}"
    return 0
  fi
  printf '%s\n' "$value"
}

demo_format_json_for_display() {
  local payload="${1:-}"
  python3 - "$payload" <<'PY'
import json
import re
import sys

text = sys.argv[1]

def shorten(value):
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{16,}", value):
        return value[:10] + "..." + value[-8:]
    if isinstance(value, list):
        return [shorten(item) for item in value]
    if isinstance(value, dict):
        return {key: shorten(item) for key, item in value.items()}
    return value

try:
    parsed = json.loads(text)
except Exception:
    print(text)
    raise SystemExit(0)

print(json.dumps(shorten(parsed), indent=2))
PY
}

demo_token_list_items_json() {
  python3 -c '
import json
import sys

text = sys.stdin.read().strip()
if not text:
    print("[]")
    raise SystemExit(0)

start_candidates = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
if start_candidates:
    text = text[min(start_candidates):]

try:
    parsed = json.loads(text)
except Exception:
    print("[]")
    raise SystemExit(0)

if isinstance(parsed, dict):
    items = parsed.get("items")
    if items is None:
        items = parsed.get("tokens")
    if items is None:
        items = parsed.get("data")
elif isinstance(parsed, list):
    items = parsed
else:
    items = []

if not isinstance(items, list):
    items = []
print(json.dumps(items))
'
}

demo_print_described_command() {
  local fd="${1:-1}"
  local action="${2:-}"
  local actor="${3:-}"
  local handler="${4:-}"
  local payload="${5:-}"
  local expect="${6:-}"

  demo_emit "$fd" ""
  demo_emit "$fd" "  ${DEMO_CLR_CYAN}▶ Action${DEMO_CLR_RESET}"
  [[ -n "$action" ]] && demo_emit "$fd" "    Action: $action"
  [[ -n "$actor" ]] && demo_emit "$fd" "    Actor: $actor"
  [[ -n "$handler" ]] && demo_emit "$fd" "    Handler: $handler"
  if [[ -n "$payload" ]]; then
    demo_emit "$fd" "    Payload:"
    while IFS= read -r line; do
      demo_emit "$fd" "      $line"
    done <<<"$(demo_format_json_for_display "$payload")"
  fi
  [[ -n "$expect" ]] && demo_emit "$fd" "    Expect: $expect"
}

demo_print_raw_output() {
  local fd="${1:-1}"
  local output="${2:-}"
  [[ -z "$output" ]] && return 0
  while IFS= read -r line; do
    demo_emit "$fd" "$line"
  done <<<"$output"
}

demo_extract_matching_lines() {
  local output="$1"
  local pattern="$2"
  printf '%s\n' "$output" | grep -E "$pattern" || true
}

demo_unique_lines() {
  local text="${1:-}"
  [[ -z "$text" ]] && return 0
  printf '%s\n' "$text" | awk 'NF && !seen[$0]++'
}

demo_normalize_status_lines() {
  local text="${1:-}"
  [[ -z "$text" ]] && return 0
  printf '%s\n' "$text" | sed -E 's/Status: String\("([^"]+)"\)/Status: \1/g'
}

demo_extract_event_lines() {
  local output="$1"
  printf '%s\n' "$output" | awk '
    /^  Events:/ { capture=1; next }
    capture && /^    - / { print; next }
    capture && /^  Deferred transactions:/ { capture=0 }
    capture && NF == 0 { capture=0 }
    capture && !/^    / { capture=0 }
  ' || true
}

demo_output_has_skipped_submission() {
  local output="$1"
  python3 -c '
import json, re, sys
text = sys.stdin.read()
for match in re.finditer(r"\{[^{}]*\"added\"\s*:\s*\d+[^{}]*\"skipped\"\s*:\s*\d+[^{}]*\}", text):
    try:
        obj = json.loads(match.group(0))
    except Exception:
        continue
    if int(obj.get("skipped", 0) or 0) > 0:
        raise SystemExit(0)
raise SystemExit(1)
' <<<"$output"
}

demo_output_has_execution_error() {
  local output="$1"
  printf '%s' "$output" | grep -q 'ExecutionError'
}

demo_require_no_execution_error() {
  local label="$1"
  local output="${2:-${DEMO_LAST_OUTPUT:-}}"
  if demo_output_has_execution_error "$output"; then
    demo_fail "$label finalized with an execution error"
    return 1
  fi
}

demo_output_has_success_receipt() {
  local output="$1"
  printf '%s' "$output" | grep -Eq 'Status: (String\("Success"\)|Success)|"status"[[:space:]]*:[[:space:]]*"Success"'
}

demo_output_has_accepted_unfinalized_submission() {
  local output="$1"
  printf '%s' "$output" | grep -Eqi 'Transaction receipt not found: timeout|Timed out after [0-9]+s waiting for (deploy|execute) receipt'
}

demo_output_should_retry() {
  local output="$1"
  if demo_output_has_skipped_submission "$output"; then
    return 0
  fi
  if demo_output_has_accepted_unfinalized_submission "$output"; then
    return 1
  fi
  if printf '%s' "$output" | grep -Eqi 'StoreError\(ActorNotFound\)|actor not found|InvalidNonce|out of cycles: used [0-9]+, limit [0-9]+'; then
    return 0
  fi
  if demo_output_has_execution_error "$output"; then
    return 1
  fi
  return 1
}

demo_wait_for_accepted_unfinalized_submission() {
  local fd="$1"
  local label="$2"
  local output="$3"
  local rpc_url="${RPC_URL:-${COWBOY_RPC_URL:-}}"
  local tx_hash

  tx_hash="$(printf "%s" "$output" | demo_extract_tx_hash)"
  if [[ -z "$tx_hash" ]]; then
    demo_fail "$label was accepted but did not include a transaction hash"
    demo_print_raw_output "$fd" "$output"
    return 1
  fi

  if [[ -z "$rpc_url" ]]; then
    demo_fail "$label was accepted as tx $tx_hash, but no RPC_URL/COWBOY_RPC_URL is set for receipt polling"
    return 1
  fi

  demo_warn "$label was accepted as tx $tx_hash; polling for a finalized receipt instead of resubmitting"
  if demo_require_successful_receipt "$rpc_url" "$label" "$output"; then
    DEMO_LAST_STATUS=0
    demo_ok "$label finalized"
    return 0
  fi
  DEMO_LAST_STATUS=1
  return 1
}

demo_retry_command() {
  local fd="${1:-1}"
  local attempts="${2:-3}"
  local delay="${3:-1}"
  shift 3
  local command_text="$1"
  shift

  local attempt status output
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if demo_capture_command "$fd" "$command_text" "$@"; then
      return 0
    fi
    status="$DEMO_LAST_STATUS"
    output="$DEMO_LAST_OUTPUT"
    if demo_output_has_accepted_unfinalized_submission "$output"; then
      demo_wait_for_accepted_unfinalized_submission "$fd" "$command_text" "$output"
      return $?
    fi
    if [[ "$attempt" -lt "$attempts" ]] && demo_output_should_retry "$output"; then
      sleep "$delay"
      continue
    fi
    return "$status"
  done
  return 1
}

demo_poll_actor_ready() {
  local cowboy_bin="$1"
  local addr="$2"
  local attempts="${3:-20}"
  local delay="${4:-2}"
  local i output status
  for ((i = 1; i <= attempts; i++)); do
    if output="$("$cowboy_bin" actor get --address "$addr" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    if [[ "$status" -eq 0 ]] && printf '%s' "$output" | grep -Eq 'Actor Information:|code_hash|Code hash'; then
      return 0
    fi
    sleep "$delay"
  done
  echo "Actor $addr did not become queryable after deployment."
  return 1
}

demo_is_local_rpc_url() {
  local rpc_url="$1"
  [[ "$rpc_url" =~ ^http://(localhost|127\.0\.0\.1):[0-9]+$ ]]
}

demo_cby_to_base_units() {
  local cby="$1"
  python3 - "$cby" <<'PY'
from decimal import Decimal
import sys

print(int(Decimal(sys.argv[1]) * Decimal(1_000_000_000)))
PY
}

demo_int_at_least() {
  python3 - "$1" "$2" <<'PY'
import sys

try:
    actual = int(sys.argv[1] or 0)
    required = int(sys.argv[2] or 0)
except Exception:
    raise SystemExit(1)

raise SystemExit(0 if actual >= required else 1)
PY
}

demo_native_balance_wei() {
  local rpc_url="$1"
  local addr="$2"
  curl -fsS --max-time 5 "$rpc_url/account/$addr" 2>/dev/null \
    | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("balance", 0) or 0))' 2>/dev/null \
    || echo 0
}

demo_wait_for_native_balance() {
  local rpc_url="$1"
  local addr="$2"
  local min_cby="${3:-${DEMO_NATIVE_MIN_CBY:-4000}}"
  local attempts="${4:-${DEMO_NATIVE_FUNDING_POLL_ATTEMPTS:-45}}"
  local delay="${5:-${DEMO_NATIVE_FUNDING_POLL_DELAY:-2}}"
  local required_wei balance i

  required_wei="$(demo_cby_to_base_units "$min_cby")"
  balance=0
  for ((i = 1; i <= attempts; i++)); do
    balance="$(demo_native_balance_wei "$rpc_url" "$addr")"
    if demo_int_at_least "$balance" "$required_wei"; then
      DEMO_NATIVE_BALANCE_WEI="$balance"
      return 0
    fi
    [[ "$i" -lt "$attempts" ]] && sleep "$delay"
  done

  DEMO_NATIVE_BALANCE_WEI="$balance"
  return 1
}

demo_extract_tx_hash() {
  python3 -c '
import re
import sys

text = sys.stdin.read()
patterns = (
    r"(?:Transaction hash|Tx hash):\s*(?:0x)?([0-9a-fA-F]{64})(?![0-9a-fA-F])",
    r"\"tx_hash\"\s*:\s*\"(?:0x)?([0-9a-fA-F]{64})(?![0-9a-fA-F])\"",
    r"\btx\s+(?:0x)?([0-9a-fA-F]{64})(?![0-9a-fA-F])",
)
for pattern in patterns:
    match = re.search(pattern, text)
    if match:
        print(match.group(1).lower())
        raise SystemExit(0)
print("")
'
}

demo_receipt_tx_hash() {
  local tx_hash="$1"
  [[ -n "$tx_hash" ]] || return 1
  tx_hash="${tx_hash#0x}"
  tx_hash="${tx_hash#0X}"
  printf "0x%s\n" "$tx_hash"
}

demo_receipt_is_success() {
  python3 -c '
import json
import sys

receipt = json.load(sys.stdin)
status = receipt.get("status", "")
if not isinstance(status, str):
    status = json.dumps(status, separators=(",", ":"))
raise SystemExit(0 if "Success" in status else 1)
'
}

demo_wait_for_transaction_receipt() {
  local rpc_url="$1"
  local tx_hash="$2"
  local label="${3:-transaction}"
  local attempts="${4:-${DEMO_TX_COMMIT_WAIT_ATTEMPTS:-120}}"
  local delay="${5:-${DEMO_TX_COMMIT_WAIT_DELAY:-1}}"
  local receipt_hash receipt_json i

  DEMO_TX_RECEIPT_JSON=""
  receipt_hash="$(demo_receipt_tx_hash "$tx_hash")" || return 1
  for ((i = 1; i <= attempts; i++)); do
    if receipt_json="$(curl -fsS --max-time 3 "$rpc_url/transaction/$receipt_hash/receipt" 2>/dev/null)"; then
      if printf "%s" "$receipt_json" | demo_receipt_is_success; then
        DEMO_TX_RECEIPT_JSON="$receipt_json"
        return 0
      fi
      demo_fail "Receipt for $label was not successful."
      DEMO_TX_RECEIPT_JSON="$receipt_json"
      return 2
    fi
    [[ "$i" -lt "$attempts" ]] && sleep "$delay"
  done
  return 1
}

demo_require_successful_receipt() {
  local rpc_url="$1"
  local label="$2"
  local output="${3:-${DEMO_LAST_OUTPUT:-}}"
  local tx_hash wait_status

  if demo_output_has_execution_error "$output"; then
    demo_fail "$label finalized with an execution error"
    return 1
  fi

  if demo_output_has_success_receipt "$output"; then
    return 0
  fi

  tx_hash="$(printf "%s" "$output" | demo_extract_tx_hash)"
  if [[ -z "$tx_hash" ]]; then
    demo_fail "$label did not include a success receipt or transaction hash"
    return 1
  fi

  if demo_wait_for_transaction_receipt "$rpc_url" "$tx_hash" "$label"; then
    return 0
  else
    wait_status=$?
  fi
  if [[ "$wait_status" -eq 2 ]]; then
    return 1
  fi

  demo_fail "$label was accepted but did not finalize before the demo timeout"
  demo_emit 1 "  tx: $tx_hash"
  return 1
}

demo_wait_for_last_tx_receipt() {
  local rpc_url="$1"
  local label="$2"
  local output="${3:-${DEMO_LAST_OUTPUT:-}}"
  demo_require_successful_receipt "$rpc_url" "$label" "$output"
}

demo_wait_for_sender_commit() {
  local rpc_url="$1"
  local sender="$2"
  local previous_nonce="$3"
  local label="$4"
  local tx_hash="${5:-}"
  local attempts="${6:-${DEMO_TX_COMMIT_WAIT_ATTEMPTS:-120}}"
  local delay="${7:-${DEMO_TX_COMMIT_WAIT_DELAY:-1}}"
  local current_nonce i wait_status

  for ((i = 1; i <= attempts; i++)); do
    if [[ -n "$tx_hash" ]]; then
      if demo_wait_for_transaction_receipt "$rpc_url" "$tx_hash" "$label" 1 0; then
        return 0
      else
        wait_status=$?
      fi
      if [[ "$wait_status" -eq 2 ]]; then
        return 2
      fi
    fi
    current_nonce="$(curl -fsS --max-time 5 "$rpc_url/account/$sender" 2>/dev/null \
      | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("nonce", 0) or 0))' 2>/dev/null \
      || echo "$previous_nonce")"
    if [[ "$current_nonce" =~ ^[0-9]+$ ]] && (( current_nonce > previous_nonce )); then
      return 0
    fi
    [[ "$i" -lt "$attempts" ]] && sleep "$delay"
  done
  return 1
}

demo_local_faucet_key_file() {
  local scratch_dir="$1"
  local key_hex="${DEMO_LOCAL_FAUCET_KEY_HEX:-ffffffffffffffff000000000000000000000000000000000000000000000000}"
  local key_file="$scratch_dir/demo-local-faucet-key"

  mkdir -p "$scratch_dir"
  printf "%s\n" "$key_hex" > "$key_file"
  printf "%s\n" "$key_file"
}

demo_acquire_local_faucet_lock() {
  local lock_dir="${DEMO_NATIVE_FAUCET_LOCK_DIR:-${TMPDIR:-/tmp}/cowboy-demo-local-faucet.lock}"
  local attempts="${DEMO_NATIVE_FAUCET_LOCK_ATTEMPTS:-120}"
  local accepted_attempts="${DEMO_NATIVE_ACCEPTED_FUNDING_POLL_ATTEMPTS:-180}"
  local accepted_delay="${DEMO_NATIVE_FUNDING_POLL_DELAY:-2}"
  local default_stale_seconds=900
  if [[ "$accepted_attempts" =~ ^[0-9]+$ && "$accepted_delay" =~ ^[0-9]+$ ]]; then
    default_stale_seconds=$((accepted_attempts * accepted_delay + 120))
    (( default_stale_seconds < 900 )) && default_stale_seconds=900
  fi
  local stale_seconds="${DEMO_NATIVE_FAUCET_LOCK_STALE_SECONDS:-$default_stale_seconds}"
  local i age

  for ((i = 1; i <= attempts; i++)); do
    if mkdir "$lock_dir" 2>/dev/null; then
      DEMO_LOCAL_FAUCET_LOCK_DIR="$lock_dir"
      printf "%s\n" "$$" > "$lock_dir/pid" 2>/dev/null || true
      return 0
    fi
    age="$(python3 -c 'import os,sys,time
p=sys.argv[1]
try:
    print(int(time.time() - os.stat(p).st_mtime))
except Exception:
    print(0)
' "$lock_dir" 2>/dev/null || echo 0)"
    if [[ "$age" =~ ^[0-9]+$ && "$age" -ge "$stale_seconds" ]]; then
      rm -f "$lock_dir/pid" 2>/dev/null || true
      if rmdir "$lock_dir" 2>/dev/null; then
        demo_warn "Cleared stale local faucet lock at $lock_dir"
        continue
      fi
    fi
    sleep 1
  done

  demo_warn "Timed out waiting for local faucet lock at $lock_dir"
  return 1
}

demo_release_local_faucet_lock() {
  local lock_dir="${DEMO_LOCAL_FAUCET_LOCK_DIR:-}"
  if [[ -n "$lock_dir" ]]; then
    rm -f "$lock_dir/pid" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || true
  fi
  DEMO_LOCAL_FAUCET_LOCK_DIR=""
}

demo_wait_for_funding_finality() {
  local rpc_url="$1"
  local addr="$2"
  local min_cby="$3"
  local attempts="$4"
  local delay="$5"
  local label="$6"
  local tx_hash="${7:-}"
  local wait_status

  if [[ -n "$tx_hash" ]]; then
    if demo_wait_for_transaction_receipt "$rpc_url" "$tx_hash" "$label" "$attempts" "$delay"; then
      :
    else
      wait_status=$?
      if [[ "$wait_status" -eq 2 ]]; then
        return 2
      fi
      demo_warn "$label was accepted, but its receipt did not finalize"
      return 1
    fi
  fi

  if demo_wait_for_native_balance "$rpc_url" "$addr" "$min_cby" "$attempts" "$delay"; then
    return 0
  fi
  demo_warn "$label finalized, but balance stayed below ${min_cby} CBY"
  return 1
}

demo_transfer_from_local_faucet() {
  local rpc_url="$1"
  local cowboy_bin="$2"
  local addr="$3"
  local scratch_dir="$4"
  local amount_cby="$5"
  local min_cby="${6:-}"
  local wait_attempts="${7:-${DEMO_NATIVE_FUNDING_POLL_ATTEMPTS:-45}}"
  local wait_delay="${8:-${DEMO_NATIVE_FUNDING_POLL_DELAY:-2}}"
  local key_file output attempts attempt status lock_owned tx_hash

  key_file="$(demo_local_faucet_key_file "$scratch_dir")"
  attempts="${DEMO_NATIVE_FAUCET_TRANSFER_ATTEMPTS:-5}"
  status=1
  lock_owned=0
  DEMO_LOCAL_FAUCET_TRANSFER_ACCEPTED=0
  DEMO_LOCAL_FAUCET_TRANSFER_HASH=""

  if [[ "${DEMO_LOCAL_FAUCET_LOCK_ALREADY_HELD:-0}" != "1" ]]; then
    if ! demo_acquire_local_faucet_lock; then
      return 1
    fi
    lock_owned=1
  fi

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if output="$("$cowboy_bin" transfer --rpc-url "$rpc_url" --private-key "$key_file" --to "$addr" --amount "$amount_cby" 2>&1)"; then
      if demo_output_has_skipped_submission "$output"; then
        if printf "%s" "$output" | grep -qiE 'nonce_too_low|stale nonce|InvalidNonce'; then
          demo_warn "Local faucet nonce is stale for $addr; falling back to HTTP faucet"
          status=1
          break
        fi
        demo_warn "Local faucet transfer to $addr was skipped: $output"
        status=1
        break
      fi
      DEMO_LOCAL_FAUCET_TRANSFER_ACCEPTED=1
      tx_hash="$(printf "%s" "$output" | demo_extract_tx_hash)"
      DEMO_LOCAL_FAUCET_TRANSFER_HASH="$tx_hash"
      if [[ -n "$min_cby" ]]; then
        if [[ -n "$tx_hash" ]]; then
          demo_warn "Local faucet accepted transfer for $addr ($tx_hash); waiting for finalized receipt"
        else
          demo_warn "Local faucet accepted transfer for $addr; waiting for finalized balance"
        fi
        if demo_wait_for_funding_finality "$rpc_url" "$addr" "$min_cby" "$wait_attempts" "$wait_delay" "Local faucet transfer for $addr" "$tx_hash"; then
          status=0
        else
          status=1
        fi
      else
        status=0
      fi
      break
    fi

    if printf "%s" "$output" | grep -qiE 'nonce_too_low|stale nonce|InvalidNonce'; then
      demo_warn "Local faucet nonce is stale for $addr; falling back to HTTP faucet"
      status=1
      break
    fi
    demo_warn "Local faucet transfer to $addr failed: $output"
    status=1
    break
  done

  if [[ "$status" -ne 0 && "$attempt" -gt "$attempts" ]]; then
    demo_warn "Local faucet transfer to $addr failed after $attempts attempts: $output"
  fi

  if [[ "$lock_owned" -eq 1 ]]; then
    demo_release_local_faucet_lock
  fi
  return "$status"
}

demo_request_http_faucet() {
  local rpc_url="$1"
  local addr="$2"
  local output
  DEMO_HTTP_FAUCET_OUTPUT=""
  DEMO_HTTP_FAUCET_TX_HASH=""
  if output="$(curl --max-time 5 --connect-timeout 2 -fsS -X POST "$rpc_url/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}")"; then
    DEMO_HTTP_FAUCET_OUTPUT="$output"
    DEMO_HTTP_FAUCET_TX_HASH="$(printf "%s" "$output" | demo_extract_tx_hash)"
    return 0
  fi
  return 1
}

demo_fund_native_account() {
  local rpc_url="$1"
  local cowboy_bin="$2"
  local addr="$3"
  local scratch_dir="${4:-${TMP_DIR:-${WORK_DIR:-${TMPDIR:-/tmp}}}}"
  local amount_cby="${5:-${DEMO_NATIVE_FUND_CBY:-100000}}"
  local min_cby="${6:-${DEMO_NATIVE_MIN_CBY:-4000}}"
  local attempts="${7:-${DEMO_NATIVE_FUNDING_POLL_ATTEMPTS:-45}}"
  local delay="${8:-${DEMO_NATIVE_FUNDING_POLL_DELAY:-2}}"
  local submit_attempts="${9:-${DEMO_NATIVE_FUNDING_SUBMIT_ATTEMPTS:-3}}"
  local accepted_attempts="${DEMO_NATIVE_ACCEPTED_FUNDING_POLL_ATTEMPTS:-180}"
  local submit_attempt

  if demo_wait_for_native_balance "$rpc_url" "$addr" "$min_cby" 1 0; then
    demo_ok "Native gas ready for $addr"
    return 0
  fi

  if demo_is_local_rpc_url "$rpc_url"; then
    for ((submit_attempt = 1; submit_attempt <= submit_attempts; submit_attempt++)); do
      if demo_transfer_from_local_faucet "$rpc_url" "$cowboy_bin" "$addr" "$scratch_dir" "$amount_cby" "$min_cby" "$accepted_attempts" "$delay"; then
        demo_ok "Native gas ready for $addr"
        return 0
      else
        if [[ "${DEMO_LOCAL_FAUCET_TRANSFER_ACCEPTED:-0}" == "1" ]]; then
          demo_fail "Local faucet transfer for $addr was accepted, but finalized balance was not observed"
          return 1
        else
          demo_warn "Could not submit local faucet transfer for $addr on attempt $submit_attempt/$submit_attempts"
        fi
        if demo_request_http_faucet "$rpc_url" "$addr"; then
          if [[ -n "${DEMO_HTTP_FAUCET_TX_HASH:-}" ]]; then
            demo_warn "Accepted HTTP faucet fallback for $addr (${DEMO_HTTP_FAUCET_TX_HASH}); waiting for finalized receipt"
          else
            demo_warn "Accepted HTTP faucet fallback for $addr; waiting for finalized balance"
          fi
          if demo_wait_for_funding_finality "$rpc_url" "$addr" "$min_cby" "$accepted_attempts" "$delay" "HTTP faucet fallback for $addr" "${DEMO_HTTP_FAUCET_TX_HASH:-}"; then
            demo_ok "Native gas ready for $addr"
            return 0
          fi
          demo_fail "HTTP faucet fallback for $addr was accepted, but balance stayed below ${min_cby} CBY"
          return 1
        else
          demo_warn "HTTP faucet fallback did not accept a top-up request for $addr"
        fi
      fi

      if demo_wait_for_native_balance "$rpc_url" "$addr" "$min_cby" "$attempts" "$delay"; then
        demo_ok "Native gas ready for $addr"
        return 0
      fi

      if [[ "$submit_attempt" -lt "$submit_attempts" ]]; then
        demo_warn "Native balance for $addr is still below ${min_cby} CBY; resubmitting faucet transfer"
      fi
    done
  else
    for ((submit_attempt = 1; submit_attempt <= submit_attempts; submit_attempt++)); do
      if demo_request_http_faucet "$rpc_url" "$addr"; then
        if [[ -n "${DEMO_HTTP_FAUCET_TX_HASH:-}" ]]; then
          demo_warn "Accepted HTTP faucet request for $addr (${DEMO_HTTP_FAUCET_TX_HASH}); waiting for finalized receipt"
        else
          demo_warn "Accepted HTTP faucet request for $addr; waiting for finalized balance"
        fi
        if demo_wait_for_funding_finality "$rpc_url" "$addr" "$min_cby" "$accepted_attempts" "$delay" "HTTP faucet request for $addr" "${DEMO_HTTP_FAUCET_TX_HASH:-}"; then
          demo_ok "Native gas ready for $addr"
          return 0
        fi
        demo_fail "HTTP faucet request for $addr was accepted, but balance stayed below ${min_cby} CBY"
        return 1
      else
        demo_warn "HTTP faucet did not accept a top-up request for $addr on attempt $submit_attempt/$submit_attempts"
      fi

      if demo_wait_for_native_balance "$rpc_url" "$addr" "$min_cby" "$attempts" "$delay"; then
        demo_ok "Native gas ready for $addr"
        return 0
      fi

      if [[ "$submit_attempt" -lt "$submit_attempts" ]]; then
        demo_warn "Native balance for $addr is still below ${min_cby} CBY; retrying HTTP faucet"
      fi
    done
  fi

  demo_fail "Native balance for $addr stayed below ${min_cby} CBY after funding attempt"
  return 1
}

demo_render_failure_output() {
  local fd="$1"
  local output="$2"
  demo_emit "$fd" "  ${DEMO_CLR_RED}✗ Command failed${DEMO_CLR_RESET}"
  local extracted
  extracted="$(demo_extract_matching_lines "$output" 'ExecutionError|StoreError|InvalidNonce|timeout|[Ff]ail|[Ee]rror|not found|rejected|unsupported')"
  if [[ -n "$extracted" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      demo_emit "$fd" "    $line"
    done <<<"$extracted"
    return 0
  fi
  demo_print_raw_output "$fd" "$output"
}

demo_render_compact_output() {
  local fd="$1"
  local output="$2"
  local rendered=0

  local tx_hashes actor_addresses code_hashes statuses deferred_lines success_lines
  tx_hashes="$(demo_extract_matching_lines "$output" 'Transaction hash:')"
  actor_addresses="$(demo_extract_matching_lines "$output" 'Actor address:')"
  code_hashes="$(demo_extract_matching_lines "$output" 'Code hash:')"
  statuses="$(demo_extract_matching_lines "$output" 'Status:')"
  deferred_lines="$(demo_extract_matching_lines "$output" 'Deferred transactions:')"
  success_lines="$(demo_extract_matching_lines "$output" 'submitted successfully|deployed successfully|executed successfully|successfully added to mempool|found in mempool')"

  tx_hashes="$(demo_unique_lines "$tx_hashes")"
  actor_addresses="$(demo_unique_lines "$actor_addresses")"
  code_hashes="$(demo_unique_lines "$code_hashes")"
  statuses="$(demo_normalize_status_lines "$(demo_unique_lines "$statuses")")"
  deferred_lines="$(demo_unique_lines "$deferred_lines")"

  if [[ -n "$tx_hashes$actor_addresses$code_hashes$statuses$deferred_lines$success_lines" ]]; then
    demo_emit "$fd" "  ${DEMO_CLR_GREEN}✓ Success${DEMO_CLR_RESET}"
    rendered=1
  fi

  for block in "$tx_hashes" "$actor_addresses" "$code_hashes" "$statuses" "$deferred_lines"; do
    [[ -z "$block" ]] && continue
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      demo_emit "$fd" "$line"
    done <<<"$block"
  done

  local events
  events="$(demo_extract_event_lines "$output")"
  if [[ -n "$events" ]]; then
    rendered=1
    demo_emit "$fd" "  ${DEMO_CLR_CYAN}📣 Events${DEMO_CLR_RESET}"
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      demo_emit "$fd" "$line"
    done <<<"$events"
  fi

  if [[ "$rendered" == "1" ]]; then
    return 0
  fi

  demo_print_raw_output "$fd" "$output"
}

demo_render_cowboy_output() {
  local fd="${1:-1}"
  local output="${2:-}"
  local status="${3:-0}"

  if demo_is_verbose; then
    demo_print_raw_output "$fd" "$output"
    return "$status"
  fi

  if [[ "$status" -ne 0 ]]; then
    demo_render_failure_output "$fd" "$output"
    return "$status"
  fi

  demo_render_compact_output "$fd" "$output"
}

demo_run_command() {
  local fd="${1:-1}"
  shift

  local output status attempt
  DEMO_TX_RECEIPT_JSON=""
  local max_attempts="${DEMO_SUBMIT_RETRY_ATTEMPTS:-3}"
  [[ "$max_attempts" -ge 1 ]] || max_attempts=1
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if output="$("$@" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    # Admission code 2017 is the node explicitly asking for a resubmit
    # (finality advanced while the submission was being validated) —
    # transient by definition; demos were failing on it instead of
    # retrying (COW-1346).
    if [[ "$status" -ne 0 && "$attempt" -lt "$max_attempts" ]] && grep -q '"code":2017' <<<"$output"; then
      demo_warn "Node asked to retry submission (admission code 2017); attempt $attempt/$max_attempts"
      sleep 1
      continue
    fi
    break
  done

  if [[ "$status" -eq 0 ]] && demo_output_has_skipped_submission "$output"; then
    status=1
  fi
  if [[ "$status" -eq 0 ]] && demo_output_has_execution_error "$output"; then
    status=1
  fi

  DEMO_LAST_OUTPUT="$output"
  DEMO_LAST_STATUS="$status"
  demo_render_cowboy_output "$fd" "$output" "$status"
  return "$status"
}

demo_capture_command() {
  local fd="${1:-1}"
  shift
  local command_text="$1"
  shift

  demo_print_command "$fd" "$command_text"
  demo_run_command "$fd" "$@"
}

demo_capture_described_command() {
  local fd="${1:-1}"
  shift
  local action="${1:-}"
  local actor="${2:-}"
  local handler="${3:-}"
  local payload="${4:-}"
  local expect="${5:-}"
  local command_text="${6:-}"
  shift 6

  if demo_is_verbose; then
    demo_print_command "$fd" "$command_text"
  else
    demo_print_described_command "$fd" "$action" "$actor" "$handler" "$payload" "$expect"
  fi

  demo_run_command "$fd" "$@"
}
