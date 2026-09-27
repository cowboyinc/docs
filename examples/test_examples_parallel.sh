#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLES_DIR="$ROOT_DIR/examples"
WORKSPACE_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
NODE_ROOT="${NODE_DIR:-$WORKSPACE_ROOT/node}"
RPC_URL="${RPC_URL:-${COWBOY_RPC_URL:-http://localhost:${RPC_PORT:-4000}}}"
export RPC_URL
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"

ARTIFACT_DIR="${DEMO_PARALLEL_ARTIFACT_DIR:-$ROOT_DIR/artifacts/examples-parallel}"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_TOKEN="$(date +%H%M%S)-$$"
RUN_ROOT="$ARTIFACT_DIR/run-$RUN_ID"
DEFAULT_JOBS="${DEMO_PARALLEL_JOBS:-4}"
DEMO_PARALLEL_SCOPE="${DEMO_PARALLEL_SCOPE:-default}"

DEFAULT_DEMO_KEYS=(
  core/00-quickstart-hello
  core/01-state-events-queries
  core/02-senders-and-permissions
  core/03-two-actor-messaging
  core/04-multi-actor-workflow
  core/05-tokens-and-balances
  core/06-token-escrow
  core/07-timers-and-automation
  core/08-minimal-runner-continuation
  core/09-runner-llm
  core/10-async-cache-refresh
  core/11-failure-recovery
  core/12-capstone-agent
  gallery/rebalancing-agent
  gallery/liquidity-pools
  gallery/copy-trading
  gallery/spam-detection
  gallery/content-recommendations
  gallery/sentiment-analysis
  gallery/audio-transcription
  gallery/image-generation
  gallery/content-moderator
  gallery/alerts-and-schedulers
  gallery/liquidation-oracle
  gallery/dao-copilot
  gallery/compliance
  gallery/fraud-watcher
  gallery/contract-summarizer
  gallery/hn-feed
  gallery/casino-rounds
  gallery/blackjack
)

SELECTORS=("$@")
ALL_KEYS=()
ALL_DIRS=()
ALL_SCRIPTS=()
SELECTED_KEYS=()
SELECTED_DIRS=()
SELECTED_SCRIPTS=()
STARTED_AT=()
FINISHED_AT=()
EXIT_STATUS=()
DURATIONS=()
LOG_PATHS=()
ACTIVE_PIDS=()
ACTIVE_INDEXES=()

