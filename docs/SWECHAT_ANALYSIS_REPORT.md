# Hedwig × SWE-chat: Analysis Report

End-to-end writeup of the SWE-chat analysis we ran to inform Hedwig's preference taxonomy. Intended as a shareable document for the research team.

## 1. What we set out to learn

Hedwig is a governance layer that sits on top of a coding agent and decides, for each proposed action, whether to proceed autonomously or pause and ask the developer. It has a preference taxonomy describing *how* a developer wants oversight to work — what kinds of actions to ask about, under what conditions, and with what scope.

The taxonomy's categories (three coding modes, three user personas, four pushback types) were imported from the SWE-chat paper's structure. That was a starting point, not a conclusion. Before investing further in the taxonomy, we wanted to know whether those categories actually match real developer behavior in the wild.

The analysis answers: **do the signals and categories we assumed match what real developers actually do, and what signals are we missing?**

## 2. The dataset

**Source.** SWE-chat, released by SALT-NLP at HuggingFace (`SALT-NLP/SWE-chat`), licensed ODC-By. Gated access; approval required. Companion paper: arXiv 2604.20779.

**Scale.** 5,776 sessions drawn from 200+ public GitHub repositories. 62,544 developer-prompt turns, 355,000+ agent tool calls, 2.7M logged interactions. A "session" is a continuous conversation between a developer and a coding agent; a "turn" is one developer prompt and the agent's response.

**Collection method.** Opt-in developers using real coding CLIs in their own repositories. Not lab-collected, not synthetic, not scraped from public transcripts. The distribution reflects how developers actually use coding agents in production work.

