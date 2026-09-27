# Inference management implementation

This directory contains an isolated handler and browser page. It is not a
deployable inference listing yet. OpenRouter is a client of Cowboy's public
OpenAI-compatible endpoints.

`actor.py` is the owner-only PVM configuration scaffold. `init` establishes the
SDK owner; `configure_slot` accepts the exact CBOR map fields `slot`, `hire_id`,
`declaration_id`, `version`, `artifact_id`, `volume_owner`, `volume_name`,
`public_base_url`, and `active_key_cap`. It stores public metadata under
`inference/slots`, with at most 128 slots and 1–100 active keys per slot. The
volume owner is 40 lowercase hex characters without `0x`. No caller-supplied
hirer identity or credential is stored. The runtime adapter normalizes the
three pin fields consistently with its retained Hire reader. Manifest KV access
supports this configuration; it does not grant offchain secret entitlements.

`capability-schema.json` describes the proposed CIP-33 listing. `route-intent.json`
is explicitly non-wire design input for the shared SDK builder. It includes
the distinct `runner_route` target and pairs session open/close in one policy.
`prepare_routes.py` lowers it through `cowboy_sdk.runner_routes`; there is no
route installer or chain submission. Actual bundle/runtime pins, budgets,
attachments and purpose-scoped CBSS references remain deployment inputs.

`offchain/manage.py` implements `Management.handle(Request) -> response`, with
`status`, `headers`, and a bytes `body`. The page lives in `offchain/page/`.
`offchain/management_runtime.py::build_management` composes the real signature
verifier, CAS nonce store, CBFS CLI volumes and optional CIP-9 grant adapter.
The executor supplies trusted configuration; HTTP callers cannot supply these
adapters or authenticate by providing an account address.

`VolumeBinding.extra_args` is a tuple of trusted CLI arguments, such as
`("--trusted-checkpoint", "/path/to/checkpoint.json")`. The binding passes it
to both volume and grant adapters without shell interpolation or monkeypatching.
Only the executor configures it; request bodies cannot override CLI arguments,
state directories or binaries. Both adapters use the same bounded subprocess
pump with concurrent stdin/stdout/stderr handling and a wall-clock deadline.

Each writer job needs a private CLI state directory and pending-commit journal.
`run_cbfs` defaults `CBFS_PENDING_DIFF_DIR` to `<state_dir>/pending-diffs`; an
explicit environment override takes precedence and must also be private to the
job. Never inherit a shared host journal path across concurrent jobs. Separate
jobs still use the same authoritative CBFS volume and manifest CAS. Within a
long-running session, serialize CLI mutations sharing one state directory, or
provide separate writer state. Isolation does not replace pending-commit
reconciliation before staging new writes after a failed or interrupted commit.

Provisioning must confirm authenticated volume read/write readiness before
exposing management endpoints. A newly created volume may not yet be visible
in relay caches; a fixed propagation sleep is not a readiness check.

Size each relay's `max_connections_per_source` for the connections it sees
from one source address. Jobs sharing a runner host or NAT share this allowance;
separate processes and CLI state directories do not give them separate network
identities. Include concurrent jobs, connections per job, and other clients such
as relay peering, and keep the configured share below the relay's global limit.
Bound job concurrency to that capacity. Verify readiness from a second process
and exercise the expected concurrent load. If readers report `manifest node
placement unavailable`, inspect relay-side connection refusals as well as
placement publication: the SDK message alone does not distinguish missing
metadata from a transport failure. Raising a connection limit does not establish
that publication succeeded or replace the read/write readiness check.

## Runtime contracts

- **Request:** the executor supplies the actual method, exact ASCII origin-form
  target including query, exact body, actor, chain ID, network, handler and raw
  account proof from the admitted context. It must check the native declaration,
  route match and context/body commitments before invoking this module. Management
  requires `hire=None`; an inactive hire must not prevent its hirer from managing it.
