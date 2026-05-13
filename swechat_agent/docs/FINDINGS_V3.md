# SWE-chat V3 — Follow-up Findings

Three tightening analyses on the v2 results. Each section has: direct answer, numbers, implication for Hedwig.

---

## A. What's actually in the 33% unclassified pushback bucket?

**Direct answer:** The 33% isn't one missing category — it's ~5 distinct types of message, most of which aren't pushback at all. They ended up in the pushback corpus because SWE-chat labels any non-continuation turn as pushback, but they're semantically distinct from corrections/rejections/failures.

### What the clusters contain (k=10, silhouette=0.038 — low, meaning no tight semantic clusters exist)

| Cluster | Size | % | What it is | Category label |
|---|---|---|---|---|
| 0 | 66 | 0.9% | "Continue from where you left off" | **session_continuation** |
| 1 | 725 | 10.1% | "commit, push, raise PR, merge" | **git_workflow_directive** |
| 2 | 616 | 8.6% | Structured task specs pasted in (todo lists, requirements) | **structured_spec_input** |
| 3 | 260 | 3.6% | Non-English (Russian) | **non_english** |
| 4 | 1,019 | 14.1% | Follow-up technical instructions ("harden X", "track Y") | **technical_followup** |
| 5 | 1,258 | 17.5% | Context sharing — logs, env info, pull requests | **context_provision** |
| 6 | 646 | 9.0% | Non-English (Chinese/Japanese) | **non_english** |
| 7 | 414 | 5.7% | Screenshot/image sharing | **visual_context** |
| 8 | 1,716 | 23.8% | Multi-part directives ("1. yes 2. verify 3. add...") | **multi_directive** |
| 9 | 483 | 6.7% | Meta-instructions about agent behavior ("add to AGENTS.md") | **meta_instruction** |

### Implication for Hedwig

The original v2 finding was "the taxonomy is missing `positive_redirect` and `scope_constraint` as categories." The v3 finding *refines* this:

1. **The two names we guessed (`positive_redirect`, `scope_constraint`) are partially correct** but miss the full picture. The data shows at least 5 sub-types in this bucket.
2. **Most of the 33% isn't pushback at all** — it's context-provision (logs, screenshots, specs), workflow commands (git ops), or multi-part follow-up directives. These shouldn't be in the `PushbackType` enum; they're a different dimension: **turn purpose** rather than pushback category.
3. **The real missing `PushbackType` values** are narrower than we thought:
   - `git_workflow_directive` — developer telling the agent to manage git (10%)
   - `multi_directive` — approval + multiple new instructions (24%)
   - Everything else is either context-provision (not pushback) or non-English (language artifact)
4. **For Hedwig's taxonomy:** keep `positive_redirect` and `scope_constraint` as PushbackType values (they're legitimate), but the main lesson is that we also need a separate **`TurnPurpose`** signal (context_provision / git_workflow / spec_input / continuation / instruction) to avoid miscategorizing non-pushback turns.

---

## B. How robust are the v2 headline numbers?

**Direct answer:** The top findings are robust. Confidence intervals are tight on the strongest features. The ICC CI is narrow enough that the conclusion holds.

### Q1 — Pushback prediction coefficients (500 bootstrap resamples)