**Labeled fields.** Each turn carries a pushback label (correction / rejection / failure report / non-pushback) and each session carries a persona label (expert nitpicker / vague requester / mind changer / other) and a coding-mode label (human-only / collaborative / vibe, based on the agent's share of surviving code).

**Tables used in this analysis.** The `sessions` table (session metadata, persona, agent authorship percentage) and the `conversations` table (per-turn prompts, pushback label, tool calls).

## 3. What we were testing, and why we picked the methods we did

The analysis had five research questions, each answered with a specific method chosen for specific reasons. Methods were picked for interpretability over performance — we wanted to know *which* signals matter, not just whether *something* does.

### Q1. What predicts when a developer pushes back?

**Method: logistic regression, 5-fold cross-validated. Decision tree as a cross-check.**

Rationale: logistic regression's coefficients are directly interpretable — each feature's weight tells you how much it contributes to the decision. That's exactly what we want when the goal is to identify signals, not build a production classifier. A decision tree serves as a different model family — if the two methods agree on the top features, we can be confident the result isn't an artifact of one algorithm's assumptions.

### Q2. What specifically predicts a failure-report turn (as opposed to other pushback)?

**Method: same logistic regression, but targeting failure reports specifically.**

Rationale: failure reports are the one pushback class with content-grounded labels (based on what the developer wrote), so patterns we find there generalize more cleanly. Failure reports also have obvious deployment value — if we can predict them, we can pre-empt them with a check-in.

### Q3. Do developers cluster into the three personas the paper proposed?

**Method: k-means clustering on session-level features, with k ranging 2–6, evaluated by silhouette score.**

Rationale: clustering asks "given no labels, does this data have natural structure?" K-means with silhouette scoring answers "yes, and the cleanest cluster count is k=N." If the data clusters at k=3, the paper's persona framing holds up. If not, the categories are theory-driven rather than empirical. Silhouette score is the standard diagnostic for "how well-separated are these clusters?" — it handles the "should there be 2 or 5 clusters" question cleanly.

### Q4. Does behavior change within a session, and is it stable across a user's sessions?

**Method: chi-squared / t-tests on early-vs-late-session thirds for within-session trends. Intraclass correlation (ICC) across sessions per developer for cross-session stability.**

Rationale: the within-session split tests whether time-in-session matters — a yes answer justifies session-scoped preferences. ICC tests whether a developer has a stable interaction style; a high ICC (close to 1) means their behavior is predictable across sessions and per-developer learning is worth pursuing. A low ICC means developer style varies too much across their own sessions for per-developer signals to be useful.

### Q5. What do developers actually push back *about*, in content terms?

**Method: TF-IDF vectorization of free-text feedback, k-means clustering on the vectors, manual assignment of clusters to 8 topical categories.**

Rationale: TF-IDF is cheap, inspectable, and sufficient for 22K messages. It weights words by how distinctive they are — common words like "the" get downweighted, domain-specific words like "migration" get upweighted. Clustering the resulting vectors groups messages that use similar distinctive vocabulary. Manual category assignment on the top clusters tells us what themes exist, not just which word combinations.

## 4. Research questions fed to the agent

The five-question brief was specific enough to direct execution and generic enough to allow the agent to extend the feature list where reasonable. Full brief at `swechat_agent/BRIEF.md`.

1. **What predicts pushback?** Extract turn-level and session-state features; fit an interpretable predictive model; report top features by coefficient magnitude.
2. **What predicts failure-report turns specifically?** Same approach, different target. Examine agent behavior in the turns preceding failure reports.
3. **Do developers cluster into the three personas the SWE-chat paper proposed?** Cluster sessions on behavioral features, try a range of k values, report how well the data separates and whether the natural clusters correspond to persona labels.
4. **Does behavior change within a session? Is it stable across a user's sessions?** Split sessions into early/middle/late thirds and test for significant differences; compute intraclass correlation on behavioral metrics across sessions per developer.
5. **What do developers actually push back about?** Extract the most common topics in feedback text; group into categories.

**Methodological contribution (human side).** The original brief was scoped as "validate Hedwig's taxonomy against SWE-chat labels" — a check, not a discovery. The brief was rewritten mid-analysis to reframe the goal as "discover what signals exist in the data that our taxonomy doesn't capture." That reframe is what produced the useful findings; validation alone would have told us the existing thresholds were miscalibrated, not that the underlying schema had structural gaps.

## 5. Features extracted

For every turn, roughly 30 measurements across two groups.

**Turn-level features** (what's happening in this specific turn):

- Prompt length in words and characters.
- Task intent, parsed from prompt text into one-hot categories: debug, refactor, create, understand, test, other.
- Preceding agent-turn tool counts: bash invocations, file reads, file writes/edits, total tool calls.
- Bash subcategories in the preceding turn: git operations, test/build commands, package management, file operations, other.
- File types touched in the preceding turn: Python, TypeScript/JavaScript, Markdown, config files, etc.
- Positional flag: is this the first turn in the session?

**Session-state features** (cumulative, showing how far into the session we are):

- Turn index within session.
- Cumulative pushback count (all prior corrections + rejections + failure reports).
- Cumulative correction count specifically.
- Cumulative failure-report count specifically.
- Count of distinct files touched so far.

**Baseline features specified in the research brief** (turn index, prompt length, tool counts, file types, cumulative pushback, cumulative failures, distinct files touched) accounted for most of the list. The agent extended the set with intent subcategories and bash subcategories.

**Features we could not extract.** SWE-chat does not log developer cognitive state (fatigue, frustration, flow), code correctness (whether what was committed actually worked), reversibility of changes, wall-clock time-of-day, or multi-session project context. These are genuine gaps in what we could measure.

## 6. What we found

### Finding 1 — Pushback is predicted by session state, not file state.

AUC 0.754 (logistic regression) and 0.751 (decision tree) — well above random (0.5). The two methods agreed on which features mattered.

Top predictors by coefficient magnitude:

| Feature | Direction | Weight |
|---|---|---|
| Cumulative pushback count in session | ↑ more pushback | 2.77 |
| Cumulative correction count | ↓ less | 1.83 |
| Prompt word count | ↓ less | 0.68 |
| Debug intent | ↑ more | 0.52 |
| Refactor intent | ↑ more | 0.33 |
| Agent's response length | ↑ more | 0.27 |
| First turn in session | ↓ less | 0.26 |

File-type features (Python, TypeScript, Markdown, etc.) all had coefficient magnitudes below 0.05. This is the core finding: **file type and change shape barely predict anything.** Session-level state dominates.

### Finding 2 — Failure reports are highly predictable and preceded by specific patterns.

AUC 0.897 for predicting failure-report turns — far stronger than general pushback prediction. Failure reports are 7.5% of turns (4,382 of 62,544).

**74.8% of failure reports come from turns where the developer signaled debug intent.** Bash activity in the preceding turn is 1.45× baseline (2.29 calls vs. 1.57). Surprisingly, only 3.8% of failure reports follow test/build commands — 36.1% follow git operations, 32.1% follow unclassified bash, 18% follow file-system operations.

The deployable implication: the pattern *debug intent + elevated bash activity + prior failures in session* is strong enough to fire a proactive check-in before the developer has to stop the agent themselves.

### Finding 3 — Developer behavior clusters into two groups, not three or four.

Silhouette scores across k values:

| k | Silhouette |
|---|---|
| **2** | **0.378** (best) |
| 3 | 0.209 |
| 4 | 0.223 |
| 5 | 0.224 |
| 6 | 0.246 |

The k=2 split is clean; k=3 and beyond show marginal improvements likely driven by outliers. The two clusters are differentiated by **session intensity**, not persona type:

| | Cluster A | Cluster B |
|---|---|---|
| Share of sessions | 82% (n=4,729) | 18% (n=1,066) |
| Pushback rate | 29.6% | 44.2% |
| Avg prompt length (words) | 149 | 213 |
| Avg preceding tool calls | 2.95 | 13.17 |
| Avg turns per session | 7.6 | 24.9 |

Cluster A: **short, low-engagement, high agent-authorship** — developers delegating to the agent and accepting most output.
Cluster B: **long, high-engagement, heavier human involvement** — developers deeply working with the agent.

Expert Nitpicker, Vague Requester, and Mind Changer labels appear in both clusters in roughly similar proportions. **The paper's persona labels do not correspond to the behavioral clusters in the data.** They describe something (possibly self-reported interaction style) that is orthogonal to session-intensity behavior.

### Finding 4 — Behavior changes within a session, but is not stable across sessions.

**Within-session trends** (for 2,703 sessions with ≥6 turns, comparing early third to late third):

| Metric | Early | Late | p-value |
|---|---|---|---|
| Pushback rate | 37.2% | 40.3% | <0.001 |
| Prompt word count | 202 | 159 | <0.001 |
| Preceding tool count | 5.40 | 4.61 | <0.001 |
| Failure-report rate | 6.4% | 6.8% | 0.18 (not significant) |

Pushback rises, prompts shorten, tool use drops as sessions progress. Failure reports are distributed throughout sessions.

**Cross-session stability**: ICC = 0.249 across 128 developers with ≥3 sessions each. Only 25% of variance in pushback rate is explained by which developer it is; 75% is variance within each developer across their own sessions. Concrete example: one developer's pushback rates across five sessions ranged from 29% to 65%.

The implication is architectural: **session-scoped preferences are supported by the data; per-developer preferences are not.** The same person behaves differently enough across sessions that inferring stable per-user style from history is unreliable.

### Finding 5 — A third of pushback doesn't fit the existing categories.

Across 21,875 pushback messages, TF-IDF topic clustering and manual category assignment yielded:

| Category | Count | % |
|---|---|---|
| Unclassified ("other") | 7,259 | 33.2% |
| Content correction | 6,514 | 29.8% |
| Failure/error | 3,116 | 14.2% |
| Approach correction | 1,501 | 6.9% |
| Style/formatting | 1,480 | 6.8% |
| Scope/requirements | 1,145 | 5.2% |
| Timing/pacing | 656 | 3.0% |
| Clarity/understanding | 204 | 0.9% |

The 33% "other" bucket is mixed. Representative verbatims include: *"can we have another subagent create that one?"* (delegation), *"I like it, make it so"* (positive redirect with instruction), *"isnt there a dourth [fourth]"* (clarifying question). What unifies them is that they are neither complaints, refusals, nor failure reports — but they aren't silent agreement either.

The existing four-category pushback schema has at least one gap. Further analysis of the 33% bucket is needed to characterize precisely what category is missing.

## 7. What this means for Hedwig

All five findings are informing the next revision of Hedwig's preference system. The implications below are conceptual; implementation details live in the codebase.

**The signals Hedwig pays attention to need to shift.** Hedwig was originally structured around file-level risk — what kind of file, what kind of change, how big, whether it touches sensitive paths. The data says those signals are weak predictors of developer reactions. Session-level state (how many prior pushbacks, what task the developer said they were doing) is the dominant axis. Hedwig will continue to enforce file-level *hard constraints* (security rules, never-touch paths) as deterministic rules, but the *learned* part of Hedwig should track session-level state instead.

**A concrete check-in trigger becomes available.** The failure-signal pattern (debug intent + elevated agent bash activity + prior failures in the session) is strong enough to act on. Hedwig will use it as a proactive check-in trigger — pausing the agent before it runs more commands in a session that's showing those warning signs.

**The persona framework needs simplification.** The three-persona schema doesn't match behavioral clusters. The data supports a simpler two-category split based on session intensity: developers who are delegating versus developers who are actively engaged. This is more than a rename — the two behaviors respond differently to the same agent actions, and Hedwig's oversight policy can be calibrated differently for each.

**Preferences are session-scoped, not developer-scoped.** The instinct to learn per-user preferences from history isn't supported here. Developer behavior varies enough across sessions that stable user profiles would encode noise. Hedwig will default inferred preferences to session scope and let them decay or reset cleanly at session boundaries. Repository-level defaults and explicitly-set preferences remain persistent; inferred behavioral preferences do not.

**The pushback taxonomy is incomplete.** A third of developer responses don't fit the existing four categories. The next step is re-analyzing that unclassified third to characterize what it actually contains before deciding which categories to add.

## 8. Limitations and what we can't claim

- **We predicted pushback, not check-ins.** SWE-chat's agents rarely pause to ask — they mostly act and developers react. We can measure what signals predict negative reactions after the fact; we cannot directly measure what signals predict a helpful check-in. The step from "this predicts pushback" to "this should trigger a check-in" is an inference, not a direct measurement.
- **Interaction effects were not exhaustively tested.** Logistic regression and decision trees capture main effects. Conditional patterns like "A matters only when B is high" could exist without showing up in our analysis. A random forest or explicit interaction modeling would catch these.
- **The 33% unclassified bucket is characterized loosely.** We know what it isn't (correction, rejection, failure report, non-pushback) and have representative verbatims, but we don't yet have a clean category label for it. Re-clustering just that bucket is the next step.
- **Features invisible to SWE-chat are invisible to us too.** Developer fatigue, code correctness, change reversibility, time-of-day effects, multi-session context. Whatever role these play, we can't see it from this data.
- **ICC is computed on 128 developers.** Adequate for the claim we make; a bootstrap confidence interval would strengthen it.

## 9. Process notes

The analysis was run by directing a Claude-Code-based research agent (Sonnet 4.6) against the dataset. The research questions, baseline feature list, and algorithm families were specified in a written brief (`swechat_agent/BRIEF.md`). The agent extended the feature list with subcategories and implemented the methods. Findings were interpreted by the human researcher.

The original brief was scoped as "validate Hedwig's taxonomy against SWE-chat labels." Partway through the analysis, the brief was rewritten to reframe the goal as discovery rather than validation — the reframe is what produced findings useful for taxonomy revision. Both briefs are preserved in version control.

All raw extraction outputs (parquet files with per-turn and per-session features, clustering results, regression coefficients, topic assignments) are available at `swechat_agent/data/`.

## 10. Next steps (original v2 list)

- **Re-characterize the 33% unclassified pushback bucket.** Separate analysis scoped to just those 7,259 messages. — *Done, see §11.A.*
- **Validate robustness with bootstrap.** Confidence intervals on the top coefficients and on ICC. — *Done, see §11.B.*
- **Implement the failure-signal check-in trigger** in Hedwig and measure whether it fires at times developers find useful in real usage. — *Implemented as a built-in Preference; deployment precision measurement is the next parallel-agent task.*
- **Refactor Hedwig's preference schema** to collapse the persona enum, extend the pushback enum, and default inferred preferences to session scope. — *Done; see `sc/preferences.py`.*
- **Investigate interaction effects.** A random forest or cross-term analysis on the same features to surface conditional patterns logistic regression missed. — *Done, see §11.E.*

---

## 11. V3 update — follow-up analyses (bootstrap CIs, 33% re-cluster, random forest)

Three follow-up analyses tightened the v2 results. Full V3 writeup at `swechat_agent/docs/FINDINGS_V3.md`.

### §11.A — The 33% bucket is ~5 distinct types, most not pushback

Re-clustering the 7,259 unclassified messages with semantic embeddings (sentence-transformers `all-MiniLM-L6-v2`) at k=10 revealed low cluster cohesion (silhouette 0.038) — there is no single clean missing category. The bucket decomposes into roughly five types, ordered by share:

| Share | Type | Example |
|---|---|---|
| 24% | Multi-part directive | "1. yes 2. verify 3. add tests" |
| 18% | Context provision | pasted logs, env, PRs |
| 14% | Technical follow-up | "harden X", "track Y" |
| 10% | Git workflow | "commit, push, raise PR" |
| 9% | Meta-instruction | "add this to AGENTS.md" |

**Implication:** the original hypothesis that we were "missing `positive_redirect` + `scope_constraint` as pushback categories" holds partially — both names are legitimate — but the deeper insight is that much of what falls through the schema isn't pushback at all. It's a separate dimension: **what a turn is *for*** (purpose) rather than how it relates to the agent's last action (pushback). Hedwig's revised taxonomy now carries a `TurnPurpose` enum alongside `PushbackType`.

### §11.B — V2 findings are robust under bootstrap

500 bootstrap resamples on the V1/V2 logistic-regression models and on the ICC computation:

**Pushback prediction (top features):**

| Feature | Mean coef | 95% CI |
|---|---|---|
| `cum_pushback_count` | 2.044 | [1.851, 2.232] |
| `is_continuation` | −1.151 | [−1.256, −1.057] |
| `cum_correction_count` | −1.108 | [−1.293, −0.917] |
| `cum_turn_index` | −0.787 | [−0.865, −0.723] |
| `prompt_word_count` | −0.718 | [−1.006, −0.476] |

**Failure-report prediction (top features):**

| Feature | Mean coef | 95% CI |
|---|---|---|
| `cum_pushback_count` | 4.378 | [4.001, 4.736] |
| `cum_correction_count` | −3.792 | [−4.126, −3.465] |
| `is_continuation` | −3.060 | [−3.724, −2.621] |

**Cross-session stability (ICC):** 0.207, 95% CI [0.115, 0.297]. Even at the upper bound, only 30% of behavioral variance is per-developer.

**Implication:** the numbers we cite are defensible. The headline claims (session-state dominates, per-developer preferences aren't supported) are stable.

### §11.E — Random forest: effects are mostly additive, one meaningful interaction

A random forest on the same features as Q1 achieved AUC 0.777 on pushback prediction — only +0.023 over the logistic-regression baseline (0.754). Effects are predominantly additive; LR captures the signal well. No model change to Hedwig's learned scorer is warranted.

**The one meaningful interaction found:** `cum_turn_index × cum_pushback_count`. Prior pushbacks predict future pushback *more strongly* later in the session. A pushback at turn 20 after 5 prior pushbacks is much more predictive than a pushback at turn 3 after 5 prior pushbacks — the two signals multiply, not just add (pair AUC 0.648 vs. either alone at 0.533).

**Implication:** Hedwig's inferred preferences should use `session_position_min` and `min_prior_pushback_count` together as a compound condition, not individually. The V3 revision of `preference_hypotheses.py::_scope_narrowing_hypothesis` already applies this.

---

## 12. V4 update — failure-signal trigger precision on SWE-chat

The built-in `FAILURE_SIGNAL_CHECKIN` preference was measured against SWE-chat data to answer: for turns where the trigger would fire, how often does the developer actually report a failure on the next turn?

**Trigger definition.** Fires when `task_intent == "debug"` AND `prev_bash_count >= 2` AND `prior_failure_count >= 1` in the session.

**Results.**

| Metric | Value |
|---|---|
| Precision on next-turn failure reports | 23.6% |
| Baseline failure-report rate across all turns | 7.0% |
| Lift over baseline | **3.37×** |
| Fraction of all turns trigger fires on | 2.2% |
| Fraction of fires that land on *any* pushback moment (not just failures) | ~67% |

**Ship-as-is recommendation.** Tighter variants (`prev_bash_count >= 3`, `prior_failure_count >= 2`) produce the same precision with smaller coverage — not a useful trade. The current threshold is near-optimal.

**Demo-worthy framing.** The trigger is selective (fires on only 2.2% of turns) and precise (3.4× the random-check-in baseline). When it fires but the next turn isn't a failure, 41% of the time the developer is still pushing back about something — the check-in pre-empts a pushback moment in about two-thirds of its fires overall.

See `swechat_agent/docs/FINDINGS_V4.md` for full methodology.

## 13. Still open (post-demo)

- **Learned `TurnPurpose` classifier.** Currently keyword-based (`sc/preference_inference.py::infer_turn_purpose`). A learned classifier trained on real Hedwig traces could replace it.
- **Cross-session transfer.** If per-developer isn't stable, is per-repo stable? Worth checking post-demo.
- **Schema upgrade for `prev_bash_count`.** Hedwig's own traces don't yet log per-turn agent tool counts. When they do, the failure-signal trigger's `prev_bash_count_min` predicate will match the SWE-chat finding exactly.