- **Account verifier:** independently verifies the wallet signature against that
  exact Request and the pinned domain. Returns `Account(address20, expiry, nonce, digest32)`.
  It must never trust a gateway's claimed account. This implementation requires
  `now <= expiry <= now + 3600 seconds`, nonce length 1–256 bytes, and account
  length 20 bytes. `account_proof.py`
  pins domain + canonical CBOR, Keccak and low-S secp256k1 with raw recid 0..3.
  `tests/fixtures/account_proof.json` is shared with the browser tests. Runtime
  composition pins handler, actor, chain and network independently of request
  claims and normalizes the verifier result to the management Account type.
- **Nonce consumer:** atomically consumes `(chain_id, network, actor, account,
  nonce)` across all management jobs/runners. `NonceStore` uses SHA-256 of the
  canonical CBOR identity, records the verified digest, and retains the claim
  through expiry + 300 seconds inclusive. Every replay returns false, even if
  identical, so a retry cannot create a second key or return a stored secret.
  Each consumer call generates a private random attempt ID. Schema-2 records
  bind this ID as well as the digest and expiry, allowing an internal CAS retry
  to recognize that this same call's commit landed despite a lost acknowledgement.
  A new call generates a new ID and remains a replay even with the same digest;
  the ID is never supplied by the HTTP caller. Existing schema-1 records remain
  replay barriers and cannot authorize recovery. This does not resume a handler
  after a process restart or replace native execution receipts.
  Every authenticated route needs write authority to this separate owner-only
  nonce volume. It must be the same volume across runners and must never be a
  hirer-readable volume. Expired claims can be pruned after their retention
  deadline; pruning is an operations dependency, not performed on reads.
  Durability and cross-process atomicity come from the backend manifest CAS.
- **Executor replay handoff:** native admission owns retries and returns the
  original invocation identity/receipt without rerunning management. Once that
  executor is available, pass `executor_claim_verifier` to `build_management`.
  It must authenticate the admitted context, match the independently recovered
  caller, canonical claim key and request digest, and verify this is the original
  authorized execution. This explicit construction switch removes management's
  nonce consumption. Every data request then requires a valid `executor_claim`;
  missing or invalid claims fail closed, with no local-store fallback. In the
  default management-owned mode, an attached executor claim is rejected. Neither
  request-field presence nor an HTTP `verified` flag is authority. The actual
  executor claim format/verifier remains a native integration dependency.
- **Hire lookup:** reads authoritative retained Hire state. Returns the hirer,
  `(declaration, version, artifact)` pins and a normalized lowercase status
  (`active`, `paused`, `revoked`, `lapsed`, etc.). Slot configuration comes from
  trusted actor state, not the request. Unknown slots, wrong hirers and mismatched
  pins get the same denial before any hire-volume access. Creation rechecks the
  retained hire on each CAS attempt. Only `active` permits new keys.
- **Volume:** `commit` is an atomic, durable manifest CAS. Mutation callbacks
  re-read and reapply against a coherent root on conflicts. `read`, `list` and
  `root` must support coherent committed views, including authoritative revocation
  refresh. `stable_read` retries a read if its root changes. LocalVolume is
  test-only and does not supply multi-process atomicity or crash durability.
- **Key issuer:** `(slot, trimmed_name, now) -> (cwir1_token, KeyRecord)`. Its CBSS
  issuing key is released only to the create-key job. A token is returned only
  after its hash record commits. CAS retries reuse that one candidate; failed
  creation never returns a secret. The active key cap defaults to 8 per slot.

Every response uses `Cache-Control: no-store`. Unexpected adapter exceptions
produce a content-free 503. Request bodies, signatures and secrets must never
be logged by the executor, gateway, browser integration or error middleware.
The page loads no remote third-party scripts, analytics or external fonts.

## Offline route preparation

Use the shared SDK in the node checkout (verified against
`node-sdk-runner-routes` commit `7c4b2bdf`):

```sh
python prepare_routes.py --sdk-lib /path/to/node/pvm/Lib \
  --deployment /path/to/explicit-deployment.json --output /path/to/route-review.json
```

The deployment JSON requires exactly `bundle_hash`, `runtime_hash`,
`ingress_verify_key` (lowercase hex32), `actor_sponsorship`, `replay_authority`
(`management` or `executor`) and `routes`. `routes` maps each exact
`"VERB /path"` in the intent to `enabled`, `limits`, `session_limits`,
`sponsorship`, `pricing`, `mounts`, `secret_refs`, `forward_headers` and
`metering_key`. Limits, session limits and sponsorship use the SDK field names
and integer wei amounts. No budget or pin is generated by this script.