resolve_cowboy_cli() {
  if [[ -n "${COWBOY:-}" ]]; then
    if command -v "$COWBOY" >/dev/null 2>&1; then
      command -v "$COWBOY"
      return 0
    fi
    echo "COWBOY is set but not executable or on PATH: $COWBOY" >&2
    exit 1
  fi

  if [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then
    echo "$NODE_ROOT/target/release/cowboy"
    return 0
  fi

  if command -v cowboy >/dev/null 2>&1; then
    command -v cowboy
    return 0
  fi

  echo "cowboy CLI not found. Build it with:" >&2
  echo "  cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: examples/test_examples_parallel.sh [example selectors...]

Runs the active core/gallery demo set concurrently against one shared RPC endpoint.
Pass selectors to run specific demos, or set DEMO_PARALLEL_SCOPE=all to run every
discovered examples/core/*/demo.sh and examples/gallery/*/demo.sh.

Environment:
  RPC_URL or COWBOY_RPC_URL      Shared validator RPC URL.
  DEMO_PARALLEL_JOBS            Max concurrent demos. Default: 4. Use "all" for no cap.
  DEMO_PARALLEL_ARTIFACT_DIR    Log directory. Default: artifacts/examples-parallel.
  DEMO_PARALLEL_SCOPE           Default selector scope: "default" runs the active
                                core/gallery demo set; "all" runs every discovered demo.sh.

Examples:
  RPC_URL=http://localhost:4000 examples/test_examples_parallel.sh
  RPC_URL=http://localhost:4000 DEMO_PARALLEL_SCOPE=all examples/test_examples_parallel.sh
  RPC_URL=http://localhost:4000 DEMO_PARALLEL_JOBS=all examples/test_examples_parallel.sh core/00-quickstart-hello gallery/blackjack
EOF
}

fail_usage() {
  echo "$*" >&2
  echo >&2
  usage >&2
  exit 2
}

normalize_selector() {
  local value="$1"
  value="${value%/}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  value="${value// /-}"
  value="${value#examples/}"
  echo "$value"
}

discover_demos() {
  local script key dir
  while IFS= read -r script; do
    dir="$(cd "$(dirname "$script")" && pwd)"
    key="${script#$EXAMPLES_DIR/}"
    key="${key%/demo.sh}"
    ALL_KEYS+=("$key")
    ALL_DIRS+=("$dir")
    ALL_SCRIPTS+=("$script")
  done < <(find "$EXAMPLES_DIR" -mindepth 3 -maxdepth 3 -name demo.sh -print | sort)
}

selector_matches() {
  local key="$1"
  local selector="$2"
  local track="${key%%/*}"
  local base="${key#*/}"
  local short="${base%%-*}"
  local slug="${base#*-}"

  [[ "$selector" == "$key" || "$selector" == "$base" || "$selector" == "$short" || "$selector" == "$slug" || "$selector" == "$track/$short" || "$selector" == "$track/$slug" ]]
}

should_select() {
  local key="$1"
  local selector

  if [[ "${#SELECTORS[@]}" -eq 0 ]]; then
    if [[ "$DEMO_PARALLEL_SCOPE" == "all" ]]; then
      return 0
    fi
    demo_in_default_scope "$key"
    return
  fi

  for selector in "${SELECTORS[@]}"; do
    selector="$(normalize_selector "$selector")"
    if selector_matches "$key" "$selector"; then
      return 0
    fi
  done

  return 1
}

demo_in_default_scope() {
  local key="$1"
  local default_key

  for default_key in "${DEFAULT_DEMO_KEYS[@]}"; do
    if [[ "$key" == "$default_key" ]]; then
      return 0
    fi
  done

  return 1
}

select_demos() {
  local i
  for ((i = 0; i < ${#ALL_KEYS[@]}; i++)); do
    if should_select "${ALL_KEYS[$i]}"; then
      SELECTED_KEYS+=("${ALL_KEYS[$i]}")
      SELECTED_DIRS+=("${ALL_DIRS[$i]}")
      SELECTED_SCRIPTS+=("${ALL_SCRIPTS[$i]}")
    fi
  done

  if [[ "${#SELECTED_KEYS[@]}" -eq 0 ]]; then
    fail_usage "No examples matched the requested selectors: ${SELECTORS[*]}"
  fi
}

check_unmatched_selectors() {
  local selector normalized key matched

  if [[ "${#SELECTORS[@]}" -eq 0 ]]; then
    return 0
  fi

  for selector in "${SELECTORS[@]}"; do
    normalized="$(normalize_selector "$selector")"
    matched=0
    for key in "${ALL_KEYS[@]}"; do
      if selector_matches "$key" "$normalized"; then
        matched=1
        break
      fi
    done
    if [[ "$matched" -ne 1 ]]; then
      fail_usage "Unknown example selector: $selector"
    fi
  done
}

resolve_jobs() {
  local selected_count="$1"
  local value="$DEFAULT_JOBS"

  if [[ "$value" == "all" ]]; then
    echo "$selected_count"
    return 0
  fi

  if [[ ! "$value" =~ ^[0-9]+$ || "$value" -lt 1 ]]; then
    fail_usage "DEMO_PARALLEL_JOBS must be a positive integer or 'all' (got: $value)"
  fi

  if [[ "$value" -gt "$selected_count" ]]; then
    echo "$selected_count"
  else
    echo "$value"
  fi
}

check_validator_rpc() {
  local health first_height next_height attempts delay i detailed
  health="$(curl -fsS --max-time 5 "$RPC_URL/health" 2>/dev/null || true)"

  if [[ -z "$health" ]]; then
    echo "Expected a Cowboy validator at $RPC_URL, but /health did not return a response." >&2
    echo "Start a validator or set RPC_URL/COWBOY_RPC_URL to the shared validator RPC." >&2
    exit 1
  fi

  if printf "%s" "$health" | grep -qiE '<!DOCTYPE html|<html'; then
    echo "Expected a Cowboy validator at $RPC_URL, but /health returned HTML." >&2
    echo "Another local service may be using that port." >&2
    exit 1
  fi

  if [[ "${DEMO_PARALLEL_SKIP_HEIGHT_CHECK:-0}" == "1" ]]; then
    return 0
  fi

  first_height="$(curl -fsS --max-time 5 "$RPC_URL/height" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("height", ""))' 2>/dev/null || true)"
  if [[ ! "$first_height" =~ ^[0-9]+$ ]]; then
    echo "Expected a Cowboy validator at $RPC_URL, but /height did not return a numeric height." >&2
    exit 1
  fi

  attempts="${DEMO_PARALLEL_HEIGHT_CHECK_ATTEMPTS:-10}"
  delay="${DEMO_PARALLEL_HEIGHT_CHECK_DELAY:-2}"
  for ((i = 1; i <= attempts; i++)); do
    sleep "$delay"
    next_height="$(curl -fsS --max-time 5 "$RPC_URL/height" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("height", ""))' 2>/dev/null || true)"
    if [[ "$next_height" =~ ^[0-9]+$ && "$next_height" -gt "$first_height" ]]; then
      return 0
    fi
  done

  detailed="$(curl -fsS --max-time 5 "$RPC_URL/health/detailed" 2>/dev/null || true)"
  echo "Cowboy validator at $RPC_URL is reachable, but block height did not advance from $first_height." >&2
  if [[ -n "$detailed" ]]; then
    echo "Health detail: $detailed" >&2
  fi
  echo "Restart or repair the validator before running examples, or set DEMO_PARALLEL_SKIP_HEIGHT_CHECK=1 to bypass this preflight." >&2
  exit 1
}

pid_is_running() {
  local pid="$1"
  local running
  while IFS= read -r running; do
    if [[ "$running" == "$pid" ]]; then
      return 0
    fi
  done < <(jobs -pr)
  return 1
}

start_demo() {
  local index="$1"
  local key="${SELECTED_KEYS[$index]}"
  local dir="${SELECTED_DIRS[$index]}"
  local script="${SELECTED_SCRIPTS[$index]}"
  local run_dir="$RUN_ROOT/$key"
  local log_key="${key//\//__}"
  local log_path="$ARTIFACT_DIR/$log_key.log"
  local salt="p-$RUN_TOKEN-$index"

  mkdir -p "$run_dir/tmp" "$run_dir/work" "$ARTIFACT_DIR"
  : >"$log_path"

  LOG_PATHS[$index]="$log_path"
  STARTED_AT[$index]="$(date +%s)"

  (
    set -euo pipefail
    cd "$dir"

    export RPC_URL="$RPC_URL"
    export COWBOY_RPC_URL="$RPC_URL"
    export USE_SHARED_DEMO_KEY=0
    export DEMO_NATIVE_FUND_CBY="${DEMO_NATIVE_FUND_CBY:-100000}"
    export DEMO_NATIVE_MIN_CBY="${DEMO_NATIVE_MIN_CBY:-4000}"
    export DEMO_NATIVE_FUNDING_POLL_ATTEMPTS="${DEMO_NATIVE_FUNDING_POLL_ATTEMPTS:-20}"
    export DEMO_NATIVE_FUNDING_SUBMIT_ATTEMPTS="${DEMO_NATIVE_FUNDING_SUBMIT_ATTEMPTS:-4}"
    export DEMO_STOP_BACKGROUND="${DEMO_STOP_BACKGROUND:-1}"
    export DEMO_ISOLATED_RUN_DIR="$run_dir"
    export TMP_DIR="$run_dir/tmp"
    export WORK_DIR="$run_dir/work"
    export ADDRESS_FILE="$run_dir/.last_actor"
    export STATE_FILE="$run_dir/deploy-state.env"
    export SALT="$salt"
    export SALT_PREFIX="$salt"

    {
      echo "example=$key"
      echo "rpc_url=$RPC_URL"
      echo "run_dir=$run_dir"
      echo "started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      echo
    }

    bash "$script"
  ) >"$log_path" 2>&1 &

  ACTIVE_PIDS+=("$!")
  ACTIVE_INDEXES+=("$index")
  echo "Started $key (pid $!, log $log_path)"
}

record_finished() {
  local pid="$1"
  local index="$2"
  local status

  if wait "$pid"; then
    status=0
  else
    status=$?
  fi

  FINISHED_AT[$index]="$(date +%s)"
  EXIT_STATUS[$index]="$status"
  DURATIONS[$index]="$((FINISHED_AT[$index] - STARTED_AT[$index]))"

  if [[ "$status" -eq 0 ]]; then
    echo "Finished ${SELECTED_KEYS[$index]}: pass (${DURATIONS[$index]}s)"
  else
    echo "Finished ${SELECTED_KEYS[$index]}: fail status=$status (${DURATIONS[$index]}s)"
  fi
}

reap_finished() {
  local new_pids=()
  local new_indexes=()
  local i pid index

  for ((i = 0; i < ${#ACTIVE_PIDS[@]}; i++)); do
    pid="${ACTIVE_PIDS[$i]}"
    index="${ACTIVE_INDEXES[$i]}"
    if pid_is_running "$pid"; then
      new_pids+=("$pid")
      new_indexes+=("$index")
    else
      record_finished "$pid" "$index"
    fi
  done

  ACTIVE_PIDS=()
  ACTIVE_INDEXES=()
  if [[ "${#new_pids[@]}" -gt 0 ]]; then
    ACTIVE_PIDS=("${new_pids[@]}")
    ACTIVE_INDEXES=("${new_indexes[@]}")
  fi
}

run_selected_demos() {
  local max_jobs="$1"
  local next=0
  local total="${#SELECTED_KEYS[@]}"

  while [[ "$next" -lt "$total" || "${#ACTIVE_PIDS[@]}" -gt 0 ]]; do
    while [[ "$next" -lt "$total" && "${#ACTIVE_PIDS[@]}" -lt "$max_jobs" ]]; do
      start_demo "$next"
      next="$((next + 1))"
    done

    reap_finished
    if [[ "${#ACTIVE_PIDS[@]}" -gt 0 ]]; then
      sleep 1
    fi
  done
}

print_summary() {
  local i status label failed=0

  echo
  echo "============================================"
  echo "  Parallel Demo Summary"
  echo "============================================"
  printf "%-32s %-12s %8s  %s\n" "Example" "Status" "Seconds" "Log"
  printf "%-32s %-12s %8s  %s\n" "-------" "------" "-------" "---"

  for ((i = 0; i < ${#SELECTED_KEYS[@]}; i++)); do
    status="${EXIT_STATUS[$i]:-999}"
    if [[ "$status" -eq 0 ]]; then
      label="pass"
    else
      label="fail($status)"
      failed=1
    fi
    printf "%-32s %-12s %8s  %s\n" \
      "${SELECTED_KEYS[$i]}" \
      "$label" \
      "${DURATIONS[$i]:-0}" \
      "${LOG_PATHS[$i]:-$ARTIFACT_DIR/${SELECTED_KEYS[$i]}.log}"
  done

  echo
  echo "Artifacts: $ARTIFACT_DIR"

  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi
  return 0
}

main() {
  local jobs

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  if [[ "$DEMO_PARALLEL_SCOPE" != "default" && "$DEMO_PARALLEL_SCOPE" != "all" ]]; then
    fail_usage "DEMO_PARALLEL_SCOPE must be 'default' or 'all' (got: $DEMO_PARALLEL_SCOPE)"
  fi

  discover_demos
  check_unmatched_selectors
  select_demos
  jobs="$(resolve_jobs "${#SELECTED_KEYS[@]}")"
  COWBOY="$(resolve_cowboy_cli)"
  export COWBOY

  mkdir -p "$RUN_ROOT"

  echo "RPC:       $RPC_URL"
  echo "CLI:       $COWBOY"
  echo "Examples:  ${#SELECTED_KEYS[@]}"
  echo "Jobs:      $jobs"
  if [[ "${#SELECTORS[@]}" -eq 0 ]]; then
    echo "Scope:     $DEMO_PARALLEL_SCOPE"
  else
    echo "Scope:     selectors"
  fi
  echo "Artifacts: $ARTIFACT_DIR"
  echo

  check_validator_rpc
  run_selected_demos "$jobs"
  print_summary
}

main "$@"
