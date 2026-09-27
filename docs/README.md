# Cowboy Public Docs

This directory is the public Mintlify documentation root for Cowboy. Keep it focused on what a reader needs to understand the protocol, build a first actor, and look up reference material.

Internal planning docs, PRDs, memos, open questions, and testing notes belong outside the published tree in `../internal-docs/`.

## Published Structure

The sidebar is configured in `docs.json` and uses four top-level groups:

- **Start Here**: landing page, product overview, quickstart, and FAQ.
- **Build**: the first useful actor and the examples curriculum.
- **How Cowboy Works**: conceptual protocol docs for the VM, fees, timers, off-chain compute, transactions, entitlements, keys, and economics.
- **Reference**: glossary, CIPs, error reference, whitepapers, and Ethereum interoperability.

## Recommended Reading Path

1. [What is Cowboy?](/introduction/what-is-cowboy)
2. [Quickstart Guide](/getting-started/quickstart)
3. [Minimal Actor](/architecture/actor-vm/minimal-actor)
4. [Examples Curriculum](/developers/examples)
5. [Architecture Overview](/overview/architecture)

## Local Preview

```bash
cd cowboy/docs
npx mintlify dev --port 3000
```

The documentation will be available at `http://localhost:3000`.

## Editing Guidelines

- Update `docs.json` when a page should appear in public navigation.
- Do not add internal planning, roadmap, PRD, memo, or testing material to Mintlify navigation.
- Keep examples docs aligned with `../examples/README.md` and `../examples/run_examples.sh`.
- Keep public pages reader-facing; implementation notes that are only useful to maintainers belong in `../internal-docs/`.

## Useful Public Files

- `docs.json`: Mintlify configuration, sidebar, redirects, and site chrome.
- `index.mdx`: public landing page.
- `introduction/what-is-cowboy.mdx`: product and protocol overview.
- `developers/examples.mdx`: builder curriculum and example sweep guide.
- `errors/overview.mdx`: public error reference.

## Internal Docs

Internal testing notes currently live at:

- `../internal-docs/engineering/testing/`
