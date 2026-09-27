# Ethereum Interoperability: Technical Integration Guide

## Overview

Cowboy is designed for **clever interoperability** with Ethereum, not reimplementation. Rather than duplicating Ethereum's extensive standards (ERC-721, ERC-1155, etc.), Cowboy actors orchestrate and interact with existing Ethereum assets while providing unique capabilities: autonomous execution via timers (CIP-1), AI/heavy compute via off-chain runners (CIP-2), and a Python-native developer experience.

This document outlines the technical architecture for Ethereum-Cowboy interoperability.

## Design Principles

1. **No Duplication**: Don't reimplement what Ethereum does well (NFT standards, DeFi primitives)
2. **Leverage CIP-2**: Use existing off-chain compute infrastructure for Ethereum integration
3. **Progressive Decentralization**: Start simple, add trust-minimization over time
4. **Developer-First**: Python-native APIs for Ethereum interaction
5. **Composability**: Cowboy actors orchestrate Ethereum assets with autonomous behavior

## Use Case Performance Analysis

To evaluate the architecture options, let's analyze three critical use cases:

### Use Case 1: Real-time Event Monitoring

**Scenario**: Actor wants to react when a specific Ethereum wallet receives a USDC transfer

| Architecture | Latency | Details |
|--------------|---------|---------|
| **Option 1: Validator nodes** | ~15-20 seconds | Ethereum block time (12s) + Cowboy block (2s). Validators monitor via websocket. **Near real-time, trustless**. |
| **Option 2: Light client** | ~13-15 minutes | Must wait for Ethereum finality (12.8 min) + proof generation (30s). **Too slow for real-time**. |
| **Option 3: Optimistic oracle** | ~6-60 minutes | Ethereum block (12s) + runner detection (5s) + challenge period (5-60 min). **Challenge period kills UX**. |
| **Option 4: Hybrid (fast path)** | ~20 seconds | Ethereum block (12s) + runner detection (5s) + Cowboy block (2s). 2/3 runner consensus. **Best balance**. |
| **Option 5: Hybrid (secure path)** | ~13-15 minutes | Same as light client. For high-value operations requiring cryptographic proof. |

**Winner**: **Option 4 (Hybrid)** with fast path = **~20 seconds**

**Implementation example**:

```python
class PriceAlertActor:
    """Monitors Ethereum DEX for price changes, reacts in ~20 seconds"""

    def __init__(self):
        # Subscribe to Ethereum events via CIP-2 runners
        subscribe_ethereum_event(
            contract="0xUniswapV2Pair",
            event="Swap",
            callback=self.on_swap,
            num_runners=3  # Require 2/3 consensus
        )

    def on_swap(self, event_data):
        # Called ~20 seconds after Ethereum event
        price = self.calculate_price(event_data)

        if price < self.alert_threshold:
            # Trigger immediate action
            self.send_message(trader_actor, {
                "action": "buy",
                "amount": 1000
            })
```

### Use Case 2: Transfer Balance Between Ethereum and Cowboy

**Scenario**: User has 10 ETH in MetaMask, wants to move 5 ETH to Cowboy actor

| Direction | Architecture | Latency | Details |
|-----------|--------------|---------|---------|
| **ETH → Cowboy** | Option 1: Validator nodes | ~30-45 seconds | Deposit (15s) + validators detect (15s) + mint (2s) |
| | Option 2: Light client | ~15 minutes | Deposit (15s) + finality (12.8 min) + proof (30s) + verify (2s) |
| | Option 3: Optimistic | ~6-60 minutes | Deposit (15s) + challenge period |
| | **Option 4: Hybrid (fast)** | **~40 seconds** | Deposit (15s) + runner consensus (20s) + mint (5s) |
| | Option 4: Hybrid (secure) | ~15 minutes | With cryptographic proof |
| **Cowboy → ETH** | Option 1: Validator nodes | ~40 seconds | Burn (2s) + validator sign (5s) + Ethereum submit (30s) |
| | Option 2: Light client | ~1 minute | Requires Cowboy light client on Ethereum (complex!) |
| | Option 3: Optimistic | ~6-60 minutes | Challenge period |
| | **Option 4: Hybrid** | **~45 seconds** | Burn (2s) + runner relay (10s) + Ethereum finality (30s) |

**Winner**: **Option 4 (Hybrid)** = **~40-45 seconds both directions**

**Implementation example**:

