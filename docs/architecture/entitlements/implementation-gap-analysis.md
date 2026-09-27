# Entitlements: Spec vs Implementation Gap Analysis

**Date:** 2026-04-07 (updated)
**Previous:** 2026-03-30

## What's Implemented (node code)

The node has a **generic RBAC-style entitlement system** built around `Scope`/`Action`/`Constraints`:

- **6 system instructions**: Grant, Revoke, Delegate, CreateRole, AssignRole, RevokeRole
- **Storage layer**: On-chain persistence in the entitlement registry system actor (`0x07`), with per-address cap of 256 entitlements and re-grant protection (immutable constraint fields cannot be relaxed)
- **Permission checker**: Two modes — `check_permission()` (read-only validation) and `consume_permission()` (increments `used_count`, tracks rate-limit windows, persists). Scope matching (Global, Namespace prefix, exact), action matching (wildcard, exact), constraint validation (block height windows, max uses, rate limits)
- **Amount-aware consumption**: `consume_use_owner_balance()` validates `max_amount_per_use`, accumulates `total_amount_used`, and enforces rate limits — used for actor gas payment delegation
- **Delegation**: Parent-child entitlement chains with `delegatable` flag, depth enforcement (`delegation_depth_max` decremented on each sub-delegation, rejected at 0), and constraint narrowing
- **Gas costs**: Defined for all operations (grant: 5k cycles, revoke: 2k, delegate: 5k, check: 500, role: 10k)
- **Tests**: 11 unit tests in `checker.rs` covering spec requirements H3.1–H3.7 and H4.1–H4.2

### Key files

| File | What it does |
|------|-------------|
| `node/types/src/entitlement.rs` | Core types: `Scope`, `Action`, `Constraints`, `Entitlement`, `Role` (553 lines) |
| `node/execution/src/entitlement/grant.rs` | Grant, revoke, delegate operations |
| `node/execution/src/entitlement/role.rs` | Role creation and assignment |
| `node/execution/src/entitlement/checker.rs` | `check_permission()`, `consume_permission()`, `consume_use_owner_balance()` (809 lines) |
| `node/execution/src/entitlement/storage.rs` | On-chain storage layer (`EntitlementStore`) |
| `node/execution/src/execution/system_instruction.rs` | System instruction dispatch |
| `node/execution/src/execution/entitlement_handlers.rs` | Public handler wrappers |
| `node/execution/src/gas.rs` | Gas cost definitions |
| `node/execution/src/transaction.rs` | `consume_use_owner_balance` integration for tx fee payment |

## What's NOT Implemented

The spec (`entitlements-specification.mdx`) and CIP-2 define a much richer system. The following are the major gaps, ordered by protocol-security impact.

### 1. VM syscall gating (Spec Section 6.3) — CRITICAL

The spec requires sensitive syscalls to fail deterministically if the actor lacks the corresponding entitlement.

**Missing:**
- PVM syscalls (timer, http, storage, etc.) execute without any entitlement checks
- No `ERR_MISSING_ENTITLEMENT` trap
- No `ERR_ENTITLEMENT_QUOTA_EXCEEDED` trap

**Impact:** Any deployed actor can use any syscall unconditionally. This is the highest-priority gap — without it, the entitlements system is advisory only.

### 2. Actor manifest / deployment-time validation (Spec Section 6.1) — CRITICAL

The spec requires actors to declare entitlements at deployment in a manifest:

```json
{
  "entitlements": [
    {"id": "econ.transfer", "params": {"max_amount": 1000}},
    {"id": "http.fetch", "params": {"allowlist_domains": ["api.coingecko.com"]}}
  ]
}
```

**Missing:**
- No manifest parsing at deployment (`actor_instruction.rs` DeployActor has no manifest checks)
- No rejection of unknown entitlements
- No parameter schema validation (Spec Section 10)
- No duplicate entitlement rejection
- No lexicographic sort enforcement

### 3. Runner attestation and scheduler matching (Spec Sections 5.1, 6.2) — CRITICAL

The spec's core enforcement model: `Actor.requires ⊆ Runner.provides`.

**Missing:**
- No `RunnerAttestation` record type
- No CBOR serialization or signing of attestations
- No `evidence_hash` for TEE quote bundles
- No capability matching in the scheduler/job dispatcher
- Zero entitlement awareness in runner code

### 4. Normative entitlement registry (Spec Section 9)

