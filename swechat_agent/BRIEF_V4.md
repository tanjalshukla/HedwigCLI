# SWE-chat V4 — failure-signal trigger precision

**Single small analysis.** Estimated time: 30 minutes of agent work.

## What we want to know

Hedwig now ships with a built-in check-in trigger grounded in V2 finding 2:

> Fire a full check-in when the current turn has `task_intent == debug`,
> the agent's preceding turn had `prev_bash_count >= 2`, and the session
> has already had `>= 1` failure report.

We want a precision measurement on this trigger. Specifically:

**For turns in SWE-chat where this trigger *would* fire, what fraction of the
*next* developer turn is actually a failure report?**

If precision is high, we have a demo-worthy number: "our empirically-grounded
check-in trigger correctly pre-empts X% of failure reports on real data."
If precision is low, we need to tighten the trigger before the demo.

## What you have

Already in `data/`:

- `features.parquet` — 62,544 turn-level rows with every feature needed
  (`intent_debug`, `prev_bash_count`, all the cumulative session-state fields)
- `session_features.parquet` — 5,795 session-level rows

You do not need to re-extract anything. Everything fits in memory.

## What to compute

1. **Filter trigger-match rows.** Load `features.parquet`. Keep rows where all
   three conditions hold:
   - `intent_debug == 1`
   - `prev_bash_count >= 2`
   - `cum_failure_count >= 1` (at least one prior failure in the same session)

   Call this set T (trigger would have fired here).

2. **For each row in T, look at the *next* turn in the same session.** Use
   `session_id` + `cum_turn_index` to find the successor. If no successor
   exists (last turn in session), skip.

   Call this set T_next.

3. **Compute precision.** What fraction of T_next turns are failure reports
   (`pushback_type == "failure_report"` in the ground-truth labels)?

   Also compute:
   - Baseline failure-report rate across all turns (should be ~7.5% from V2).
   - Lift = trigger precision / baseline rate.

4. **Stratify by session intensity.** Recompute precision separately for
   "active" sessions (long, tool-heavy) vs. "delegating" sessions. This
   tells us whether the trigger's precision varies by user type.

5. **Report false positives.** For rows in T where the next turn was *not*
   a failure report, look at what it was. If a meaningful fraction were
   some other pushback type (e.g., correction), the trigger is "wrong" but
   not useless — it still pre-empted a pushback moment, just not a failure.
   Report the breakdown.

## Deliverables

```
swechat_agent/
├── data/
│   └── trigger_precision.json      # numerical results
├── docs/
│   └── FINDINGS_V4.md               # short writeup (one page)
└── scripts/
    └── trigger_precision.py
```

`FINDINGS_V4.md` structure:

1. **Trigger definition** (exact conditions, for reproducibility).
2. **Headline numbers.**
   - |T| = number of trigger-match rows
   - Precision on next-turn-failure-report
   - Baseline failure rate
   - Lift
3. **Stratified results** (active vs. delegating).
4. **False-positive breakdown** (when the next turn wasn't a failure, what was it?).
5. **Recommendation** — if precision is strong (>2x lift), ship as-is. If
   precision is weak, recommend tightening (e.g., `prev_bash_count >= 3`,
   or require `cum_failure_count >= 2`).

## Ground rules

1. Do not modify `../sc/`. Pure analysis.
2. Use existing features. No re-extraction.
3. Report honestly. If precision is low, say so. The point of the measurement
   is to inform the demo, not to manufacture a good number.
4. Time budget: 30 min execution. If stuck longer than 1 hour, stop and
   write `docs/V4_BLOCKERS.md`.

## Why this matters

This is the one number we want to be able to cite at the demo when someone
asks "how do you know your check-in trigger actually works?" Any answer
that isn't grounded in real data is hand-waving. This analysis gives us the
answer grounded in 62K real developer turns.
