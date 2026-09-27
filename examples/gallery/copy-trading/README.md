# Copy Trading

> Autonomous agents that mirror trades from wallets or public disclosures

## Use Case

Copy trading lets you automatically follow the trades of others. This example demonstrates two approaches:

1. **Wallet Copy Trading** (`actors/wallet/main.py`) - Monitor on-chain wallet activity and mirror swaps
2. **Disclosure Copy Trading** (`actors/disclosure/main.py`) - Follow Congressional stock disclosures (PTR reports)

Both use cases are **90% off-chain data gathering** and **10% on-chain execution**, making them perfect for Cowboy's actor model.

## Why Ethereum Alone Doesn't Work

Ethereum contracts cannot access the internet:

```solidity
// This doesn't exist in Solidity
contract CopyTrader {
    function checkWallet() public {
        // How do I fetch blockchain data?
        // How do I scrape websites?
        // How do I parse PDFs?
        // IMPOSSIBLE!
    }
}
```

Traditional solutions require complex infrastructure:
- AWS/GCP servers ($50-200/month)
- RPC providers like Alchemy ($100-500/month)
- Oracle services like Chainlink ($10-50/call)
- Monitoring and alerting ($20-50/month)
- Hundreds of lines of glue code

**With Cowboy**: 30-40 lines of Python, deploy once, runs forever.

## How It Works

### Wallet Copy Trading

```
┌──────────────────────────────────────────────────────────────────┐
│                    Copy Trading Agent                             │
│                                                                   │
│  Every 5 minutes:                                                 │
│  1. Submit task to blockchain indexers (5 runners)               │
│  2. Indexers scan: Ethereum, Base, Arbitrum                      │
│  3. Detect swaps from target wallet                              │
│  4. Return consensus results (3/5 must agree)                    │
│                                                                   │
│  For each detected trade:                                         │
│  5. Check if trade size >= minimum                                │
│  6. Apply slippage protection (2%)                                │
│  7. Execute same swap on our DEX                                  │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 │ send_message("swap", {...})
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│                          DEX Pool                                 │
└──────────────────────────────────────────────────────────────────┘
```

### Disclosure Copy Trading

```
┌──────────────────────────────────────────────────────────────────┐
│                Congressional Copy Trader                          │
│                                                                   │
│  Every 24 hours:                                                  │
│  1. Submit task to web scrapers (5 runners)                      │
│  2. Scrapers fetch: House Clerk, Senate, Unusual Whales          │
│  3. Parse PDF filings (often scanned, need OCR)                  │
│  4. Return consensus results (3/5 must agree)                    │
│                                                                   │
│  For each new filing:                                             │
│  5. Submit to AI for analysis (3 runners)                        │
│  6. AI extracts: ticker, side, size, confidence                  │
│  7. SDK verifies consensus (2/3 must agree)                      │
│  8. Adjust position size by confidence                           │
│  9. Execute trade on broker                                       │
└──────────────────────────────────────────────────────────────────┘
```

## Key Features

### Wallet Copy Trading
- **Multi-chain**: Monitors Ethereum, Base, Arbitrum simultaneously
- **Consensus**: 5 indexers, need 3/5 to agree (prevents fake data)
- **Near real-time**: Checks every 5 minutes
- **Customizable**: Minimum trade size, slippage limits

### Disclosure Copy Trading
- **Multi-source**: House Clerk, Senate, Unusual Whales, Capitol Trades
- **PDF parsing**: Handles scanned documents with OCR
- **AI analysis**: Extracts structured data from messy filings
- **Confidence scoring**: Positions sized by AI confidence

## Quick Start

```bash
./demo.sh
```

The runnable walkthrough deploys the `disclosure` target, initializes portfolio
limits, ingests one mock disclosure through `ingest_disclosure`, waits for the
LLM continuation, and reads back `get_last_trade` plus `get_stats`.

## Demo Scope

The runnable demo uses one deterministic mock disclosure and stores the latest
analysis result. It does not monitor wallets, scrape disclosure sites, run a
multi-indexer consensus network, or execute trades unless that behavior is
explicitly shown in `demo.sh`; treat those capabilities as extension points.

## Architecture

### Consensus Pipeline

