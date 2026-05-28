# Hedwig End-to-End

A deep-dive reference for understanding Hedwig's architecture and design decisions. File references included so you can open the source when a question gets specific.

## 1. Elevator pitch

Hedwig is a **harness** that wraps a coding agent (Claude on Bedrock).
It does not generate code. For every agent-proposed action on a file it
decides — autonomously and per stage — whether to proceed or pause for
the developer. The decision is calibrated from real interaction traces:
a deterministic risk assessor, a heuristic scorer for cold start, an
online logistic classifier with isotonic calibration that takes over
after ten real decisions, and a hypothesis bank that lets the developer
confirm or reject inferred preferences before they ever affect behavior.
Everything is local and per-repo. The taxonomy and thresholds are
grounded in an empirical study of real coding-agent sessions
(see §5 for methods; specific findings are cited inline below).

## 2. The five-concept anchor

- **Action** — one agent-proposed operation on one file: `read`, `write`,
  `patch`, `verify`. Atomic unit of oversight.
- **Stage** — `read` / `plan` / `apply` / `verify` / `report`. Authority is
  granted per stage; a lease that lets the agent read freely says nothing
  about apply.
- **Decision trace** — immutable per-action row in `decision_traces`
  (SQLite). Captures the action, the scorer that fired, the developer's
  decision, pushback, regret. The substrate everything else reads from.
- **PolicyScorer** — seam in `sc/policy.py` with two adapters:
  `HeuristicScorer` (cold-start, weighted priors) and `PolicyClassifier`
  (online logistic + isotonic calibration). Both consume a single
  `RiskSignals` object and return the same `PolicyDecision`.
- **Preference** — the 5-dim taxonomy
  (Trigger / Condition / PreferenceAction / Scope / Lifecycle) in
  `sc/preferences.py`. Matched per file per action *after* the scorer
  decides. Tightens by default; developer-confirmed `auto_apply` preferences can loosen on low-risk writes.

## 3. End-to-end flow of one task

One task typed in the REPL from keystroke to wrap-up:

1. **Entry and prompt assembly** — `sc/run/repl.py::run_repl` (REPL). System prompt is built by
   `sc/prompt_builder.py::build_run_system_prompt`, which pulls four
   relevance-ranked categories from SQLite via `RuleStore`:
   `relevant_logic_notes` (rule_store.py:360) — developer-stated repo
   facts plus LLM-inferred facts auto-stored with cited evidence;
   `relevant_behavioral_guidelines` (rule_store.py:467) — developer-stated
   style rules plus LLM-inferred style patterns confirmed by the developer;
   `relevant_feedback_snippets` (rule_store.py:528) — verbatim developer
   corrections auto-accumulated from prior sessions.
   Hard-constraint text, active leases, and the autonomy mode are also
   folded in. Before per-task ranked snippets, the prompt opens with a
   synthesized "What we've learned about this repo" paragraph.

2. **Read stage** — agent emits `read_request` JSON
   (`sc/schema.py::ReadRequest`). `sc/run/read_stage.py::_process_read_request`
   runs each file through the cascade in `helpers._resolve_pre_scorer`
   (hard constraints → read leases) followed by
   `helpers._policy_decision_for_file` (scorer). Files that need
   developer input are batched into a single prompt
   (`sc/run/ui.py::_prompt_read`) with four options:
   `a` approve all, `r` approve+remember all, `s` select per-file
   (each file gets `a / r / d`, default `a`), `d` deny all. Per-file
   denial via `s` records the rejected subset with
   `pushback_type=scope_constraint` so the hypothesis bank treats
   partial denial as a scope-narrowing signal, and the agent gets a
   system message listing the denied paths so it doesn't re-request
   them. Every read writes a row with `stage="read"`.

3. **Plan + plan_gate** — agent emits an `intent_declaration`
   (`sc/schema.py::IntentDeclaration`); `sc/plan_gate.py::evaluate_plan`
   computes the planned blast radius from declared files+actions and
   shifts authority before any apply call fires. Plan-stage pushback can
   also surface a hypothesis early — see `apply_stage.py:546`.

4. **Apply stage cascade** — `sc/run/apply_stage.py::_evaluate_apply_stage`,
   per file, layer by layer:

   1. **Hard constraints** (`sc/constraints.py`, stored in
      `trust_db.hard_constraints` via `RuleStore`). Override everything;
      a `always_deny` here ends the file's flow.
   2. **Active leases** (`trust_db.leases`). A lease is a temporary
      trust grant from a prior approval; if one matches, the action is
      auto-approved without a scorer call.
   3. **Threshold adaptation** —
      `sc/autonomy.py::adjusted_policy_thresholds` computes the
      proceed/flag thresholds *before* the scorer runs. Four additive
      shifts: (1) persistent mode (`autonomous` loosens by 0.25, `strict`
      tightens; path/topic scoping adds 0.10); (2) model check-in
      calibration (if the agent has initiated ≥5 check-ins and the
      developer rejected >60% of them, thresholds rise by 0.15);
      (3) session intensity (`hands-on`/`active` +0.08, `delegating`
      −0.05); (4) coding mode (`vibe` +0.06, human-authored −0.04).
   4. **PolicyScorer** — `assess_risk` (`sc/features.py:107`) produces a
      `RiskSignals`; `assess_risk_via_model` (`sc/model_risk.py`) adds
      an advisory `model_risk_score`; `select_scorer` picks the
      heuristic or learned adapter; `score()` returns a raw float.
      That score is then compared against the *adjusted* thresholds from
      step 3 to produce a `PolicyDecision` (`proceed` / `check_in` /
      `flag_for_review`). When the second-opinion reviewer produced a
      rationale, it appears as a dim `reviewer note` line in the
      check-in panel before the approve/deny prompt.
   5. **Preference override** —
      `sc/run/preference_coordinator.py::PreferenceCoordinator` matches
      the 5-dim `Preference` rows against the action and **tightens**
      the scorer's verdict if any matches (`_apply_forced_action`).
      Developer-confirmed `auto_apply` preferences can loosen a
      `check_in` to `proceed` on low-risk writes (diff < 20, blast
      radius ≤ 2, not security-sensitive, not new file). All other
      preferences only tighten.

   The decision is rendered to the developer (`apply_ui.py`); on apply,
   the file is written atomically and a regret check runs.

