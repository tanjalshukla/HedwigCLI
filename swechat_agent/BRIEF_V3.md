# SWE-chat follow-up analyses — brief (v3)

**Supersedes v2.** Three follow-up analyses to tighten the findings from v2 before the Hedwig demo. Parallel track; runs alongside Hedwig schema revisions on the main repo.

## Why we're running these

The v2 analysis produced the core findings Hedwig is now revising around. This v3 brief strengthens three specific weaknesses the v2 results have, in the order they matter for the demo:

- **A.** The 33% unclassified pushback bucket is real but not *characterized*. TF-IDF grouped messages with similar distinctive vocabulary; it didn't assign semantic meaning. We don't yet know what category is actually missing from the PushbackType enum. We'd like a proper name and clean examples before the demo, since this is one of the five headline findings.
- **B.** Every number in the v2 report is a point estimate. An advisor or reviewer who asks "what's the confidence interval?" has no answer. We want bootstrapped CIs on the top logistic-regression coefficients and on the cross-session ICC.
- **E.** v2 used logistic regression + decision tree. Neither captures interaction effects well ("feature A matters only when feature B is high"). A random forest on the same features would surface conditional patterns we missed.

Output is a findings update that attaches to `SWECHAT_ANALYSIS_REPORT.md` in the main repo. Feeds Hedwig's schema revision if anything shifts; otherwise tightens defensibility.

## Dataset

Already extracted. Reuse `data/features.parquet` and `data/session_features.parquet` from the v2 run. Do not re-extract; do not re-download SWE-chat raw data.

## Three analyses

Run in order. Each has its own section in the final report.

### A. Re-characterize the 33% unclassified pushback bucket

**Input:** the 7,259 messages that fell into the `other` category in `data/feedback_topics.json`.

**Method:**

1. Re-extract or filter the raw feedback text for those 7,259 messages.
2. Encode with a pretrained sentence-embedding model. `all-MiniLM-L6-v2` from `sentence-transformers` is the right choice — small (~80MB), fast, runs on CPU, quality is strong for short messages.
3. Cluster the embeddings. Try k=3, 5, 7, 10 with silhouette scoring and pick the best. HDBSCAN is also fine if you prefer density-based clustering.
4. For each resulting cluster, pick the 5 closest-to-centroid verbatims and 5 random verbatims. Manually assign a category label based on what the cluster actually contains.
5. Report: how many clusters, what each cluster contains (verbatim examples), what category labels you assigned.

**Why this matters:** the v2 finding was "33% of pushback doesn't fit the existing taxonomy." The v3 finding needs to be "the missing category is X, here's what it looks like, here are examples." That lets Hedwig's PushbackType enum be extended with grounded category names rather than a speculative pair like `positive_redirect` + `scope_constraint`.

**Output:** `data/pushback_other_clusters.json` with cluster assignments, centroid verbatims, and category labels. A section in the findings doc naming what's in the bucket.

### B. Bootstrap CIs on the headline findings

**Method:**

1. For Q1's pushback-prediction logistic regression: bootstrap-resample the 62,544 turns 500 times. For each resample, refit the logistic regression. Record the coefficient for each feature in the top 10. Report the 95% CI (2.5th–97.5th percentile) for each.
2. For Q2's failure-report logistic regression: same method, 500 resamples.
3. For Q4's ICC calculation: bootstrap-resample the 128 developers with ≥3 sessions. Record the ICC each time. Report the 95% CI on ICC.

**Why this matters:** point estimates are suggestive, not defensible. An advisor asking "how confident are you in ICC=0.249?" needs an answer like "95% CI [0.19, 0.31]." Same for the top coefficients — we claim debug intent has a 0.52 coefficient; a CI tells us whether that's robust or could be anywhere from 0.1 to 0.9.

**Output:** `data/bootstrap_cis.json` with per-feature CI arrays, plus a table in the findings doc. Keep the raw bootstrap results too so we can compute other statistics later if needed.

### E. Random forest + interaction effects

**Method:**

1. Fit a random forest (100 trees, default depth) on the same features as Q1, targeting pushback. 5-fold cross-validated AUC.
2. Compare AUC to the logistic regression baseline (0.754). If the RF does meaningfully better (say +0.02+), non-linear or interaction effects are real.
3. Compute feature importance via mean decrease in impurity *and* permutation importance. Report both — they can disagree when features are correlated, and the disagreement is itself informative.
4. For the top 5 features: plot partial dependence pairwise (top feature × each of the other 4). Look for non-additive effects — curves that bend based on the other feature. Report which interactions are meaningful.
5. Do the same for failure-report prediction (Q2's target).

**Why this matters:** v2 said "debug intent matters; prior pushback count matters." v3 might reveal "debug intent matters *only* when the agent has been bash-heavy" — a conditional pattern that reshapes the failure-signal trigger Hedwig is shipping. If the RF reveals no meaningful interactions, that's also a useful finding ("effects are additive; our logistic model captured the signal well").

**Output:** `data/rf_importance.json` with both feature-importance rankings, partial dependence plot data, and a section in the findings doc naming any interaction effects found.

## Deliverables

```
swechat_agent/
├── data/
│   ├── pushback_other_clusters.json   # A
│   ├── bootstrap_cis.json              # B
│   └── rf_importance.json              # E
├── docs/
│   └── FINDINGS_V3.md                  # update that attaches to v2 report
└── scripts/
    ├── recluster_other.py              # A
    ├── bootstrap_ci.py                  # B
    └── random_forest.py                 # E
```

`FINDINGS_V3.md` is the primary deliverable. Three sections (one per analysis), each with:

- Direct answer (what the analysis says)
- Supporting numbers (confidence intervals, importance scores, interaction findings)
- Implication for Hedwig's taxonomy revision (does this change anything we're already planning?)

## Ground rules

1. **Do not modify `../sc/`.** Read-only imports only if needed.
2. **Reuse existing features.** The features.parquet files from v2 have what you need. Only A needs the raw feedback text; B and E reuse the feature matrix directly.
3. **No full model retraining.** These are statistical analyses on existing data, not new ML training runs.
4. **Semantic embeddings for A only.** Don't use semantic embeddings anywhere else — we still want interpretable results for B and E.
5. **Bootstrap with fixed random seed.** Seed the numpy RNG so results are reproducible.
6. **Report honestly.** If bootstrap CIs are wide enough to undermine a v2 finding, say so. If RF reveals no meaningful interactions, say that too. Negative findings are findings.
7. **Time budget.** A: half a day. B: half a day. E: half a day. Total: 1.5 days of agent time. If you hit 2 days with no progress on one of them, stop and write what you're stuck on to `docs/V3_BLOCKERS.md`.
8. **Progressive commits.** Each analysis's output gets committed when it's done, not at the end.

## Handoff back to Hedwig

Once `FINDINGS_V3.md` is written, the Hedwig main track will:

1. Update `PREFERENCE_TAXONOMY.md` if the 33% characterization suggests different category names than `positive_redirect` / `scope_constraint`.
2. Tune the failure-signal check-in trigger if RF reveals interaction effects that should be part of the rule.
3. Attach CIs to the findings in `SWECHAT_ANALYSIS_REPORT.md` so the advisor meeting materials are defensible.

## What to tell the user agent running this

Read `BRIEF_V3.md`. The previous `BRIEF.md` (v2) is still relevant for context about the dataset and feature set, but v3 is the active task. Do not repeat v2 analyses.

Start with A (fastest, highest demo value), then B, then E. If time runs short, having A + B is better than having all three partially done.
