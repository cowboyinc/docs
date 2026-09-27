Current Failures

What has actually gone wrong, in the order it matters. Each entry gives the mechanism, the evidence, and whether it is fixed.


1. RUNNER HEARTBEAT LOCKOUT — the one that kept ending runs
Status: root cause fixed, awaiting production proof

This is what stopped trading in every long run. It is deceptive because the chain stays perfectly healthy while the application dies.

The mechanism is a chain of five links. Transactions wedge in the mempool and push the next-expected nonce beyond the admission ceiling. Admission then requires a nonce at or above that ceiling and simultaneously below it, so no nonce is admissible at all. Runners cannot submit heartbeats, go stale past the timeout, and drop out of job dispatch. Jobs continue to be created and are assigned to nobody. Every actor continuation then starves, and the book freezes while blocks keep being produced normally.

We shipped age-based mempool eviction to break this, and it never fired: zero evictions across three hours with about ninety-five transactions wedged and all three runners pinned at the ceiling. The reason turned out to be circular. Eviction measured transaction age from a timestamp that is cleared when a transaction is popped for a proposal and re-stamped as new when a cancelled proposal restores it. A wedged mempool cancels proposals every few seconds, so every restore reset the age of exactly the transactions the eviction existed to remove. The deadlock was continuously refreshing the clock of the mechanism designed to break it.

Fixed by tracking a separate first-admission timestamp that survives pop and restore cycles. The regression test fails against the old behaviour and passes with the fix. It has never run in production, so the correct posture is expectation rather than confidence.


2. BUILDER-DEX ILLIQUIDITY — the one that wasted the most time
Status: fixed, awaiting production proof

Equity perps on the builder-deployed exchange have no resting liquidity. We observed thirty-four consecutive refusals on equity symbols while every core crypto order filled, including orders priced one to two percent through the mark, which rules out pricing as the cause.

The compounding problem was that research had no way to represent "cannot be executed." A refused order never becomes a position, so it never appears as a winner or a loser, and synthesis kept proposing the same assets indefinitely. During one eight and a half hour window every rebalance planned only unfillable equities, and the system opened nothing while reporting success at every level.

Fixed by tracking per-symbol execution feasibility, suppressing new positions in repeatedly-refused assets while never suppressing exits, and feeding an unfillable list back into research so it stops proposing them.


3. SILENT FAILURES — the pattern behind most lost time
Status: instrumented

Almost every hard-to-diagnose hour traced to a failure that looked identical to success. A refused order and a zero fill returned the same value. A rebalance that planned nothing completed normally and incremented its counter. A dead timer loop left no trace. A hung continuation set a flag and never cleared it, with no error anywhere.

The specific case that cost the most: order responses come back in two different refusal shapes, one nested inside a status array and one at the top level, and the parser only checked the first. So a duplicate-nonce rejection was scored as a successful zero-fill.

Each of these now emits an event. This is not a fix so much as making the next occurrence diagnosable in minutes instead of hours.


4. CONSENSUS LIVELOCK — fixed and proven
Status: fixed, validated over twenty-four hours

Block production cost about 2.1 seconds against a 2-second deadline, so proposals were cancelled every round. The adaptive controller responded by halving the transaction budget, but that budget governs only about six percent of the cycle cost, so it drove itself to the floor while the deadline was still missed. The chain re-executed a single height roughly a thousand times without ever committing.

The documented mitigation is to raise the leader timeout, but it was capped by a hardcoded certification timeout that made any larger value panic at boot. Fixed by making the certification timeout configurable and clamping the pair rather than aborting. Validated over a twenty-four hour run with zero deadline misses.


5. BLOCK RATE DECAY — open, cause known
Status: open

Block production decays from roughly 0.76 to 0.27 blocks per second over about ten hours, then plateaus and holds. Profiling attributes most of it to a fixed per-block decompression cost of around 900 milliseconds, with a quarter of total CPU spent constructing a decompression context that should be reused.

Because every cadence, timeout and guard is expressed in blocks rather than wall time, the decay stretches all of them by roughly a factor of three. A rebalance every 26 minutes becomes every 74 minutes. A 20-minute recovery guard becomes 55 minutes. This does not break anything on its own now that the deadline has headroom, but it makes the demo progressively less lively and makes every recovery mechanism slower exactly when it is most needed.

A first fix attempt introduced a data-corruption bug by changing decode semantics, and a follow-up patch addressed that specific issue but a bootstrap test still fails. It was never diagnosed because the codebase cannot be built on macOS. The next step is a single continuous-integration run on Linux to read the actual failure.


6. LEARNING FROM NOISE — open, partially addressed
Status: open

The system was drawing roughly one confident lesson per closed trade, several stated as causal rules from a single observation. Confidence gating and market-relative attribution now address the measurement side, but the underlying issue is time horizon: at twenty to sixty minute holds, the outcome of any single decision is dominated by market drift rather than by the decision itself. More data does not straightforwardly produce better rules when the signal is mostly noise.

There is also an asymmetry we have not fixed. Only losing theses are retired and archived, and only archived theses become lessons. Winning theses stay active and are never distilled, so the system learns exclusively from failure and has no mechanism to reinforce what worked.


7. OPERATIONAL SELF-INFLICTED FAILURES
Status: fixed, worth recording

Several hours were lost to tooling that reported success while doing nothing. The recovery watchdog was installed and inert for its first hour because a state file parsing bug made it compute the book as stalled for fifty-six years, and the arithmetic error that resulted was the only thing preventing it from firing constantly. It also ran with a key that was not authorized to call the handlers it needed, and it logged success unconditionally regardless of the outcome.

Separately, the environment teardown command reported "torn down" when passed a flag in the wrong form, having torn down nothing.

The lesson recorded from this: verify by behaviour, never by exit code, and treat "the component that reports problems" as itself needing a test.