5. **Verify** — agent emits a `file_update` followed by verification
   commands; `sc/verification.py` runs them and a failure here is
   replayed as negative classifier signal in the next regret pass.

6. **Report and trace recording** — `sc/run/reporting.py` summarizes
   the turn; every action above writes through `trust_db.record_trace`
   (in `sc/store/trace_store.py`) so the next turn's scorer, hypothesis
   bank, and observability commands can see it.

## 4. The scoring stack

- **`assess_risk` and `RiskSignals`** (`sc/features.py:107`) — the only
  place change-pattern categories live (`CHANGE_PATTERNS`). Pure data
  in, pure data out: blast radius, change pattern, diff size, edit
  distance, model_risk_score, model_risk_rationale. Deterministic.
- **`assess_risk_via_model`** (`sc/model_risk.py`) — second Bedrock
  pass with a different system prompt, **no access to the agent's
  intent_declaration**. Returns `(score in [0,1], rationale)`; defaults
  to `(0.5, "")` on every failure path (Bedrock down, parse failure,
  schema mismatch, timeout). Advisory only — never load-bearing.
- **`should_review`** (`sc/model_risk.py`) — gate in front of the
  reviewer call to keep Bedrock cost in check. Returns True only if at
  least one trigger holds: new file, security-sensitive,
  `blast_radius >= 4`, `diff_size >= 80`, change_pattern in
  {`api_change`, `data_model_change`, `config_change`,
  `dependency_update`}, or no prior history on this file. Otherwise the
  reviewer is skipped and the score stays at the 0.5 no-opinion default
  (which contributes zero to the heuristic). The reviewer runs whenever
  `should_review()` returns True — no hard cap. The gate itself is the
  cost control: familiar, low-risk, well-trusted files are never sent
  to the reviewer. Net effect: roughly 50–70% of files in a typical
  apply stage skip the reviewer entirely.
- **`HeuristicScorer`** (`sc/policy.py`) — cold-start; weighted sum of
  RiskSignals + a ±0.3 nudge from `model_risk_score` around the 0.5
  no-opinion midpoint. Weights are documented in `SPEC.md`'s weight
  table; they are priors, not tuning knobs.
- **`PolicyClassifier`** (`sc/ml_policy.py`) — `SGDClassifier(loss="log_loss")`
  over the 14-feature vector (see `FEATURE_NAMES`). `update()`
  partial-fits on each real decision; an `IsotonicRegression`
  calibrator refits once 20 real decisions accumulate
  (`_CALIBRATION_MIN_SAMPLES=20`). The classifier is pickled and
  reloaded from SQLite every apply turn.
- **`select_scorer`** (`sc/policy.py`) — picks the learned adapter
  iff `PolicyClassifier.ready()` returns true
  (`sample_count >= MIN_SAMPLES_FOR_LEARNED=10`,
  `sc/ml_policy.py:56`). Which adapter fired is recorded in
  `PolicyDecision.reasons` and lands in
  `decision_traces.policy_reasons` so heuristic-era and learned-era
  traces can be separated post-hoc.
