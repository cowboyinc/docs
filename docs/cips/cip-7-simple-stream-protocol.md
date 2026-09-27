---
title: "CIP-7: Watchtower"
description: "Signed feeds on CBQS and CBFS, bounded on-chain authorization, checkpoints and predicate wake."
icon: tower-broadcast
---

<Note>

**Status:** Draft specification; the near-chain implementation and coordinated devnet replacement have not shipped.<br/>
**Updated:** 2026-09-23<br/>
**Requires:** [CIP-3](./cip-3-fee-model), [CIP-5](./cip-5-timers), [CIP-9](./cip-9-runner-storage), [CIP-39](./cip-39-cowboy-queue-system).<br/>
**Related:** [CIP-18](./cip-18-payments), [CIP-24](./cip-24-secrets-manager), [CIP-28](./cip-28-cowboy-agent-banking).

</Note>

The words MUST, MUST NOT, SHOULD and MAY specify conformance requirements. All limits and formats below describe the replacement release, not existing SDK or node behavior. Only disposable devnet exists; coordinated replacement requires no historical-state migration, dual decoder or legacy service fallback. This document does not authorize a network reset.

## Summary

Watchtower provides publisher-signed, ordered feeds. An `ANCHORED` feed stores bodies on CBQS and CBFS and periodically commits a head and message-index root to consensus. A `CONSENSUS` feed stores a small bounded message window in actor state. Actors cannot fetch near-chain data: a submitter supplies evidence and requests bounded callback dispatch.

The replacement removes the shared Stream Key Manager's per-epoch and per-subscriber-per-epoch rows, which can exhaust its 10,000-key budget (COW-3365). New state is bounded per feed and per active relationship. Content-key delivery uses the Watchtower service, not CIP-7 SealRequests. This changes the trust boundary: v1 trusts that service for reported usage and authorized key delivery.

## Scope and responsibilities

| Component | Responsibility |
| --- | --- |
| CBQS | Opaque append, delivery, cursor replay and time/byte retention under CIP-39 |
| CBFS | Body and proof-material storage, retrieval and archive under CIP-9 |
| Watchtower service | Publisher signing, durable sequence allocation, CBQS/CBFS writes, publisher-chain validation, Merkle index, anchors, subscriber authentication, key delivery, usage receipts and wake submission |
| Feed actor | Configuration, authorization, evidence verification, usage settlement and bounded wake dispatch |
| Recipient actor | Authenticated subscription, deterministic guard recheck and business callback |

One provider EOA owns each feed and its dedicated CBQS stream. The registry binds the feed to that stream, provider account and service endpoint. CBQS grants authorize transport operations; they do not prove a paid Watchtower entitlement. The publisher key signs messages; the usage key signs metering receipts. They are separate registered ed25519 keys even if the same operator controls both; initialization and rotation MUST reject equal publisher and usage public keys. A CBQS checkpoint proves its own transport commitment, not Watchtower finality.

These responsibilities MAY run in one deployment. This CIP does not require a new repository or put a product-specific daemon in generic Runner. v1 has one provider, no handoff, no automatic failover, no actor-owned feed and no automatic CIP-2 ingestion bridge. An external publisher may use a Runner result as input, but correctness of that result is outside this CIP.

## Feed Classes

- `ANCHORED` is the default and the only paid class. CBQS carries the ordered hot window; bodies above the inline limit and archives use CBFS. Consensus does not retain the continuous body stream. A bounded wake job may temporarily carry one submitted inline message for execution.
- `CONSENSUS` is open-only. Each owner-authorized signed message is committed to the feed actor's ring buffer and admitted atomically to its bounded wake queue. When that queue is full, `publish` rejects with `WAKE_BUSY` and commits neither the message nor its sequence; the provider retries the identical signed message after capacity becomes available (subject to timestamp validity). It needs no separate off-chain submitter: one provider-funded CIP-5 timer per feed, interval one block, dispatches the oldest committed job using the same bounded dispatch rules. Initialization MUST register that timer atomically, and it cannot consume subscriber callback funds for its own overhead. Timers are one-shot: an authentic firing must match the canonical Feed's stored timer ID; it schedules a new dispatch timer for the next block and stores the new ID, with the same immutable provider as payer under the narrowly scoped CIP-5 renewal rule. After funding exhaustion/removal, the owner replenishes its balance and calls `restart_dispatch_timer()`; this is idempotent while the recorded timer remains live. No other caller may rearm or select a payer. Exhausted timer funding stops dispatch and is visible as a provider availability failure. Delivery still requires funded callbacks and block capacity.

Class, provider, CBQS stream, numeric scales and tariff are immutable after initialization. To change them, create a new feed. No class-switch or provider-handoff protocol is part of v1.

## Limits and state budget

