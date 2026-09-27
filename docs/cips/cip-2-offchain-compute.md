---
title: "CIP-2: Verifiable Off-Chain Compute"
description: Verifiable, asynchronous off-chain computation framework with Fisher-Yates VRF runner selection, adaptive committee sizing, stake × √reputation weighting, EMA reputation, commit-reveal aggregation with eligibility-threshold aggregator selection, JobType::Custom extension pattern, slash distribution schema, and Entitlement-based Runner Pool access control.
---

<Note>
  **Status:** Draft for Internal Review  
  **Type:** Standards Track  
  **Category:** Core  
  **Created:** 2025-10-02  
</Note>

<Tip>
  This specification defines the verifiable, asynchronous off-chain compute framework: Fisher-Yates VRF runner selection, adaptive committee sizing, `stake · sqrt(reputation)` VRF weighting with EMA reputation (14-day half-life), commit-reveal aggregation with eligibility-threshold aggregator selection, non-reveal classification with `CrashAttestation` exemption, the `JobType::Custom` extension pattern, agent-job validation (per-job loop-budget caps and path-traversal rejection, §3.1), an explicit slash-distribution schema (defaulting to 100% burn per WP §8.4 C7), SemanticSimilarity embedding-model pinning, and Entitlement-based Runner Pool access control.
</Tip>

# CIP-2: Verifiable Asynchronous Off-chain Computation Framework

---

### **Abstract**

This proposal introduces a framework for executing verifiable, off-chain computations within the Cowboy ecosystem. It defines a standardized, asynchronous protocol for smart contracts to request external data fetching, complex computations, or AI model inferences from a decentralized network of off-chain "Runners." The architecture is built on a deterministic, stake-weighted VRF selection mechanism using Fisher-Yates shuffle, ensuring verifiable task assignment that is resistant to correlation attacks and Sybil manipulation. The core design philosophy emphasizes user-centric verification, task clarity through mandatory result schemas, and on-chain stability via a deferred transaction model for callbacks (per CIP-1).

---

### **Motivation**

To unlock advanced use cases involving AI/ML, large datasets, or Web2 APIs, smart contracts need a secure and reliable bridge to the off-chain world. This CIP proposes a flexible and unopinionated approach, tailored for Cowboy's on-chain Python environment, that addresses the limitations of existing oracle solutions.

This framework empowers developers to:

1. **Integrate Complex Logic:** Run Python-based AI model inferences or heavy computations off-chain.  
2. **Preserve On-chain Stability:** Utilize an asynchronous, deferred transaction model to prevent network congestion from off-chain interactions.  
3. **Achieve Verifiable Decentralization:** Leverage a stake-weighted VRF-based system to eliminate centralized schedulers and allow anyone to verify task assignments.  
4. **Enforce Clarity:** Mandate task and result schemas to reduce ambiguity and ensure Runners can reliably execute and be verified.  
5. **Choose Their Trust Model:** Allow developers to select the number of Runners, the verification mode, and optional Runner pool constraints for their specific application's security needs.

---

### **Specification**

> **Normative conventions.** The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** in this document are to be interpreted as described in RFC 2119.

The framework consists of seven on-chain System Actor components and the off-chain Runner network:

| Address | System Actor | Role |
|---------|-------------|------|
| `0x0000…0001` | **Runner Registry** | Runner registration, staking, capabilities, health, reputation |
| `0x0000…0002` | **Job Dispatcher** | Job submission, VRF selection, job lifecycle management |
| `0x0000…0003` | **Result Verifier** | Commit-reveal aggregation, result verification, callback dispatch |
| `0x0000…0004` | **Secrets Manager** | Encrypted secret storage and TEE-gated secret release |
| `0x0000…0005` | **TEE Verifier** | Remote attestation verification for TEE-based runners |
| `0x0000…0007` | **Entitlement Registry** | Permission management: Runner Pool access control and general RBAC (see §7) |
| — | **Off-chain Runners** | External nodes that execute tasks and submit results |

> **Address gap note:** `0x0006` is allocated to `DUAL_BASEFEE` (CIP-3). The post-2026-04 address space extends through `0x13` and includes a virtual entry at `0x1D`; the canonical assignments live in `node/runner/src/system_actors.rs` and the per-CIP specifications that introduce each address.

#### **1. Task Definition and Result Schema**

Every off-chain task submission **must** include a `result_schema`. This mandatory payload defines the explicit output constraints of the task, enabling objective validation by the protocol.

The schema defines at minimum:

* `max_return_bytes`: Maximum result size to prevent gas-bombing attacks.  
* `expected_execution_ms`: Target execution time to help Runners assess feasibility.  
* `data_format`: Expected data structure (e.g., JSON schema, binary format).

#### **2. Job Specification**

Every job submitted to the Dispatcher carries a `JobSpec`:

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | H256 | Unique job identifier |
| `job_type` | JobType | `Llm`, `Http`, `Mcp`, `Agent`, or `Custom` (the `Agent` runner-loop job encoding is defined in CIP-11 §9.4; §3.1 below is authoritative for its loop-budget caps + path validation) |
| `bounds` | ResourceBounds | Resource limits for execution |
| `verification` | VerificationConfig | Verification mode and parameters |
| `max_price` | u64 | Maximum price (CBY wei) |
| `tip` | u64 | Priority tip |
| `timeout_blocks` | u64 | Timeout in blocks |
| `callback` | CallbackInfo | Callback Actor and handler |
| `submitter` | Address | Submitting Actor's address |
| `submitted_at` | u64 | Submission block height |
| `required_runner_pool` | `Option<Vec<u8>>` | Optional: Pool ID bytes; only Runners holding a `RunnerJoinPool(pool_id)` Entitlement may be selected (§7) |
| `attachments` | `Option<Vec<VolumeAttachment>>` | Volume attachments — the wire-canonical 10-field struct of CIP-9 §8.1; admission caps in CIP-9 §8.1.1 |
| `secret_refs` | `Vec<SecretKeyRef>` | The `secrets=` read set: `{account, key_hash}` pairs (CIP-24 §3.1, §5.2) |
| `precondition` | `Option<JobPrecondition>` | Inbound-verification precondition (`verify=`, CIP-24 §5.5) |
| `hire_binding` | `Option<HireBinding>` | Trading Post hire binding — `(hire_id, pinned artifact)` the job executes under (CIP-33 §2.5.1) |
| `result_schema` | `Option<ResultSchema>` | Output constraints (§1 above); mandatory on submission paths that return results |
| `req_hash` | H256 | **Node-generated**: the `JobType`-only digest stamped at submission (CIP-23 §3.5.1); excluded from the `job_spec_hash` preimage (CIP-11 §12.6) |

This table is the complete `JobSpec` field set, in wire order. The canonical
encoding of the struct and of every nested type, the `job_spec_hash` recipe,
and the generated-field rules (`job_id`, `submitter`, `submitted_at`,
`req_hash` are node-stamped and excluded from the client submission wire) are
defined by `cowboy-protocol-codec::job_spec` per CIP-11 §12.6 — any field
added to the struct MUST be added to this table and to the CIP-11 §12.6
preimage enumeration in the same amendment.

#### **3. Core Workflow (Asynchronous & Deferred)**

```
Actor calls submit_job(job_spec)
    │  → Job Dispatcher (0x0000...0002)
    │
    ▼
1. Validate JobSpec (resource bounds within the §3.1 caps; verification.runners ≥ 1; threshold ∈ 1..=runners; max_price ≥ 0)
2. Record submission_block S → fixes the candidate snapshot (T = S)
3. Build candidate list (see §4)
4. M == 1: derive the selection seed from the parent beacon (see §5),
           run weighted VRF selection in block S → assign 1 runner,
           store job → JobStatus::Assigned
   M > 1:  bind the job — no assignment in block S; selection runs at the
           pending-selection pass once the verified threshold seed for the
           absolute round r_seed = advance_round(r_submit, K) is committed
           (CIP-11 §9.2) → assign M runners, store job → JobStatus::Pending

Runner polls get_assigned_jobs(runner_addr)
    │
    ├── Execute → submit commitment → Aggregator collects → submit_result
    └── Do not execute → job times out → automatic re-selection (see §6)

Result Verifier (0x0000...0003)
    └── Verify commitments → produce VerifiedResult → deferred callback to Actor
```

##### **3.1 JobSpec Validation Caps (normative)**

Step 1 above validates every submitted `JobSpec` authoritatively, on **all three**
submission paths (`JobRequest`, canonical `JobSpec` JSON, legacy CBOR), before any
selection work. These are **raw-input upper bounds** that the chain does not otherwise
recompute — their purpose is anti-DoS, rejecting absurd inputs early. The node
implementation is `execution/src/runner/normalize.rs::validate()`.

| Field | Cap | Rationale |
|-------|-----|-----------|
| `bounds.max_input_tokens` | 1,000,000 | Bound per-job LLM input cost |
| `bounds.max_output_tokens` | 1,000,000 | Bound per-job LLM output cost |
| `bounds.max_wall_time_seconds` | 3,600 | Bound per-job wall-clock |
| `bounds.max_memory_mb` | 65,536 | Bound per-job memory request |
| `bounds.max_retries` | 10 | Bound per-job execution retries |
| `verification.runners` | 64 (`MAX_RUNNERS`) | Bound the requested committee **input** |

