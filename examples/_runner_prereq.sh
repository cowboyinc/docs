#!/usr/bin/env bash

runner_heartbeat_required_wei() {
  local headroom_cby="${RUNNER_GAS_HEADROOM_CBY:-25000}"
  python3 - "$headroom_cby" <<'PY'
import sys
print(int(sys.argv[1]) * 1_000_000_000)
PY
}

runner_balance_at_least() {
  python3 - "$1" "$2" <<'PY'
import sys
sys.exit(0 if int(sys.argv[1]) >= int(sys.argv[2]) else 1)
PY
}

runner_active_addresses() {
  curl -fsS --max-time 5 "$RPC_URL/runners/active?limit=1000" 2>/dev/null \
    | python3 -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

runners = data.get("runners", []) if isinstance(data, dict) else data
for runner in runners:
    if isinstance(runner, dict):
        addr = runner.get("address")
    elif isinstance(runner, str):
        addr = runner
    else:
        addr = None
    if addr:
        print(addr)
'
}

runner_account_balance_wei() {
  local addr="$1"
  curl -fsS --max-time 5 "$RPC_URL/account/$addr" 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("balance", 0))' 2>/dev/null \
    || echo 0
}

runner_faucet_if_available() {
  local addr="$1"
  curl -fsS --max-time 5 -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}" >/dev/null 2>&1
}

eligible_runner_rows() {
  local max_age_blocks="${1:-${RUNNER_HEARTBEAT_MAX_AGE_BLOCKS:-50}}"
  local required_job_type="${DEMO_RUNNER_JOB_TYPE:-llm}"
  local min_reputation_ema_x1e9="${RUNNER_REPUTATION_EMA_MIN_X1E9:-50000000000}"

  curl -fsS --max-time 5 "$RPC_URL/runners/active?limit=1000" 2>/dev/null \
    | python3 -c '
import json
import sys

try:
    max_age = int(sys.argv[1])
except Exception:
    max_age = 50
required_job_type = sys.argv[2]
try:
    min_reputation_ema_x1e9 = int(sys.argv[3])
except Exception:
    min_reputation_ema_x1e9 = 50000000000

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

runners = data.get("runners", []) if isinstance(data, dict) else data
current_block = int(data.get("current_block", 0)) if isinstance(data, dict) else 0

def reputation_value(value):
    if isinstance(value, dict):
        for key in ("score", "value", "ema", "legacy"):
            if key in value:
                return reputation_value(value[key])
        return 0
    try:
        return int(value)
    except Exception:
        return 0

for runner in runners:
    if not isinstance(runner, dict):
        continue
    addr = runner.get("address")
    if not addr:
        continue

    capabilities = runner.get("capabilities") or {}
    job_types = capabilities.get("job_types") or []
    active_jobs = int(runner.get("active_jobs") or 0)
    max_jobs = int(capabilities.get("max_concurrent_jobs") or 1)
    last_heartbeat = int(runner.get("last_heartbeat") or 0)
    probation_until = int(runner.get("probation_until") or 0)
    age = current_block - last_heartbeat if current_block >= last_heartbeat else max_age + 1
    health = str(runner.get("health") or "").lower()
    reputation = reputation_value(runner.get("reputation"))
    has_reputation_ema = "reputation_ema_x1e9" in runner
    reputation_ema = reputation_value(runner.get("reputation_ema_x1e9"))

    if health != "healthy":
        continue
    if age > max_age:
        continue
    if has_reputation_ema:
        if reputation_ema < min_reputation_ema_x1e9:
            continue
    elif reputation < 50:
        continue
    if required_job_type and required_job_type not in job_types:
        continue
    if active_jobs >= max_jobs:
        continue
    if current_block < probation_until:
        continue

    print(f"{addr} {age} {current_block} {last_heartbeat} {reputation_ema}")
' "$max_age_blocks" "$required_job_type" "$min_reputation_ema_x1e9"
}