Integers MUST use checked arithmetic. Exhaustion rejects the operation; it MUST NOT wrap identifiers or bypass the host's key/byte limits. CBY amounts are native atomic units (10^9 per CBY), stored as u64; products use checked u128 intermediates.

| Limit | v1 value |
| --- | --- |
| Inline body, including nonce/ciphertext/tag when encrypted | 16,384 bytes |
| CBFS body | 16 MiB |
| Canonical message envelope, including inline body | 24 KiB |
| Guard | 512 encoded bytes, depth 4, at most 16 comparisons |
| Tags | At most 16; keys at most 32 UTF-8 bytes; text values at most 128 bytes |
| `kind` / `content_type` | At most 64 / 128 UTF-8 bytes |
| Numeric tag type / scale | Signed i64 / decimal scale 0–9 |
| Wake subscriptions per feed | 1,024 |
| Usage authorizations per feed | 1,024 |
| CONSENSUS ring capacity | 1–4,096 messages, default 1,024 |
| Retained anchors / key-generation records | 1,024 / 64 |
| Pending wake jobs per feed | 4 |
| Scan / callbacks per transaction | At most 64 subscription records / 32 callbacks |
| Callbacks per feed per block | At most 64, additionally bounded by CIP-3 block capacity |
| Callback cycles / memory cells | CONSENSUS: at most 100,000 / 20,000; ANCHORED: at most 1,000,000 / 20,000, charged under CIP-3 |
| Replay page | At most 128 messages and 1 MiB of envelopes, whichever comes first |
| Minimum anchor spacing / append-to-anchor deadline | 600,000 ms / 600,000 ms of committed block time |
| Body/proof retention | 7 days (604,800,000 ms) after first covering anchor finality |
| Wake message maximum age / future skew | 1 hour / 30 seconds against committed block time |
| Pending job lifetime | 256 blocks |
| Content-key epoch | 600,000 ms |

A feed uses at most 16 fixed metadata rows, 1,024 subscription rows (including watermark and wake budget), 1,024 authorization rows (including cumulative settlement), 1,024 anchor rows, 64 key-history rows, 4 jobs, and 4,096 ring rows: at most **7,252 keys**. No auxiliary on-chain index may silently add unbounded rows. All records MUST fit the host's value-size limit; serialized size and total actor byte limits remain enforced independently. Admission may fail earlier on a lower configured host limit. ANCHORED feeds have no ring rows.

Counters for authorization IDs, subscription IDs, generation, sequence and anchor sequence are retained in fixed metadata, never decremented or reused. Closed relationship rows can be deleted without retaining one tombstone per customer because recreating a relationship receives a new ID from the feed-wide counter. There is no shared global SKM customer table.

Before admitting a row, prune eligible expired records. Live anchors/keys MUST NOT be evicted to make room: reject with `CAPACITY_REACHED` until safe cleanup is possible. Subscription deletion atomically returns unused reserved wake funds; unresolved callback liability cannot be deleted. No row grows once per message, epoch or receipt except the explicitly bounded ring/anchor windows.

## Registry and configuration

`StreamConfig` has exactly these fields:

| Field | Type / rule |
| --- | --- |
| `chain_id`, `generation`, `next_authorization_id`, `next_subscription_id` | u64; generation and allocated IDs start at 1 |
| `stream_id` | bytes32; MUST equal the unique feed actor address left-padded with 12 zero bytes |
| `owner` | address20, authenticated provider EOA |
| `feed_class` | `ANCHORED` or `CONSENSUS` |
| `publisher_key`, `usage_key` | ed25519 bytes32 |
| `cbqs_stream_id` | bytes32 for ANCHORED, null for CONSENSUS |
| `service_url` | HTTPS URL, at most 2,048 bytes; untrusted network locator |
| `numeric_tag_scales` | map of declared numeric tag names to u8 scale; immutable |
| `access_mode` | `OPEN` or `SUBSCRIBER_PAID`; CONSENSUS MUST be OPEN |
| `unit_price` | u64 total CBY atomic units per delivered body byte; zero only for OPEN |
| `publisher_treasury` | address20; immutable, nonzero |
| `ring_buffer_capacity` | u32, within the bound above; zero for ANCHORED |

`chain_id` is the genesis network identity advertised as `/chain-info.chain_id`, not the separate CIP-8 `session_chain_id`. Initialization, publisher messages, usage receipts and access signatures bind this same network identity.

All keys and configuration needed for verification are read from committed state. Payloads naming an owner or actor do not authenticate that identity. Initialization and administrative calls require the actual owner sender; subscriptions and spending authorizations require the actual subscriber account/actor context. A keyless actor uses authenticated actor calls, never a caller-supplied address as authority.

v1 has one tariff: bytes of the delivered encrypted body (or plaintext for OPEN), including its AEAD envelope, excluding transport/envelope overhead. Replaying a body is another delivery. Key delivery is included and not billed separately. Connection-hour, per-message and mixed tariffs are deferred. Changing price requires a new feed and new customer consent.