**Agent job-type caps (normative; apply only when `job_type == Agent`).** These bound the
per-job agent reasoning loop, enforced in the same `validate()` gate and ratified with the
COW-2136 path-hardening work (node impl: cowboyinc/node#844). The agent loop is
*additionally* bounded by `bounds.max_wall_time_seconds` above; these caps further bound
model/tool-call cost amplification within that wall-clock window.

| Field | Cap | Rationale |
|-------|-----|-----------|
| `job.max_iterations` | 256 (`MAX_AGENT_ITERATIONS`) | Bound agent reasoning iterations |
| `job.max_tool_calls_per_iter` | 64 (`MAX_AGENT_TOOL_CALLS_PER_ITER`) | Bound tool calls per iteration |
| `job.timeout_seconds` | 600 (`MAX_AGENT_TIMEOUT_SECONDS`) | Bound the requested agent-loop wall-clock |

Additional structural checks: `verification.runners ≥ 1`; `verification.threshold ∈
1..=runners`; DNS verifier checks (`DnsTxtRecordMatch` / `DnsCnameMatch`) are unsupported: node admission rejects jobs containing either variant with `UnsupportedInstruction`. Codec validation may reject an invalid combination earlier. Neither check can dispatch a DNS-verification job. For `JobType::Agent`, the
path-bearing fields `system_prompt_path`, `session_dir`, and `session_id` MUST be safe
**relative** paths — rejected (`unsafe_path`) if empty, absolute (leading `/` or `\`),
carrying a colon (`:`) anywhere (drive letter or URI scheme), or containing a `..` path
segment (including a segment that Win32 trailing dot/space stripping would collapse to `..`,
e.g. `.. ` or `...`). This is an on-chain **pre-filter**. The runner additionally
canonicalizes the `system_prompt_path` read and confines it under the mounted volume root:
volumes are mounted per CIP-9 §12.1 (`/mnt/volumes/{volume}/`) inside the container-isolation
boundary of CIP-10 §14, and the canonicalization + root-prefix containment itself is
implemented in the runner agent's `Sandbox` (cowboyinc/runner#133). With both layers, an
agent path traversal is rejected here at admission and — were one ever admitted — still
cannot escape the mounted volume at the runner (COW-2136 / cowboyinc/node#844,
cowboyinc/runner#133).

Notes on scope, to prevent two recurring confusions:

- **`verification.runners` (≤ 64) bounds the *request*, not the committee that runs.**
  For quorum modes the dispatcher computes the *effective* committee size adaptively
  and clips it to `M_max` (§5.1); for `None`/`EconomicBond` it dispatches exactly one
  runner (§5.1, *Scope of adaptive sizing*). A submitter may name up to 64 runners,
  but the verification mode and adaptive sizing govern how many are actually selected.
- **`bounds.max_retries` (per-job execution retries, ≤ 10) is distinct from
  `MAX_RETRIES` (default 3)** in §6.3 (Timeout-based re-selection), which counts
  runner-selection retry rounds before a job is marked `Failed`. They are unrelated knobs.

`max_price` is **not** required to be positive: `max_price = 0` denotes a best-effort /
unpriced job. The dispatcher escrows `max_price + tip` only when that sum is positive,
and itself submits internal jobs with `max_price = 0`. With both operands `u64`, the
escrow sum MUST be computed with overflow-checked (or widened) arithmetic; a submission
whose sum overflows is rejected at admission, never wrapped. `bounds` and `timeout_blocks` are
never zero in a normalized `JobSpec` because the node fills non-zero defaults for any
omitted field (`normalize.rs::default_bounds` / per-job-type default timeout). The caps in
this section — the table above, the agent job-type caps, and the structural checks — are
the values `validate()` enforces against raw submitter input.

#### **4. Runner Registry & Candidate List Construction**

**System Actor:** `0x0000...0001`

The Registry maintains runners with full registration data including stake, capabilities, rate card, health, and reputation. The runner stake floor is `stake ≥ max(10,000 CBY, 1.5 × declared_max_job_value)` (CBY-denominated; see §12 for the HOLD rationale on CBY-vs-USD pegging).

**Candidate list construction** (at `submission_block`):

1. **Health filter:** `HealthStatus::Healthy` (heartbeat received within `heartbeat_timeout_blocks`)  
2. **Reputation filter:** `reputation ≥ 50` (against the EMA-updated reputation defined in §6.1)
3. **Capability filter:** Runner supports the required `JobType` and — for `Llm`/`Agent` jobs — the requested model (see *Model advertisement* below)
4. **TEE filter:** If `tee_required`, runner must declare TEE support (attestation verified by TEE Verifier `0x05`)  
5. **Price filter:** Estimated cost ≤ `job_spec.max_price`  
6. **Concurrency filter:** `active_jobs < max_concurrent_jobs`  
7. **Entitlement filter** *(optional)*: If `required_runner_pool = Some(pool_id)`, the Entitlement Registry (`0x07`) must confirm that the runner holds an Entitlement with `Scope::RunnerPool(pool_id)` and `Action::RunnerJoinPool(pool_id)` that has not expired and has not exceeded its `max_uses` (see §7.3)

**Model advertisement.** A runner's capabilities carry `supported_models`: the set of model names it serves for `Llm`/`Agent` jobs, in the namespace jobs request. `["*"]` advertises any model; an empty set matches only jobs with no model constraint. A runner advertising the `llm` or `agent` job type MUST advertise at least one model (`["*"]` is the explicit any-model opt-out). The capability filter admits a runner for a model-bearing job only when `supported_models` contains `"*"` or the job's model: `Agent` jobs match the requested `model` string directly; `Llm` jobs match `blake3_id(name)` against the job's `model_id`, with the no-model encoding `blake3_id("")` treated as unconstrained. The set is bounded and validated identically at registration and on every `RunnerUpdateCapabilities` (≤ 64 entries, ≤ 128 bytes each; entries non-empty, unique, and free of leading/trailing whitespace — the stored strings are exactly what selection compares against, no normalization) so this per-dispatch, per-candidate check stays cheap. The **same predicate scopes the capability-eligible pool** that adaptive committee sizing draws against (§5.1): a model served by few runners sizes its committee from that smaller pool rather than the full active set. Capabilities are part of the registration record and may be replaced in place — wholesale, not merged — via a `RunnerUpdateCapabilities` registry transaction (owner-authenticated, same registry actor `0x0000...0001`), taking effect at the next block's selection without deregistering; in-flight assignments are unaffected.

**Candidate list ordering:** After filtering, candidates are **sorted ascending by address bytes** to produce a globally consistent, deterministic ordered list. This sort is mandatory — different nodes must arrive at the identical ordered list.

**Candidate list snapshot:** The filtered and sorted list is fixed at `submission_block`. Runners who join or leave after submission do not affect the selection for this job.

#### **5. Verifiable Runner Selection (Fisher-Yates VRF)**

##### **5.1 Adaptive committee sizing**

The committee size adapts to the active runner population `N_active` and the Herfindahl-Hirschman Index `HHI` of effective stake:

```
M_target(N_active, HHI) = ceil(2 · log₂(N_active) / max(HHI, HHI_min))
M       = clip(M_target, M_min, M_max)
N_threshold = ceil(2 · M / 3)
```

Parameters (Tier-0 tunable, stored at `0x09` under `system:cip2:committee.*`):

| Parameter | Default | Notes |
|---|---|---|
| `M_min` | 3 | Floor for the committee size (matches BFT N≥3 minimum) |
| `M_max` | 9 | Ceiling (caps verification cost per job) |
| `HHI_min` | 0.01 | Numerical floor on the denominator — prevents division blow-up when HHI is computed over near-uniform stake distribution |
| `HHI_smoothing_alpha` | 0.125 | EMA smoothing factor applied to HHI to prevent committee-size flapping on single-block stake shocks |
| `committee_recompute_period` | 1 epoch (= 3,600 blocks at 1s, per WP §13) | Frequency of M recomputation; on each recompute boundary, jobs in flight retain their committee, new dispatches use the new M |

**HHI computation.**

```
shares       = effective_stake[i] / Σ effective_stake[i]    over registered + healthy runners
HHI_instant  = Σ shares[i]²                                  ∈ (0, 1]
HHI          = HHI_smoothing_alpha · HHI_instant + (1 − HHI_smoothing_alpha) · HHI_prior_epoch
```

`effective_stake` per CIP-13 §3.2 is `registration.stake + delegation_totals.total_active`.

**Worked example.**

| `N_active` | `HHI` | `M_target` | `M` (after clip) | `N_threshold` |
|---|---|---|---|---|
| 20 | 0.40 | ceil(2·4.32 / 0.40) = 22 | 9 (clip @ M_max) | 6 |
| 50 | 0.20 | ceil(2·5.64 / 0.20) = 57 | 9 (clip) | 6 |
| 100 | 0.05 | ceil(2·6.64 / 0.05) = 266 | 9 (clip) | 6 |
| 100 | 0.30 | ceil(2·6.64 / 0.30) = 45 | 9 (clip) | 6 |
| 100 | 1.00 (1 runner monopoly) | ceil(2·6.64 / 1.00) = 14 | 9 (clip) | 6 |
| 8 | 0.40 | ceil(2·3.00 / 0.40) = 15 | 9 (clip) | 6 |
| 4 | 0.50 | ceil(2·2.00 / 0.50) = 8 | 8 | 6 |
| 3 | 0.40 | ceil(2·1.58 / 0.40) = 8 | 8 (limited by N_active in dispatch) | 6 |

Empirically: at low concentration (HHI ≤ 0.30), M saturates at `M_max = 9`; at very concentrated runner sets (HHI > 0.40) and small `N_active`, M scales back toward the floor. The clip bounds prevent both committee-size explosion (DoS via large committees) and undersized committees on small networks.

**Scope of adaptive sizing.** Adaptive sizing governs the **quorum verification modes** — `MajorityVote`, `StructuredMatch`, `Deterministic`, and `SemanticSimilarity` (the `N ≥ 2` / `N ≥ 3` rows of the §9 table). It does not apply to the single-runner modes: a job with `verification.mode ∈ {None, EconomicBond}` MUST be dispatched with **`M = 1`, `N_threshold = 1`**, selected via the §5.2 immediate mode (`0x00`), per the §9 table and WP §9.2 (`economic_bond` = 1 runner). No output of the formula above can be 1 at defaults (dispatch additionally caps `M` at the eligible pool, a separate mechanism — see the `N_active = 3` worked-example row) — `M_min = 3` is a quorum-mode floor matching the BFT `N ≥ 3` minimum, not a lower bound on single-runner dispatch. In both cases `verification.runners` remains a raw request bound (the §3.1 structural checks are unchanged), and the chain writes the effective `runners`/`threshold` values into the stored spec at submission. Single-runner dispatch inherits the immediate mode's security trade-off — no proposer-inclusion neutrality; submitters that need selection neutrality SHOULD request a quorum mode — see Security Considerations (*VRF Grinding*) for the analysis.

##### **5.2 VRF Seed (deterministic, no private key required)**

The randomness source for runner selection is a verified `commonware` threshold-VRF `Seed { round: Round(epoch, view), signature }` identified by a **named absolute consensus round** — never a block hash, never a height-anchored beacon (`R_{H-1}`/`R_n` addressed by height), and never "the seed of whatever round finalizes height `S`". Height `S` and `Seed.round` are not interchangeable.

- **`M == 1`:** selection is immediate in submission block `S` (no added latency), using the parent beacon `R_{S-1}` (notation below), known before executing `S`. Immediate mode trades proposer-inclusion neutrality for zero added latency (see Security Considerations).
- **`M > 1`:** commit-then-reveal. `JobSubmit` binds the job in block `S`; the committee is selected later from the verified seed of the absolute future round `r_seed = advance_round(r_submit, K)`, where `r_submit = Round(epoch, view)` is the round in which block `S` was proposed and `K = SELECTION_SEED_DELAY_VIEWS` (value set in CIP-11 §13). `commonware` produces exactly one valid seed per absolute round — whether the round notarizes, finalizes, or nullifies — so a withholding leader can delay the reveal but cannot re-roll `r_seed`.

**Parent-beacon notation.** For a block `S` proposed in round `r_submit` (the block's consensus round, `block.context.round`), the parent beacon `R_{S-1}` is the verified `Seed` of the absolute round immediately preceding `r_submit` in total round order — a seed that exists even if that preceding round nullified. The subscript is block-parentage shorthand: `R_{S-1}` is never a height-addressed seed lookup, and "the seed of height `S-1`" is not the same object.

The selection preimage uses the domain `cowboy-runner-select-v3:` with a consensus-critical mode byte (`0x00` = immediate `M == 1`, `0x01` = delayed `M > 1`):

```
seed = Keccak256(
    "cowboy-runner-select-v3:"   // domain separator (prevents cross-context collisions)
    || mode_u8                   // 0x00 immediate (M == 1) / 0x01 delayed (M > 1)
    || canonical_seed_bytes(R)   // := beacon_hash_v1(R), 32-byte Keccak-256 of the verified threshold Seed (CIP-11 §9.2)
    || job_id                    // unique per job
    || submitted_at_le8          // 8-byte LE submission block height
)
```

For `M == 1`, `R` is the parent beacon `R_{S-1}`; for `M > 1`, `R` is the threshold seed whose `Seed.round` equals the bound `r_seed`. The seed is a public, consensus-verified value — no dispatcher private key is involved, so any node can recompute the selection. The `cowboy-runner-select-v2:` domain belongs exclusively to the retired `block_hash` preimage and MUST NOT be used with this preimage. CIP-11 §9.2 is the normative definition of the selection algorithm (`canonical_seed_bytes(R) := beacon_hash_v1(R)` and its hash material, the `advance_round` round arithmetic — including the rule when the referenced future round is not yet mappable under the known epoch schedule — the `weighted_draw`/`partition` iteration semantics, and the `M > 1` pending-selection timing and storage); `SELECTION_SEED_DELAY_VIEWS` is set in CIP-11 §13; activation follows CIP-11 §13/§14. The normative CIP-11 sections are those of **CIP-11 r1.3**; the earlier v2 selection-seed function is retired.

> **Implementation note:** Use `cowboy_types::keccak256`, which is the project-standard hash primitive (consistent with job ID generation, timer ID generation, token ID generation, etc.). The `pvm_host::randomness()` API uses HKDF-SHA256 specifically because it is a security-critical PRF exposed to Actor code; the runner selection seed has no such requirement.

##### **5.3 Stake × √reputation weighted Fisher-Yates shuffle (select M from N)**

The weight function combines stake and reputation. Pure-stake weighting would amplify "rich-get-richer"; the square-root reputation term provides a graceful merit signal without enabling pure reputation-mining attacks.

```
w_i = effective_stake[i] · max(sqrt(reputation[i] / REPUTATION_NORMALIZER), w_min_floor)
```

Parameters:

| Parameter | Default | Mutability | Notes |
|---|---|---|---|
| `REPUTATION_NORMALIZER` | 100 | Tier-2 (reputation config, §6.1) | Treats reputation as 0..100 score; sqrt(1) = 1 means a runner at the normalizer value gets the same weight as pure stake |
| `w_min_floor` | 0.1 | Protocol constant | Cold-start protection — a brand-new runner (zero reputation) still gets `weight = 0.1 · stake` rather than zero |

Why `sqrt(r)` and not `r^1` or `log(r)`:

- `r^0` (stake-only) gives no reputation signal — vulnerable to pure stake-monopoly.
- `r^1` rewards established runners too aggressively — locks out new entrants the way the old aggregator rule did.
- `r^0.5` (= `sqrt(r)`) is the geometric-mean middle ground; a 4× reputation differential becomes a 2× selection-probability differential. Matches the heuristic Polkadot uses in NPoS phragmen and the staking-as-collateral literature.

**Fisher-Yates Shuffle:**

```
candidates ← sorted filtered list (length N)
weights[i] ← stake · sqrt(reputation / REPUTATION_NORMALIZER) per the formula above

current_seed ← seed
for i in 0..M:
    total_remaining ← sum(weights[i..N])
    hash ← Keccak256(current_seed || i_le8)
    r ← u64_from_le(hash[0..8]) mod total_remaining
    j ← weighted_pick(weights[i..N], r)   // cumulative weight lookup → O(N)
    swap(candidates[i], candidates[i+j])
    swap(weights[i], weights[i+j])
    current_seed ← hash

selected ← candidates[0..M].addresses
```

**Properties:**
- Selected runners are **pairwise independent**: no two selections are correlated
- Higher stake → higher probability of selection (Sybil resistance)
- Higher reputation → graceful merit signal without lockout
- Same `seed` + same `candidates` always produces the same result (determinism)
- Any node can independently verify the selection (verifiability)

**On-chain verification** (in Result Verifier and Job Dispatcher):  
Given `(candidates_snapshot, seed, M)`, re-run the above algorithm to verify that `msg.sender` is among the selected set. The `candidates_snapshot` Merkle root is stored at submission time.

#### **6. Reputation and Non-Reveal Handling**

This section covers two interrelated mechanisms: how reputation is updated over time, and how missing reveals are classified. The non-reveal classification (§6.2) defends against a denial-of-verification attack — an adversary committing `hash(garbage_result || sig)`, observing other commits, then refusing to reveal if consensus is unfavorable.

##### **6.1 EMA reputation with 14-day half-life**

```
on each timer fire / job settlement, for runner i:
    score_i_block = f(success, latency, slash_event)    in [0, 100]

    α = ln(2) / HALF_LIFE_BLOCKS                          // = ln(2) / 1_209_600  ≈ 5.73e-7
    reputation_i = reputation_i + α · (score_i_block − reputation_i)
    reputation_i = clip(reputation_i, REPUTATION_FLOOR, REPUTATION_CEILING)
```

Score function (Tier-2 tunable):

| Outcome | Score contribution |
|---|---|
| Successful settlement | 100 |
| Successful settlement as aggregator (bonus) | 110 (clipped at REPUTATION_CEILING) |
| Timeout (no commit) | 0 |
| Commit-without-reveal (non-reveal) | 0 (+ slash per §6.2 / §11) |
| Invalid reveal (proof mismatch) | 0 (+ slash per §11) |
| Jail (multiple consecutive failures) | reputation reset to `JAIL_EXIT_FLOOR` |

An **invalid reveal** (a revealed result that fails its mode's verification — including a `Deterministic` byte-mismatch or an INVALID canonical form per CIP-23 §3.5.2) is distinct from a **non-reveal** (§6.2 covers commit-without-reveal only). Both zero the round's reputation; the invalid-reveal **slash magnitude** is the §11 stake-slash figure (there is no separate invalid-reveal `bps` — it shares the §11 slash schedule, not the `non_reveal_slash_bps` of the non-reveal path).

Parameters (stored at `0x09` under `system:cip2:reputation_config` as a single JSON-serialized `ReputationConfig` blob; the `.*` notation names logical fields of that struct, **not** separate storage sub-keys — cf. `system:cip2:non_reveal_config`):

| Parameter | Default | Mutability |
|---|---|---|
| `HALF_LIFE_BLOCKS` | 1,209,600 (= 14 days at 1s blocks per WP §13) | Tier-2 |
| `REPUTATION_FLOOR` | 0 | Tier-2 |
| `REPUTATION_CEILING` | 200 | Tier-2 |
| `JAIL_EXIT_FLOOR` (`jail_exit_floor_x1e9`) | Static Tier-2 governance value (genesis default 50); constrained to `[reputation_floor, reputation_ceiling]`. (The dynamic `max(round(0.1 · network_median_reputation), 50)` formula is a design note, **not** implemented — there is no on-chain network-median computation.) | Tier-2 |
| `REPUTATION_NORMALIZER` | 100 (used by §5.3 VRF weight) | Tier-2 |

**Initial reputation.** Runners enter with `reputation = 50`. The EMA and its floor/ceiling rules apply to every subsequent update.

##### **6.2 Non-reveal classification**

When a committed runner does not reveal within the window, the verifier classifies the non-reveal into one of three paths: success (reveal arrived), operational failure (a `CrashAttestation` was filed within the exemption window), or proven dishonesty (default).

```
classify_non_reveal(runner, job, commit_block, reveal_window_blocks):
    if reveal_received(runner, job) within commit_block + reveal_window_blocks:
        → success path (no penalty)

    if CrashAttestation(runner, sig, height ∈ [commit_block, commit_block + reveal_window_blocks])
       was submitted before commit_block + reveal_window_blocks:
        → "operational failure" path: reputation penalty per §6.1 (-= 0 deferred decay) + 
           `CrashAttestation` event emitted; no stake slash; runner exempted from slash this round

    otherwise:
        → "proven dishonesty" path:
           - reputation reset to 0
           - stake slash per §11 (non-reveal slash magnitude)
           - `NonRevealSlash` event emitted
```

**Crash-attestation mechanism.** A runner that legitimately crashes between commit and reveal self-attests via the **`SubmitCrashAttestation` system instruction (opcode 97)** — there is no free-standing signed `CrashAttestation` struct on the wire. The instruction carries only:

```rust
SubmitCrashAttestation {
    job_id:       [u8; 32],
    crash_signal: CrashSignal,   // enum { OOM, NetworkPartition, HardwareFault, TEEAttestationLost, Other } (wire 0..=4)
}
```

The runner identity is `tx.from` (self-attestation; the tx signature authenticates it — no separate `self_signature` field), and the block is implicit. The chain persists a `CrashAttestationRecord { runner, job_id, crash_signal, submitted_at_block }` under the Result Verifier actor (`node/types/src/execution.rs`; handler `system_instruction.rs`). Admissible at or before `commit_block + crash_exemption_blocks` (`UpdateNonRevealConfig` param, opcode 98); requires an existing commitment and no prior reveal; idempotent. The attestation does not get the runner the job payment, but it converts the non-reveal from "proven dishonesty" (slash) to "operational failure" (reputation hit only).

**Why an exemption mechanism instead of pure slash:**

- Without exemption, legitimate hardware faults cause runner stake destruction → discourages decentralised runner participation.
- Without slash by default, a denial-of-verification attack costs only reputation.
- With exemption: runners must take an explicit action to claim "crash" status, leaving an on-chain trail. Repeated `CrashAttestation`s from the same runner trigger Tier-2 review (default: ≥ 5 attestations in 1,000-block window → automatic flag).

**Reveal window:** `reveal_window_blocks` defaults to `commit_deadline_blocks + 60` (i.e. 60 blocks after commit deadline; ~1 minute at 1s blocks). Tier-2 tunable.

| Parameter | Default | Mutability |
|---|---|---|
| `crash_exemption_blocks` | 50 (at 1s blocks ≈ 50 s) | Tier-2 |
| `reveal_window_blocks` | commit_deadline_blocks + 60 | Tier-2 |
| `crash_attestation_review_threshold` | 5 per 1,000 blocks | Tier-2 (off-chain governance-monitoring heuristic — **not** a field of the on-chain `NonRevealConfig` / `UpdateNonRevealConfig` opcode 98) |

##### **6.3 Timeout-based re-selection**

For the case where a selected runner never commits in the first place (distinct from the commit-without-reveal case handled above), timeout-based re-selection preserves liveness:

1. Selected runners have `timeout_blocks` to submit their result commitment.  
2. If no commitment is received within `timeout_blocks`:  
   - All timed-out runners receive a reputation penalty per §6.1 (timeout outcome → score 0)
   - Dispatcher triggers re-selection:  
     `retry_seed = Keccak256(original_seed || "retry:" || retry_count_le4)`  
   - Timed-out runners are excluded from the retry candidate list  
   - A new set of M runners is selected from the remaining candidates  
3. After `MAX_RETRIES` (default: 3) consecutive failures, the job transitions to `Failed` and the persistent non-responders receive an additional reputation hit
4. After `SLASH_THRESHOLD` consecutive slashes, a stake slash is triggered per §11

**Economic alignment:** Not executing = passive timeout = reputation loss via the §6.1 EMA. Runners are incentivized to either execute or proactively avoid accepting jobs they cannot execute (by not heartbeating for job types they lack). There is no `skip_task` interface; explicit skipping creates adverse incentives (runners cherry-picking jobs) and unnecessary on-chain transactions.

#### **7. Entitlement Registry (`0x0000…0007`)**

The Entitlement Registry is a system Actor that provides a unified, on-chain permission management layer. For this CIP, its primary role is **Runner Pool access control** (gating job assignments to a curated set of runners). It also provides general RBAC infrastructure consumed by other subsystems (Token admin delegation, Actor access control, etc.).

##### **7.1 Core Type System**

The Entitlement type system is defined in `cowboy_types::entitlement`:

```rust
/// Scope — defines the resource boundary a permission applies to
pub enum Scope {
    Global,                   // chain-wide
    Actor(Address),           // specific Actor
    Token([u8; 32]),          // specific Token (token_id)
    Runner(Address),          // specific Runner
    RunnerPool(Vec<u8>),      // Runner Pool membership (pool_id bytes)
    Namespace(Vec<u8>),       // custom named namespace
}

/// Action — defines the permitted operation (single action per Entitlement record)
pub enum Action {
    All,                                       // wildcard
    // Token actions
    TokenTransfer, TokenMint, TokenBurn,
    TokenFreeze, TokenSetHook, TokenTransferOwnership,
    // Actor actions
    ActorDeploy, ActorSendMessage,
    ActorExecuteHandler(Vec<u8>),              // handler name
    // Runner actions
    RunnerRegister { min_stake_override: Option<u128> },
    RunnerSubmitJob, RunnerSubmitResult,
    RunnerJoinPool(Vec<u8>),                   // pool_id — grants pool membership
    // System actions
    SystemTransfer, SystemCreateAccount, SystemUpgrade,
    // Custom
    Custom(Vec<u8>),
}

/// Constraints — use conditions attached to an Entitlement
pub struct Constraints {
    pub valid_from:          Option<u64>,   // block height activation
    pub valid_until:         Option<u64>,   // block height expiry
    pub max_uses:            u64,           // 0 = unlimited
    pub used_count:          u64,
    pub max_amount_per_use:  Option<u64>,
    pub max_total_amount:    Option<u64>,
    pub total_amount_used:   u64,
    pub rate_limit:          Option<RateLimit>,
    pub delegatable:         bool,
    pub delegation_depth_max: u8,           // max delegation chain depth (0 = non-delegatable)
    pub revocable:           bool,
}

/// A single Entitlement record (one scope + one action per record)
pub struct Entitlement {
    pub grantee:    Address,
    pub scope:      Scope,
    pub action:     Action,
    pub constraints: Constraints,
    pub parent_id:  Option<EntitlementId>,  // set when derived via delegation
}

pub type EntitlementId = [u8; 32];  // keccak256(grantee || scope || action || parent_id?)

/// Named permission set (RBAC role)
pub struct Role {
    pub name:        Vec<u8>,
    pub scopes:      Vec<Scope>,
    pub actions:     Vec<Action>,
    pub constraints: Constraints,
}
```

> **Design note:** Each `Entitlement` record covers **one** `(Scope, Action)` pair. Granting multiple actions requires multiple `Grant` calls (or an `AssignRole` that references a `Role` collecting them). This keeps revocation granular: revoking one action does not affect others.

##### **7.2 System Instructions (instruction numbers 30–39)**

```rust
pub enum SystemInstruction {
    // ... existing instructions 0–29 ...

    // Entitlement instructions (30–35). scope/action/constraints are the
    // TYPED codec structs (crate::{Scope, Action, Constraints}), carried
    // directly in the variant — NOT opaque Vec<u8> blobs
    // (cowboy-protocol-codec/src/instruction.rs::EntitlementGrant).
    EntitlementGrant {
        grantee:     Address,
        scope:       Scope,
        action:      Action,
        constraints: Constraints,
    },                                              // 30
    EntitlementRevoke {
        entitlement_id: [u8; 32],
    },                                              // 31
    EntitlementDelegate {
        entitlement_id: [u8; 32],
        delegatee:      Address,
        constraints:    Constraints,   // must be a strict subset of parent constraints
    },                                              // 32
    EntitlementCreateRole {
        name:          Vec<u8>,
        scopes:        Vec<Scope>,
        actions:       Vec<Action>,
        constraints:   Constraints,
    },                                              // 33
    EntitlementAssignRole {
        role_id:  [u8; 32],
        assignee: Address,
    },                                              // 34
    EntitlementRevokeRole {
        role_id:  [u8; 32],
        assignee: Address,
    },                                              // 35
}
```

**CIP-2 v3 parameter-governance opcodes.** Beyond the entitlement instructions above, the CIP-2 v3 mechanism parameters are governance-tunable via their own system opcodes (**94–96, 98–100**), each restricted to `GOVERNANCE_SYSTEM_ACTOR` (`0x09`); opcode **97** (`SubmitCrashAttestation`) sits in the same numeric block but is a runner self-attestation, not a governance update. This CIP MUST enumerate them (`cowboy-protocol-codec/src/instruction.rs`):

| Opcode | Instruction | Updates (CIP-2 v3 §) |
|---|---|---|
| 94 | `UpdateReputationConfig` | `{half_life_blocks, reputation_floor_x1e9, reputation_ceiling_x1e9, jail_exit_floor_x1e9, reputation_normalizer}` — §3 reputation |
| 95 | `UpdateAggregatorConfig` | `{eligibility_percentile, bonus_bps}` — §4 aggregator selection |
| 96 | `UpdateSlashDistribution` | `{burn_bps, submitter_bps, treasury_bps}` (sum 10000) — §6 slash routing |
| 97 | `SubmitCrashAttestation` | `{job_id, crash_signal}` — §5 non-reveal (**runner-submitted**, not governance) |
| 98 | `UpdateNonRevealConfig` | `{crash_exemption_blocks, reveal_window_blocks, non_reveal_slash_bps, max_non_reveal_slash_wei}` — §5 non-reveal policy |
| 99 | `UpdateCommitteeConfig` | `{m_min, m_max, hhi_min_x1e9, hhi_smoothing_alpha_x1e6, committee_recompute_period_blocks}` — §1 adaptive committee sizing |
| 100 | `UpdateSemanticSimilarityModel` | `{model_id: Vec<u8>}` (≤256 B, ASCII) — §7 canonical embedding model |

##### **7.3 Runner Pool Membership Model**

A **Runner Pool** is an on-chain access-control list identified by an arbitrary `pool_id: Vec<u8>`. Membership is represented as an Entitlement:

```
Scope::RunnerPool(pool_id) + Action::RunnerJoinPool(pool_id)
```

**Grant flow:**

```
Pool Owner / Governance
    │
    └── EntitlementGrant {
            grantee:  <runner_address>,
            scope:    Scope::RunnerPool(pool_id),
            action:   Action::RunnerJoinPool(pool_id),
            constraints: Constraints {
                valid_until: Some(expiry_block),  // optional time-bound
                max_uses: 0,                      // unlimited while valid
                delegatable: false,               // pool membership is non-delegatable
                revocable: true,
                ..Default::default()
            },
        }
```

**Candidate filter check** (step 7 of §4, at `submission_block`):

```
EntitlementRegistry.check(
    grantee = runner_address,
    scope   = Scope::RunnerPool(required_runner_pool),
    action  = Action::RunnerJoinPool(required_runner_pool),
    at_block = submission_block,
) → bool
```

The check passes if at least one non-expired, non-exhausted Entitlement matching `(grantee, scope, action)` exists. `used_count` is **not** incremented by the membership check — only by explicit `max_uses`-bounded grants (e.g. one-time trial membership).

**Access modes enabled:**

| Mode | `required_runner_pool` | Effect |
|------|----------------------|--------|
| **Open** (default) | `None` | Any qualified runner may be selected |
| **Whitelist** | `Some(trusted_pool_id)` | Only runners with pool Entitlement |
| **Compliance-gated** | `Some(eu_tee_pool_id)` | Only runners with regional or TEE-certified Entitlement |
| **Governance-curated** | `Some(dao_pool_id)` | DAO grants pool membership via on-chain governance |

##### **7.4 Gas Costs**

| Operation | Cycles | Cells |
|-----------|--------|-------|
| `EntitlementGrant` | 5,000 | 500 |
| `EntitlementRevoke` | 2,000 | 100 |
| `EntitlementDelegate` | 3,000 | 300 |
| `EntitlementCreateRole` | 5,000 | 500 |
| `EntitlementAssignRole` / `RevokeRole` | 2,000 | 100 |
| Entitlement check (per candidate, §4 filter) | 500 | 0 |

##### **7.5 Safety Limits**

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max Entitlements per address | 256 | Prevent storage inflation |
| Max delegation chain depth | 5 (`delegation_depth_max`) | Prevent unbounded chains |
| Max Roles per address | 64 | Bound role-expansion attacks |
| Lazy expiry cleanup | On-access | Expired Entitlements deleted on first check post-expiry |

##### **7.6 Integration Requirements**

1. **Missing registry:** If the Entitlement Registry Actor does not exist, all checks return `false` (no runner is admitted via pool filter). Jobs with `required_runner_pool = None` are unaffected.
2. **Existing subsystems:** Token `owner == caller` checks remain the first-priority gate; Entitlement provides an additional delegation path, not a replacement.  
3. **Instruction numbering isolation:** Instructions 30–39 do not conflict with existing instructions 0–29.

#### **8. Result Submission: Commit-Reveal + Designated Aggregator**

<Note>
  The commit-reveal + aggregator model reduces on-chain Gas by ~(N-1)/N compared to having all runners independently submit full results to chain, and prevents runners from copying each other's results after seeing them.
</Note>

**Participants:**

- **Runners:** All M selected runners execute the job independently.
- **Aggregator:** Selected from the committee per §8.1. Acts as the coordinator.

**Submission flow:**

```
Step 1 – Commit (all M runners, on-chain, low cost):
    commit(runner_addr, job_id, hash(result_bytes || runner_sig))
    Deadline: submitted_at + commit_deadline_blocks

Step 2 – Collect (Aggregator, off-chain):
    Aggregator receives result_bytes from other runners via direct HTTP push
    Aggregator runs verification logic (majority vote, structured match, etc.)

Step 3 – Reveal (Aggregator, on-chain):
    submit_verified_result(job_id, VerifiedResult, reveals[])
    reveals[i] = { runner, result_bytes, runner_sig }
    Aggregator receives a bonus reward for honest aggregation (§8.2)

Step 4 – On-chain verification (Result Verifier):
    For each reveal[i]:
        assert hash(result_bytes || runner_sig) == stored_commitment[i]
        assert runner_sig is valid Ed25519 over result_bytes
    Run verification mode checks (MajorityVote, StructuredMatch, etc.)
    Produce VerifiedResult → trigger deferred callback (CIP-1)
```

**Safety properties:**

- Runners cannot copy others' results after seeing them (commitment pre-locks the result).
- Aggregator cannot forge other runners' results (commitment-bound).
- Aggregator failure: other runners may submit individual reveals after the per-job reveal deadline (`JobSpec.timeout_blocks`, CIP-11); Result Verifier falls back to self-aggregation. *(Note: there is no distinct `aggregator_timeout_blocks` constant in code — `AggregatorConfig` carries only `{eligibility_percentile, bonus_bps}`; the fallback is gated by the per-job `timeout_blocks`, not an aggregator-level timeout.)*
- Non-reveal handling: see §6.2 for the `CrashAttestation`-gated classification.

##### **8.1 Aggregator selection — eligibility threshold + uniform random**

A "highest reputation in committee" rule would give new runners zero probability of aggregating and prevent them from bootstrapping reputation via successful aggregation. Instead, the aggregator is selected as follows:

```
ineligible = runners_to_slash ∪ runners_unrewarded   // slashed, or submission did not verify
submitted  = { runner in committee : a result for this job is stored under runner }
pool       = { runner in submitted : runner ∉ ineligible }

if pool == ∅:
    return NONE              // STOP. No aggregator is elected; aggregator_bonus = 0 (see below)

// Sort ASCENDING by (reputation, address bytes). The address tie-break is
// normative: two runners at equal reputation must order identically on every
// node, and the sort must be stable.
sorted = sort_asc(pool, key = (reputation[runner], address_bytes(runner)))
n      = |sorted|

// INTEGER lower-rank percentile on n-1 — NOT interpolation, NOT nearest-rank.
// pct is clamped to 100. All arithmetic is integer; the multiplication happens
// before the division.
pct       = min(aggregator_eligibility_percentile, 100)
   where aggregator_eligibility_percentile = 50 (Tier-0 tunable; stored as the
   `eligibility_percentile` field of the `AggregatorConfig` blob under
   `system:cip2:aggregator_config`, NOT as a per-field key)
idx       = (pct * (n - 1)) / 100
threshold = reputation[sorted[idx]]

eligible = [ runner in sorted : reputation[runner] >= threshold ]   // keeps sorted order

if eligible == ∅:
    eligible = sorted        // defensive only: sorted[idx] is always in the set

// The pick indexes the SORTED sequence, and the index comes from the FIRST 8
// SEED BYTES read LITTLE-ENDIAN. Both are normative.
seed     = Keccak256(job_id || "agg-select-v3")
return eligible[ u64_from_le_bytes(seed[0..8]) mod |eligible| ]
```

**A runner that committed but never revealed is excluded by ABSENCE, not by `ineligible`.** The election runs over the runners with a stored result, and non-reveal classification happens *after* it, so at election time a non-revealer is in neither `runners_to_slash` nor `runners_unrewarded`. Reading `pool` as the drawn committee therefore admits a runner that the implementation never considers — and one that is not payable either, since payment is computed over the same submitted set. That is exactly the elected-but-unpayable aggregator this filter exists to prevent, so `submitted` is normative: with committee `{A, B, C}` where C never reveals, the pool is `{A, B}` and not `{A, B, C}`, which changes the percentile threshold, the eligible set and therefore the elected aggregator.

**The `ineligible` filter is normative, and it is conservation rather than deterrence.** Election carries `aggregator_bonus`; an elected-but-unpayable aggregator strands that bonus outright — neither paid, burned, nor sent to treasury. The percentile alone does not prevent it: a griefer already driven down on reputation is excluded anyway, but a fresh one at the registration default sits above the median whenever the committee is uniformly fresh. The exclusion also covers `runners_unrewarded`, not only `runners_to_slash`, for the reason CIP-11 §9.4 gives: election is a reward, so the question is *did this submission do the work*, not *was this runner provably dishonest* — a state-dependent fault is unattributable without punishing an honest loser of a build-vs-verify race, and unattributable is not the same as unearned.

Note the fallback subtracts `ineligible` **before** the percentile, and never falls back to the full committee: an ineligible runner cannot be re-admitted by an empty eligible set. When the whole committee is ineligible the result is no aggregator at all, and the distribution falls back to pre-aggregator semantics with `aggregator_bonus = 0`.

**Why the sort, the estimator and the byte order are spelled out.** Aggregator identity carries `aggregator_bonus` and therefore payment distribution, so two implementations that disagree here fork. "Reputation ≥ the 50th percentile" admits several standard readings that select different pools — over `[1,2,3,4,5,6]`, the integer lower-rank rule above yields threshold `3` and a 4-member pool, while linear interpolation yields `3.5` and a 3-member pool, so `seed mod 4` and `seed mod 3` pick different runners from identical inputs. The same applies to reading the seed big-endian, or to indexing the committee order instead of the sorted sequence. `select_aggregator` in `execution/src/runner/aggregator.rs` is the reference — as of `node` `devnet` @ `c7339ba5`, named so this block is checkable against a specific tree rather than against whichever branch a reader lands on; this block now states what it does rather than leaving it to be inferred.

**Why eligibility + uniform-random and not pure highest-reputation:**

- "Highest reputation" gives new runners zero probability of aggregating — they cannot bootstrap their reputation by aggregating successfully.
- Eligibility threshold (p50) ensures aggregators are competent without locking new runners out permanently — once a new runner crosses p50, they enter the eligibility pool.
- Uniform random within the eligible set prevents any deterministic-tie-breaker from concentrating aggregator share at a single high-reputation runner.

##### **8.2 Aggregator bonus — 1.5% of gross**

```
aggregator_bonus = gross_job_payment · aggregator_bonus_bps / 10000     // default bps = 150 = 1.5%
```

Paid from the **runner share** (89% of gross under WP §8.4) — i.e. the bonus does not change the burn / treasury split. Conceptually: 89% runner share splits into `(89% − 1.5%) = 87.5%` divided across non-aggregator runners pro-rata to commit-reveal weight, plus `1.5%` aggregator bonus.

| Parameter | Default | Mutability |
|---|---|---|
| `aggregator_eligibility_percentile` | 50 (p50 reputation) | Tier-0 |
| `aggregator_bonus_bps` | 150 (1.5% of gross) | Tier-0 |
| `aggregator_selection_seed_domain` | `"agg-select-v3"` | fixed (consensus-relevant domain separator) |

**Aggregator bonus paid only on successful settlement** (verification mode check passes, no slash event). Failed aggregations cost the aggregator the bonus; the §6.2 non-reveal classification handles outright dishonesty.

#### **9. Verification Modes**

| Mode | Runners | Description |
|------|---------|-------------|
| **None** | 1 | No verification. Dev/test only. |
| **EconomicBond** | 1 | Single runner with economic stake bond. |
| **MajorityVote** | N ≥ 3 | Extract `vote_field`, majority wins with threshold. |
| **StructuredMatch** | N ≥ 2 | Pipeline of checks: JsonSchema, field matching, numeric tolerance, numeric range, per-field majority (§9.1). |
| **Deterministic** | N ≥ 2 | Requires `tee_required = true` for meaningful guarantees: LLM inference also requires a fixed model hash and deterministic execution. TEE attestation is verified by the TEE Verifier (`0x05`). Equality uses `canonical_result_cbor(result.data)` (CIP-11 §12.6 / CIP-23 §3.5.1a–§3.5.2), with the first valid result selecting the reference cohort. Consensus-reproducible outputs MUST use scaled-integer `Nano` under the whitepaper no-floats rule; a non-integral `binary64`, NaN, infinity, or over-cap value is INVALID and has no equality key. If the reference cohort reaches `threshold`, invalid and mismatching reveals receive the §6.1 invalid-reveal slash. If it is below `threshold`, the job fails with no slash; all-invalid results also fail with no slash. These are required canonicalization and disposition rules; CIP-23 records the remaining producer/verifier implementation gaps. |
| **SemanticSimilarity** | N ≥ 3 | Cosine similarity clustering; largest cluster meeting threshold wins. Embedding model pinned per §9.2. |

The two single-runner rows (`None`, `EconomicBond`) are dispatched with `M = 1` via the §5.2 immediate mode; adaptive committee sizing applies only to the quorum rows (§5.1, *Scope of adaptive sizing*).

**`EconomicBond` scope.** `EconomicBond` settlement accepts the single revealed result, with the runner's slashable stake bond validated at dispatch; the settlement arm enforces exactly one result. The mode's assurance is that slashable bond plus the §6 reputation/timeout machinery. Objective-failure classification (schema violation, timeout, empty/garbage output, non-delivery, wrong model, prompt-injection leak) and objective-failure challenge-window semantics are out of scope for this CIP; WP §9.2 defines them as the target semantics for the mode (1 runner, use case "subjective generation").

##### **9.1 VerifierCheck variants**

`VerifierCheck` is the per-check enum used inside `verification.checks: Vec<VerifierCheck>`. The supported variants:

```rust
pub enum VerifierCheck {
    MajorityVote     { field: String },
    JsonSchemaValid  { schema: String },
    StructuredMatch  { fields: Vec<String> },
    NumericTolerance { field: String, tolerance: f64 },
    NumericRange     { field: String, min: f64, max: f64 },
    Custom           { actor_hex: String, method: String },
}
```

The `f64` fields above are the node's in-memory surface only. On the canonical
wire — and therefore in every hash preimage — `tolerance`/`min`/`max` are
scaled-integer `Nano` values per CIP-11 §12.6 (`cowboy-protocol-codec` maps
`f64 ↔ Nano` at the boundary); canonical encodings never contain floats, per
the whitepaper's no-floats rule.

The codec retains `DnsTxtRecordMatch` and `DnsCnameMatch` representations, but node admission rejects both before dispatch (§3.1). They are not supported verifier checks.

##### **9.2 SemanticSimilarity embedding pinning**

`SemanticSimilarity` mode (N ≥ 3; cosine similarity clustering; largest cluster wins) requires a pinned embedding model so that a runner cannot collude with the embedding choice. The default:

```
system:cip2:semantic_similarity_embedding_model = "sentence-transformers/all-mpnet-base-v2"
                                                  (default; resolved via model registry)
```

The value is a model identifier resolvable via the existing model registry (`model_id` field in §1). Changes follow the **consensus-criticality carve-out**:

- **Default routing.** Per CIP-12 §5.1, "model flags/bans" are Tier 1 (15% quorum / >55% approval). The general model-registry path remains Tier 1.
- **Carve-out.** The specific entry `system:cip2:semantic_similarity_embedding_model` is flagged Tier-3 (15% quorum / >60% approval) because changes alter consensus-verification semantics — a runner colluding with the embedding-model choice could swing similarity clustering. The carve-out is documented here (CIP-2 §9.2) rather than in CIP-12; the registry path remains Tier 1 for non-consensus-critical entries.

**Embedding-model requirement.** SemanticSimilarity verification MUST use the canonical embedding model defined above; an undefined or unresolvable model is not a valid settlement input.

#### **10. `JobType::Custom` Extension Pattern**

CIP-2's existing `JobType::Custom { executor_hash: [u8; 32], params: Vec<u8> }` (`runner/src/types.rs:146-149`) is the established mechanism for adding new verifier-bearing job types without expanding the `JobType` discriminant set.

Pattern for a new built-in verifier:

1. Build the verifier as a deterministic executor binary (Rust → reproducible build).
2. Hash the binary with BLAKE3.
3. Pin the hash via governance: write a record at `GOVERNANCE_SYSTEM_ACTOR=0x09` keyed `system:executor_registry:<name>`.
4. Issue jobs as `JobType::Custom { executor_hash: PINNED_HASH, params: <serialized job-specific params> }`.
5. Runners that have whitelisted the executor execute the job. Verification proceeds via the standard `VerifierCheck` chain using the supported checks in §9.1.

This avoids fragmenting `JobType` for every new built-in verifier and keeps the discriminant space stable.

##### **10.1 Governance pinning vs. open executors**

`JobType::Custom` accepts any `executor_hash`, including user-supplied ones — the protocol does not require governance pinning. Governance pinning is the convention for **protocol-level** executors used by system actors; user-deployed verification jobs (e.g., a DAO running custom market-data validation) can use any hash they trust.

The distinction is purely operational: governance-pinned executors can be referenced by system actors because the hash is itself part of the chain's normative state. User-supplied hashes are the user's own trust assumption.

#### **11. Slash Distribution Schema**

```
SlashDistribution {
    burn_bps:      u16,      // 0..10000; default 10000 (100% burn) — matches WP §8.4 C7
    submitter_bps: u16,      // 0..10000; default 0           — challenger/submitter share, inactive at launch
    treasury_bps:  u16,      // 0..10000; default 0           — treasury share, inactive at launch
    // invariant: burn_bps + submitter_bps + treasury_bps == 10000
}
```

Stored at `0x09` under `system:cip2:slash_distribution.{burn_bps, submitter_bps, treasury_bps}`. Genesis values `(10000, 0, 0)` — consistent with WP §8.4 commitment C7 ("Slashed stake | 100% | Burned"). Submitter and treasury shares are **inactive** at launch; the schema exists so that a future amendment is a flag flip rather than a CIP rewrite.

**Mutability.** Any non-trivial change (any `bps` shifted away from the `(10000, 0, 0)` default) requires a **Tier-3 governance proposal** that also amends WP §8.4 C7 in lockstep — the schema-level flexibility does **not** unilaterally override the C7 commitment. CIP-12 §5.1 Tier-3 row implicitly covers this because changing slash distribution is a substantive economic-mechanism change, not a scalar tweak.

**Why a Tier-0 schema with Tier-3 mutability:** the field structure lives in code from the outset (no migration when the C7 amendment lands); only governance authority changes per-amendment. This avoids the "schema added later" trap that creates two competing code paths.

**Non-reveal slash magnitude (used in §6.2 path 3):**

```
non_reveal_slash_amount = min(
    runner_effective_stake · non_reveal_slash_bps / 10000,
    max_non_reveal_slash_wei    // 100,000 CBY expressed in wei (× 10^9)
)
```

| Parameter | Default | Mutability |
|---|---|---|
| `non_reveal_slash_bps` | 2500 (25% of effective stake) | Tier-2 |
| `max_non_reveal_slash_wei` | 100,000 CBY (= 100,000 × 10^9 wei) | Tier-2 |

The 25% fractional slash is the architecture-review recommendation; the absolute cap prevents catastrophic overshoot on extremely large stake positions.

#### **12. Stake Floor (HOLD on CBY-denominated)**

The runner stake floor is preserved as `max(10,000 CBY, 1.5 × declared_max_job_value)` (CBY-denominated, not USD-pegged). Per the architecture-review Decision Register item #4, the analysis default is **CBY-denominated stake floor with documented Tier-0 monitoring cadence** rather than USD-pegged via TWAP oracle.

- **Monitoring cadence.** The Cowboy Foundation publishes monthly the implied USD value of the 10,000-CBY floor and the median runner's effective stake. If the implied USD floor drifts outside the target band `[$1,000, $10,000]` for two consecutive monthly reviews, a **Tier-0 governance proposal** MUST be filed to adjust `MIN_RUNNER_STAKE_CBY`.
- **No oracle dependency.** This CIP does NOT introduce a consensus-layer CBY/USD oracle. Re-pegging is a future option gated on a forthcoming oracle CIP.
- **Failure mode.** If CBY appreciates 10×+ and the 10,000-CBY floor becomes punitive for small runners, the monitoring cadence above triggers a Tier-0 adjustment. The dispatcher continues to admit runners meeting the formula until that proposal lands.

Decision Register item #4 status: **HOLD on CBY-denominated**. Re-evaluation triggers: (a) availability of a battle-tested oracle module (e.g., a CIP-31-style oracle parameter spec), or (b) sustained USD floor drift outside the target band beyond what Tier-0 cadence can address.

#### **13. Randomness Source**

The selection seed IKM is the `commonware` threshold-BLS beacon: a verified `Seed { round: Round(epoch, view), signature }` for a named absolute round, with the timing and domain rules of §5 and the security assumption that t-of-n validators are honest. There is no selection-seed stage in which the IKM is a block hash, an execution hash, a proposer timestamp, or any proposer-local value (e.g. a proposer EC-VRF output): a proposer-generated seed is grindable and can steer committee membership.

The `pvm_host.rs` randomness API is independent of this path and unchanged: Actors keep `HKDF(beacon, label)` with no Actor-code change.

#### **14. Governance Parameters**

The mechanisms in §5–§11 are in effect for all jobs; the values in those sections are the launch defaults. Each parameter group is mutable through its own governance proposal instruction, at the tier its defining section assigns. Proposal payloads are **atomic per group** — a proposal replaces the group's full config, not a single field:

| Config group (atomic proposal payload) | Payload fields | Tier | Section |
|---|---|---|---|
| Committee sizing (`UpdateCommitteeConfig`) | `m_min`, `m_max`, `hhi_min_x1e9`, `hhi_smoothing_alpha_x1e6`, `committee_recompute_period_blocks` | Tier-0 | §5.1 |
| Aggregator (`UpdateAggregatorConfig`) | `eligibility_percentile`, `bonus_bps` | Tier-0 | §8 |
| Reputation (`UpdateReputationConfig`) | `half_life_blocks`, `reputation_floor_x1e9`, `reputation_ceiling_x1e9`, `jail_exit_floor_x1e9`, `reputation_normalizer` | Tier-2 | §6.1 |
| Non-reveal (`UpdateNonRevealConfig`) | `crash_exemption_blocks`, `reveal_window_blocks`, `non_reveal_slash_bps`, `max_non_reveal_slash_wei` | Tier-2 | §6.2 |
| Slash distribution (`UpdateSlashDistribution`) | `burn_bps`, `submitter_bps`, `treasury_bps` (sum = 10,000) | Tier-3 (amends WP §8.4 C7 in lockstep) | §11 |
| SemanticSimilarity embedding model (`UpdateSemanticSimilarityModel`) | the pinned model identifier (`system:cip2:semantic_similarity_embedding_model`) | Tier-3 (consensus-verification semantics) | §9.2 |

The VRF draw-weight floor (`w_min_floor`, §5.3) is a protocol constant, not a governance parameter — no proposal payload carries it.

Jobs in flight when a parameter changes retain their committee, aggregator, and verification mode for completion; new jobs use the updated values.

#### **15. Open Questions Deferred to Phase-5 Simulation**

- **HHI-based committee shock.** If a single large runner exits/enters and HHI shifts 0.10 → 0.50 in one epoch, M halves. The smoothing factor `α = 0.125` damps this, but extreme cases may still cause perceptible per-job cost swings. Phase-5: simulate with realistic runner-set dynamics.
- **Reputation half-life calibration.** 14 days is the launch default; longer is more stable but slower to recover from rare-event mistakes; shorter is more reactive but lets recent bad runners re-enter selection too fast.
- **`crash_exemption_blocks = 50` default.** Long enough for hardware reboot signal propagation, short enough to deter "stall, observe, decide" tactical non-reveals. Phase-5: model attacker dwell time against block-time-to-attestation latency.
- **VRF weight `sqrt(reputation)` calibration.** Whether `r^0.5` or `r^0.4` better matches empirical merit signals — sensitivity to the exponent is high in the upper tail.
- **SlashDistribution `(10000, 0, 0)` HOLD vs `(5000, 2000, 3000)` AMEND** — gated on Decision Register #1 (WP §8.4 C7 amendment).

---

### **Rationale**

* **Fisher-Yates VRF over ring-buffer:** A ring-buffer selection (selecting N consecutive indices) would be vulnerable to correlation attacks — an adversary controlling adjacent positions in the active list is always selected together for any N-runner job. Fisher-Yates with independent weighted draws eliminates this correlation entirely.

* **Stake × √reputation weighting:** Equal-probability selection ignores stake, making Sybil attacks cheap. Pure-stake weighting amplifies "rich-get-richer". `sqrt(reputation)` provides a graceful merit signal — a 4× reputation differential becomes a 2× selection-probability differential — without enabling reputation-mining attacks or locking out new entrants.

* **Adaptive committee sizing:** A static `M=5` committee is wrong at both extremes — too costly when the network is large and decentralised, too thin when stake is concentrated. The HHI-driven adaptive rule scales M with measured concentration; the `M_min`/`M_max` clip bounds the worst case.

* **EMA reputation with 14-day half-life:** Earlier revisions referenced reputation as a filter, penalty target, and aggregator selector but never defined update mechanics. The EMA model gives a smooth decay path that recovers from rare bad events without letting a recent bad runner re-enter selection too quickly.

* **Eligibility + uniform-random aggregator:** Pure "highest reputation" lock-in gives new runners zero probability of aggregating — they cannot bootstrap reputation. Eligibility threshold + uniform random preserves quality (only ≥ p50 runners aggregate) while allowing entry.

* **Non-reveal classification with `CrashAttestation`:** Pure reputation penalty for missed reveals is a denial-of-verification attack vector — commit garbage, observe others' commits, refuse to reveal if unfavorable. Default-slash with a `CrashAttestation` exemption path closes the attack without destroying legitimate operators' stake on hardware faults.

* **No private key for VRF seed:** Deriving the seed from the consensus threshold beacon (`Keccak256(domain || mode || canonical_seed_bytes(R) || job_id || submitted_at)`) instead of a dispatcher private key eliminates the hardcoded-key security hole. The threshold seed is consensus-verified, unique per absolute round, and globally consistent — unlike a block hash, no single proposer can grind the randomness source itself. For `M > 1` the commit-then-reveal path closes proposer selection bias; `M == 1` retains the bounded inclusion-timing residual described in Security Considerations.

* **Timeout re-selection over `skip_task`:** `skip_task` creates adverse incentives (runners selectively skip low-value jobs) and adds unnecessary on-chain transactions. Passive timeout with reputation penalty provides the same liveness guarantee with better economic alignment.

* **Commit-reveal aggregator:** All-runners-submit-to-chain would cost Gas × N and would allow result copying. Commit-reveal prevents copying; designating the aggregator off-chain is Gas-efficient. The eligibility-threshold selection (§8.1) is preferred over a highest-reputation tie-breaker because it allows new runners to bootstrap their reputation.

* **Deferred Transactions (CIP-1):** Callbacks are delivered as deferred transactions, decoupling off-chain execution latency from the main chain execution path.

* **Mandatory Result Schemas:** Creates a clear contract between developers and Runners, prevents gas-bombing, and simplifies result verification.

* **`JobType::Custom` as the extension pattern:** Adding a new `JobType` discriminant for every new built-in verifier fragments the type space. The existing `JobType::Custom { executor_hash, params }` covers all cases; governance pinning of the hash makes a verifier "built-in" without protocol changes.

* **Pinned SemanticSimilarity embedding:** Without pinning, a runner could collude with the embedding-model choice. The Tier-3 carve-out from the Tier-1 model registry path reflects that this specific entry is consensus-critical.

* **Schema-level slash flexibility, C7-preserving defaults:** `SlashDistribution { burn_bps, submitter_bps, treasury_bps }` is in code from the outset, but defaults `(10000, 0, 0)` preserve WP §8.4 C7. A future challenger-bounty amendment is a flag flip, not a CIP rewrite.

* **Entitlement as membership proof (not just a flag):** Pool membership is represented as an on-chain Entitlement record (with expiry, rate-limit, delegatability) rather than a simple boolean flag. This allows time-bounded trial access, revocable membership, and delegation to sub-pools without protocol changes.

* **Single action per Entitlement record:** Each record carries one `(Scope, Action)` pair. This enables fine-grained revocation without affecting co-granted actions, and keeps the membership check path O(1) per action.

---

### **System Actors and Runner Integration**

CIP-2 directly claims `0x01` (Runner Registry), `0x02` (Job Dispatcher), `0x03` (Result Verifier), `0x04` (Secrets Manager), `0x05` (TEE Verifier), and `0x07` (Entitlement Registry). The canonical allocation table is in whitepaper §9.1; `node/runner/src/system_actors.rs` defines the implementation addresses.

Timeout re-selection (§6.3) provides job liveness when a runner does not execute its assignment. `JobType::Custom` provides the extension mechanism in §10. Runners SHOULD handle `CrashAttestation` events from the Result Verifier; a crash without a valid attestation receives the ordinary slash disposition.

---

### **Security Considerations**

* **VRF Grinding:** Both grinding surfaces must be considered: a **submitter** choosing job bytes / `submitted_at` to influence the seed, and a **proposer/leader** influencing the randomness source itself (a block hash is proposer-assembled, and a height-anchored beacon can drift across rounds under a withholding leader — which is why neither is an acceptable seed source). The absolute-round threshold seed (§5; CIP-11 §9.2, §15.9) closes both paths for `M > 1` committees: the job binds before the seed for the named future round exists, and that round's seed is unique — a withholding leader can delay it but cannot re-roll it. For `M == 1`, two residuals remain: the **submitter** may know the parent beacon before submitting, and the **proposer** of the including block — who sees the pending `JobSubmit` and already knows the parent beacon — can include, omit, or defer the transaction to sample a different parent beacon (inclusion-timing selection). Immediate mode therefore does not provide proposer-inclusion neutrality. Both residuals are MEV/fairness-bounded (each attempt is an ordinary paid job, deferral is bounded by normal mempool/liveness/economic costs, and the selected runner stays economically accountable), not a committee-independence break — submitters that need selection neutrality SHOULD request a quorum verification mode (which dispatches `M > 1` whenever `N_active` permits; §5.1, *Scope of adaptive sizing*).

* **Stake Concentration:** The `stake · sqrt(reputation)` weight (§5.3) gives a graceful reputation signal without enabling reputation-mining or pure stake monopoly. The adaptive committee rule (§5.1) further mitigates concentration by scaling M with the measured HHI of effective stake.

* **Aggregator Collusion:** The Aggregator sees all results before submitting to chain. Mitigations: (1) other runners' commitments are locked before Aggregator submits; (2) other runners can independently reveal if Aggregator is unresponsive; (3) Aggregator's extra reward is forfeit if the submitted VerifiedResult is later challenged; (4) the eligibility-threshold + uniform-random selection (§8.1) prevents permanent aggregator capture by a single high-reputation runner.

* **Active List Manipulation:** Minimum stake (`MIN_STAKE = 10,000 CBY`) and reputation threshold (`min_reputation = 50`) provide economic deterrence. The Entitlement pool mechanism (§7) allows job submitters to further restrict runner eligibility.

* **Callback Griefing:** A malicious developer could write a callback that always fails, preventing runners from being paid. The `failed_callbacks` counter accrues as a reputational penalty, and runners may blacklist actors with high failure rates.

* **Historical Snapshot Integrity:** The candidate list is fixed at `submission_block`. The Merkle root of the candidate list must be stored on-chain at submission time to enable re-selection verification. Runners who join or leave post-submission do not affect the snapshot.

* **Slash distribution governance:** The default `SlashDistribution = (10000, 0, 0)` preserves WP §8.4 commitment C7. Any non-trivial change requires Tier-3 governance that also amends C7 in lockstep — the schema-level flexibility does not unilaterally override the C7 commitment.

* **Stake-floor drift:** The CBY-denominated floor may drift relative to USD purchasing power. The §12 monitoring cadence and the lack of consensus-layer oracle dependency are the v3 trade-off; re-pegging is gated on a forthcoming oracle CIP.

* **Entitlement Inflation:** Each address is limited to 256 active Entitlements and 64 assigned Roles. `EntitlementGrant` consumes 500 Cells to deter spam. Expired Entitlements are lazily cleaned on first post-expiry check.

* **Delegation Chain Depth:** `delegation_depth_max` (max 5) and the requirement that delegated constraints be a strict subset of parent constraints prevent privilege escalation through chained delegation.

* **Pool Membership Expiry:** Pool Entitlements with `valid_until` set are automatically ineligible after the specified block. Node operators running compliance-sensitive pools should set time-bounded grants and renew via governance.
