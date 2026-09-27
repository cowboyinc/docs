The Inspect Tool

A read-only snapshot of everything both actors currently believe. It calls every public view handler on the research and trader actors and prints the full contents with no truncation, which makes it the fastest way to answer "what does the system actually think right now?"

Usage:
  RPC=http://<ip>:4000 python3 tools/inspect.py [research|trader|all] [--json]

Actor addresses are read automatically from tools/research.txt and tools/actor.txt, so there is nothing to pass in. Add --json for machine-readable output instead of the sectioned human view.

It only reads. It sends no transactions, spends no gas, and cannot change the state of a running system, so it is always safe to run against a live demo.


WHAT IT SHOWS FROM RESEARCH

get_stats
The pipeline's vital signs: how many times each loop has run (ingest, synth, validate, distill), how many feeds are configured, how many news items are currently in the window, how many theses are active, how many lessons have been learned, the size of the tradeable universe, and the standing mandate. This is the first place to look to confirm the research side is alive and which loops are firing.

get_theses
Every thesis, active and retired, in full. Each carries its statement, the long and short baskets, conviction, the economic context that produced it, the price baseline captured at creation, and its running return. This is the system's current worldview and how it has performed.

get_ideas
The current trade candidates: asset, side, confidence, theme, rationale, and how well each fits the active thesis. Synthesis pushes this set to the trader for its next rebalance.

get_items
The scored news window — the individual articles that survived relevance filtering, with their scores. Useful for seeing what the system is reasoning from.

get_wiki
The accumulated memory: distilled lessons, past cases pairing prior market contexts with their outcomes, and the long-term per-asset track record. This is what the system has learned rather than what it currently thinks.

get_universe
The tradeable asset list with exchange metadata.


WHAT IT SHOWS FROM THE TRADER

get_stats
Whether the actor is running, how many positions are open, how many rebalances have executed, and realized P&L.

get_pnl
The full accounting: budget, equity, realized and unrealized P&L, return percentage, win rate, and counts of open and closed positions.

get_portfolio
Every open position with entry price, current mark, quantity, notional, the confidence it was opened on, the block it was opened at, and its return. Also includes the last-known price for every asset it tracks.

get_scorecard
Per-position returns for open and closed trades, plus the unfillable list — assets the venue has repeatedly refused. This is the same payload research consumes as feedback, so it shows exactly what the learning loop is being told.


HOW TO READ IT

The most useful comparison is between actors. If research shows fresh ideas but the trader shows no new positions, the problem is in execution rather than reasoning. If the trader's scorecard shows an unfillable list, those assets cannot be traded on this venue no matter how attractive research finds them. If research stats show synth runs climbing while thesis count stays flat, deduplication is folding restatements into existing theses rather than creating new ones.

A frozen rebalance count alongside a healthy research pipeline is the signature of the failure mode that has ended most runs: the reasoning side is fine and the execution side has stalled.