## Canonical Hashing and Signing

Encoding is deterministic CBOR: shortest integer/length forms, definite lengths, map keys sorted by encoded length then bytewise, no floats, duplicate keys, tags or unknown fields. Strings are valid UTF-8 without normalization. Numeric tag values are i64 CBOR integers. `bool` is distinct from integer. Fixed byte fields are CBOR byte strings, never hex text.

The signing object is a map with exactly:

```text
version, chain_id, stream_id, generation, sequence, prev_message_hash,
timestamp_unix_ms, kind, content_type, tags, payload_format, payload_ref,
payload_hash, key_epoch, signing_key_id
```

`version = 1`; chain/generation/sequence/time/key_epoch/signing_key_id are u64; `stream_id`, `prev_message_hash`, `payload_hash` are bytes32. `signing_key_id = generation`. `payload_format` is the text `PLAINTEXT` or `CIPHERTEXT`; `payload_ref` is `INLINE` or `CBFS`. `key_epoch` is null for plaintext. The signature is ed25519 over `ASCII("cowboy/watchtower/message/v1") || canonical_cbor(signing_object)`. The envelope hash is SHA-256 of those same signing bytes, excluding the signature.

The transport map adds `publisher_sig` (bytes64), `payload_inline` (bytes or null), and `payload_handle` (bytes or null, at most 2,048 bytes). Exactly the body field selected by `payload_ref` is non-null. The handle is an untrusted locator; the signed body hash authenticates bytes, not location. `payload_hash = SHA256(complete body bytes)`, including nonce and authentication tag for ciphertext. A resolver MUST bound a fetch by the declared maximum body size and verify its hash before accepting it.

Sequence starts at 1 and is contiguous across generations. The first message has a zero predecessor hash; all others bind the preceding envelope hash. The service MUST persist the complete signed envelope and append intent before network submission, serialize publishing per feed, and recover that same envelope after uncertain append. It MUST NOT sign a different message for an already assigned sequence. Publisher admission and CONSENSUS publish MUST reject a timestamp more than 30 seconds ahead of their observed committed chain time. Transport retries may duplicate the identical envelope; conflicting same-sequence envelopes stop publication and require operator reconciliation.

### Numeric tags and guards

The SDK MUST convert decimal literals exactly to the declared scale. At scale 2, `200.00` encodes as 20000; `200.005` rejects. No implicit rounding, float conversion, undeclared numeric tags, overflow or scale mutation is permitted.

Guards are CBOR maps over `kind`, `tags.<key>`, `sequence` and `timestamp_unix_ms`. A comparison is `{field: text, op: text, value: scalar}`; `exists` omits value. Operators are `eq`, `ne`, `in`, `nin`, `gte`, `lte`, `gt`, `lt`, `exists`; membership uses an array of at most 16 same-type scalars. Logical nodes are exactly one of `{all: [guards]}`, `{any: [guards]}`, `{not: guard}`; all/any contain 1–16 children and total comparisons remain bounded above. Missing fields evaluate false, including `ne` and `nin`; type mismatch rejects at subscription validation. Text supports equality/membership only; booleans equality only; integers support comparisons/membership. Unknown syntax rejects.

Guards are pure functions of one header, with no memory or side effects. Their shared evaluation identity is SHA256(`ASCII("cowboy/watchtower/guard/v1") || canonical_cbor([chain_id, stream_id, numeric_tag_scales, guard])`). Watchtower may deduplicate off-chain evaluation; recipients MUST re-evaluate before acting. A trailing stop or private predicate belongs in application compute, not this DSL. Header tags are visible to the service and to consensus when submitted; encryption does not hide them.

## Key Plane

Paid ANCHORED bodies MUST be encrypted; OPEN messages MUST be plaintext. The publisher holds a CSPRNG-generated 32-byte `stream_secret`. Neither consensus nor a CBQS operator acting solely as transport receives this secret.

```text
key_epoch = floor(timestamp_unix_ms / 600000)
content_key = HKDF-SHA256(
  IKM = stream_secret,
  salt = ASCII("cowboy/watchtower/content-key/v1"),
  info = u64_be(chain_id) || stream_id || u64_be(generation) || u64_be(key_epoch),
  L = 32)
```

The publisher assigns and signs the timestamp; consumers verify the epoch formula. This is publisher-asserted time, not proof that a message existed at a block height. Timestamps MUST be nondecreasing along the publisher chain. Live wake additionally enforces the age/skew bounds; archival reads do not reject solely because the timestamp is old.

The cipher is XChaCha20-Poly1305. Body bytes are `nonce(24) || ciphertext || tag(16)`; nonce comes from a CSPRNG and MUST NOT repeat under a key. AAD is canonical CBOR of exactly `{version, chain_id, stream_id, generation, sequence, timestamp_unix_ms, key_epoch, kind, content_type}` with values taken from the signing object. The body hash is calculated after encryption.

