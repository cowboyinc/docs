CANYON — current deployment

The September 2026 Canyon reset removed the previously recorded actors. Deploy
a fresh fleet before using the watcher or UI; do not reuse the old addresses.

RPC: https://rpc.canyon.cowboylabs.net   (requires Tailscale)
gateway: https://0x<actor>.canyon.cowboylabs.net (public; no QuickTunnel)
faucet: POST /faucet {"address":"..."} -> 5000 CBY
deploy: bundled trader uses 40M cycles/cells; notifier and UI fit the 10M
default. The 60M lane is rejected; salts must be hex.

Verified 2026-09-09 after the reset:
- notifier GET `/telegram/health` and authenticated POST `/telegram/webhook`
  both return 200 through the native gateway;
- the UI host returns 200 for `/ui/`, its static assets, and `/ui/api/health`;
- `TELEGRAM_BOT_TOKEN` is a CBSS secret entitled and policy-allowlisted to the
  notifier; actor storage contains only `chat_id` and the webhook-secret hash.

Recovery 2026-09-10:
- the chain is producing blocks again;
- notifier `0xa62E085B363A2e93C6bD86Ca8a9cDbb75452C3e3` was fenced with
  successful transaction `fb00857d23729519ea870676f0645110f5ea643c00ffe0bae38a28395c2fccc2`;
- do not register Telegram to that actor: it contains the retired polling code
  and remains quarantined;
- deploy a fresh webhook-only notifier from the current branch.

watcher: tools/canyon_watch.py <rpc> <addr> <out.jsonl> [interval]