```python
# User deposits ETH on Ethereum
# (via MetaMask transaction to bridge contract)

class EthereumBridgeActor:
    """Handles ETH <-> Cowboy transfers in ~40 seconds"""

    def on_ethereum_deposit(self, deposit_event):
        # Called ~40 seconds after Ethereum deposit
        # deposit_event from 2/3 runner consensus

        user = deposit_event["user"]
        amount = deposit_event["amount"]

        # Mint wrapped ETH on Cowboy
        wrapped_eth = get_actor("wrapped_eth_cip20")
        wrapped_eth.mint(user, amount)

        # Optional: Request light client proof for large amounts
        if amount > 100 * 10**18:  # > 100 ETH
            request_verification_proof(deposit_event)

    def withdraw_to_ethereum(self, user, amount):
        # User wants to move Cowboy ETH back to Ethereum

        # Burn wrapped ETH
        wrapped_eth = get_actor("wrapped_eth_cip20")
        wrapped_eth.burn(user, amount)

        # Request runner to submit Ethereum withdrawal
        submit_ethereum_withdrawal(
            user_eth_address=user,
            amount=amount,
            payment_to_runner=calculate_eth_gas_cost() * 1.2
        )
        # Ethereum transaction submitted in ~45 seconds
```

### Use Case 3: MetaMask Integration

**Scenario**: User signs Cowboy transaction with MetaMask without requiring MetaMask to implement a new protocol

**Key Insight**: This is **independent of the Ethereum interop architecture**! It's about wallet UX and cryptographic compatibility.

**Solution**: Cowboy uses the same cryptography as Ethereum:

- **Same elliptic curve**: secp256k1 (ECDSA)
- **Same address format**: `keccak256(pubkey)[12:]`
- **Same signature format**: (r, s, v) tuples
- **Result**: **Ethereum addresses work natively on Cowboy!**

**MetaMask signs Cowboy transactions via EIP-712 structured data**:

```typescript
// Frontend code - MetaMask signs Cowboy transaction
const cowboyTx = {
  types: {
    CowboyTransaction: [
      { name: "to", type: "address" },
      { name: "method", type: "string" },
      { name: "args", type: "bytes" },
      { name: "cyclesLimit", type: "uint64" },
      { name: "cellsLimit", type: "uint64" },
      { name: "maxFeePerCycle", type: "uint256" },
      { name: "nonce", type: "uint256" }
    ]
  },
  domain: {
    name: "Cowboy",
    version: "1",
    chainId: 42069,  // Cowboy chain ID
  },
  message: {
    to: "0xActorAddress",
    method: "transfer",
    args: "0x000...abc",  // ABI-encoded args
    cyclesLimit: 1000000,
    cellsLimit: 10000,
    maxFeePerCycle: 100,
    nonce: 42
  }
}

// User sees familiar MetaMask popup
// "Sign message for Cowboy"
const signature = await ethereum.request({
  method: "eth_signTypedData_v4",
  params: [userAddress, JSON.stringify(cowboyTx)]
})

// Submit to Cowboy
await fetch("https://cowboy-rpc.example.com", {
  method: "POST",
  body: JSON.stringify({
    transaction: cowboyTx.message,
    signature: signature
  })
})
```

**Cowboy validators verify the signature**:

```python
# In Cowboy transaction validation
def verify_transaction(tx, signature):
    # Standard ECDSA recovery (same as Ethereum)
    message_hash = eip712_hash(tx)
    signer = ecrecover(message_hash, signature)

    # Address derivation identical to Ethereum
    # User's MetaMask address works on Cowboy!
    assert signer == tx.from_address

# Cross-chain identity example
class UnifiedAccount:
    """Same address on both Ethereum and Cowboy"""

    def __init__(self, private_key):
        # Identical key derivation
        self.eth_address = derive_address(private_key)
        self.cowboy_address = derive_address(private_key)

        # These are the SAME!
        assert self.eth_address == self.cowboy_address
```

**User Experience**:

1. User visits Cowboy dApp
2. Clicks "Connect Wallet" → MetaMask popup (familiar!)
3. MetaMask shows: "Cowboy wants to connect to 0x742d35Cc..."
4. User signs transaction → familiar EIP-712 popup
5. Transaction submitted to Cowboy network
6. **No MetaMask changes required!**

**Winner**: **Works with ALL architecture options** via EIP-712 structured signing

---

## Recommendation Summary

Based on the three use cases:

| Requirement | Best Option | Latency |
|-------------|-------------|---------|
| Real-time event monitoring | **Option 4 (Hybrid, fast path)** | ~20 seconds |
| Asset transfers (ETH ↔ Cowboy) | **Option 4 (Hybrid, fast path)** | ~40-45 seconds |
| High-value operations | **Option 4 (Hybrid, secure path)** | ~13-15 minutes |
| MetaMask integration | **All options** (EIP-712) | N/A |

**Overall Winner**: **Option 4 (Hybrid Approach)**

- Start with CIP-2 oracle consensus (fast, ~20-45 seconds)
- Add light client proofs for high-value operations (trustless, ~15 minutes)
- Support MetaMask via EIP-712 (no changes needed)
- Validators don't need to run Ethereum nodes
- Progressive decentralization path

---

## Architecture Options

### Option 1: Validator-Run Ethereum Nodes

