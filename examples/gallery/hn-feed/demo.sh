#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXAMPLES_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$EXAMPLES_ROOT/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
NODE_ROOT="$WORKSPACE_ROOT/node"
source "$EXAMPLES_ROOT/_demo_output.sh"
MESA_RPC_URL="${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}"
RPC_URL="${RPC_URL:-$MESA_RPC_URL}"
export COWBOY_RPC_URL="${COWBOY_RPC_URL:-$RPC_URL}"

STREAM_TARGET="stream"
SUBSCRIBER_TARGET="subscriber"
SALT_PREFIX="${SALT_PREFIX:-hn-feed-$(date +%s)}"
CYCLES="${CYCLES:-4000000}"
CELLS="${CELLS:-4000000}"
FUND_CBY="${FUND_CBY:-100000}"
FAUCET_ROUNDS="${FAUCET_ROUNDS:-5}"
FAUCET_KEY_HEX="ffffffffffffffff000000000000000000000000000000000000000000000000"

find_cowboy() {
  if [[ -n "${COWBOY_BIN:-}" && -x "$COWBOY_BIN" ]]; then
    echo "$COWBOY_BIN"
    return
  fi
  if [[ -n "${COWBOY:-}" ]] && command -v "$COWBOY" >/dev/null 2>&1; then
    echo "$COWBOY"
    return
  fi
  if command -v cowboy >/dev/null 2>&1; then
    echo "cowboy"
    return
  fi
  if [[ -x "$NODE_ROOT/target/release/cowboy" ]]; then
    echo "$NODE_ROOT/target/release/cowboy"
    return
  fi
  echo "cowboy CLI not found. Build it with:"
  echo "  cargo build -p cowboy-cli --manifest-path $NODE_ROOT/Cargo.toml --release"
  exit 1
}

COWBOY_BIN="$(find_cowboy)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

OWNER_KEY_FILE="$TMP_DIR/owner.key"
FAUCET_KEY_FILE="$TMP_DIR/faucet.key"

show_manual() {
  demo_print_command 1 "$*"
}

step_header() {
  local number="$1"
  local title="$2"
  echo
  echo "── Step $number: $title ──"
  echo ""
}

salt_hex() {
  local raw="$1"
  printf "%s" "$raw" | xxd -p -c 9999 | cut -c1-64
}

owner_address() {
  "$COWBOY_BIN" wallet address --key "$OWNER_KEY_FILE" 2>/dev/null | tail -1
}

faucet_if_available() {
  local addr="$1"
  curl --max-time 5 --connect-timeout 2 -fsS -X POST "$RPC_URL/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$addr\"}" >/dev/null 2>&1 || true
}

fund_with_faucet_key() {
  local addr="$1"
  local amount="${2:-$FUND_CBY}"
  "$COWBOY_BIN" transfer --rpc-url "$RPC_URL" --private-key "$FAUCET_KEY_FILE" --to "$addr" --amount "$amount" >/dev/null 2>&1
}

fund_heavily() {
  local addr="$1"
  local rounds="${2:-$FAUCET_ROUNDS}"
  local i
  for ((i = 1; i <= rounds; i++)); do
    faucet_if_available "$addr"
  done
}

fund_account() {
  local addr="$1"
  if fund_with_faucet_key "$addr" "$FUND_CBY"; then
    return 0
  fi
  fund_heavily "$addr"
}

poll_account_ready() {
  local addr="$1"
  local attempts="${2:-20}"
  local delay="${3:-2}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 2 "$RPC_URL/account/$addr" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  echo "Account $addr did not become queryable after faucet funding."
  exit 1
}

poll_actor_ready() {
  local addr="$1"
  local attempts="${2:-20}"
  local delay="${3:-2}"
  demo_poll_actor_ready "$COWBOY_BIN" "$addr" "$attempts" "$delay" || exit 1
}

run_exec() {
  local actor="$1"
  local handler="$2"
  local payload="$3"
  local cycles="${4:-$CYCLES}"
  local cells="${5:-$CELLS}"
  execute_actor "$actor" "$handler" "$payload" "$cycles" "$cells" "print"
}

