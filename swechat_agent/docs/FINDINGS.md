# SWE-chat Pattern Discovery — Findings

Answers five research questions about real developer-agent behavior in 5,776
sessions and 62,429 user-prompt turns. Every number here is computed from
SWE-chat's `sessions` and `conversations` tables.

---

## Q1. What predicts pushback?

**Direct answer:** Session history dominates turn-level features. Intent (debug/refactor) is the strongest single-turn signal. Tool-use features matter less than expected.

### Numbers

- Base pushback rate: 41.1% of annotated turns (correction + rejection + failure_report)
- Logistic regression 5-fold AUC: **0.754** (decision tree: 0.751 — essentially equal)
- The model has real signal. AUC well above 0.5 means behavioral history predicts pushback.

### Top features by LR coefficient magnitude

| Rank | Feature | Direction | Magnitude |
|------|---------|-----------|-----------|
| 1 | `cum_pushback_count` (prior pushbacks in session) | ↑ pushback | 2.77 |
| 2 | `cum_correction_count` (prior corrections) | ↓ pushback* | 1.83 |
| 3 | `intent_debug` | ↑ pushback | 0.52 |
| 4 | `prompt_word_count` | ↓ pushback | 0.68 |
| 5 | `intent_refactor` | ↑ pushback | 0.33 |
| 6 | `prev_resp_word_count` (agent verbosity) | ↑ pushback | 0.27 |
| 7 | `is_first_turn` | ↓ pushback | 0.26 |
| 8 | `cum_distinct_files` | ↑ pushback | 0.14 |

*The negative direction of cum_correction_count is counterintuitive — it likely reflects that after several corrections the session has converged (late-session effect, collinear with cum_turn_index).

The decision tree confirms the ordering: `intent_debug` (0.35), `intent_refactor` (0.23), `cum_pushback_count` (0.14), `prompt_word_count` (0.11).

### What's absent from the top features

Hedwig's current `Trigger` fields — `blast_radius`, `diff_size`, `is_new_file`, `change_pattern`, `is_security_sensitive` — are not present in SWE-chat and cannot be directly compared. However, file-type features (`prev_has_ts_js`, `prev_has_py`, etc.) rank low (below 0.05), which suggests **file-level action type is a weak predictor** relative to session state.

### Implication for Hedwig

The strongest predictors are **session-state variables** (prior pushback count, intent type). Hedwig's `Trigger` is purely action-level (what file, what change). The `Condition` dimension is where session state lives — but currently `Condition` only tracks `max_recent_denials` and `min_recent_approvals`, not cumulative counts or task intent. This is a gap.

---

## Q2. What predicts failure_report specifically?

**Direct answer:** Failure reports are highly predictable (AUC 0.897) and are strongly preceded by debug intent, higher bash activity, and prior failure history. They are not preceded by test/build bash calls — git and unclassified bash dominate.

### Numbers

- Failure_report rate: 7.5% of annotated turns (4,382 turns)
- LR 5-fold AUC: **0.897** — failure reports are the most predictable pushback class

### Preceding agent behavior for failure_report turns vs. all turns

| Tool type | FR turns mean | All turns mean | Ratio |
|-----------|--------------|----------------|-------|
| `prev_bash_count` | 2.29 | 1.57 | **1.45×** |
| `prev_read_count` | 1.83 | 1.33 | 1.37× |
| `prev_write_edit_count` | 1.17 | 0.99 | 1.19× |
| `prev_tool_count` | 5.93 | 4.64 | 1.28× |

Failure reports follow heavier agent activity overall, not just writes.

### Preceding bash category for failure_report turns

| Category | % of FR turns |
|----------|--------------|
| `git` | 36.1% |
| `other` | 32.1% |
| `file ops` | 18.0% |
| `package manager` | 10.1% |
| `test/build` | 3.8% |

**Surprising finding:** Only 3.8% of failure reports are preceded by test/build bash calls. "Agent ran tests → failure" is not the dominant pattern. `git` operations (commit, push, checkout) precede more than a third of failure reports. Likely: agent makes a commit or branch operation, it fails or produces an unexpected state, developer reports the failure.

### Intent of failure_report turns

- `debug`: 74.8% — the overwhelming majority of failure reports come from debug-intent turns, not from turns where the developer intended something else and got a failure.