**Architecture:**
- Every Cowboy validator runs a full Ethereum node (geth, reth, etc.) alongside their Cowboy node
- Ethereum block hashes are included in Cowboy consensus
- Actors query Ethereum state via deterministic host functions
- All validators must agree on Ethereum state at specific block heights

**Implementation:**
```python
# In Cowboy actor
from cowboy_sdk.ethereum import eth_call

# Host function queries local geth node
balance = eth_call(
    contract="0x...",
    method="balanceOf",
    args=[user_address],
    eth_block=19000000  # Pinned block height for determinism
)
```

**Pros:**
- ✅ Fully trustless - no reliance on external oracles
- ✅ Low latency - direct RPC access
- ✅ Simple consensus model - all validators have same view

**Cons:**
- ❌ High resource requirements (each validator needs ~1TB+ for Ethereum)
- ❌ Tight coupling between Cowboy and Ethereum consensus
- ❌ Validators must sync/maintain Ethereum nodes
- ❌ Ethereum state bloat affects Cowboy validators

**Consensus Mechanism:**
```
Cowboy Block Header:
  - cowboy_state_root
  - ethereum_block_hash    ← Checkpoint Ethereum state
  - ethereum_block_height

Determinism: All actors reading Ethereum state at block N
must get identical results across all validators.
```

---

### Option 2: Embedded Light Client

**Architecture:**
- Cowboy protocol embeds Ethereum beacon chain light client
- Validators track Ethereum finality (post-merge proof-of-stake)
- Actors submit Merkle proofs for state reads
- No full Ethereum node required

**Implementation:**
```python
from cowboy_sdk.ethereum import eth_call_with_proof

# Actor must provide Merkle proof
result = eth_call_with_proof(
    contract="0x...",
    method="balanceOf",
    args=[user_address],
    eth_block=19000000,
    proof=merkle_proof  # Generated off-chain, verified on-chain
)
```

**Proof Verification:**
- Cowboy validators verify Merkle proofs against Ethereum state root
- State root is trustlessly obtained from Ethereum beacon chain
- Only light client (~few MB) needed per validator

**Pros:**
- ✅ Trustless - cryptographic verification
- ✅ Low resource requirements (~10 MB vs ~1 TB)
- ✅ No dependency on full Ethereum nodes

**Cons:**
- ❌ Complex proof generation (actors or runners must generate Merkle proofs)
- ❌ Higher latency (proof generation + submission)
- ❌ Increased on-chain computation (proof verification costs Cycles)
- ❌ Ethereum beacon chain protocol changes require Cowboy updates

**Consensus Mechanism:**
```
Cowboy validators maintain:
  - Ethereum beacon chain headers
  - Finality checkpoints
  - State root commitments

Actor submits:
  - Claim: "balanceOf(0xabc) = 100 at block 19000000"
  - Merkle proof: [node1, node2, ..., nodeN]

Validator verifies:
  - Proof validates against state_root(19000000)
  - state_root is from finalized Ethereum beacon block
```

---

### Option 3: Optimistic Oracle Pattern

**Architecture:**
- Specialized "Ethereum oracle" runners (similar to CIP-2 off-chain compute)
- Runners submit Ethereum state with stake
- Fraud proof window for challenges
- Slashing for incorrect data

**Implementation:**
```python
from cowboy_sdk.ethereum import eth_call_optimistic

# Request goes to oracle runners
result = eth_call_optimistic(
    contract="0x...",
    method="balanceOf",
    args=[user_address],
    eth_block=19000000,
    challenge_period_blocks=100  # ~10 minutes on Cowboy
)

# Result available after challenge period
# If no challenge, result is finalized
```

**Oracle Mechanism:**
1. Actor submits Ethereum read request to Oracle contract
2. N registered oracle runners independently query Ethereum
3. Runners submit results + stake to Cowboy
4. If consensus (e.g., 2/3 agree), result is accepted after challenge period
5. Anyone can challenge with fraud proof during challenge period
6. Incorrect runners are slashed

**Pros:**
- ✅ No validator requirements - only oracle runners need Ethereum nodes
- ✅ Flexible - can support any Ethereum RPC call
- ✅ Leverages existing CIP-2 runner infrastructure
- ✅ Economic security via staking/slashing

**Cons:**
- ❌ Latency from challenge period (minutes to hours)
- ❌ Trust assumption on oracle honesty (mitigated by staking)
- ❌ Requires oracle runner infrastructure
- ❌ Challenge mechanism adds complexity

**Consensus Mechanism:**
```
Oracle Contract State:
  requests: {
    request_id => {
      query: (contract, method, args, block),
      submissions: [
        {runner: 0xabc, result: "100", stake: 1000 CBY},
        {runner: 0xdef, result: "100", stake: 1000 CBY},
        {runner: 0x123, result: "99",  stake: 1000 CBY}  ← Outlier
      ],
      finalized_at: block_height + challenge_period
    }
  }

Finalization:
  - After challenge period, majority result is canonical
  - Minority runners are slashed
  - Majority runners collect fees
```

---

