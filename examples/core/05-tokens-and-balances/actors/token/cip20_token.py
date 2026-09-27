"""
CIP-20: Cowboy Token Standard
==============================

CIP-20 tokens are first-class chain primitives on Cowboy, implemented as
system instructions rather than actor-based contracts. This means:

  - Token operations (create, transfer, approve, mint, burn) are native
    validator instructions — fast, cheap, and atomically enforced.
  - Token state (balances, allowances, metadata) lives in the Token Registry,
    not in per-actor storage.
  - Actors can interact with tokens via runtime host functions
    (runtime.token_*), enabling DeFi-style composability.

Key differences from ERC-20 on Ethereum:
  - No Solidity contract needed — tokens are created with a single system tx.
  - Balances are stored in a global Token Registry, not per-contract storage.
  - The validator enforces all invariants (overflow, underflow, frozen accounts).
  - Actors can call runtime.token_transfer(), runtime.token_approve(), etc.
    from Python code running in the PVM.

CLI usage (from outside the chain):
  cowboy token create --name "My Token" --symbol MTK --decimals 18 --initial-supply 1000000
  cowboy token transfer --token-id <ID> --to <ADDRESS> --amount 500
  cowboy token balance --token-id <ID> --address <ADDRESS>
  cowboy token approve --token-id <ID> --spender <ADDRESS> --amount 100
  cowboy token mint --token-id <ID> --to <ADDRESS> --amount 200
  cowboy token burn --token-id <ID> --amount 50
  cowboy token info --token-id <ID>
  cowboy token list

Actor code usage (inside PVM):
  See main.py for an actor that creates and manages tokens
  using runtime.token_* host functions.
"""

# ============================================================================
# Actor-side token operations (for use inside PVM actor code)
# ============================================================================
#
# When running as an actor on the Cowboy chain, you can interact with
# CIP-20 tokens using the runtime host functions:
#
#   from cowboy_sdk import runtime
#
#   # Create a new token (returns 32-byte token_id)
#   token_id = runtime.token_create(
#       name=b"My Token",
#       symbol=b"MTK",
#       decimals=18,
#       initial_supply=1_000_000,
#       max_supply=None,         # or a u64 cap
#       transfer_hook=None,      # or an actor address for hooks
#       metadata_uri=None,       # or bytes URI
#   )
#
#   # Query
#   balance = runtime.token_balance_of(token_id, account_address)
#   supply  = runtime.token_total_supply(token_id)
#   allowed = runtime.token_allowance(token_id, owner, spender)
#
#   # Mutate
#   runtime.token_transfer(token_id, to_address, amount)
#   runtime.token_approve(token_id, spender_address, amount)
#   runtime.token_transfer_from(token_id, from_address, to_address, amount)
#   runtime.token_mint(token_id, to_address, amount)
#   runtime.token_burn(token_id, amount)
#
# ============================================================================
# System instruction format (for reference)
# ============================================================================
#
# CIP-20 token operations are SystemInstruction variants submitted as
# transactions to the validator:
#
#   TokenCreate { name, symbol, decimals, initial_supply, max_supply,
#                 transfer_hook, metadata_uri }
#   TokenTransfer { token_id, to, amount }
#   TokenTransferFrom { token_id, from, to, amount }
#   TokenApprove { token_id, spender, amount }
#   TokenMint { token_id, to, amount }
#   TokenBurn { token_id, amount }
#   TokenFreeze { token_id, account }
#   TokenUnfreeze { token_id, account }
#   TokenSetHook { token_id, hook }
#   TokenTransferOwnership { token_id, new_owner }
#   TokenTransferBatch { transfers: [(token_id, to, amount), ...] }
#
# Token state is stored in the Token Registry with these key prefixes:
#   mint:{token_id}                    → TokenMint metadata (JSON)
#   bal:{owner}{token_id}              → u64 balance (8 bytes BE)
#   alw:{owner}{spender}{token_id}     → u64 allowance
#   frz:{token_id}{account}            → frozen flag
#
# All addresses are 20-byte Ethereum-style (secp256k1 + keccak256).
# Token IDs are 32-byte identifiers assigned at creation.
# Supply values are u64, so max representable is ~18.4 * 10^18.
#
# ============================================================================
# HTTP API endpoints for token queries
# ============================================================================
#
#   GET /tokens                                  → list all tokens
#   GET /token/{token_id}                        → token metadata
#   GET /token/{token_id}/balance/{address}      → balance query
#   GET /token/{token_id}/allowance/{owner}/{spender} → allowance query
#
