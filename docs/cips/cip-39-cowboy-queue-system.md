---
title: "CIP-39: Cowboy Queue System"
description: A durable off-chain messaging service for cross-runner coordination, with an on-chain registry that owns identity, authorization, and billing, and a broker data plane that runs independently of the chain.
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-07-27
  **Revised:** 2026-08-24 — document version 2. Version 1 is superseded in full; §18 states how a deployment activates, and how genesis rejects version-1 governance keys without enumerating them.
  **Requires:** CIP-3 (fee handling), CIP-12 (governance), CIP-31 (§4, the Platform Fee Account rule §6 cites)
  **Related:** CIP-2 and CIP-10 (the runner workloads that are CBQS clients), CIP-7 (Watchtower, the actor-native stream protocol), CIP-42 (`/statusz`)
</Note>

## Abstract

This proposal introduces the **Cowboy Queue System (CBQS)**, a durable
off-chain messaging service for workloads running on different Cowboy Runners.
CBQS provides one ordered stream primitive from which applications construct
work queues, publish/subscribe, request/reply, and replayable event logs.
Broker-side **lanes** let many related rooms or documents share one
chain-created stream.

The Cowboy chain is a **governance and registry plane**. It records who owns a
stream, which provider serves it, which key may authorize access to it, and
whether its owner has paid. A new off-chain service, `cbqsd`, is the data
plane: it stores records, manages consumer groups and delivery leases, pushes
records to connected clients, and commits batches durably.

The two planes are deliberately decoupled. No chain transaction is required to
append, deliver, acknowledge, or replay a message, and no chain round trip
occurs on any message path. A broker serves from a periodically refreshed
registry snapshot and keeps serving while the chain is unreachable; chain
authority is applied within a bounded, stated lag rather than per request.

Message payloads are opaque to the broker. CBQS does not define a key
distribution mechanism; applications that need confidentiality encrypt payloads
before appending them.

## Background

Cowboy actors and actor-to-actor messages are consensus execution. They are the
correct path for finality-bearing application state, but not for
high-frequency coordination between off-chain workloads.

Cowboy Runners execute jobs and persistent workloads away from consensus, but
there is no shared messaging backplane between them. A coordinator that talks
to several specialist agents must otherwise implement persistence, retry,
fan-out, cursors, leases, reconnection, authorization, and crash recovery as
application infrastructure.

CBQS is that backplane. It is an ordinary message broker whose tenancy,
authorization, and billing happen to be recorded on a blockchain, and it is
specified so that the broker's steady-state behaviour is that of an ordinary
message broker.

## Goal

CBQS MUST provide:

1. cross-runner message exchange with no consensus write and no chain read on
   the data path;
2. ordered replay, and at-least-once work delivery in which consumers compete
   for records rather than receive them in order;
3. push delivery with bounded, explicit backpressure;
4. owner-controlled authorization and revocation through chain authority,
   applied within a bounded lag;
5. durable commit before acknowledgement, with a signed provider commitment
   over each committed batch that a party holding that batch can verify;
6. cheap logical room or document creation inside one stream;
7. continued data-plane service for established sessions while the chain is
   unavailable — bounded by `cbqs.max_snapshot_age_blocks` for opening new
   sessions and by grant expiry for holding open ones (§3).

Payload confidentiality as a platform service, key distribution and rotation,
exactly-once application effects, communication-graph secrecy, consensus
publication, semantic routing, distributed transactions, and generic
Redis-style key/value or lock operations are **not** goals.

The following are outside this document and require follow-up CIPs: an
async-durable delivery class for repairable logs, per-lane cryptographic
verification, provider reassignment, provider staking or slashing, shared
durable cursors, scheduled delivery, capacity-priced or tiered billing,
platform-managed encryption, CBFS archival integration, and transport bindings
other than the one in §11.

## Specification

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHOULD**,
**SHOULD NOT**, and **MAY** in this document are to be interpreted as described
in RFC 2119.

Every normative requirement in this document is stated so that it can be
observed from outside the implementation it constrains: from chain state, from
canonical bytes on the wire, or from a broker response. §19 states the
conformance surface, and enumerates the requirements that are **not**
externally observable, which are stated as operator obligations rather than as
protocol guarantees.

### 1. Terms

| Term | Definition |
| ---- | ---------- |
| **Stream** | An ordered append-only sequence of records with bounded retention. |
| **Lane** | A broker-side filtered view inside a stream, identified by a broker-assigned integer. Lanes inherit the parent stream's provider, billing, and retention. |
| **Provider** | A registered operator of `cbqsd`. |
| **Consumer group** | Broker-side delivery state that lets one or more consumers compete for each record. |
| **CBQS account** | One per `(owner, provider)` pair. Holds the prepaid balance and the entire billing state for the streams that provider serves for that owner. |
| **Registry snapshot** | The broker's most recent read of Stream Registry state at a finalized height, used for authorization and billing decisions until it is refreshed. |
| **Authorization generation** | A chain-controlled counter embedded in every StreamGrant. A bump invalidates all older grants. |
| **Checkpoint** | A provider-signed commitment to one contiguous range of durably committed records. |

### 2. Trust model

The chain is authoritative for stream existence, ownership, provider
assignment, admin key, authorization generation, and the owner's prepaid
balance and solvency. `cbqsd` is authoritative for the records, lanes, groups,
and delivery state it has accepted.

Payloads are opaque bytes to the broker. The broker sees, and this document
does not hide: which workloads connect, stream and lane and group identifiers,
message sizes, timing, traffic volume, and delivery state. The chain
additionally exposes stream existence, owner, provider, billing activity, and
authorization-generation changes.

A provider can censor, delay, or destroy data. Checkpoints make some
misbehaviour detectable after the fact; they do not provide availability, and
v2 has no on-chain dispute, staking, or slashing path. An application whose
data must survive its provider MUST maintain its own durable copy — an
obligation on the application, not on any party this document's conformance
surface can observe, which is why §19 item J names it among the exceptions.

Applications that require payload confidentiality encrypt before appending.
The broker never receives a key and never inspects a payload, so
confidentiality is exactly as strong as the application's own key management.

### 3. Architecture

```text
        CHAIN (governance + registry plane)
   provider records, stream ownership, admin keys,
   authorization generations, prepaid accounts,
   governance parameters
                 |
                 |  periodic finalized snapshot read
                 v
        +-----------------+          +-------------------------+
        |      cbqsd      | <======> |   runner workloads      |
        | records, lanes, |  push +  |   producers + consumers |
        | groups, leases, |  credit  |   (StreamGrant auth)    |
        | checkpoints     |          |                         |
        +-----------------+          +-------------------------+
```

The chain MUST NOT receive message payloads, lane operations, group operations,
delivery leases, acknowledgements, or cursor advances.

The Stream Registry system actor is allocated at `0x17`. CBQS uses one
top-level system-instruction opcode:

```text
SYS_CBQS = 218
```

The payload is a bounded, versioned `CbqsInstructionV2` (§5).

**Data-plane independence.** A broker MUST serve appends, deliveries,
acknowledgements, and replays from its registry snapshot without contacting the
chain. It MUST attempt a refresh at least every `cbqs.snapshot_refresh_ms`, and
when a refresh fails it MUST continue serving from the snapshot it has.

Two bounds keep that independence from becoming unbounded authority:

- **A new session requires a snapshot no older than
  `cbqs.max_snapshot_age_blocks`.** Past that age the broker MUST refuse
  `OpenSession` with `ChainStateStale`.
- **Every grant expires.** `cbqs.max_grant_ttl_ms` caps the lifetime an owner
  can issue, and §7 requires a broker to terminate a session when its grant
  expires. That expiry, not the snapshot age, is the guaranteed bound on a
  revoked or unpaid holder, because it is the one bound that holds even against
  a broker that never refreshes.

The snapshot-age bound is enforced by the broker on itself, so a client cannot
observe it from protocol messages — §19 item B covers the closest observable
proxy — whether the `snapshot_height` a broker reports is truthful — and §19
item 4 makes the refusal itself a conformance test, which a harness that
controls the chain can run and a wire client cannot. **Grant expiry is
different**: it elapses without the broker's cooperation, and a client observes
it directly by continuing to send past `expires_at_ms` and seeing whether it is
served. That is why the revocation argument rests on expiry and not on the
snapshot bound, and why §19 item 4 makes it a conformance test.

**The snapshot.** A broker obtains the snapshot by reading finalized Stream
Registry state from a Cowboy node, and MUST evaluate every decision for one
request from one snapshot rather than combining values read at different
heights. A snapshot MUST carry, for each stream it covers: the
`StreamRecordV2`, the `CbqsAccountV2` for `(stream.owner, stream.provider)`
when one exists, the assigned `ProviderRecordV2`, the §15 parameter values, and
the finalized height it was taken at, which this document calls `H_snapshot`. A
`Closed` stream may have no account record — §4.3 deletes an empty one — and
needs none, because §7 refuses a session for it on status alone. The node RPC
surface that serves it is an implementation matter; the content above and the
atomicity are normative.

Clients connect directly to a provider endpoint. The Cowboy Gateway is not on
the message data path.

### 4. Chain objects

Every canonical CBQS object defined with a `version` field carries
`version: u8 = 2` as its first field, and a decoder MUST reject any other
value, including the version-1 bytes of the superseded document. The objects
that carry it are every chain record (§4.1–§4.3), every broker record that
outlives a connection (§8, §9.2, §13), the record header inside a signed digest
(§9), and every signed object (§7, §10). Instruction payloads (§5) and
transport frames (§11.1) are versioned by their envelope — the
`CbqsInstructionV2` version byte and the frame's `version: u8 = 2` — and the
structs carried inside them keep whatever version byte their own definition
gives them.

Public keys carry an explicit algorithm tag. v2 defines exactly one:

```text
SigningPublicKey = alg_tag_u8 || key_bytes    // Ed25519 = 1, 32 key bytes
```

A **decoder** MUST reject a `SigningPublicKey` whose `alg_tag` is not `1`. The
tag is an enum tag, so it fails at decode alongside the version byte and the
instruction tag rather than at admission — which settles its charge, because a
payload that does not decode is not a CBQS charge at all (§15.1). The key-byte
count needs no rule: the type is a fixed 33-byte string, so a decoder that read
33 bytes has 32 key bytes by construction.

Consensus admission performs **no curve arithmetic**: a
point decompression and subgroup check would be unmetered work on four
instructions whose gas vectors price no cryptography, and the only party harmed
by a malformed key is the owner who installed it, whose stream becomes
ungrantable at its own expense. Strict point validation happens where the key
is used, at verification.

Every Ed25519 verification defined by this document — `StreamGrantV2` (§7),
`SessionProofV2` (§7), `CheckpointReceiptV2` (§10) — uses pure Ed25519 (RFC
8032) with a 32-byte public key and a 64-byte signature, verified with strict
semantics: canonical point and scalar encodings, and rejection of weak or
small-order public keys and `R` values. A verifier MUST verify each signature
individually and MUST NOT accept a batch-verification result. No v2 chain
instruction carries a signature, so the addressee is the broker and any
off-chain verifier, not consensus.

**Canonical encoding.** All CBQS canonical bytes are emitted by
`cowboy-protocol-codec::cbqs`. Integers are unsigned big-endian of their
declared width. Fixed byte strings and addresses are their raw fixed-width
bytes. A variable byte string is `u32 length || bytes`. A `bool` is one byte,
`0` for false and `1` for true; any other value is invalid. An option is `0`
for absent or `1 || value` for present; any other value is invalid. A vector is
`u32 count || elements`.

**An enum tag is one byte** — the numeric tag listed at that definition —
followed by the variant's fields; unknown tags are invalid.
**`SigningPublicKey` is a fixed 33-byte string**, the one-byte algorithm tag
followed by 32 key bytes, and **`Signature` is a fixed 64-byte string**;
neither is length-prefixed. `SigningPublicKey`'s rule exists because it enters
the one signed preimage that carries a key — `StreamGrantV2`'s
`holder_signing_key` — which is the one place no pinned artifact can
catch a disagreement; `Signature`'s exists because it is decoded from every
frame that carries a signed object, and no preimage contains it.

Struct fields appear in the order listed here. Field names and map keys never
enter canonical bytes. A decoder handed a self-delimiting input — a §11.1
frame, or a stored record — MUST consume it exactly: a well-formed body
followed by any unconsumed byte violates this encoding. A struct decoded inline
from a larger reader is bounded by its own fields, not by the end of the input.

Every option is encoded with its explicit presence tag. An implementation MUST
NOT encode an absent option as a fixed-width sentinel: two distinct values MUST
NOT share canonical bytes.

**Reserved system addresses.** Several rules below reject an instruction whose
owner, provider, payer, or account is a **reserved system address**. For this
document that means the zero address `0x00…00` and every address in `0x00…01`
through `0x00…FF` — the low range from which Cowboy allocates system actors,
and which contains the two this document names (`0x17`, `0x18`) and the zero
address that §4.1 and §6.3 both burn to. The set is enumerated rather than
cited so that a validator can be built from this document alone, and the zero
address is inside it because two handlers read and write it separately from a
caller's own native account: `CreateStream` from the owner's and
`RegisterProvider` from the caller's (§15.1). A caller at `0x00…00` would alias
the two slots and net a burn against its own credit, so nothing would be
burned.

**Distinct accounts.** Several handlers move value between accounts named
separately in §15.1. Those accounts MUST be distinct addresses, and three
rejections enforce it: `CreateStream` when `provider == owner` or when `owner`
is a reserved system address; `TopUpAccount` when the payer is the account's
provider, or when the payer or owner is a reserved system address;
and `RegisterProvider` when the caller is a reserved system address (§4.1),
which is what keeps its own two native-account slots distinct.

The rejection names the payer and the owner but not the provider, because a
reserved provider is already unreachable: §4.3 rejects a `TopUpAccount` naming
a provider with no record, and §4.1 forbids registering at a reserved address,
so no fixture can isolate a reserved-provider rejection. The reserved-address
half is not redundant with §4.1's rule that a provider cannot register at one.
An owner or payer of `0x17` would alias the custody account, so the creation
charge would be debited from pooled custody or a credit would net to zero
against it, breaking the invariant that custody equals the sum of balances;
`0x18` aliases the Platform Fee Account the same way. Both require a reserved
address to originate an instruction, which this document does not otherwise
exclude — so the enumeration covers it rather than resting on who can sign. An
earlier draft instead defined what an aliased write means, twice, and got it
wrong both times — the second attempt cited an ordering §15.1 does not define,
under which the later write clobbers the earlier and either mints or destroys
the creation charge. A rule that cannot be stated unambiguously in two attempts
is better replaced by making its subject unreachable. An operator that wants to
serve its own streams uses a second address to own them, which costs one
address and removes a consensus divergence.

**Signing registry.** For every signed object the named codec function returns
`domain_separator || canonical(all fields except signature)`, and the signer
signs the Keccak-256 digest of those bytes. This table is the complete v2
registry; an implementation MUST NOT reconstruct a preimage from JSON, from
transport framing, or from locally repeated field concatenation.

| Signed object | Domain separator | Sole signing-bytes function |
| ------------- | ---------------- | --------------------------- |
| `StreamGrantV2` | `cbqs/stream-grant/v2` | `stream_grant_signing_bytes_v2` |
| `SessionProofV2` | `cbqs/session/v2` | `session_proof_signing_bytes_v2` |
| `CheckpointReceiptV2` | `cbqs/checkpoint-receipt/v2` | `checkpoint_signing_bytes_v2` |

The three domains are distinct and none is a prefix of another, so a signature
valid over one object type is invalid over every other.

#### 4.1 ProviderRecordV2

```text
ProviderRecordV2 {
  version:            u8,      // 2
  chain_instance_id:  Bytes32,
  provider:           Address,
  signing_key:                SigningPublicKey,
  signing_key_since:          u64,   // finalized height at which signing_key took effect
  previous_signing_key:       Option<SigningPublicKey>,
  previous_signing_key_since: u64,   // lower bound of the previous key's interval
  endpoints:          Vec<Bytes>,     // each a wss:// URI, printable ASCII
  accepts_new:        bool,
  max_streams:        u64,
  assigned_streams:   u64,
  metadata_hash:      Bytes32,
}
```

**A provider record has no status field.** `accepts_new = false` refuses new
streams without disturbing existing ones, which is the whole of what a
`Draining` state expressed. A `Deregistered` state would add one thing an
implementation could get wrong and nothing it could act on: version 1 gated it
on `assigned_streams == 0`, which any single owner could hold above zero
indefinitely by keeping one stream funded, so deregistration was never a right
a provider actually held. A provider leaves by clearing `accepts_new` and
letting its streams close.

That is a real limitation and not a disguised right: **an owner who keeps
paying cannot be exited from.** `assigned_streams` stays above zero for as long
as one funded stream names the provider, so `max_streams` cannot be lowered
past it and the record is permanent in practice. This is deliberate — a paying
customer's service is not unilaterally terminable — and it is the same property
version 1 had, stated here instead of dressed as a status field that suggested
otherwise. A provider that must stop serving a specific paying owner has one
protocol move: stop accepting new streams, and negotiate the rest off chain.

`RegisterProvider` MUST be rejected when a record already exists for the
caller. Re-registration would otherwise reset `assigned_streams` to zero while
live streams still name the provider, and reset `signing_key_since`, voiding
every checkpoint the provider had signed. Everything mutable is changed through
`UpdateProvider`.

An endpoint is a URI and nothing else — v2 defines one transport, so a
transport tag would be a field with one legal value. Consensus admission
validates each endpoint as follows, rejecting the instruction on the first
failure:

1. the endpoint list is nonempty and at most `cbqs.max_provider_endpoints`
   long;
2. each URI is at most `cbqs.max_provider_endpoint_uri_bytes` bytes and begins
   with the byte string `wss://`, compared byte-exactly; and
3. every byte is in the **closed** range `0x21`–`0x7E` inclusive — printable
   ASCII, excluding space and every control byte.

Rule 2's length bound is belt-and-braces.
`cbqs.max_provider_endpoint_uri_bytes` has Default equal to Maximum and §15
caps the same length at decode at that value, so no URI that survives decode
can fail it. The handler's read of the parameter is real and reserved (§15.1),
and the rule is stated for a reader building admission from this section alone
— but no fixture can isolate it, the way §5 says of its own two unreachable
rejections.

```text
ACCEPT   wss://cbqs.example/ws
ACCEPT   wss://cbqs.example:8443/ws
ACCEPT   wss://[2606:4700:4700::1111]/ws
ACCEPT   wss://198.51.100.7:8443/ws
ACCEPT   wss://cbqs.example/~a!b                 (rule 3: 0x21 and 0x7E both admitted)

REJECT   https://cbqs.example/ws                 (rule 2: scheme)
REJECT   WSS://cbqs.example/ws                   (rule 2: case)
REJECT   wss://cbqs.example/a{HT}b               (rule 3: control byte)
REJECT   wss://cbqs.example/a{SP}b               (rule 3: space)
REJECT   wss://169。254。169。254/ws              (rule 3: non-ASCII)
REJECT   wss://good.example／evil/ws             (rule 3: non-ASCII)
```

The vectors above are **normative in full**: a conforming implementation MUST
admit every ACCEPT and reject every REJECT. `HT` denotes a single `0x09` byte
and `SP` a single `0x20` byte, written as names because the byte itself is
invisible in this document's rendering; a conformance harness MUST substitute
the byte rather than the four literal characters, which rule 3 would otherwise
admit.

The two non-ASCII vectors are homoglyphs, and are named by codepoint because a
renderer may normalise them away: the first substitutes `U+3002` IDEOGRAPHIC
FULL STOP for each `.`, the second `U+FF0F` FULLWIDTH SOLIDUS for the `/` that
would end the authority. Rule 3 rejects both on their bytes without having to
know what they resemble, which is why it is a byte-range rule rather than a
list of confusables.

**Consensus does not parse the host, and deliberately so.** An earlier draft
added a fourth rule: a grammar for the authority — canonical dotted-quad, RFC
5952 lowercase IPv6 inside brackets, DNS labels whose last label begins with a
letter, and a port with no leading zero and not 443. It was written to stop one
registered byte string from meaning two things to two readers, one parsing the
URI and one scanning its bytes. It is deleted, for two reasons that point the
same way.

It bought nothing. Consensus stores this field and never interprets it: no rule
compares two endpoints, and no handler resolves one. The only reader that acts
on it is a client deciding where to dial, and §11.4 governs that decision at
the destination it dials, whether that came from a resolution or straight out
of the URI. Walk the vectors the grammar existed for and both readings land in
the same place — `wss://0251.0376.0251.0376/ws` is `169.254.169.254` to a
parser and an ordinary name to a scanner, and §11.4 rejects the first outright
and rejects whatever the second resolves to;
`wss://good.example@169.254.169.254/ws` sends a parser to a link-local address
§11.4 refuses and a scanner to `good.example`, which is safe. Two clients can
disagree about which host a provider meant, and neither can be walked into a
private network.

It also cost something real. "The canonical lowercase form of RFC 5952" is not
a single form: RFC 5952 §5 leaves the mixed hex/dot-decimal notation for
IPv4-mapped addresses a recommendation, so the two mainstream standard-library
canonicalisers disagree — Rust renders `::ffff:c000:201` as `::ffff:192.0.2.1`
and Python renders `::ffff:192.0.2.1` as `::ffff:c000:201`. A validator built
on either honestly implements the sentence and admits an instruction the other
rejects. That is an admission fork on `RegisterProvider` and `UpdateProvider`,
reachable by anyone who can pay the registration charge, and no vector in the
block above would have caught it. Replacing the citation with a hand-written
IPv6 grammar would have closed that instance while leaving every validator
obliged to implement one; deleting the rule closes the class.

What is given up is stated plainly: consensus no longer guarantees that a
registered endpoint is syntactically dialable. A provider that registers
nonsense has wasted its own registration charge and is unreachable, which is
self-harm, priced, and visible.

**A URI admitted here is not a statement that the endpoint is safe to dial.** A
client MUST apply the dialing policy in §11.4 to every destination it connects
to, whether it resolved a name or read a literal out of the URI; §19 item 7
makes that testable.

The registry maintains `assigned_streams`: it increments on each `CreateStream`
that names the provider and decrements on that stream's transition to `Closed`,
the only terminal status v2 defines. `RegisterProvider` MUST set it to zero,
and no instruction may set it directly. A `CreateStream` MUST be rejected when
`accepts_new` is false or when `assigned_streams` already equals `max_streams`.
An `UpdateProvider` MUST NOT lower `max_streams` below the current
`assigned_streams`; §5's provider-side `CloseStream` is what lets a provider
reduce that floor without the cooperation of an owner who has stopped paying.

A provider MUST NOT be registered at a reserved system address, and
`RegisterProvider` MUST be rejected when the caller is one: §6 moves value
between the custody account, the provider account, and the Platform Fee Account
in one transition, and an alias between any two would make one credit overwrite
another.

Changing a provider signing key is prospective, and the registry keeps **one**
previous key. `UpdateProvider` that carries a `signing_key` MUST move the
current key into `previous_signing_key`, move `signing_key_since` into
`previous_signing_key_since`, and set `signing_key_since` to the height at
which it commits.

An `UpdateProvider` carrying a `signing_key` MUST be rejected when
`signing_key_since` already equals the height at which it would commit. Two
rotations at one height would leave
`previous_signing_key_since == signing_key_since`, an empty interval that §10's
second row can never select — so the stored previous key would resolve for
nothing and the provider's whole history would go unverifiable in one block,
which is the self-repudiation the previous key exists to prevent.