capture_exec() {
  local actor="$1"
  local handler="$2"
  local payload="$3"
  local cycles="${4:-$CYCLES}"
  local cells="${5:-$CELLS}"
  execute_actor "$actor" "$handler" "$payload" "$cycles" "$cells" "capture"
}

# The CLI runs `init` atomically inside the deploy frame with sender = deployer,
# so configuration belongs in --init-payload. Calling init again afterwards would
# be denied: the SDK sets __INITIALIZED__ once init returns, after which init
# follows deny-by-default like every other undecorated handler.
deploy_actor() {
  local target="$1"
  local salt_raw="$2"
  local init_payload="${3:-}"
  [[ -n "$init_payload" ]] || init_payload='{}'
  local output
  local addr
  local salt
  salt="$(salt_hex "$salt_raw")"
  # Targets are project-relative: the CLI discovers them from the nearest
  # cowboy.toml, so the deploy runs from the example directory.
  cd "$SCRIPT_DIR"
  demo_retry_command 2 3 1 \
    "$COWBOY_BIN actor deploy $target --salt $salt --private-key $OWNER_KEY_FILE --init-payload $init_payload --cycles-limit 10000000 --cells-limit 10000000" \
    "$COWBOY_BIN" actor deploy "$target" \
      --salt "$salt" \
      --private-key "$OWNER_KEY_FILE" \
      --init-payload "$init_payload" \
      --cycles-limit 10000000 \
      --cells-limit 10000000
  output="$DEMO_LAST_OUTPUT"
  addr="$(printf "%s\n" "$output" | awk '/Actor address:/ {print $NF}' | tail -1)"
  if [[ -z "$addr" ]]; then
    echo "Failed to extract actor address for target $target" >&2
    exit 1
  fi
  poll_actor_ready "$addr"
  sleep 2
  printf "%s\n" "$addr"
}

execute_actor() {
  local actor="$1"
  local handler="$2"
  local payload="$3"
  local cycles="${4:-$CYCLES}"
  local cells="${5:-$CELLS}"
  local mode="${6:-print}"
  local attempt output fd cmd_status
  # Every handler reached from here decodes CBOR, so send JSON via --payload-json.
  # The one raw-JSON handler, init, runs at deploy time via --init-payload.
  local -a payload_args
  local display_payload
  payload_args=(--payload-json "$payload")
  display_payload="--payload-json $payload"
  if [[ "$mode" == "capture" ]]; then
    fd=2
    if demo_capture_command "$fd" \
      "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $OWNER_KEY_FILE --cycles-limit $cycles --cells-limit $cells" \
      "$COWBOY_BIN" actor execute \
        --actor "$actor" \
        --handler "$handler" \
        "${payload_args[@]}" \
        --private-key "$OWNER_KEY_FILE" \
        --cycles-limit "$cycles" \
        --cells-limit "$cells"; then
      output="$DEMO_LAST_OUTPUT"
      if demo_output_has_success_receipt "$output"; then
        printf "%s" "$output"
        return 0
      fi
    fi
    return "$DEMO_LAST_STATUS"
  fi

  fd=1

  for attempt in $(seq 1 15); do
    if demo_capture_command "$fd" \
      "$COWBOY_BIN actor execute --actor $actor --handler $handler $display_payload --private-key $OWNER_KEY_FILE --cycles-limit $cycles --cells-limit $cells" \
      "$COWBOY_BIN" actor execute \
        --actor "$actor" \
        --handler "$handler" \
        "${payload_args[@]}" \
        --private-key "$OWNER_KEY_FILE" \
        --cycles-limit "$cycles" \
        --cells-limit "$cells"; then
      output="$DEMO_LAST_OUTPUT"
      if demo_output_has_success_receipt "$output"; then
        return 0
      fi
      cmd_status="$DEMO_LAST_STATUS"
    else
      cmd_status="$DEMO_LAST_STATUS"
      output="$DEMO_LAST_OUTPUT"
    fi
    if demo_output_should_retry "$output" && [[ "$attempt" -lt 15 ]]; then
      sleep 2
      continue
    fi
    if demo_output_has_success_receipt "$output"; then
      return 0
    fi
    if demo_output_has_execution_error "$output"; then
      return "$cmd_status"
    fi
    if [[ "$cmd_status" -eq 0 ]]; then
      return 0
    fi
    return "$cmd_status"
  done
  return 1
}