### Option 4: Hybrid Approach (Recommended)

**Architecture:**
- Combines flexibility of oracles with security of light clients
- Standard reads use CIP-2 runner consensus (fast, cheap)
- High-value operations use light client proofs (trustless, expensive)
- Validators optionally run Ethereum nodes (not consensus-required)

**Implementation:**
```python
from cowboy_sdk.ethereum import eth_call, eth_call_verified

# Standard read: Fast, via CIP-2 runners
balance = eth_call(
    contract="0x...",
    method="balanceOf",
    args=[user_address],
    eth_block=19000000,
    num_runners=3  # Consensus from 3 runners
)

# Verified read: Slower, with cryptographic proof
balance_verified = eth_call_verified(
    contract="0x...",
    method="balanceOf",
    args=[user_address],
    eth_block=19000000,
    proof=merkle_proof  # Must provide proof
)

# Actor chooses based on value at risk
if amount > 10000:
    use_verified_read()
else:
    use_standard_read()
```

**How It Works:**

1. **Standard Path (CIP-2 Oracle):**
   - Actor calls `eth_call()` → creates off-chain task (CIP-2)
   - Multiple runners independently query Ethereum
   - Runners submit results to Cowboy
   - Consensus via majority (e.g., 2/3 agreement)
   - Fast finality, economic security

2. **Verified Path (Light Client):**
   - Actor (or helper service) generates Merkle proof
   - Submits proof + claim to Cowboy
   - Validators verify against beacon chain state root
   - Cryptographic security, slower, more expensive

**Pros:**
- ✅ Flexible: Actors choose security/cost/speed tradeoff
- ✅ Gradual decentralization: Start with oracles, add proofs later
- ✅ Uses existing CIP-2 infrastructure
- ✅ No mandatory validator requirements
- ✅ Trustless option available when needed

**Cons:**
- ❌ More complex: Two paths to maintain
- ❌ Requires developer judgment on which path to use

---

## Recommended Implementation Roadmap

### Phase 1: CIP-2 Oracle Integration (Months 1-3)

**Goal:** Enable basic Ethereum reads via off-chain runners

**Components:**
1. **Standard Ethereum Read Task Type**
   - Extend CIP-2 task definitions to include `ethereum_read`
   - Task schema defines: contract, method, args, block height
   - Runners with Ethereum RPC access can execute

2. **Runner Infrastructure**
   - Runners operate Ethereum archive nodes or use Infura/Alchemy
   - Multiple runners submit results for consensus
   - Implement majority-vote finalization

3. **Actor SDK**
```python
from cowboy_sdk.ethereum import EthereumContract

class MyActor:
    def __init__(self):
        self.usdc = EthereumContract(
            address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            abi=USDC_ABI
        )

    def check_whale_balance(self, address):
        # Submits CIP-2 off-chain task
        balance = self.usdc.call("balanceOf", [address])
        return balance > 1_000_000 * 10**6  # > 1M USDC
```

**Deliverables:**
- CIP-4: Ethereum State Reads via Off-chain Compute
- SDK implementation of `EthereumContract` wrapper
- Reference runner implementation with Ethereum RPC

### Phase 2: Ethereum Transaction Submission (Months 3-6)

**Goal:** Enable Cowboy actors to submit Ethereum transactions

**Components:**
1. **Transaction Construction in Actors**
   - Actors build unsigned Ethereum transactions
   - Submit to runners as CIP-2 tasks

2. **Runner-Managed Key Signing**
   - Runners operate hot wallets
   - Sign and broadcast Ethereum transactions
   - Return transaction hash to actor

3. **Escrow and Payment**
   - Actor locks CBY payment for runner
   - Runner proves transaction inclusion (tx receipt)
   - Payment released on proof

**Example:**
```python
from cowboy_sdk.ethereum import EthereumTransaction

class NFTBidBot:
    def on_timer(self):
        # Build Ethereum transaction
        tx = EthereumTransaction(
            to="0x...",  # NFT marketplace
            data=encode_function_call("bid", [token_id, price]),
            value=1_000_000_000_000_000_000,  # 1 ETH
            max_gas=500_000
        )

        # Submit to runners (CIP-2)
        tx_hash = submit_ethereum_tx(
            tx=tx,
            payment=100_000_000,  # Pay runner 100 CBY
            num_runners=1
        )

        # Store tx_hash for later verification
        set_storage("pending_tx", tx_hash)
```

**Deliverables:**
- CIP-5: Ethereum Transaction Submission via Runners
- Transaction escrow contract
- Runner transaction submission service

### Phase 3: Light Client Verification (Months 6-12)

**Goal:** Add trustless option via cryptographic proofs

**Components:**
1. **Beacon Chain Light Client**
   - Embed Ethereum consensus light client in Cowboy
   - Track finalized beacon blocks
   - Expose state roots to actors