The spec defines 15 specific entitlements with typed parameter schemas:

| Entitlement | Inheritable | Attested | Quota | Status |
|-------------|:-----------:|:--------:|:-----:|--------|
| `exec.spawn` | Yes | No | Yes | Not implemented |
| `sys.upgrade` | No | No | No | Not implemented |
| `http.fetch` | Yes | Yes | Yes | Not implemented |
| `storage.kv` | Yes | No | Yes | Not implemented |
| `storage.blob` | Yes | No | Yes | Not implemented |
| `oracle.llm` | No | Yes | Yes | Not implemented |
| `accel.gpu` | No | Yes | Yes | Not implemented |
| `sec.tee_required` | Yes | Yes | No | Not implemented |
| `sec.data_residency` | Yes | Yes | No | Not implemented |
| `econ.hold_balance` | Yes | No | No | Not implemented |
| `econ.transfer` | No | No | Yes | Not implemented |
| `timer.schedule` | Yes | No | Yes | Not implemented |
| `bridge.asset` | No | No | Yes | Not implemented |
| `bridge.subscribe_event` | No | No | Yes | Not implemented |

The node code uses a generic `Action` enum (`TokenTransfer`, `ActorDeploy`, etc.) that does not map to the spec's dotted-string IDs.

### 5. Parameter compatibility checking (Spec Section 8)

**Missing:**
- Domain allowlist subset matching (Section 8.2)
- Domain set union logic (Section 8.4)
- `max_*` range comparison (Section 8.3)
- `tee_type` and `region` equality matching (Section 8.3)
- Wildcard `*` superset logic

### 6. Inheritance rules (Spec Section 7)

**Missing:**
- No `inheritable` flag tracking per entitlement
- No enforcement that child actor entitlements are a strict subset of parent's inheritable set
- No inheritance logic in actor spawn path

### 7. Quota consumption — partially implemented

`consume_permission()` increments `used_count` and tracks rate-limit windows, but does **not** accept an `amount` parameter and therefore cannot enforce amount-based quotas. `consume_use_owner_balance()` does validate amounts but is only wired up for the gas-payment delegation path.

**Remaining gaps:**
- `consume_permission()` has no `max_amount_per_use` or `total_amount_used` enforcement (no amount parameter)
- `max_requests`, `max_tokens`, `max_bytes` consumption tracking for normative entitlements
- Rate-limit `max_per_window` enforcement (window tracking is implemented but the cap is not checked against)

### 8. Attestation expiry (Spec Section 6.4)

**Missing:**
- No attestation validity window checking (`valid_from`/`valid_until` on attestation records)
- No expired attestation rejection in scheduler

### 9. Deprecation and removal lifecycle (Spec Section 9.11)

**Missing:**
- No governance deprecation proposals
- No deprecation window tracking
- No warning events for new deployments using deprecated entitlements
- No non-executable actor state (`ActorNonExecutable` event)
- No `DEPRECATION_WINDOW` constant (recommended 604,800 blocks)

### 10. Deterministic error codes (Spec Section 11)

**Missing:**
- `ERR_MISSING_ENTITLEMENT`
- `ERR_ENTITLEMENT_QUOTA_EXCEEDED`
- `ERR_ENTITLEMENT_PARAM_INVALID`
- `ERR_ACTOR_NON_EXECUTABLE`
- `ERR_SYSTEM_ENTITLEMENT_UNAUTHORIZED`

### 11. Scope authorization gaps in existing code

| Location | Issue |
|----------|-------|
| `grant.rs:27` | Global scope authorization is TODO — currently returns `InvalidData` |
| `grant.rs:28` | Only `Actor` scope grants work; `Token`, `Runner`, `RunnerPool`, `Namespace` scopes always fail |

## Summary

The node has a **working RBAC primitive** with grant/revoke/delegate/roles, on-chain storage, use-count and rate-limit tracking, delegation depth enforcement, and 11 spec-aligned unit tests. The `consume_use_owner_balance` path demonstrates full amount-aware consumption for gas delegation.

What's missing is the **domain-specific entitlements layer**: the 15 normative entitlements, actor manifests, runner attestations, scheduler matching, syscall gating, parameter compatibility, and amount-aware quota consumption for `consume_permission`. The plumbing exists but the spec-defined semantics are largely unimplemented. The three critical gaps — syscall gating, manifest validation, and scheduler matching — are all enforcement boundaries. Without them, entitlements are declarative only.