extract_json() {
  printf "%s" "$1" | python3 -c '
import json
import sys

text = sys.stdin.read()
candidates = []
for start, ch in enumerate(text):
    if ch != "{":
        continue
    depth = 0
    for end in range(start, len(text)):
        current = text[end]
        if current == "{":
            depth += 1
        elif current == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:end + 1]
                try:
                    json.loads(candidate)
                except Exception:
                    break
                candidates.append(candidate)
                break
print(candidates[-1] if candidates else "")
'
}

extract_event_json() {
  local topic="$1"
  local text="$2"
  printf "%s" "$text" | python3 -c '
import json
import sys

topic = sys.argv[1]
needle = f"- {topic}: "

for line in sys.stdin.read().splitlines():
    if needle not in line:
        continue
    candidate = line.split(needle, 1)[1].strip()
    try:
        json.loads(candidate)
    except Exception:
        continue
    print(candidate)
    break
' "$topic"
}

assert_demo_results() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

stream_stats = json.loads(sys.argv[1])
subscriber_stats = json.loads(sys.argv[2])
signals = json.loads(sys.argv[3])

errors = []
if int(stream_stats.get("articles_ingested", 0)) <= 0:
    errors.append("stream articles_ingested did not increase")
if int(stream_stats.get("subscriber_count", 0)) < 1:
    errors.append("stream subscriber_count is still 0")
if int(subscriber_stats.get("signals_generated", 0)) <= 0:
    errors.append("subscriber signals_generated did not increase")
if len(signals.get("signals", [])) == 0:
    errors.append("subscriber returned no recent signals")

if errors:
    raise SystemExit("; ".join(errors))
PY
}

json_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

obj = json.loads(sys.argv[1])
value = obj
for part in sys.argv[2].split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
print("" if value is None else value)
PY
}

wait_for_subscription() {
  local stream_addr="$1"
  local attempts="${2:-12}"
  local delay="${3:-2}"
  local i output json subscriber_count
  for ((i = 1; i <= attempts; i++)); do
    output="$(capture_exec "$stream_addr" "get_hn_stats" '{}' || true)"
    json="$(extract_json "$output")"
    if [[ -n "$json" ]]; then
      subscriber_count="$(json_field "$json" "subscriber_count")"
      if [[ "$subscriber_count" =~ ^[0-9]+$ ]] && (( subscriber_count >= 1 )); then
        return 0
      fi
    fi
    sleep "$delay"
  done
  echo "Subscriber registration did not reach the stream in time."
  exit 1
}

build_pull_batch_payload() {
  python3 - "$1" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
payload["type"] = "pull_batch"
print(json.dumps(payload, separators=(",", ":")))
PY
}

trigger_pull_fallback() {
  local stream_addr="$1"
  local subscriber_addr="$2"
  local output json payload
  output="$(capture_exec "$stream_addr" "get_since" '{"from_sequence":0}')"
  json="$(extract_event_json "hn_stream.since" "$output")"
  if [[ -z "$json" ]]; then
    echo "Failed to fetch stream batch for pull fallback."
    exit 1
  fi
  payload="$(build_pull_batch_payload "$json")"
  run_exec "$subscriber_addr" "handle_message" "$payload"
}

"$COWBOY_BIN" wallet create --output "$OWNER_KEY_FILE" >/dev/null
printf "%s\n" "$FAUCET_KEY_HEX" > "$FAUCET_KEY_FILE"
OWNER_ADDR="$(owner_address)"
fund_account "$OWNER_ADDR"
poll_account_ready "$OWNER_ADDR"

