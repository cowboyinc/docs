# AgentMail Store wrapper

A hire-gated, wallet-billed wrapper over [AgentMail](https://agentmail.to) that
gives an actor its own real email address: create inboxes, send, reply, and,
the new part, **receive**. The vendor key stays in CBSS as `AGENTMAIL_API_KEY`;
the runner injects it into every request.

Receiving is the capability an actor could not have before. It lets an actor act
on inbound replies and notifications, not just fire mail off.

> **Everything an actor sends or receives becomes public.** A request's fields
> and the fetched result are recorded in consensus state and returned through
> `get_result`, so message bodies, subjects, and addresses are visible on chain.
> Do **not** route one-time passcodes, password resets, or any confidential mail
> through this wrapper — treat every inbox as a public mailbox.

This example is outside the default gallery sweep because a live run requires
CBSS proxies, runners, an AgentMail key, and a Trading Post hire.

## Handlers

| Handler | AgentMail API | Purpose |
|---|---|---|
| `create_inbox` | `POST /v0/inboxes` | make an inbox; returns its address |
| `send` | `POST /v0/inboxes/{id}/messages/send` | send a message |
| `reply` | `POST /v0/inboxes/{id}/messages/{msg}/reply` | reply in a thread |
| `list_messages` | `GET /v0/inboxes/{id}/messages` | the receive path: poll for mail |
| `get_message` | `GET /v0/inboxes/{id}/messages/{msg}` | one message with its parsed text |
| `search` | `GET /v0/inboxes/{id}/messages/search` | full-text search the inbox |

Plus `init`, `register_hire`, `get_result` (the poll handler), `capability`,
`get_config`, `get_last_output`. Every call is hire-gated and asynchronous: the
handler validates input and returns an acceptance record, and the registered
hirer reads the result from `get_result`. Under the hood it is a single
`runner.http` resumed once as a continuation.

## Receiving

AgentMail can push inbound mail over webhooks or a WebSocket, but an on-chain
actor cannot hold an inbound endpoint or a socket, so the wrapper **receives by
polling `list_messages`**, which the AgentMail docs endorse as a supported
pattern. List rows carry metadata and a short `preview`, not the body; call
`get_message` for the parsed text. Poll with `after` (a timestamp) or a
`labels` filter to see only new mail.

```json
{"hire_id": 42, "username": "support"}
```

```json
{"hire_id": 42, "inbox_id": "support@agentmail.to", "to": "user@example.com",
 "subject": "Your order shipped", "text": "Tracking number 1Z999AA10123456784."}
```

```json
{"hire_id": 42, "inbox_id": "support@agentmail.to", "limit": 20, "labels": ["unread"]}
```

## Security

The key rides every call in the `Authorization` header. More than that, a
`${AGENTMAIL_API_KEY}` placed by a caller in a subject or body would have the
runner paste the live key into the **outbound email**, leaking it to the
recipient. So any caller input containing the `${NAME}` secret-template syntax
is refused before dispatch. Inbox ids and message ids are validated and, for
message ids, percent-encoded into the path, so a caller cannot repoint the URL.

Inboxes are bound to the hire that created them. When `create_inbox` succeeds
the actor records that the new inbox belongs to that hire, and `send`, `reply`,
`list_messages`, `get_message`, and `search` refuse an `inbox_id` that belongs to
a **different** hire — so one tenant cannot send from or read another tenant's
inbox. (An inbox this actor never created is unbound and allowed, so a hirer may
still use an address made directly at AgentMail.) Each hire is capped to a fixed
number of inboxes and of in-flight requests, so no single hire can exhaust the
actor's storage or request table. Mutating sends carry an `Idempotency-Key` so a
runner retry does not double-send.

## Response

A handler does **not** return the result inline. Because the Runner callback
lands after the buyer's transaction, each handler returns an **acceptance
record**, and the registered hirer reads the result from `get_result` with the
returned `request_id` — so the result reaches that buyer and is not overwritten
by an overlapping request (v2.0.0; a breaking change from the inline contract, so
a redeploy is required).

```json
{"ok": true, "request_id": 7, "hire_id": 42, "operation": "list_messages", "status": "pending", "poll_handler": "get_result"}
```

Read it as the same caller and hire — `get_result` returns `status` `pending`
until the callback resolves, then `ok`/`error` with the `output`, whose `data` is
the native AgentMail envelope, bounded to 38,000 bytes under the 40 KB
runner-result cap. `get_message` returns the parsed `text`/`html`, never raw MIME.

## Excluded

Webhooks and WebSocket push (no inbound endpoint on chain), raw-MIME download,
and attachment upload are not exposed. Custom domains require a paid AgentMail
plan; inboxes default to `@agentmail.to`.

## Entitlements

Allow `http.fetch` to `api.agentmail.to`; allow `secrets.read` for
`<deployer>:AGENTMAIL_API_KEY`, and `storage.kv`. The CBSS release policy must
also allow-list the deployed actor for the current secret version.

## Run

```bash
export RPC_URL=https://rpc.canyon.cowboylabs.net
export CBSS_INGEST_ENDPOINT=http://<cbssd>:<port>
export AGENTMAIL_API_KEY_FILE=/path/to/key.txt
./demo.sh
```

The demo runs the full store flow and ends in a gated `create_inbox`, a
self-addressed `send`, and a `list_messages` poll that receives it. Get a free,
no-card key at https://console.agentmail.to. Do not pass the key inline; use the
file input so it does not enter shell history.

## Test

Offline (no chain, no key). The contract suite loads the actor with the real SDK
decorators against the in-memory host, so point `COWBOY_SDK_PATH` at a
`node/pvm/Lib` checkout; the drift guard is standalone:

```bash
cd examples/gallery/agentmail-inbox
export COWBOY_SDK_PATH=/path/to/node/pvm/Lib
uv run pytest
cowboy actor check inbox
uv run python tools/agentmail_parity.py
uv run python tools/agentmail_parity.py --schema openapi.json
```

`test_agentmail.py` exercises the acceptance-record + `get_result` contract for
all six handlers: dispatch shape and secret custody, the receive (`list_messages`)
path as a GET, authenticated reads (wrong caller / wrong hire rejected),
secret-template refusal, unregistered and stolen hires, duplicate / late / forged
callbacks, oversized-reply bounding, the request-capacity and hire caps, and
deadline expiry. The parity test fetches AgentMail's OpenAPI and fails if an
endpoint the actor calls moves, changes method, or drops the Bearer auth.

Live, with a key file (creates one of the 3 free inboxes, self-sends, and polls
until the message is received):

```bash
AGENTMAIL_API_KEY_FILE=/secure/key.txt \
  uv run python tools/live_agentmail_comparison.py
```

On chain, against a deployed and hired Canyon actor:

```bash
uv run python tools/live_agentmail_canyon.py \
  --actor "$ACTOR" --hire-id "$HIRE_ID" --key-file /secure/key.txt
```
