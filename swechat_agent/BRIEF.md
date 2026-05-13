# SWE-chat pattern discovery — brief (v2)

**Supersedes the previous brief.** The previous task validated Hedwig's taxonomy against SWE-chat labels. That was the wrong task. Keep the old artifacts as baseline context, but the goal below is different.

## What we actually want

**Discover what preference signals exist in real developer-agent traces. Don't validate our guesses.**

Hedwig has a preference taxonomy with five dimensions — `Trigger`, `Condition`, `PreferenceAction`, `Scope`, `Lifecycle` — and three inferred signals (`CodingMode`, `UserPersona`, `PushbackType`). These were designed from the SWE-chat paper's section headings. That's a starting point, not a conclusion.

Your job is to mine the 5,776 sessions and 62K turns to answer: **what does real developer-agent behavior actually look like, and what does that say about the preference schema?**

Output is a findings report. The report feeds taxonomy revision on Hedwig's side. You do not modify Hedwig.

## Dataset

Already in place. See `data/DATASET.md` for access details. Two tables matter: `sessions` (metadata + persona label) and `conversations` (per-turn interactions + pushback label).

## Five research questions, in priority order

These are what the report must answer. Each one is worth a section in your findings doc. Don't skip questions because they're hard — flag the difficulty and proceed.

### 1. What predicts pushback?

For each turn, extract every feature you can compute from SWE-chat: turn index in session, time since previous turn, prompt length (tokens/words), response length, tool-call count in the preceding agent turn, file types touched, edit distance (if available or a proxy), whether a bash tool call was made, session's cumulative turns so far. Also cumulative session state: prior pushback count, prior failure-report count, distinct files touched.

Run any pushback type (any of correction/rejection/failure_report) as the target. Fit a simple logistic regression or decision tree — something you can read. Report the top features by coefficient magnitude or feature importance.

**Why this matters:** those top features are candidate `Trigger` or `Condition` fields for the Hedwig taxonomy. Hedwig currently uses `change_pattern`, `blast_radius`, `diff_size`, `is_security_sensitive`, `is_new_file`. If SWE-chat says the real predictors are different — turn index, time gap, prompt length — then the taxonomy's `Trigger` shape is wrong.

### 2. What predicts a failure_report specifically?

Failure reports are the one pushback class where the classification isn't circular (it's based on text content, not label derivation). So agreement rates on failure_report actually mean something, and what precedes them is a strong signal.

For each failure_report turn, look at the preceding 1-3 agent turns. What did the agent do? What file types? What tool sequence? Are there patterns like "agent ran tests → failed → user reported failure"?

**Why this matters:** these patterns become candidate check-in triggers. If 40% of failure reports follow a specific agent action sequence, that sequence should trigger a proactive check-in.

### 3. Does behavior cluster into the 3-4 personas we assumed?

**Ignore SWE-chat's persona labels for this question.** Take each session's behavioral features (mean edit distance, pushback rate, turn count, mean time-per-turn, variance in task strings, distinct file count, etc.) and cluster them. K-means or HDBSCAN or whatever you like, but justify the choice.

Try k = 2, 3, 4, 5, 6. Look at silhouette scores or the distortion elbow. Report what the natural clusters look like — how many are there, what distinguishes them, and do they roughly match our `{expert_nitpicker, vague_requester, mind_changer}` or do they suggest different personas?

**Why this matters:** if the data says k=5 is right, or k=2, that directly changes the `UserPersona` enum. If the clusters don't match our assumed personas, that's a finding we need to feed into the paper.

### 4. Does behavior change within a session?

Per session, bucket turns into early/middle/late thirds. For each third, compute: pushback rate, edit distance mean, prompt length, tool call counts. Does any of these change significantly within-session?

If early-session pushback is higher than late-session, that tells us developers calibrate fast and preferences should be **session-scoped** (Hedwig has `Scope.level = "session"` — is it justified?). If behavior is stable within-session, session scoping is architectural overkill.

Also do a cross-session check if possible: for users with multiple sessions, does persona-assigned-by-clustering stay stable across their sessions? (This is the question SWE-chat's paper explicitly said they didn't address.)

**Why this matters:** directly answers whether Hedwig's multi-level `Scope` dimension carries real information.

### 5. What do developers actually push back *about*?

For every turn with non-empty feedback text, extract the top topics. Simple TF-IDF or keyword clustering is fine. Don't need LLM clustering unless trivial.

Produce a ranked list of the 20 most common feedback topics with example verbatims. Group into categories (content correction / approach correction / timing / style / safety / clarity / other).

**Why this matters:** Hedwig's preference taxonomy has dimensions for what (`Trigger`), when (`Condition`), and action (`PreferenceAction`). But it has no dimension for *what the developer objected to*. Leijie's PhD feedback explicitly named "developer-intent labeling" as missing — this is that data. It might imply a 6th taxonomy dimension.

## Deliverables

```
swechat_agent/
├── data/
│   ├── features.parquet           # per-turn feature matrix (from Q1, Q2, Q4)
│   ├── clusters.json              # cluster assignments + centroids (from Q3)
│   └── feedback_topics.json       # topic rankings + verbatims (from Q5)
├── docs/
│   ├── FINDINGS.md                # the main report — one section per research question
│   ├── FEATURE_CATALOG.md         # what each feature is, how it was computed
│   └── SCHEMA_IMPLICATIONS.md     # what the findings say Hedwig's taxonomy should change
└── scripts/
    ├── extract_features.py
    ├── predict_pushback.py        # Q1, Q2
    ├── cluster_personas.py        # Q3
    ├── session_trajectories.py    # Q4
    └── feedback_topics.py         # Q5
```

`docs/FINDINGS.md` is the primary deliverable. It's what I'll read to decide what to change in Hedwig.

## Ground rules

1. **Do not modify `../sc/`.** Read-only imports only, if any.
2. **Do not train large models.** Logistic regression, decision trees, k-means, TF-IDF — all fine. No fine-tuning, no transformers.
3. **Do not try to validate against SWE-chat's own labels.** That was the old task. Their labels are useful for sanity-checking clusters, not for scoring agreement.
4. **Do not invent features.** If a feature can't be computed from the available fields, document the gap in `FEATURE_CATALOG.md` and move on.
5. **Report honestly.** If a question has no signal (e.g., "behavior doesn't change within session"), that's a finding. Say so. Don't fabricate.
6. **No paper citations in code comments.** They go in the final paper only.
7. **Progressive commits.** Start with features.parquet for one question, not all five at once.

## What "done" looks like

`docs/FINDINGS.md` answers all five research questions with:

- A direct answer ("yes/no/maybe, with this caveat")
- The supporting numbers (top features, cluster count, verbatim topics)
- An implication for Hedwig's taxonomy — what should change, what stays

Plus `docs/SCHEMA_IMPLICATIONS.md` as a short summary: given the findings, what in the five Hedwig dimensions (`Trigger`, `Condition`, `PreferenceAction`, `Scope`, `Lifecycle`) should be added, changed, or removed?

That document is what we use to revise Hedwig's taxonomy. It's the point of the whole exercise.

## Previous work

The old validation run produced `data/validation.json` with agreement rates. Keep it — it's a useful baseline that says our existing thresholds are miscalibrated. Don't re-run it. The new work doesn't depend on it.

## If you get stuck

Stop and write what you're stuck on to `docs/BLOCKERS.md`. Don't force a bad answer.
