---
title: "CIP-42: Standardized Daemon Status (/statusz)"
description: One GET /statusz JSON self-report served by every Cowboy daemon, with a canonical Rust schema and cowboy top as the first consumer
icon: heart-pulse
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core (Operations)
  **Created:** 2026-08-11
  **Related:** CIP-9 (runner-backed storage — the relay registry this CIP's chain view reads), CIP-24 (CBSS), CIP-39 (CBQS)
</Note>

## Abstract

This CIP specifies `GET /statusz`: one JSON self-report with a shared
envelope and a system-specific drill-down payload, to be served by every
Cowboy daemon — validator RPC, indexer, CBFS relay, cbssd, cbqsd, runner.
The canonical schema is Rust, in `cowboy-protocol-types::statusz`
(cowboy-protocol#79, pending): a daemon that compiles against it serves the
same shape every consumer parses. `cowboy top` (node#1307, pending) is the
first consumer. See [Implementation status](#implementation-status) for the
full PR stack; nothing named by this CIP is merged yet.

## Background

Cluster health lives in per-system formats: the node's `/health/*` family,
cbqsd's former `/healthz` + Prometheus text, cbfs and cbssd metrics
registries, and nothing at all from the runner. The gateway
(`/_cowboy/health`) and the RAS write-relayer (its own `/healthz`) are two
more dialects again. Any tool that wants a topological view must speak all
of them, and each new daemon invents another. (The gateway and
write-relayer are not in this CIP's initial `SystemKind` set; adding them
is the compatible-extension path described under
[Compatibility](#compatibility).)

Two sources of truth exist and both matter. The **chain view** (the node
RPC's read surface: `/ras/stats`, `/runners/active`, `/cbss/proxies`,
`/cbqs/broker-state` — the explorer-class aggregates are unauthenticated
and per-IP rate-limited) is what the network believes about a component and
is the only view a public user can reach. The **self-report** is what the
daemon knows about itself — version, uptime, live counters — and is an
operator surface. This CIP standardizes the second; it does not replace the
first. Consumers merge them; see the precedence rule under
[Envelope](#envelope).

**Goal:** one endpoint, one schema, so status consumers scale with the
contract instead of with the daemon count.

## Specification

The key words MUST, MUST NOT, SHOULD, and MAY are to be interpreted as in
RFC 2119. The rustdoc of `cowboy-protocol-types::statusz` is normative
alongside this document; where they disagree, the Rust module wins and this
CIP has a bug.

### Endpoint

`GET /statusz` → `200` with `Content-Type: application/json`, on the
listener and exposure each daemon's row specifies:

| System        | Listener                            | Exposure |
| ------------- | ----------------------------------- | -------- |
| validator RPC | RPC router                          | dev mode only (see [Security considerations](#security-considerations)) |
| indexer       | API router                          | public — its whole surface is public read |
| CBFS relay    | blob HTTP listener                  | loopback only, by conditional mount: the route MUST NOT be mounted when the listener binds a non-loopback address |
| cbssd         | ops listener (`/metrics` sibling)   | loopback only: the listener MUST refuse a non-loopback bind |
| cbqsd         | broker transport router             | wherever the broker listens (replaces its former `/healthz`; see [Compatibility](#compatibility)) |
| runner        | dedicated listener (`statusz_addr` config, off by default) | loopback only: the listener MUST refuse a non-loopback bind |

Exposure is a per-row requirement, not a property inherited from
`/metrics` (the node's `/metrics`, for instance, has a different posture
than its `/statusz`). Where a row says "loopback only", the stated
mechanism is the requirement an implementation carries.

A poller MUST treat "no `/statusz`" as a rendered state (fall back to the
chain view), never an error.

### Envelope

Canonical definition: `cowboy-protocol-types::statusz::StatuszReport`.

```json
{
  "system": "cbfs-relay",
  "version": "0.3.1",
  "network": "mesa",
  "status": "healthy",
  "uptime_seconds": 86400,
  "boot_id": 17293822569102704642,
  "counters": { "blob_uploads_total": 12034 },
  "gauges":   { "store_used_bytes": 1073741824, "store_capacity_bytes": 10737418240 }
}
```

- `system` — one of `node`, `indexer`, `cbfs-relay`, `cbssd`, `cbqsd`,
  `runner`. Reports self-identify: a consumer polls a URL list and buckets
  by this field, so fleets (relays, runners) need no per-instance config.
- `version` — build version string; display-only, daemon's choice of
  format.
- `network` — the network the daemon believes it is on (e.g. `mesa`,
  `canyon`, `local`). Empty string is the defined sentinel for "this
  daemon does not know its network"; consumers display it as absent, never
  as a network named `""`.
- `status` — the daemon's judgment of its own duty: `healthy` |
  `degraded` | `unhealthy` are the only values a daemon emits (a cbqsd
  whose store is unwritable is `unhealthy`, while one that cannot refresh
  its registry snapshot is `degraded`, because CIP-39 §3 requires it to
  keep serving from the snapshot it has; a runner missing three heartbeats
  is `degraded`). `unreachable` is a poller judgment and
  never appears on the wire. A fourth wire value, `unknown`, exists as
  reader vocabulary: both wire enums carry a `#[serde(other)]` `Unknown`
  fallback, so an unrecognized string buckets instead of failing the
  report — and a bucketed report re-serialized through the typed struct
  legally emits the literal `"unknown"`. Strict validators MUST accept it.
  Consumers MUST NOT re-serialize a bucketed report as if it were the
  original (the source string is not preserved); forward raw bytes
  instead. When merging judgments from multiple sources, the worse one
  wins, in the order `healthy < degraded < unhealthy`; an unrecognized
  status MUST be treated as at least as severe as `degraded`.
- `uptime_seconds` — seconds since the daemon started.
- `boot_id` — random per-process identity, generated once at startup.
  Counter deltas are only meaningful within one `boot_id`: a restart that
  re-reaches its old count, or a load balancer swapping the instance
  behind the URL, presents as an *increase* that no decrease rule can
  catch. Pollers MUST skip the delta interval whenever `boot_id` changes.
  `0` is the sentinel for a daemon that predates the field; pollers then
  fall back to decrease-only detection.
- `counters` — monotonic series: never decreasing within one `boot_id`.
  Most are process-lifetime totals; an externally monotonic series (e.g.
  chain height) is also a valid counter — it just keeps climbing across
  boots. **Daemons never compute rates for this endpoint**; pollers derive
  activity from deltas between their own ticks, scoped to one `boot_id`. A
  decrease within one `boot_id` MUST be treated as a reset (skip the
  interval, never compute an underflowed delta).
- `gauges` — **integer** (`u64`) point-in-time values, possibly the
  daemon's most recent cached observation (e.g. a chain probe on its own
  cadence) — the poll interval, not the envelope, bounds staleness. Every
  declared gauge is a discrete integer quantity (counts, bytes, seconds,
  heights, epochs); `f64` is excluded because a non-finite value
  serializes to JSON `null` and poisons the report parse exactly when a
  daemon is in trouble, and byte counters lose precision above 2^53.
- `detail` — free-form system-specific JSON for drill-downs; it may evolve
  without an envelope change. **Omitted from the JSON** when the daemon
  has none (readers of the typed struct see `Value::Null` either way).

Well-known counter/gauge keys live in `statusz::keys`; the example above
uses only registered keys. Daemons populate what they have and SHOULD
register new keys rather than emit bare literals (a registered key is
covered by the rename-guard test; a literal is not); consumers tolerate
absent and unknown keys.

Envelope evolution: new fields land as `#[serde(default)]` —
**wire**-compatible (old readers ignore them, old payloads default) but
**source**-coordinated: producers construct the struct by literal, so any
new field is a compile error in every daemon repo until its pin bump adds
it; that error is the coordination mechanism. Adding a field without
`#[serde(default)]` is a breaking wire rev and needs the full stack to
move together.

### Transport

The endpoint is served over HTTP **GET** — the verb is part of the
contract. A successfully generated report is always `200` with
`Content-Type: application/json`, **regardless of the health state it
carries** — `unhealthy` is a self-report from a daemon that can still
answer, not a 5xx. An implementation that returns 5xx for unhealthy makes
pollers misclassify the daemon as unreachable, the opposite of this
endpoint's purpose. Non-2xx means the report could not be produced.

### Compatibility

Adding a `SystemKind` variant or a well-known key is a compatible change
(consumers ignore unknown keys, bucket unknown systems via the `Unknown`
fallback, and must be rebuilt to *place* a new system in a UI — the
intended pin-bump discipline). Renaming any contract literal is breaking
and pinned by test: enum strings and the route by
`wire_strings_are_the_contract`, the key registry by
`key_strings_are_the_contract`.

cbqsd's former `/healthz` is removed, not deprecated: pre-launch there is
no installed base and no external prober depended on it
(aws-infrastructure was checked), per the workspace's
no-back-compat-shims convention. Post-launch, removing an operational
endpoint would require a deprecation window; that machinery is
deliberately out of scope now.

## Security considerations

`/statusz` is an unauthenticated read endpoint by design; its security
model is exposure control, not authentication.

- **Information disclosure.** The envelope necessarily carries `version`
  and `uptime_seconds` — exactly the pair COW-762 withholds from the
  validator's public surface, because build fingerprints and restart
  timing aid targeting of consensus nodes. Hence the validator row is
  dev-mode-only: in production the route is not registered at all, and
  public consumers use the chain view. For non-consensus daemons the same
  pair is less sensitive but not free: the default posture is loopback
  (cbfs relay, cbssd, runner). The two deliberate exceptions are the
  indexer, whose entire surface is already public read and whose operator
  accepts version disclosure as part of running a public archive, and
  cbqsd, whose broker listener is an operator-run, TLS-terminated service
  endpoint — its report adds traffic counters, never queue contents or
  key material. cbssd's gauges (`shares_held`, `committee_epoch`) reveal
  committee participation shape, which is why its row is loopback-only
  with a MUST-refuse bind check.
- **Denial of service.** Handlers MUST be cheap reads of in-process state
  (atomic counters, cached observations, a small directory listing) and
  MUST NOT perform network calls on the request path. Public rows inherit
  whatever rate limiting their listener already carries.
- **Fail-closed bind checks.** Where the endpoint table says loopback
  only, the implementation MUST enforce it in code (refuse the bind or
  conditionally mount), not in deployment convention; cbssd's existing
  refusal is the reference behavior.

## Implementation status

Nothing below is merged as of this draft; the CIP describes the target
state of an open PR stack, in merge order:

| Artifact | Where | PR |
| --- | --- | --- |
| Schema: `cowboy-protocol-types::statusz` | cowboy-protocol | [#79](https://github.com/cowboyinc/cowboy-protocol/pull/79) |
| CBFS relay `/statusz` (`node/src/blob_http.rs`) | cbfs | [#135](https://github.com/cowboyinc/cbfs/pull/135) |
| cbssd `/statusz` (`ingest.rs`, `serve_ops` — introduced by the PR) | cbss | [#84](https://github.com/cowboyinc/cbss/pull/84) |
| cbqsd `/statusz` (`transport/mod.rs`) | cbqs | [#10](https://github.com/cowboyinc/cbqs/pull/10) |
| runner `/statusz` (`runner-node/src/statusz.rs`) | runner | [#214](https://github.com/cowboyinc/runner/pull/214) |
| node RPC + indexer `/statusz`, `cowboy top` (`cli/src/top/`) | node | [#1307](https://github.com/cowboyinc/node/pull/1307) |

Merge order is a hard constraint, not a preference: consumers that
depend on each other must pin **one** cowboy-protocol rev or cargo
builds two instances of the types crate (each repo's
`check-cowboy-protocol-revisions.py` gate enforces this). The statusz
branch is therefore based on the protocol **devnet lineage** (the union
that also carries the CBQS client crates, which never landed on `main`),
and cowboy-protocol#79 targets `devnet`. Order: (1) protocol#79; (2)
cbfs#135, cbss#84, cbqs#10 — independent of each other; (3) node#1307,
which pins cbfs's merged rev; (4) runner#214 last — it pins node, cbfs,
and cbss revs and its gate only converges once those exist. Until the
stack lands, consumers pin the branch-head union rev; each PR re-pins to
the durable merged rev of its parents immediately before merging.