```
Stage 1: Data Gathering (5 runners)
├── Runner 1: Etherscan API
├── Runner 2: Alchemy RPC
├── Runner 3: Custom indexer
├── Runner 4: TheGraph
└── Runner 5: Backup RPC
    ↓
    PROOF_MAJORITY (3/5 must agree)
    ↓
Stage 2: Policy Enforcement (on-chain)
├── Check minimum size
├── Apply slippage limits
├── Check cooldown periods
└── Record for audit
    ↓
Stage 3: Execution
└── send_message(dex, "swap", {...})
```

### AI Analysis for Disclosures

```python
ctx.result = await runner.llm(
    f"""Analyze this Congressional filing:

{pdf_text}

Return JSON: {{
    "should_trade": true/false,
    "ticker": "NVDA",
    "side": "buy",
    "size_usd": 25000,
    "confidence": 85
}}""",
    system_prompt="Summarize delayed trading disclosures into JSON only.",
    max_tokens=180,
)
```

## Configuration

### Wallet Copy Trader

| Parameter | Description | Default |
|-----------|-------------|---------|
| `target_wallet` | Address to copy | Required |
| `our_dex` | DEX for execution | Required |
| `min_trade_usd` | Minimum trade size | $1,000 |
| `interval_blocks` | Check frequency | 25 (~5 min) |

### Disclosure Copy Trader

| Parameter | Description | Default |
|-----------|-------------|---------|
| `target_member` | Name as in filings | Required |
| `our_broker` | Execution venue | Required |
| `max_position_usd` | Max position size | $10,000 |
| `interval_blocks` | Check frequency | 7200 (~24h) |

## Caveats & Disclaimers

### Wallet Copy Trading

- **Identity risk**: Verify the wallet actually belongs to who you think
- **Frontrunning**: Your trade executes after theirs (worse price)
- **MEV**: Bots may sandwich your trades
- **No endorsement**: Don't claim they endorse this

### Disclosure Copy Trading

- **30-45 day delay**: PTRs filed weeks after the actual trade
- **Late filings**: Many filed 6-12 months late
- **Size ranges only**: "$15k-$50k" not exact amounts
- **No alpha guarantee**: Academic research shows minimal edge after delays
- **Data quality**: PDFs often scanned, contain OCR errors

### General

- **This is risky**: You could lose all your capital
- **Not financial advice**: Educational example only
- **Compliance**: Check your local regulations
- **Privacy**: Respect people's privacy with public data
- **Backtest first**: Simulate before deploying real capital

## Comparison: Web3 vs Web2

| Feature | Wallet (Web3) | Disclosure (Web2) |
|---------|---------------|-------------------|
| Data source | Blockchain | Websites + PDFs |
| Latency | ~5 minutes | 30-45 days |
| Accuracy | High | Medium (OCR errors) |
| Runner task | Query RPC | HTTP + PDF + OCR |
| Alpha potential | Low-medium | Very low |

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/wallet/main.py` - on-chain wallet copy trading
- `actors/disclosure/main.py` - Congressional disclosure copy trading
- `tests/test_wallet.py`, `tests/test_disclosure.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Simulate Locally

The tests drive the actors through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler
on the simulated PVM. Payloads and expected outputs are the handler's wire
bytes, hex-encoded; this call is `get_stats({})` on a fresh disclosure actor
and expects the CBOR-encoded default portfolio limits:

```bash
cowboy test --target disclosure --handler get_stats \
  --payload 0xa0 \
  --expect 0xa46662726f6b6572606b74726164655f636f756e74006d7461726765745f6d656d62657260706d61785f706f736974696f6e5f757364192710
```

`cowboy actor check disclosure` and `cowboy actor check wallet` validate the
targets offline. `cowboy test` and `cowboy dev` need `cowboy-dev-runner` on
`PATH` or at `COWBOY_DEV_RUNNER` (`cargo build --release -p cowboy-dev-runner`
in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the disclosure actor with `cowboy actor deploy disclosure`,
ingests one mock disclosure, and reads back the stored trade suggestion. The
wallet variant deploys the same way with `cowboy actor deploy wallet`. A project deploy
checks the node's actor runtime profile, so use the CLI release that matches
the network you target.