wait_runner_eligible_for_jobs() {
  if [[ "${DEMO_SKIP_RUNNER_CHECK:-0}" == "1" ]]; then
    return 0
  fi

  local attempts="${RUNNER_ELIGIBLE_POLL_ATTEMPTS:-30}"
  local delay="${RUNNER_ELIGIBLE_POLL_DELAY:-1}"
  local max_age_blocks="${RUNNER_HEARTBEAT_MAX_AGE_BLOCKS:-50}"
  local i rows addr age current_block last_heartbeat reputation_ema

  for ((i = 1; i <= attempts; i++)); do
    if rows="$(eligible_runner_rows "$max_age_blocks")" && [[ -n "$rows" ]]; then
      read -r addr age current_block last_heartbeat reputation_ema <<< "$(printf '%s\n' "$rows" | head -1)"
      if [[ -n "${reputation_ema:-}" && "$reputation_ema" != "0" ]]; then
        echo "Runner readiness check: $addr heartbeat age ${age}/${max_age_blocks} blocks, EMA reputation $reputation_ema at block $current_block."
      else
        echo "Runner readiness check: $addr heartbeat age ${age}/${max_age_blocks} blocks at block $current_block."
      fi
      return 0
    fi
    sleep "$delay"
  done

  echo "No eligible runner became fresh within ${attempts}s."
  echo "Runner-backed demos need a healthy runner with heartbeat age <= ${max_age_blocks} blocks, scheduler reputation >= 50, supported job type, spare capacity, and no active probation."
  curl -fsS --max-time 5 "$RPC_URL/runners/active?limit=1000" 2>/dev/null || true
  echo ""
  return 1
}

wait_runner_balance() {
  local addr="$1"
  local required_wei="$2"
  local attempts="${RUNNER_FUNDING_POLL_ATTEMPTS:-10}"
  local delay="${RUNNER_FUNDING_POLL_DELAY:-2}"
  local i balance=0

  for ((i = 1; i <= attempts; i++)); do
    balance="$(runner_account_balance_wei "$addr")"
    if runner_balance_at_least "$balance" "$required_wei"; then
      echo "$balance"
      return 0
    fi
    sleep "$delay"
  done

  echo "$balance"
  return 1
}

ensure_runner_heartbeat_funds() {
  if [[ "${DEMO_SKIP_RUNNER_CHECK:-0}" == "1" ]]; then
    return 0
  fi

  local required_wei
  required_wei="$(runner_heartbeat_required_wei)"

  local addresses
  if ! addresses="$(runner_active_addresses)"; then
    echo "Could not query active runners from $RPC_URL."
    return 1
  fi

  if [[ -z "$addresses" ]]; then
    echo "No active runners found at $RPC_URL."
    echo "Runner-backed demos need at least one registered runner with spendable CBY for heartbeat gas."
    return 1
  fi

  local addr balance funded_addr funded_balance
  while IFS= read -r addr; do
    [[ -n "$addr" ]] || continue
    balance="$(runner_account_balance_wei "$addr")"
    if runner_balance_at_least "$balance" "$required_wei"; then
      funded_addr="$addr"
      funded_balance="$balance"
      break
    fi

    if [[ "${DEMO_TOP_UP_RUNNERS:-1}" == "1" ]]; then
      echo "Runner $addr has $balance wei; requesting faucet top-up for heartbeat gas..."
      if runner_faucet_if_available "$addr"; then
        if funded_balance="$(wait_runner_balance "$addr" "$required_wei")"; then
          funded_addr="$addr"
          break
        fi
      fi
    fi
  done <<< "$addresses"

  if [[ -z "${funded_addr:-}" ]]; then
    echo "Active runners exist, but none has RUNNER_GAS_HEADROOM_CBY=${RUNNER_GAS_HEADROOM_CBY:-25000} CBY spendable for heartbeat gas."
    echo "Fund a runner operating account, or leave DEMO_TOP_UP_RUNNERS=1 when the RPC faucet is available."
    return 1
  fi

  echo "Runner heartbeat gas check: $funded_addr has $funded_balance wei spendable."
  wait_runner_eligible_for_jobs
}