- **`adjusted_policy_thresholds`** (`sc/autonomy.py`) — computes the
  proceed/flag thresholds before the scorer runs. The scorer produces a
  raw score; that score is compared against these adjusted thresholds
  to produce the final decision.

  These shifts respond to **session-level signals** — how engaged the
  developer is, how much of the code is agent-authored, how the current
  session is going. This is deliberately separate from the classifier,
  which responds to **file-level signals** (what files, what change
  patterns, what history). The SWE-chat analysis showed these are
  orthogonal axes that operate on different timescales: file-level trust
  accumulates across sessions; session-level context resets each turn.

  The shifts are **hand-coded constants, not learned**, for a specific
  reason: the classifier needs real decisions to learn anything, but
  session-level responsiveness is needed from turn 1. Encoding the
  SWE-chat findings directly (active sessions warrant more caution;
  agent-led sessions are riskier) gives correct behavior immediately
  without waiting for data. A unified model that learns both file-level
  and session-level weights jointly would require far more decisions
  than the cold-start scenario provides, so the hand-coded constants are
  an inductive bias injection.

  Four additive shifts:
  1. **Persistent mode** — `/config set-mode autonomous` sets
     `prefer_fewer_checkins=True`, which shifts proceed/flag down by
     0.25 (loosening). `strict` tightens. Path and topic scoping add
     a further 0.10 loosening when active.
  2. **Model check-in calibration** — if the agent has initiated ≥5
     check-ins and the developer rejected >60% of them (the agent keeps
     proposing things the developer doesn't want to proceed with),
     thresholds rise by 0.15 — Hedwig becomes more conservative overall
     because the session has shown the agent's judgment can't be trusted.
  3. **Session intensity** — `/oversight hands-on` (or auto-inferred
     `active`) raises thresholds by 0.08 (more pauses).
     `/oversight delegating` lowers by 0.05 (fewer pauses). Small
     delta — the scorer still dominates.
  4. **Coding mode** — agent-led sessions (`vibe`) raise thresholds
     by 0.06. Human-authored sessions lower by 0.04.

  All four shifts compose additively. Floor is −0.5; flag threshold
  is capped at proceed threshold so the ordering is preserved.
- **Borderline model vote** — when the scorer's raw score is within
  0.25 of the proceed threshold (genuinely uncertain), and the
  the score is genuinely uncertain, Hedwig makes one additional Bedrock
  call: a separate system prompt asking "should a developer review
  this?" (`ask_model_to_vote` in `sc/model_risk.py`).
  The response nudges the score by up to ±0.15 and re-buckets the
  decision. On failure the score is unchanged. This fires only on
  borderline cases — clear auto-proceeds and obvious check-ins are
  untouched, so it adds latency only when uncertainty is real.

## 5. Empirical grounding — the SWE-chat study

Most of what follows in §5b through §8 only makes sense if you know
the study underneath it. Hedwig's original taxonomy was theory-driven
(four personas, four pushback categories, per-developer preferences).
Before investing further in it, we tested it against real coding-agent
sessions in the wild.

**Dataset.** **5,776 sessions / 62,544 developer turns / 355K agent
tool calls / 2.7M logged interactions** drawn from 200+ public GitHub
repos via the SWE-chat dataset. Opt-in developers, real CLIs, real
work. Each turn carries a pushback label (correction / rejection 
/ failure_report / non_pushback) andeach session carries persona + 
coding-mode labels, **inherited from the SWE-chat dataset**. We did not run our own 
labeling pass.

**Methods.** Five research questions, each with a method picked for
**interpretability over performance** — we wanted to know *which*
signals matter, not ship a classifier.

| Q | Method | Why this method |
|---|---|---|
| What predicts pushback? | Logistic regression, 5-fold CV; decision tree as cross-check | LR coefficients are directly interpretable; agreement with a different model family rules out single-algorithm artifacts |
| Can we predict failure reports specifically? | Same LR, targeting `failure_report` | `failure_report` is the one pushback class with content-grounded labels; results generalize cleanest there |
| Do developers cluster into the 3 personas the schema proposes? | K-means on session-level features, k = 2..6, **silhouette score** to pick k | Clustering asks "does this data have natural structure without labels?" Silhouette handles "should there be 2 or 5 clusters?" cleanly |
| Are preferences stable across sessions for the same developer? | **Intraclass correlation (ICC)** across 128 developers with ≥3 sessions each; chi-squared / t-tests for within-session trends | ICC quantifies how much of total variance is explained by group identity (here, the developer). Standard answer to "is this a stable property of the person?" |
| What's the missing 33% of pushback? | V2: TF-IDF + K-means + manual labels. V3: sentence-transformer embeddings (`all-MiniLM-L6-v2`) + K-means at k=10 | TF-IDF is cheap and inspectable for surface vocabulary; embeddings catch meaning the 33% bucket couldn't be split by lexically |

**ICC (Intraclass Correlation Coefficient)** — proportion of total variance explained by group identity. ICC = 1 means group membership explains everything; ICC = 0 means it explains nothing. We got **ICC = 0.249** on pushback rate across 128 developers, meaning only 25% of variance is between developers; 75% is within-developer across their own sessions. One developer's pushback rate ranged from 29% to 65% across five sessions. That's the empirical answer to "should preferences be per-developer?" — see §6.

**Process note.** The analysis was executed by a Claude-Code-based
research agent (Sonnet 4.6) directed by a written brief. The agent
implemented the methods and extended feature lists; a human
researcher interpreted the findings.

**Findings are cited inline below**, in the section where each one
shaped the design. The short summary, in case you want it once
before moving on:

- File-level risk is a weak pushback predictor; **session-level state
  dominates** → drives the feature set in §5b.
- `debug_intent + prior_failure` predicts future failure at **AUC 0.90**
  → `FAILURE_SIGNAL_CHECKIN` (§6, §7).
- Behavioral clustering shows **k=2, not k=3** → `UserPersona` collapsed
  to `active` / `delegating` (§5b).
- **ICC = 0.249** → preferences are per-repo, not per-developer (§6, §13).
- 33% of pushback **isn't pushback at all** — it's a separate "what
  is this turn for" dimension → `TurnPurpose` added alongside
  `PushbackType` (§5b).

## 5b. Session signals

All in `sc/preferences.py` and inferred in `sc/preference_inference.py`.

- **CodingMode** (`human_only` / `collaborative` / `vibe`) — inferred
  from **edit distance**: how much the developer modified the agent's
  proposed output per trace. `avg_edit_distance < 0.1` → `vibe`
  (developer accepts agent output as-is); `> 0.5` → `human_only`
  (developer rewrites most of it); middle range → `collaborative`.
  Inferred in `infer_coding_mode` (`sc/preference_inference.py`);
  consumed by `adjusted_policy_thresholds` (`vibe` +0.06,
  `human_only` −0.04). Does not currently filter hypothesis
  generators — that's intended future work (vibe sessions producing
  different candidate patterns than human-only sessions).
- **UserPersona** (`active` / `delegating` / `unknown`) — inferred
  from **turn count** and **tool-calls-per-turn** (how many Bedrock/
  bash calls the agent made per turn on average). `active` if turns
  ≥ 12 OR tool-calls-per-turn ≥ 6.0; `delegating` if below both.
  Inferred in `infer_user_persona`; consumed by `adjusted_policy_thresholds`
  (`active` +0.08, `delegating` −0.05).
  **From SWE-chat (§5):** the original schema had four persona types.
  K-means with silhouette scoring across k = 2..6 found the cleanest
  split is **k=2** — the four labels appear in both clusters in
  similar proportions, so they're orthogonal to behavior. The two
  clusters are differentiated by *session intensity*, not persona
  type. Cluster centers (active ≈ 24.9 turns, delegating ≈ 7.6 turns)
  are the literal thresholds in `preference_inference.py:28`.