### Top LR features for failure_report (unique vs. Q1)

`cum_pushback_count` (+2.87) and `intent_debug` (+1.05) lead. `prompt_char_count` (+1.24) and negative `prompt_word_count` (−0.94) together suggest failure reports use dense short words (error codes, tracebacks) rather than long natural-language explanations.

### Implication for Hedwig

Failure reports are well-predicted by: (a) prior failure count in session, (b) debug intent, (c) elevated bash activity. A check-in trigger along the lines of "agent made ≥2 bash calls AND session has had ≥1 prior failure report" would fire before ≈40% of failure reports. The current Hedwig triggers (file-level) would miss this class almost entirely.

---

## Q3. Does behavior cluster into the 3-4 personas we assumed?

**Direct answer: No.** The data clusters most cleanly into **k=2**, not 3 or 4. The k=2 split is session-length × engagement intensity, not persona type. SWE-chat's three labeled personas (Expert Nitpicker, Vague Requester, Mind Changer) do not correspond to behavioral clusters — all three appear in similar proportions in every cluster.

### Silhouette scores across k

| k | Silhouette | Interpretation |
|---|-----------|----------------|
| 2 | **0.378** | Clear best |
| 3 | 0.209 | Drop — no clean third cluster |
| 4 | 0.223 | Marginal improvement — mostly splitting noise |
| 5 | 0.224 | Same |
| 6 | 0.246 | Small uptick from one tiny outlier cluster (n=2) |

k=2 wins cleanly. k=3+ show only marginal gains that likely reflect outlier sessions, not meaningful behavioral groups.

### What k=2 separates

| | Cluster 0 (n=4,729) | Cluster 1 (n=1,066) |
|---|---|---|
| pushback_rate | 0.296 | 0.442 |
| failure_rate | 0.072 | 0.097 |
| mean_prompt_words | 149 | 213 |
| mean_prev_tools | 2.95 | 13.17 |
| n_turns | 7.6 | 24.9 |
| agent_pct | 63.5% | 49.3% |

Cluster 0: **short sessions, low tool use, low pushback, high agent authorship** — delegate-and-accept style.  
Cluster 1: **long sessions, heavy tool use, higher pushback, more human involvement** — active collaboration.

### Persona labels within each cluster

Both clusters contain Expert Nitpicker (37% vs. 58%), Vague Requester (34% vs. 30%), and Other in comparable proportions. Mind Changer is rare in both (5% vs. 10%). **SWE-chat's persona labels are essentially orthogonal to behavioral session intensity.**

### What k=4 reveals (for completeness)

k=4 adds two distinctions within Cluster 1: one small group of very long sessions (n=184, mean 86 turns) with highest pushback (0.446) — truly intensive collaboration. And a verbose-prompt group (n=586, mean 283 words/prompt) with moderate pushback — developers who write long specifications.

### Implication for Hedwig

The `UserPersona` enum (`expert_nitpicker`, `vague_requester`, `mind_changer`, `unknown`) does not correspond to the natural behavioral clusters in the data. The real behavioral split is **session intensity**: short/delegating vs. long/active. A 2-value enum (`delegating` / `active`) would better describe what the data shows. The current three-value schema is a theoretical construction, not a data-driven one.

---

## Q4. Does behavior change within a session?

**Direct answer: Yes, significantly — but only in predictable directions.** Pushback rate rises through the session. Prompt length and agent tool use decline. This pattern is consistent with developer calibration, but the effect size is moderate.

### Within-session trends (sessions with ≥6 turns, n=2,703 sessions, 54,964 turns)

| Metric | Early third | Mid third | Late third | Trend | Significance |
|--------|------------|----------|-----------|-------|-------------|
| `pushback_is_any` | 0.372 | 0.402 | 0.403 | ↑ | p<0.001 |
| `is_failure_report` | 0.064 | 0.072 | 0.068 | ~ | p=0.18 ns |
| `prompt_word_count` | 202 | 152 | 159 | ↓ | p<0.001 |
| `prev_tool_count` | 5.40 | 4.57 | 4.61 | ↓ | p<0.001 |
| `prev_bash_count` | 1.63 | 1.67 | 1.74 | ↑ | p<0.001 |
| `prev_write_edit_count` | 1.02 | 0.98 | 1.00 | ~ | p<0.001 |