`rotate_keys(new_publisher_key, new_usage_key)` increments generation at the last finalized head, records its last sequence/hash and first new sequence, and keeps sequence continuity. Pending wake jobs MUST be empty. For ANCHORED, the service MUST first anchor all published messages and stop appending until rotation commits; consensus cannot prove the service has no withheld messages. New messages use the new generation and signing key. Rotation immediately invalidates old-generation usage authorizations; the service MUST settle owed receipts before requesting rotation. Subscribers explicitly authorize the new generation. Retired-key records remain at least seven days after the rotation block becomes final; the current generation key is never pruned. Later anchors that include an old prefix do not extend the old key's retention obligation.

Changing generation changes derived content keys but does not protect against compromise of `stream_secret`. In that case the publisher MUST replace the secret before resuming. Previously delivered keys and plaintext cannot be revoked. A revoked customer that holds the current epoch key may still decrypt that epoch's ciphertext if it obtains it.

For paid reads/key delivery, v1 subscribers MUST be EOA accounts. The service issues a CSPRNG 32-byte challenge scoped to the requested operation, expiring within 60 seconds; it retains at most 4,096 outstanding challenges and rejects admission when full. The subscriber signs a 65-byte recoverable secp256k1 signature over `keccak256(ASCII("cowboy/watchtower/access/v1") || u64_be(chain_id) || stream_id || subscriber(20) || u64_be(authorization_id) || SHA256(canonical_cbor(operation)) || challenge(32) || u64_be(expires_at_ms))`. The operation is exactly `{method: "read_since", sequence: u64, limit: u32}`, `{method: "tail"}`, or `{method: "get_content_key", generation: u64, epoch: u64}`. The service verifies the recovered subscriber, single-use challenge, signed operation and expiry, then the live committed authorization and generation, before confidential delivery over authenticated HTTPS. Consuming the challenge is atomic; expired entries may be deleted and used nonces are never reissued. Retrying obtains a fresh challenge but cannot bypass the cumulative allowance. v1 supports EOA-authenticated external key consumers; keyless-actor paid-key delegation and encrypted-body callbacks are deferred. Service key lookup responses MUST NOT expose `stream_secret`. Key delivery has no per-epoch on-chain row.

## Billing

This is Watchtower usage billing, separate from CIP-18 request purchases and CIP-28 outbound purchasing allowances. Reusing arithmetic/authorization primitives does not make their wire formats interchangeable. v1 supports an allowance drawn from subscriber balance; there is no deposit mode and no guarantee of reserved funds for served-but-unsettled usage.

`authorize_usage(total_limit, expires_at_ms)` creates or replaces the caller's row after validating an EOA subscriber, a nonzero limit and future expiry. Replacement assigns a fresh feed-wide authorization ID and atomically revokes the old row. The row snapshots chain, feed, subscriber, provider/treasury, generation, usage key, tariff, expiry, limit, cumulative bytes and cumulative total settled. There is at most one live row per subscriber per feed. Expiry is exclusive against committed block time.

The service signs `ASCII("cowboy/watchtower/usage/v1") || canonical_cbor(receipt)` with the snapshotted usage key. The receipt is exactly `{chain_id, stream_id, generation, subscriber, authorization_id, expires_at_ms, unit_price, cumulative_bytes, cumulative_amount}`. IDs/time/counts/prices/amounts are u64; addresses are bytes20. `cumulative_amount` MUST equal checked `cumulative_bytes * unit_price` and not exceed the authorized limit.

`settle_usage(receipt, signature)` is permissionless but MUST validate every field against the live row, its signature, generation and expiry. Lower counters reject; identical counters are an idempotent no-op. For an increase, debit only `new_total - settled_total`. Insufficient balance rejects atomically and the service SHOULD stop delivery. Underreported usage forfeits corresponding revenue and protocol fees; overreporting remains a service trust risk bounded by the customer's cumulative authorization.

The protocol fee is included in the authorized total, fixed at 500 bps for v1. At cumulative total A, `fee(A) = floor(A * 500 / 10000)`. On settlement the fixed protocol Treasury account **`0x0000000000000000000000000000000000000008`** (node `SystemActorAddresses::TREASURY`) receives `fee(new) - fee(old)` and the configured immutable `publisher_treasury` receives the remaining debit. The debit and both credits MUST commit atomically; an uncreditable recipient rejects the whole settlement. This Watchtower fee destination is fixed for v1 and is not a configurable caller input or the separate [CIP-31](./cip-31-cbfs-rent-schedule) platform-fee account `0x18`. This prevents receipt splitting from changing rounding. Debits, both credits and counters MUST commit or roll back together.

