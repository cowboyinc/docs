# Cowboy Examples

This directory is split into two tracks:

- [core](./core/) - the default new-developer builder curriculum.
- [gallery](./gallery/) - applied demos that combine core patterns in product-shaped examples.

## Project Layout And Local Simulation

Every core lesson is a `cowboy init` project: `cowboy.toml`, one target per
`actors/<name>/main.py`, and `tests/` driving the actor through
`cowboy_sdk.testing.SimulatedChain`. From a lesson directory:

```bash
uv run pytest                          # SimulatedChain tests, no node
cowboy actor check <target>            # compile the target offline
cowboy test --target <target> --handler <h> --payload 0x.. --expect 0x..
./demo.sh                              # deploy the targets to a network and walk the lesson
```

`cowboy test` and `cowboy dev` run a handler on the simulated PVM through
`cowboy-dev-runner` (`cargo build --release -p cowboy-dev-runner` in a node
checkout; set `COWBOY_DEV_RUNNER` if it is not on `PATH`). The tests import
`cowboy_sdk` from a node checkout beside this repository until `cowboy-sdk`
0.1.2 is on PyPI; set `COWBOY_SDK_PATH` to point elsewhere (see
`_sdk_path.py`). `./check_projects.sh [--pytest]` runs the checks over every
project.

`demo.sh` deploys by target name (`cowboy actor deploy <target>`), which
checks the node's actor runtime profile: use the CLI release that matches
the network you target.

## Project Layout And Local Simulation

Every core lesson is a `cowboy init` project: `cowboy.toml`, one target per
`actors/<name>/main.py`, and `tests/` driving the actor through
`cowboy_sdk.testing.SimulatedChain`. From a lesson directory:

```bash
uv run pytest                          # SimulatedChain tests, no node
cowboy actor check <target>            # compile the target offline
cowboy test --target <target> --handler <h> --payload 0x.. --expect 0x..
./demo.sh                              # deploy the targets to a network and walk the lesson
```

`cowboy test` and `cowboy dev` run a handler on the simulated PVM through
`cowboy-dev-runner` (`cargo build --release -p cowboy-dev-runner` in a node
checkout; set `COWBOY_DEV_RUNNER` if it is not on `PATH`). The tests import
`cowboy_sdk` from a node checkout beside this repository until `cowboy-sdk`
0.1.2 is on PyPI; set `COWBOY_SDK_PATH` to point elsewhere (see
`_sdk_path.py`). `./check_projects.sh [--pytest]` runs the checks over every
project.

`demo.sh` deploys by target name (`cowboy actor deploy <target>`), which
checks the node's actor runtime profile: use the CLI release that matches
the network you target.

## Fast Path

If you want to build something interesting quickly, run these first:

| Step | Lesson | What You Build |
|---:|---|---|
| 1 | [00-quickstart-hello](./core/00-quickstart-hello/) | A deployed actor you can initialize, call, and read |
| 2 | [01-state-events-queries](./core/01-state-events-queries/) | A stateful actor with events and query handlers |
| 3 | [08-minimal-runner-continuation](./core/08-minimal-runner-continuation/) | The smallest async runner callback workflow |
| 4 | [09-runner-llm](./core/09-runner-llm/) | An actor that submits an LLM job and stores the response |
| 5 | Pick a gallery starter | Try [contract-summarizer](./gallery/contract-summarizer/), [spam-detection](./gallery/spam-detection/), or [dao-copilot](./gallery/dao-copilot/) |

## Full Builder Path

The default sweep runs this full curriculum.

| Lesson | What You Build | Main Concept | Prerequisite | Runner | Tokens | Typical Runtime |
|---|---|---|---:|---:|---:|---:|
| [00-quickstart-hello](./core/00-quickstart-hello/) | One deployable actor | deploy, init, execute, read | none | no | no | short |
| [01-state-events-queries](./core/01-state-events-queries/) | Stateful counter | storage, events, queries | 00 | no | no | short |
| [02-senders-and-permissions](./core/02-senders-and-permissions/) | Role-gated writer | caller identity, owner checks | 01 | no | no | short |
| [03-two-actor-messaging](./core/03-two-actor-messaging/) | Sender and worker actors | deferred actor messaging | 01 | no | no | short |
| [04-multi-actor-workflow](./core/04-multi-actor-workflow/) | Job, validation, settlement | actor composition | 03 | no | no | medium |
| [05-tokens-and-balances](./core/05-tokens-and-balances/) | CIP-20 token operations | create, transfer, approve, mint, burn | 01 | no | yes | medium |
| [06-token-escrow](./core/06-token-escrow/) | Custody, release, refund | token-backed state machines | 04, 05 | no | yes | medium |
| [07-timers-and-automation](./core/07-timers-and-automation/) | Scheduled counter | autonomous timer work | 01 | no | no | short |
| [08-minimal-runner-continuation](./core/08-minimal-runner-continuation/) | Async callback state | runner continuation mechanics | 01 | HTTP | no | medium |
| [09-runner-llm](./core/09-runner-llm/) | LLM-backed actor response | prompts, capture, result extraction | 08 | LLM | no | medium |
| [10-async-cache-refresh](./core/10-async-cache-refresh/) | Cached external data refresh | async refresh with cheap reads | 08 | HTTP | no | medium |
| [11-failure-recovery](./core/11-failure-recovery/) | Recoverable request lifecycle | pending, failed, retried, recovered | 08 | HTTP | no | medium |
| [12-capstone-agent](./core/12-capstone-agent/) | Proposal review copilot | state, permissions, runner.llm | 02, 09 | LLM | no | medium |