`RegisterProvider` MUST set `signing_key_since` to the height at which it
commits, `previous_signing_key` to absent, and `previous_signing_key_since` to
zero. Stating all three matters: they are consensus-visible record bytes that
no instruction body carries, so two implementations seeding them differently
produce different state roots while metering identically — and seeding
`previous_signing_key` to the current key rather than to absent would change
which row of §10's resolution table a never-rotated provider reaches.

One key of history is what makes rotation survivable in both directions. With
none, a provider could not sign at all between the rotation and its next
finalized read — §10 closes a batch every `cbqs.commit_batch_max_ms` and §3
forbids it to stop serving through a chain outage — and every receipt it had
ever signed would become uncheckable the moment it rotated, which is
self-repudiation for the price of one instruction. With unbounded history the
registry grows without bound. One key covers the rotation window and the
preceding epoch — provided rotations are at least one height apart, which the
rule above enforces; §10 states what a verifier does beyond that.

Provider registration is permissionless and creates no slashable stake, but it
is **not free**. `RegisterProvider` MUST debit
`CBQS_PROVIDER_REGISTRATION_CHARGE` from the caller's native account and credit
it to the zero address, in the same transition that writes the record, failing
without registering when the debit cannot be completed.

```text
CBQS_PROVIDER_REGISTRATION_CHARGE = 5_000_000_000     // nano-CBY
```

A provider record is permanent — v2 has no deregistration — and it is the
largest record this document defines: at the §15 defaults a maximum-size one
encodes to 8,364 bytes, or 8,397 once it carries a previous signing key —
roughly fifty times a stream record, which pays the same charge.

§6.3 states the rule that permanent registry state costs unrecoverable capital
no governance write can zero, and an unpriced, unremovable, permissionless
record was the one case that rule did not cover.

The record carries no `registered_block` and the stream record carries no
`created_at_block`. Version 1 had both and no rule read either. A consumer that
wants either
height reads the `cbqs.provider.registered` or `cbqs.stream.created` event
(§16).

#### 4.2 StreamRecordV2

```text
StreamStatusV2 = Active = 1 | Closed = 2

StreamRecordV2 {
  version:                  u8,      // 2
  chain_instance_id:        Bytes32,
  stream_id:                Bytes32,
  owner:                    Address,
  owner_nonce:              u64,
  admin_key:                SigningPublicKey,
  provider:                 Address,
  authorization_generation: u64,
  status:                   StreamStatusV2,
}
```

**A stream record carries no economic state and no configuration.** It has no
rate, no balance, no settlement height, and no suspension. All of that lives on
the owner's account (§4.3), and retention, message size, and the two rate
ceilings are broker-side settings (§13) bounded by governance parameters
(§15). What
remains on chain is exactly what the chain must arbitrate: who owns the stream,
who serves it, and which key may authorize access to it.

The stream id is:

```text
stream_id =
  keccak256(
    "cbqs/stream-id/v2" ||
    chain_instance_id ||
    owner_address_20 ||
    owner_nonce_u64_be
  )
```

`chain_instance_id` is the genesis-configuration fingerprint. Genesis MUST seed
it into a Stream Registry singleton before any CBQS instruction can execute.
Handlers MUST read that singleton rather than node-local configuration and MUST
reject an absent or malformed singleton.

The domain separator is `"cbqs/stream-id/v2"`, and version 1's was
`"cbqs/stream-id/v1"`, so no version-1 id can collide with a version-2 id and
every version-1 id is unreachable. Any out-of-tree re-derivation — a test
harness that reconstructs ids from an owner and a nonce — must change on the
same flag day, or it will address the wrong stream silently rather than fail.

**The preimage carries no separate chain id.** An earlier draft prefixed
`chain_id_u64_be`, and that term appeared nowhere else in the document: it had
no stated provenance, no reserved read, and two readings — the chain's
configured identifier, or the `chain_id` on the transaction being executed. The
second reading is a known hazard on this platform and would additionally put a
caller-supplied value inside a consensus-derived id. `chain_instance_id` is a
genesis-configuration fingerprint, so it already differs across chains, which
was the whole of what the chain id was doing here. Removing it deletes a fork
class rather than documenting one.

`CreateStream` MUST set `authorization_generation` to zero and `status` to
`Active`. Neither is carried in the instruction body, so both are stated here
for the reason §4.1 and §4.3 state their own seeds: they are consensus-visible
record bytes no payload supplies, and two implementations seeding them
differently produce different state roots while metering identically. The
generation is load-bearing beyond the root — §7 check 2 tests a grant's
generation for equality against it, so a disagreement about the seed is a
disagreement about which grants are live. `UpdateStream` is the only
instruction that raises the generation, and `CloseStream` the only one that
changes the status.

The Stream Registry MUST reject a `CreateStream` whose `(owner, owner_nonce)`
has already been used, including after that stream closes. An owner nonce is
never reusable, and a `Closed` record is retained to enforce that: a
resurrected id would let a grant issued against the old stream authorize the
new one.

`owner`, `owner_nonce`, and `provider` are immutable for the stream's lifetime.
Moving an application to another provider is an application migration; §4.4
states its ordering obligation.

#### 4.3 CbqsAccountV2

```text
CbqsAccountV2 {
  version:            u8,       // 2
  owner:              Address,
  provider:           Address,
  balance:            u128,     // nano-CBY held in registry custody
  active_streams:     u64,
  rate_per_block:     u128,     // snapshotted; see §6.1
  last_settled_block: u64,      // finalized height
  suspended:          bool,
}
```

**The account is keyed by `(owner, provider)`, and it holds the entire economic
relationship between those two parties.** An owner with streams at three
providers funds three accounts, which is an honest description of owing three
vendors. Everything follows from that key:

- the rent clock, the balance, and the solvency flag are shared by exactly the
  streams one provider serves for one owner, so two of them can never disagree
  about any of it;
- `settle` has exactly one provider to pay, so the rent split is a single
  credit rather than a distribution over an unbounded set, and every
  instruction's state-I/O is a fixed pair (§15.1); and
- a provider's collectible is **isolated** from the same owner's streams at
  other providers. A sibling elsewhere cannot drain the pool this provider is
  owed from, so the exposure §6.3 discloses is bounded by this provider's own
  settlement cadence.

The broker is the same party as `provider`, so it evaluates §6.2 against the
one account it is actually owed by, from fields its snapshot already carries.

`active_streams` counts that account's streams in `Active` status.
`CreateStream` increments it and `CloseStream` decrements it, and **every
instruction that touches an account settles it first** (§6.1), so
`active_streams` is constant across any settled interval. `CreateStream` MUST
be rejected when it would exceed `cbqs.max_streams_per_account`.

**Creation.** An account is created by exactly one instruction: a
`TopUpAccount` whose caller is the owner and whose `amount` is at least

```text
CBQS_MIN_ACCOUNT_TOPUP = 1_000_000_000     // nano-CBY
```

Two rules, one purpose. **Only the owner may create**, so a third party can
fund an existing account but cannot mint permanent registry state at an address
it does not control. **A creating top-up has a floor**, so the record costs its
creator something: without it an owner could pre-mint an account against every
registered provider for one nano-CBY each, which is unbounded permanent state.
The floor is a **refundable deposit, not a burn** — a handler deletes the
record at `balance == 0 && active_streams == 0` — so what it prices is
**entry**: one CBY must be locked to bring a pair's record into existence. It
does not price standing balance, and §5 says why it cannot: `settle` may leave
any residue, so a record can legitimately persist holding less than the floor.
The only route to one is to spend `CBQS_STREAM_CREATION_CHARGE` and let rent
draw the balance down, which costs five times the floor. §5's resulting-balance
rule is what stops an owner shortcutting that by withdrawing to a dust balance
instead. An earlier draft argued the floor also stopped an owner locking
today's rate for business it had not yet done; that was false, because a
created account seeds `rate_per_block = 0` and only `CreateStream` writes the
parameter in.

`CreateStream` does **not** create the account. Funding precedes owning: an
owner tops up, then creates streams. An earlier draft let `CreateStream` create
the record and then guarded it with a solvency test the fresh record could
never pass, so the path was dead and two implementers could reasonably resolve
it differently — an admission divergence on a consensus instruction.

A created record is exactly:

```text
balance = amount, active_streams = 0, suspended = false,
rate_per_block = 0,
last_settled_block = H            // the finalized height of the creating instruction
```

`rate_per_block` is seeded to zero rather than to the parameter. Four of its
five readers — `settle`'s `due`, `finalize`'s revival test, §6.2's
serviceability, and §5.1's provider-close predicate — multiply by
`active_streams`, so a zero rate cannot affect them while the account has no
streams.

The fifth reader does not vanish, and it is why §5 fixes an order rather than
leaving one to be inferred: `CreateStream`'s admission guard multiplies by
`active_streams + 1`, which is `1` at the transition. **`CreateStream` MUST
write the rate before evaluating that guard, on the transition that writes it
at all.** An implementation that wrote it
afterwards, alongside the `active_streams` increment, would still satisfy every
other reader and would still meter identically — and would test a stale rate
against a fresh balance, which is an admission fork on a consensus instruction
whenever an idle account carries a rent residue and governance has since moved
the parameter.

Seeding the parameter at creation instead would store a value no reader can
reach before `CreateStream` overwrites it, and charge every top-up a metered
read to produce it.

Every field is stated because leaving `last_settled_block` unstated is a
consensus divergence a gas vector cannot catch: an implementation seeding `0`
would charge `H × rate` at the first settlement that finds a stream — tens of
CBY against an account that owes nothing — and meter identically to one that
does it correctly.

**Custody.** Balance is real custody held by the Stream Registry system actor
at **`0x17`**. `TopUpAccount` transfers native CBY from the payer into `0x17`
and credits `balance` by the same amount in the same transition; every debit of
`balance` moves the same amount out of `0x17` in the same transition.

**No handler in §15.1 may change the quantity `native(0x17) − Σ balance`**, and
§18 rule 1 makes it zero at genesis. It is stated as a per-transition
preservation rather than as a global equality for two reasons, both of which a
global equality would get wrong. A handler MUST NOT read or test the quantity:
§15.1 reserves no read that could sum the accounts, there is no account index
or running total, and §16's events carry no amounts, so not even an indexer can
reconstruct the sum — an equality no handler can evaluate invites an assertion
inside consensus, which is a halt rather than a safety property.

And the equality is falsifiable from outside this document. A native transfer
into `0x17` is not a CBQS instruction, so §4's reserved-address rejections do
not reach it — those constrain the owner, payer, provider and caller *of a CBQS
instruction*. Such a credit raises the quantity and can never leave, because
every custody debit is bounded by some account's `balance`. This document does
not attempt to prevent it: `TopUpAccount` is itself a transfer into `0x17`, so
a carve-out would cost more than the disclosure. A debit from outside would
lower it, and every custody debit would then fail closed; nothing here recovers
from that either.

An account whose balance has no custody behind it can never be withdrawn from
and can never be deleted, because deletion requires `balance == 0`. A handler
MUST fail closed rather than credit a share it cannot debit from custody, and a
credit or debit that does not fit the native account's width MUST fail closed
rather than truncate.

**The account record is not permanent.** A handler that leaves `balance == 0`
and `active_streams == 0` MUST delete it. `suspended` is not part of that
condition, because §6.1 clears it whenever `active_streams` reaches zero — an
account with no streams owes nothing and cannot be insolvent — and requiring
`suspended == false` as a separate conjunct would have made the rule
unreachable on exactly the path it exists for: the abandoned account whose last
stream a provider closes under §5.1. A state delete meters as a state write
(§15.1).

`TopUpAccount` MUST be rejected when no provider record exists for `provider`,
and under the reserved-address and payer rules of §4. Every check is inside the
reservation §15.1 gives it.

#### 4.4 Application migration

Provider assignment is immutable, so moving an application to another provider
— and the only remedy v2 offers when a provider stops serving — is a migration
the application performs:

1. create a new stream naming the new provider;
2. copy or replay the data from a durable source the application controls;
3. issue new grants and redirect clients; and
4. **close the old stream only after verifying that the target's latest
   checkpoint covers everything the source held.**

Step 4's ordering is the obligation, not a suggestion: closing first turns a
migration into data loss, and v2 has no dispute path that could recover it.

### 5. Chain instructions

`CbqsInstructionV2` is `version: u8 = 2 || tag: u8 || body`:

| Tag | Instruction | Authorization | Effect |
| --: | ----------- | ------------- | ------ |
| 1 | `RegisterProvider` | caller account | Create `ProviderRecordV2` and charge the registration charge. |
| 2 | `UpdateProvider` | provider account | Change endpoints, key, metadata, or admission. |
| 3 | `CreateStream` | owner account | Settle, then create a stream and charge the creation charge. Requires a funded account. |
| 4 | `UpdateStream` | owner account | Rotate the admin key and/or bump the authorization generation. |
| 5 | `CloseStream` | owner account, or the assigned provider under §5.1 | Settle, then transition the stream to `Closed`. |
| 6 | `TopUpAccount` | paying account | Settle, then credit the named `(owner, provider)` account, creating it when the caller is the owner (§4.3). |
| 7 | `SettleAccount` | permissionless | Settle elapsed rent and materialize suspension or revival. |
| 8 | `WithdrawAccount` | owner account | Settle, then withdraw when that account has no live streams. |

```text
RegisterProviderV2 {
  signing_key:   SigningPublicKey,
  endpoints:     Vec<Bytes>,
  max_streams:   u64,
  accepts_new:   bool,
  metadata_hash: Bytes32,
}

UpdateProviderV2 {
  signing_key:   Option<SigningPublicKey>,
  endpoints:     Option<Vec<Bytes>>,
  max_streams:   Option<u64>,
  accepts_new:   Option<bool>,
  metadata_hash: Option<Bytes32>,
}

CreateStreamV2 {
  owner_nonce: u64,
  provider:    Address,
  admin_key:   SigningPublicKey,
}

UpdateStreamV2 {
  stream_id:                     Bytes32,
  admin_key:                     Option<SigningPublicKey>,
  bump_authorization_generation: bool,
}

CloseStreamV2      { stream_id: Bytes32 }
TopUpAccountV2     { owner: Address, provider: Address, amount: u128 }
SettleAccountV2    { owner: Address, provider: Address }
WithdrawAccountV2  { provider: Address, amount: Option<u128> }   // absent = the whole balance
```

An `UpdateStream` that carries `admin_key` MUST increment
`authorization_generation` **by exactly one**, whatever
`bump_authorization_generation` says — the step is stated because the field is
consensus-visible record state that §7 check 2 tests for equality, and a
reading under which the key change and the flag each contribute one puts two
validators on different state roots for the same permissionless instruction; one
that carries neither a new admin key nor `bump_authorization_generation = true`
MUST be rejected as a no-op. A key rotation that did not invalidate outstanding
grants would leave the old key's grants live, which is the opposite of what a
rotation is for.

`UpdateStream` and `CloseStream` MUST be rejected when the stream is `Closed`.

`TopUpAccount` names the account it credits, so a third party MAY fund an owner
— but only the owner may bring a record into existence (§4.3). It MUST be
rejected when `amount` is zero. `WithdrawAccount` acts on the caller's
`(caller, provider)` account: it MUST debit `amount` from `balance` and the
same amount from custody at `0x17`, and credit it to the **owner's native
account**, in the same transition. An absent `amount` withdraws the **whole
balance**; a present one MUST be nonzero and at most `balance`. It MUST be
rejected unless `active_streams == 0` and the resulting balance is either zero
or at least `CBQS_MIN_ACCOUNT_TOPUP`. `suspended` is not tested, because §6.1
clears it whenever `active_streams` reaches zero.

`amount` is an option so that "withdraw everything" does not have to name a
number. `TopUpAccount` against an existing account is permissionless, so if the
only way to empty an account were to name `amount == balance` exactly, any
third party could add one nano-CBY ordered ahead of the withdrawal and reject
it — indefinitely, for a nano a block. The exit has to be expressible without
naming the balance.

The resulting-balance rule is what makes §4.3's creation floor bite. Without it
an owner pays the floor, withdraws all but one nano-CBY in the next block, and
keeps a permanent record for a nano. It bounds withdrawals, not rent: `settle`
can leave any residue below the floor, so a record may legitimately persist
under it — the floor is a rule about what an owner may *take out*, not a
minimum the record always holds.

**`CreateStream` MUST be rejected when no account record exists** for
`(owner, provider)` — §4.3 gives creation to `TopUpAccount` alone — **and,
after settling and, when this `CreateStream` takes `active_streams` from 0 to
1, writing `cbqs.stream_rate_per_block` into `account.rate_per_block` (§6.1),
unless
`balance >= rate_per_block * (active_streams + 1)`.**

The `+ 1` is the whole guard: it asks whether the account can fund the stream
being created, not merely the ones it already has. Testing the `suspended` flag
instead admitted the *first* stream of every account, because `finalize` clears
the flag at zero streams and the increment has not happened yet — so a
zero-balance owner could mint `Active` chain state the broker refuses every
session for, earn the provider nothing, burn the creation charge for nothing,
and pin a provider slot until the provider spent a §5.1 close on it, up to
`cbqs.max_streams_per_account` times.

**`CreateStream` MUST also be rejected when `provider == owner`, and when
`owner` is a reserved system address** (§4). Neither rejection is reachable as
the sole cause of a rejection: `CreateStream` already requires an existing
`(owner, provider)` account, and §4's `TopUpAccount` rejections mean no account
with a reserved owner or with `owner == provider` can exist. They are stated
anyway, as declared belt-and-braces on the instruction that mints permanent
registry state — but a conformance harness cannot build a fixture that isolates
them.

`SettleAccount` and `WithdrawAccount` MUST be rejected when no record exists
for the named pair. Neither creates one: §4.3 gives creation to `TopUpAccount`
alone, and letting a permissionless `SettleAccount` create records would reopen
the third-party minting §4.3 closes.

#### 5.1 The provider's close

The assigned provider may close a stream, and the rule is stated as an ordering
because the ordering is the whole of it:

> `CloseStream` submitted by the assigned provider MUST first settle the
> account (§6.1) and is authorized **only if, after that settlement,
> `balance < rate_per_block * active_streams`** — the account cannot fund even
> one more block of the streams it has. Otherwise it MUST be rejected without
> effect.

Settling first is what makes the authorization honest. The projected inequality
is preferred over the stored `suspended` flag because the flag is a
materialized record of a *past* default, and nobody is obliged to submit the
settlement that materializes it, whereas this rule has to authorize on whether
the account can fund the **next** block — the same quantity §6.2 refuses
service on. Using the flag would let the provider's close and the broker's
refusal fire on two conditions that disagree while no one settles. Settling
first means the provider's own instruction gives the owner credit for every
block the owner paid for, and the close proceeds only if the owner still cannot
pay.

The predicate is written out rather than deferred to §6.2 because §6.2 is
defined over `H_snapshot`, a broker-side quantity a consensus handler does not
have. Substituting the handler's own height would make the elapsed factor zero
and the test degenerate to `suspended == false` — the rule this one replaced —
and leaving the substitution unstated would let two implementations differ on
whether a provider's close is admitted. The inequality above is the negation of
§6.2 evaluated one block on — the first block the account would have to pay for
— stated directly so there is nothing to substitute.

The provider's close is not a seizure of value: it moves no balance the owner
did not already owe. It is a release of the provider's own capacity, which
`assigned_streams` otherwise pins forever against an owner who has stopped
paying and stopped participating.

What it does not do is protect the owner's data, which §12 leaves to the
provider in any case, and it is not a grace mechanism: an owner who wants a
stream kept keeps the account funded, and §6's suspension is reversible for as
long as the stream exists.

### 6. Billing

CBQS charges a flat per-stream rate for the existence of a stream, drawn from
the owner's account balance. It does not meter messages, bytes, or capacity:
the provider's exposure is bounded instead by the per-stream ceilings of §15,
which the broker enforces, and by its own `max_streams`. A stream's durable
footprint has three parts, and only the first is measured in bytes:

- **`retained_bytes_limit` measures the records and their checkpoint
  receipts** (§12), each receipt discarded once the floor passes the last
  record it attests.
- **Delivery cycles are bounded by a product, not by that limit.** One cycle
  exists per (retained record, applicable group), and §9.2 reclaims each with
  the record it names — so the floor bounds them, but their bytes are not
  inside `retained_bytes_limit`. The bound is
  `retained records × cbqs.max_groups_per_stream`, and it is stated rather than
  folded into the byte limit because folding it in would make creating a group
  evict data: a group starting at `At(first_retained_sequence)` mints a cycle
  for every retained record at once, and a byte limit that saw them would
  advance the floor in response.
- **Everything else is bounded by count**: lanes by
  `cbqs.max_lanes_per_stream`, groups by `cbqs.max_groups_per_stream`, leases
  by `cbqs.max_groups_per_stream × max_in_flight`, and one settings record per
  stream. The lease figure is a ceiling on the configuration rather than a
  reachable count: a lease exists only while a delivery is outstanding, so at
  the §15 defaults the number a broker actually holds is bounded an order of
  magnitude lower by the delivered rate — 4,194,304 B/s sustained over a
  30-second `max_visibility_ms`, divided by a 152-byte delivery frame, is about
  828,000 against a configured ceiling of 16,777,216. The two do not stay in
  that order: at the §15 maximums the rate-derived figure is the larger of the
  two, so a provider takes whichever is smaller for the settings in front of
  it.

Naming all three matters because two earlier drafts each named one and implied
it was the whole: the first claimed the byte limit covered the footprint, and
the second claimed cycles were inside it. At the §15 defaults the difference
between those two readings is a gigabyte against a few terabytes at the
defaults, and a terabyte against tens of petabytes at the §15 maximums, so a
provider sizing from either one alone sizes wrong. The product is the number a provider
budgets against, and `cbqs.max_groups_per_stream` is the lever that moves it.

Rent is measured in **finalized block height**. There is no pause-adjusted
clock. Version 1 subtracted CIP-12 pause coverage so that an emergency pause of
the registry — during which no owner can top up, because no CBQS instruction
executes — would not accrue rent nobody could have paid. Version 2 does not
need that machinery, and the cost of not having it is bounded and stated: **if
the registry is ever pausable, a pause consumes at most the balance each
account held when it began.** The conditional is deliberate — CIP-12 §7's
allowlist does not currently name `0x17`, though the implementation it cites
does — and it is the reason this document no longer depends on the answer. Rent
accrues through the pause, the balance covers what it covers, and the rest is
written off by the first settlement afterwards — the same write-off §6.3
describes, arriving through a different door. Recovery is one `TopUpAccount`
per account, which settles, writes off, credits, and revives in a single
instruction (§6.1).

What a pause cannot do is compound: a suspended account accrues nothing, so the
damage does not grow with the pause's length once the balance is gone. That
bound is what makes the coverage clock unnecessary — not an argument that the
pause is harmless — and it buys the removal of a state record, a coverage
expression, a per-instruction read, and a cross-document dependency on CIP-12's
pausable-actor list.

#### 6.1 Settlement

