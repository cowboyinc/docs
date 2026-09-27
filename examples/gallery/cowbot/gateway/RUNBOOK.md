# Cowbot gateway cutover runbook

End-to-end procedure to replace `tunnel/main.py` with the real Cowboy
Gateway on jefferson. Pre-release devnet — no migration concerns, no
back-compat constraints.

```
            BEFORE                                AFTER
                                            
    Slack ──HTTPS──▶ cloudflared ──▶  Slack ──HTTPS──▶ Tailscale Funnel
                       │                                    │
                       ▼                                    ▼
                  tunnel/main.py                   /opt/cowboy-gateway
                  (HMAC + sig verify,              (path resolution,
                   POST raw body to                 envelope encode,
                   actor.slack_event)               /submit_wait)
                       │                                    │
                       ▼                                    ▼
                  validator                          validator
                  (consensus)                        (consensus)
                       │                                    │
                       ▼                                    ▼
                  cowbot.slack_event(payload)        cowbot.slack_http(envelope)
                  (HMAC verify already done            (NO secret in-actor;
                   upstream — actor trusts              runner MAC-verifies via
                   tunnel)                             the first runner.http
                                                       verify={SLACK_SIGNING_SECRET},
                                                       CIP-24 §5.5)
```

The new pieces (gateway code + actor migration) have all landed already.
This runbook walks the operator through deploying them.

---

## 0. Prereqs

- Jefferson is running the validator devnet (`./env-bootstrap.sh devnet`
  is up; `curl http://127.0.0.1:4002/height` returns a height).
- Cowbot actor is deployed (`./deploy.sh` produced an actor address —
  call this `$COWBOT_ACTOR`, e.g. `0x3096e2…`).
- You have the cowbot deployer's private key file (`$COWBOT_PRIVATE_KEY`)
  and another funded wallet PEM that can transfer CBY to the gateway
  (`$FUNDING_PRIVATE_KEY` — typically the same key).
- `tailscale` is installed on jefferson.

---

## 1. Bootstrap the gateway

On jefferson, as root:

```bash
sudo COWBOY_ROOT=/path/to/cowboy \
     FUNDING_PRIVATE_KEY=/path/to/funded-wallet.pem \
     COWBOT_ACTOR=0x<cowbot-actor> \
     ./gateway/bootstrap-gateway.sh
```

What the script does, in order:

1. Builds `gateway` if not already at `gateway/target/release/gateway`.
2. Creates the `cowboy-gateway` system user + `/opt/cowboy-gateway` +
   `/var/lib/cowboy-gateway`.
3. Generates a fresh secp256k1 keypair at
   `/var/lib/cowboy-gateway/keypair.json` (`cowboy wallet create`).
4. Funds it with 100M CBY from `$FUNDING_PRIVATE_KEY` (`cowboy transfer`).
   Idempotent — re-runs only top up if balance < 50M CBY.
5. Drops `gateway.toml` + the systemd unit into place.
6. `systemctl enable --now cowboy-gateway.service`.
7. Prints the gateway address + next-step commands.

Verify:

```bash
systemctl status cowboy-gateway.service
journalctl -u cowboy-gateway.service -n 50
curl -i http://127.0.0.1:8080/healthz   # should return 200 from the gateway itself
```

---

## 2. Expose the gateway via Tailscale Funnel

On jefferson:

```bash
tailscale serve --bg https / http://127.0.0.1:8080
tailscale funnel 443 on
```

Note the public hostname:

```bash
tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//'
# → e.g. <host>.<tailnet>.ts.net
```

Smoke test path-based actor resolution (no Slack involvement, no gas):

```bash
curl -i "https://<jefferson-funnel-hostname>/0x<cowbot-actor-no-prefix>/health"
# Expected: HTTP 200, body "ok"
```

If this fails: tail `journalctl -u cowboy-gateway -f` and the validator
log. Common causes — wrong actor address in the URL, wrong RPC port in
`gateway.toml`, gateway keypair has zero balance.

---

## 3. Provision the Slack secrets as CBSS secrets

The actor holds NO Slack secret (CIP-24 §5.5). The signing secret and bot
token are CBSS secrets owned by the deployer EOA and ACL'd to the actor; the
runner threshold-decrypts each per job. `deploy.sh` provisions both if you
export `COWBOT_SLACK_BOT_TOKEN` / `COWBOT_SLACK_SIGNING_SECRET` before running
it. To set (or rotate) them by hand:

```bash
# paste the value, then Ctrl-D — --value-file - keeps it out of shell history
cowboy secrets --rpc-url http://127.0.0.1:4002 --private-key $COWBOT_PRIVATE_KEY \
    set SLACK_SIGNING_SECRET --value-file - --actor $COWBOT_ACTOR
cowboy secrets --rpc-url http://127.0.0.1:4002 --private-key $COWBOT_PRIVATE_KEY \
    set SLACK_BOT_TOKEN      --value-file - --actor $COWBOT_ACTOR
```