2. **Merkle Proof Generation Service**
   - Off-chain service generates Ethereum state proofs
   - Actors can request proofs for critical operations
   - Proof verification as Cowboy host function

3. **Dual-Mode SDK**
   - Actors specify `verified=True` for proof-based reads
   - Automatic proof generation and verification

**Example:**
```python
from cowboy_sdk.ethereum import eth_call_verified

class HighValueVault:
    def withdraw(self, amount):
        # For large amounts, use cryptographic verification
        if amount > 100_000:
            balance = eth_call_verified(
                contract=self.collateral_address,
                method="balanceOf",
                args=[self.address],
                eth_block=get_latest_finalized_block()
            )
        else:
            # For small amounts, oracle is fine
            balance = eth_call(...)
```

**Deliverables:**
- CIP-6: Light Client Verification for Ethereum State
- Beacon chain light client integration
- Proof generation infrastructure

### Phase 4: Bidirectional Bridges (Months 12-18)

**Goal:** Enable asset movement between Ethereum and Cowboy

**Components:**
1. **Ethereum → Cowboy Bridge**
   - Lock assets in Ethereum escrow contract
   - Mint wrapped assets in Cowboy (using CIP-20)
   - Light client proof of Ethereum lock

2. **Cowboy → Ethereum Bridge**
   - Burn wrapped assets in Cowboy
   - Prove burn to Ethereum (via Cowboy light client on Ethereum)
   - Release locked assets from Ethereum escrow

3. **Standard Wrapped Asset Pattern**
   - CIP-20 tokens representing Ethereum assets
   - Metadata linking to Ethereum origin
   - Redemption mechanism

**Example:**
```python
from cowboy_sdk.bridge import bridge_from_ethereum

class BridgeActor:
    def handle_ethereum_deposit(self, eth_tx_hash):
        # Verify Ethereum lock event with proof
        deposit = verify_ethereum_event(
            tx_hash=eth_tx_hash,
            event="Deposit",
            expected_fields={"token": USDC, "amount": int, "recipient": address}
        )

        # Mint wrapped USDC on Cowboy
        wrapped_usdc = get_actor("wrapped_usdc_cip20")
        wrapped_usdc.mint(deposit.recipient, deposit.amount)
```

**Deliverables:**
- CIP-7: Ethereum-Cowboy Asset Bridge
- Ethereum escrow contracts
- Cowboy bridge actor implementation
- Wrapped asset standard

---

## Technical Deep Dive: CIP-2 Oracle Implementation

Since the **hybrid approach** is recommended and Phase 1 uses CIP-2, here's how Ethereum reads work in detail:

### Task Lifecycle

1. **Actor Request**
```python
# Actor code
result = eth_call(
    contract="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    method="balanceOf",
    args=["0x742d35Cc6634C0532925a3b844Bc454e4438f44e"],
    eth_block=19000000,
    num_runners=3
)
```

2. **Task Submission** (via CIP-2 Dispatcher)
```python
task_id = dispatcher.submit_task(
    task_definition={
        "type": "ethereum_read",
        "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "method": "balanceOf",
        "args": ["0x742d35Cc6634C0532925a3b844Bc454e4438f44e"],
        "block": 19000000,
        "abi": [...],  # Function ABI
    },
    result_schema={
        "max_return_bytes": 32,  # uint256
        "expected_execution_ms": 1000,
        "data_format": "abi_encoded_uint256"
    },
    num_runners=3,
    payment_per_runner=1000000,  # 1 CBY per runner
    timeout_blocks=100
)
```

3. **Runner Execution**
```python
# Off-chain runner code
class EthereumReadRunner:
    def execute_task(self, task):
        # Runner has access to Ethereum RPC (geth, infura, etc.)
        web3 = Web3(Web3.HTTPProvider(self.eth_rpc_url))

        # Execute read at specified block
        contract = web3.eth.contract(
            address=task.contract,
            abi=task.abi
        )

        result = contract.functions[task.method](
            *task.args
        ).call(block_identifier=task.block)

        # Submit result to Cowboy
        self.submit_result(
            task_id=task.id,
            result_data=encode_abi(result),
            execution_time_ms=elapsed_ms
        )
```

4. **Consensus & Callback**
```python
# Cowboy protocol (automatic)
# When 3 runners have submitted:

submissions = [
    {"runner": "0xaaa", "result": "0x0000...1234"},  # 4660
    {"runner": "0xbbb", "result": "0x0000...1234"},  # 4660
    {"runner": "0xccc", "result": "0x0000...1234"}   # 4660
]

# Majority consensus (all agree)
if all_match(submissions):
    trigger_callback(actor, result="0x0000...1234")
else:
    # Slash minority, reward majority (future: slashing)
    handle_dispute(submissions)
```

### Determinism Considerations

**Problem:** Different Ethereum nodes might have different views (reorgs, sync status)

**Solution:**
- Always specify **exact block height** (no "latest")
- Only use **finalized** Ethereum blocks (>= 64 blocks old)
- Runners must verify block is finalized before submitting
- Cowboy consensus waits for Ethereum finality