Settlement is defined once, here, and **every instruction that touches an
account calls it first** — `CreateStream`, `CloseStream`, `TopUpAccount`,
`SettleAccount`, and `WithdrawAccount`, with no exceptions. Uniformity is the
rule, not an optimisation: an earlier draft exempted `TopUpAccount` on the
ground that funding does not change what is owed, which was true and beside the
point. It changes what is *collectible*, so topping up before settling handed
the next settlement a larger balance to take for an interval the owner had
already been refused service through, and made a provider's optimal strategy
*never* to settle a lapsed account.

```text
settle(account, H):                       // charges; changes no counter
  require H >= account.last_settled_block            // else fail closed

  if not account.suspended:
      due = (H - account.last_settled_block)
            * account.rate_per_block
            * account.active_streams                 // checked u128
      if account.balance >= due:
          account.balance -= due
          paid = due
      else:
          paid = account.balance
          account.balance = 0
          account.suspended = true
  else:
      paid = 0                                       // a suspended account accrues nothing

  account.last_settled_block = H
  distribute(paid, account.provider)
  return paid

finalize(account):                        // runs LAST in every handler
  if account.active_streams == 0:
      account.suspended = false                      // nothing is owed
  else if account.suspended
          and account.balance >= account.rate_per_block * account.active_streams:
      account.suspended = false
  if account.balance == 0 and account.active_streams == 0:
      delete account
```

**Every account-touching handler runs `settle` first, then its own effects,
then `finalize`.** The split is the point. An earlier draft folded the revival
and deletion tests into `settle`, where they ran *before* the handler changed
`active_streams` or credited a top-up — so closing the last stream of a
suspended account left the flag set on an account that owed nothing, and
`TopUpAccount` ended with a fully funded account still suspended, which in turn
let §5.1 authorize its provider to close its streams. Both were reachable, and
both are structural: a test that must observe a mutation cannot run before it.

Five properties are load-bearing.

**`active_streams` is constant across every settled interval**, because every
mutator settles before it changes the counter and finalizes after. `due` is
exact rather than an approximation, and the broker computing the same product
from the same record gets the same answer.

**The rate is snapshotted when a billing run starts, and is fixed for its
duration.** `CreateStream` copies `cbqs.stream_rate_per_block` into the account
on the transition from `active_streams == 0` to `1` — the moment the account
begins to be billed — and nothing else reads the parameter. A newly created
account stores a rate of zero. Four of its five readers multiply by
`active_streams` and so cannot observe it while the account has no streams; the
fifth is `CreateStream`'s own guard, which is why §5 orders the rate write
before it.

Both halves matter. Fixing the rate *within* a run makes `settle` a pure
function of the account and the height, which is what §6.3 claims: an earlier
draft re-snapshotted at every settlement, and since settlement is
permissionless any caller could pick the moment a rate rise took effect and
hand the provider 90% of the difference. Re-reading it *at the start* of a run
keeps governance's price lever alive: a later draft snapshotted only at
creation, and because a zero-stream account accrues nothing and can be held
indefinitely, an owner could hold an old rate forever and never take the
repricing door. The `0 → 1` transition is the right place because only the
owner can trigger it, so no third party can choose when a repricing lands.

An owner therefore pays, for each continuous run of streams at one provider,
the rate in force when that run began.

**The underfunded branch takes the whole balance** rather than a whole number
of blocks, so nothing is left that is neither paid nor owed, and §6.3 states
what the write-off costs.

**A suspended account accrues nothing**, so an abandoned account's arrears
cannot grow without bound and revival never costs more for having been left.
The broker refuses its mutations (§6.2), which is what makes charging nothing
correct rather than generous.

**An account with no streams is never suspended**, because `finalize` clears
the flag after the handler has decremented the counter. That is what makes
§4.3's deletion rule reachable on the abandoned-account path — the one the rule
exists for.

`distribute` splits every paid amount out of custody:

```text
CBQS_PROVIDER_BPS = 9_000                      // protocol constant

provider_share = paid * CBQS_PROVIDER_BPS / 10000
platform_share = paid - provider_share
```

The split is a **protocol constant, not a governance parameter**. A live
parameter here has the same retroactivity as a live rate — `settle` pays for an
interval that has already elapsed — and at `provider_bps = 0` one governance
write would pay providers nothing for service already rendered. The rate is
fixed by snapshotting; the split is fixed the cheaper way, by not being
writable. There is no separate platform parameter: the platform share is the
complement, and a stored copy of `10000 - CBQS_PROVIDER_BPS` could only
disagree with it.

`provider_share` is credited to `account.provider` — one recipient, because
§4.3 keys the account by it — and `platform_share` to the **Platform Fee
Account, system actor `0x18`**, the same account and rule as CIP-31 §4. Both
addresses are known without a lookup, so settlement's state-I/O is a fixed
pair. Integer division truncates toward zero and the platform share is the
exact complement, so `provider_share + platform_share == paid` for every input,
and custody is debited by exactly `paid`. The truncation is one-directional:
`provider_share` is zero exactly when `paid <= 1` nano-CBY, which is why §15
gives `cbqs.stream_rate_per_block` a floor rather than only requiring it to be
nonzero.

**`TopUpAccount` credits between `settle` and `finalize`**, so the sequence is:
settle the elapsed interval against the old balance, write off whatever it
could not cover, credit the new money, then finalize — which sees the new
balance and clears `suspended`. A funded owner is revived by **one
instruction**, whoever else submitted what in the same block: an adversary who
orders `SettleAccount` ahead of the top-up changes nothing, because the second
settlement finds `due == 0` and `finalize` runs on the credited balance.

#### 6.2 Solvency, and what the broker refuses

An account is **serviceable** at `H_snapshot` when

```text
suspended == false
  AND balance >= (H_snapshot - last_settled_block)
                 * rate_per_block
                 * active_streams          // checked; overflow fails closed
```

This is the same expression `settle` evaluates, over the same fields, at the
same scope — which is what makes it sound. Version 1 could not state it,
because the balance was per stream; the draft of version 2 that put the balance
on the account and left the test per stream could not state it either, because
one stream's dues say nothing about its siblings' claims on a shared pool.

The broker MUST evaluate serviceability **on every request that appends or
creates broker state**, not only when a session opens, and MUST refuse those
requests with `AccountSuspended` when it fails. The test is whether the action
**creates or extends** durable broker state. That covers every append,
`CreateLane`, `CreateGroup`, `SetStreamSettings`, and any `UpdateGroup` that
raises `record_deadline_ms`. `DeleteGroup` is exempt: it removes state rather
than creating any. Among the four lease actions the test covers `Extend` alone:
`Ack`, `Reject`, and `Nack` all *release* a lease, and the no-new-lease rule
below forbids re-acquiring one, so all three are **exempt**; `Extend` keeps a
record leased past the expiry that would otherwise return it, so it is not.
That discriminator is exact because `Nack` moves no deadline: §9.2 releases a
`Nack`ed record at `issued_at + visibility_timeout_ms`, the moment silence
would have released it, so no exempt action extends a record's unavailability
beyond what the lease already bought. An earlier draft let a `Nack` park a
record for an hour while `Extend` bought at most thirty seconds, and the exempt
action then bought a hundred and twenty times the lifetime of the gated one.
Without the exemption a suspended account's consumers cannot terminate the
cycles they already hold: each lease expires, each expiry counts toward
`max_attempts` exactly as a `Nack` would (§9.2), and at the §15 defaults a
suspension of any length costs each in-flight cycle exactly one attempt, since
the no-new-lease rule below forbids the redelivery a second attempt would need —
and refusing `Nack` would have burned an attempt on the very cycle its holder
was asking to release. Evaluating it
only at session open would let a session established while solvent keep
appending for the whole life of its grant against an account that had since
been suspended — up to `cbqs.max_grant_ttl_ms` of free service, which is
exactly the exposure §6.1's zero-accrual rule would otherwise create.

Reads and replays are the provider's call: it **MAY** continue serving them,
because they cost bandwidth rather than durable state and cutting a consumer
off from data already paid for serves nobody, and it **MAY** refuse them with
the same code once an account is suspended, because §6.1 guarantees it is paid
nothing for them and `max_delivered_bytes_per_sec` is not a small number. This
is the one place the document leaves a provider a choice, and it is stated as a
choice rather than left as a contradiction between "serves it nothing" and
"reads may continue".

**A `Group` delivery is not a request, so the rule above does not reach it and
this one does.** While an account is unserviceable the broker MUST NOT issue a
new lease — a lease is durable state created at the broker's own initiative,
and it is the one growth path a suspended owner would otherwise still get for
free. Existing leases run to their expiry unchanged. Together with the
`Ack`/`Reject`/`Nack` exemption this makes the in-flight set monotonically
shrink through a suspension rather than churn: a consumer drains what it holds,
no attempt is burned on a cycle its holder was forbidden to terminate, and the
group is quiescent by the time the owner tops up.

The test needs no chain round trip; every field is in the snapshot the broker
already holds.

#### 6.3 Creation charge, and the write-off

`CreateStream` MUST debit `CBQS_STREAM_CREATION_CHARGE` from the **owner's
native account**, not from its CBQS balance, and credit it to the zero address
in the same transition that writes the stream record, failing without creating
the stream when the debit cannot be completed.

```text
CBQS_STREAM_CREATION_CHARGE = 5_000_000_000     // nano-CBY
```

Three properties are load-bearing. It is a **protocol constant**, so no
governance write can price permanent registry state at zero. It is **credited
to the zero address**, so a self-providing owner cannot recover it through the
rent split the way it recovers `CBQS_PROVIDER_BPS`. And it is debited from the
**native** account rather than from custody, so it cannot consume balance that
providers have already earned but not yet settled.

**The shortfall is written off, not carried.** When settlement finds the
balance short, the balance goes to zero and the unpaid remainder ceases to
exist; no later instruction can collect it, and because §6.1 makes
`TopUpAccount` settle *before* it credits, new money cannot be reached
backwards through the interval it was written off from. A provider's
collectible is bounded by what the account holds, and **settlement cadence does
not change it.** `balance` has exactly three debit sites — `settle`'s two
branches, both paying this provider, and `WithdrawAccount`, which requires
`active_streams == 0` and so requires a `CloseStream` that §6.1 makes settle
first. The balance is therefore unreachable except through a settlement that
pays this provider, so settling every block and settling once after a year
collect the same amount. An earlier draft called cadence "the provider's real
protection" and gave it a governance parameter; it protects nothing, and both
are deleted. What bounds unpaid *service* is §6.2's per-request serviceability
test, evaluated against a snapshot no older than `cbqs.max_snapshot_age_blocks`
— a bound three orders of magnitude tighter than any settlement interval.

Settlement is permissionless: nobody is obliged to submit it, so a provider
that wants to be paid submits `SettleAccount` itself. A permissionless caller
can therefore materialize another owner's suspension or revival, but not change
what that owner owes: `settle` is a pure function of the account and the
height, so the only thing the caller chooses is *when* the same arithmetic runs
and *how often* — and how often costs the provider nothing, because §15
requires `cbqs.stream_rate_per_block` to be a multiple of 10. `provider_share`
is therefore exact on the full branch, so settling every block for a year pays
the provider exactly what settling once would. An earlier draft disclosed the
per-call truncation as an exposure a permissionless caller could force; the
multiple-of-10 rule closes it instead, which is the better trade for one clause
on a parameter whose Default, floor, and Maximum already satisfy it.

The same key is why the exposure is bounded: because the account belongs to one
provider, no other provider's settlement can reach the balance this one is owed
from. A provider that settles on its stated cadence is exposed to at most that
cadence of service, and to nothing that happens elsewhere on the network.

### 7. Authorization

The stream's current admin key signs a `StreamGrantV2`. The owner controls that
key through `UpdateStream`. An owner account signature is not itself a valid
grant signature.

```text
StreamGrantV2 {
  version:                  u8,      // 2
  chain_instance_id:        Bytes32,
  stream_id:                Bytes32,
  authorization_generation: u64,
  grant_nonce:              Bytes32,
  holder_signing_key:       SigningPublicKey,
  verbs:                    u16,
  lane_scope:               Any = 1 | Exact = 2 (u64) | Set = 3 (Vec<u64>),
  group_scope:              Any = 1 | Exact = 2 (u64),
  not_before_ms:            u64,
  expires_at_ms:            u64,
  max_message_bytes:        u32,
  max_append_bytes_per_sec: u64,
  signature:                Signature,
}

grant_id = keccak256(stream_grant_signing_bytes_v2(grant))
```

| Bit | Verb |
| --: | ---- |
| 0 | `APPEND` |
| 1 | `CONSUME` |
| 2 | `ACKNOWLEDGE` |
| 3 | `REPLAY` |
| 4 | `GROUP_ADMIN` |
| 6 | `LANE_ADMIN` |
| 7 | `STREAM_ADMIN` |

Bits above 7 MUST be rejected, as check 6 of the ordered list below. **Bit 5 is
unassigned**: version 1 used it for `REDRIVE`, and version 2 has no dead-letter
queue to redrive. It is left as a hole rather than closed up so that the
remaining bits keep the meanings they carried in version 1 and a v1 verb table
ports unchanged; `STREAM_ADMIN` is new at bit 7 and authorizes §13 settings
changes. A grant setting bit 5 is not rejected — it simply authorizes nothing,
which is the same disposition as a grant setting no bits at all.

The grant carries **no per-holder in-flight limit**. Version 1 had one there,
and on the grant it needed a durable per-holder counter, a rule about when to
check it relative to lease release, and an unstated answer for a holder
presenting several valid grants. The bound itself is not gone — it moved to the
group as `max_in_flight_per_holder` (§9.2), where it is one number beside the
group total, needs no per-grant counter, and has one answer per holder.

**`holder_signing_key_id` is `keccak256("cbqs/signing-key-id/v2" ||
canonical(holder_signing_key))`.** It is stated here because it is the identity
three later rules are scoped by — lease ownership and
`max_in_flight_per_holder` (§9.2), and the per-holder append rate that
aggregates across a holder's sessions (§13) — and all three collapse if it is
read as a per-session or per-grant handle instead. `grant_nonce` exists so an
owner can mint distinct grants over identical terms, so a per-grant reading
gives one holder as many budgets as it holds grants, which is the
multiplication those scopes exist to prevent. An earlier draft carried the
derivation inside a deduplication key and lost it when that key was deleted.

The broker MUST verify the following **in the order listed**, rejecting on the
first failure. Two things fix the order. Check 5 subtracts two values the grant
supplies, and orders its own two comparisons so that the subtraction cannot
underflow. And the signature is
verified **third**, after two comparisons that cost nothing: a strict
Ed25519 verification is roughly three orders of magnitude more work than a
`memcmp`, an unauthenticated peer can send `OpenSession` frames as fast as a
socket drains, and `cbqs.max_sessions_per_connection` caps open sessions rather
than handshake attempts. Ordering the free checks first means a peer that
guesses neither the chain instance nor a stream the broker serves pays for
nothing. Two of the three codes this exposes before authentication —
`ChainInstanceMismatch` and `StreamUnknownToBroker` — tell such a peer nothing
it did not already supply. The third, `AuthorizationGenerationStale`, does: it
reports whether *this broker's snapshot* has caught up to a generation bump,
which is broker state and is the signal an attacker timing the use of a revoked
grant wants. It is exposed deliberately, because the alternative is to verify a
signature before deciding whether the stream is one this broker serves, and a
peer that learns a broker is behind still needs a valid admin signature to use
anything.

1. the exact `chain_instance_id` and `stream_id`;
2. `authorization_generation` equal to the snapshot's value;
3. the admin-key signature over `stream_grant_signing_bytes_v2`;
4. the time window, allowing at most `cbqs.max_clock_skew_ms`, evaluated as
   `not_before_ms <= now + cbqs.max_clock_skew_ms` and
   `now <= expires_at_ms`. The skew is applied to `not_before_ms` only: a grant
   already past its raw `expires_at_ms` MUST be rejected with `GrantExpired`,
   because admitting it inside the skew would open a session §7 requires to
   terminate at once — a session with no usable lifetime, and no single
   ordering of `SessionOpened` and `GrantExpired` for a client to expect. Both
   comparisons arrange the skew
   against the broker's own clock rather than against a grant field, for the
   reason check 5 gives: the grant's two timestamps are `u64`s an
   unauthenticated peer chose, so `not_before_ms - skew` underflows at
   `not_before_ms = 0` and `expires_at_ms + skew` overflows at `u64::MAX`,
   while `now + skew` is bounded by a clock and a governance parameter;

5. `not_before_ms < expires_at_ms` **first**, then
   `expires_at_ms - not_before_ms <= cbqs.max_grant_ttl_ms` — the ordering is
   normative because the subtraction is over two `u64`s an unauthenticated peer
   chose, and an underflow is a panic in a checked build; and a `Set` lane
   scope of at least two and at most `cbqs.max_lane_ids_per_grant` sorted,
   unique ids — a one-element `Set` MUST be rejected, because `Exact` already
   expresses it, and a broker admitting both would verify two grant preimages
   and issue two `grant_id`s for one authority. That is not forbidden in
   general — `grant_nonce` exists so an owner can mint distinct grants over
   identical terms, and so give each holder a distinct `grant_id` to name in
   its session proof — but it should be deliberate rather than a side effect of
   two spellings of one scope. Distinct grants are **not** separately
   revocable: version 2 has no per-grant revocation, and `UpdateStream`'s two
   levers both act on the whole stream, so revoking one holder invalidates
   every outstanding grant at the next snapshot refresh. An owner that needs a
   smaller blast radius uses shorter expiries, which check 5 bounds;

6. `verbs` has no bit above 7 set, then the requested verb, and **both** scope
   dimensions by intersection — a group scope never widens a lane scope; and
7. the grant's `max_message_bytes` and `max_append_bytes_per_sec` against the
   stream's §13 settings, the effective bound being the lower of the two.

**A lane scope may name a lane that does not exist yet**, because §8 assigns
lane ids from a counter: `Exact(7)` authorizes whatever lane becomes the
seventh. That is a standing authorization over part of the stream's future
namespace, and owners issuing narrow scopes SHOULD name only lanes that already
exist. **`CreateLane` therefore requires `lane_scope = Any`**, returning
`LaneScopeTooNarrow` otherwise: a holder scoped to specific lanes cannot mint
the very ids its scope names, which is the one place the counter would
otherwise let a narrow grant widen itself.

**`CreateGroup` requires `group_scope = Any` for exactly the same reason**,
returning `GroupScopeTooNarrow` otherwise. §9.2 assigns `group_id` from a
per-stream counter, so `Exact(3)` is the same standing claim on the future that
`Exact(7)` is for lanes, and a holder that can advance the counter to 3 reaches
a group the owner meant to create itself — setting `start` and
`max_in_flight_per_holder`, which §9.2 makes **immutable**, so `UpdateGroup`
cannot repair it. The rule is stated in both directions because the earlier
draft's protection was a deletion tombstone against a client-chosen id;
replacing client-chosen ids with a counter removed the *reuse* half of that
hazard and left the *advance* half, which is the half this rule closes.

A holder that must both create groups and be confined to one afterwards needs
two grants: an `Any`-scoped one to create with, and a narrow one to operate
with. That is the same shape §8 already forces for lanes.

**Scope is checked on every operation, including lease actions.** An operation
that resolves to a `(lane, group)` pair — every `Ack`, `Nack`, `Extend`,
and `Reject`, whose bodies name a lease rather than a lane — MUST have both components inside the acting session's scopes, returning
`LaneScopeDenied` or `GroupScopeDenied`. Naming a lease does not exempt an
operation from the scopes the grant states.

**Session establishment.** A session begins with the client's `OpenSession`
carrying the grant. The broker answers with `SessionChallenge`, echoing that
frame's `request_id` and carrying an unpredictable 32-byte challenge, an
unpredictable `session_id`, and an `expires_at_ms` no more than
`cbqs.session_handshake_timeout_ms` in the future. Client-initiated is the
normative order — a challenge issued unprompted on connection could not echo a
`request_id`, and would cap the connection at one session, contradicting
`cbqs.max_sessions_per_connection`. The client returns:

```text
SessionProofV2 {
  version:           u8,       // 2
  chain_instance_id: Bytes32,
  stream_id:         Bytes32,
  grant_id:          Bytes32,
  session_id:        Bytes32,
  challenge:         Bytes32,
  signature:         Signature,
}
```

signed by the grant's holder key over
`keccak256(session_proof_signing_bytes_v2(proof))`. The broker MUST reject the
proof, and MUST close **the session being established** — not the connection,
whose other sessions are unaffected by one bad proof — unless all of:

- `proof.session_id` and `proof.challenge` are the exact pair issued for
  **this `OpenSession` on this connection**, and that pair has not already
  completed a session — so a captured `CompleteSession` frame opens nothing,
  and the broker retains at most `cbqs.max_sessions_per_connection` pairs,
  which die with the socket;
- the proof arrives before the challenge's `expires_at_ms`;
- `proof.grant_id == grant_id(grant)` for the grant carried in `OpenSession`,
  and `proof.stream_id` and `proof.chain_instance_id` equal that grant's; and
- the signature verifies under `grant.holder_signing_key`.

**Requests inside a session are not individually signed.** Every `Request`
frame carries the `session_id` it executes under (§11.1), and the broker MUST
execute it with exactly that session's grant and MUST reject a `session_id`
that is not open on the connection that sent it. No request body carries a
`stream_id`: the session determines it, so there is nothing to disagree with.

The exposure this accepts is stated in Security Considerations: an attacker who
can inject into an established TLS session acts with that session's authority.
Per-request signatures would not have prevented the same attacker from
reordering or dropping frames.

**Session termination.** A broker MUST terminate a session, and every
subscription opened under it, when any of the following becomes true:

- the grant's `expires_at_ms` passes — this is the bound §3 relies on, and it
  needs no snapshot refresh to observe;
- the snapshot shows the grant no longer matches (a bumped
  `authorization_generation`, a rotated admin key, or a `Closed` stream), no
  later than the first request the broker serves after the refresh that
  observed the change; or
- the connection carrying it closes.

A broker MUST NOT open a new session for a stream that is `Closed`, absent from
its snapshot, or whose owner account is not serviceable (§6.2). The codes are
`StreamNotActive`, `StreamUnknownToBroker`, and `AccountSuspended` (§14).

### 8. Lanes

Lane `0` is the default lane. It exists for every stream, has no
`LaneRecordV2`, and is always `Active` — `CloseLane` rejects it, so there is no
status to report. `ListLanes` therefore does not include it, and
`cbqs.max_lanes_per_stream` counts created lanes, so a stream may hold that
many lanes with ids `1` upward *in addition to* lane `0`. Stating all three
removes the readings under which two brokers return different pages for the
same empty stream, or differ by one at the creation ceiling.

Lanes above `0` are broker-side state:

```text
LaneRecordV2 {
  version:       u8,       // 2
  stream_id:     Bytes32,
  lane_id:       u64,
  status:        Active = 1 | Closed = 2,
}
```

**`lane_id` is assigned by the broker, not chosen by the client.** `CreateLane`
returns the next unused integer for that stream, starting at `1`, and the
broker never reissues one: it keeps a per-stream high-water mark, committed in
the same atomic write as the `LaneRecordV2` it assigns (§10), so a recovery
cannot hand the same id out twice. `cbqs.max_lanes_per_stream` bounds how many
may be `Active` at once, and `cbqs.max_lane_creates_per_min` bounds the rate at
which a stream may mint them — ids cannot be squatted, but creation still costs
durable state under flat billing.