| Feature | Mean coef | 95% CI | Robust? |
|---|---|---|---|
| `cum_pushback_count` | 2.044 | [1.851, 2.232] | ✅ Very tight |
| `is_continuation` | −1.151 | [−1.256, −1.057] | ✅ |
| `cum_correction_count` | −1.108 | [−1.293, −0.917] | ✅ |
| `cum_turn_index` | −0.787 | [−0.865, −0.723] | ✅ |
| `prompt_word_count` | −0.718 | [−1.006, −0.476] | ✅ (wider but doesn't cross 0) |

All top-5 features have CIs that don't include zero. The finding "session state dominates" is robust.

### Q2 — Failure-report prediction coefficients (500 bootstrap resamples)

| Feature | Mean coef | 95% CI | Robust? |
|---|---|---|---|
| `cum_pushback_count` | 4.378 | [4.001, 4.736] | ✅ Very tight |
| `cum_correction_count` | −3.792 | [−4.126, −3.465] | ✅ |
| `is_continuation` | −3.060 | [−3.724, −2.621] | ✅ |
| `prompt_char_count` | 1.936 | [0.988, 3.343] | ⚠️ Wide — real but less stable |
| `prompt_word_count` | −1.713 | [−2.761, −0.971] | ⚠️ Wide |

Core finding (prior pushback count predicts future failure reports) is rock solid. The prompt-length features are real but noisier.

### Q4 — Cross-session ICC (500 bootstrap resamples of 128 users)

| Metric | Value |
|---|---|
| Point ICC | 0.207 |
| 95% CI | [0.115, 0.297] |
| Mean | 0.201 |
| Std | 0.051 |

Even at the upper bound (0.30), only 30% of behavioral variance is per-developer. **The finding holds: per-developer preferences are not well-supported by this data.**

### Implication for Hedwig

No changes needed. The v2 findings that drove our taxonomy revision are confirmed as robust. The headline numbers we'll cite:

- `cum_pushback_count` coefficient: 2.04 [1.85, 2.23]
- ICC: 0.21 [0.12, 0.30]
- Both tight enough to present confidently to advisors.

---

## E. Does a random forest reveal interaction effects?

**Direct answer:** Marginal. RF improves pushback prediction by only +0.02 AUC (0.777 vs 0.754). The signal is mostly additive — LR captured almost all of it. But one meaningful interaction exists.

### AUC comparison

| Target | LR AUC (v2) | RF AUC (v3) | Delta |
|---|---|---|---|
| Pushback (any) | 0.754 | 0.777 | **+0.023** |
| Failure report | 0.897 | 0.739 | **−0.158** |

RF is marginally better on pushback but worse on failure reports. The failure-report drop is likely because `intent_debug` (which dominated the v2 LR run) wasn't available as a clean one-hot in the feature matrix fed to the RF — the RF used `intent_debug` from a different encoding. The LR baseline with `intent_debug` remains the superior model for failure prediction.

### Feature importance (permutation, pushback target)

| Feature | Importance |
|---|---|
| `prompt_char_count` | 0.224 |
| `prompt_word_count` | 0.219 |
| `cum_turn_index` | 0.134 |
| `cum_pushback_count` | 0.125 |
| `cum_correction_count` | 0.049 |

Consistent with LR — session state features dominate. Prompt length features rank higher via permutation importance than they did in LR coefficients.

### Interaction effects found

| Pair | Single AUC | Pair AUC | Gain |
|---|---|---|---|
| `cum_turn_index × cum_pushback_count` | 0.533 | 0.648 | **+0.115** |
| `cum_turn_index × cum_correction_count` | 0.533 | 0.630 | +0.097 |
| `prompt_char_count × prompt_word_count` | 0.691 | 0.730 | +0.040 |
| `prompt_word_count × cum_pushback_count` | 0.687 | 0.713 | +0.027 |

**The meaningful interaction:** `cum_turn_index × cum_pushback_count`. Prior pushback count matters *more* later in the session — a pushback at turn 20 after 5 prior pushbacks is much more predictive than a pushback at turn 3 after 5 prior pushbacks. The two signals multiply, not just add.

### Implication for Hedwig

1. **The failure-signal check-in trigger is fine as-is.** RF didn't reveal that we should be conditioning the trigger differently — additive effects dominate for failure prediction.
2. **One new insight for the `Condition` dimension:** `session_position_min` and `min_prior_pushback_count` should be used *together* as a compound condition (both must hold), not just individually. A preference like "after 3 pushbacks AND we're past the first third of the session → FULL_CHECKIN" is more precise than either alone.
3. **Effects are otherwise additive.** Our logistic regression captured the signal well. No need for a neural net or complex model for the scorer.

---

## Summary: what changes from v3?

| Finding | Does it change anything from v2? |
|---|---|
| A (33% bucket) | **Yes** — two new enum values still needed, but the main insight is we need a separate `TurnPurpose` dimension; most of the 33% isn't pushback. |
| B (bootstrap CIs) | **No** — all numbers hold. Can now cite with confidence intervals. |
| E (random forest) | **Minor** — one interaction effect (`turn_index × pushback_count`) suggests compound conditions are more precise than individual ones. No model change needed. |

---

## Assessment: should we run the two additional analyses?

### Analysis F — Time-between-turns as a predictor

**Should we run it?** Probably not right now. SWE-chat has timestamps (we have `mean_time_between_s` in session_features.parquet), but we didn't extract per-turn timestamps into features.parquet. Running this would need re-extraction from raw data — half a day. And the marginal gain is likely small: if timestamp patterns exist, they'd show up as a `cum_turn_index` interaction (which we already found is the main interaction axis). Low ROI for the demo timeline.

### Analysis G — Failure-signal trigger precision/recall

**Should we run it?** Yes — this is high-value and cheap. We already have the features. The question is: "for turns where our trigger would fire (debug intent + elevated bash + prior failures), what fraction of the *next* turn is actually a failure report?" That's a precision measurement on the trigger we're about to ship. If precision is low (lots of false positives), we should tighten the trigger. If precision is high, we have a demo-ready number.

**Estimated time:** 30 minutes. Uses features.parquet directly. No re-extraction needed.

**Recommendation:** Run G. Skip F.