**Enforcement:**
```python
# In runner validation
if task.block > eth_latest_finalized_block():
    return skip_task()  # Block not finalized yet

# In Cowboy consensus
if task.block > ethereum_finalized_checkpoint:
    return reject_task()  # Too recent, not safe
```

---

## FAQ

### Do Cowboy validators need to run Ethereum nodes?

**No, not in the recommended hybrid approach.** Only the off-chain runners (CIP-2) need Ethereum access. Validators just need to verify runner consensus. In Phase 3, validators would run a light beacon chain client (~10 MB), not a full node (~1 TB).

### How do we prevent Ethereum RPC centralization?

**Multiple strategies:**
1. Require N-of-M runner consensus (e.g., 3-of-5)
2. Economic incentives: Slashing for incorrect data
3. Public runner set: Anyone can become an Ethereum oracle runner
4. Light client proofs (Phase 3): Cryptographic security, no trust needed

### What about Ethereum reorgs?

**Use finalized blocks only.** Post-merge Ethereum has clear finality (~12.8 minutes = 64 blocks). Cowboy tasks should only reference blocks that have reached finality. The protocol enforces this:

```python
MIN_ETH_BLOCK_AGE = 64  # ~12.8 minutes at 12s blocks

def validate_eth_read_task(task):
    eth_current_block = get_eth_current_block()
    if task.eth_block > (eth_current_block - MIN_ETH_BLOCK_AGE):
        raise TaskError("Block not finalized")
```

**Important:** This means there's an inherent **~13 minute delay** for safe Ethereum event processing. Cowboy actors cannot react to Ethereum events in the same block they occur - there will always be latency due to:

1. Ethereum finality requirements (12.8 min)
2. Consensus separation (Cowboy and Ethereum are independent chains)
3. Detection and propagation time (seconds to minutes)

See "Understanding Event Latency" section below for details on fast vs. safe approaches.

### Can actors write to Ethereum?

**Yes, via runners (Phase 2).** Actors construct transactions, runners sign and broadcast. This is more complex and requires:
- Hot wallet management by runners
- Transaction inclusion proofs
- Payment escrow
- Potential multi-sig for high-value operations

### What about gas costs on Ethereum?

**Actors pay runners, runners pay Ethereum gas.** The runner factors Ethereum gas into their pricing. An actor might pay 100 CBY to a runner, who then spends 0.01 ETH in gas to execute the Ethereum transaction.

```python
# Runner pricing calculation
eth_gas_price = web3.eth.gas_price
estimated_gas = 500_000
eth_cost_wei = gas_price * estimated_gas
eth_cost_usd = eth_cost_wei * eth_price_usd / 10**18

# Runner markup (e.g., 20%)
required_payment_cby = eth_cost_usd * 1.2 / cby_price_usd

if task.payment_per_runner < required_payment_cby:
    skip_task()  # Not profitable
```

### Why not just use Chainlink or another oracle?

**Cowboy's approach is more composable with existing infrastructure (CIP-2).** Rather than integrating yet another oracle network, we use the same runner infrastructure for both AI compute and Ethereum reads. Actors can also request light client proofs when they need stronger guarantees.

---

## Understanding Event Latency: The Fundamental Constraint

A critical architectural reality: **Cowboy cannot react to Ethereum events in the same block they occur.** There will always be latency. This section explains why and how to handle it.

### Why There's Always a Delay

Cowboy is an **independent L1** that watches Ethereum, not an Ethereum L2. This creates fundamental constraints:

```
┌─────────────────┐
│  Ethereum L1    │ ← Source of truth
│  (independent   │   Runs own consensus
│   consensus)    │   Block N: Transfer event occurs
└────────┬────────┘
         │
         │ Events flow one direction →
         │ (with inherent delay)
         ↓
┌─────────────────┐
│  Cowboy L1      │ ← Observer/reactor
│  (independent   │   Block M: Processes event
│   consensus)    │   M always comes after N
└─────────────────┘
```

**The timeline for a safe Ethereum event:**

```
12:00:00  - Ethereum block N mined (Transfer event occurs)
12:12:48  - Ethereum block N+64 finalized (can't be reorged)
12:12:53  - Cowboy runners detect finalized event
12:12:58  - Runners submit to Cowboy with 2/3 consensus
12:13:00  - Cowboy block M includes event, actor callback fires

Total latency: ~13 minutes
```

### The Fast vs. Safe Tradeoff

You must choose between speed and safety:

#### Option A: Fast but Risky (~15-20 seconds)

Accept **unfinalized** Ethereum blocks:

```python
class FastEthereumWatcher(Actor):
    """Reacts to unfinalized events - use for low-value operations only"""

    def on_eth_transfer_fast(self, event, eth_block_height):
        # This fires ~20 seconds after Ethereum event
        # BUT: Event might get reorged!

        if event.value > 100e18:
            # React quickly
            self.send_alert("Potential whale deposit: {event.value}")

            # Schedule verification after finality
            set_timer(
                blocks=6000,  # ~13 minutes
                handler="verify_event_finalized",
                data={"event_id": event.id, "eth_block": eth_block_height}
            )

    def verify_event_finalized(self, event_id, eth_block):
        # Verify the event is still there after finality
        finalized_events = eth_get_finalized_events(
            from_block=eth_block,
            to_block=eth_block
        )

        if event_id not in finalized_events:
            # Event was reorged! Roll back
            self.send_alert(f"FALSE ALARM - event {event_id} was reorged")
            self.undo_provisional_action(event_id)
        else:
            # Confirmed! Finalize the action
            self.confirm_provisional_action(event_id)
```

**When to use fast mode:**
- Low-value alerts/notifications
- Speculative trading (you accept reorg risk)
- UX where speed > correctness
- Operations with built-in rollback mechanisms

**Risks:**
- Ethereum reorg → event disappears
- Actor made decision on false data
- Potential losses if funds were moved

#### Option B: Safe but Slow (~13 minutes)

Wait for **Ethereum finality**:

```python
class SafeEthereumWatcher(Actor):
    """Only processes finalized events - use for high-value operations"""

    def on_eth_transfer_safe(self, event, eth_block_height, finalized=True):
        # This fires ~13 minutes after Ethereum event
        # BUT: Event is guaranteed final, can't be reorged

        if not finalized:
            raise Error("Only process finalized Ethereum events")

        if event.value > 100e18:
            # React with confidence - this is final
            self.execute_high_value_logic(event)
```

**When to use safe mode:**
- Bridge deposits (MUST wait for finality)
- High-value trades/transfers
- Governance actions
- Anything where correctness > speed

**Tradeoffs:**
- ~13 minute latency
- But cryptographically guaranteed to be correct

### Watchtower Use Case: Safe Mode is Correct

For the Watchtower data registry (SEC filings, governance proposals, protocol upgrades), **safe mode is the right choice:**

```python
class WatchtowerGovernanceIngester(IntervalActor):
    """Ingests Ethereum governance data for Watchtower registry"""

    def ingest_uniswap_proposals(self):
        # Check for finalized proposals every hour
        # ~13 minute latency is acceptable for governance data

        proposals = eth_get_finalized_events(
            contract=UNISWAP_GOVERNOR,
            event="ProposalCreated",
            from_block=self.last_block,
            finalized=True  # MUST be finalized
        )

        for proposal in proposals:
            # Store in Watchtower registry
            watchtower = get_actor("watchtower_registry")
            watchtower.store_versioned_data(
                key=f"uniswap.governance.proposal.{proposal.id}",
                data={
                    "proposer": proposal.proposer,
                    "targets": proposal.targets,
                    "description": proposal.description,
                    "start_block": proposal.startBlock,
                    "end_block": proposal.endBlock
                },
                ethereum_block=proposal.blockNumber,
                timestamp=block_timestamp()
            )
```