`CloseLane` MUST be rejected for lane `0` with `LaneNotClosable`. Lane `0` is
where a client appends when it has no reason to route — `AppendRequestV2.lane_id`
is a plain `u64`, so there is no wire form for "no lane" and `0` is the value a
client sends — and closing it would make the stream unusable for every holder
that never created a lane, irreversibly and from one request.

A closed lane rejects new appends and remains replayable through the retention
floor, after which the broker MAY drop its `LaneRecordV2` — the counter, not
the record, is what prevents reuse, so the record has no reason to outlive the
data it describes.

Lane creation is rate-limited per stream at `cbqs.max_lane_creates_per_min`,
and a creation over that rate returns `RateLimited`.
Broker-assigned ids removed the *squatting* problem version 1's per-grant rate
limit also covered, but not the *rate* problem: billing is flat per stream, so
without a limit a `LANE_ADMIN` holder can mint records and high-water-mark
writes at no marginal cost. The limit is per stream rather than per grant
because it bounds the resource the stream actually consumes.

`ListLanes` requires at least one of `CONSUME`, `REPLAY`, or `LANE_ADMIN`;
`APPEND` alone does not authorize topology discovery. It is paginated by
`cbqs.max_lane_list_page`, filtered by the caller's lane scope before the page
limit, with an exclusive `after_lane_id` cursor.

A lane is a routing filter over one total order. Lanes have no independent
provider, billing, retention, or ordering authority, and no per-lane quota
carve-out: a hot lane consumes the stream's shared throughput and may
backpressure its siblings. A lane-scoped grant restricts what an honest broker
serves; it is not a cryptographic boundary.

### 9. Records and delivery

```text
AppendRequestV2 {
  lane_id: u64,
  payload: Bytes,
}

RecordHeaderV2 {
  version:   u8,       // 2
  stream_id: Bytes32,
  lane_id:   u64,
}
```

A record is identified by its `sequence: u64`. The first record of a stream has
`sequence = 1`; sequences increase by exactly one per committed record and are
never reused. **`0` is not a record.** It is the value every exclusive lower
bound in this document takes before anything has been committed — a cursor that
has consumed nothing (§11.3), a consumer group's progress watermark (§9.2), a
retention floor over an empty stream (§12) — and reserving it is what lets each
of those be a plain `u64` instead of an `Option<u64>` that every reader has to
unwrap the same way. An origin of `0` would make the sentinel and the first
record the same number, which is the one collision a sequence space cannot
tolerate. The broker fills `stream_id` from the session.

**The header carries no client-chosen field.** Every field in it is one the
broker assigns and reads. A message identifier and an application timestamp
belong in the payload for the same reason the next paragraph gives, and putting
them in the header would be worse than redundant: a broker-visible field the
broker never interprets invites a later rule to interpret it, which is the
history §9.1 and §12 both record.

`RecordHeaderV2` carries the version byte because it is the canonical preimage
inside `record_digest` (§10), which a provider signs.

Application semantics, reply routing, storage references, encryption framing,
and application deduplication identifiers belong inside the payload. The
broker-visible header contains only delivery mechanics; the broker MUST NOT
interpret payload bytes. Payloads above the effective `max_message_bytes` — the
lower of the §13 setting and the grant's limit — MUST be rejected with
`MessageTooLarge`.

An append is acknowledged only after the record has crossed the durable-commit
boundary of §10. The broker MUST NOT acknowledge from memory or an
operating-system page cache.

**What a checkpoint proves, and to whom.** `records_digest` is flat over the
batch, so verifying a receipt means recomputing that digest — which requires
holding **every** record in the batch. A party that holds the whole batch can
prove the provider committed to exactly that range. A party that holds only
some of it cannot, and no arrangement of this receipt changes that: a producer
whose records shared a batch with another producer's is such a party.

Goal 5 is stated to match. It is a batch commitment verifiable by a full-batch
observer, not an individual receipt a producer can check against its own
append. An earlier draft returned the receipt inside `Appended` to close a
timing gap, which gave every producer 245 bytes it could not verify and, at the
§15 defaults, required 6.00× the acknowledgement egress the delivered ceiling
allows — 87,381 minimal appends per second answered with a 288-byte frame is
25,165,728 B/s against a 4,194,304 B/s ceiling. Per-record binding — a Merkle path over the batch — would make a
producer's own proof checkable, and is deliberately not here: nothing in v2
consumes such a proof, and §11 already declines lane-scoped verification on the
same ground.

#### 9.1 Producer retries and duplicates

**The broker does not deduplicate appends.** A retried `Append` that the broker
already committed produces a second record with a second sequence. Version 1
carried a broker-side deduplication store, and four drafts of version 2 tried
to bound it; this document does not have one.

Nor does the record carry a client-chosen identifier for a consumer to
deduplicate on. Version 1's broker-side key was `(producer, client_message_id)`
— a **pair**, because `client_message_id` is client-chosen and two honest
producers on one stream may pick the same one. A single header field would
therefore have been unusable for exactly the purpose it appeared to serve: a
consumer deduplicating on it alone drops messages it has never seen. The
identity a deduplicating party needs is the producer's to define, and §9's
first paragraph puts it where a producer can define it — the payload.

§9.2 already places the obligation on the party that acts on a record:
at-least-once delivery repeats application effects, and a consumer MUST make
those effects idempotent or deduplicate in its own state. A consumer
deduplicating on an identifier its producers agreed on gets exactly the
property the broker was approximating, at the layer that must implement it
anyway, over the records it actually holds.

**What this costs a producer, stated rather than hidden.** A producer whose
`Append` times out cannot learn whether it landed — this document defines no
query for it — so it chooses between retrying, which may duplicate, and not
retrying, which may lose. Under an at-least-once service the consistent choice
is to retry, and it is the choice the delivery model already assumes on the
consuming side.

**Why it is not the broker's job here.** A broker-side store is a per-append
durable object that outlives the record it describes, so it is bounded by the
append rate rather than by any byte budget: at the §15 defaults the version
that reached this document last would have carried roughly 195 GB per stream,
182× `retained_bytes_limit`'s own default, non-evictable, against a flat rent.
Three attempts to bound it each produced the next fault — a count parameter
363× tighter than the permitted rate, then a shared byte budget whose eviction
lever could not reach the entries and wedged the stream, then a derived figure
no rule enforced. The fourth, a monotone floor, compared a **client** clock
against a **broker** clock and so admitted a duplicate under compliant inputs:
an entry received before the floor still carried a client timestamp up to
`cbqs.max_clock_skew_ms` beyond it, so its retry passed the test its own
eviction was supposed to fail. That defect is resolved by this deletion rather
than by repair, and it is recorded because it is the reason the deletion is
right: the store needed a rule relating two clocks that the protocol does not
relate anywhere else. Deleting the store took the client clock off the record
with it — the field existed to be compared against that floor, and no other
rule in this document reads a client timestamp.

#### 9.2 Consumer groups

Groups are broker-side objects and are not chain state.

```text
GroupConfigV2 {
  version:                  u8,       // 2
  group_id:                 u64,      // broker-assigned, §9.2
  lane_id:                  u64,
  start:                    Head = 1 | At = 2 (sequence: u64),
  visibility_timeout_ms:    u64,
  max_visibility_ms:        u64,
  max_in_flight:            u32,
  max_in_flight_per_holder: u32,
  max_attempts:             u16,
  record_deadline_ms:       u64,
}
```

**Every field is stated; nothing is inherited.** An earlier draft made six of
them options that fell back to per-stream defaults, which bought a client six
fewer values to supply and cost the document an inheritance rule, a
"stores the resolved value" rule, six `Option`s in a signed-adjacent encoding,
and a question about what a later settings change means for an existing group.
There are no per-stream defaults to fall back to: §13 carries five settings
and none of them is a group default. A client supplies all six values on every
`CreateGroup`, and `GetStreamSettings` returns the three governance **ceilings**
those values are validated against (`GroupCeilingsV2`, §11.1) so a client can
choose values that will be accepted rather than discover the bound by being
refused.

`GroupConfigInvalid` is returned, with `details` naming the field, when any of
the rows below is violated — except the `start` row, which returns
`CursorTooOld` carrying the floor, because a client whose start is merely too
old needs the floor to retry with and not the name of a field it set
correctly:

| Field | Rule |
| ----- | ---- |
| `visibility_timeout_ms` | nonzero, `<= max_visibility_ms` of the same record |
| `max_visibility_ms` | nonzero, `<= cbqs.visibility_ms` |
| `max_in_flight` | nonzero, `<= cbqs.max_in_flight_per_group` |
| `max_in_flight_per_holder` | nonzero, `<= max_in_flight` of the same record |
| `max_attempts` | nonzero, `<= cbqs.max_attempts` |
| `record_deadline_ms` | nonzero, `<=` the stream's `retention_ms` (§13) |
| `start = At(sequence)` | **at creation only**: at or after `first_retained_sequence` |
| `start = Head` | resolved at creation to the stream's next sequence |

`UpdateGroup` may change only `visibility_timeout_ms`, `max_visibility_ms`,
`max_in_flight`, `max_attempts`, and `record_deadline_ms`, and re-validates
only those five. The per-holder bound
needs no cross-field re-check because §9.2 applies it as
`min(max_in_flight_per_holder, max_in_flight)` at lease time: lowering the
group bound lowers the effective per-holder bound with it, and raising
`max_in_flight_per_holder` is impossible because the field is immutable. An
earlier draft instead re-validated the cross-field row, which froze
`max_in_flight` for the life of any group created with the two set equal — the
natural "no per-holder restriction" configuration — with no escape but deleting
the group. The `start` row is checked at creation and never again: it is
measured against `first_retained_sequence`, which advances, so re-checking it
would make every `UpdateGroup` on a group created at an older position fail
forever — the group would have to carry the one value the check now refuses —
and the five mutable fields would become unreachable. **`group_id`, `lane_id`,
`start`, and `max_in_flight_per_holder` are immutable**, and an
`UpdateGroup` differing in any of them MUST be rejected with
`GroupConfigInvalid`. Each is immutable for its own reason: `group_id` and
`lane_id` are the record's key; a mutable `start` would let one holder rewind a
shared group and redeliver the whole retained log to every co-tenant, breaking
the monotonicity a group's order depends on: a cycle is created once, when its
record enters the group's range, and is never recreated; and
`max_in_flight_per_holder` is immutable so that the bound is not trivially
edited away by the holder it constrains.

**`Head` resolves once, at creation.** A group created with `start = Head`
takes the stream's next sequence — the first record committed after the atomic
write that created the group (§10) — and stores it as though it had been given
`At(n)`. It does not re-resolve when a subscription opens. A `Cursor`
subscription's `Head` is a different thing and stays that way: a cursor is
ephemeral, so binding it to subscription-open is the only reading available; a
group is durable and outlives every subscription that attaches to it, so
binding it to any subscription would give one holder's connection timing
control over what every co-tenant receives.

**`DeleteGroup` is atomic against everything the group holds.** In the write
that removes the record it drops every delivery cycle of the group, invalidates
every outstanding lease on it, and closes every subscription attached to it
(§10 commits the set as a unit). The cycles are named because they are the
large part: a group created at `At(first_retained_sequence)` holds one cycle per
retained record, and §12's floor — the only other thing that drops a cycle —
cannot reach them once the group they belong to is gone.
Afterwards a lease action naming any of those leases returns `LeaseStale`, not
`GroupNotFound` and not `DeliveryTerminal`: the handle no longer resolves,
which is what `LeaseStale` means, and a client that refetches discovers the
group is gone from its subscription being closed. Leaving it unstated left four
codes reachable for one condition and no rule for a lease whose group had
vanished mid-flight.

Immutability is not a defence against `GROUP_ADMIN` itself: **`GROUP_ADMIN` is
full control of the group**, and a holder that has it can lower
`max_in_flight`, set `record_deadline_ms` to expire the whole backlog, or
delete the group outright. The immutable set prevents accidents and rules out
the one change whose effect on other holders is invisible — a rewound `start`
silently redelivering the retained log. An owner
that does not trust a consumer does not give it `GROUP_ADMIN`.

Broker state keys a group by `(stream_id, lane_id, group_id)`. Creating,
updating, or deleting a group requires `GROUP_ADMIN` scoped to both the group
and its lane. `cbqs.max_groups_per_stream` bounds active groups per stream, and
exceeding it returns `GroupLimitExceeded`.

**`group_id` is assigned by the broker, not chosen by the client**, exactly as
`lane_id` is (§8). A `CreateGroup` body is a `GroupConfigV2` whose `group_id`
MUST be `0` — the request carries the field because it carries the whole
config, and a value the broker is going to overwrite has to be pinned rather
than ignored: "ignored" admits three conforming answers to the same bytes
(assign anyway, reject, require a sentinel), and `0` is outside the assigned
range so it can never collide with a real id. A body naming any other
`group_id` MUST be rejected with `GroupConfigInvalid` naming the field.

`CreateGroup` assigns the next unused integer for that
stream, starting at `1`, and returns it as `GroupCreated` (§11.1); the broker
never reissues one and keeps a
per-stream high-water mark committed in the same atomic write as the group it
assigns (§10).

That single change removes two mechanisms an earlier draft needed and leaves a
third. A client-chosen `group_id` could be deleted and recreated, so the id had
to be protected by a deletion tombstone against a grant scoped to
`Exact(group_id)` authorizing a group it was never issued for, and the
tombstone needed an expiry, which needed a governance parameter. A monotone
counter makes reuse impossible by construction, so neither has anything left to
do. It is the same reasoning §8 already applied to lanes.

**The rate limit is the one that stays.** Group creation is rate-limited per
stream at `cbqs.max_group_creates_per_min`, and a creation over that rate
returns `RateLimited`. The limit bounds how many groups may be created, not how
much each one costs: a group created at `At(first_retained_sequence)` mints a
cycle for every record the stream currently retains, so the expensive create is
the one that starts at the oldest retained record rather than at `Head`. A
provider budgets for the rate times the retained-record count, which is the same
product §6 states for the footprint as a whole. `cbqs.max_groups_per_stream` bounds only groups that are
*active*, so on its own it is defeated by exactly the churn it appears to
prevent: a `GROUP_ADMIN` holder that creates and deletes in a loop never raises
the active count, never reaches the cap, and mints a `GroupConfigV2` and a
high-water-mark bump per cycle — durable writes at no marginal cost under flat
per-stream rent. Assignment from a counter changes none of that; it removes id
*reuse*, not creation *cost*. §8 states the same thing for lanes in the same
words, and an earlier draft of this section dropped the group limit on the
reasoning that the counter had made all three redundant, which was true of two
of them.

Each record is delivered at least once to each applicable group until one
terminal state occurs:

```text
ACKED | EXPIRED
```

**There are two terminal states, and no dead-letter queue.** A record that a
group cannot process — because its attempts ran out, because a consumer
rejected it, or because its deadline passed — reaches `EXPIRED` and stays in
the log, where a `Cursor` subscription replays it by sequence until §12's floor
passes it. An earlier draft added a third state and a `Redrive` verb to drain
it, and the draft itself recorded that the queue could not be *inspected* —
only drained blindly — which left it a second index over bytes the log already
holds, carrying its own TTL, its own governance parameter, its own page limit,
and its own reclamation rule. Deleting it costs a client one thing: a
`Cursor` replay names records by sequence rather than handing back only the
failed ones.

**At-least-once delivery repeats application effects. A consumer MUST make
those effects idempotent, or deduplicate in its own state.** This is the
consuming side of §9.1's decision not to deduplicate on the producing side, and
it is stated here — inside the section that defines delivery, and inside §19
clause 8's conformance surface — rather than only in Security Considerations,
because it is the obligation §9.1's argument rests on.

Delivery creates a lease naming the group, sequence, delivery-cycle id, attempt
number, consumer `holder_signing_key_id`, and expiry. **The lease handle is a
32-byte value the broker MUST choose unpredictably**, so that a co-tenant
cannot name another holder's lease and read the denial codes as an oracle over
its delivery state. `Ack`, `Nack`, `Extend`, and `Reject` MUST name that lease,
and the broker MUST reject a lease action whose session's
`holder_signing_key_id` differs from the one the lease was issued to, with
`LeaseNotYours`. Without that check any holder carrying `ACKNOWLEDGE` could
terminate another holder's in-flight records — `Reject` is terminal in one
request — and under session-level authority the acting identity is no longer on
the wire per request.

**A lease expires.** Its expiry is `visibility_timeout_ms` after issue, and
when it passes without a terminal action the record returns to the group for
redelivery, the attempt counter increments, and the attempt counts toward
`max_attempts` exactly as a `Nack` would. Without that rule a silent consumer
would hold a record's delivery cycle open until §12 evicted the record
underneath it, so every retry a producer was owed would be spent on a consumer
that had stopped answering; with it, `max_attempts` bounds how long silence
costs before the cycle reaches `EXPIRED` and stops consuming the group's
in-flight budget.

**Expiry and `Nack` share one transition, including the last one.** An expiry
that completes the attempt exhausting `max_attempts` transitions the cycle to
`EXPIRED` directly; it does not return the record for a further delivery it has
no attempt left for. At `max_attempts = 1` the first lease's expiry therefore
terminates the cycle, and a second delivery never happens. Stating it removes
the only reading under which the two paths differ.

- `Ack` transitions the cycle to `ACKED`.
- `Nack` ends the attempt and frees the in-flight slot at once, and the record
  becomes deliverable again at `issued_at + visibility_timeout_ms` — the moment
  a silent expiry would have released it — or reaches `EXPIRED` when the
  completed attempt exhausts `max_attempts`. **A record waiting out that
  interval holds no lease and occupies no `max_in_flight` slot**, which is
  stated because the interval is the one point where a cycle is neither leased
  nor terminal: a broker that held the slot would let one holder freeze a group
  for the whole interval with a single round trip, and `Backpressure` would
  then clear on a timer rather than "as other holders drain" (§14).
- `Extend` sets expiry to `min(now + extend_ms, issued_at + max_visibility_ms)`.
- `Reject` transitions directly to `EXPIRED`.
- passage of `record_deadline_ms`, measured from the moment the record's batch
  crossed the durable-commit boundary (§10), transitions the cycle to
  `EXPIRED`.

**`Nack` carries no delay of its own, and it moves no deadline.** It ends the
attempt and frees the in-flight slot at once; the record becomes deliverable
again at `issued_at + visibility_timeout_ms`, which is exactly when it would
have become deliverable had the holder said nothing. So `Nack` and a silent
expiry are one transition with one timing, and answering is never worse for the
group than staying quiet. That is the whole of the backoff the protocol
expresses: a consumer that wants longer raises `visibility_timeout_ms`, where
the value is validated against a ceiling and applies to every holder alike.

Three drafts got this wrong in three different ways, and each failure is the
reason for one clause above. The first let `Nack` carry its own `delay_ms` of
up to an hour while the `Extend` beside it could buy at most thirty seconds —
so the action §6.2 *exempts* from the solvency test bought a hundred and twenty
times the lifetime of the action §6.2 *gates*. The second deleted the delay and
released immediately, which is worse in a way that is easy to miss: a group
orders its cycles by sequence, so a just-released cycle is the lowest
non-terminal sequence and is redelivered **ahead of** everything appended after
it — the `Nack`-redeliver loop then runs at round-trip speed, spends all of
`max_attempts` in milliseconds, and since a record whose attempts run out
reaches `EXPIRED` with no dead-letter path, a consumer retrying the way client
libraries retry destroys the record it was trying to process. The third
deferred from the moment of the `Nack` rather than from the lease's issue,
which reinstated the first failure at a smaller magnitude — a `Nack` at
`issued_at + visibility_timeout_ms - 1` would have held the record for almost
twice the window — and made a prompt `Nack` cost the group more than silence.

**`Extend`'s bound.** `extend_ms` is added to **now**, not to the current
expiry and not to `issued_at`, and the sum is capped at
`issued_at + max_visibility_ms` — anchored to the lease's **original** issue, so
a sequence of extends is bounded in total rather than rolling forward. An
`Extend` whose `now + extend_ms` exceeds that cap is rejected with
`DelayTooLarge` rather than silently clamped. Both operands are stated because
both were ambiguous, and either ambiguity alone lets the same request sequence
succeed on one broker and be refused on another.

**A terminal cycle is reclaimed with its record.** A terminal cycle **MUST** be
dropped once §12's floor has passed the record it names, and MUST NOT be
dropped before. With two terminal states and no dead-letter TTL, the floor is
the only *reclamation* deadline a cycle has — `record_deadline_ms` above ends a
cycle's delivery but does not drop it — so a stream's delivery state cannot
outlive the
records it describes: cycles are bounded by `retained records × applicable
groups` and §6 states that product as part of a provider's exposure.

`MUST`, not `MAY`: an optional reclamation is not a bound, and it is not
externally silent either. A lease naming a dropped cycle returns `LeaseStale`,
and one naming a retained cycle returns `DeliveryTerminal`, so two conforming
brokers would answer the same request differently according to whether either
had collected. `LeaseStale` is the right code for the dropped case — the handle
no longer resolves, and its remedy, refetch, is the correct one.

**A group orders its cycles by sequence, and may lease up to `max_in_flight`
of them at once.** There is no per-group queue counter and no second ordering:
with `Redrive` gone, a cycle is created once, when its record enters the
group's range, and never recreated — so a record's position in the group is its
position in the log. Three earlier drafts needed more than that. A `queue_position`
counter existed because a redriven record had to sort behind records appended
after it while its sequence was still the lowest, and two drafts before that
ordered by `(redrive_epoch, sequence)`, which put a later redrive of a lower
sequence ahead of a cycle that already existed. Deleting redrive deletes the
thing all three were sorting around.

A lease requires room under **both** `max_in_flight` for the group
and `min(max_in_flight_per_holder, max_in_flight)` for the requesting
`holder_signing_key_id`; the first returns `Backpressure`, which clears as
other holders drain, and the second returns `HolderInFlightLimitExceeded`,
which does not.

### 10. Durability and checkpoints

The broker commits appends in **batches, one batch per stream**. A stream's
batch closes when `cbqs.commit_batch_max_ms` elapses with at least one record
buffered, or when `cbqs.commit_batch_max_records` records are buffered, and one
synchronous durable write commits it. An append is acknowledged only after the
batch containing it commits, so a batch is the unit of both durability and
signing. A broker MAY commit several streams' batches in one physical write;
the batch a checkpoint attests is always one stream's. A batch with no records
produces no checkpoint.

```text
CheckpointReceiptV2 {
  version:             u8,       // 2
  chain_instance_id:   Bytes32,
  stream_id:           Bytes32,
  provider:            Address,
  first_sequence:      u64,
  last_sequence:       u64,      // >= first_sequence
  records_digest:      Bytes32,
  previous_checkpoint: Bytes32,
  committed_at_ms:     u64,
  snapshot_height:     u64,      // finalized height, physical
  signature:           Signature,
}

checkpoint_id = keccak256(checkpoint_signing_bytes_v2(receipt))

record_digest(r) =
  keccak256("cbqs/record/v2" || canonical(RecordHeaderV2) || keccak256(payload))

records_digest =
  keccak256(
    "cbqs/records-digest/v2" ||
    record_count_u32_be ||
    record_digest(first) || ... || record_digest(last)
  )
```

**A broker MUST retain a receipt for as long as any record it attests is
retained** (§12). Those bytes count against `retained_bytes_limit` like the
records themselves, so the obligation is bounded by the limit the owner sets.