`pricing` contains explicit `allow_zero` and hex32 `price_advert_digest`.
`mounts` are hex-encoded SDK VolumeAttachment bytes. `secret_refs` maps every
required intent label to `{purpose, ref}`; purpose is `read`, `verify`, or
`sign`, except `metering_sign` uses `metering`. References are hex52 SDK
SecretKeyRef bytes. `metering_key` is the compressed secp256k1 public key as
hex33 for the session route, otherwise null. SDK validation rejects duplicate
or conflicting key purposes. Bearer routes must forward `authorization`.
Executor replay mode requires removing the management nonce secret binding;
the default management mode requires it on every authenticated route.

Preparation requires exact route and secret-label coverage, builds each
RouteDeclaration through the SDK, validates it, and checks the outer table
projection. The review JSON includes route IDs, table entries and encoded actor
sponsorship, with byte values tagged `{hex: ...}`. This JSON is not an
installable Routes object. It proves offline schema/consistency and lane fit,
not bundle availability, live persistence fit, entitlement, budget availability
or native admission. Test inputs are synthetic and must never be deployed.

## Routes

All routes use native `RunnerRoute` declarations with actor funding and explicit
resource/sponsorship bounds. No legacy Runner lowering or PVM HTTP handler is
implemented here.

| Method | Path | Authentication | Implementation |
|---|---|---|---|
| GET | `/manage`, `/manage/` | public | Page shell |
| GET | `/manage/assets/manage.css`, `/manage/assets/manage.js` | public | Fixed allowlisted assets |
| GET | `/manage/assets/wallet.js`, `/manage/assets/vendor/{sha3,_u64,utils,crypto}.js` | public | Fixed allowlisted signer/hash modules |
| GET | `/manage/config` | public | Trusted public actor, chain ID, network and handler |
| GET | `/manage/api/catalog?slot=...` | account | Public model IDs/limits, public base URL, hire status |
| GET | `/manage/api/keys?slot=...` | account | Public key metadata, including revoked keys |
| POST | `/manage/api/keys?slot=...` | account | Body `{"name":"OpenRouter"}`; secret returned once |
| DELETE | `/manage/api/keys/:id?slot=...` | account | Idempotent revocation |
| GET | `/manage/api/usage?slot=...` | account | Bounded usage read |
| PUT, DELETE | `/manage/api/read-grant?slot=...` | account | Register/remove a whole-volume read-only grant for the authenticated hirer; 503 if unconfigured |

The catalog is the same `catalog.json` used by inference, shaped as
`{models: {public_id: {target, upstream_model, context_tokens, max_output_tokens}},
pricing_version, limits: {max_requests, max_concurrency}}`. Only model IDs,
context/output limits and the two public session limits leave the management
handler. Serving endpoints, upstream model names and delegation do not.

The page uses `window.cowboy.connect()` / `signMessage(hashHex)`, the existing
Cowboy wallet provider. `wallet.js` constructs the canonical CBOR proof, hashes
the exact UTF-8 body and raw target, and sends the gateway's account headers.
The provider returns `{r,s,v}`; v must be raw 0..3 and S low. Private keys stay
in the wallet. Keccak is self-hosted from the pinned MIT-licensed noble-hashes
1.8.0 modules; no CDN or CSP exception. A host may still supply an equivalent
`window.cowboyWallet.connect/signedFetch` bridge. The signer uses fresh 32-byte
nonces, a 120-second expiry, no cookies/storage, no automatic retries, and
refuses redirects, cross-origin targets and normalization of the signed target.
This does not bring dashboard passkey/Google wallets across origins; those need
an actual wallet bridge. Without a provider the page reports unavailability.

PUT read-grant accepts exactly `{signing_key, encryption_key}`, two lowercase
32-byte public keys in hex. It never accepts an address, access mode or prefix
from the caller. The retained hirer is the grantee, including for paused hires.
The CLI performs owner-authorized registration and private DEK sealing; Python
returns only owner/name/read-only coordinates. DELETE removes that hirer's
grant. The panel is shown only when the runtime has configured grant bindings.