`revoke_usage()` immediately deletes the live row at its chain ordering point. Expired/revoked authorizations cannot settle previously unsubmitted usage; v1 gives the service no late-claim right. That collection risk is explicit. Reauthorization never accepts receipts from an old ID, even if subscriber, amount and tariff match. No automatic top-up, recurring debit beyond the allowance, usage dispute adjudication or clawback is introduced.

## Anchoring

v1 uses one anchor per feed, never a multi-feed batch. An anchor commits `{chain_id, stream_id, generation, head_sequence, head_message_hash, index_root}`. The chain adds its block height and timestamp; an owner cannot supply them. An anchor becomes usable only after the containing block is final.

The index covers publisher sequences 1 through `head_sequence`, including prior generations. For message M:

```text
leaf(M) = SHA256(0x00 || canonical_cbor([
  M.chain_id, M.stream_id, M.generation, M.sequence, envelope_hash(M)]))
node(L, R) = SHA256(0x01 || L || R)
```

A singleton root is its leaf. For n > 1, split the ordered leaves at the largest power of two strictly below n, recursively hash left/right roots, then apply `node`. Empty anchors are invalid. Proofs contain a zero-based leaf index (`sequence - 1`), tree size (`head_sequence`) and bottom-up sibling hashes, at most 64; directions follow the recursive split and index, with no caller-chosen direction bits. Reject unused/missing siblings, wrong size, or out-of-range index. No duplicate-last-leaf padding is used.

`commit_anchor` includes the full signed head envelope and its index proof. The actor verifies owner, chain/feed/generation, registered signing key, head leaf/root and strictly increasing head sequence. An identical latest commitment is a no-op and MUST NOT refresh retention or timestamps; a different commitment at the same or a lower sequence rejects. A previously finalized prefix is expected to be preserved by the publisher, but v1 does **not** validate append-only consistency between roots. Membership and head checks do not prove absence of equivocation.

Distinct anchors MUST be separated by at least 600,000 ms of committed block time; earlier commits reject with `ANCHOR_TOO_SOON`. The first anchor has no spacing restriction, and identical retries remain no-ops. The service records the committed timestamp it observes at the first unanchored append and MUST target inclusion no later than the first block whose committed time reaches that timestamp plus 600,000 ms. This is a provider availability obligation, not an on-chain proof of append time or a guarantee of transaction inclusion. The service submits in the available slot, respecting spacing from the previous anchor. Cadence and retention both use committed milliseconds, never an assumed blocks-per-second conversion. An idle feed needs no empty heartbeat. Unanchored messages cannot satisfy FINALIZED; elapsed cadence does not finalize them.

Once an anchor is finalized, retain it and its historical verification keys until committed block time exceeds its containing block timestamp plus 604,800,000 ms. The containing block timestamp is the retention origin; wall-clock finality observation does not extend it. Once older, they may be pruned and on-chain historical verification returns `PROOF_EXPIRED`. The service MUST keep bodies and inclusion proofs for each newly covered interval `(previous_head_sequence, head_sequence]` for seven days after its first covering anchor finalizes, using CBFS when CBQS retention would remove them sooner. Later cumulative roots do not renew old-body retention. Verification requires both a retained anchor and generation key; a newer root cannot recover a pruned key/body. A requested capacity beyond 1,024 simultaneously live anchors rejects rather than deleting evidence early. At the minimum spacing, the inclusive seven-day window contains at most `floor(604,800,000 / 600,000) + 1 = 1,009` anchors, below the 1,024-row cap. Prune expired rows before admission; faster blocks cannot increase this count. The window supports verification/recovery, not an implied slashing, dispute or compensation system.

## Actor Activation and Evidence

Every wake subscription explicitly chooses `PROVISIONAL` or `FINALIZED`; there is no default.

- PROVISIONAL accepts a valid current publisher signature. The publisher may have signed a conflicting message at that sequence.
- For ANCHORED, FINALIZED requires membership in a retained finalized anchor and the relevant registered key. It is a publisher-honesty checkpoint, not hard equivocation protection.
- For CONSENSUS, FINALIZED means the publishing block is final. Callbacks run no earlier than the next block using committed parent state.

An irreversible action requiring protection against a dishonest publisher MUST use CONSENSUS in v1. Calling an ANCHORED policy FINALIZED MUST NOT be presented as that guarantee. Confidential plaintext/body-dependent actions use application compute; v1 actor callbacks accept only OPEN, INLINE bodies, verify their hash, and cannot resolve CBFS handles.

## Predicates and Wake

CONSENSUS subscription admission MUST reject `max_cycles > 100,000`; ANCHORED permits up to 1,000,000. Both cap callback cells at 20,000. The smaller CONSENSUS cap reserves headroom in the existing 550,000-cycle timer for evidence and bookkeeping; it grants no timer-limit increase. Governed timer/auction limits and remaining page budget still apply.