**There is no server-pushed checkpoint frame.** Three drafts had one: it was
delivered per batch, then bounded to one pending frame per session when that
proved an unbounded write obligation against a peer that grants no credit, and
then paired with `GetCheckpoint` when the bound proved to discard evidence a
client could not recover. Once the pull existed the push had no reader left —
§4.4 step 4 wants the latest checkpoint, a replay wants a sequence to resume
from, and a
client retaining a `checkpoint_id` outside the provider wants the latest; all
three are one `GetCheckpoint` away, and a `Request` is not governed by
subscription credit, so a consumer out of credit reaches its evidence by
asking. Deleting it also removed a credit-exempt-but-not-rate-exempt carve-out
in §13 and the only unsolicited channel by which a lane-scoped holder learned
the whole stream's commit cadence.

Sequences in a batch are contiguous, so `record_count` is `last_sequence -
first_sequence + 1` and is derived rather than carried: a carried copy could
disagree with the range it accompanies. The digest commits that count alongside
the concatenation, so two batches cannot share a digest by regrouping.

**The chain of checkpoints covers the stream without gaps.** For a checkpoint
whose `previous_checkpoint` names another checkpoint of the same stream,
`first_sequence` MUST equal that predecessor's `last_sequence + 1`; for the
first checkpoint of a stream, `previous_checkpoint` MUST be `checkpoint_genesis
= keccak256("cbqs/checkpoint-genesis/v2" || chain_instance_id || stream_id)`
and `first_sequence` MUST be `1` — the first record of the stream (§9.1), since
`0` is the empty-stream sentinel and never names a record. `committed_at_ms`
MUST NOT decrease along the
chain. Without the contiguity rule a provider could link A(1..100) to
B(201..300) and destroy every record between them while a verifier walking the
chain saw unbroken linkage — linkage integrity and coverage integrity have to
be the same check.

**A verifier MUST check both halves, and the linkage half is the digest.** For
each receipt after the first, `previous_checkpoint` MUST equal the
predecessor's `checkpoint_id` (§10). Contiguity on its own is the mirror of the
gap above and fails the same way: a provider that swapped one receipt for
another covering the same range with a different `records_digest` would be
equivocating about what it had committed, and a verifier comparing only
sequences would accept either — both are genuinely signed, because the provider
signed both. The chain is a hash chain or it is a list.

**Verification across a §12 seam.** A broker keeps a receipt only as long as
the records it attests, so a stream older than its own `retention_ms` has no
genesis checkpoint left to serve and a verifier cannot walk to one. Such a
verifier MAY anchor instead on a `checkpoint_id` it has previously verified,
together with that checkpoint's `last_sequence`: linkage and coverage are both
decidable from those two values, and the receipt they name need not still
exist. The `committed_at_ms` and `snapshot_height` orderings compare fields the
discarded receipt carried and are therefore NOT decidable at such a seam; a
verifier that needs them MUST retain the predecessor receipt rather than its
id. A verifier MUST NOT report an empty set of receipts as verified: there is
no proposition in it to be true.

**Signing and key resolution.** The provider signs
`keccak256(checkpoint_signing_bytes_v2(receipt))` with the key whose validity
interval contains `snapshot_height`, and `snapshot_height` MUST be a finalized
height the provider actually read and MUST NOT decrease along the chain.

A verifier resolves the key from the provider record by the same interval:

```text
snapshot_height >= signing_key_since                        -> signing_key
snapshot_height >= previous_signing_key_since
    and previous_signing_key present                        -> previous_signing_key
otherwise                                                   -> no key resolvable
```

The previous key's **lower** bound is what makes the third row reachable. An
earlier draft carried the previous key with no lower bound, so the second row
swallowed every earlier epoch: a receipt honestly signed two rotations ago
resolved to the previous key, failed the signature check, and was adjudicated
*invalid* — which §10 elsewhere calls "signed evidence of provider
misbehaviour". That is worse than the conflation it replaced, because its
polarity accuses an honest provider.

That gives three verdicts, and they are decidable rather than merely named:

- a key resolves and the signature verifies — **valid**;
- a key resolves and the signature does not verify — **invalid**, and the
  verifier MUST reject it; and
- no key resolves, because the receipt predates the rotation before last —
  **unverifiable**, which the verifier MUST report as such rather than as
  invalid.

An earlier draft had only the current key, so a verifier saw the same predicate
in the invalid and the unverifiable case and could not tell them apart, and the
"invalid" verdict was unreachable in the only verifier the document describes.
It also left a provider unable to sign anything during the window between a
rotation committing and its next finalized read.

The third verdict is not eliminated, only bounded: two rotations still put a
receipt beyond the registry's reach, and a client that wants one to stay
checkable retains the key alongside it. That is the disclosed limit of on-chain
key history, and Security Considerations states what it costs.

A verifier MUST also reject a receipt whose `provider` is not the `provider`
the stream record names.

The atomic durable commit set for one batch is: the record bytes, the assigned
sequences, the checkpoint receipt,
and the checkpoint-chain tail. For delivery mutations it is the delivery state
and the lease state. For a lane creation it is the `LaneRecordV2` and the
stream's lane high-water mark (§8); for a group creation it is the
`GroupConfigV2` and the stream's group high-water mark (§9.2). Both counters
are named beside the record they assign, and for the same reason: a crash
between the record and its counter lets the next create reissue an id, and a
grant scoped `Exact(n)` would then authorize an object it was never issued for
— the failure broker assignment exists to make impossible. Recovery MUST replay
or roll back an incomplete transaction as a unit. A broker MUST NOT acknowledge
an append, expose a sequence, or serve a record whose batch has not committed.

**What a checkpoint establishes.** A subscriber holding every record of a range
recomputes `records_digest` and compares it with the signed value; a mismatch,
a break in linkage, a gap in coverage, or two different checkpoints naming one
range is signed evidence of provider misbehaviour. Three limits are stated
rather than implied: a subscriber holding only some lanes cannot verify a
checkpoint at all, because the digest is flat over the batch; no consensus
state anchors any checkpoint chain, so a provider serving two divergent chains
is detectable only by a party holding both branches, and clients that need this
SHOULD retain their latest `checkpoint_id` outside the provider; and no
availability guarantee follows, because v2 has no dispute or slashing path.

### 11. Transport and flow control

#### 11.1 Frames

The transport is a bidirectional TLS WebSocket carrying binary frames. One
WebSocket message contains exactly one canonical frame: `version: u8 = 2`, a
one-byte frame tag, then the canonical body. A frame larger than
`cbqs.max_wire_frame_bytes` MUST be rejected before allocation, and every
variable-length field inside a frame MUST be capped **at decode** by its §15
bound — in particular a grant's `Set` lane scope at
`cbqs.max_lane_ids_per_grant` — rather than only at verification, so an
unauthenticated peer cannot make the broker allocate or verify beyond those
bounds.

```text
CbqsClientFrameV2 =
  1 OpenSession     { request_id: Bytes32, grant: StreamGrantV2 } |
  2 CompleteSession { request_id: Bytes32, proof: SessionProofV2 } |
  3 Request         { request_id: Bytes32, session_id: Bytes32, body: CbqsRequestBodyV2 }

CbqsServerFrameV2 =
  1 SessionChallenge { request_id: Bytes32, session_id: Bytes32,
                       challenge: Bytes32, expires_at_ms: u64 } |
  2 SessionOpened    { request_id: Bytes32, session_id: Bytes32,
                       snapshot_height: u64 } |
  3 Response         { request_id: Bytes32, body: CbqsResponseBodyV2 } |
  4 Delivery         { session_id: Bytes32, subscription_id: Bytes32,
                       lease: Option<Bytes32>, header: RecordHeaderV2,
                       sequence: u64, payload: Bytes } |
  5 Error            { request_id: Option<Bytes32>, code: u16, message: Bytes, details: Bytes } |
  6 DeliveryState    { session_id: Bytes32, subscription_id: Bytes32,
                       state: DeliveryStateBodyV2 }

CbqsResponseBodyV2 =
  1 Appended        { sequence: u64 } |
  2 LaneCreated     { lane_id: u64 } |
  3 Lanes           { lanes: Vec<LaneRecordV2>, next_after_lane_id: Option<u64> } |
  4 Settings        { settings: StreamSettingsV2, group_ceilings: GroupCeilingsV2 } |
  5 Checkpoint      { receipt: CheckpointReceiptV2 } |
  6 Group           { config: GroupConfigV2 } |
  7 GroupCreated    { group_id: u64 } |
  8 Ok

GroupCeilingsV2 {
  max_visibility_ms:    u64,     // cbqs.visibility_ms
  max_attempts:         u16,     // cbqs.max_attempts
  max_in_flight:        u32,     // cbqs.max_in_flight_per_group
}

DeliveryStateBodyV2 =
  1 CreditShort     { needed_bytes: u32 }
```

`Delivery.lease` is present for a `Group` subscription and absent for a
`Cursor` one, which takes no leases (§11.2); it is an `Option` rather than a
zero sentinel because §4 forbids the sentinel. `DeliveryStateBodyV2` has one
variant. An earlier draft also carried a `Terminal` variant reporting a cycle's
terminal state, and no rule anywhere produced it — the consumer that acts on a
cycle already gets a `Response`, and no other consumer needs to be told.
`CreditShort` is how §11.2's broker reports the head frame's size, and it names
no group because a `Cursor` subscription has none.

`message` is a human-readable summary of the code's own condition. It MUST NOT
carry implementation internals — store errors, file paths, query text, stack
frames — and MUST NOT vary with the existence or state of an object the acting
session is not authorized to observe; without that, `Internal` (§14) is an
oracle over exactly the state the denial-precedence rule works to hide.
`message` and `details` MUST each be at most `cbqs.max_error_message_bytes`;
`details` is empty unless §14 defines a payload for the code. A connection MUST
hold at most `cbqs.max_sessions_per_connection` open sessions and
`cbqs.max_subscriptions_per_connection` open subscriptions; exceeding the first
returns `SessionLimitExceeded`.

Two deadlines bound an unauthenticated peer, and both are needed. Every
outstanding challenge MUST be closed after `cbqs.session_handshake_timeout_ms`
— per challenge, so a second session cannot hold one open indefinitely — **and
a connection that has completed no session within that same interval of its
**first byte** MUST be closed.** Without the second, a peer that sends
`OpenSession` frames and never completes one holds zero sessions and zero
subscriptions, so every other per-connection bound is satisfied at zero while
each frame still costs the broker a grant verification before any proof of
possession.

The deadline runs from the first **byte**, not the first frame. Separately, and
for the life of the connection rather than only before its first session, a
broker MUST close any connection whose partially-received message has not
completed within `cbqs.session_handshake_timeout_ms` of **that message's** first
byte. The
second rule is scoped to the whole connection because the hold it closes is not
a handshake hold: a peer that completes one session satisfies every
pre-authentication bound permanently and can then hold a partial message on
every connection it opens. Both halves are stated because a WebSocket message can be
fragmented indefinitely: a peer that opens a connection, begins a message and
sends one byte completes no frame, so a frame-anchored deadline never starts
while the broker accumulates reassembly buffer up to
`cbqs.max_wire_frame_bytes` — the "reject before allocation" rule of this
section can only fire once a frame is known to *exceed* the cap, and a length
that is never declared never exceeds it.

`CbqsRequestBodyV2` is a tagged union whose tag is the method tag below:

| Tag | Method | Body | Required verb | Response |
| --: | ------ | ---- | ------------- | -------- |
| 1 | `Append` | `AppendRequestV2` (§9) | `APPEND` | `Appended` |
| 2 | `CreateLane` | `{}` | `LANE_ADMIN` with `lane_scope = Any` | `LaneCreated` |
| 3 | `CloseLane` | `{ lane_id: u64 }` | `LANE_ADMIN` | `Ok` |
| 4 | `ListLanes` | `{ after_lane_id: Option<u64>, limit: u32 }` | `CONSUME`, `REPLAY`, or `LANE_ADMIN` | `Lanes` |
| 5 | `CreateGroup` | `GroupConfigV2` (§9.2), `group_id = 0` | `GROUP_ADMIN` with `group_scope = Any` | `GroupCreated` |
| 6 | `UpdateGroup` | `GroupConfigV2` | `GROUP_ADMIN` | `Ok` |
| 7 | `DeleteGroup` | `{ lane_id: u64, group_id: u64 }` | `GROUP_ADMIN` | `Ok` |
| 8 | `OpenSubscription` | `{ subscription_id: Bytes32, source: SubscriptionSourceV2 }` | `CONSUME` or `REPLAY` | `Ok` |
| 9 | `CloseSubscription` | `{ subscription_id: Bytes32 }` | — | `Ok` |
| 10 | `Credit` | `{ subscription_id: Bytes32, bytes: u32 }` | — | `Ok` |
| 11 | `Ack` | `{ lease: Bytes32 }` | `ACKNOWLEDGE` | `Ok` |
| 12 | `Nack` | `{ lease: Bytes32 }` | `ACKNOWLEDGE` | `Ok` |
| 13 | `Extend` | `{ lease: Bytes32, extend_ms: u64 }` | `ACKNOWLEDGE` | `Ok` |
| 14 | `Reject` | `{ lease: Bytes32 }` | `ACKNOWLEDGE` | `Ok` |
| 15 | `SetStreamSettings` | `StreamSettingsV2` (§13) | `STREAM_ADMIN` | `Ok` |
| 16 | `GetStreamSettings` | `{}` | `CONSUME`, `REPLAY`, `GROUP_ADMIN`, `LANE_ADMIN`, or `STREAM_ADMIN` | `Settings` |
| 17 | `GetCheckpoint` | `{ checkpoint_id: Option<Bytes32> }` | `CONSUME` or `REPLAY` | `Checkpoint` |
| 18 | `GetGroup` | `{ lane_id: u64, group_id: u64 }` | `CONSUME` or `GROUP_ADMIN` | `Group` |

`GetGroup` returns the stored `GroupConfigV2`, or `GroupNotFound`. It exists
because `UpdateGroup` carries a whole config and MUST be rejected when any of
the four immutable fields differs from the record: without a read path, a
holder newly granted `GROUP_ADMIN` cannot construct an acceptable `UpdateGroup`
at all, and would have to be handed the full configuration out of band — which
contradicts `GROUP_ADMIN` being full control of the group.

`GetCheckpoint` returns the named checkpoint's `CheckpointReceiptV2`, or the
stream's latest when `checkpoint_id` is absent; an id that is not a checkpoint
of this stream, or one whose records are no longer retained, returns
`CheckpointNotFound`. A broker MUST resolve the id by a bounded index lookup,
not by scanning history.

A replay does not take a checkpoint id. An earlier draft let `Cursor` start at
one, and never said whether that meant the checkpoint's `first_sequence` or its
`last_sequence + 1` — two brokers replayed disjoint ranges from the same
cursor — while drawing two codes for one condition, `CursorTooOld` from §11.3
and `CheckpointNotFound` from here, which §14's precedence leaves unordered. A
client that wants to replay from a checkpoint reads its `last_sequence` here
and passes `At(last_sequence + 1)`. **`At(n)` delivers from sequence `n`
inclusive**, and `Head` delivers only records committed after the subscription
opens — stated because an explicit number is explicit only once its own
resolution is fixed, which is the defect that removed the checkpoint start.

Unknown method tags are invalid and return `UnknownMethod`.

`ListLanes.limit` MUST be at least one and at most
`cbqs.max_lane_list_page`; a value
outside that range returns `PageLimitInvalid` rather than being clamped, so a
client never has to guess whether its page size was honoured. `ListLanes` is
the only paged method version 2 has: an earlier draft also paged a dead-letter
queue, whose page bound existed because each page did durable work, and §9.2
deleted the queue.

**A subscription belongs to the session that opened it.** `CloseSubscription`,
`Credit`, and every `Delivery` for it MUST name a `subscription_id` opened
under the acting session; a request naming another session's subscription MUST
be rejected with `SubscriptionNotYours`, even on the same connection. Without
that rule a holder with an `APPEND`-only grant could open a second session on
one connection and close, or exhaust the credit of, a consumer's subscription.

#### 11.2 Subscriptions and credit

```text
SubscriptionSourceV2 =
  1 Cursor { lane_scope, start: Head = 1 | At = 2 (u64) } |
    // At(n) delivers from sequence n inclusive; Head delivers only records
    // committed after the subscription opens.

  2 Group  { lane_id: u64, group_id: u64 }
```

A `Cursor` subscription replays a lane scope from a position and takes no
leases; it needs `REPLAY` (or `CONSUME` for `Head`). A `Group` subscription
attaches the session to a consumer group and receives leased deliveries under
§9.2; it needs `CONSUME`. **Without this second form the entire lease model
would be unreachable from the wire**, which is what an earlier draft of this
document got wrong: `Ack` and its siblings name a lease that nothing could
produce.

A subscription's lane scope MUST be inside the grant's lane scope, and a
`Group` subscription's `(lane_id, group_id)` MUST be inside both the grant's
lane and group scopes.

A subscription carries a credit balance in encoded frame bytes. The broker MUST
NOT send a **`Delivery`** frame whose encoded length exceeds the remaining
credit, and MUST decrement the balance by exactly that length. `DeliveryState`
and `Response` are outside the credit balance — neither gated by it nor
decremented from it — and are bounded by §13's `max_delivered_bytes_per_sec`
alone. Binding the rule to the frame class rather than to the word "delivery"
is what makes `CreditShort` sendable: it is a `DeliveryState` frame of 71
bytes, and a credit-gated reading would forbid it in exactly the condition it
exists to report. `Credit` adds `bytes` to the balance, saturating at
`cbqs.max_subscription_credit_bytes`; a `Credit` that would exceed it saturates
rather than wrapping, and the balance is a `u64` so no sequence of `u32`
additions can overflow it. An uncapped credit would disable the mechanism Goal
3 rests on.

A subscription is a **slow consumer**, and MUST be closed with `SlowConsumer`,
when its credit has been at least the encoded size of the next pending delivery
for `cbqs.slow_consumer_timeout_ms` and it has accepted nothing in that window.
The condition is stated over credit sufficient for the head frame, not over
credit in general: a client whose credit is merely smaller than the next frame
is complying, and closing it would punish the party doing the right thing. When
credit is below the head frame's size the broker MUST report that size in a
`DeliveryState` frame carrying `CreditShort { needed_bytes }` (§11.1).

Scheduling across subscriptions on one connection MUST be starvation-bounded: a
subscription with sufficient credit and pending records MUST be served within
`cbqs.max_subscriptions_per_connection` scheduling rounds.

#### 11.3 Replay

A `Cursor` start behind the retention floor returns `CursorTooOld` (§12). A
replay MUST deliver in ascending sequence within each lane, and each
`Delivery` frame it sends is bounded by `cbqs.max_wire_frame_bytes` like every
other frame (§11.1) and by the subscription's remaining credit (§11.2).

An earlier draft instead bounded "the bytes it materializes per response".
Neither half survived: bytes materialised inside a broker are not chain state,
a wire byte, or a response, so the rule was outside the observability the
Specification preamble claims and outside §19's lettered exceptions; and
`Response` is a frame class that carries no records, so the bound named the
wrong frame. What the draft was reaching for is already true of `Delivery`, so
this section states no bound of its own.

#### 11.4 Dialing policy

A client MUST determine the destination address of every connection it makes —
by resolving a name, or by reading an address literal straight out of the URI —
and MUST NOT connect unless that address passes the check below. Stating it
over the destination rather than over "addresses it resolves" matters because
§4.1 admits `wss://127.0.0.1/ws` and `wss://[::1]/ws`: both are byte-legal, and
neither involves a resolution step at all.

The destination MUST NOT be loopback, unspecified, link-local, unique-local,
multicast, or in a private or special-use range. Concretely, a client MUST
reject any IPv4 destination in
`0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`, `127.0.0.0/8`, `169.254.0.0/16`,
`172.16.0.0/12`, `192.0.0.0/24`, `192.0.2.0/24`, `192.88.99.0/24`,
`192.168.0.0/16`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, or
`224.0.0.0/3`, and any IPv6 address outside `2000::/3` or inside `2001::/23`,
`2001:db8::/32`, `2002::/16`, `3ffe::/16`, or `3fff::/20`.

It MUST connect only to a destination it checked, resolving once if it resolves
at all and never re-resolving between check and connect. **A name that resolves
to more than one address is checked in full**: every address in the result MUST
pass, and a client MUST NOT fall back to a member of the result it did not
check. The singular reading was a real hole, because every mainstream resolver
returns a list and every mainstream connector races or falls back across it — a
name answering with one public address and `::1` reaches loopback on a client
that checked only the first.

**A client MUST NOT follow a redirect.** An earlier draft required the check to
be re-applied after each one, which is strictly weaker than it appears: the
check constrains the address range and nothing else, so an open redirect
anywhere on a provider's edge sends the client to any publicly routable host
the attacker names — and the client has no provider identity to compare the new
peer against, since §7's handshake authenticates only the client. Refusing
redirects is one rule instead of the three (same-host, same-scheme, bounded
count) that constraining them would need, and a provider that wants to move an
endpoint updates its registry record, which is the mechanism §4.1 already
provides.

The check is a client obligation because only the client knows what it is about
to connect to; consensus admission (§4.1) cannot, and does not try to.

### 12. Retention

The retention floor advances past a record when **either** bound is reached:

- `retention_ms` has elapsed since the broker committed it, or
- keeping it would put the stream above `retained_bytes_limit`, in which case
  the floor advances over the oldest records until it would not.

`retained_bytes_limit` is measured over the stream's **evictable** footprint:
its records and the checkpoint receipts attesting them (§10). A receipt is
discarded when the floor has passed the **last** record it attests, which is
§10's rule read from this side; a floor landing inside a batch is ordinary at
these defaults, and the two sections would otherwise disagree about whether the
receipt's bytes are still counted — which moves the floor itself, so two
brokers would refuse different `Cursor` replays from the same append stream. Receipts belong inside it because they are per-batch and
outlive nothing. At one record per batch — the worst case, reached at any
append rate at or below `1000 / cbqs.commit_batch_max_ms`, 50 records/s at the
§15 defaults — a 245-byte receipt runs about six times a 41-byte record, so
a limit that did not see them would bound a seventh of what the stream stores.
The ratio falls as the rate rises, because batches close on a fixed clock while
records do not.

Time or size, whichever comes first — the same pair every log broker exposes.
`retention_ms` is therefore a guaranteed window only while the stream stays
inside its byte budget, and §13's two settings together are what a client can
rely on for replay.

**The clock is the broker's, not the client's.** A record's retention is
measured from the moment its batch crossed the durable-commit boundary (§10),
which the broker records for itself. `RecordHeaderV2` carries no client
timestamp for it to be measured from instead (§9), and that is the durable form
of this rule: under a client-supplied clock a producer backdates its own
records and evicts them early, which is the window this section exists to
guarantee.

Two earlier drafts got this wrong in opposite directions, and the pair is worth
recording. The first made the floor's advance conditional on every consumer
group being terminal or past its deadline: vacuous for a stream with no groups,
so a broker could evict the instant a record was appended and `Cursor` replay
(§11.2) had nothing to work with. The second removed the group condition and
made time the only trigger, which turned `retained_bytes_limit` into a hard
ingest ceiling — at the §15 defaults a producer at its own permitted rate fills
1 GiB in seventeen minutes and then stalls for the remaining seven days, a
sustained rate 591 times below what its own settings allow. A window bounded by
time alone cannot also be bounded by bytes.