**Key pattern:** Pushback rises through sessions (+8.4% early to late). Prompt length drops significantly (−21%). Total tool use drops but bash use increases — the agent shifts from mixed exploration to more execution-heavy work as sessions progress.

Failure reports specifically do **not** change significantly within sessions (p=0.18). Failures are distributed throughout sessions, not concentrated at any phase.

### Cross-session stability per user

- Users with ≥3 sessions: **128**
- Intraclass correlation (ICC) of pushback rate across sessions per user: **0.249**
- Interpretation: **low stability**. Only 25% of variance in pushback rate is explained by which user it is; 75% is within-user variance across sessions.

Sample: one user (`marcus-sa`) has pushback rates [0.65, 0.55, 0.29, 0.58, 0.38] across sessions — the same person varies from 29% to 65% pushback depending on the task/session.

### Implication for Hedwig

Two distinct findings here:

1. **Session-scoped preferences are justified** for capturing within-session calibration. The rising pushback rate means early-session behavior is genuinely different from late-session behavior, so a `Scope.level = "session"` preference that loosens thresholds after the developer has corrected several times is reasonable.

2. **Repo-scoped (user-level) preferences are not well-supported.** ICC of 0.249 means a developer's behavior is not stable across sessions. Per-repo (per-project) preferences make sense; claiming we can infer a user's style from their history and apply it stably is not supported by this data.

---

## Q5. What do developers actually push back about?

**Direct answer:** Scope-narrowing ("just do X", "don't touch Y"), failure/error reports, and content corrections dominate. A significant fraction (33%) doesn't match any of our assumed categories — mostly meta-level directives, delegations, and multi-part instructions rather than corrections or complaints.

### Category distribution across 21,875 pushback texts

| Category | Count | % |
|----------|-------|---|
| `other` (unclassified) | 7,259 | 33.2% |
| `content_correction` | 6,514 | 29.8% |
| `failure_error` | 3,116 | 14.2% |
| `approach_correction` | 1,501 | 6.9% |
| `style_formatting` | 1,480 | 6.8% |
| `scope_requirements` | 1,145 | 5.2% |
| `timing_pacing` | 656 | 3.0% |
| `clarity_understanding` | 204 | 0.9% |

### Top TF-IDF terms overall

Most discriminative pushback terms: `fix`, `use`, `commit`, `agent`, `test`, `let`, `check`, `make`, `error`, `branch`, `pr`, `review`.

The `pr` (pull request) and `commit`/`branch` terms are striking — a substantial portion of pushback turns are about **git workflow and PR management**, not code quality.

### Per-type distinctions

**Corrections** are centered on directives: `fix`, `use`, `add`, `let`, `make`. Generic instructional language.

**Failure reports** show distinct vocabulary: `image`, `error`, `failed`, `output`, `png`, `failing`, `tests`. The `image`/`png` cluster likely reflects screenshot-based debugging (developers pasting screenshots of failures). `failed`/`failing`/`tests` confirm test failures.

**Rejections** (the smallest class, 635 turns) are centered on `stop`, `commit`, `changes`, `push` — developers telling the agent to stop committing or pushing things they didn't approve.

### What the 33% "other" contains

The verbatim examples reveal this category is **not noise** — it includes:
- Delegation: "can we have another subagent create that one?"
- Partial agreement: "I like it, make it so"
- Multi-part instructions that reassign rather than correct

These are **positive directives** after seeing agent output — neither corrections nor rejections. They represent a turn type our taxonomy doesn't currently capture: **redirect without dissatisfaction**.

### Implication for Hedwig

Hedwig's `PushbackType` enum misses two important real categories:
1. **Scope narrowing** ("just do X", "don't touch Y", "only") — currently absorbed into `correction` but behaviorally distinct. These are constraints, not corrections.
2. **Positive redirect** ("I like it, now do X") — approval combined with new instruction. Currently categorized as `non_pushback` or `correction` but it's neither. This is the "redirect without dissatisfaction" type that the 33% unclassified block suggests.

The absence of a dimension for *what the developer objected to* (the object/subject of pushback, not just its valence) is confirmed. Leijie's PhD feedback about "developer-intent labeling" is supported: developers push back about PR/commit workflow, scope constraints, and implicit redirects in roughly equal measure — none of which is captured by correction/rejection/failure_report.