- **PushbackType** — six values: `correction`, `rejection`,
  `failure_report`, `non_pushback`, `positive_redirect`,
  `scope_constraint`. Inferred per turn by `classify_pushback()` —
  rule-based string matching, no model call. Stored in every
  `decision_traces` row. Consumed in four places:

  1. **Regret detector** — `failure_report` only. A failure report
     on any turn after an auto-approved write on the same file becomes
     a regret event. Explicit denials are caught via `user_decision`
     separately.
  2. **Session summary counts** — rolls into `n_denials` (rejection),
     `n_failures` (failure_report), `n_feedback` (correction +
     scope_constraint). These counts feed `Condition` matching in
     preference triggers — e.g. `min_prior_pushback_count=2`.
  3. **Rule-based generators** — `scope_constraint` is the most
     consumed: three scope-narrowing responses on multi-file writes →
     candidate "pause before bundling tests with service code."
     `positive_redirect` is stored but no rule-based generator
     currently produces candidates from it (parked).
  4. **LLM noticer** — raw pushback types are in the trace digest so
     the noticer can propose hypotheses across all six types even when
     rule-based generators don't cover them.

  **From SWE-chat (§5):** the original four-category schema left 33%
  of turns unclassified. `positive_redirect` and `scope_constraint`
  were discovered by re-clustering that bucket; both are now
  first-class enum values.
- **TaskIntent** (`debug` / `refactor` / `create` / `test` /
  `understand` / `other`) — inferred from the prompt by
  `infer_task_intent`. `debug` is the strongest pushback predictor;
  thresholds and prompt assembly both react.
  **From SWE-chat (§5):** Q1's logistic regression identified
  `debug` intent as the dominant pushback predictor in coefficient
  magnitude — that's why it gets dedicated handling here and feeds
  the failure-signal trigger (§7).
- **TurnPurpose** (`correction_or_directive` / `context_provision` /
  `structured_spec_input` / `session_continuation` / `other`) —
  orthogonal to PushbackType. Distinguishes "developer pasted an
  error log" (context_provision) from "developer wants a correction."
  **From SWE-chat (§5):** V3 re-clustered the 33% unclassified bucket
  using sentence-transformer embeddings at k=10. Silhouette = 0.038
  — *no single missing category*. The bucket decomposes into ~5
  loose types (multi-part directives 24%, context provision 18%,
  technical follow-up 14%, git workflow 10%, meta-instruction 9%).
  The insight: most of the 33% **isn't pushback at all** — it's a
  separate dimension, *what a turn is for*. That's why TurnPurpose
  exists as its own enum rather than as more PushbackType values.

## 6. The two preference surfaces

There are two systems.

- **`AutonomyPreferences`** (`sc/autonomy.py`) — coarse, repo-scoped
  toggles: `prefer_fewer_checkins`, `allowed_checkin_topics`,
  `scoped_paths`. Consumed **before** the scorer fires by
  `adjusted_policy_thresholds`. Threshold shift only.
- **`Preference`** (`sc/preferences.py`) — 5-dim
  (Trigger / Condition / PreferenceAction / Scope / Lifecycle).
  Matched **after** the scorer decides by
  `PreferenceCoordinator._apply_forced_action`. Action override only,
  and only ever tightening.

The bridge: `autonomy_prefs_to_preferences()` translates legacy
toggles into `Preference` rows when the new surface needs them; the
inverse never runs. **Safety invariant:** preferences add caution;
they never remove it. `prefer_fewer_checkins` reaches the cascade
only as a threshold shift, never as a post-scorer loosening.
`auto_apply` is a no-op at the override layer for the same reason.

**`_apply_forced_action` — how preference overrides work.**
This function takes the scorer's decision and the highest-priority matched preference action
(selected by `force_action_from_preferences`, which picks the most restrictive:
`FULL_CHECKIN=3 > SOFT_CHECKIN=2 > AUTO_APPLY=1`), and returns a possibly-modified decision:

- `full_checkin` — upgrades any non-`check_in` decision to `check_in`.
- `soft_checkin` — upgrades any non-`check_in` decision to `proceed_flag` (5s countdown).
- `auto_apply` — can loosen a `check_in` to `proceed`, but only under strict conditions
  (see below). Otherwise defers to the scorer's original decision.

**`auto_apply` conditions.** A developer-confirmed `auto_apply` preference
(`provenance` in `user_explicit` or `inferred_user_confirmed`) loosens a `check_in` to
`proceed` only when ALL of:
- `diff_size < 20`
- `blast_radius <= 2`
- `is_security_sensitive == False`
- `is_new_file == False`

If any condition fails, the scorer's `check_in` is preserved. Built-in defaults
(`provenance="default"`) can never loosen regardless of conditions.

The logic: a developer who explicitly sets (or confirms a hypothesis for) "auto-apply
writes to `utils/helpers.py`" has made a conscious judgment. Hedwig respects that judgment
on genuinely low-risk changes. Anything structurally risky — security-sensitive paths, new
files, large diffs, high blast radius — still pauses regardless of the preference.

**For most cases, loosening belongs at the threshold layer.** `/config set-mode autonomous`
(−0.25) or `/oversight delegating` (−0.05) shift the bar before the scorer runs, so the
scorer still evaluates all risk signals against a more permissive bar. `auto_apply`
preferences are for specific paths where the developer has decided the scorer's verdict on
low-risk writes doesn't need their attention.

**Why per-repo and not per-developer.** **From SWE-chat (§5):** the
ICC analysis on 128 developers with ≥3 sessions gave **ICC = 0.249**
on pushback rate — only 25% of variance is between developers, 75%
is within-developer across their own sessions. A per-developer
preference would mostly encode session-level noise. The repo is the
stable target: `trust.db` keys on `repo_root`, and both preference
surfaces above are scoped to it. This is also the reason inferred
preferences default to session scope and only persist when the
developer confirms them via the hypothesis bank.

## 7. Hypothesis bank — how a hypothesis is formed

The section where the "learning without overclaiming" claim is grounded. All in
`sc/hypothesis_bank.py`.