The group condition is gone for good, and is not needed in either direction:
§9.2 caps `record_deadline_ms` at the stream's `retention_ms`, so a group can
neither shorten the window nor outlast it.

**Eviction is a terminal transition.** When the floor passes a record, every
non-terminal delivery cycle for it transitions to `EXPIRED`. That is what lets
the byte bound evict without contradicting §9.2's "delivered at least once
to each applicable group until one terminal state occurs": the cycle reaches a
terminal state, and the state is `EXPIRED`.

`retention_ms` and `retained_bytes_limit` are read at eviction, so a
`STREAM_ADMIN` holder that lowers either (§13) shortens the window for records
already retained — which §13 states plainly as the most destructive thing that
verb can do.

Behind the floor the broker returns `CursorTooOld`, whose `details` carry the
current `first_retained_sequence` as a big-endian `u64`. The floor is monotone
per stream and never regresses.

A `Closed` stream, or one whose owner account is suspended, retains data at the
provider's discretion; a provider SHOULD state its policy in its registered
metadata. This is not consensus state.

### 13. Broker-side stream settings

```text
StreamSettingsV2 {
  version:                     u8,      // 2
  retention_ms:                u64,
  retained_bytes_limit:        u64,
  max_message_bytes:           u32,
  max_append_bytes_per_sec:    u64,
  max_delivered_bytes_per_sec: u64,
}
```

Five settings, one per resource the provider actually spends. The six
`default_*` fields an earlier draft carried existed only to feed group-config
inheritance, and §9.2 removed the inheritance.

A holder with `STREAM_ADMIN` sets these through `SetStreamSettings`, which
replaces the whole record. Each field is validated against the ceiling below on
every change — enumerated rather than stated as a rule, because the ceilings do
not share one naming convention — and a stream with no explicit settings uses
those parameters' defaults. Every field MUST be nonzero. A violation returns
`SettingsInvalid` with `details` naming the field; `SettingsRejected` is
reserved for a provider declining settings it will not serve, so a client can
tell an invalid value from a policy refusal.

| Setting | Ceiling |
| ------- | ------- |
| `retention_ms` | `cbqs.retention_ms` |
| `retained_bytes_limit` | `cbqs.max_retained_bytes_per_stream` |
| `max_message_bytes` | `cbqs.max_message_bytes` |
| `max_append_bytes_per_sec` | `cbqs.max_append_bytes_per_sec` |
| `max_delivered_bytes_per_sec` | `cbqs.max_delivered_bytes_per_sec` |

**Ceilings are admission-time only.** A stream stores its resolved settings and
a group stores its config, and no request path re-reads a §15 ceiling to bound
an already-accepted value. A governance write constrains the next
`SetStreamSettings` or `CreateGroup` and never retroactively narrows a stream
that is already running.

`GetStreamSettings` returns the settings **and the §15 ceilings a client needs
to build a valid `GroupConfigV2`** — `cbqs.visibility_ms`, `cbqs.max_attempts`,
and `cbqs.max_in_flight_per_group` — in the `Settings` response body (§11.1).
§9.2 made every group field mandatory, so the client has to know its bounds,
and a client holding only a grant has no other way to learn them. The ceiling
on `record_deadline_ms` is the stream's own
`retention_ms`, which the same frame already carries in `settings`; repeating
it among the ceilings would be a second copy of one value in one frame, with no
rule for a client that finds the two different — the pattern §10 and §6.1 both
refuse elsewhere.

Settings are broker state: they need no chain transaction and do not change
what the owner is charged.

A provider MUST enforce the accepted settings. `max_append_bytes_per_sec` and
`max_delivered_bytes_per_sec` are enforced as rate limits returning
`RateLimited`, not as silent drops. Both are **per stream, in aggregate over
every session and every connection acting on it**. A grant's own
`max_append_bytes_per_sec` is a second bound at a different scope — per
`holder_signing_key_id`, across that holder's sessions — and a request must
pass both; §7 check 7's "lower of the two" is the holder's bound, not a
replacement for the stream's. Without the aggregate scope one holder key
opening sessions on many connections multiplies the rate.

`max_append_bytes_per_sec` is measured over the **encoded `AppendRequestV2`**
of every append the broker decodes, accepted or refused, charged before the
admission checks. Charging refusals is what stops the limit defeating itself:
a producer over its rate would otherwise pay nothing to stay over it, since
everything it sends is refused and a refusal that costs nothing is free. It is
measured over the request alone, because the request is the only part of the
exchange whose size is fixed by the encoding — an `Error`'s `message` is
implementation-chosen prose bounded only above, so metering the reply would make
the admitted append rate depend on how verbose a broker's diagnostics are.

**The two ceilings are related, and §13 does not enforce the relation.** Each
setting is validated against its own §15 row and never against the other, so an
owner may configure a stream whose permitted ingest cannot be delivered. Two
couplings matter, and they behave differently.

The first is fan-out, and it is inherent. One accepted append produces one
`Appended`, then per applicable group one `Delivery` and one `Ok` answering the
`Ack` that terminates it. Writing `A` and `D` for the two settings, `n` for the
payload and `G` for the number of groups, one delivery attempt per record needs

```text
D  >=  (G x (152 + 35 + n) + 43) / (12 + n)  x  A
```

which at the §15 defaults (`D / A = 4`) admits no groups at all below a 61-byte
payload, one from 61 bytes, two from 185, three from 556, and never four — the
bound approaches 4 asymptotically without reaching it, against a
`cbqs.max_groups_per_stream` of 4,096. It is a lower bound in a second sense
too: `max_attempts` permits up to that many delivery attempts per record per
group, so a stream whose consumers retry needs a multiple of it. No accounting
rule removes this coupling, because the bytes are the service the stream exists
to provide. The excess does not accumulate — §12's floor evicts, and eviction
transitions every non-terminal cycle for an evicted record to `EXPIRED` — but a
client sees records expire that it never saw delivered.

The second is refusals, and it is not inherent. A refused append is answered
with an `Error` of up to `45 + cbqs.max_error_message_bytes` bytes — 2,093 at
the defaults — against a 12-byte request, inside `D`, at whatever rate `A`
permits — so a holder with
`APPEND` alone and a bad `lane_id` can exhaust a stream's delivered budget
without exceeding its own, starving every consumer on the stream. An earlier
draft moved an append's reply into `A` to close this, which closed it and broke
three other things: it metered `Error.message`, whose width no rule fixes, so
two brokers admitted different append rates from one setting; it charged the
refusal announcing exhaustion to the budget already exhausted, while the stop
rule that ends the delivered budget's exhaustion was not carried across; and the
reply cannot exist until its batch commits, so two brokers charged it to
different one-second windows. What closes it without those is a bound on the
reply rather than a change of budget, and this document does not have one: a
provider that needs it caps `message` far below the `Codec` ceiling, which costs
diagnostics rather than correctness.

What a provider needs from this section is the inequality; what an owner needs
is that `cbqs.max_groups_per_stream` is a ceiling on groups rather than a
promise of delivery to them.

`max_delivered_bytes_per_sec` is measured over `Delivery`, `DeliveryState`,
`Response` and `Error` — four of §11.1's six server frames, an append's reply
included. `SessionChallenge` and `SessionOpened` are outside it because they
are answered at most once per `OpenSession`, and §11.1's two handshake
deadlines close the connection of a peer that opens sessions it never
completes. `Error` is inside, with one exception: the single `RateLimited`
frame reporting that the budget is exhausted is sent outside the budget, one
per session per exhaustion **and at most one per connection per exhaustion**,
after which the broker serves that session no further bytes **from this budget
until the budget refills**. It does not close the session. Both caps are stated
because only the second bounds the exemption: sessions are capped per
connection, connections are not capped at all (§11.1), so a per-session cap
alone scales with a quantity this protocol does not bound. An earlier draft had it stop serving the session outright, which
punished whichever session was next to be served rather than whichever had
consumed the budget — the ceiling is per stream, so under contention that is
routinely a different holder. Exempting the whole class instead
would have handed any holder an unmetered egress channel at roughly sixty times
amplification — and handed it first to a suspended account, which §6.1
guarantees pays the provider nothing, inverting the incentive §6.2 rests on.
Scoping the limit to `Delivery` alone would leave `Response` outside every
*rate* bound — it is bounded per frame, by `cbqs.max_wire_frame_bytes` and by
the page caps, but not per second — and `Response` is the amplifying class: a
`ListLanes` at `cbqs.max_lane_list_page` answers an 80-byte request frame with
a page two orders of magnitude larger, on a verb a read-only holder has.

**Lowering a setting is destructive, and the document does not pretend
otherwise.** A reduction of `retention_ms` advances the eviction horizon, and
§12's floor then moves past everything older — one request can discard nearly
the whole retained log, because the only constraint on the new value is that it
be nonzero and within the ceiling. A reduction of `retained_bytes_limit` below
current usage does the same through §12's other bound: the floor advances until
the stream fits, so the excess is discarded at once rather than aging out.

An earlier draft offered "MUST NOT prune below the floor already published to
clients" as the mitigation. That clause forbids nothing: pruning only ever
advances the floor, and §12 already makes the floor monotone, so it restated an
existing property and read as a protection. The real mitigation is that
`STREAM_ADMIN` is a separate verb bit an owner need not issue, which Security
Considerations says plainly.

### 14. Typed errors

**§14 is the broker's wire-error table.** Every code below is something a
broker returns to a client over §11's transport. Chain-instruction rejections
are not in it: a rejected `SYS_CBQS` instruction fails its transaction and
surfaces through the platform's ordinary execution-error path, so enumerating a
CBQS wire code for each of the roughly fifteen refusals §4–§6 state would
invent a second, parallel error channel that nothing reads. An earlier draft
gave exactly one of them a wire code, which implied the other fourteen were
oversights rather than a scope boundary.

All bindings expose stable typed errors.

**Version 2 renumbers freely.** Activation is a reset (§18), so no version-1
client exists to be broken, and several codes below carry conditions version 1
gave to different ones. Stating a stability property the
document does not have is worse than not having it, so the claim is withdrawn
rather than repaired. Within version 2 the numbers are fixed.

| Code | Name | Returned when |
| ---: | ---- | ------------- |
| 3901 | `StreamNotActive` | the snapshot's stream status is `Closed` |
| 3902 | `AccountSuspended` | the owner's account fails the §6.2 solvency test |
| 3903 | `MalformedFrame` | a frame, or a field inside it, failed to decode under §4's canonical rules, or exceeded a `Codec`-class §15 bound. Four decode failures have their own codes and are excluded: a version byte other than `2` returns `UnsupportedVersion`, an unknown request method tag returns `UnknownMethod`, a frame over `cbqs.max_wire_frame_bytes` returns `FrameTooLarge`, and a payload over the stream's stored `max_message_bytes` returns `MessageTooLarge`. `request_id` is absent exactly when the frame is shorter than 34 bytes — `version` and the frame tag precede it at a fixed offset in every client frame (§11.1), so its readability is a property of the input and not of how far a particular decoder happened to get |
| 3904 | `AuthorizationGenerationStale` | the grant's generation is not the snapshot's |
| 3905 | `SessionNotOpen` | the `Request`'s `session_id` is not open on the connection that sent it |
| 3906 | `InvalidGrant` | signature, context, scope, or bound check failed |
| 3907 | `GrantExpired` | outside the grant's time window |
| 3908 | `VerbDenied` | the grant lacks the verb the method requires |
| 3909 | `GroupScopeDenied` | the group is outside the grant's group scope |
| 3910 | `InvalidProofOfPossession` | the session proof failed any §7 check |
| 3912 | `MessageTooLarge` | payload above the effective `max_message_bytes` |
| 3913 | `RateLimited` | a §13 rate limit, or §8's lane-creation or §9.2's group-creation rate, was exceeded; retry with backoff |
| 3914 | `Backpressure` | group capacity exhausted; clears as other holders drain, so retry with backoff |
| 3917 | `CursorTooOld` | start behind the retention floor; `details` = floor, u64 BE |
| 3918 | `GroupNotFound` | no such `(stream_id, lane_id, group_id)` |
| 3919 | `LeaseStale` | the lease is unknown or superseded; refetch and retry |
| 3920 | `DeliveryTerminal` | the cycle already reached a terminal state |
| 3921 | `LeaseNotYours` | the lease was issued to a different holder key; do not retry |
| 3922 | `HolderInFlightLimitExceeded` | the group's `max_in_flight_per_holder` for this holder |
| 3923 | `SubscriptionNotYours` | the subscription belongs to another session |
| 3924 | `UnknownMethod` | unknown request method tag |
| 3925 | `ChainStateStale` | `OpenSession` against a snapshot older than `cbqs.max_snapshot_age_blocks` |
| 3926 | `LaneNotFound` | no such lane |
| 3927 | `LaneClosed` | append to a closed lane |
| 3928 | `LaneScopeDenied` | the lane is outside the grant's lane scope |
| 3929 | `LaneLimitExceeded` | `cbqs.max_lanes_per_stream` reached |
| 3930 | `LaneNotClosable` | `CloseLane` named lane `0` |
| 3931 | `SessionLimitExceeded` | `cbqs.max_sessions_per_connection` reached |
| 3932 | `SubscriptionLimitExceeded` | per-connection subscription bound reached |
| 3933 | `FrameTooLarge` | frame above `cbqs.max_wire_frame_bytes` |
| 3934 | `GroupLimitExceeded` | `cbqs.max_groups_per_stream` reached |
| 3935 | `DelayTooLarge` | an `Extend` that would advance expiry past `issued_at + max_visibility_ms` |
| 3936 | `LaneScopeTooNarrow` | `CreateLane` from a grant whose lane scope is not `Any` |
| 3937 | `UnsupportedVersion` | a version byte other than `2` |
| 3938 | `ChainInstanceMismatch` | the object's `chain_instance_id` is not the broker's |
| 3939 | `GroupConfigInvalid` | a §9.2 group-config rule was violated; `details` names the field |
| 3940 | `Internal` | the broker failed for a reason no other code in this table describes; `details` is empty, and a client MUST NOT retry it on a fixed schedule |
| 3941 | `GroupScopeTooNarrow` | `CreateGroup` from a grant whose group scope is not `Any` |
| 3948 | `SlowConsumer` | the §11.2 slow-consumer condition |
| 3949 | `SettingsRejected` | the provider declines to serve these §13 settings |
| 3950 | `StreamUnknownToBroker` | the stream is absent from the broker's snapshot |
| 3951 | `CheckpointNotFound` | a `GetCheckpoint` naming an id that is not this stream's, one whose records are no longer retained, or an absent id on a stream that has no checkpoint yet |
| 3952 | `SettingsInvalid` | a §13 settings rule was violated; `details` names the field |
| 3954 | `PageLimitInvalid` | a `ListLanes` limit outside its §11.1 bound |

**Denial precedence.** When a request fails checks in more than one of the
classes below the broker MUST return a code from the earliest class: unknown
method (`UnknownMethod`), then malformed frame (`MalformedFrame`), then session
binding (`SessionNotOpen`), then verb (`VerbDenied`), then scope
(`LaneScopeDenied`, `GroupScopeDenied`), then object state
(`LeaseNotYours`, then `LeaseStale`, then `DeliveryTerminal`, then
`AccountSuspended`, then the not-found codes). Each class names the code it
returns, because an ordering over classes with no code in one of them orders
nothing — and the object-state class is ordered internally for the same reason
lease handles are unpredictable (§9.2): a broker that answered
`DeliveryTerminal` before `LeaseNotYours` would tell a holder naming someone
else's handle what state that delivery is in. Codes outside those classes — capacity, rate, size, and
version refusals — are unordered relative to them and to each other.

An `Extend` that would advance expiry past `issued_at + max_visibility_ms` is
**rejected with `DelayTooLarge`** rather than silently clamped, so a client
never has to guess whether its request was honoured. The remedy is to send a
smaller number.

`StreamNotFound` (3900) has no version-2 condition: the broker decides from a
snapshot, so "the registry does not have this stream" and "this stream is
absent from my snapshot" are one observation, which `StreamUnknownToBroker`
states without implying the broker read the chain at request time. The numbers
absent from the table above — 3900, 3911, 3915, 3916, 3942 through 3947, 3953,
and 3955 — are
unassigned in version 2 and MUST NOT be emitted, and so is every number outside
the 3901–3954 band
: an implementation that reports a wire failure as 3998
because this document gave it no code is emitting an unassigned number.

Unknown future errors MUST be preserved by clients as `(code, message,
details)` rather than mapped to success or retry.

### 15. Parameters and bounds

All decode-time collection and byte lengths MUST be capped before allocation,
and the cap applied at decode is the row's **Maximum**, never its deployed
value. An instruction body carries two: the endpoint vector's `u32 count`,
capped at `cbqs.max_provider_endpoints`' Maximum of 8, and each endpoint URI's
`u32 length`, capped at `cbqs.max_provider_endpoint_uri_bytes`' Maximum of
2,048. Capping at the deployed value instead would make the same payload decode
on one deployment and fail on another.

The **deployed** value of either is then an admission check (§4.1 rules 1 and
2), which is a different disposition and a different charge: a 9-endpoint
payload fails decode and is charged nothing, while a 5-endpoint payload on a
Default-4 deployment decodes, reaches its row, and is rejected with its row's
reservation charged. `cbqs.max_provider_endpoint_uri_bytes` has Default equal
to Maximum, so for that row the two coincide and only the decode cap can fire.

This applies to **input arriving over the wire**, not to records already in
state: a stored `ProviderRecordV2` decodes at whatever length it was admitted
with, so lowering `cbqs.max_provider_endpoints` stops new registrations without
making live providers unreadable.

**Read by** says who reads the parameter: `C` a consensus handler, `B` the
broker. **Class** says when a change reaches an object:

- **Admission** — read when an instruction or a request is admitted; the
  accepted object never consults it again.
- **Ceiling** — bounds a value at the moment it is set (§13 settings, §9.2
  group config). The accepted record stores its resolved value, so a later
  write never redefines an existing stream or group. §13 states this as a rule,
  not only as a class.
- **Runtime** — read continuously by the broker; a change takes effect with the
  snapshot that carries it. Only operational limits live here.
- **Codec** — a decode bound whose Maximum equals its Default, so a conforming
  decoder accepts the same byte strings on every deployment. Maximum equals
  Default does not by itself make a row immovable; the rule below does that.

**A `Codec` row is not governance-writable at all.** A governance write naming
one MUST be rejected. That is the whole of the protection, and it is the half
that does work: §18 rule 1 seeds every row at its Default, so genesis cannot
produce a divergent Codec value in the first place and a genesis check for one
would never fire. An earlier draft had only a genesis check and then placed the
Codec rows outside Tier 0, which left the four rows whose whole purpose is
that every deployment reads them alike as the only rows with no post-genesis
protection. Lowering `cbqs.max_wire_frame_bytes` or
`cbqs.max_lane_ids_per_grant` on a running network splits decoders and
invalidates outstanding grants.

Every CIP-12 change to any other `cbqs.*` row is Tier 0.

| Parameter | Default | Minimum | Maximum | Read by | Class |
| --------- | ------- | ------- | ------- | ------- | ----- |
| `cbqs.stream_rate_per_block` | 80,000 | 10,000 | 10,000,000 | C | Admission |
| `cbqs.max_streams_per_account` | 1,024 | 0 | 16,384 | C | Admission |
| `cbqs.max_provider_endpoints` | 4 | 0 | 8 | C | Admission |
| `cbqs.max_provider_endpoint_uri_bytes` | 2,048 | — | 2,048 | C | Codec |
| `cbqs.snapshot_refresh_ms` | 2,000 | 1 | 60,000 | B | Runtime |
| `cbqs.max_snapshot_age_blocks` | 300 | 0 | 3,000 | B | Runtime |
| `cbqs.max_clock_skew_ms` | 60,000 | 0 | 300,000 | B | Admission |
| `cbqs.max_grant_ttl_ms` | 86,400,000 | 1 | 604,800,000 | B | Admission |
| `cbqs.session_handshake_timeout_ms` | 10,000 | 1 | 60,000 | B | Runtime |
| `cbqs.max_sessions_per_connection` | 64 | 0 | 1,024 | B | Runtime |
| `cbqs.max_message_bytes` | 262,144 | 1 | 1,048,576 | B | Ceiling |
| `cbqs.max_retained_bytes_per_stream` | 1,073,741,824 | 1 | 1,099,511,627,776 | B | Ceiling |
| `cbqs.max_append_bytes_per_sec` | 1,048,576 | 1 | 1,073,741,824 | B | Ceiling |
| `cbqs.max_delivered_bytes_per_sec` | 4,194,304 | 1 | 4,294,967,296 | B | Ceiling |
| `cbqs.retention_ms` | 604,800,000 | 1 | 2,592,000,000 | B | Ceiling |
| `cbqs.visibility_ms` | 30,000 | 1 | 900,000 | B | Ceiling |
| `cbqs.max_attempts` | 10 | 1 | 100 | B | Ceiling |
| `cbqs.max_in_flight_per_group` | 4,096 | 1 | 65,536 | B | Ceiling |
| `cbqs.max_groups_per_stream` | 4,096 | 0 | 65,536 | B | Admission |
| `cbqs.max_lanes_per_stream` | 16,384 | 0 | 65,536 | B | Admission |
| `cbqs.max_lane_ids_per_grant` | 4,096 | — | 4,096 | B | Codec |
| `cbqs.max_lane_creates_per_min` | 256 | 0 | 4,096 | B | Runtime |
| `cbqs.max_group_creates_per_min` | 64 | 0 | 1,024 | B | Runtime |
| `cbqs.max_lane_list_page` | 256 | 1 | 1,024 | B | Admission |
| `cbqs.max_subscriptions_per_connection` | 256 | 0 | 4,096 | B | Runtime |
| `cbqs.max_subscription_credit_bytes` | 67,108,864 | 0 | 4,294,967,296 | B | Runtime |
| `cbqs.max_wire_frame_bytes` | 5,242,880 | — | 5,242,880 | B | Codec |
| `cbqs.max_error_message_bytes` | 2,048 | — | 2,048 | B | Codec |
| `cbqs.slow_consumer_timeout_ms` | 60,000 | 1 | 900,000 | B | Runtime |
| `cbqs.commit_batch_max_ms` | 20 | 1 | 1,000 | B | Runtime |
| `cbqs.commit_batch_max_records` | 1,024 | 1 | 65,536 | B | Runtime |

**A governance write below a row's Minimum MUST be rejected**, on the same
path and with the same disposition as one above its Maximum. A `Codec` row is
not governance-writable at all, so its Minimum is written `—`.

The Minimum is not a policy preference. It is the value below which some OTHER
rule in this document becomes unsatisfiable, or the broker spins, or it
destroys data:

- `cbqs.retention_ms` at 0 is the sharpest. §13 gives a stream that never set
  its own settings the parameter's value, and §12 then treats every record as
  past its retention — so one write deletes the entire retained window on every
  such stream, chain-wide and irreversibly.
- `cbqs.visibility_ms`, `cbqs.max_attempts` and `cbqs.max_in_flight_per_group`
  at 0 make §9.2's group config unsatisfiable in both directions at once — the
  field must be greater than zero AND no greater than the parameter — so no
  group can be created or updated again.
- `cbqs.max_message_bytes`, `cbqs.max_retained_bytes_per_stream`,
  `cbqs.max_append_bytes_per_sec` and `cbqs.max_delivered_bytes_per_sec` at 0
  do the same to §13's settings.
