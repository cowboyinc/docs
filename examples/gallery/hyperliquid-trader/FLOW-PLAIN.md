System Flow

How a headline becomes a position, and how the result becomes a lesson.


THE FORWARD PATH — HEADLINE TO POSITION

Step 1. Fetch
Actor: research, ingest loop, every 419 blocks
Six RSS feeds are pulled through a runner and deduplicated against the last 500 URLs.

Step 2. Score
Actor: research, same continuation
Each article is rated 1 to 10 for market relevance by an LLM. Anything below 7 is discarded and the top 8 are kept. The cycle also appends one aggregate row to the sentiment tape.

Step 3. Synthesize
Actor: research, synth loop, every 599 blocks
News, prices, memory and the sentiment trend go into a single LLM call, which returns one thesis (long basket, short basket, conviction) and up to eight ideas. The thesis snapshots every universe price as its baseline.

Step 4. Pull
Actor: trader, rebalance loop, every 1201 blocks
The trader calls research for ideas and filters them by confidence of 60 or higher.

Step 5. Plan
Actor: trader, pure function with no I/O
The target book is diffed against current holdings to produce opens, closes and flips. Orders can be blocked by minimum hold time, flip margin, repeated venue refusal, or minimum notional. Every skipped symbol records a reason.

Step 6. Sign
Runner, off-chain
The order is built with a placeholder signature. CBSS releases the key to the runner under a 4-of-5 quorum, and signing happens inside the runner. The key never exists on-chain.

Step 7. Execute
Hyperliquid
The order is posted and returns one of three outcomes: filled, so the position enters the book; refused for lack of liquidity; or refused because the request itself was rejected. Both refusal types increment the per-symbol feasibility counter.

Step 8. Mark
Actor: trader, mark loop, every 293 blocks
Prices are refreshed and unrealized P&L is recomputed against entry.


THE FEEDBACK PATHS — RESULT TO LESSON

Three loops run continuously, and all three land back at Step 3.

Path A. Realized performance
The book and unfillable record are pushed back to research as traded_performance. This is the most heavily weighted evidence in synthesis.

Path B. Execution feasibility
Rejections accumulate in exec_feasibility, which produces an unfillable list, and those assets are stripped from the candidate set. This teaches the system what it can trade, not just what performed.

Path C. Thesis outcomes
Theses are marked by the validate loop every 1801 blocks, scored as alpha against a benchmark, and archived. The distill loop then compresses them into lessons every 3607 blocks, and those lessons feed back into synthesis. Retirement uses raw return; learning uses alpha.


THE HUMAN LOOP

Outbound: the book flows to the notifier every 1009 blocks, which sends briefs to Telegram.
Inbound: your Telegram reply becomes operator_guidance and is weighed in synthesis. Advice is weighed but can never override the risk limits enforced in code.


THE WHOLE THING IN ONE PARAGRAPH

Research reads the news and forms a view. The trader turns that view into orders, which a runner signs with a threshold-released key and sends to Hyperliquid. Whatever comes back — a fill, a refusal, a profit, a loss — flows back into research as evidence, and the next view is formed with that evidence in hand. Nothing external drives it: each actor schedules itself on-chain, and the only human input is optional advice over Telegram.