An authenticated actor calls `subscribe(guard, callback, evidence_policy, max_cycles, max_cells, wake_allowance_wei)`; callback is a nonempty UTF-8 selector of at most 64 bytes. `wake_allowance_wei` is a cumulative authorization to reserve/debit callback gas from that actor, not a per-fire unlimited authorization. Each record has a never-reused subscription ID, creation sequence, last attempted sequence, status and remaining allowance. Creation sequence is the maximum of the committed feed head and admitted-wake watermark at subscription time; provisional unpublished history cannot be inferred from that value. Deposits are not a second funding mode. `set_wake_allowance` replaces the remaining authorized amount and cannot erase consumption watermarks.

`submit_wake(message, proof?)` validates feed, current generation, signature, body, timestamp and required evidence; it creates at most one job per increasing message sequence. A feed-wide admitted-sequence watermark prevents replay after job deletion. Submitters SHOULD submit in publisher order: older sequences after a later admitted job reject; missing nonmatching messages need not be submitted. A job stores the message, proof reference, subscription-ID cutoff, next scan cursor and expiry height; it does not create one row per recipient. Duplicate admission is a no-op with no callback debit.

`dispatch_wake(job_id)` scans subscriptions in ascending subscriber-address order, at most 64 per call, dispatching at most 32 callbacks and respecting the feed's 64/block limit. A subscription must still be live, have ID no greater than the job cutoff, have creation sequence strictly below that message sequence, match its guard, satisfy its evidence policy, and have a lower attempt watermark. New/recreated subscriptions do not join old jobs. Unsubscribe takes effect before any later dispatch. Jobs are serviced in admission order; the provider may continue pages in later transactions until expiry. When four jobs are pending, admission returns `WAKE_BUSY`; it never grows the queue or implicitly drops an older job. In CONSENSUS this backpressure rejects the entire `publish`, including ring/head updates; publication throughput is therefore bounded by fan-out. For example, 1,024 subscriptions can require at least 16 scan transactions per job, and the one-block timer does not promise one accepted publish per block. Expired jobs may be removed before admission, without clearing watermarks. ANCHORED append is independent: a full wake queue rejects wake admission, not transport append.

The submitter (or the provider-funded timer for CONSENSUS) pays transaction, evidence verification, scan and guard-check costs, including valid shared work, invalid submissions and duplicates. Each subscriber pays only the lane-adjusted block basefee for its callback's actual cycles/cells under CIP-3, after an atomic reservation of `max_cycles × effective_cycle_basefee + max_cells × effective_cell_basefee`. Use the same effective rates and integer arithmetic as that transaction's lane, including the timer lane when applicable. `effective_cycle_basefee` and `effective_cell_basefee` are the execution engine's CIP-3 lane-adjusted cycle and cell prices; the timer cycle price comes from its timer basefee. For each resource, `effective_basefee = floor(basefee × lane_multiplier_ppm / 1,000,000)` using wide integer intermediates. [CIP-43](https://github.com/cowboyinc/cowboy/blob/main/docs/cips/cip-43-access-meter.md) defines the independent Access resource, charged for logical state access; it is not a memory-cell charge. Subscriber reservation and actual debit use cycles/cells only, while the transaction payer pays `charged_accesses × effective_access_basefee` and all applicable tips. The subscriber does not authorize a submitter-selected priority fee: all priority fees, all Access fees (including callback Access), and shared execution costs belong to the submitter or provider-funded timer. Split payment sources at settlement; do not subtract callback resource consumption from transaction/block meters, charge either basefee twice, or reimburse the submitter after charging it for subscriber work. There is no ambiguous shared-fee reimbursement. If balance or allowance is insufficient, mark that message skipped for this subscription and continue; do not debit or block others. A subscriber may authorize more funds for future messages, but v1 does not replay the skipped message.

Before invocation, persist the attempt watermark and reserve gas. Callback application writes use an isolated rollback boundary: failure reverts those writes but does not revert the consumed attempt or actual gas charge. Release unused gas reservation exactly once. On success or failure, decrease the remaining allowance by actual charged gas. A failed callback is never automatically retried. Generic `send_message` behavior is insufficient unless it implements this isolation and accounting (including the COW-2884 cells boundary).

The full callback caps and bounded shared bookkeeping MUST fit the remaining transaction/timer authority before invocation. Otherwise, leave that recipient pending at the current cursor, without consuming its attempt or charging its allowance. CIP-5 per-fire limits remain in force: configuring callback caps above the available timer budget does not grant a larger timer budget. v1 code replacement must run in a separate transaction; a callback that attempts to replace actor code fails atomically with its other application writes.

A dispatch page emits one canonical-CBOR `WakePage` record, avoiding one event per recipient: `{job_id: u64, complete: bool, scanned: u64, callbacks: u64, outcomes: [[subscriber: bytes20, subscription_id: u64, status: u8, cycles: u64, cells: u64, charged: u64], ...]}`. Status is `1` for callback success, `2` for callback failure, and `3` for insufficient balance/allowance. Skips carry zero resource/charge values. Nonmatching rows and budget-blocked recipients have no outcome entry. At most 64 tuples are emitted; at most 32 describe actual callbacks. This bounded record fits the existing 4-KiB event payload limit. It reports application outcomes; finalized fee records remain authoritative if enclosing execution fails.