- `cbqs.max_grant_ttl_ms` at 0 admits no grant, since §7 check 5 requires
  `not_before_ms < expires_at_ms`. `cbqs.session_handshake_timeout_ms` at 0
  expires every challenge before it can be answered.
  `cbqs.max_lane_list_page` at 0 admits no valid page size, since §11.1 rejects
  a limit of 0 and a limit above the parameter alike.
- `cbqs.snapshot_refresh_ms`, `cbqs.slow_consumer_timeout_ms`,
  `cbqs.commit_batch_max_ms` and `cbqs.commit_batch_max_records` at 0 make a
  loop spin or fire on every pass.

Where 0 merely DISABLES an operation it stays admissible, and those rows carry
a Minimum of 0. Freezing new streams, sessions, lanes, groups or subscriptions
is a governance choice this table leaves open, and so is tolerating no
staleness (`cbqs.max_snapshot_age_blocks`) or no clock skew
(`cbqs.max_clock_skew_ms`) at all.

`cbqs.stream_rate_per_block`'s Minimum of 10,000 is the floor that was stated
here in prose; it is now the table's, so a floor is read from one place. It
MUST also be **a multiple of 10** after any governance write — that rule is
separate, and stays in prose because the table has no column for it. Neither
needs a genesis clause: §18 rule 1 seeds every parameter at its default, and
the default is 80,000.

Every row's Default is inside its own `[Minimum, Maximum]`, so §18's genesis
state is admissible under the rule above.

The multiple-of-10 requirement is what makes `provider_share` **exact** on
`settle`'s full branch, where `paid = elapsed * rate_per_block *
active_streams` and so `paid * 9000 / 10000` divides evenly. Without it, the
truncation is per call, and `SettleAccount` is permissionless: anyone could
settle every block and move up to one nano-CBY per call from the provider to
the Platform Fee Account. With it, N settlements across an interval pay the
provider exactly what one would, and the only residue left anywhere is the
partial branch's single truncation below. A floor rather than merely nonzero,
because §6.1's `provider_share = paid * 9000 / 10000` truncates toward zero,
and is zero exactly when `paid <= 1` nano-CBY, with the Platform Fee Account
taking all of it. The **full** branch has no loss to bound — the multiple-of-10
rule above makes `provider_share` exact there. The floor is for the **partial**
branch, where `paid` is whatever the balance holds, so a final `paid` of 1
nano-CBY pays the provider nothing. That residue is one nano-CBY per insolvency
event and does not compound, because reviving the account costs the owner at
least `rate_per_block * active_streams`.

Four protocol constants are deliberately not parameters, because a governance
write that set any of them to zero would price permanent state at nothing or
pay a provider nothing for service already rendered:

```text
CBQS_STREAM_CREATION_CHARGE       = 5_000_000_000   // nano-CBY (§6.3)
CBQS_PROVIDER_REGISTRATION_CHARGE = 5_000_000_000   // nano-CBY (§4.1)
CBQS_MIN_ACCOUNT_TOPUP            = 1_000_000_000   // nano-CBY (§4.3)
CBQS_PROVIDER_BPS                 = 9_000           // (§6.1)
```

#### 15.1 State-I/O reservations and metered cost

Each instruction reserves exactly this many state reads and writes before its
first admission check, and therefore before its first state operation. The
pairs are protocol constants: they do not vary with stream shape, balance,
branch, or any other observed state, and changing one is a consensus change
requiring a new document version.

| Instruction | Reserved reads | Reserved writes |
| ----------- | -------------: | --------------: |
| `RegisterProvider` | 6 | 3 |
| `UpdateProvider` | 4 | 1 |
| `CreateStream` | 11 | 8 |
| `UpdateStream` | 2 | 1 |
| `CloseStream` | 7 | 6 |
| `TopUpAccount` | 7 | 5 |
| `SettleAccount` | 5 | 4 |
| `WithdrawAccount` | 6 | 5 |

Every handler reads the Stream Registry singleton. Each row is then exactly:

| Instruction | Reads beyond the singleton | Writes |
| ----------- | -------------------------- | ------ |
| `RegisterProvider` | `cbqs.max_provider_endpoints`, `cbqs.max_provider_endpoint_uri_bytes`, the provider record, the caller's native account, the zero address | the provider record, the caller's native account, the zero address |
| `UpdateProvider` | `cbqs.max_provider_endpoints`, `cbqs.max_provider_endpoint_uri_bytes`, the provider record | the provider record |
| `CreateStream` | `cbqs.max_streams_per_account`, `cbqs.stream_rate_per_block`, the provider record, the account record, the stream slot, the custody account, the provider's native account, the Platform Fee Account, the owner's native account, the zero address | the stream record, the account record, the provider record, the custody account, the provider's native account, the Platform Fee Account, the owner's native account, the zero address |
| `UpdateStream` | the stream record | the stream record |
| `CloseStream` | the stream record, the account record, the custody account, the provider's native account, the Platform Fee Account, the provider record | the stream record, the account record, the provider record, the custody account, the provider's native account, the Platform Fee Account |
| `SettleAccount` | the account record, the custody account, the provider's native account, the Platform Fee Account | the account record, the custody account, the provider's native account, the Platform Fee Account |
| `TopUpAccount` | the provider record, the account record, the custody account, the provider's native account, the Platform Fee Account, the payer's native account | the account record, the custody account, the provider's native account, the Platform Fee Account, the payer's native account |
| `WithdrawAccount` | the account record, the custody account, the provider's native account, the Platform Fee Account, the owner's native account | the account record, the custody account, the provider's native account, the Platform Fee Account, the owner's native account |

Five of the eight carry settlement's reads and writes, because §6.1 makes every
account-touching instruction settle: `CreateStream`, `CloseStream`,
`TopUpAccount`, `SettleAccount`, and `WithdrawAccount`. `TopUpAccount` reads
the provider record as well, to reject a top-up naming a provider that does not
exist (§4.3). Only `RegisterProvider`, `UpdateProvider`, and `UpdateStream`
touch no CBQS account and carry none of it; `RegisterProvider` still reads and
writes two native accounts for its §4.1 charge.

**Only `CreateStream` reads `cbqs.stream_rate_per_block`**, because §6.1 lets
only the `0 → 1` transition write the account's rate. `settle` multiplies by
the rate the account already stores, so no other instruction consults the
parameter. That correspondence is the metered form of §6.1's rule: an
instruction reads the parameter if and only if it can start a billing run.

**Every branch of every instruction is charged exactly its reserved reads and
writes. A successful branch also performs them; a rejected branch persists
none of them and is still charged for all of them, and what it actually
performed before failing is informational (below).**

Handlers perform their credit writes unconditionally, including for a zero
amount and on `settle`'s suspended branch; a handler that deletes the account
record under §4.3 performs a state write in place of the update it would
otherwise have made. A handler that skipped a zero-value write, or took a
cheaper path on one branch, would meter differently from one that did not, and
the two would disagree on gas in the same block. This is why the artifact below
pins several branches of one instruction at identical read, write, and cell
counts.

**Cycles may still differ between branches, and only for one reason**: the
payload. Cycles include `cycles_per_instruction_byte ×
decoded_instruction_bytes`, so a branch whose payload omits an optional field
is cheaper by exactly those bytes. Every instruction whose payload is not
fixed-width has branches that differ this way, and the property is read off
`decoded_instruction_bytes` rather than off which structs contain an `Option`:
`RegisterProvider` and `UpdateProvider` vary through `endpoints`, a vector
whose two pinned `RegisterProvider` paths are 8,183 cycles apart — the largest
spread in the artifact — and `WithdrawAccount`, `UpdateProvider`, and
`UpdateStream` vary through their options. What must not vary is the
**state-I/O**, which is the part an implementation could take a cheaper path
through.

The exact metered cost is pinned as a normative artifact beside this document
as `cip-39-gas-vectors-v2.json` (schema `cowboy.cbqs.gas-vectors.v2`), covering
every branch §19 item 2 names. A conforming implementation MUST reproduce every
pinned field of every vector, measured against that artifact, whose SHA-256 is
`e097e2a3b49af0b6bd0b949853c52fd43757c8a6d44b2c9387926c23f023907a` after
CIP-43 removes the state-read/write cycle terms and adds the Access total.

Every vector is reachable under the genesis parameter set §18 rule 1 mandates;
none requires a prior governance write.

The artifact's `constants` block reproduces the platform's implemented metering
schedule — including a flat per-state-write Cell charge — rather than restating
CIP-3 §2.2.2's per-byte `state_set` model, which describes PVM host metering.
CIP-39 does not redefine either; reconciling them is CIP-3's to do.

The vectors are CBQS-dispatch scoped: each covers this document's decode
surcharge plus its handler's hash, signature, and state-I/O work, and excludes
the transaction base, intrinsic calldata cells, the outer system-instruction
dispatch, and event emission (§16), all of which the platform levies uniformly
outside this family. Hash charging is `max(1, ceil(len / 32))` words.

**A rejected instruction is charged its reservation.** `charged_reads` and
`charged_writes` equal the table above on **every** path, successful or
rejected: the reservation is taken before the first admission check and is
never refunded, and a handler computes every hash its row names before that
first check — excepting only the §4.2 singleton read that the stream-id
derivation's own inputs depend on, which §18 rule 1 makes unreachable at
genesis. So a rejected instruction is charged the handler base, its decoded
instruction bytes, its reservation, and its hash words, and differs from the
successful path only in what it persists — nothing, and no event (§16).

**A payload that does not decode is not a CBQS charge at all.** A `SYS_CBQS`
payload whose version byte is not `2`, whose tag is outside 1–8, or whose body
violates §4's canonical encoding fails **transaction** decode: the instruction
is a field of the transaction, not a length-prefixed blob a handler unwraps, so
the enclosing transaction never reaches CBQS dispatch and this document charges
it nothing. That is the platform's uniform decode path, which §15.1 already
excludes. An earlier draft of this section pinned a charge for the case, on the
premise that a handler would see it; §18 rule 3 has always said **decoder**,
and rule 2 now agrees.

§15 draws the decode/admission boundary these charges rest on: a payload over a
row's **Maximum** fails decode and is charged nothing, and one within the
Maximum but over the **deployed** value decodes, reaches its row, and is
charged that row's reservation. Without a single statement of that boundary the
same instruction has two charges depending on which side an implementation puts
a parameter, which is the free variable this section exists to remove.

**On a rejected path, `state_reads` and `state_writes` are informational.**
Only `reserved_*`, `charged_*`, `cycles`, and `cells` are normative there. This
document fixes only the orderings whose outcome depends on them — §5's rate
write before `CreateStream`'s balance guard, §5.1's settlement before a
provider's close, and §6.1's `settle` → effects → `finalize`. It fixes no order
among the remaining admission checks, so how far a particular rejection got
before failing is an implementation's choice; pinning it would impose a check
order through the back door. What is pinned is that the charge does not depend
on which check fired.

The rule is stated because fees on a failed transaction are consensus-visible
on this platform, so an unpinned rejection path is a free variable in the state
root, not merely undocumented. An earlier draft left it unpinned on the ground
that a rejected instruction persists nothing; that covers state and not gas. On
a rejected `CreateStream` the two readings — charge the reservation, or charge
only I/O actually performed — differ by up to 2,700 cycles and 8,000 cells on a
transaction anyone can submit deliberately. The artifact carries one rejection
vector per instruction, so the rule is tested rather than asserted.

### 16. Chain events

Events are consensus data: emitter, topic, payload bytes, and order commit
through `logs_root` into `receipt_root`. The topic is the exact UTF-8 byte
string shown; the payload is exactly the one fixed-length identifier listed.

| Instruction | Topic | Payload |
| ----------- | ----- | ------- |
| `RegisterProvider` | `cbqs.provider.registered` | 20-byte provider address |
| `UpdateProvider` | `cbqs.provider.updated` | 20-byte provider address |
| `CreateStream` | `cbqs.stream.created` | 32-byte `stream_id` |
| `UpdateStream` | `cbqs.stream.updated` | 32-byte `stream_id` |
| `CloseStream` | `cbqs.stream.closed` | 32-byte `stream_id` |
| `TopUpAccount` | `cbqs.account.credited` | 20-byte owner address |
| `SettleAccount` | `cbqs.account.settled` | 20-byte owner address |
| `WithdrawAccount` | `cbqs.account.debited` | 20-byte owner address |

**Each successful instruction emits exactly one event — the one on its row —
and a rejected instruction emits none. The emitter is the Stream Registry
system actor `0x17` for every row**, not the transaction sender and not the
account named in the payload, whatever account authorized the instruction. The
address is stated rather than derived from a rule like "the actor that owns the
state", because six of the eight handlers also write native accounts at
`0x00…00`, `0x18`, and the provider's own address, and a derivable rule
reopens the question for each of them. Leaving the emitter unstated is a
consensus divergence of the same kind §4.1 and §4.3 close for their seeded
record fields: it is committed data no instruction body carries, so two
implementations choosing differently produce different `logs_root` and
therefore different `receipt_root` while metering identically.

v2 publishes **levels, not edges**, and states the consequence rather than
claiming they are equivalent. A consumer that needs to know whether a
particular settlement suspended or revived an account diffs
`CbqsAccountV2.suspended` across the blocks around the event; two settlements
for one account in one block leave the intermediate state unobservable. Events
carry no amounts, so provider revenue, platform accrual, and the two burns —
the stream creation charge and the provider registration charge — are
reconstructed from balance deltas rather than from the log. An edge-and-amount
event set is a follow-up CIP if indexers need one. The event payload carries
the owner rather than the `(owner, provider)` pair because a 20-byte payload is
the established shape; a consumer that needs the provider reads the account
record at that block.

### 17. Operational surface

`cbqsd` exposes `/statusz` (CIP-42 self-report, where answering at all is the
liveness signal), `/readyz`, and `/metrics` in Prometheus text form.

`/readyz` reports **only data-plane readiness**: 503 when the store is
unwritable, or when the commit loop has stopped making progress while records
wait. **A failed registry snapshot refresh MUST NOT make the broker unready**,
and neither MUST a refresh loop whose last *success* is old — only one whose
last *attempt* is, since §3 requires the broker to keep attempting and to keep
serving from the snapshot it has. An earlier draft said "a background loop has
stopped heartbeating", which reaches the snapshot refresher and so selected the
opposite behaviour from the sentence beside it, according to whether an
implementer read a loop's heartbeat as its last attempt or its last success.

A readiness probe that failed on chain reachability would have a load balancer
remove exactly the brokers still able to serve. That argument holds while the
snapshot is inside `cbqs.max_snapshot_age_blocks`; past it the broker refuses
every `OpenSession` with `ChainStateStale` (§3) and serves only its established
sessions, which is still service and still worth routing to, but an operator
draining such a broker for new connections is acting on the `/metrics` snapshot
age rather than on `/readyz`. The condition is reported as a `/statusz` degraded
flag and a `/metrics` gauge carrying the age of the current snapshot.

### 18. Activation and removed surface

**Document version 2 supersedes version 1 in full.** CBQS has never activated
on a network whose state must be preserved, and version 1 already specified
itself as reset-only and pre-launch. Activation is a reset, not a migration:

1. genesis MUST seed the Stream Registry singleton and every §15 parameter at
   its **default**, MUST reject a parameter set carrying any `cbqs.*` key §15's
   table does not name, MUST NOT seed any other record under `0x17`, and MUST
   leave
   `0x17` holding no native balance. Stating
   the rule exhaustively rather than naming version-1 artifacts is what keeps
   it checkable from this document alone — a validator does not need to know
   what version 1 stored in order to reject it. Seeding defaults also satisfies
   §15's `cbqs.stream_rate_per_block` floor and its `Codec`-class equality, so
   neither needs a genesis check of its own; both bind governance writes, which
   is where §15 states them;

2. a decoder MUST reject a `SYS_CBQS` payload whose version byte is not `2`;

3. a decoder MUST reject any stored CBQS record whose version byte is not `2`;
   and
4. a broker store MUST carry a store-format marker `cbqs/store-format = 2`
   written at creation, and a version-2 `cbqsd` MUST refuse to open a store
   whose marker is absent or not `2` rather than attempt a partial decode. The
   marker's **key** is `cbqs/store-format` and its value is the single
   character `2`; a store carrying a version marker under any other key has not
   satisfied this rule, whatever that marker says. The key is pinned because a
   deployed version-1 store writes its marker under a different key entirely,
   so a version-2 broker opening one finds the pinned key **absent** and
   refuses — the disposition this rule wants, and the one a rule stated only
   over "a version marker" would have left to the implementation. The marker,
   not a per-record byte, is what covers the durable broker state this document
   describes without giving it a canonical struct — leases, delivery cycles,
   both high-water marks, and the checkpoint-chain tail.

An operator MUST NOT carry version-1 chain state, broker stores, or archived
snapshot bundles across this boundary.

**No retired-key list.** An earlier draft enumerated the thirty-two version-1
governance keys genesis must reject, so that a validator could be built from
this document alone — and then conceded that the list itself was not: it was
derived from the deployed key set union the version-1 document's table, and a
genesis carrying a key neither source recorded would have passed it.

Rule 1 closes the world instead. A parameter set carrying any `cbqs.*` key §15
does not name is rejected, which is checkable from §15 alone, strictly stronger
than any enumeration, and cannot go stale. It rejects `cbqs.rent.burn_bps` —
which version 1's text never named but its implementation carried, paired with
`cbqs.rent.provider_bps` by a sum-to-10,000 invariant — without anyone having
had to know that.

**No removed-surface table.** An earlier draft mapped each version-1 object to
its version-2 disposition, in thirty-one rows. It was a changelog: no rule
cited it, and every right-hand cell restated a section that states the same
thing normatively. Rule 1 already answers the only question an upgrading
implementation has — activation is a reset, so the left column is *everything*
— and rules 2 through 4 reject every version-1 byte mechanically.

### 19. Conformance

An implementation conforms when:

1. it admits and rejects exactly the chain instructions §4–§6 define, with the
   state effects §6 defines and the events §16 defines. That clause is total,
   and deliberately not expanded into a checklist: an earlier draft enumerated
   fourteen cases under it, which added nothing a harness could not derive and
   drifted from the body twice. Two classes are worth naming because no gas
   vector can catch them — the initial field values §4.1, §4.2 and §4.3 state
   for a created provider record, stream record, and account, since two
   implementations seeding them differently meter identically; and the
   `settle` → effects → `finalize` order of §6.1, which a test distinguishes
   by closing the last stream of a suspended account whose balance is exactly
   zero and observing that the record is deleted;
2. it reproduces every **pinned** field of every vector in
   `cip-39-gas-vectors-v2.json` under the genesis parameter set of §18 rule 1
   with no prior governance write — a `null` on a rejection path is not a value
   to reproduce (§15.1) — and meters every branch of every instruction with
   identical **charged** reads, charged writes, and cells, including `settle`'s
   full, partial, and suspended branches and the §4.3 account deletion, cycles
   differing only by payload bytes;

3. its canonical encodings round-trip through `cowboy-protocol-codec::cbqs`,
   reject every version byte other than `2` on every object §4 gives one, and
   produce byte-identical signing preimages for the three §4 registry entries;
4. its broker accepts and rejects sessions and requests as §7 and §11 define —
   continued service for established sessions across a simulated chain outage
   (§3); refusal of `OpenSession` past `cbqs.max_snapshot_age_blocks`;
   single-use `session_id`/`challenge`; rejection of a `session_id` not open on
   the sending connection; **termination on grant expiry with no snapshot
   refresh**; and termination under a revoked grant on the first request served
   after the observing refresh;
5. it refuses a lease action from any holder other than the one the lease was
   issued to; refuses a
   subscription request naming another session's subscription; evaluates lane
   and group scope by intersection on every operation including lease actions;
   refuses `CreateLane` from a grant whose lane scope is not `Any` with
   `LaneScopeTooNarrow`, and `CreateGroup` from one whose group scope is not
   `Any` with `GroupScopeTooNarrow`; redelivers a lease that expires without a
   terminal action **while an attempt remains** and counts that attempt toward
   `max_attempts`, terminating instead on the expiry that exhausts them (§9.2);
   rejects an
   `UpdateGroup` that differs in any immutable field; and returns codes
   consistent with §14's denial precedence;
6. its broker acknowledges no append before the batch containing it is durable;
   every acknowledged record still retained appears in a checkpoint a client
   holding `CONSUME` or `REPLAY` can **retrieve** with `GetCheckpoint` (§11.1)
   and whose `records_digest` recomputes;
   `first_sequence` equals the predecessor's `last_sequence + 1` along
   the whole chain and `1` at `checkpoint_genesis` (§9.1), and
   `previous_checkpoint` names the predecessor's `checkpoint_id` at every step; `snapshot_height` and
   `committed_at_ms` do not decrease; a receipt whose `provider` is not the
   stream's is rejected; and §10's three verdicts are each reachable and correctly
   assigned — a receipt in the current key's window verifies as **valid**, one in
   the previous key's window verifies against `previous_signing_key` and is
   equally **valid**, one whose resolved key rejects the signature is **invalid**,
   and one below `previous_signing_key_since` is reported **unverifiable** rather
   than rejected;
7. its client rejects every destination in §11.4's list — for an address
   literal read straight out of the URI as well as for a resolved name, and
   for every address a name resolves to and not only the first — refuses to
   follow a redirect at all, and connects only to a destination it checked;

8. its broker's externally observable behaviour is the behaviour §6.2 and
   §§7–17 define. That clause is total, and clauses 4 through 7 name classes
   under it rather than exhausting it, for the reason clause 1 gives: an
   enumeration here adds nothing a harness cannot derive from the body, and the
   one this clause replaces had already fallen behind the body it was
   enumerating. The range names §6.2 explicitly and runs to §17 because both
   ends were once outside it: §6.2 is broker behaviour sitting in a chapter of
   chain instructions, and §17's readiness rules were reachable by no clause at
   all. Three classes under it are worth naming because a passing
   implementation can differ without a client observing it on a healthy path —
   §6.2 evaluated on **every** append and every state-creating request rather
   than only at session open, which a test distinguishes by suspending an
   account mid-session; §6.2's converse, that `Ack`, `Reject`, and `Nack` stay
   available while an account is suspended and that no *new* lease is issued
   while it is, which an over-refusing broker would otherwise satisfy
   vacuously; and §9.2's reclamation of an `ACKED` or `EXPIRED` cycle, which is
   a `MUST` precisely because a broker that never reclaims answers a late `Ack`
   with `DeliveryTerminal` where a conforming one answers `LeaseStale`; and

9. a crash injected on either side of a durable write boundary leaves, after
   restart, either no record and no acknowledgement, or the record, its
   sequence, and its checkpoint together (§10); and
10. its activation satisfies §18: a genesis that seeds the singleton and every
   §15 parameter at its default and nothing else under `0x17`, holds no native
   balance there, and carries no `cbqs.*` key §15 does not name; a handler that
   rejects a `SYS_CBQS` payload whose version byte is not `2`; a decoder that
   rejects a stored record whose version byte is not `2`; and a broker that
   refuses to open a store without `cbqs/store-format = 2` under that key. Each
   is a boundary an implementation can be run against, and none of them is
   proved by the gas vectors, the structural checks, or the system-address
   invariant — those pass equally against a version-1 deployment.

**The requirements below are not externally observable, and are stated as
operator obligations rather than as protocol guarantees.** The Specification
preamble claims observability for everything else; these are the exceptions,
lettered so they are not confused with the numbered conformance items above:

- **A.** the broker's snapshot-refresh attempt interval (§3);
- **B.** the truthfulness of the `snapshot_height` it reports in
  `SessionOpened` (§3) — a broker on a year-old snapshot can report the current
  height, because the frame carries no proof and none is defined;
- **C.** that it evaluates one request from one snapshot rather than mixing
  heights (§3);
- **D.** that `session_id`, `challenge`, and lease handles are chosen
  unpredictably (§7, §9.2) — a counter is indistinguishable from entropy on the
  wire, and the anti-replay and anti-oracle arguments rest on it;
- **E.** that lane ids and group ids are never reissued (§8, §9.2), which
  depends on durable state a client cannot inspect. It is lettered rather than
  dropped because a grant scoped to `Exact(lane_id)` or `Exact(group_id)` rests
  on it, and a broker that reissues an id hands an old grant authority over a
  new object without emitting a byte that differs;
- **F.** the starvation bound in §11.2, whose "scheduling round" has no wire
  representation; and
- **G.** that a checkpoint's `snapshot_height` is a height the provider
  actually read (§10). It selects the verification key, so misstating it
  downward moves a batch into an earlier key's window — where the receipt still
  verifies, since the provider holds that key too. What bounds the move is
  `previous_signing_key_since`, below which a receipt stops resolving to any
  key at all. Monotonicity bounds it only from the other side — a provider can
  never stamp a batch below where its own chain already stands — and does not
  constrain later batches, which remain free to return to the current height;
  and
- **H.** that an oversized frame is rejected **before allocation** and that
  every variable-length field is capped **at decode** rather than at
  verification (§11.1). The refusal is observable — it has a code and a
  response — but its position relative to allocation and signature verification
  produces the same response bytes either way, so a client cannot tell a
  conforming broker from one that allocates first and refuses second; and
- **I.** that the broker does not interpret payload bytes (§9) — one that
  parses, indexes, or routes on them emits identical frames, identical chain
  state, and identical codes, so §2's confidentiality framing rests on an
  obligation no probe can distinguish; and
- **J.** that an application whose data must survive its provider keeps its own
  durable copy (§2) — an obligation on a party outside this protocol, which no
  chain state, wire byte, or broker response can evidence.

A client needing assurance about any of them reads the chain itself or measures
the broker's behaviour over time. Grant expiry — which elapses without the
broker's cooperation — is the one bound that holds regardless, which is why §3
and Security Considerations rest the revocation argument on it.

## Security Considerations

**Session authority.** Requests inside a session are not individually signed;
each carries the `session_id` it executes under and the broker binds it to that
session's grant (§7). An attacker who can inject into an established TLS
session acts with that session's authority until it closes. Version 1's
per-request signatures did not defend against that attacker either — they were
bound to a session id, so the same injection position permitted reordering and
dropping — and cost a signature and a verification per message. What version
1's counter did provide, and what §7 replaces explicitly, is the binding of a
request to its session: a single-use `session_id`/`challenge` pair scoped to
one connection, a handshake deadline per challenge, and a per-request
`session_id` the broker checks. Holder keys SHOULD be workload-scoped and
grants SHOULD carry the shortest practical expiry.

**Revocation and the only bound that holds.** Chain authority reaches an
established session at the next successful refresh; during a chain outage it
does not reach it at all. `cbqs.max_snapshot_age_blocks` stops a broker from
admitting *new* sessions against stale state, but a broker that lies about its
snapshot age cannot be caught from the wire (§19). The bound that does not
depend on the broker's cooperation is **grant expiry**: it elapses on its own,
§7 requires termination on it, and `cbqs.max_grant_ttl_ms` caps what an owner
can issue. An owner rotating a key away from a compromised holder MUST treat
that expiry as the guarantee and everything else as best effort.

**`STREAM_ADMIN` outlives its grant.** Bit 7 changes broker state, not session
state: a holder that lowers `retention_ms` discards nearly the whole retained
log in one request, and one that lowers `retained_bytes_limit` below current
usage discards the excess just as immediately, through §12's other bound.
Either reduction is a deletion that takes one request. Revoking the grant
restores neither. There is no mitigation inside the verb — §13 says why the
clause an earlier draft offered was vacuous — so the mitigation is not issuing
it: it is a separate bit, and a consumer's grant has no reason to carry it.

`LANE_ADMIN` is the same shape one verb over, and is disclosed here for the
same reason: it closes any lane inside its scope, v2 defines no reopen, and at
`lane_scope = Any` that is every lane but 0. It is a permanent-denial power
over the lanes it names, mitigated only by not issuing the bit.

**A narrow lane scope is a claim on the future.** Lane ids come from a counter
(§8), so `Exact(7)` authorizes whatever lane becomes the seventh, not a lane a
particular party created. §7 keeps that from compounding by requiring
`lane_scope = Any` to create a lane at all, so a narrowly scoped holder cannot
mint the ids its own scope names; but an owner issuing `Exact` or `Set` over
ids that do not exist yet is pre-authorizing part of the namespace and SHOULD
name only lanes that already exist.

**Billing exposure.** Rent accrues on chain and is collected by permissionless
settlement, so a provider that never settles carries the unpaid remainder as
credit risk, and §6.3 writes off whatever the balance could not cover. Because
§4.3 keys the account by `(owner, provider)`, that exposure is **isolated**: no
other provider's settlement can reach the balance this one is owed from, and
the bound is the service rendered since the broker's snapshot was last
refreshed, which `cbqs.max_snapshot_age_blocks` caps. That is why §6.3 states a
cadence and why §6.2's solvency test is evaluated per request rather than per
session — a session established while solvent would otherwise keep appending
for the whole life of its grant against an account that had since been
suspended.

**Flat pricing.** Every stream pays the same rate regardless of traffic, so a
heavy stream is cross-subsidised by a light one. The provider's exposure is
bounded by §15's per-stream ceilings and its own `max_streams`, not by price.
A provider unwilling to serve at the governance rate sets
`accepts_new = false`, which refuses every new counterparty rather than a
chosen one: v2 has no per-counterparty refusal, so a provider facing a repeat
defaulter closes the abandoned streams under §5.1 and, if it must, stops
accepting new ones.

**Registry growth.** `CBQS_STREAM_CREATION_CHARGE` is a protocol constant
credited to the zero address, so permanent stream state costs unrecoverable
capital no governance write can zero, and it is debited from the owner's native
account so it cannot consume rent a provider has already earned.
`cbqs.max_streams_per_account` and each provider's `max_streams` bound the
count from the other two directions, and §4.3 deletes an account that holds no
balance and no streams. A provider record is permanent and unremovable, so
§4.1 prices it with a charge of its own — it is the largest record here and the
only one that never goes away.

**Rotation is prospective, not revocation.** One rotation does **not** cost a
provider its history: §4.1 keeps the previous key and its lower bound, so every
receipt signed under it still verifies (§10). The same property is the
exposure: a **compromised** key keeps resolving, and keeps producing **valid**
verdicts, for every `snapshot_height` in its own interval — and §19 item G
concedes that height is unverifiable, which is what lets a holder of a stolen
key place a forged receipt inside that window. Rotating away from it does not
stop that.

A provider responding to a compromise rotates **twice**, which removes the key
from the registry — and in the same moment makes every *honest* receipt of that
epoch unverifiable rather than valid, because the epoch before last has no key
there. So the remedy and the loss arrive together, and a client that needs
those receipts must have retained the key alongside them before the second
rotation. A client that wants a receipt checkable across an arbitrary number of
rotations retains the key with it as a matter of course. There is no dispute,
staking, or slashing path in v2; the remedy for a provider that stops serving
is the application's own durable copy plus the §4.4 migration.

**Broker identity is not established at handshake time.** §7 authenticates the
*client* to the broker and nothing in the other direction: a client dials an
endpoint the registry names and relies on TLS to establish that it reached the
host that endpoint names. Three things follow.

**A network-position attacker is detected afterwards, not prevented.** A
mis-issued certificate, a DNS or BGP hijack, or a compromised edge host puts an
attacker in the session with the client's grant and payloads, and TLS is the
whole of the defence against that happening. It is not the whole of the
defence against it going unnoticed: a provider signs every `CheckpointReceiptV2`
under the `signing_key` the registry carries (§10), and an impostor does not
hold that key. Who can use this is exactly who §9 says can — a party holding
**every** record in the batch a receipt attests, since `records_digest` is flat.
A `REPLAY` holder whose lane scope is `Any` qualifies: a batch spans the
stream's total order rather than one lane (§8), so a holder scoped to specific
lanes holds a strict subset of it and is the party §10 says cannot verify at
all. A consumer competing in a group does not qualify either, because it sees
only the records the group hands it. The window is also finite: §12 discards a
receipt when the floor passes the **last** record it attests, so a checkpoint
whose earliest records have already been evicted still resolves while no longer
being recomputable. §4.4 step 4 is
the case the document already builds on this. Detection is not prevention — the
payloads are disclosed either way — but "no way to tell" would overstate it.

**A chain-account compromise is worse, and is only partly detectable.**
`UpdateProvider` changes `signing_key` and `endpoints` together, so an attacker
holding the provider's chain account key repoints the endpoint *and* installs a
key it holds, in one instruction, and its checkpoints verify. What it cannot
overwrite is `previous_signing_key`: §4.1 makes the handler move the outgoing
key there, so the honest key survives the attacker's first rotation and §10
resolves it at any height inside its interval. A client that pinned a height
before the takeover can therefore still detect one — until a second rotation
flushes it, which §4.1 permits at the very next block, and a client meeting
this provider for the first time has nothing to pin. This document defines no pinning rule and no
first-contact remedy.

**Neither is closed, and a third is not addressed at all.** `SessionProofV2`
binds the grant, the session and the challenge, and no channel: an attacker who
relays a whole handshake to the genuine broker holds a session there under its
own connection, authenticated, which outlives the client's and expires only
with the grant. Closing that needs channel binding this document does not
define. An earlier draft added a provider signature to `SessionChallenge` and
claimed it closed the second consequence; it closed none of the three — a
signature resolved from the record the attacker just rewrote proves nothing,
and a relayed signature is still valid — while making a client's chain snapshot
a precondition of opening any session, so a client that could not read the
chain could not reach a data plane §3 keeps available without it. It was
removed rather than repaired.

**Endpoint reachability.** Consensus admission does not establish that a
registered endpoint is safe to dial; a hostname admitted at registration may
resolve into private address space later, and DNS may change between one
resolution and the next. §11.4 places the check on the client, names the
ranges, and requires the client to connect to the address it checked rather
than re-resolving — the rebinding gap a check-then-dial client would otherwise
leave open. §4.1 admits the URI without parsing it, so two readers of one
admitted URI **may** disagree about which host it names — `wss://a.example@b.example/ws`
names `b.example` to a WHATWG parser and `a.example` to a byte scanner. §11.4
makes that disagreement *non-SSRF*, since neither reading can reach an address
in its ranges. It does not make it harmless: pick both halves public and one
reading lands on a host the registrant chose and the other did not. That is the
whole of the guarantee, it is a client-side one, and it is about address ranges
rather than about which host a client ends up talking to. Nothing in this
document lets a client verify that the peer it reached is the provider the
registry names — see **Broker identity** above.

