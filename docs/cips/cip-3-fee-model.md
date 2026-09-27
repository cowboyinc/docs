---
title: "CIP-3: Dual-Metered Fee Model"
description: Authoritative specification for Cycles, Cells, and fee markets
icon: gauge-high
---

<Note>
  **Status:** Draft for Internal Review  
  **Type:** Standards Track  
  **Category:** Core  
  **Created:** 2025-10-03<br />
  **Relates-to:** CIP-43 (access meter and third fee-market track), CIP-40 (transaction verification — §7.3's mode branch runs after the §2.5 preflight), CIP-41 (multi-signer authorization — consumes §2.5)
</Note>

<Tip>
  This specification, as amended by CIP-43, defines Cowboy’s tri-metered gas (Cycles/Accesses/Cells) and independent EIP‑1559 fee markets.
</Tip>

[CIP-43](./cip-43-access-meter) specifies the consensus `accesses` meter for store I/O, moves flat Storage KV Read/Write work out of Cycles and read bandwidth out of Cells, adds the third EIP-1559 fee track, and defines `gas-vectors.v2`. CIP-43 owns the Access weight table, capacities, and conformance requirements.

### **Cowboy Improvement Proposal (CIP-3): Dual-Metered Gas Calculation and Dynamic Fee Market**

**Status:** Draft for internal review

**Type:** Standards Track

**Category:** Core

### **Abstract**

Existing programmable blockchains face two major challenges in resource pricing: **inequitable fee models** and **non-deterministic execution metering**. A single gas unit cannot distinguish between computation and storage costs, leading to inefficient resource pricing and unpredictable fees. Furthermore, to securely support high-level languages like Python on-chain, the core problem of "how to deterministically meter its execution" must be solved; otherwise, the network cannot achieve consensus.

This CIP directly confronts these challenges by providing a definitive solution for Cowboy's tri-metered gas system. As amended by CIP-43, it divides priced resource consumption into three dimensions:

* **Cycles:** By instrumenting the virtual machine at the bytecode level, every **computational** step is precisely metered.  
* **Accesses:** By charging at a sealed store gate before dispatch, every cold **store I/O** operation is deterministically metered (CIP-43 §§3–5).
* **Cells:** By accounting at persistence and payload boundaries, every byte of **persisted data** and transaction data is precisely metered.

This proposal provides an authoritative, implementable engineering specification for these three mechanisms and their corresponding dynamic fee markets, thereby establishing a fair, predictable, and deterministic economic foundation for the Cowboy network.

### **1. Motivation**

The Cowboy Whitepaper introduces a novel dual-fee model to create more predictable costs and fairly price distinct resources: computation and data. However, for a decentralized network of clients to maintain consensus, the *exact* method of calculating this usage must be rigorously defined.

This CIP is necessary to:

* **Ensure Determinism:** Provide a canonical reference for how Python VM operations translate into Cycles and Cells, preventing consensus failures due to differing client implementations.  
* **Create a Fair Resource Market:** Formally separate the costs of on-chain state transitions from the costs of off-chain, real-world computation, allowing a more efficient market to form for each.  
* **Prevent DoS Attacks:** Standardize the resource limits (cycles_limit, cells_limit) and their enforcement within the VM, providing a robust defense against computational and state-bloat attacks.  
* **Clarify Runner Economics:** Define how Runners, operating in diverse environments like containers and TEEs, should conceptualize their costs, fostering a healthy and competitive off-chain marketplace.

### **2. Specification**

#### **2.1 Core Definitions**

* **Cycle (unit: c)**: A unit of abstract computational work. Every Python bytecode operation and every host function call has a fixed Cycle cost. This is analogous to an instruction step count.  
* **Access (unit: a)**: One cold `StateGet`; other store I/O is weighted relative to it and charged before dispatch under CIP-43 §§3–5.
* **Cell (unit: b)**: A unit of persistent data or payload work, where **1 Cell is equivalent to 1 byte**. Cells are consumed by transaction payloads, return data, and persisted state; CIP-43 §6 makes transient read bandwidth and `memory_cells` non-priced on this track.

#### **2.2 On-Chain Metering: The Actor VM**

The Actor's Python VM MUST implement Cycle, Access, and Cell metering. CIP-43 §§3–5 specify the Access gate; the following describes the existing Cycle and Cell reference strategies.

**2.2.1 Cycle Calculation (Instruction-based Metering)**

Cycles are metered by instrumenting the core bytecode execution loop of the Python VM.

* **Mechanism:** Before the execution of each Python bytecode instruction, a corresponding cost is deducted from the call's remaining cycles_limit. If the cost exceeds the remaining limit, execution MUST halt immediately with an "Out of Cycles" error.  
* **Cost Table:** A static, consensus-critical cost table (`HashMap<Instruction, u64>`) maps every Python bytecode instruction to a fixed Cycle cost. This table forms part of the protocol constants.  
  **Partial Cost Table (Cycles):**

| Operation Type | Cycles Cost |
| :---- | ----: |
| Python Arithmetic Ops (+, -, *, /, %) | 1 |
| Python Function Call | 10 |
| Dictionary Get/Set | 3 |
| List Append/Access | 2 |
| String Operation (per character) | 1 |
| Mailbox Send (per message, payload extra) | 80 |
| Set Timer (+ cells) | 200 |
| Cancel Timer | 200 |
| Commit Blob (per KiB) | 40 |
| Storage KV Read (per call) | Moved to the `accesses` meter by CIP-43 §6 (legacy 100-cycle term removed) |
| Storage KV Write (per call, + cells) | Moved to the `accesses` meter by CIP-43 §6 (legacy 200-cycle term removed) |
| Cryptography (secp256k1 signature verification) | 10,000 |
| Cryptography (BLS signature verification) | 8,000 |
| Hashing (keccak256, per 32 bytes) | 6 |

**Actor Lifecycle Costs.** Actor deployment and clone (CIP-27 `fork()`) are host-level operations metered on both tracks. Their costs are anchored to a fixed base plus a per-byte term over the supplied code blob, expressed as named protocol constants:

| Constant | Track | Initial Value | Charged When |
| :---- | :---- | ----: | :---- |
| `actor_deploy_base_cycles` | Cycles | 50,000 | Per `DeployActor`; covers address derivation + actor-record write |
| `actor_deploy_cycles_per_byte` | Cycles | 100 | Per byte of supplied code; covers `validate_actor_code` + content-addressed code write |
| `actor_deploy_base_cells` | Cells | 10,000 | Per `DeployActor`; covers the actor-record write |
| `actor_deploy_cells_per_byte` | Cells | 200 | Per byte of supplied code; covers code-blob storage |

Code upgrade (`UpgradeActor`, and the in-actor `upgrade_self` host call) is metered by a parallel `actor_upgrade_*` family with identical initial values (50,000 / 100 / 10,000 / 200).

Library gas is owned by **one canonical table: CIP-26 §3.6** (mirrored verbatim from `node/execution/src/gas.rs`, including the cold-load and catalog-row lines). This CIP restates no values — a partial list here would just drift.

The clone path defined by CIP-27 `fork()` MUST charge `actor_deploy_base_cycles` / `actor_deploy_base_cells` for the new actor-record write, but MUST NOT charge any per-byte code term — no code is supplied or stored (the parent's `code_hash` is reused, and the code blob is already content-addressed on chain). The implementation physically calls `copy_actor_storage` for at most `MAX_ACTOR_KV_COUNT` entries; CIP-30's O(1) storage-root fork is not consensus-live, so this path MUST charge CIP-43 §5.1's `Copy(n) + W(out-keys)` terms. `Copy(n)` is the single aggregate out-access charge for the `n` copied child keys, consumes the structural units specified by CIP-43’s canonical `Copy(n)` weight, is precharged from the O(1) count, and warms those destination keys; the copied keys MUST NOT also charge `W`. Here `W(out-keys)` covers only the child actor record, inherited pin records, and endowment account writes. When CIP-30 activates `child.storage_root := parent.storage_root`, CIP-43 MUST be amended so the fork row becomes `R(child collision) + W(root)` and the per-key copy charge disappears. (Sources: CIP-30 §§3.3–3.4; CIP-43 §5.1.)

**The complete fork schedule** (each component charged only when its condition holds):

| Component | Charge | When |
|---|---|---|
| Actor-record base | `actor_deploy_base_cycles` / `actor_deploy_base_cells` | always |
| Storage copy | CIP-43 §5.1 `Copy(n) + W(out-keys)` per the paragraph above (root-pointer write once CIP-30's subtree share is consensus-live) | full mode |
| Birth slots (`__INITIALIZED__`, `__FORK_MODE__`, `__FORK_PAYLOAD_HASH__`; `__OWNER__` only when written) | reserved-slot `state_set` each | per slot written |
| Clean-mode payload | `payload.len()` Cells at the inline-blob rate | clean mode |
| Fork profile (`child_manifest`) | `len()` Cells at the inline-blob rate | when supplied |
| Library-pin rows | `pin_count × (LIB_PIN_OVERHEAD_CYCLES = 300, LIB_PIN_OVERHEAD_CELLS = 64)` | always |
| Endowment legs | `2 × state_set` | `endowment > 0` |
| Quota-bond legs | `2 × state_set` | bond required (CIP-27 §3.3.3) |
| Residue purge | `state_delete` per discovered row | per row (CIP-27 §3.2) |

Total-cost vectors pin representative sums (bare clean fork; full fork with endowment + bond + profile) and an OOG vector at each of the fork order's two metered points.

**Fee denomination.** Cycle/cell rates, basefee, and every fee product in this CIP are denominated in native ledger **wei** — the number the execution layer multiplies directly into u64 balance debits. Source comments calling these rates "attoCBY/attoDBY" are terminology drift; no conversion factor exists anywhere in the fee path, and an absolute-debit vector (N cycles at rate R ⇒ exactly N×R wei debited) pins the denomination end to end.

These constants are part of the fixed protocol gas schedule. Unlike the dual basefee and lane-fee multipliers (§2.2.3), they are not in the CIP-12 governable parameter set and change only through a node software release.

**2.2.1.1 Per-Instruction Metering Requirements**

The instruction-cost table in §2.2.1 is the required metering model. The node supports enforced instruction charging: `execution/src/pvm_executor.rs` folds interpreter charges into `cycles_used` in `PvmMeteringMode::Enforce`, and `validator/src/setup.rs` selects that mode for generated genesis. `StateSnapshot.pvm_per_instr_gas` reports the instruction contribution; it is not an additional charge. The diagnostic `ObserveOnly` mode excludes that contribution from committed accounting and does not establish conformance to this pricing model.

A conforming implementation MUST:

1. Use the protocol instruction-cost table, with cross-client vectors producing identical totals for the same handler and inputs.
2. Charge **before** executing each bytecode, so the `OutOfCycles` halt point depends on the instruction stream rather than wall-clock timing or host scheduling.
3. Give nested cross-actor calls their own prepaid budgets and compose their totals in call order.
4. Calibrate the table and block/lane budgets against representative workloads before treating per-instruction totals as authoritative. Investigate differences from host-boundary measurements rather than assuming the totals are equivalent.
5. Reject execution on exhaustion without silently falling back to another cost model. Divergent metering is a consensus fault.

Conformance requires checking the enforced cost table and caller paths against these requirements, including nested-call totals and exact exhaustion points. Support for enforced charging alone does not prove that coverage.

**2.2.2 Cell Calculation (Event-based Metering)**

Unlike Cycles, Cells are not consumed on every instruction. They are consumed at specific events where data is read, written, or stored.

* **Mechanism:** Cell costs are charged by explicit calls to meter.consume_cells(byte_count) within the Python VM's host functions and at the transaction processing boundary.  
* **Metering Points:**  
  1. **Intrinsic Calldata:** Before Python VM execution begins, the size of the transaction's payload is charged as Cells.  
  2. **Host Function Calls:** Host functions that interact with storage or data MUST charge for it.  
     * state_set(key, value): charges key.len() + value.len() Cells. Inline blob writes are ordinary stored values, so their bytes are charged here; the per-KiB blob-commit term in §2.2.1 is a Cycles charge, not Cells. Under **CIP-30 (Per-Actor Storage Root)**, state_set (and state_delete) additionally recompute the actor's `storage_root`; that update carries a deterministic, bounded storage-trie-update charge on top of the Cell cost above (CIP-30 §3.3). The exact formula is a fixed protocol constant that MUST be pinned before the per-actor-trie scheme goes consensus-live; until then this term is not charged against consensus.  
     * emit_event(topic, data): charges data.len() Cells for event payload bytes (1 cell/byte, same model as calldata); the topic and any fixed emission overhead are not billed. An explicit schema that first sets or changes the topic declaration is a separate persistent index write and charges the emitter 32 additional Cells for the identifier (CIP-29 §§2.1, 2.3).
  3. **Return Data:** After an Actor's handler successfully returns, the byte size of its return value is charged as Cells.

**2.2.3 Execution Lanes**

The block cycle budget is partitioned into reserved lanes to prevent any single transaction type from monopolizing block space (see Whitepaper §17.9):

| Lane | Budget | Share | Fee Multiplier |
|------|--------|-------|----------------|
| System | 40,000,000 cycles | 50% | 1.0× |
| User | 22,400,000 cycles | 28% | 1.0× |
| Runner | 8,800,000 cycles | 11% | 1.0× |
| Timer | 8,800,000 cycles | 11% | 1.0× |

Shares are of the **80,000,000-cycle block cap** (= 4 × `BLOCK_CYCLES_TARGET`),
which is the sum of the four lanes. Each lane budget is an **independent
absolute per-block ceiling**, not a reserved share of a smaller pool, and unused
capacity in one lane does **not** cascade into another: a transaction is
rejected when its own lane's already-accumulated cycles have reached that lane's
ceiling, checked before the transaction executes. The hard cap on any single
workload is therefore the largest lane (System, 40,000,000 = 2 ×
`BLOCK_CYCLES_TARGET`), not the 80,000,000 sum — which is what makes a
single-lane flood saturate the EIP-1559 curve at the per-block cap.

The four constants are `LANE_SYSTEM_CYCLES` / `LANE_USER_CYCLES` /
`LANE_RUNNER_CYCLES` / `LANE_TIMER_CYCLES` in `node/types/src/constants.rs`, and
the 50/28/11/11 split published here is the ratified one (COW-3411). WP §13
carries the same four values under `<!-- param: … -->` markers, so
`scripts/check_parameter_drift.py` fails if the node and the whitepaper diverge;
this table is prose and is **not** covered by that check — keep it in sync by
hand.

**Lanes partition cycles only — there is no `LANE_*_CELLS` constant and no per-block cells admission cap** (cells have a basefee *target*, `BLOCK_CELLS_TARGET`, but no hard cap — cf. CIP-33 §2.6.4 reservation-lane erratum). A lane-admission vector set pins the four constants.

The **Fee Multiplier** column scales the global cycle/cell basefee for transactions submitted in that lane: `lane_basefee = global_basefee × lane_fee_multiplier`. All lanes default to `1.0×` at genesis (no subsidy, no surcharge). The four multipliers are governance-tunable via CIP-12 **Tier 0** and stored at `0x09` under the single key `system:lane_fee_multipliers` (a table with one entry per lane). Each multiplier MUST lie in `[LANE_FEE_MULT_MIN, LANE_FEE_MULT_MAX] = [1, 1_000_000_000]` ppm; `0` is rejected, since a zero multiplier would drive that lane's effective basefee to `0` and remove the fee floor (a lane-DoS vector). Genesis default is `1_000_000` ppm (1.0×) per lane. Changing a multiplier does not change lane capacity; it only re-weights the per-lane fee curve relative to the global EIP-1559 basefee adjustment.

**2.2.4 Advanced Metering Challenges and Solutions**

Statically pricing high-level bytecode instructions is insufficient. To ensure full determinism, the VM must handle the dynamic nature of the Python language. The following specifications are mandatory:

1. **Dynamic Typing and Operation Surcharges:**  
   * **Problem:** The cost of a bytecode like BINARY_ADD varies depending on the operand types (integers, strings, lists).  
   * **Specification:** The protocol adopts a **base cost + dynamic surcharge** model. The static cost of a bytecode is its minimum base Cycle cost for execution. The VM must inspect the type and size of the operands at runtime and charge a corresponding surcharge. For example, for string or list concatenation, the surcharge must be proportional to their length.  
2. **Cost of Built-in Functions:**  
   * **Problem:** The cost of built-in functions (e.g., len(), sum(), max()) is directly related to their arguments.  
   * **Specification:** All permitted built-in functions must be treated as special host calls and have a well-defined, consensus-critical cost calculation function. For len(), the cost is fixed and low; for sum() or max(), the cost must be proportional to the number of elements iterated over.  
3. **C API Extensions:**  
   * **Problem:** Arbitrary C extensions are a major source of non-determinism and security vulnerabilities.  
   * **Specification:** **The protocol strictly forbids loading and executing arbitrary C extension modules.** Any functionality requiring high-performance implementation (e.g., cryptographic primitives) must be provided by the VM as deterministic, fully metered **host functions**, not through external C libraries.  
4. **Garbage Collection:**  
   * **Problem:** Standard garbage collection (especially generational GC and cycle detection) is inherently non-deterministic regarding when it triggers and how long it runs.  
   * **Specification:** **The VM's garbage collection mechanism must be deterministic.** The protocol recommends a combination of the following strategies:  
     * **Memory Allocation:** The cost of memory allocation operations (creating new objects) is metered via Cells.  
     * **Reference Counting:** The primary memory management is done through reference counting. The costs of these reference counting operations are factored into the Cycle cost of their respective bytecode instructions.  
     * **Cycle Detection:** During the execution of a single transaction, running a non-deterministic cycle detection algorithm that could cause long pauses is **forbidden**. Memory management must ensure that all memory is deterministically reclaimed by the end of the transaction.  
5. **Floating-Point Determinism:**  
   * **Problem:** Floating-point operations can yield minute, inconsistent results across different CPU architectures, operating systems, or compilers, which is fatal for a consensus system.  
   * **Specification:** **The protocol MUST enforce that all floating-point operations are executed via a deterministic, cross-platform software implementation.** Client implementations **MUST NOT** use their host machine's native FPU (Floating-Point Unit). All transcendental functions (e.g., trigonometry, logarithms) must come from a network-wide, version-locked deterministic math library.  
6. **Exception Handling Costs:**  
   * **Problem:** The operations for throwing and catching exceptions (try...except...finally) involve internal processes whose costs are not fixed and could be exploited for cheap attacks.  
   * **Specification:** The cost of exception handling must be explicitly metered. The raise statement, entering a try block, and the jump instructions for executing except / finally blocks must each have their own fixed base Cycle cost.  
7. **Prohibition of Just-In-Time (JIT) Compilation:**  
   * **Problem:** While JIT compilers can improve performance, their compilation timing, optimization paths, and resulting machine code are highly non-deterministic.  
   * **Specification:** To guarantee absolute determinism and predictability, **the protocol strictly forbids the use of any form of Just-In-Time compilation technology within the Actor VM.** The VM must execute bytecode in a pure interpretation mode.  
8. **Module Import System and Standard Library Whitelist:**  
   * **Problem:** Python's import mechanism can load arbitrary modules, including those that depend on the filesystem, network, or system calls. This breaks determinism and security.  
   * **Specification:**  
     * **The protocol MUST maintain a strict "allowed module whitelist".** Only modules on the whitelist can be imported by Actor code.  
     * The whitelist should include: core data structure modules (e.g., collections, itertools), math modules (deterministic implementation of math), deterministic hashlib, and protocol-specific host API modules (e.g., cowboy.messaging, cowboy.storage).  
     * Attempting to import a module not on the whitelist will result in an ImportError and consume a fixed **50 cycles** as a penalty.  
     * Module import cost: **100 cycles** (first import) + actual execution cost of module initialization. Modules are cached, and repeated imports within the same transaction cost **5 cycles**.  
9. **Large Integer Precision Limit:**  
   * **Problem:** Python supports arbitrary precision integer arithmetic. While powerful, malicious code can perform computational DoS attacks by creating astronomically large numbers (e.g., 2**100000000).  
   * **Specification:**  
     * **Integer bit length MUST NOT exceed 4096 bits** (approximately 1234 decimal digits).  
     * Any operation that produces a result exceeding this limit (e.g., pow(), factorial, large multiplication) will raise an OverflowError.  
     * Large integer operation Cycles cost must be proportional to bit length: `base cost + max(bitlen(a), bitlen(b)) / 64` cycles.  
10. **String Encoding Determinism:**  
    * **Problem:** The encode() and decode() operations on strings may have different handling for certain edge Unicode characters across different Python versions or platforms (e.g., replacement characters, error modes).  
    * **Specification:**  
      * The protocol only allows **UTF-8 encoding** for byte string and string conversion.  
      * The error handling mode for str.encode() and bytes.decode() must be fixed to **errors='strict'**, which will raise a UnicodeError on invalid characters rather than silently replacing them.  
      * Cost: 10 + len(input) cycles.  
11. **Coroutine and async/await Deterministic Scheduling:**  
    * **Problem:** The whitepaper mentions allowing "cooperative yields via async/await", but the scheduling order of async code may introduce non-determinism.  
    * **Specification:**  
      * **The protocol prohibits true concurrent execution.** All async/await code must execute in a single thread in **strictly deterministic order**.  
      * await operation scheduling follows a FIFO queue. When a coroutine awaits, control passes to the next ready coroutine in the queue.  
      * Use of asyncio.gather() or other concurrency primitives that may cause non-deterministic execution order is not allowed.  
12. **Object Serialization Determinism:**  
    * **Problem:** Passing data between Actors via messages and persisting state to storage require serialization of Python objects. If serialization is non-deterministic, the same object may produce different byte streams on different nodes.  
    * **Specification:**  
      * The protocol must use Canonical CBOR (RFC 8949, §4.2 deterministic encoding) — map keys sorted, shortest integer encoding, no indefinite-length containers. CIP-6 establishes Canonical CBOR as the authoritative protocol serialization standard.  
      * Dictionary keys must be **sorted lexicographically** before serialization.  
      * Floats must be serialized using the exact bit representation according to the IEEE 754 standard.  
      * Serialization cost: 20 + total_bytes cycles (20 is the base serialization overhead).

#### **2.3 Off-Chain Fee Model: The Runner Market**

It is critical to distinguish on-chain gas from off-chain job fees. The protocol does **not** calculate gas for Runner execution. Instead, it facilitates a free market.

* **Job Fee:** The payment_per_runner specified in CIP-2 is a **market-driven price in CBY**, not a gas calculation. Runners are free to ignore jobs they deem underpriced.  
* **Runner Cost Factors and Metering:** A Runner's operational cost determines its market price. The protocol does not enforce a cost model, but a mature Runner will typically combine **a priori estimation** (to decide whether to accept a job) and **post-mortem metering** (for precise profit calculation).  
  1. **A Priori Estimation (Job Decision):**  
     * **Based on Job Metadata:** A Runner's decision relies heavily on the result_schema and task_definition from CIP-2. The developer-provided expected_execution_ms, identification of the model ID (model_id), and analysis of the input data size are key estimation inputs.  
     * **Driven by Historical Data:** Runners should maintain a database of historical jobs. For a previously executed model_id or a similar task, a Runner can query the average or P95 resource consumption (CPU time, peak memory) as a basis for the current estimation.  
     * **Benchmarking:** For common public models (e.g., specific LLMs), a Runner can perform advance benchmarks on its standardized hardware to build an internal cost model (e.g., "The cost of generating 100 tokens with Llama3-8B is approximately X").  
  2. **Post-Mortem Metering (Cost Accounting):**  
     * **Utilizing Container Runtime APIs:** A Runner's orchestration service can obtain precise resource usage data via the container runtime API.  
     * **Computation Time:** Total execution duration can be obtained by recording the start and end timestamps of the container. For more granular CPU time, container monitoring tools or direct reads from the kernel's cgroup filesystem can be used.  
     * **Memory Usage:** Container monitoring tools can provide the peak memory usage (MAX USAGE) during the container's lifecycle. This is a key metric for determining memory costs.  
     * **Data Transfer:** The orchestration service must record inbound traffic from downloading models or data from the network (e.g., IPFS) and outbound traffic from making HTTP requests.  
* This "estimate-execute-meter" feedback loop allows a Runner to dynamically balance maximizing profit and minimizing risk, enabling it to remain competitive in the decentralized computation market.  
* **The TEE Premium:** When a job request sets tee_required=true, Runners that support TEE will only accept jobs with a significant price premium. This premium accounts for:  
  1. **Hardware Cost:** The requirement for specialized server hardware (e.g., Intel SGX or AMD SEV enabled).  
  2. **Performance Overhead:** TEE execution incurs a non-trivial performance penalty due to memory encryption and enclave transitions, increasing the required compute time.  
  3. **Confidentiality as a Service:** The Runner is charging for the high-value guarantee of data confidentiality, which is a premium feature.

#### **2.4 Dual Basefee Adjustment Mechanism**

To smooth short-term fee volatility and make them predictable in the long term, Cowboy implements an independent EIP-1559-style basefee adjustment mechanism for both the Cycles and Cells tracks. At its core is a negative feedback loop designed to keep the resource usage of each block stable around a preset target.

CIP-43 §8 extends this mechanism with an independent Access track, `access_basefee`, and target `T_a = block_access_target`. For that track, `x` below also includes `a`; the authoritative common constants are Technical Whitepaper §17.8's `alpha = 96`, `+/-1/96` clamp, and `MIN_BASEFEE = 10,000`.

* **Core Principle:** The protocol sets an ideal EIP-1559 "target usage" (T_c and T_b) for Cycles and Cells that the basefee tracks. **Two distinct capacity bounds, both real and both constitutional (WP §4.2), must be stated together:** the **per-workload cap = the largest lane = `LANE_SYSTEM_CYCLES = 40,000,000 = 2 × T_c`** — the EIP-1559 elasticity limit a single-lane flood saturates (this is the `2× target` relation `basefee.rs` SPEC-MG-1 asserts as a core invariant; the target is 50% *of this*), and the **whole-block hard cap = the lane sum = `80,000,000 = 4 × T_c`** — the aggregate admission ceiling across all four lanes (the target is 25% *of this*). **Canonical constants (genesis defaults; the live `T_c`/`T_b` are `BasefeeConfig` fields, CIP-12 Tier-0 governance-mutable — values mirror `types/src/constants.rs`): `BLOCK_CYCLES_TARGET = 20,000,000`, `BLOCK_CELLS_TARGET = 4,000,000`; `BASEFEE_ALPHA = 96`; `BASEFEE_MAX_CHANGE_DENOM = 96`; `MIN_BASEFEE = 10,000`; `MAX_BASEFEE = 10^24`.** There is no separate "block capacity 40,000,000" constant — `LANE_SYSTEM_CYCLES` is the largest-lane per-workload cap, not a block-wide cap.
  * If the previous block's actual usage U was **higher** than the target T, the current block's basefee will be adjusted **upwards** to curb demand.  
  * If the previous block's actual usage U was **lower** than the target T, the current block's basefee will be adjusted **downwards** to stimulate demand.  
* **Update Rule Explained:** Each block calculates the new basefee based on the parent block's usage via the following formula:  
  ```
  delta_x = basefee_{x, old} * |U_x - T_x| / T_x / ALPHA        # geometric update
  basefee_{x, new} = basefee_{x, old} ± min(delta_x, basefee_{x, old} / DENOM)
  ```  
  Where:  
  * x: Represents c (Cycle), b (Cell), or — under CIP-43 §8 — a (Access).
  * U_x: Total usage of resource x in the parent block.  
  * T_x: The target usage for resource x.  
  * ALPHA = 96: the learning-rate denominator (a larger ALPHA means smoother adjustment).  
  * The change is clamped to basefee/DENOM (DENOM = 96) per block, and the result to [MIN_BASEFEE, MAX_BASEFEE] = [10,000, 10^24].  
* **Example Calculation:**  
  * T_c (cycle target) is 20,000,000.  
  * Parent block's actual usage U_c was 24,000,000 (20% over target).  
  * The fee delta is (24M - 20M) / 20M / 96 = 0.2 / 96 ≈ 0.00208, or ≈ +0.21% (well inside the 1/96 ≈ 1.04% per-block clamp).  
  * Therefore, the new cycle basefee is ≈0.21% higher than the old one. A canonical vector set pins the update at exact/over/under/clamped boundaries.  
* **Fee Composition and Distribution:**  
* **User Transaction:** When submitting a transaction, a user specifies a `max_fee_per_*` and a `tip_per_*` for each resource.  
  * **Fee Burn:** After a transaction is included, its basefee portion (cycles_used * basefee_cycle + accesses_used * access_basefee + cells_used * basefee_cell) **MUST be 100% burned** (CIP-43 §8). This exerts a constant deflationary pressure on the native CBY token.
  * **Proposer Tip:** The portion paid to the block producer is the tip. The effective tip paid is min(tip_per_*, max_fee_per_* - basefee_*). This provides a direct economic incentive for block producers to include transactions and creates a priority "tip market".

#### **2.5 Multi-Signer Transaction Admission and Metering**

A transaction may carry up to `MAX_ADDITIONAL_SIGNERS` (16) co-signers, each costing one secp256k1 recovery at admission. That work is bounded by three independent mechanisms, because no single one suffices.

* **Consensus constants.** The structural math and block budget are owned by `cowboy-protocol-codec`'s constants module, alongside `MAX_ADDITIONAL_SIGNERS`, and re-exported through `cowboy_types`: `TX_BASE_CYCLES = 5,000`, `COSIGNER_VERIFY_CYCLES = 10,000`, and `MAX_BLOCK_COSIGNERS = 5,000`. None of these exists today, and the execution-side crypto price lives only in node code (`CRYPTO_SECP256K1_VERIFY_CYCLES`) — but the preflight runs inside `Transaction::verify()`, which is protocol-codec code, so the codec must own the numbers it checks. Equality tests pin the codec constants against their node-side counterparts so admission and billing cannot drift — and they live on the **node side**, because the dependency points that way (`node/types` depends on the codec; the codec cannot import node crates): `node/execution` asserts `TX_BASE_CYCLES == GasCosts::default().base_cycles == BASE_CYCLES_SPAM_PENALTY` and `COSIGNER_VERIFY_CYCLES == CRYPTO_SECP256K1_VERIFY_CYCLES`; `node/types` asserts `MAX_BLOCK_COSIGNERS == MAX_BLOCK_TRANSACTIONS`. The codec's own tests cover its local constants and the preflight.
* **Intrinsic per-co-signer charge.** A transaction is charged `COSIGNER_VERIFY_CYCLES` per entry in `additional_signers`. Let `k = additional_signers.len()` and `sig_cycles = k × COSIGNER_VERIFY_CYCLES`, computed with a checked multiplication.
  * *Structural minimum.* A non-deferred transaction is rejected unless `cycles_limit ≥ TX_BASE_CYCLES + sig_cycles`. The check runs inside `Transaction::verify()` **before any signature recovery** — RPC admission, the mempool listener, and block verification all reach recovery by calling `verify()` directly, so placing the preflight inside makes the ordering structural rather than a call-site contract, and it runs before either signature mode's digest computation or recovery (CIP-40 §7.3). The constants are deliberately not read from the cost table (the table is configuration; a consensus rule cannot depend on it). Rejection changes no nonce, no fee, and no state. Deferred transactions are exempt: their `additional_signers` is structurally empty, so `sig_cycles = 0`.
  * *Precharge.* `sig_cycles` is consumed before `tx_base` and calldata, so receipts and any deferred child's `origin_remaining_cycles` account for it. The structural minimum is also load-bearing for correctness: the meter calls after worst-case escrow propagate errors with `?`, so a limit too small for the precharge would otherwise escape the shared charging tail.
  * *Non-sponsorable, split at settlement.* Gas normally resolves through the sponsorship cascade (actor balance → CIP-28 default card → `UseOwnerBalance` → sender). The co-signer charge is excluded from every tier: co-signers are chosen unilaterally by whoever assembles the envelope, and the sponsor never consented to them — a sponsorable charge would let an attacker drain 160,000 Cycles per transaction from any sponsoring agent without executing anything the sponsor wanted. The settlement algebra, self-contained: escrow `R = cycles_limit × max_fee_per_cycle + cells_limit × max_fee_per_cell` is deducted up front with checked arithmetic. The fee function is linear, so it splits exactly: `F_sig = F(sig_cycles, 0)` and `F_exec = F(total_cycles − sig_cycles, cells_used)`. The cascade tiers are offered only `F_exec`, with the invariant `actor_paid + owner_paid ≤ F_exec`. The sender's share is `S = F_sig + (F_exec − actor_paid − owner_paid)`; the refund is `R − S`, and burn + tip equal `F_sig + F_exec` — conservation holds with no new debit path. When the `UseOwnerBalance` owner **is** the sender, the owner debit folds into the single sender write-back (one `set_account`, matching the existing fold) rather than a second write that the write-back would clobber; the final `tx.from` debit is then `S + owner_paid = F_sig + F_exec − actor_paid`. All settlement arithmetic — the subtraction in `F_exec`, the products and sum in `max_execution_cost`, the refund — is checked, with the structural minimum guaranteeing the `cycles_limit − sig_cycles` subtraction cannot underflow. The CIP-28 card cap precheck tests `max_execution_cost = (cycles_limit − sig_cycles) × max_fee_per_cycle + cells_limit × max_fee_per_cell`, not the full worst case — otherwise a card rejects on headroom it is forbidden to fund.
  * *Post-escrow settlement.* The following closed category of **deterministic intrinsic-meter/arithmetic failures** after the escrow deduction — meter exhaustion (cycles, base cells, calldata cells) and arithmetic failures such as a calldata `checked_mul` overflow — MUST follow the shared settlement tail (nonce increment, receipt, `F_sig` retention, refund of unused escrow) identically to `OutOfGas`. This is a **prospective behavior change**, not a description: today these pre-dispatch charges `?`-escape before the nonce, execution result, and fee tail are reached, and the implementation captures and converts those escapes into the charged outcome above — an uncaptured escape produces a zero-charge receipt whose signature work was already performed. The boundary preserved exactly: the meter and arithmetic failures named above flow through **charged** settlement; the existing `StoreError` escape paths — which cover both genuine node-local faults **and** deterministic committed-state storage-cap rejections (`StorageFull`, `MailboxFull`, `TimerListFull`, and kin) — keep their current, path-specific behavior. On the `Instruction::System` arm that means rollback to the per-transaction savepoint (which is deliberately scoped to that arm; Actor/Custom/Library take no such savepoint, and post-settlement `StoreError` escapes occur outside it) followed by the zero-charge failed result under the existing #343 policy. This rule changes none of those paths: it neither converts any `StoreError` escape to charged settlement, nor claims a blanket rollback where none exists today, nor claims they produce no receipt. Acceptance, one test per path with exact fee/state outcomes, each on a co-signed transaction: (1) a System-arm `StoreError` rolls back to the savepoint and escapes zero-charge under #343; (2) an Actor-arm execution `StoreError` takes no savepoint and is converted to a **charged** `ExecutionStatus::ExecutionError` through the normal fee tail; (3) a settlement-phase `StoreError` (actor/card/owner debit) escapes zero-charge under #343, outside the System savepoint, with no rollback guarantee asserted.
* **Block co-signer budget.** A block's total `additional_signers` across all transactions MUST NOT exceed `MAX_BLOCK_COSIGNERS` (5,000, pinned equal to `MAX_BLOCK_TRANSACTIONS`), bounding total recoveries per block at 10,000 — twice the pre-existing worst case, a stated multiple rather than a chosen number. The bound is checked in block verification from the decoded transactions **before** any recovery runs. This is the binding constraint the charge cannot provide: a transaction failing the nonce, basefee, or balance check never reaches metering, yet its signatures were already recovered — a leader could otherwise fill a block with valid-signature stale-nonce transactions and impose 85,000 recoveries (5,000 × 17) on every validator for free. The budget counts additional signers only: a block admits 312 fully loaded 16-co-signer (17-signature) transactions (4,992) and rejects at 313 (5,008). Proposer selection and verification MUST accumulate the count identically; a proposer at the budget skips and requeues the candidate rather than truncating it.
* **Ingress caps.** The block budget does not bound what an unauthenticated submitter makes a node do before a block exists — both ingress surfaces (RPC submit, mempool listener) verify immediately after decode, and one submission decodes up to `MAX_SUBMISSION_TRANSACTIONS` (128) transactions, i.e. up to 2,048 co-signer recoveries per request — 2,176 total including primaries. Each surface MUST enforce a per-request co-signer cap and a per-peer rate limit denominated in **co-signers**, rejecting over-cap submissions before any recovery runs. The per-peer key MUST be stable across connections — the authenticated client identity where one exists, otherwise the remote network identity under explicit trusted-proxy rules (direct source address by default; forwarded-for only from a configured trusted proxy) — since a connection-scoped limiter is reset by reconnecting. A **global per-node co-signer-work budget** (token bucket over total co-signer recoveries per unit time, applied before recovery regardless of attribution) backstops per-peer dilution. The budget is one shared limiter: a single atomic bucket injected into both ingress paths, consumed before recovery on each — two per-surface buckets would double the promised budget. Rate limiting is enabled by default with finite defaults; missing or malformed configuration fails closed to the defaults, never to "unlimited." Numeric values are admission policy; the pre-recovery ordering, co-signer denomination, key stability, and global backstop are normative.

Acceptance (every fee test pins basefee and tip to fixed values and asserts exact deltas, not inequalities): at k = 0/1/16, exact cycles, sender-balance, burn, tip, and deferred-child `origin_remaining_cycles` deltas; each cascade tier funds `F_exec` only while the sender funds `F_sig`, including `owner == sender` and locked vesting balances; below-minimum `cycles_limit` rejected pre-recovery with **no nonce increment, no fee, and no state change**; stale-nonce, basefee-failed, and underfunded transactions remain zero-charge, bounded by the budget and ingress caps rather than the charge; post-escrow failures (cycle exhaustion, low base cells, calldata-cell exhaustion, and calldata `checked_mul` overflow) all settle through the shared tail with no zero-charge receipt; `k × COSIGNER_VERIFY_CYCLES` overflow checked; the three codec/node constant-equality tests above; card precheck admits on `max_execution_cost` only; blocks at exactly 5,000 co-signers verify and 5,001 reject without performing recoveries; a stale-nonce 16-co-signer block at `MAX_BLOCK_TRANSACTIONS` is bounded by the budget, not the charge; proposer requeues over-budget candidates on both selection paths; ingress accepts cap `C` and rejects `C + 1` pre-recovery per surface; under a deterministic clock, per-peer request `R` within the window is accepted and `R + 1` rejected, per surface; the per-peer key survives reconnection and parallel connections; the global bucket saturates under all-compliant requests; a node started with absent or malformed limiter config enforces the defaults.

**Implementation map (§2.5):**

| Change | Location |
| :---- | :---- |
| `TX_BASE_CYCLES`, `COSIGNER_VERIFY_CYCLES`, `MAX_BLOCK_COSIGNERS` constants + structural preflight inside `verify()`, ahead of recovery; local constant/preflight tests | `cowboy-protocol/crates/cowboy-protocol-codec/src/{constants,transaction}.rs` (re-exported via `cowboy_types`) |
| Node-side equality tests: `TX_BASE_CYCLES == GasCosts::default().base_cycles == BASE_CYCLES_SPAM_PENALTY`, `COSIGNER_VERIFY_CYCLES == CRYPTO_SECP256K1_VERIFY_CYCLES` in `node/execution/src/gas.rs` tests; `MAX_BLOCK_COSIGNERS == MAX_BLOCK_TRANSACTIONS` in `node/types/src/constants.rs` tests | `node/execution`, `node/types` |
| Two-bucket metering: `sig_cycles` precharge, `F_sig`/`F_exec` settlement split, card precheck on `max_execution_cost`; conversion of the current pre-dispatch `?`-escapes (base/calldata meter and arithmetic failures after escrow) into captured, charged outcomes through the shared tail | `node/execution/src/execution/transaction.rs` |
| Block co-signer budget, checked before signature recovery; proposer accumulation on both selection paths (nonce-ordered and fallback), skip-and-requeue | `node/chain/src/application.rs`, proposer selection |
| Per-request cap and cross-connection per-peer limiter per surface; ONE shared atomic global work bucket injected into both paths, consumed before recovery; fail-closed config | `node/rpc/src/handlers/chain.rs`, `node/chain/src/mempool_listener.rs`, shared limiter service |


### **3. Rationale**

* **Separation of Concerns:** By cleanly separating on-chain gas (Cycles/Cells) from off-chain market fees, the protocol avoids trying to price real-world, variable resources like electricity and hardware. The on-chain component remains a pure, deterministic system, while the off-chain component is an efficient free market.  
* **Bytecode-Level Cycle Metering:** This is the only robust method to ensure deterministic compute accounting. It naturally handles all control flow (loops, recursion) without special cases and is difficult to game, as all work is metered.  
* **Event-Based Cell Metering:** Data costs are not continuous like computation. Metering Cells only at I/O boundaries is more efficient and accurately reflects how data impacts the system (e.g., during state writes or network propagation).  
* **TEE as a Market Feature:** Making TEE a priced, optional feature allows users to pay for confidentiality only when they need it, rather than forcing the cost onto all users of the off-chain system.  
* **Fee Market Predictability and Efficiency (EIP-1559):** The EIP-1559-style mechanism is introduced to solve the inefficiencies of traditional first-price auction fee models. By algorithmically adjusting the base fee, it provides a clear "market price" for users, which greatly simplifies fee estimation and reduces average user wait times and transaction costs. Furthermore, the burning of the base fee creates a continuous deflationary pressure on the native CBY token, directly linking its value to the network's actual usage and benefiting the long-term health of the ecosystem.

### **4. Security Considerations**

* **Metering Consensus:** The bytecode-to-cycle cost table is a highly sensitive, consensus-critical parameter. Any change requires a coordinated hard fork. All clients MUST implement the exact same table.  
* **Infinite Loops:** The cycles_limit per transaction is the primary defense against infinite loops in Actor code, preventing them from halting the chain.  
* **Data Gas-Bombing:** The cells_limit per transaction, combined with max_return_bytes defined in CIP-2, prevents Actors or Runners from overwhelming the chain with excessively large data payloads that would incur high processing and storage costs for nodes.  
* **Host Function Metering:** Every host function exposed to the VM that reads, writes, or allocates data MUST be correctly instrumented with Cell metering. A missing metering call is a potential DoS vector and a critical security vulnerability.
* **Signature-Verification Cost Asymmetry:** Signature recovery runs across every transaction in a block before execution, and a transaction that fails the nonce, basefee, or balance check returns zero-charge — its signatures were nevertheless all recovered. Pricing alone therefore cannot bound recovery work; the §2.5 block co-signer budget (checked before recovery) is the binding constraint, the intrinsic charge prices only transactions that execute, and the ingress caps bound pre-block work from unauthenticated submitters.