The runner uses `SLACK_SIGNING_SECRET` only to MAC-verify the inbound webhook
(verify-only; never substituted) and substitutes `${SLACK_BOT_TOKEN}` into the
outbound post. If you rotate either value in Slack, re-run the matching command.

---

## 4. Flip the Slack URL

Manual step in the Slack app admin console:

1. Open <https://api.slack.com/apps> → your cowbot app.
2. **Event Subscriptions** → toggle on (if it isn't already).
3. **Request URL** → paste:
   ```
   https://<jefferson-funnel-hostname>/0x<cowbot-actor-no-prefix>/slack/events
   ```
4. Slack fires a `url_verification` challenge. The actor's
   `slack_http` echoes the challenge back synchronously
   (round-trips through `/submit_wait` in ~1s on devnet). url_verification
   is unsigned and needs no secret — it never reaches the runner MAC.
5. Slack shows a green "Verified ✓" badge.

If verification fails:
- Check `journalctl -u cowboy-gateway -f` for the incoming request.
- Check the validator log for `slack_http` execution.
- Most common causes: wrong actor address in the Request URL, or the
  gateway can't reach the validator (`gateway.toml` RPC port). The signing
  secret is NOT involved here — url_verification carries no MAC.

---

## 5. Smoke test events

In Slack, mention the bot or DM it:

```
@cowbot what does CIP-15 say about route caching?
```

Watch:

```bash
# On jefferson
journalctl -u cowboy-gateway -f
tail -f /tmp/cowbot-validator.log

# Cowbot's request history
cowboy actor read \
    --address $COWBOT_ACTOR \
    --handler get_requests \
    --rpc-url http://127.0.0.1:4002
```

You should see:
- Gateway log: `POST /0x.../slack/events` → **502 today** for a normal message
  (the tx succeeds and the runner job runs; only the acknowledgement is wrong —
  COW-3895); 2xx/fallback once gateway PR #93 lands. (A `url_verification` ping
  is answered 200 inline.)
- Validator log: `slack_http` — it IS the `@runner.continuation`. Its pre-await
  segment (prefilters + url_verification) returns explicit envelopes inline for
  the early-return branches; on the normal path the initial segment returns no
  envelope. TODAY the gateway answers the caller 502 for that path (the tx
  succeeded and the runner job runs; COW-3895); after gateway PR #93 it answers
  with the route's fallback response or 2xx. Its first `runner.http` (the MAC
  gate) then submits as a first-level
  child of the same tx, and the answer lands via `slack_http__resume` in a later
  block. There is NO self-dispatch: a self-sent message would run the runner
  call under a deferred child the node can't fund a `submit_job` inside (D36,
  COW-3878).
- Bot replies in the thread within ~10–30s (LLM agent runtime).

---

## 6. Retire the tunnel

After ~24h of stable Slack traffic on the new path:

```bash
# On any machine running the old tunnel — stop it first.
systemctl stop cowbot-tunnel 2>/dev/null || true
pkill -f 'tunnel/main.py'    2>/dev/null || true

# The tunnel/ directory has already been removed from the repo — there is
# nothing left to `git rm`. Just confirm no host is still running the process.
```

The `tunnel/` directory has no remaining consumers once the Slack URL
is on the gateway — the actor's old tunnel-era `slack_event` payload
handler is gone too. Under the gateway, the route handler `slack_http`
IS the `@runner.continuation` (COW-3878): it MAC-gates and answers inline,
with no self-send/deferred hop.

---

## Operations notes

- **Gas burn rate.** Every Slack POST creates one `ExecuteActor` tx
  signed by the gateway keypair. At cowbot's volume (handful of
  questions per day) the 100M CBY initial funding lasts ~indefinitely
  on devnet. If you scale, raise `GATEWAY_FUND_AMOUNT` or run the
  bootstrap script periodically (it tops up automatically when balance
  < 50M).
- **Gateway logs.** `journalctl -u cowboy-gateway --since '5 min ago'`.
  Set `RUST_LOG=trace,gateway=trace` in the systemd unit if you need
  request/response payloads.
- **Receipt timeouts.** If Slack starts retrying because the gateway's
  `/submit_wait` timed out, look at devnet block latency. The gateway
  default timeout is 3s; the validator-side default is also 3s. Both
  configurable but should never need to be touched on a healthy devnet.
- **Multi-instance.** The gateway tracks the keypair's nonce locally
  via an atomic + lazy chain seed. Two gateway pods sharing one keypair
  WILL collide on nonces. If you ever need HA, give each pod its own
  keypair.