**Why safe mode for Watchtower:**
- Data changes slowly (governance proposals, SEC filings)
- Correctness is paramount (can't have false data in registry)
- 13 minute delay is acceptable (these aren't time-sensitive)
- Downstream actors depend on accuracy

### MEV/Trading Use Case: Fast Mode with Rollback

For MEV-sensitive operations, you might choose fast mode:

```python
class MEVArbitrageBot(Actor):
    """Reacts quickly to Ethereum price changes - accepts reorg risk"""

    def on_uniswap_swap_fast(self, event, eth_block_height):
        # React in ~20 seconds (unfinalized)
        # Risk: Event might reorg, losing the arbitrage opportunity

        price_impact = self.calculate_price_impact(event)

        if price_impact > 0.05:  # >5% price movement
            # Execute arbitrage immediately
            arb_tx = self.execute_arbitrage(event)

            # Mark as provisional
            self.set_storage(f"provisional_arb_{arb_tx.id}", {
                "eth_block": eth_block_height,
                "event_id": event.id,
                "profit": arb_tx.estimated_profit
            })

            # Verify after finality
            set_timer(
                blocks=6000,
                handler="verify_arb_still_valid",
                data={"arb_id": arb_tx.id, "eth_block": eth_block_height}
            )
```

**Why fast mode for MEV:**
- Time-sensitive (arbitrage opportunities disappear quickly)
- Can accept some reorg risk (built into profit calculations)
- Rollback mechanism handles false events
- Speed advantage outweighs occasional false positive

### The "Same Block Reaction" is Impossible

**What developers often imagine:**

```python
# Ethereum block N happens
#   ↓ (instant)
# Cowboy block M sees it and reacts in same block
```

**Why this can't work:**

1. **Consensus separation**: Cowboy and Ethereum have independent consensus mechanisms
2. **Finality requirement**: Ethereum blocks aren't final for 12.8 minutes
3. **Physical impossibility**: Cowboy block M is being produced while Ethereum block N is still unfinalized

The only way to react "in the same block" would be to run Cowboy consensus on top of Ethereum consensus - which would make Cowboy an **Ethereum L2**, not an independent L1.

### Validators Running Ethereum Nodes Doesn't Help

Even if every Cowboy validator ran a full Ethereum node, you still can't eliminate the delay:

```
With validators running geth:
12:00:00  - Ethereum block N mined
12:00:02  - Validators' geth sees block N (unfinalized!)
12:00:04  - Cowboy block M could reference it
          ↓
          But it's unsafe! Block N could reorg.
          Still need to wait for finality.
```

**The only benefit of validators running nodes:**

- Slightly lower latency (~15s instead of ~20s)
- No trust assumption on runners
- But still can't bypass finality requirement

**The cost:**

- Every validator needs ~1TB Ethereum node
- Tight coupling between chains
- Not worth it for most use cases

### Watchtower and Real-time Data Considerations

**Watchtower is a versioned data registry** for external data (governance proposals, SEC filings, protocol upgrades). Even though some use cases want "real-time" data, validators running Ethereum full nodes doesn't solve the latency problem.

**Key insight: Validators and runners serve different purposes**

- **Validators**: Validate Cowboy consensus, cannot submit Ethereum transactions
- **Runners**: Execute off-chain tasks, including Ethereum interactions

Even if validators run Ethereum nodes, you still need **runners** to:

1. **Monitor Ethereum events** → feed Watchtower registry
2. **Submit Ethereum transactions** → execute actor commands on Ethereum
3. **Handle MEV-sensitive operations** → require mempool proximity

**For Watchtower specifically:**

| Data Type | Latency Need | Approach | Runner Setup |
|-----------|--------------|----------|--------------|
| Governance proposals | ~13 min OK | Safe (finalized) | RPC endpoint (Infura/Alchemy) |
| SEC filings | ~13 min OK | Safe (finalized) | RPC endpoint |
| Protocol upgrades | ~13 min OK | Safe (finalized) | RPC endpoint |
| Price feeds | ~20 sec needed | Fast (unfinalized + verify) | RPC endpoint |
| DEX trades (MEV) | Sub-second needed | Fast + mempool access | **Full node + mempool** |

**The runner infrastructure scales to the use case:**

- **Basic Watchtower ingestion**: Runners use cheap RPC endpoints, no full nodes needed (~$0-10/month)
- **MEV-sensitive operations**: Specialized runners with full nodes near mining pools, premium fees (~$500/month)

**Runners are not validators** - they're separate infrastructure that anyone can operate. This keeps Cowboy validator requirements minimal while allowing specialized runners to handle high-performance use cases.

**Bottom line for Watchtower**: No validator-run Ethereum nodes needed. Watchtower data changes slowly enough that ~13 minute finalized data ingestion via CIP-2 runners with standard RPC endpoints is the correct architecture.

### Recommended Approach by Use Case

| Use Case | Approach | Latency | Rationale |
|----------|----------|---------|-----------|
| **Watchtower data registry** | Safe (finalized only) | ~13 minutes | Correctness paramount, data changes slowly |
| **Bridge deposits** | Safe (finalized + proofs) | ~13-15 minutes | Security critical, must be trustless |
| **Governance monitoring** | Safe (finalized only) | ~13 minutes | Accuracy required, no time pressure |
| **Price alerts** | Fast (unfinalized + verify) | ~20 seconds | UX benefit, rollback acceptable |
| **MEV/arbitrage** | Fast (unfinalized + rollback) | ~20 seconds | Speed critical, risk priced in |
| **NFT minting** | Safe (finalized only) | ~13 minutes | Prevent double-mints from reorgs |

### Key Takeaway

**The ~13 minute delay for safe Ethereum event processing is not a bug - it's a fundamental constraint of having two independent blockchains.**

You can trade safety for speed (fast mode), but you must handle rollbacks. For most use cases, especially data registries like Watchtower, the safe approach is correct.

---

## Summary

**Recommended Approach:** Hybrid (Option 4)
- **Phase 1:** Ethereum reads via CIP-2 runners (oracle consensus)
- **Phase 2:** Ethereum writes via runner-signed transactions
- **Phase 3:** Light client proofs for high-value operations
- **Phase 4:** Bidirectional asset bridges

**Key Insight:** Cowboy doesn't compete with Ethereum on asset issuance (ERC-721, ERC-1155). Instead, Cowboy provides what Ethereum lacks: autonomous execution, AI compute, and Python-native development—all while orchestrating existing Ethereum assets.

**Next Steps:**
1. Draft CIP-4: Ethereum State Reads
2. Extend CIP-2 task types to include `ethereum_read`
3. Implement `EthereumContract` wrapper in Cowboy SDK
4. Deploy reference runner with Ethereum RPC support