**One built-in is data-grounded out of the box.**
**From SWE-chat (§5):** Q2's logistic regression found that
`debug intent + prior failure report` predicts future failure
reports at **AUC 0.90** — well above the AUC 0.75 we got predicting
pushback in general. It lives as `FAILURE_SIGNAL_CHECKIN` in `sc/preferences.py` 
— a built-in `Preference` row that fires a soft check-in when both signals are
present in the current session. It's the one place where Hedwig acts on the SWE-chat 
data directly rather than waiting for repo-local evidence.

Two generators feed candidates into `hypothesis_candidates`:

- **Rule-based generators** (`sc/preference_inference.py`) — pattern
  matchers that emit candidate `Preference` shapes when session
  signals fit (e.g. three rejections of writes to `*.md` in the same
  session → candidate "always check-in on docs writes").
- **LLM noticer** (`maybe_generate_llm_hypotheses`,
  `sc/hypothesis_bank.py`) — fires every `LLM_GENERATION_INTERVAL=5`
  turns when there are at least `MIN_EVIDENCE=3` traces. Sends Bedrock
  a digest of recent `decision_traces` rows. Proposes up to 3 items
  across three output types (see below).

**The noticer proposes three output types.** All require cited trace
IDs — hallucinated cites are dropped before storage.

1. **`logic_note`** — a fact about the codebase visible from how
   files are used: *"tests live in `demo_recipe_api/tests/`"*,
   *"`models.py` and `store.py` always change together"*. Auto-stored
   directly into `rule_store` with `source="llm_inferred"` — no
   developer confirmation needed. Appears in the agent's prompt on
   the next relevant task.

2. **`behavioral_guideline`** — a coding style pattern the developer
   consistently enforces. Surfaces for confirmation: *"Save this as
   a coding style guideline?"* Confirmed → written to `rule_store`
   and retrieved into the agent's prompt. Declined → silently
   skipped. Visible in `/prefs` under "Learned style guidelines."

3. **`preference`** — a governance rule about when to pause.
   Goes through the full evidence accumulation loop and surfaces via
   `/prefs`. Confirmed → becomes a `Preference` row that fires
   deterministically in the apply cascade.

**Citation requirement.** Every LLM candidate must cite real
`decision_traces.id` values. Uncited or hallucinated cites are
filtered out before storage. Candidates that arrive citing
≥ `MIN_EVIDENCE` valid traces can promote directly to
`ready_to_surface`, bootstrapped from concrete prior history.

**Evidence accumulation** (preferences only). Each new trace runs
through `update_evidence`: candidates whose Trigger fits get +1 for
or +1 against based on the developer's decision. Confidence is
`evidence_for / total`. Logic notes and behavioral guidelines bypass
this loop — they're observations, not patterns to accumulate
evidence for.

**Surfacing and pruning thresholds** (preferences only).

- `SURFACE_CONFIDENCE = 0.70` — surface when confidence ≥ this and
  total ≥ floor.
- `PRUNE_THRESHOLD = 0.30` — prune when confidence ≤ this and
  total ≥ floor.
- `MIN_EVIDENCE = 3` — default floor.
- `high_stakes` — raises the floor to `2 * MIN_EVIDENCE = 6` for
  preferences whose mis-application would be costly.

**Confirmation flow.** Preference candidates appear in `/prefs` with
their cited traces and a confidence bar. Developer says yes → a
`Preference` row is written. Developer says no → stays in the bank
for transparency, stops accumulating evidence.

**Nothing affects behavior until the developer confirms** (or in the
case of logic notes, until the noticer has cited evidence). The only
learning that touches behavior is: (a) the classifier (developer as
labeler), (b) confirmed governance preferences (developer as
gatekeeper), (c) confirmed behavioral guidelines and auto-stored
logic notes (both grounded in cited trace evidence).

## 8. Regret loop

`sc/regret.py::detect_regret_events` walks a session's traces in
order. An **auto-approved** action followed by any of: a denial of a
related action, a `failure_report` pushback, or a verification
failure becomes a `RegretEvent`.
`apply_stage._apply_regret_corrections` replays each event as
`classifier.update(pi, approved=False, count_sample=False)` —
`count_sample=False` because the corrective gradient is not a new
developer decision and must not push the classifier past
`MIN_SAMPLES_FOR_LEARNED`. Each regret fires exactly once: the
`_corrected_regret_ids` set is **persisted with the pickle** and
checked on every reload. Regret events also surface in
`/retrospective` (`sc/run/retrospective.py`) and the HTML export.

## 9. Context retrieval — what Hedwig pulls into every prompt

Every task rebuilds the system prompt from scratch by reading
repo-scoped memory. This is what makes Hedwig feel like it
*remembers* your project across sessions without ever claiming to
"learn." All retrieval lives in `sc/prompt_builder.py::build_run_system_prompt`.

**The four retrieval categories** (each pulled from `RuleStore` in
`sc/store/rule_store.py`):

- **Logic notes** (`relevant_logic_notes`, rule_store.py:360) —
  repo-specific facts. Two sources: developer-stated via `/rules add`
  (*"tests live in `tests/`, not `test/`"*) and LLM-inferred by the
  hypothesis noticer from trace patterns with cited evidence
  (*"`models.py` and `store.py` always change together"*).
  Auto-stored on inference — no confirmation needed. Limit: 3 per task.