SAMPLE_INGEST_PAYLOAD='{"hits":[{"objectID":"40001","title":"OpenAI ships a smaller GPT model for local coding tools","url":"https://example.com/openai-gpt-local","points":182,"author":"alice","num_comments":97,"created_at":"2026-04-15T09:00:00Z"},{"objectID":"40002","title":"Nvidia backs new AI infrastructure startup","url":"https://example.com/nvidia-ai-startup","points":164,"author":"bob","num_comments":61,"created_at":"2026-04-15T09:05:00Z"},{"objectID":"40003","title":"Ask HN: favorite keyboard firmware?","url":"https://example.com/ask-hn-keyboard","points":53,"author":"carol","num_comments":19,"created_at":"2026-04-15T09:10:00Z"}]}'

echo "============================================"
echo "  Hacker News Feed Demo"
echo "============================================"
echo ""
echo "This walkthrough deploys an HN CIP-7 stream and a downstream subscriber,"
echo "creates a push subscription, seeds deterministic Algolia-shaped hits, and"
echo "then reads the published stream stats, recent events, and generated signals."
echo ""
echo "RPC: $RPC_URL"
echo ""

step_header "1" "Deploying and initializing stream and subscriber actors"
STREAM_ADDR="$(deploy_actor "$STREAM_TARGET" "$SALT_PREFIX-stream" '{"min_score":80}')"
SUBSCRIBER_ADDR="$(deploy_actor "$SUBSCRIBER_TARGET" "$SALT_PREFIX-subscriber")"
echo "HN stream:   $STREAM_ADDR"
echo "Subscriber:  $SUBSCRIBER_ADDR"

step_header "2" "Subscribing to the tech news stream"
run_exec "$SUBSCRIBER_ADDR" "subscribe_to_hn" "{\"stream_address\":\"$STREAM_ADDR\",\"mode\":\"PUSH_WITH_PULL_FALLBACK\",\"min_score\":120,\"category\":\"tech\"}"
wait_for_subscription "$STREAM_ADDR"

step_header "3" "Injecting deterministic HN hits"
run_exec "$STREAM_ADDR" "on_ingest_result" "$SAMPLE_INGEST_PAYLOAD"
trigger_pull_fallback "$STREAM_ADDR" "$SUBSCRIBER_ADDR"

step_header "4" "Reading stream stats"
STREAM_STATS_OUTPUT="$(capture_exec "$STREAM_ADDR" "get_hn_stats" '{}')"
STREAM_STATS_JSON="$(extract_event_json "hn_stream.stats" "$STREAM_STATS_OUTPUT")"
if [[ -z "$STREAM_STATS_JSON" ]]; then
  echo "Failed to parse stream stats JSON output."
  exit 1
fi
echo ""

step_header "5" "Reading recent stream events"
capture_exec "$STREAM_ADDR" "get_recent_events" '{"limit":10}' >/dev/null
echo ""

step_header "6" "Reading subscriber signals and stats"
SIGNALS_OUTPUT="$(capture_exec "$SUBSCRIBER_ADDR" "get_signals" '{"limit":10}')"
SIGNALS_JSON="$(extract_event_json "trading_subscriber.signals" "$SIGNALS_OUTPUT")"
SUBSCRIBER_STATS_OUTPUT="$(capture_exec "$SUBSCRIBER_ADDR" "get_stats" '{}')"
SUBSCRIBER_STATS_JSON="$(extract_event_json "trading_subscriber.stats" "$SUBSCRIBER_STATS_OUTPUT")"
if [[ -z "$SIGNALS_JSON" || -z "$SUBSCRIBER_STATS_JSON" ]]; then
  echo "Failed to parse subscriber JSON output."
  exit 1
fi

assert_demo_results "$STREAM_STATS_JSON" "$SUBSCRIBER_STATS_JSON" "$SIGNALS_JSON"

echo ""
echo "============================================"
echo "  Demo complete!"
echo "============================================"
echo ""
echo "  HN stream:  $STREAM_ADDR"
echo "  Subscriber: $SUBSCRIBER_ADDR"
echo "  Owner:      $OWNER_ADDR"
