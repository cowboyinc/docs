---
title: "CIP-40: EIP-712 Signing & WalletConnect"
description: An EIP-712 signature mode for Cowboy transactions, a chain-instance-bound transaction identity, WalletConnect integration, and the minimal Ethereum-facade RPC used for network discovery.
icon: wallet
---

<Note>
  **Status:** Draft
  **Type:** Standards Track
  **Category:** Core
  **Created:** 2026-08-05
  **Requires:** CIP-28 (fee-payer wire surface)
  **Relates-to:** CIP-3 (fee preflight runs before either mode's digest or recovery), CIP-33 (co-owner of the Transaction-v2 decoder; kind 1 remains decode-only and consensus-disabled here), CIP-41 (multi-signer authorization — relies on the §7.3 duplicate-primary rule)
</Note>

## 1. Abstract

Cowboy already uses Ethereum's cryptography end to end: secp256k1 ECDSA, keccak256, and Ethereum-identical address derivation. Any Ethereum keypair is a valid Cowboy account. What an Ethereum *wallet* cannot do today is produce a valid Cowboy signature: transactions are signed over a raw keccak256 of the canonical codec encoding, and stock wallets only expose prefixed signing surfaces (`personal_sign`, `eth_signTypedData_v4`).

This proposal closes that gap with an **EIP-712 typed-data signature mode** accepted alongside the native mode for caller-signed transactions. `Transaction` gains a `signature_mode` discriminant; mode `eip712` verifies an EIP-712 digest of the transaction's typed representation. Both native and EIP-712 transaction identity bind an immutable `ChainIdentity`, while signature bytes remain excluded from `digest(chain_identity)`.

With mode `eip712` in place, a compatible EVM wallet can become a Cowboy signer through an injected provider or the standard WalletConnect `eip155` namespace and `eth_signTypedData_v4`. Cowboy's own wallets ride the same rails and recognize the Cowboy transaction domain to render native, instruction-aware approval UI instead of raw typed-data fields.

The proposal also specifies the **Ethereum facade**: a deliberately minimal JSON-RPC endpoint (`POST /eth`) implementing just enough of the `eth_*` surface for `wallet_addEthereumChain`, balance display, and nonce lookup. It is not EVM compatibility and does not accept Ethereum transactions.

## 2. Motivation

1. **Wallet reach without custody.** Today the only way to sign a Cowboy transaction is to hold the raw private key in the signing context — the browser extension, the wasm SDK, or a server key. The wasm SDK explicitly documents this as a weakened threat model. EIP-712 mode moves signing into hardware wallets and audited wallet software that never reveal the key.
2. **One testable core.** Transaction v2, explicit chain identity, the connector, and the facade form one implementation slice that can be exercised end to end without activating route-authentication, payment-admission, governance, or storage redesigns.
3. **Ecosystem leverage.** Signing via the `eip155` namespace lets dapps reuse wagmi/viem/AppKit and compatible wallets use an existing typed-data surface. A custom WalletConnect namespace would initially reach only Cowboy-specific wallets.
4. **Standard wallet surface.** EIP-712 gives the transaction a domain-separated typed-data digest that provider and WalletConnect APIs can request without exporting the user's key.

## 3. Design Goals

- **The signature determines the transaction.** In both modes, every field of the executed object is bound under the signature, either directly in the digest or by a structural invariant enforced at verification. No field is a free variable.
- **One digest identity.** `tx.digest(chain_identity)` — the BMT leaf, receipt key, and dedup identity — is the tagged, chain-bound construction over canonical transaction bytes with signatures zeroed. Signature bytes never affect identity; `signature_mode` does, so otherwise-identical native and EIP-712 authorizations are distinct transactions.
- **One construction.** The transaction domain shape, encoding rules, field names, and verification context are defined once and consumed everywhere a transaction is signed or verified.
- **Single implementation.** Transaction EIP-712 machinery lives in `cowboy-protocol-codec` and is consumed by node and the wasm/FFI/SDK bindings. No hand-copied digest code.
- **Fail closed.** Unknown signature modes, unknown typed-data versions, and malformed signatures are rejected at decode or verification, at every admission point.
- **Wallet-agnostic dapp surface.** Dapps integrate via standard `eip155` WalletConnect sessions and `eth_signTypedData_v4`. Cowboy-specific logic (building the typed data, encoding the submission) lives in a connector library, not in wallets.
- **No display spoofing.** Every field in a typed struct is verified against the object it authorizes. No derived, display-only fields.

## 4. Non-goals

- **EVM execution or general `eth_*` compatibility.** The Ethereum facade (§10) serves wallet onboarding only. `eth_call`, `eth_sendTransaction`, `eth_sendRawTransaction`, `eth_getLogs`, `eth_estimateGas`, and EVM semantics are permanently out of scope for it.
- **A custom `cowboy:` WalletConnect namespace.** CAIP-2 registration of a `cowboy` namespace adds no capability over `eip155` + typed data and would fragment wallet support. Not pursued.
- **Per-instruction expanded typed structs.** The typed representation carries the instruction as canonical bytes (§7.2). Expanded, human-readable per-instruction structs (e.g. a dedicated `Transfer` type) are a candidate follow-up once the generic mode is proven; see §15.
- **Mixed-mode multisig.** `signature_mode` applies to a transaction as a whole; the primary signer and all additional signers use the same mode.
- **Route-authentication and payment signing.** Those designs remain separate drafts owned by their respective CIPs. This release adds no route/payment version gate, connector method, server readiness claim, or activation dependency.

## 5. Definitions

- **Native mode.** The non-EIP signature scheme: 65-byte recoverable secp256k1 over the tagged, chain-identity-bound §7.1 signing preimage with all signatures zeroed.
- **EIP-712 mode.** The scheme defined here: the same 65-byte recoverable secp256k1, but over an EIP-712 typed-data digest (`keccak256(0x19 ‖ 0x01 ‖ domainSeparator ‖ structHash)`).
- **Signature mode.** A `u8` discriminant selecting the verification scheme: `0` = native, `1` = eip712. All other values are invalid.
- **Chain identity.** `ChainIdentity { chain_id: u64, chain_instance_id: [u8; 32] }`. `chain_id` names the network. `chain_instance_id` names one exact release-pinned genesis and is derived in §16 independently of block-header digest versions.
- **Cowboy transaction domain.** `EIP712Domain(string name,string version,uint256 chainId,bytes32 salt)`, with `salt = chain_instance_id`. No `verifyingContract` is used because no contract is the transaction verification authority.
- **Connector.** The dapp-side library (`@cowboyinc/connect`) that builds typed data, requests signatures over WalletConnect or an injected provider, assembles signed transactions, and submits them to the REST RPC.
- **Ethereum facade.** The minimal JSON-RPC endpoint of §10.

## 6. Architecture

### 6.1 Signature schemes touched by this CIP

| Object | Native digest | EIP-712 domain name | Selected by |
|---|---|---|---|
| Transaction | `keccak256(b"cowboy.tx.v2" ‖ chain_id ‖ chain_instance_id ‖ encoding, sigs zeroed)` (§7.1) | `Cowboy Transaction` v2 | `signature_mode` field |

The native transaction digest is separated from EIP-712 by the `0x19 0x01` prefix; the EIP-712 transaction is additionally separated by domain name, version, chain ID, and salt. The same logical transaction on two chain instances therefore produces different signing digests.

Other authorization schemes remain owned by their respective CIPs and are neither changed nor declared release-ready here. To keep native/EIP separation checkable as the protocol grows, this CIP establishes the rule: **no native signing preimage may begin with the byte `0x19`.** ASCII domain tags satisfy the rule naturally; schemes led by a version byte avoid the value `0x19`.

### 6.2 Signing-flow comparison

```
NATIVE (today, unchanged)                EIP-712 (this CIP)

dapp/SDK holds raw key                   dapp holds no key
  build canyon JSON                        build canyon JSON
  signing_hash(tx)                         eip712_typed_data(tx) ──► wallet
  sign(hash, privkey)                        (WalletConnect / injected)
  attach sig, mode=0                       wallet renders & signs typed data
  encode_submission ──► POST /submit       attach sig, mode=1
                                           encode_submission ──► POST /submit
```

The submission path, mempool, consensus, and execution are identical for both modes; only the digest recomputed at verification differs.

### 6.3 EIP-712 encoding conventions

All Cowboy typed structs follow EIP-712 encoding exactly as specified in the standard, with these conventions for the schemes defined here:

- **Field names are snake_case**, matching the canonical Rust field names. Type strings in this document are normative byte-for-byte; a wallet or SDK that re-derives a typehash from differently-cased names produces a different digest and fails verification.
- **The transaction domain is the four-field form** `EIP712Domain(string name,string version,uint256 chainId,bytes32 salt)`. `chainId` and `salt = chain_instance_id` come from one closed `GET /chain-info` response and are treated as one identity. The domain `version` is the decimal transaction version (§7.2).
- **`bytes` fields** hash as `keccak256(contents)` per EIP-712; **arrays** hash as `keccak256` of the concatenated encoded elements.
- **Signatures** are 65-byte `r ‖ s ‖ v` with `v ∈ {0, 1}` on the wire. EVM wallets emit `v ∈ {27, 28}` from `eth_signTypedData_v4`; normalizing to `{0, 1}` is the connector's job (§11.2), and verifiers MUST reject any wire `v ∉ {0, 1}`. Verifiers MUST reject high-S signatures. Together these give each authorization exactly one valid wire encoding.

## 7. Transaction EIP-712 Mode

### 7.1 Wire change

Transaction v2 has the closed framing `u8 version=2 ‖ u8 kind ‖ body`. The shared standalone decoder accepts exactly two kinds. `kind=0` is the ordinary caller-signed transaction, with no inner version byte and this complete canonical field order:

```text
chain_id, nonce, instruction,
cycles_limit, cells_limit,
max_fee_per_cycle, max_fee_per_cell,
max_priority_fee_per_cycle, max_priority_fee_per_cell,
from, access_list, metadata,
origin_tx_hash, origin_remaining_cycles, origin_remaining_cells,
signature_mode, fee_payer_override, signature, additional_signers
```

`signature_mode` is one byte and decoders accept only `0` or `1`. `fee_payer_override` is the commonware `Option<Address>` encoding: tag `0x00`, or tag `0x01` followed by 20 address bytes.

`kind=1` is the CIP-33 `TradingPostSpawn` body. It carries no signature fields. The standalone shared decoder accepts it only for exact codec/corpus reproduction. Every external RPC/mempool submission and every consensus block-validation, import, replay, and execution entry point MUST reject a decoded `kind=1` before lane selection, digest use, recovery, state access, event, or execution; a block containing it is invalid even when proposed directly by a Byzantine validator. This release provides no kind-1 producer or capability advertisement and does not activate the CIP-33 spawn path. Every unknown kind and every outer version other than `2` rejects at the outer discriminant.

Ownership is explicit: CIP-40 owns the outer shell, kind-0 body, `ChainIdentity`, transaction digest,
and disabled-kind policy. CIP-33 owns the exact kind-1 body and any future activation that changes
the disabled policy. CIP-33 §2.6.4 mirrors this contract and pins its fixed-width body; kind 1 does
not inherit kind 0's minimal-`UInt` scalar encoding.

**Digest and native signing preimage, exact for kind 0:**

```
keccak256( b"cowboy.tx.v2" ‖ u64_be(chain_id) ‖ chain_instance_id(32) ‖ full v2 encoding with every signature field zeroed )
```

The pair is required: `chain_id` names the network and `chain_instance_id` names the exact genesis. Zeroing covers signature bytes only—`signature_mode`, the `fee_payer_override` Option tag/value, and signer addresses remain—so identity is signature-independent but authorization-mode-dependent.

The codec exposes no context-free v2 digest API:

```text
ChainIdentity { chain_id: u64, chain_instance_id: [u8; 32] }
Transaction::signing_hash(&self, identity: &ChainIdentity) -> Result<[u8; 32], TxIdentityError>
Transaction::digest(&self, identity: &ChainIdentity) -> Result<Digest, TxIdentityError>
TransactionVerificationContext { chain_identity: ChainIdentity }
Transaction::verify(&self, context: &TransactionVerificationContext) -> Result<(), TxVerifyError>
```

Both methods reject unless `self.chain_id == identity.chain_id`. The same immutable `ChainIdentity` flows through block construction, block verification/replay, mempool admission, execution, RPC, CLI, wasm, FFI, and every transaction producer. The CIP-3 preflight still runs before either mode's digest computation. Version 2 is the sole transaction encoding specified by this release, and all native/EIP-712 vectors are re-issued over it.

### 7.2 Typed representation

Domain:

```
EIP712Domain(string name,string version,uint256 chainId,bytes32 salt)
  name    = "Cowboy Transaction"
  version = "2"                    (decimal CURRENT_TX_VERSION)
  chainId = <tx.chain_id>
  salt    = <chain_identity.chain_instance_id>
```

The domain `version` string MUST equal the decimal rendering of `CURRENT_TX_VERSION`. Bumping the transaction version therefore changes the mode-1 domain separator, and no mode-1 signature is valid across a version boundary. Per §7.1 the mode-1 digest additionally binds the shell's `kind` as a struct field (kind=0 is the only signable kind — kind=1 is structurally unsigned); its slot is pinned in the type string below.

Primary type (normative type string, no whitespace deviations):

```
Transaction(uint8 kind,uint64 nonce,bytes instruction,uint64 cycles_limit,uint64 cells_limit,uint64 max_fee_per_cycle,uint64 max_fee_per_cell,uint64 max_priority_fee_per_cycle,uint64 max_priority_fee_per_cell,address from,bytes metadata,address[] additional_signers,bool has_fee_payer_override,address fee_payer_override)
```

Field mapping:

- `kind` — the shell's kind byte (§7.1), leading the struct as it leads the wire body. Always `0` for a signable transaction; binding it makes the §7.1 cross-kind replay rejection derivable from the typehash itself.
- `instruction` — the canonical codec encoding of the transaction's `Instruction`, byte-identical to what `Transaction::write` emits for that field. This binds the full instruction payload (including nested actor calls, deploy code, amounts) without replicating the enum in EIP-712's type system.
- `has_fee_payer_override` / `fee_payer_override` — the exact typed projection of CIP-28's wire `Option<Address>`. `None` maps to `false` and the zero address. `Some(address)` maps to `true` and that address, including zero. The boolean binds the Option tag, so `None` and `Some(ZERO)` cannot share a digest.
- `additional_signers` — the transaction's additional-signer *addresses*, in the transaction's canonical (strictly ascending) order. Binding the signer set into the digest preserves the native mode's property that no signer can be added or removed after signing.
- `chain_id` and `chain_instance_id` appear in the domain, not the struct.
- All other fields map one-to-one to the transaction fields of the same name.

The typed struct deliberately omits `access_list` and the deferred-execution fields (`origin_tx_hash`, `origin_remaining_cycles`, `origin_remaining_cells`). They are not left unbound: §7.3 forbids them outright in mode 1. When access lists activate, the type string gains an `access_list` field — a typehash change that cleanly partitions old and new signatures.

### 7.3 Digest and verification

```
struct_hash = keccak256(typehash ‖ enc(kind) ‖ enc(nonce) ‖ keccak256(instruction_bytes) ‖ … ‖ keccak256(enc(additional_signers)) ‖ enc(has_fee_payer_override) ‖ enc(fee_payer_override))
digest      = keccak256(0x19 ‖ 0x01 ‖ domain_separator(chain_id, chain_instance_id) ‖ struct_hash)
```

`Transaction::verify(context)` is the single verification entry point called from RPC admission, the mempool listener, consensus block verification, execution, and transport dispatch. It branches on `signature_mode` only after validating the trusted context and shared structural rules.

- **Deferred transactions** (`origin_tx_hash` present) are structurally unsigned. They require
  `signature_mode == 0`, `fee_payer_override == None`, a zero primary signature, and an empty
  `additional_signers` list. These checks run before fee-payer resolution or state access.
- **mode 0:** verify against `signing_hash(&context.chain_identity)`, the §7.1 tagged construction.
- **mode 1:** the following structural invariants are checked before any recovery, and violation is rejection:
  - `access_list` is `None`;
  - `origin_tx_hash`, `origin_remaining_cycles`, and `origin_remaining_cells` are all `None`.

  These invariants close the fields the typed struct does not carry: with them, every byte of the canonical encoding is either bound in the EIP-712 digest, fixed by invariant, or a signature slot — so a mode-1 signature determines the full transaction and its `digest()`, exactly as a native signature does. Verification then computes the EIP-712 digest above from the transaction's own fields and runs the identical recovery path: primary `signature` must recover to `from`; each additional signer's signature must recover to its stored address, all against the *same* digest (matching the native mode's shared-digest multisig semantics).

For every primary and additional signature, `Transaction::verify` first rejects a wire recovery id
outside `{0,1}`, before any recovery call, and then uses `EthSignature::recover_address`, whose
high-S rejection remains authoritative. The `{0,1}` precheck is new; the current helper's acceptance
of recovery ids `2` and `3` is not relied upon. Structural checks shared by both modes
(`MAX_ADDITIONAL_SIGNERS`, strict signer ordering) are unchanged, and both modes gain one:

- **`from` MUST NOT appear in `additional_signers`.** Previously unchecked: one key could fill both slots with valid signatures and pass verification, letting a single signer satisfy any consumer that counts `1 + additional_signers.len()` as a quorum of two (the Council's `HashSet` accumulation is immune only by accident of implementation). The check runs with the other structural invariants, before any recovery in either mode — after the CIP-3 §2.5 common preflight, before the mode digest is computed. Every transaction **producer** ships the matching contract and rejects a duplicate primary **with an error** rather than silently de-duplicating — a caller who listed the primary has a confused roster, and silent repair would mask it.

  The producer contract, per surface, with its concrete error API:

  | Surface | Location | Contract |
  | --- | --- | --- |
  | Verifier | `cowboy-protocol-codec/src/transaction.rs` `verify()` | structural rejection before zero recovery calls |
  | Rust signer | `cowboy-protocol-codec` — `sign_multi` becomes `sign_multi(…) -> Result<Self, TransactionSigningError>` with variant `DuplicatePrimary` (it currently returns `Self` and silently sorts/de-duplicates); all call sites and golden fixtures migrate in the same change | `Err(TransactionSigningError::DuplicatePrimary)` |
  | Python SDK signer | `python-sdk/cowboy/signing.py` (today sorts and de-duplicates but never checks the primary) | raises `DuplicatePrimarySignerError` (a `ValueError`) |
  | CLI build/assemble (the CIP-41 §4.4 flow — the commands are defined there, not here) | `node/cli/src/commands.rs` | exits non-zero, emits no artifact |
  | wasm | `cowboy-protocol-wasm/src/lib.rs` — every export that accepts transaction JSON with a roster; note `sign_transaction` derives and **overwrites `from` after parsing**, so roster validation MUST run after the final `from` is known, not at parse time | throws `JsError` |
  | wasm-FFI | `cowboy-protocol-wasm-ffi`, same exports and the same post-derived-`from` ordering | error sentinel + last-error message |

  Every table row carries one executable rejection test asserting its exact error: Rust `Err(TransactionSigningError::DuplicatePrimary)`; Python `DuplicatePrimarySignerError`; CLI `build-unsigned` and `assemble` tested **separately**, each exiting non-zero with no artifact written; every roster-taking wasm export throwing `JsError`; every mirrored FFI export returning its packed-ABI sentinel (`out_ptr == 0`) and setting `cpw_last_error()`. The `sign_transaction` post-derived-`from` collision — parsed JSON clean, derived `from` colliding with a listed co-signer — is tested on both the wasm and FFI routes.

  Fixtures: a dedicated **invalid corpus** at `cowboy-protocol/crates/cowboy-protocol-codec/fixtures/commonware-codec-invalid/`, layout `<case>/input.json` + `<case>/expected_error.txt` — deliberately not the four-file shape of the success corpus at `…/fixtures/commonware-codec/`, whose harness (`…/tests/golden_vectors.rs`) assumes successful encodings; the invalid corpus is driven by a dedicated harness alongside it (`…/tests/invalid_vectors.rs`). Each duplicate-primary vector is **otherwise valid** — canonical ordering, valid mode structure, and `cycles_limit ≥ TX_BASE_CYCLES + k × COSIGNER_VERIFY_CYCLES` so the CIP-3 §2.5 minimum is not what rejects it; duplicate-primary is the sole violation, in mode 0 and mode 1, with an instrumented assertion that **zero recovery calls** occur. The valid deferred case (mode 0, empty `additional_signers`) remains accepted.

`digest(chain_identity)` — transaction identity — is the §7.1 tagged construction over domain tag, chain identity, and the signature-zeroed v2 encoding. Two transactions identical except for `signature_mode` have different hashes: they are different authorizations, both well-defined.

### 7.4 What a wallet displays

A stock EVM wallet renders the domain name ("Cowboy Transaction"), the chain id, and the struct fields — legible `nonce`, fee fields, and `from`, but hex blobs for `instruction` and `metadata`. Since the instruction carries the recipient, amount, or deploy payload, mode 1 in a wallet without Cowboy support is **blind signing with a domain tag**: the user proves which bytes they signed but cannot read what those bytes do. The anti-spoofing property holds — nothing displayed is unbound — but instruction legibility requires a Cowboy-aware signer.

Cowboy's own wallets recognize the domain, decode `instruction` with the canonical codec, and render a native approval screen (transfer amount and recipient, actor and handler, deploy summary). Connectors SHOULD render the decoded instruction alongside the wallet prompt, and documentation SHOULD steer high-value signing toward Cowboy-aware wallets until expanded per-instruction typed structs land (§15).

## 8. Deferred Route-Authentication Signing

Route-authentication EIP-712 forms are not activated or specified by this release. They remain
companion work owned by the route-authentication CIP. CIP-40 adds no route-auth version to its
activation payload, exposes no `signRouteAuth`, and makes no Gateway/Runner/Workload readiness
claim.

## 9. Deferred Payment Signing

Payment-intent EIP-712 forms are not activated or specified by this release. They remain
companion work owned by the payments CIP. CIP-40 adds no payment-auth version to its activation
payload, exposes no `signPayment`, and makes no payment-admission or settlement readiness claim.

## 10. Ethereum Facade

A wallet adding Cowboy as a network needs a JSON-RPC URL whose `eth_chainId` agrees with the requested chain. The facade provides that narrow onboarding and display surface; it is not an EVM endpoint.

`GET /chain-info` returns exactly this closed JSON object with `Cache-Control: no-store`:

```json
{
  "chain_id": "26909",
  "network": "mesa",
  "chain_instance_id": "0x<64 lowercase hex digits>",
  "activation_manifest_digest": "0x<64 lowercase hex digits>",
  "transaction_version": 2,
  "transaction_signature_modes": ["native", "eip712"]
}
```

`chain_id` is a canonical unsigned decimal `u64` string and never passes through a JavaScript `number`; the values shown are illustrative, not an allocation. At startup the node independently recomputes and stores the complete §16 release identity, including the external manifest digest; this handler reads that trusted local result and MUST NOT echo request data or an unchecked configured digest. `/chain-info` is configuration discovery, not a self-authenticating state proof. The connector's trust anchor is separately release-pinned metadata obtained with the application/release, and HTTPS; it compares every configured mirror to that expected identity and to every other mirror. Missing, extra, duplicate, reordered mode, wrong-type, wrong-case, or unequal identity/version fields make the connector refuse to sign.

`GET /chain-info` and `POST /eth` are deliberately public, read-only browser endpoints. Successful and error responses on those exact paths include `Access-Control-Allow-Origin: *` and never include `Access-Control-Allow-Credentials`. Connector requests use `credentials: "omit"`. An `OPTIONS` request to `/chain-info` returns `204` with `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET, OPTIONS`, and `Access-Control-Allow-Headers: content-type`; `/eth` returns the same headers with `POST, OPTIONS`. No neighboring authenticated RPC endpoint inherits this CORS policy.

### 10.1 Endpoint

`POST /eth` on the existing REST RPC server accepts a UTF-8 `application/json` JSON-RPC 2.0
body. The root is one request object or a nonempty array of at most 16 request objects. Each request
has exactly `jsonrpc:"2.0"`, a method string, optional `params`, and optional `id`; an id is a JSON
string or I-JSON integer and is echoed unchanged. An absent id is a notification and produces no
response entry. No-parameter methods accept absent `params` or exactly `[]`; every other method
requires the exact positional array below. Duplicate keys, extra request members, non-I-JSON
numbers, or trailing JSON reject as an invalid request.

Every response object has exactly `jsonrpc:"2.0"`, the request `id`, and one of `result` or
`error`. An error is exactly `{"code":<integer>,"message":<string>}` with no `data`: parse error
`(-32700,"Parse error")`, invalid request `(-32600,"Invalid Request")`, method not found
`(-32601,"Method not found")`, invalid params `(-32602,"Invalid params")`, or internal error
`(-32603,"Internal error")`. JSON-RPC success and error bodies use HTTP 200. A malformed root,
empty batch, or batch larger than 16 returns one invalid-request object with `id:null` and executes
nothing. Batch responses preserve request order while omitting notifications; an all-notification
batch returns HTTP 204 with an empty body.

`QUANTITY` is lowercase `0x` hexadecimal with no leading zero (`0x0` is zero). `DATA<n>` is `0x`
plus exactly `2n` lowercase hex digits. Input addresses are `0x` plus 40 case-insensitive hex
digits. `FINALIZED_TAG` is exactly one of `"latest"`, `"safe"`, or `"finalized"`; all three resolve
to the same locally finalized Cowboy head. Every other tag, including `"pending"`, `"earliest"`
and numeric block tags, is invalid params.

| Method | Exact params | Exact result |
|---|---|---|
| `eth_chainId` | absent or `[]` | `QUANTITY(chain_id)` from the accepted local `ChainIdentity` |
| `net_version` | absent or `[]` | canonical unsigned decimal string of `chain_id` |
| `eth_blockNumber` | absent or `[]` | `QUANTITY(finalized_height)` |
| `eth_getBlockByNumber` | `[FINALIZED_TAG, false]` | the closed compatibility block object below |
| `eth_getBalance` | `[address, FINALIZED_TAG]` | `QUANTITY(checked(balance_wei9 × 10^9))` (§10.2) |
| `eth_getTransactionCount` | `[address, FINALIZED_TAG]` | `QUANTITY(committed_next_nonce)` |
| `eth_gasPrice` | absent or `[]` | exactly `"0x0"` |
| `web3_clientVersion` | absent or `[]` | exactly `"cowboy-rpc/"` plus the build's nonempty ASCII semver |
| everything else | any | method-not-found error |

The compatibility block result has exactly these members. `block_hash`, `parent`, `tx_root`,
`state_root`, `receipt_root`, `height`, and `timestamp` come from the same finalized Cowboy block.
`ZERO8`, `ZERO20`, `ZERO32`, and `ZERO256` are zero-filled `DATA<n>` values. This is a typed wallet
liveness object, not an Ethereum block or an EVM commitment.

| Member | Exact JSON value |
|---|---|
| `number` | `QUANTITY(height)` |
| `hash` / `parentHash` | `DATA32(block_hash)` / `DATA32(parent)` |
| `timestamp` | `QUANTITY(timestamp)` |
| `transactionsRoot` / `stateRoot` / `receiptsRoot` | `DATA32(tx_root)` / `DATA32(state_root)` / `DATA32(receipt_root)` |
| `nonce` / `miner` / `mixHash` / `logsBloom` | `ZERO8` / `ZERO20` / `ZERO32` / `ZERO256` |
| `sha3Uncles` | exactly `"0x1dcc4de8dec75d7aab85b567b6ccd41ad312451b948a7413f0a142fd40d49347"` |
| `difficulty` / `totalDifficulty` / `size` / `gasLimit` / `gasUsed` | exactly `"0x0"` |
| `extraData` | exactly `"0x"` |
| `transactions` / `uncles` | exactly `[]` / `[]` |
| `baseFeePerGas` | JSON `null` |

`eth_getBlockByNumber` returning `baseFeePerGas: null` signals a pre-EIP-1559 network, so wallets fall back to legacy fee display and never attempt fee-market calls in earnest. The facade is read-only and stateless. It MUST NOT implement `eth_sendTransaction`, `eth_sendRawTransaction`, `eth_call`, or `eth_estimateGas` — a wallet or tool that submits an Ethereum-format transaction gets a clean method-not-found, not a silent misinterpretation.

### 10.2 Balance scaling

CBY has 9 protocol decimals (1 CBY = 10⁹ Cowboy wei). The supported-wallet metadata uses `nativeCurrency.decimals = 18`, so `eth_getBalance` returns `checked(balance_wei9 × 10⁹)` as a JSON-RPC quantity. The multiplication uses at least `u128` or arbitrary-precision arithmetic and never narrows through `u64`. This is a facade display convention only.

### 10.3 Chain id uniqueness

In the `eip155` namespace, chain ID 1 is Ethereum mainnet. A Cowboy network exposing the facade MUST use one noncolliding ID allocated in the release-pinned [`docs/cips/registries/cip-40-chain-ids-v1.json`](./registries/cip-40-chain-ids-v1.json). The registry is the closed object `{"allocations":[...],"format_version":1}`. Each allocation is the closed object `{"allocation_pr":N,"chain_id":"D","network":"S","public":B}`: `N` is a JSON integer in `1..=2^32-1` naming the allocating `cowboyinc/cowboy` pull request; `D` is a nonzero canonical unsigned decimal `u64` string; `S` matches `[a-z][a-z0-9-]{0,31}`; and `B` is a JSON boolean. Rows are strictly ascending by the numeric `u64` value of `D`; both `D` and `S` are globally unique, and existing rows are immutable. The file bytes are RFC-8785 JCS of that object followed by exactly one LF; the LF is file framing and is excluded from any JCS digest or equality check. This revision is intentionally empty, so the illustrative `26909` cannot be used for a production release until a separately reviewed row is added and collision-checked against a pinned `ethereum-lists/chains` checkout.

Wallet display metadata is a separate release-pinned object containing the allocated chain ID, chain name, 18-decimal CBY metadata, HTTPS `/eth` RPC URLs, and optional explorer URLs. Before asking a provider to add or switch networks, the connector validates every listed RPC's `eth_chainId` and `/chain-info` response against that metadata, the expected activation-manifest digest, and each other. One failed or divergent mirror rejects the operation.

## 11. WalletConnect Integration

### 11.1 Provider state machines

Injected and WalletConnect providers have different onboarding state machines. For an injected EIP-1193 provider, the connector first validates the release-pinned network metadata and every direct facade mirror, reads the provider's active `eth_chainId`, and requests the EIP-3326 method `wallet_switchEthereumChain` when it differs. Only its exact unknown-chain `ProviderRpcError.code = 4902` permits a subsequent EIP-3085 `wallet_addEthereumChain`; `4001` and every unmapped error fail without an add request. After adding, the connector requests the switch again and re-reads `eth_chainId` before signing.

For WalletConnect, dapps request a standard `eip155` session:

- **Chains:** `eip155:<cowboy_chain_id>` per network the dapp supports.
- **Methods:** `eth_signTypedData_v4` (required). No transaction-sending methods are requested.
- **Events:** `accountsChanged`, `chainChanged`.

The WalletConnect session can succeed only when the wallet already supports or has provisioned the Cowboy chain; the facade cannot generically add an unknown chain before session approval. A wallet-specific out-of-session onboarding flow MAY provision it, after which the connector creates a fresh Cowboy session. Before every signature, the approved CAIP account chain, provider active chain, EIP-712 `domain.chainId`, `/chain-info.chain_id`, domain salt, and connector `chain_instance_id` MUST agree. Account/chain/session changes invalidate pending typed data.

### 11.2 Connector

`@cowboyinc/connect` is the dapp-side integration surface. This release exposes `connect`, `getAccount`, and `signTransaction` over injected and WalletConnect transports; it adds no arbitrary-message, route-authentication, or payment-signing method. For each signature operation it:

1. Validates every configured facade mirror and the release-pinned activation-manifest digest, then freezes the resulting `ChainIdentity` for the operation.
2. Builds canonical canyon JSON without routing any `u64` through JavaScript `number`.
3. Calls the canonical wasm export with that exact `ChainIdentity` to produce the full typed object, then checks the returned domain identity and bound account.
4. Requests exactly `eth_signTypedData_v4` for the approved account.
5. Accepts exactly 65 bytes, leaves `v=0/1` unchanged, maps `27/28` to `0/1`, rejects every other value, attaches mode 1, re-verifies locally through `TransactionVerificationContext`, encodes, and submits.

### 11.3 Cowboy wallets

The iOS wallet integrates Reown WalletKit. A request is recognized as Cowboy only when domain name/version/chain/salt, `primaryType`, exact type/member sets, and bound account match the pinned schema; malformed near-misses using a reserved Cowboy domain hard-reject. For a recognized transaction, the wallet decodes and re-encodes the exact signed `instruction` bytes before rendering them. Stock-wallet mode remains blind signing with a domain tag (§7.4).

### 11.4 SDK surface

New canonical exports in `cowboy-protocol-wasm` (mirrored in the C-ABI FFI crate, as the export surfaces are kept in lockstep):

| Export | Purpose |
|---|---|
| `eip712_typed_data(tx_json, chain_identity_json) -> String` | Full canonical typed object for a transaction |
| `eip712_signing_hash(tx_json, chain_identity_json) -> [u8;32]` | The EIP-712 digest, for verification and vectors |
| `attach_signature(tx_json, sig, mode) -> String` | Returns canyon JSON with signature and mode attached |

Rust, wasm, C ABI, Swift FFI, Python, CLI, and JS transaction bindings accept the same explicit `ChainIdentity`; every verification binding accepts `TransactionVerificationContext`. Typed-data JSON is generated only by the canonical implementation—connectors and wallets MUST NOT hand-assemble it.

## 12. Security Considerations

- **Domain and instance separation.** Native v2 and EIP-712 use structurally distinct prefixes, and
  both bind the same immutable `ChainIdentity`. Same-chain-ID/different-instance and cross-mode
  mutations therefore fail.
- **Complete transaction binding.** The typed struct plus §7.3 invariants cover every canonical
  field. Structural checks run in `verify(context)` before recovery at every admission point.
- **Version and kind policy.** The version/kind bytes are inside the native preimage; mode 1 binds
  version through its domain and kind through its message. Kind 1 may decode in the standalone
  codec but is invalid in every external and consensus path in this release.
- **Option-tag binding.** `has_fee_payer_override` binds the wire Option tag, preventing `None`
  from sharing a digest with `Some(ZERO)`.
- **Signature canonicality.** Verifiers reject high-S signatures and every wire recovery id outside
  `{0,1}`; connectors normalize only provider `27/28`.
- **Signed bytes are the authorization.** A stock wallet cannot interpret the opaque instruction.
  This release provides cryptographic interoperability, not a human-readable stock-wallet consent
  claim. Cowboy-native rendering is trusted only after exact decode→re-encode.
- **Facade misuse.** The facade refuses transaction-sending and EVM-execution methods. An
  Ethereum-format transaction is never interpreted as a Cowboy submission.
- **Release identity.** Connectors compare every configured `/chain-info` mirror to one pinned
  activation-manifest digest before building typed data. Chain/provider/session changes invalidate
  the operation.

## 13. Protocol Constants

| Constant | Value |
|---|---|
| `CURRENT_TX_VERSION` | `2` |
| transaction kinds decoded | `0` ordinary, `1` decode-only `TradingPostSpawn`; unknown rejects |
| transaction kinds admitted/executed | `0` only |
| `signature_mode` | `0` native, `1` EIP-712; all others invalid |
| transaction domain type | `EIP712Domain(string name,string version,uint256 chainId,bytes32 salt)` |
| transaction domain | `("Cowboy Transaction", "2", chain_id, chain_instance_id)` |
| facade endpoint | `POST /eth`, batch cap 16 |
| transaction discovery | `/chain-info.transaction_signature_modes = ["native","eip712"]` exactly |
| chain-instance derivation | `Keccak-256(b"cowboy/chain-instance/v1" ‖ SHA-256(JCS(activation_payload)))` |
| chain-ID registry | `docs/cips/registries/cip-40-chain-ids-v1.json` |
| native-domain rule | no native signing preimage begins with `0x19` |

## 14. Release Corpus

This document freezes the corpus schema and minimum matrix. It does not publish placeholder v2
bytes or hashes. Before release, implementation-generated vectors are independently recomputed and
pinned in every consumer.

A successful kind-0 vector contains:

- `input.json` and `chain_identity.json`; the latter is exactly the closed object
  `{"chain_id":"<canonical u64 decimal>","chain_instance_id":"0x<64 lowercase hex>"}`;
- exact `preimage.bin`, `signing_hash.hex`, and `submission.bin`;
- for mode 1, exact `typed_data.json`, `domain_separator.hex`, `typehash.hex`,
  `struct_hash.hex`, `digest.hex`, and `signature.hex`.

Every `.bin` file is raw bytes. Every `.hex` file is lowercase hexadecimal without `0x` and with
exactly one trailing LF; hashes contain 64 digits and signatures 130. JSON files are UTF-8, closed
schema, duplicate-key-free, and end in one LF. `typed_data.json` is the exact object passed to
`eth_signTypedData_v4`, not a hand-built equivalent.

A decode-only kind-1 fixture instead contains exactly `input.json`, `submission.bin`, and
`expected_rejection.txt`. It has no signing preimage, signing hash, typed-data object, or
signature. The shared codec must decode and re-encode it byte-for-byte; RPC, mempool, directly
proposed block, block import, replay, and execution fixtures must all reject it before lane/state
use. Unknown kinds reject at the outer discriminant before body decode.

The kind-1 `expected_rejection.txt` contains exactly `TxKindDisabled(1)\n`; every boundary harness
asserts that semantic rejection after successful decode. A raw decode-failure fixture contains
exactly `submission.bin` and `expected_decode_error.txt`; the text file contains one stable codec
error token plus LF. Decode failures are not mislabeled as disabled-kind failures.

The kind-0 matrix covers native and EIP-712 transfer, actor execution, multisig, all three
fee-override encodings (`None`, `Some(ZERO)`, `Some(nonzero)`), maximum-width lossless integer
JSON, provider `v` normalization, duplicate-primary rejection, wrong chain/instance/domain,
version/kind/mode mutation, malformed Option tags, high-S, invalid recovery id, deferred-mode
invariants, and signature-byte-independent identity. An independent EIP-712 implementation consumes
the canonical wasm-emitted object.

The release matrix exercises protocol codec, node RPC/mempool/block verification/replay/execution,
CLI, wasm, FFI, Python/JS SDKs, connector, wallet, indexer, and explorer. It also pins the §16
JCS/identity fixture; the closed `/chain-info` schema; one-divergent-mirror rejection; injected
provider switch/add behavior including `4902` and `4001`; WalletConnect session invalidation; and
the facade's exact request/result/error schemas, batch and notification behavior, quantities,
finalized-tag aliases, complete compatibility-block object, balance scaling, method refusal,
endpoint-scoped CORS, credential omission, and preflights. Tests also prove that an adjacent
authenticated RPC route receives none of these permissive CORS headers and that a configured but
non-recomputed manifest digest cannot be served as local chain identity. Registry vectors cover
empty, one-row, numeric sorting (`"9"` before `"10"`), duplicate ID/network, extra member, and
missing/excess final-LF cases.

## 15. Rationale

- **Generic `bytes instruction` over per-instruction typed structs.** Expanded structs would make stock-wallet prompts legible, but each adds another reconstruction path for a large instruction enum. The generic form gives one verifier and full cryptographic binding. Stock-wallet blind signing remains an explicit product/security acceptance gate; a connector-rendered summary is not part of the signed authorization.
- **Structural invariants over a catch-all hash field.** The alternative to forbidding `access_list`/`origin_*` in mode 1 was binding them via an extra `bytes32 tx_extra` field. The invariant approach was chosen because the fields are semantically meaningless in a caller-signed, non-deferred transaction today — forbidding them costs nothing, keeps the typed struct legible, and the typehash-change mechanism handles their future activation.
- **`eip155` over a `cowboy` CAIP namespace.** A native namespace would require wallet-specific support. `eip155` + typed data reuses provider machinery in wallets that support the Cowboy chain; §11 does not claim universal compatibility. The facade answers only the pinned read-only subset and refuses transactional methods.
- **Mode as a per-transaction field, not per-signer.** Per-signer modes would let a MetaMask co-signer join a natively-signed multisig, at the cost of a mode vector in the encoding and a per-signer digest branch in verification. Deferred until a concrete need exists; the typed struct already binds the signer set, so adding per-signer modes later is a version bump, not a redesign.

## 16. Clean-Genesis Release Contract

This release starts from one clean genesis and defines no state-conversion procedure. The immutable
`Cip40ActivationV1` payload is a closed JSON object with exactly these six members:

| Member | Exact JSON representation |
|---|---|
| `chain_id` | canonical unsigned decimal `u64` string; production value nonzero |
| `format_version` | JSON number exactly `1` |
| `genesis_config_sha256` | `0x` plus exactly 64 lowercase hex digits |
| `genesis_nonce` | `0x` plus exactly 64 lowercase hex digits; fresh 32-byte CSPRNG value |
| `network` | ASCII `[a-z][a-z0-9-]{0,31}` |
| `transaction_version` | JSON number exactly `2` |

`genesis_config_sha256` hashes the exact separately pinned genesis-input bytes. Those bytes
completely determine validator/consensus configuration and height-zero application state except for
the payload-derived genesis parent. `make_genesis_v2` rejects a mismatch before construction.

Duplicate keys, non-I-JSON input, missing/extra members, or values outside the grammars reject before
RFC 8785 canonicalization. Derivations consume raw digest bytes:

```text
activation_payload_hash = SHA-256(RFC 8785 JCS(activation_payload))
chain_instance_id       = Keccak-256(
                            ASCII("cowboy/chain-instance/v1")
                            ‖ raw_32_bytes(activation_payload_hash)
                          )
genesis_v2.parent       = raw_32_bytes(chain_instance_id)
```

The final manifest is the same object plus exactly two lowercase bytes32 strings,
`activation_payload_hash` and `chain_instance_id`.
`activation_manifest_digest = SHA-256(RFC 8785 JCS(final_manifest))` is an external release pin,
not a manifest member. Keccak-256 is the Ethereum function, not FIPS SHA3-256.

The following fixture is normative for JCS and hash construction. Its genesis-input bytes are the 31
ASCII bytes `{"accounts":[],"validators":[]}` with no newline. `26909` is illustrative and
unallocated, so it reproduces the bytes but fails the separate production allocation gate.

```text
genesis_config_sha256 = 0xb46e81a9bc58b918716cf5eb58c1a54c6de65668e63b2704a5b0ea24bae95075

JCS payload (258 UTF-8 bytes, no newline):
{"chain_id":"26909","format_version":1,"genesis_config_sha256":"0xb46e81a9bc58b918716cf5eb58c1a54c6de65668e63b2704a5b0ea24bae95075","genesis_nonce":"0x0101010101010101010101010101010101010101010101010101010101010101","network":"mesa","transaction_version":2}

activation_payload_hash = 0x2b6a1de5e097e9608fe5675646e4b586ac6a64d25e1e4e86f1fd1cf8240aa60a
chain_instance_id       = 0x9d5585bb2f3bc88cb10fb37e0bed53e7f482dacc29a194fba0873c8bf2871812

JCS final manifest (442 UTF-8 bytes, no newline):
{"activation_payload_hash":"0x2b6a1de5e097e9608fe5675646e4b586ac6a64d25e1e4e86f1fd1cf8240aa60a","chain_id":"26909","chain_instance_id":"0x9d5585bb2f3bc88cb10fb37e0bed53e7f482dacc29a194fba0873c8bf2871812","format_version":1,"genesis_config_sha256":"0xb46e81a9bc58b918716cf5eb58c1a54c6de65668e63b2704a5b0ea24bae95075","genesis_nonce":"0x0101010101010101010101010101010101010101010101010101010101010101","network":"mesa","transaction_version":2}

activation_manifest_digest = 0x14f29f8611696652b46c49660f0c6ed69e5708c4c1fdd3844d5c1f36dc4b0d99
```

Node startup recomputes the payload hash, chain instance, genesis parent, final-manifest bytes, and
external manifest digest and refuses a mismatch. The deployed `/chain-info` implementation is not
yet this contract: its current two-member response and numeric `chain_id` must be replaced by the
closed six-member §10 object before connector release.

The release set is deliberately narrow:

1. land CIP-40, CIP-28's reciprocal field/type mapping, CIP-33's reciprocal decoder policy, the
   Technical Whitepaper §2/Appendix A mirror, the changelog, and the chain-ID registry artifact;
2. allocate a production `(network, chain_id)` row separately, derive one clean genesis, and start
   validators only after every node agrees on chain identity, manifest digest, and v2 codec;
3. deploy protocol codec, node, CLI, wasm/FFI, SDKs, connector, wallet, indexer, and explorer only
   after the §14 corpus passes at each real sign/verify/import boundary.

Route-authentication, payment, governance, storage, session, banking admission, and actor-fork
redesigns are not activation dependencies or readiness claims of this release. CIP-40 owns only the
outer transaction wire's `fee_payer_override` encoding; CIP-28 retains all Bank behavior. Kind 1
remains decoder-only and consensus-invalid until a later CIP-33 release explicitly changes that
policy.