- **Behavioral guidelines** (`relevant_behavioral_guidelines`,
  rule_store.py:467) — how the agent should write code. Two sources:
  developer-stated (*"Explain before patching"*, *"Avoid speculative
  refactors"*) and LLM-inferred style patterns confirmed by the
  developer (*"Developer prefers small focused functions"*).
  Limit: 6 per task.
- **Feedback snippets** (`relevant_feedback_snippets`,
  rule_store.py:528) — verbatim developer corrections from past
  sessions, auto-accumulated from `user_feedback_text` in
  `decision_traces`. *"You said: 'don't add error handling for things
  that can't fail.'"* Limit: 4 per task.

**How rules are classified.** When a developer uses `/rules add`, a
model call (`client.compile_rule` in `sc/agent_client.py`) classifies
the plain-English text into one or both of: `constraints` (path-
enforceable — produces `HardConstraint` objects) or
`behavioral_guidelines` (prose-level guidance — stored in the rule
store). Logic notes come from explicit `/rules add` facts or are auto-inferred by
 the LLM noticer. Governance preferences come only from the hypothesis bank
 or built-indefaults — not from `/rules add`.

**Ranking.** Keyword overlap between the task prompt and each candidate
row's text. The implementation is in `rule_store.py`; ties are broken
by recency. The fixed per-category budget means a long history
doesn't crowd out new context.

**What else gets folded in** (top of the system prompt, not
keyword-ranked because they're always relevant):

- Hard constraints from `trust_db.hard_constraints` (always shown)
- Active leases (so the agent knows what it's already trusted on)
- `AutonomyPreferences` mode and `prompt_lines()` output
- Trust summary: high-trust areas, low-trust areas, corrected patterns
- A 40-file `_repo_file_tree` snapshot so the agent uses real paths
- Workflow phase guidance (`research` / `planning` / `implementation` / `review`)
- Calibration signal — *"your recent check-ins were mostly accepted,
  keep surfacing high-impact decisions"* vs. *"your check-ins are
  often denied, ask fewer."*

**The session-start summary** (`synthesize_repo_summary`,
`prompt_builder.py`). Before the per-task ranked snippets, the
prompt opens with a short paragraph: *"Confirmed preferences: I'll
check in before multi-file changes; I'll soft-check-in on small
follow-ups. Repo facts: tests live in tests/; recipes are seeded
with id recipe-1..4. Recent developer feedback: avoid speculative
refactors."* Pure string templating over the top 3 confirmed
preferences (humanized via `_humanize_preference`), top 2 logic
notes, and 1 most-relevant feedback snippet. No Bedrock call, no
new query — composed from data already retrieved. Empty string when
nothing meaningful to say (fresh repo). The agent gets a coherent
high-level picture before the detail; reasoning quality on the
first turn improves because it isn't piecing the picture together
from bullets. Surfaced in `/context` as a *"What we've learned about
this repo"* lead block.

**Surfacing retrieval to the developer.** Three layers:

1. **Apply-panel inline line** (`sc/run/apply_ui.py::render_context_retrieved_line`).
   One dim line under every apply policy snapshot:
   *"Context retrieved: 3 repo notes, 2 guidelines, 1 past correction."*
   Singular/plural handled. Suppressed silently when nothing was retrieved.

2. **`/context` REPL command** (`sc/run/repl.py`). Full panel showing
   the task, then bulleted sections — repo notes, behavioral
   guidelines, past developer feedback — each item truncated to 100
   chars. Footer: *"Ranked by keyword overlap with the task."* Use
   it to walk through what the agent actually saw on the previous turn.

3. **Capture mechanism** (`sc/run/context_capture.py`). A
   process-local singleton (`_LAST: LastContext`) that
   `build_run_system_prompt` writes to immediately after retrieval.
   No schema change, no migration — the data lives only for the
   lifetime of the REPL process. Read-only consumers
   (`render_context_retrieved_line`, `/context`) read from it
   downstream.

**The session-read line** (`sc/run/apply_ui.py::render_session_read_line`)
sits next to context retrieval in the apply panel. One dim line:
*"Reading session as: refactor task, working alongside you."* Two
plain-English phrases come from translating `TaskIntent` and
`UserPersona`, both of which are inferred per turn (see §5
"Session signals"). Silent if neither is meaningfully set.

## 10. Co-change graph — files that move together

A second axis of "remembering your project" lives in
`sc/cochange.py`. Where retrieval surfaces *what was said* about a
codebase, co-change surfaces *what was done*, which files
historically move together when a developer is working on a task.

**Definition.** A file pair is considered to co-change if both files
appear under the same `task` (apply stage) in `decision_traces`.
*Task*, not `session_id`, is the grouping unit, because:

- A single REPL session can span multiple unrelated tasks.
- Seeded prior history (`seed_demo`) shares one `session_id` by
  construction, which would collapse all co-change pairs.
- The natural unit of "things changed together" is the developer's
  task description, not the wall-clock session.

**The query** (single SQL, indexed columns, sub-millisecond at
demo scale):

```sql
SELECT file_path, COUNT(DISTINCT task) AS n_tasks
FROM decision_traces
WHERE repo_root = ?
  AND stage = 'apply'
  AND file_path != ?            -- not the source file
  AND file_path != '__session__'
  AND task IS NOT NULL AND task != ''
  AND task IN (
      SELECT DISTINCT task FROM decision_traces
      WHERE repo_root = ? AND stage = 'apply'
        AND file_path = ? AND task IS NOT NULL AND task != ''
  )
GROUP BY file_path
HAVING n_tasks >= ?            -- min_count, default 2
ORDER BY n_tasks DESC, file_path ASC
LIMIT ?                        -- default 3
```

It's a derived view over data the trace
store already holds. `cochanged_files()` returns the per-file
adjacency; `cochange_graph()` returns the full repo-level
adjacency dict for visualization.

**Surfacing.** Two layers:

1. **Apply-panel inline line** (`sc/run/apply_ui.py::render_cochange_lines`).
   For each touched file at apply stage, one dim line:
   *"`store.py` — historically co-changes with: `models.py` (2)"*.
   Silent if the file has no co-change history meeting `min_count`.
   A concrete, demonstrable cross-session pattern — the kind of thing
   rules and preferences can't express.

2. **`/cochange` REPL command** (`sc/run/repl.py`). Full graph view:
   each file with co-change history, indented under it the top-3
   neighbors with their session counts. Falls back to a
   *"patterns appear as you edit files together"* message when the
   repo has no qualifying pairs.

Acting on this information is the developer's call. Same governance
 philosophy as the hypothesis bank: surface patterns, let the human confirm.

## 11. Persistence

`sc/trust_db.py::TrustDB` is a layer over five focused mixins
under `sc/store/`:

- `lease_store.py` — `leases` and `read_leases` (temporary trust grants).
- `rule_store.py` — hard constraints, behavioral guidelines, logic
  notes, feedback snippets.
- `trace_store.py` — `decision_traces`, the primary substrate.
- `pref_store.py` — confirmed `Preference` rows and
  `hypothesis_candidates`.
- `model_store.py` — the pickled `PolicyClassifier` per repo.

Local files: `.sc/config.json`, `.sc/trust.db`. Both per-repo.

## 12. Observability

- `/prefs` (REPL) — accepted preferences plus pending patterns with
  evidence bars, and rejected candidates.
- `/weights` (REPL) — learned classifier drift from defaults, with
  ▲/▼ arrows per feature. Shows pre-warmed drift even before the
  learned model activates; switches to "active" label after 10 real
  decisions.
- `/retrospective` (REPL) — session-level regret summary (where
  Hedwig was too loose or too cautious).
- `/cochange` (REPL) — files that historically move together in this
  repo, derived from interaction history.
- `/status` (REPL) — current session read: engagement level, coding
  mode, second-opinion usage, decision model state.
- `/context` (REPL) — what was retrieved from repo memory for the
  last task (logic notes, guidelines, past feedback).
- `/showcase` (REPL) — all of the above in one uninterrupted display;
  leave it up as a live panel while explaining the system.
- `hw observe traces` — raw interaction history browse with filters.
- `hw observe report` — terminal prose summary.
- `hw observe export --html` — single-file researcher-grade HTML
  report: sessions, preferences, hypothesis bank, coefficient drift,
  decision distribution, pushback breakdown. Open in browser as a
  companion panel alongside the terminal.

## 12b. REPL controls — what the developer can do mid-session

Observability is read-only; the REPL also exposes two **controls**:

- **`/oversight [hands-on | balanced | delegating]`**
  (`sc/run/oversight_toggle.py`, dispatched from
  `sc/run/repl.py:355`). User-facing label for the
  inferred **session intensity** that feeds
  `adjusted_policy_thresholds` (§4). Mapping:
  `hands-on → active` (tightens proceed/flag thresholds),
  `balanced → unknown` (no shift),
  `delegating → delegating` (loosens proceed slightly).
  The current label is shown in the REPL banner
  (`sc/run/banner.py:92`). This is the only in-session knob a
  developer has on cascade thresholds without editing config —
  the only in-session knob a developer has on cascade thresholds
  without editing config.
- **`/status`** — calibration snapshot (current oversight, recent
  check-in acceptance, model-initiated-check-in trend).
- **`/context`** — see §9.
- **`/cochange`** — see §10.
- **`/prefs`**, **`/retrospective`** — see §12.


## 13. Q&A

**"Is this RL?"** No. It's a calibrated supervised classifier with
online updates (`SGDClassifier(loss="log_loss")` + isotonic
calibration). The developer is the labeler; every `update()` call
takes a real approve/deny. There is no reward, no environment, no
exploration policy. Regret replay is a corrective gradient on
already-labeled history, not exploration.

**"Why not warm-start the classifier from the heuristic weights?"**
Two reasons. First, the calibration math assumes that every (raw
prob, label) pair came from a real `partial_fit` step against a real
developer decision; mixing in seeded weights breaks the isotonic
fit. Second, auditability: which adapter fired for any given
decision is recorded in `decision_traces.policy_reasons`, so a
researcher can separate heuristic-era from learned-era traces. A
warm start would smudge that boundary. The cold-start handoff at
`MIN_SAMPLES_FOR_LEARNED=10` is the price; it's a small price.

**"What stops the agent from gaming its own risk score?"**
`assess_risk` is deterministic — pure function of the action and
diff. The model-pass reviewer (`assess_risk_via_model`) runs with a
different system prompt and **no access to the agent's
intent_declaration**, so a persuasive intent string can't move it.
Confirmed preferences only ever tighten. The agent's only lever is
to do less risky things; doing them quietly doesn't help.

**"Per-developer preferences?"** No — per-repo. The SWE-chat
study (see §5) settled this empirically. We computed intraclass
correlation on pushback rate across 128 developers with ≥3
sessions each; **ICC = 0.249** — only 25% of variance is between
developers, 75% is within-developer across their own sessions
(one developer's pushback ranged 29%–65% across five sessions).
Per-developer preferences would mostly encode session noise. The
repo is the stable target — `trust.db` keys on `repo_root`, and
that's what Hedwig actually holds.

**"How does Hedwig manage context?"** Two layers. (1) A
session-start synthesized paragraph — *"What we've learned about
this repo"* — composed from the top confirmed preferences,
top logic notes, and the most-relevant feedback snippet. The
agent reads this before anything else, so it walks in oriented
instead of piecing the picture together from bullets. (2) Per-task
keyword-ranked retrieval from four categories: logic notes (repo
facts — developer-stated or LLM-inferred), behavioral guidelines
(style rules — developer-stated or LLM-inferred and confirmed),
hard rules, and developer feedback snippets (verbatim past
corrections, auto-accumulated). Plus a 40-file
repo tree, a non-numeric trust summary, hard constraints, active
leases, and the autonomy mode. Visible to the developer: every
apply panel shows a one-line *"Context retrieved: 3 repo notes,
2 guidelines, 1 past correction"* summary, and `/context` in the
REPL shows exactly what was pulled — including the synthesized
lead paragraph.

**"Does Hedwig learn cross-file patterns?"** Yes — see
`/cochange`. A file pair is considered to co-change if both files
appear under the same task in `decision_traces`. The query is one
SQL statement over data the trace store already holds; no separate
table or migration. At apply stage, each touched file gets a dim
inline line: *"`store.py` — historically co-changes with
`models.py` (2)"*. The graph is descriptive — it doesn't shift
thresholds or seed the classifier — same oversight philosophy as
the hypothesis bank.

**"Doesn't the adversarial reviewer double Bedrock cost?"** It would,
unconditionally — so it isn't unconditional. `should_review` gates
every call: the reviewer fires only when at least one risk signal
warrants a second opinion (new file, security-sensitive, large blast
radius, large diff, structural change pattern, or no prior history on
this file). For familiar, low-risk, well-trusted edits the heuristic
decides on its own and the reviewer never runs. The gate is the cost
control — no hard cap. Roughly 50–70% of apply-stage files in a
typical session don't trigger the gate; cold-start sessions trigger
more (which is correct — that's exactly when an outside opinion adds
the most signal).

**"What's Hedwig's latency profile?"** The write-stage cascade has
three categories of work:

*On the critical path* (developer waits): risk assessment (deterministic,
sub-millisecond), the adversarial reviewer and borderline vote when
they fire (1–3 Bedrock calls per turn in practice, each ~3–8s, gated
by `should_review()` and `is_borderline()`), SQLite reads (sub-millisecond), and the
in-process classifier (pickle reload + sklearn inference, ~1ms).

*Off the critical path — daemon threads*: A daemon thread is a
background thread that runs independently and gets discarded if the
process exits before it finishes. Hedwig uses them for best-effort
work where a missed update is acceptable: (1) **feedback learning** —
after a check-in approval, any natural-language feedback the developer
typed is classified and used to update autonomy preferences; runs in a
daemon thread so the developer is not blocked waiting for a Bedrock
call to finish. (2) **LLM hypothesis noticer** — every 5 turns, a
digest of recent interaction history is sent to Bedrock to surface
novel pattern suggestions; daemon thread, never blocks. (3) **logic
note summarization** — at task end, a summary of what was learned is
written to the repo memory store; daemon thread. If the developer exits
mid-flight on any of these, the work is abandoned — no data is lost
because the underlying interaction traces are already recorded, and a
future session can pick up from there.

*Caching*: The adversarial reviewer caches results in-process keyed on
`(file_path, sha256(diff))` — repeated calls for unchanged content are
free. The cache resets per CLI invocation, which is fine since a given
`(path, content)` pair is reviewed at most once per turn.

## 14. Reading list — files in the order to learn this codebase

Read these in order. Each step builds vocabulary needed for the next.

### Tier 1 — vocabulary and core data shapes

1. `CONTEXT.md` — what is and isn't Hedwig's job.
2. `CLAUDE.md` — domain vocabulary and the safety invariant.
3. `sc/features.py` — `RiskSignals` dataclass + `assess_risk`. Single source of truth for change-pattern categories.
4. `sc/policy.py` — `PolicyInput`, `PolicyDecision`, `PolicyScorer` protocol, `HeuristicScorer`, `select_scorer`.
5. `sc/preferences.py` — the 5-dim `Preference` taxonomy + matching.

### Tier 2 — scoring + adaptation

1. `sc/ml_policy.py` — `PolicyClassifier` (online SGD + isotonic calibration). Note `MIN_SAMPLES_FOR_LEARNED`, `_corrected_regret_ids`, `build_cold_classifier`.
2. `sc/autonomy.py` — `AutonomyPreferences` + `adjusted_policy_thresholds`. The legacy threshold-shift surface that composes with `Preference`.
3. `sc/model_risk.py` — adversarial reviewer + `should_review` gate.

### Tier 3 — the cascade in motion

1. `sc/run/helpers.py` — `_resolve_pre_scorer`, `_policy_decision_for_file`, `infer_session_intensity`. Shared seam between read and apply.
2. `sc/run/read_stage.py` — read cascade end-to-end.
3. `sc/run/apply_stage.py` — write cascade. The big one. Reads cleanly because Tiers 1+2 explain every called helper.
4. `sc/run/preference_coordinator.py` — post-scorer override layer. Re-read the safety invariant in CLAUDE.md while looking at this.
5. `sc/plan_gate.py` — plan-stage authority shift before apply fires.

### Tier 4 — the learning loops

1. `sc/preference_inference.py` — session signals + rule-based candidate generators.
2. `sc/hypothesis_bank.py` — evidence accumulation, LLM noticer.
3. `sc/regret.py` — regret detection from session traces.
4. `sc/run/traces.py` — what gets recorded and why each field exists.

### Tier 5 — UI + observability

1. `sc/run/ui.py` — `_prompt_approval`, `_prompt_read`, `_prompt_optional_feedback`. The terminal contract.
2. `sc/run/apply_ui.py` — apply panel rendering.
3. `sc/run/repl.py` — REPL loop, slash-command dispatch, `_PENDING_TASK_QUEUE` for revise re-issue, three-way reset.
4. `sc/run/retrospective.py` + `sc/commands/observe.py` — the observability surfaces.

### Tier 6 — persistence

1. `sc/trust_db.py` — facade. Skim, then dive into the mixin you care about.
2. `sc/store/trace_store.py` — primary artifact. Every analysis starts here.
3. `sc/store/pref_store.py`, `lease_store.py`, `rule_store.py`, `model_store.py` — focused mixins.

### Tier 7 — the agent boundary

1. `sc/schema.py` — the structured-JSON protocol. Every model output is validated through one of these before the CLI acts.
2. `sc/agent_client.py` — the Bedrock wrapper.
3. `sc/prompt_builder.py` — system prompt assembly with retrieved context.

**If you only have an hour:** 1, 2, 3, 4, 9, 11.
**If you have a night:** all of Tier 1, 2, 3, plus 14, 15, 17, 20.
**If you're prepping to defend the design:** add 6 (calibration
math), 12 (safety invariant), and re-read the regret loop in 16.

For each file: read the module docstring first, then the public
class/function definitions and their docstrings, then dive into one
representative function body. Skip private helpers on first pass.