**Denial of service.** Every variable-length field in a frame is capped at
decode by a §15 bound, before allocation and before any signature verification.
Two deadlines bound an unauthenticated peer: each outstanding challenge, and
the connection itself, which is closed if it completes no session within
`cbqs.session_handshake_timeout_ms` (§11.1) — the per-connection session and
subscription bounds are satisfied at zero by a peer that never completes a
handshake, so the connection deadline is the one that binds it. Paging is
bounded by `cbqs.max_lane_list_page`, lane creation by
`cbqs.max_lane_creates_per_min`, and group creation by
`cbqs.max_group_creates_per_min`.

The session and subscription bounds are per connection, and the protocol bounds
no one's connection count; that is an operator concern, and a provider SHOULD
bound concurrent connections per source at its terminator. The other two do not
scale that way and must not be enforced as though they did:
`cbqs.max_lane_list_page` is per request, and the two creation limits are
per **stream** (§8, §9.2) — enforcing those per connection would restore
exactly the N-connections-N-times multiplication per-stream scoping removes.
Lane ids are broker-assigned, so the namespace cannot be squatted, and
`cbqs.max_lanes_per_stream` bounds what is open at once. `record_deadline_ms`
is capped by the stream's own retention, so a group's delivery cycles always
terminate within the window the stream is paid for — under §12 no group can
affect the floor at all, on any timescale. Credit saturates at
`cbqs.max_subscription_credit_bytes`, so the backpressure mechanism cannot be
disabled by a client.

**Delivery, and co-tenancy.** At-least-once delivery repeats application
effects, and §9.2 requires a consumer to make them idempotent or deduplicate in
its own state. A lease may be mutated only by the holder it was issued to, and its
handle is unpredictable so the denial codes cannot be read as an oracle over
another holder's in-flight set (§9.2). `max_in_flight_per_holder` is what
bounds one consumer's share of a shared group, and it is a real bound now that
there is one delivery mode. A `CONSUME`-only holder — the weakest consumer
verb, and one that can take no terminal *action*, since `Ack`, `Nack`,
`Reject` and `Extend` all need `ACKNOWLEDGE` — can absorb at most that many
leases and hold them silently. **Silence still terminates.** Three of the four
routes to `EXPIRED` need no action at all: attempt exhaustion, the group's
`record_deadline_ms`, and eviction by the retention floor. So a silent holder
burns one attempt per lease per `visibility_timeout_ms`, and at the §15 defaults
it walks each record it holds into `EXPIRED` in ten timeouts — five minutes —
after which no path returns that record to the group. What
`max_in_flight_per_holder` bounds is *how many records at a time* it can do
that to; its co-tenants keep `max_in_flight - max_in_flight_per_holder` leases,
and the records under them, throughout.

**The bound is only a bound when the two differ.** `max_in_flight_per_holder`
is validated at `nonzero, <= max_in_flight`, so a group with
`max_in_flight = 1` forces it to 1 and one silent holder reaches every record
in the group, one at a time. An earlier draft also offered a strict-FIFO
delivery mode that exposed only the lowest non-terminal cycle; that mode is
gone, but `max_in_flight = 1` is the configuration a client wanting serialized
processing will choose, and it reproduces the same exposure. **A group with
`max_in_flight = 1` is a single-consumer configuration**, whatever else is set
on it. Setting the two *equal* at a larger value is different and is not a
defect: honest consumers still compete, and §9.2 keeps that setting reachable
because it is the escape from a frozen `max_in_flight`. What it gives up is the
bound against one hostile holder, and an owner choosing it should know it is
choosing that — the more so because `max_in_flight_per_holder` is immutable, so
the choice cannot be narrowed later without deleting the group.

None of this bounds a **`GROUP_ADMIN`** holder, which can lower
`max_in_flight`, expire the backlog through `record_deadline_ms`, or delete the
group; §9.2 says so plainly. A hostile co-tenant with `ACKNOWLEDGE` can still
`Reject` records it was legitimately leased. Sharing a group with a party you
would not trust with those verbs means not issuing them: a group shared with an
untrusted consumer is a trust decision the owner makes when it issues the
grant.

**Egress has no per-holder scope, and the fairness rule is per connection.**
`max_append_bytes_per_sec` is bounded twice — per stream in aggregate, and per
`holder_signing_key_id` across that holder's sessions (§13). Egress is bounded
only the first way: the grant carries no delivered-bytes field, and §11.2's
starvation bound is over the subscriptions on **one connection**. So a holder
with the weakest read verb can open subscriptions across many connections, each
inside every stated per-connection bound, and take a share of the stream's
`max_delivered_bytes_per_sec` proportional to how many it opened. Its
co-tenants are slowed, and at §12's floor a sufficiently slowed consumer's
backlog ages out. The asymmetry is disclosed rather than closed because closing
it means a second per-holder rate on the grant and a fairness rule with a
scope wider than a connection, and version 2's answer to a co-tenant that
behaves this way is the one §6.2 and §7 give throughout: it is a party the
owner issued a grant to, and the remedy is the grant.

**A producer can evict a consumer's backlog.** §12's byte bound is reached by
appending, and any holder with `APPEND` can reach it: at the §15 defaults a
producer running at its own permitted `max_append_bytes_per_sec` fills
`retained_bytes_limit` in the seventeen minutes §12 computes, advancing the
floor past everything older and transitioning every co-tenant's non-terminal
cycle for that range to `EXPIRED`. The bound on this is `retained_bytes_limit`
and the grant's own rate limit, and nothing else — a producer needs no admin
verb to do it. A stream shared with an untrusted producer is therefore the same
kind of trust decision as a group shared with an untrusted consumer, made at
the same moment, and an application that cannot make it gives the producer its
own stream.

**Lane scope does not hide traffic volume.** `sequence` is assigned over the
whole stream (§9), so every `Appended`, every `Delivery`, the
`first_retained_sequence` in `CursorTooOld`, and every `GetCheckpoint` response
tells any holder with any lane scope how many records the *stream* has taken.
Three of the four require the holder to act. `Delivery` does not: it arrives
within credit the holder granted once, so a lane-scoped consumer measures the
stream's growth passively for as long as its subscription is open. What the
deleted checkpoint push additionally leaked was commit *cadence* — timing for
batches containing no record the holder can see — which no surviving channel
carries. §2's disclosure of what identifiers and volumes are visible is written
against the provider; this one is visible to co-tenants as well. Lanes
partition delivery and authorization, not observation, so tenants who must not
measure each other get separate streams.

## Rationale

### The chain is a governance plane, not a data plane

Version 1 made the broker read finalized chain state on every request and fail
closed when that state aged past a bound. The result was a message queue that
stopped when the chain stopped, and whose latency and availability were coupled
to consensus for decisions — ownership, an admin key, a balance — that change
on the order of days. Version 2 keeps those decisions on chain and applies them
from a periodically refreshed snapshot, so the data plane's failure domain is
its own store.

### The economic unit is the (owner, provider) relationship

This is the change that removed the most machinery, and it took two attempts to
find. Version 1 put the balance on the **stream**: every stream carried its own
escrow, rate, settlement height, suspension state, and grace deadline, priced
across three capacity dimensions admitted through either a signed quote or a
genesis-frozen schedule and reserved against four per-provider counters. A
first pass at version 2 moved the balance to the **owner**, which removed the
per-stream economics but created three problems in exchange: a broker could not
soundly test solvency, because one stream's dues say nothing about its
siblings' claims on a shared pool; a provider's collectible was no longer
isolated, because a sibling at another provider could drain the pool first; and
the rent split had to be distributed over an unbounded set of providers, which
made settlement's gas unbounded.

Keying the account by `(owner, provider)` fixes all three at once, because it
is the scope the economics actually live in. The broker testing solvency *is*
the provider being paid, so it evaluates the same expression over the same
fields the chain does. The settlement has one recipient, so its state-I/O is a
fixed pair. And a provider's exposure is bounded by its own cadence rather than
by what other providers do. A stream record ends up with no economic state at
all, and nine fields, none of them money.

### Flat rent, snapshotted per billing run

The rate is a governance parameter, but each account copies it when a run of
streams begins and charges every interval of that run at the copy. A change
never reprices an elapsed interval and never reprices a run in progress; it
reaches an account when the owner next starts one.

Where to put that copy took three attempts, and the two failures are worth
recording because they fail in opposite directions. Re-copying at every
settlement makes settlement *timing* worth money, and settlement is
permissionless — so a provider could pick the block a rate rise took effect.
Copying only at account creation makes the parameter unreachable, because a
zero-stream account accrues nothing and can be held forever, so nobody ever
takes the repricing path. The `0 → 1` transition is the only point that is both
inside the owner's control and outside anyone else's. The same reasoning
applied to the provider/platform split gives a different answer: a split is
read at settlement too, so a live parameter would be equally retroactive, but
unlike the rate it has no natural per-account snapshot point that isn't just a
second copy of the same idea. It became a protocol constant instead — the
cheaper fix for the same hazard.

### One delivery class, batch checkpoints, no lane proofs

Version 1 specified an at-least-once class and an async-durable class together,
signed a receipt per append and per acknowledgement, maintained a global and a
per-lane receipt chain, and committed synchronously per record. Version 2 keeps
the at-least-once class, commits in batches, and signs one checkpoint per
batch, with a contiguity rule so linkage and coverage are the same check. The
evidence is coarser and the cost falls by the batch size in signatures and
fsyncs. The sparse-Merkle lane commitment solved a real problem — a lane-scoped
subscriber cannot check a flat digest — but it protected a claim no one can
enforce, since v2 has no dispute path, so §10 states the limitation instead of
implementing it.

### Broker-assigned lane and group ids

A client-chosen nonce hashed into a lane id forced a permanent tombstone set so
a closed id could not be recreated with a different history, a bound on that
set, a rate limit so one holder could not squat the namespace, and a wedge when
both bounds filled at once. A monotonic counter the broker assigns makes reuse
impossible by construction and retires three of them. The fourth survives
re-scoped: §8 and §9.2 both keep a per-stream creation rate limit, because
assignment removes *squatting* but not *rate* — an id a holder cannot squat is
still an id it can mint, and minting is a durable write under flat rent.

That is the shape of most of what version 2 removed, and the residue is the
part worth stating plainly: a compensating mechanism usually decomposes into
the part that was compensating and the part that was doing its own work, and
only the first goes away. §9.2 applies the same counter to group ids and
retires the same three, keeps the rate limit, and gains §7's
`group_scope = Any` requirement — which is the one thing a counter *adds*, since
an id nobody can squat is an id a narrow grant can reach by advancing the
counter to it.

### Rejected alternatives

- **Keep version 1 and fix it incrementally.** Most of the removed surface is
  load-bearing for other removed surface: quotes exist because rates are
  per-stream, reservations because quotes do, lane tombstones because lane ids
  are client-chosen. Removing them one at a time leaves the document
  inconsistent at every intermediate step.
- **Drop billing entirely and let providers bill off chain.** Ownership,
  authorization, and payment are what the platform exists to arbitrate; a
  broker with a private customer table is not a Cowboy service.
- **Adopt an existing broker unchanged.** Kafka, NATS, or RabbitMQ cannot
  verify a StreamGrant or enforce a chain-controlled generation.

  That is a true statement about a false choice, and it is worth naming as
  such: the alternative to adopting a broker unchanged is not defining one from
  scratch. A third option is a **thin chain-aware adapter** that verifies
  grants, generations and tenancy, and maps durability, consumer groups, retry
  and backpressure onto a mature broker underneath — which is where most of
  §§9–13 would go. This document does not take it, because the mapping is not
  free (lease semantics, ordering within a lane, and the checkpoint boundary
  each have to survive it) and no candidate has been evaluated against those
  three. But it has not been evaluated *against*, either, and a reader
  comparing v2's size to an off-the-shelf broker deserves to know the option
  was recognised rather than argued away. `cbqsd`'s storage engine remains
  pluggable, which is the smallest version of the same idea.

- **Keep per-request signatures.** They cost a signature and a verification per
  message and defend against an attacker who, having reached the required
  position, can already reorder and drop frames.
- **Meter messages and bill per byte.** Metering requires trusting a
  provider-reported counter or putting usage on chain. A flat rate bounded by
  per-stream ceilings the broker already enforces keeps both off the table.
- **Carry arrears instead of writing them off.** An abandoned account would
  accumulate a debt nobody will pay, which no party can act on and which makes
  revival cost more the longer it is left.
