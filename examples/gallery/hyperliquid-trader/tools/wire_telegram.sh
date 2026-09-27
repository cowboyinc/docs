#!/usr/bin/env bash
# Wire the ALREADY-DEPLOYED notifier to Telegram, without redeploying anything.
#
# Set TG_CHAT (or ~/.telegram_chat_id) to the operator chat that is allowed to
# issue commands. The notifier is webhook-only and never calls getUpdates.
set -uo pipefail
: "${TG_WEBHOOK_SECRET:?set TG_WEBHOOK_SECRET before wiring Telegram; use the same secret_token when registering the webhook}"
S="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME="${ENV_NAME:-cal-river}"
export AWS_PROFILE="${AWS_PROFILE:-cowboy-river}"
RPC="${RPC_URL:-}"
if [ -z "$RPC" ]; then
  IP="$(aws ec2 describe-instances --region us-east-1 \
    --filters "Name=tag:Name,Values=river-$ENV_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null | head -1)"
  RPC="http://$IP:4000"
fi
GATEWAY_HOST_SUFFIX="${GATEWAY_HOST_SUFFIX:-}"
case "$RPC" in
  *canyon.cowboylabs.net*) GATEWAY_HOST_SUFFIX="${GATEWAY_HOST_SUFFIX:-canyon.cowboylabs.net}" ;;
esac
TOK="${TG_TOKEN:-$(cat ~/.telegram_bot_token 2>/dev/null)}"
[ -n "$TOK" ] || { echo "no bot token (~/.telegram_bot_token)"; exit 1; }

CHAT="${TG_CHAT:-$(cat ~/.telegram_chat_id 2>/dev/null)}"
[ -n "$CHAT" ] || { echo "no chat_id (set TG_CHAT or ~/.telegram_chat_id)"; exit 1; }

NA=$(cat "$S/notify.txt")
echo "  notifier=$NA  rpc=$RPC"
CLI="${CLI:-$(command -v cowboy)}"
[ -n "$CLI" ] && [ -x "$CLI" ] || { echo "set CLI to a version-matched cowboy binary"; exit 1; }
curl -fsS "$RPC/actor/$NA" | grep -q 'TELEGRAM_BOT_TOKEN' \
  || { echo "notifier lacks the TELEGRAM_BOT_TOKEN secrets.read entitlement; redeploy it"; exit 1; }
WEBHOOK_PAYLOAD="$(TG_WEBHOOK_SECRET="$TG_WEBHOOK_SECRET" python3 -c 'import hashlib,json,os; print(json.dumps({"secret_hash": hashlib.sha256(os.environ["TG_WEBHOOK_SECRET"].encode()).hexdigest()}))')"
COWBOY_RPC_URL="$RPC" "$CLI" \
  actor execute --actor "$NA" --handler set_webhook_secret \
  --payload-text "$WEBHOOK_PAYLOAD" \
  --private-key "$S/deploykey" --cycles-limit 20000000 --cells-limit 20000000 \
  >/dev/null 2>&1 || { echo "webhook secret setup failed"; exit 1; }
COWBOY_RPC_URL="$RPC" "$CLI" \
  actor execute --actor "$NA" --handler set_telegram \
  --payload-text "{\"chat_id\":\"$CHAT\"}" \
  --private-key "$S/deploykey" --cycles-limit 20000000 --cells-limit 20000000 2>&1 \
  | grep -E "Status|Error" | head -2
[ -n "$GATEWAY_HOST_SUFFIX" ] || { echo "set GATEWAY_HOST_SUFFIX for webhook delivery"; exit 1; }
python3 "$S/lower_notify_routes.py" >/dev/null || { echo "route lowering failed"; exit 1; }
COWBOY_RPC_URL="$RPC" "$CLI" actor execute --actor "$NA" --handler set_routes \
  --payload "$(xxd -p "$(dirname "$S")/notify_routes.cbor" | tr -d '\n')" \
  --private-key "$S/deploykey" --cycles-limit 20000000 --cells-limit 20000000 \
  >/dev/null 2>&1 || { echo "route install failed"; exit 1; }
HOST="$(printf '%s' "$NA" | tr 'A-F' 'a-f').${GATEWAY_HOST_SUFFIX}"
for _ in $(seq 1 20); do
  CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/telegram/health" || true)"
  [ "$CODE" = "200" ] && break
  sleep 3
done
[ "$CODE" = "200" ] || { echo "gateway health failed: ${CODE:-000}"; exit 1; }
WEBHOOK_URL="https://$HOST/telegram/webhook"
RESULT="$(curl -fsS "https://api.telegram.org/bot${TOK}/setWebhook" \
  -d "url=$WEBHOOK_URL" -d "secret_token=$TG_WEBHOOK_SECRET")" \
  || { echo "Telegram setWebhook failed"; exit 1; }
printf '%s' "$RESULT" | python3 -c 'import json,sys; assert json.load(sys.stdin).get("ok") is True' \
  || { echo "Telegram rejected setWebhook"; exit 1; }
echo "  webhook registered: $WEBHOOK_URL"
curl -s -m 15 -X POST "https://api.telegram.org/bot${TOK}/sendMessage" \
  -d "chat_id=${CHAT}" -d "text=HL trader notifier wired to $ENV_NAME ✅" >/dev/null && echo "  test message sent"