## Usage and pricing

`from` and `to` are inclusive UTC dates. Default: the last seven calendar days,
including today. Maximum range: 31 days. Optional `key_id` and `model` filters
are applied after slot authorization. Admission time controls date selection.

The scan folds journal segments in numeric order per session and deduplicates
by `(session_id, request_id)`. It accepts the journal's first-line segment
headers and reports `committed_at` as the writer's recorded commit time, not a
chain-confirmed timestamp. `latest_record_at` remains separate. Legacy segments
without a header yield no commit timestamp. The producer/consumer test uses the
actual Journal class, not a separately invented format.

Bounds are 4,096 segments, 16 MiB of segment bytes, 50,000 distinct request
identities and 128 price versions per read; price tables are at most 64 KiB.
Key metadata scans are limited to 10,000 records of at most 4 KiB each. Exceeding
a bound returns 503 rather than silently reporting truncated totals. A production
backend must bound allocation during list/read as well; this interface currently
materializes lists/bytes before checking their length. Larger histories need an
index or paginated snapshot API before these limits can be raised safely.

Immutable `pricing/<version>.json` files use:

```json
{"schema":1,"unit":"nano_usd_per_token","models":{"cowboy-v1":{"input":100,"output":200}}}
```

Both rates are nonnegative integers. Zero means a configured zero rate. No price
file/model rate means unavailable. Reads cache each version once per snapshot.
The current catalog price is never substituted for a record's admitted version.
Token totals and cost are decimal strings to preserve integer precision through
JavaScript. Request counts are bounded JSON integers. Unknown/pending usage
does not contribute tokens or cost; totals carry separate unknown/unpriced
counts and partial flags. If no value is known, the total is null, never zero.
Usage cost is not an invoice or balance deduction.

## Verification and remaining gates

From this directory:

```sh
uv run --with pytest --with cryptography --with coincurve --with pycryptodome python -m pytest tests -q
# Include the real SDK permission wrappers with its CPython mock host:
COWBOY_SDK_PATH=/path/to/node/pvm/Lib uv run --with pytest --with cryptography --with coincurve --with pycryptodome python -m pytest tests -q
node --input-type=module --check < offchain/page/manage.js
node --input-type=module --check < offchain/page/wallet.js
# With Playwright resolvable on NODE_PATH and Chrome installed:
node tests/check_page.cjs
node tests/check_wallet.cjs
```

Actor tests append the SDK library to sys.path; do not use PYTHONPATH, which
would shadow CPython's standard library with the PVM library. The SDK mock host
proves the Python owner guard, validation, slot bound and reinitialization denial,
not native PVM execution. Without that SDK path the actor tests explicitly skip.

The browser check uses an isolated Chrome profile and an explicitly fake
wallet/API. It covers public loading, absent wallet integration, key lifecycle,
secret clearing, lost-response no-retry, text-safe rendering, partial usage,
absence of browser storage, and desktop/mobile layout. It proves no real wallet,
gateway admission, CBFS write, chain state or model execution. `check_wallet.cjs`
also runs the actual browser codec against the shared golden vector, signs via a
provider using the published test key, and verifies captured HTTP requests with
the Python verifier. It covers exact targets/bodies, nonce freshness, rejected
signing, invalid recovery IDs and refused external/normalized targets. It does
not operate a real installed wallet. The runtime composition test uses real
crypto and subprocess adapters with a fake CLI/chain backend.

Before deployment: supply the native declaration builder/validator, actual slot
configuration, bundle/runtime pins and publish the Store capability; bind actual executor
context, authoritative retained Hire/actor readers and wallet provider; provision
the shared nonce volume (or configure the native executor claim verifier) and
bind actual CBFS state directories and purpose-scoped CBSS releases. Verify the
native admission and management replay handoff with exactly one consumer.
Then run the real gateway, hire pause, cross-account, key revocation,
concurrent-runner, private-volume grant/revocation and model-usage gates. Local
composition and browser tests do not close those deployed integration gates.