A job expires after 256 blocks; remaining recipients are skipped and the job is deleted, while feed and subscription watermarks remain. Publisher cadence, funding, queue capacity and transaction inclusion bound availability: v1 does not promise that every matching actor executes. Providers inspect emitted job/wake outcomes and MUST NOT advertise guaranteed fan-out. Memory-based predicates and automatic retries are outside v1.

## Delivery and retention

Each ANCHORED feed uses lane 0 of its dedicated CBQS stream. Provider grants restrict appends to its authorized publisher path. Watchtower maps each publisher sequence to the earliest CBQS record position carrying that exact envelope hash; identical append retries may occupy later positions and are deduplicated. A different envelope at the same publisher sequence is `PUBLISHER_CONFLICT`. CBQS record positions are not assumed equal to publisher sequences.

The service stores the mapping with its durable publishing/index state. `read_since(sequence, limit)` is exclusive of sequence, returns verified envelopes in publisher order, an opaque CBQS continuation cursor and `next_sequence`, and enforces the page bounds above. `sequence=0` requests from genesis; `tail` is a separate explicit operation that snapshots the current head once. Missing sequences stop contiguous replay with `GAP_DETECTED`; they cannot be silently skipped. Transport reconnect resumes the returned cursor and deduplicates already accepted envelopes.

Use CIP-39 `retention_ms` and `retained_bytes_limit`; there is no Watchtower record-count setting for the hot window. Default hot settings are one day and 256 MiB, subject to provider admission. These are eviction limits, not a promise to retain all traffic for one day. Before eviction could remove a body/proof under the seven-day anchor obligation, the service MUST archive it to CBFS; if archive durability cannot be maintained, stop appending. Client replay consults the archive when below the CBQS floor. Beyond retained history return `CURSOR_TOO_OLD`, never fabricate continuity. CONSENSUS reads use the bounded actor ring; overwritten bodies are unavailable even though their block was final.

## Interfaces and outcomes

Methods below specify logical actor/service calls, not allocated native opcodes or already shipped CLI commands. CBOR call arguments use the types above. Unknown fields reject. Mutations are atomic; failure returns `{error: code}` without partial writes. Successful mutations return the affected ID/counters; reads return committed records or `NOT_FOUND`.

| Surface | Authorization and result |
| --- | --- |
| Actor `init(config)` | Owner; once only; returns config |
| Actor `publish(message)` | Owner, CONSENSUS only; validates continuity/body/time, appends ring and wake job atomically; returns sequence/hash |
| Service `append(message)` | Provider publishing authority; ANCHORED only; idempotently returns sequence/hash/cursor |
| Actor `restart_dispatch_timer()` | CONSENSUS owner only; returns the live or newly rearmed timer ID |
| Actor `commit_anchor(head, root, head_proof)` | Owner; returns committed anchor or identical latest commitment |
| Actor `rotate_keys(publisher_key, usage_key)` | Owner; finalized boundary, empty jobs and available history capacity; returns new generation/effective sequence |
| Actor `authorize_usage(limit, expires_at_ms)` / `revoke_usage()` | Authenticated subscriber; returns new authorization ID / revoked ID |
| Actor `settle_usage(receipt, signature)` | Permissionless valid receipt; returns cumulative settled bytes/total and this debit |
| Actor `subscribe(...)` / `set_wake_allowance(amount)` / `unsubscribe()` | Authenticated recipient actor; returns subscription ID/status; duplicate active subscribe rejects |
| Actor `submit_wake(message, proof?)` / `dispatch_wake(job_id)` | Permissionless; submitter funds shared work; returns job ID or next cursor/completion |
| Actor `get_config`, `get_head`, `get_anchor(sequence)`, `get_generation(id)`, `get_usage(subscriber)`, `get_subscription(actor)` | Read-only; ANCHORED head is the anchored head, not an asserted live network head |
| Actor `get_since(sequence, limit)` | CONSENSUS ring read; bounded; below floor returns CURSOR_TOO_OLD |
| Service `read_since(sequence, limit)` / `tail` / `get_content_key(generation, epoch, authorization_id)` | OPEN reads need no billing authorization; paid delivery requires authenticated live customer authorization; historical key access restricted to its authorized generation and retained bodies |
| Actor `prune()` | Permissionless bounded cleanup, at most 64 eligible rows; never clears ID/wake watermarks |

