# SWE-chat V4 — Failure-Signal Trigger Precision

## 1. Trigger definition

Three conditions must all hold for the trigger to fire:

1. `prompt_intent == "debug"` — the developer's current turn has debug intent
2. `prev_bash_count >= 2` — the agent's preceding turn included ≥2 bash calls
3. `cum_failure_count >= 1` — at least one failure report has already occurred in this session

When all three hold, Hedwig fires a `FULL_CHECKIN` before the agent's next action.

## 2. Headline numbers

| Metric | Value |
|---|---|
| Turns where trigger fires | 1,402 (2.2% of all turns) |
| Of those, turns with a successor | 1,317 |
| Next turn was a failure report | 311 |
| **Precision** | **23.6%** |
| Baseline failure-report rate | 7.0% |
| **Lift** | **3.37×** |

**The trigger correctly pre-empts a failure report 23.6% of the time — 3.4× better than random.** It fires on 2.2% of all turns, so it's not a constant nuisance.

As a secondary metric: when the trigger fires, the next turn is *any* pushback 55.6% of the time (vs 38.3% baseline, 1.45× lift). So even when the next turn isn't specifically a failure report, it's often a correction or other pushback moment — the trigger is capturing genuine friction, not random turns.

## 3. Stratified results (active vs. delegating sessions)

| Session intensity | n trigger-match turns | Precision | Lift |
|---|---|---|---|
| Active (long, tool-heavy) | 1,142 | 23.0% | 3.29× |
| Delegating (short, low-tool) | 175 | 27.4% | 3.91× |

Precision is slightly *higher* in delegating sessions, which is counterintuitive but interpretable: when a low-engagement developer hits the debug+bash+failure pattern, it's a stronger signal than when a deeply-engaged developer does (because engaged developers work through more varied patterns, so the signal is noisier).

## 4. False-positive breakdown

When the trigger fires but the next turn is NOT a failure report (1,006 of 1,317 cases):

| Next turn type | Count | % of false positives |
|---|---|---|
| non_pushback (clean approval) | 438 | 43.5% |
| correction | 411 | 40.9% |
| rejection | 10 | 1.0% |
| takeover | 5 | 0.5% |

**Useful framing:** 40.9% of false positives are corrections — moments where the developer *would have* corrected the agent anyway. A check-in there isn't wasted; it surfaces the correction before the agent goes further. Only 43.5% are truly clean approvals where the check-in was unnecessary.

So the "true" false-positive rate (where the check-in was genuinely unwanted) is closer to 43.5% of the 76.4% of cases where precision fails = ~33% of all trigger fires are genuinely unnecessary. 67% of the time the trigger fires, something worth pausing for is happening.

## 5. Tighter variants considered

| Variant | n | Precision | Lift |
|---|---|---|---|
| bash≥2, fail≥1 (current) | 1,317 | 23.6% | 3.37× |
| bash≥3, fail≥1 | 977 | 22.7% | 3.24× |
| bash≥2, fail≥2 | 871 | 24.8% | 3.54× |

Tightening the trigger reduces coverage without meaningfully improving precision. The current threshold is near-optimal.

## 6. Recommendation

**Ship as-is.**

23.6% precision at 3.37× baseline lift is a strong empirically-grounded check-in trigger. For context:

- A random check-in fires at baseline rate (7.0% chance of pre-empting a failure)
- This trigger fires at 23.6% — more than 3× better
- It activates on only 2.2% of all turns, so it's not intrusive
- When it fires "unnecessarily," 40.9% of those cases are corrections anyway

**Demo statement:** *"Our failure-signal check-in trigger pre-empts developer failure reports at 3.4× the rate of a random check-in, validated against 62K real developer-agent turns from SWE-chat. It fires on 2.2% of turns — targeted enough to be useful, rare enough not to be annoying."*

## 7. Data

All numerical outputs: `data/trigger_precision.json`.
