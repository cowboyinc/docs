# cowboy-browse — Kernel executor app

The **hands** for on-chain agentic browsing. A THIN Kernel app: one Playwright
step per invocation against a PERSISTENT Kernel browser session. No LLM here —
the agent loop (deciding each step) lives in the on-chain assistant via
`runner.llm`. This exists because the current Kernel API drives browsers over a
CDP **websocket**, which the on-chain `runner.http` (one-shot request/response)
cannot hold. So this app runs the CDP/Playwright on Kernel's infra and exposes
one **synchronous** `/invocations` call the runner CAN make.

## Deploy
    export KERNEL_API_KEY=$(cat ~/.cowboy/kernel-key.txt)
    npx @onkernel/cli deploy index.ts -e KERNEL_API_KEY=$KERNEL_API_KEY

## The one action: `step`
`payload` (JSON string) → `{op, session_id?, url?, selector?, text?, key?, dy?}`
- `open`   → `{session_id}` (persistent, survives across steps)
- `goto`   → navigate, returns `{url,title,text,links}`
- `click` / `type` / `press` / `scroll` → act, return new page state
- `extract`→ current `{url,title,text,links}`
- `close`  → release the session

Invoked via `POST https://api.onkernel.com/invocations`
`{app_name:"cowboy-browse", action_name:"step", payload:"<json>"}` — sync,
blocks ≤100s, returns `{output:"<json>", status:"succeeded"}`. PROVEN live
2026-09-03: open→goto→close round-trips returning real page text.

## Why session state matters
Each agent step is a SEPARATE invocation. Kernel holds the browser server-side
(`timeout_seconds`); each step re-fetches a fresh `cdp_ws_url` via
`GET /browsers/{id}` and reconnects. Tested: navigate in one invocation, the
next invocation sees the same page.