Events are `FeedInitialized(stream_id)`, `MessagePublished(stream_id,generation,sequence,hash)` (CONSENSUS only), `AnchorCommitted(stream_id,generation,sequence,hash,index_root)`, `KeysRotated(stream_id,generation,effective_sequence)`, `UsageAuthorized(stream_id,subscriber,id,limit,expiry)`, `UsageRevoked(stream_id,subscriber,id)`, `UsageSettled(stream_id,subscriber,id,cumulative_bytes,cumulative_total,debit)`, `SubscriptionChanged(stream_id,subscriber,id,status)`, `WakeAdmitted(stream_id,sequence,job_id)`, `WakePage(job_id,complete,scanned,callbacks,outcomes)`, and `WakeJobClosed(stream_id,job_id,reason)`. WakePage status codes and tuple order are defined in Predicates and Wake; its authenticated emitting Feed identifies the stream, so no extra stream field is encoded. Job reasons are `COMPLETED` or `EXPIRED`. Canonical event bodies use maps with exactly the listed fields and the same scalar types as the records. The nested `WakePage.outcomes` array deliberately uses the bounded six-element tuples specified above. Off-chain append does not claim to emit an on-chain event per message.

Errors are `ALREADY_INITIALIZED`, `UNAUTHORIZED`, `INVALID_FORMAT`, `INVALID_SIGNATURE`, `WRONG_CLASS`, `SEQUENCE_MISMATCH`, `PUBLISHER_CONFLICT`, `INVALID_PROOF`, `PROOF_REQUIRED`, `PROOF_EXPIRED`, `GENERATION_UNFINALIZED`, `GUARD_INVALID`, `CAPACITY_REACHED`, `ANCHOR_TOO_SOON`, `WAKE_BUSY`, `STALE_MESSAGE`, `INSUFFICIENT_BALANCE`, `ALLOWANCE_EXCEEDED`, `AUTHORIZATION_EXPIRED`, `CURSOR_TOO_OLD`, `GAP_DETECTED`, `DECRYPTION_FAILED`, `NOT_FOUND`, `PROVIDER_UNAVAILABLE`. Type/size/overflow errors use INVALID_FORMAT; authorization ID/generation mismatches use UNAUTHORIZED. No failure authorizes implicit re-payment or a fallback to retired APIs.

## Replacement and related specifications

The coordinated release retires Stream Key Manager at **0x0D** and permanently reserves its address. It MUST NOT reassign it. Remove CIP-7 `register_content_keys`, `acquire_epoch_access`, WrappedContentKey, account-key registration and per-epoch SealRequest delivery from node/PVM/CLI/SDK and CBSS's CIP-7-specific service path. There is no migration or mixed-format decoder for old devnet data. The retired implementation is not evidence that this replacement is deployed.

CIP-24 account-secret and CIP-9 volume-key services remain in scope of their own specifications; their shared cryptography MUST NOT be removed merely because CIP-7 leaves the committee delivery path. CIP-18 PaymentGate epochs remain a separate HTTP/MCP entitlement product; they are not Watchtower epoch-key purchases. CIP-28 purchase signatures are not usage receipts. Existing Watchtower gallery code and SDK methods using the old scheme are historical examples and MUST NOT be advertised as replacement conformance.

## Deferred to v2

- Consensus enforcement of append-only anchor consistency and hard ANCHORED equivocation protection.
- Actor-side CBFS body witnesses, keyless-actor paid-key delegation and encrypted-body callbacks.
- Multi-feed batch anchors, provider failover/handoff, class changes and mutable tariffs/scales.
- Automatic ingestion/signing bridge, stateful predicates and callback retries.
- Multiple usage meters, deposit funding, automatic refunds, dispute adjudication and compensation.

## Implementation and acceptance

The architecture and v1 contracts above are fixed by this draft; deployment requires implementation and review. No product feature above is declared implemented by this document. Shared vectors in `docs/cips/vectors/cip7-watchtower-v1.json` pin CBOR signing bytes, signatures, message hashes, Merkle proofs, key derivation and guard cases. All language implementations MUST consume them and reject malformed/noncanonical alternatives.

Acceptance MUST exercise duplicate append/restart, missing/conflicting messages, CBFS hash mismatch, cross-chain/generation replay, key rotation, revoked/recreated authorization, repeated cumulative receipt, insufficient funds with atomic rollback, ring/key/anchor capacity (including more than seven days of continuous anchoring at minimum spacing and faster block cadence), timestamp bounds, full-queue publish rollback and retry, queue expiry, callback failure isolation, unsubscribe races, and sustained state growth. A callback must not charge the publisher for recipient cycles/cells basefees. Changing a submitter-selected priority fee must not change the subscriber debit or allowance consumption; Access and all priority fees remain with the submitter/provider. Verify exact fee conservation and unused-reservation release on callback success, callback failure and enclosing transaction failure. Test at least one authenticated paid delivery and one finalized CONSENSUS wake through real components; unit vectors alone do not establish deployment readiness.

## Copyright

CC0 1.0 Universal.