## Runner Path

Use the runner lessons in order:

1. [08-minimal-runner-continuation](./core/08-minimal-runner-continuation/) teaches continuation mechanics with a small HTTP-shaped task: request state, generated resume entrypoint, result normalization, and status reads.
2. [09-runner-llm](./core/09-runner-llm/) builds on that with `runner.llm(...)`: prompt construction, system prompt, `capture()` across the await boundary, model response extraction, and indexed response storage.
3. [10-async-cache-refresh](./core/10-async-cache-refresh/) and [11-failure-recovery](./core/11-failure-recovery/) show production patterns around reuse and operational state.
4. [12-capstone-agent](./core/12-capstone-agent/) combines LLM review with permissions and a complete proposal workflow.

## Gallery

Gallery examples are product-shaped demos. They are useful after the core patterns are familiar.

### Trading And DeFi

| Example | Description |
|---|---|
| [rebalancing-agent](./gallery/rebalancing-agent/) | Deterministic portfolio drift analysis |
| [liquidity-pools](./gallery/liquidity-pools/) | Token-backed AMM actor |
| [copy-trading](./gallery/copy-trading/) | Disclosure and wallet-copying strategy actors |

### Security And Compliance

| Example | Description |
|---|---|
| [spam-detection](./gallery/spam-detection/) | LLM-backed spam classification |
| [content-moderator](./gallery/content-moderator/) | Moderation decisions and review state |
| [compliance](./gallery/compliance/) | Sanctions screening workflow |
| [fraud-watcher](./gallery/fraud-watcher/) | Transaction risk classification |
| [contract-summarizer](./gallery/contract-summarizer/) | Contract source analysis |

### Content And AI

| Example | Description |
|---|---|
| [content-recommendations](./gallery/content-recommendations/) | Recommendation ranking |
| [sentiment-analysis](./gallery/sentiment-analysis/) | Topic sentiment reports |
| [audio-transcription](./gallery/audio-transcription/) | Transcription request workflow |
| [image-generation](./gallery/image-generation/) | Image prompt request workflow |

### Coordination And Markets

| Example | Description |
|---|---|
| [advanced-messaging-ring](./gallery/advanced-messaging-ring/) | Ring-shaped actor messaging visualization |
| [alerts-and-schedulers](./gallery/alerts-and-schedulers/) | Scheduled checks plus runner analysis |
| [price-watcher](./gallery/price-watcher/) | Price-drop watcher with a two-way Telegram bridge |
| [liquidation-oracle](./gallery/liquidation-oracle/) | Lending account liquidation analysis |
| [dao-copilot](./gallery/dao-copilot/) | Governance proposal analysis |
| [hn-feed](./gallery/hn-feed/) | CIP-7 open stream starter with deterministic ingest |
| [watchtower](./gallery/watchtower/) | Canonical CIP-7 Watchtower Form 4 reference |
| [casino-rounds](./gallery/casino-rounds/) | Token-backed prediction rounds |
| [blackjack](./gallery/blackjack/) | Token-backed blackjack table |
| [cowbot](./gallery/cowbot/) | Experimental Slack bot with CBFS and runner-agent prerequisites |

### Runtime Comparisons

| Example | Description |
|---|---|
| [erlangcowboydemo](./gallery/erlangcowboydemo/) | Erlang actor-model comparison demo |

Gallery examples can be heavier than the core curriculum. Non-standard run paths
are acceptable when the README states the status, prerequisites, and smoke
command.

## Running Examples

```bash
# Default: run the full core builder curriculum against mesa
./run_examples.sh

# Run one core lesson by id, slug, or path
./run_examples.sh 00
./run_examples.sh quickstart-hello
./run_examples.sh 03-two-actor-messaging
./run_examples.sh runner-llm
./run_examples.sh core/08-minimal-runner-continuation
./run_examples.sh 12-capstone-agent

# Run gallery examples only
./run_examples.sh --gallery advanced-messaging-ring
./run_examples.sh --gallery copy-trading

# Run both tracks
./run_examples.sh --all

# Start a temporary validator, then run selected examples
./run_examples.sh --spawn-local 09-runner-llm

# Reuse an existing validator
./run_examples.sh --server local
./run_examples.sh --server http://localhost:4000 core/00-quickstart-hello

# Show full Cowboy CLI output
./run_examples.sh --verbose core/04-multi-actor-workflow
```

Targeting behavior:

- `--server mesa` uses `${MESA_RPC_URL:-https://rpc.mesa.cowboylabs.net}`
- `--server canyon` uses `${CANYON_RPC_URL:-https://rpc.canyon.cowboylabs.net}`
- `--server local` uses `${LOCAL_RPC_URL:-http://localhost:${RPC_PORT:-5000}}`
- `--spawn-local` starts an isolated temporary local validator for the run
- `--gallery` switches the sweep to gallery examples
- `--all` runs both core and gallery examples
- `--verbose` prints the full raw Cowboy CLI output instead of the compact command/result view

## Contributing

Add new teaching examples to `core` only when they introduce a distinct concept in the builder curriculum. Add product-shaped combinations of existing concepts to `gallery`.

Each example should include:

1. `README.md`
2. actor implementation file or files
3. `demo.sh` or another explicit runnable entrypoint
4. caveats for runner, token, network, or timing requirements
