# Cowboy docs and examples

Source for [docs.cowboy.inc](https://docs.cowboy.inc) and the example actors
that go with it. Cowboy is a Layer-1 for autonomous agents: Python actors run
in a metered VM on validators, keep persistent state, schedule their own work,
and call off-chain services (LLMs, HTTP, MCP, containers) through runners.

| Directory | What it is |
|---|---|
| `docs/` | The documentation site (Mintlify). Start at `docs/getting-started/quickstart.mdx`. |
| `examples/core/` | The numbered builder curriculum: one actor per lesson, in order. |
| `examples/gallery/` | Product-shaped demos that combine the core patterns. |
| `examples/README.md` | Which lesson teaches what, and how to run the sweep. |

## Using the examples

Install the CLI, then run any lesson against the hosted devnet:

```bash
brew install cowboyinc/tap/cowboy
cowboy version
```

Each lesson directory has its own README with the exact commands. The runtime
(node, runner, storage, secrets) is not in this repository; the CLI and the
hosted network are the interface.

## How this repository is maintained

This tree is promoted from Cowboy's private working repository on a regular
cadence, as a snapshot without history. `PROMOTED_FROM` records the source
commit. Corrections are welcome as issues or pull requests here; accepted
changes are applied at the source and arrive with the next promotion.
