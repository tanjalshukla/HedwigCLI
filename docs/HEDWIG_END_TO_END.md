# Hedwig End-to-End

A booth companion to the poster. Audience: a CS researcher who has read the
poster and wants the next 30 minutes of depth — file references included so
they can open the source if a question gets specific.

## 1. Elevator pitch

Hedwig is a **governance layer** that wraps a coding agent (Claude on
Bedrock). It does not generate code. For every agent-proposed action on a
file it decides — autonomously and per stage — whether to proceed or pause
for the developer. The decision is calibrated from real interaction traces:
a deterministic risk assessor, a heuristic scorer for cold start, an online
logistic classifier with isotonic calibration that takes over after ten real
decisions, and a hypothesis bank that lets the developer confirm or reject
inferred preferences before they ever affect behavior. Everything is local
and per-repo; no synthetic training data, no claims of "learning" without
the classifier behind them.

## 2. The five-concept anchor

- **Action** — one agent-proposed operation on one file: `read`, `write`,
  `patch`, `verify`. Atomic unit of governance.
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
  decides; can only tighten, never loosen.

## 3. End-to-end flow of one task

A single `hw run "Add a unit test for foo"` from keystroke to wrap-up:

1. **Entry and prompt assembly** — `sc/run/command.py::run` (single-shot)
   or `sc/run/repl.py::run_repl` (REPL). System prompt is built by
   `sc/prompt_builder.py::build_run_system_prompt`, which pulls three
   relevance-ranked categories from SQLite via `RuleStore`:
   `relevant_logic_notes` (rule_store.py:360),
   `relevant_behavioral_guidelines` (rule_store.py:467), and
   `relevant_feedback_snippets` (rule_store.py:528). Hard-constraint text,
   active leases, and the AutonomyPreferences mode are also folded in.

2. **Read stage** — agent emits `read_request` JSON
   (`sc/schema.py::ReadRequest`). `sc/run/read_stage.py::evaluate_read_stage`
   runs each file through the cascade in `helpers._resolve_pre_scorer`
   (hard constraints → read leases) followed by
   `helpers._policy_decision_for_file` (scorer). Approved reads are
   served; check-ins block. Every read writes a row with `stage="read"`.

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
   3. **PolicyScorer** — `assess_risk` (`sc/features.py:107`) produces a
      `RiskSignals`; `assess_risk_via_model` (`sc/model_risk.py`) adds
      an advisory `model_risk_score`; `select_scorer` picks the
      heuristic or learned adapter; `score()` returns a `PolicyDecision`
      (`proceed` / `check_in` / `flag_for_review`).
   4. **Threshold adaptation** —
      `sc/autonomy.py::adjusted_policy_thresholds` shifts the
      proceed/flag thresholds by `AutonomyPreferences`,
      model-initiated-check-in calibration, and inferred session
      intensity from `helpers.infer_session_intensity`.
   5. **Preference override** —
      `sc/run/preference_coordinator.py::PreferenceCoordinator` matches
      the 5-dim `Preference` rows against the action and **tightens**
      the scorer's verdict if any matches (`_apply_forced_action`).
      `auto_apply` is a deliberate no-op at this layer — preferences
      never loosen.

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
- **`HeuristicScorer`** (`sc/policy.py`) — cold-start; weighted sum of
  RiskSignals + a ±0.3 nudge from `model_risk_score` around the 0.5
  no-opinion midpoint. Weights are documented in `SPEC.md`'s weight
  table; they are priors, not tuning knobs.
- **`PolicyClassifier`** (`sc/ml_policy.py`) — `SGDClassifier(loss="log_loss")`
  over the 7-feature vector (see `FEATURE_NAMES`). `update()`
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
- **`adjusted_policy_thresholds`** (`sc/autonomy.py`) — shifts
  proceed/flag thresholds by: (a) `AutonomyPreferences`
  (`prefer_fewer_checkins` loosens proceed; `allowed_checkin_topics`
  re-tightens around topic), (b) model-initiated-check-in
  calibration (if the model has been crying wolf, the threshold for
  its check-ins rises), (c) session intensity
  (`active` persona tightens, `delegating` loosens slightly).

## 5. Session signals

All in `sc/preferences.py` and inferred in `sc/preference_inference.py`.

- **CodingMode** (`human_only` / `collaborative` / `vibe`) — agent
  authorship ratio per session. Inferred in `infer_coding_mode`;
  consumed by hypothesis generators to filter which patterns are
  even plausible (vibe sessions get different rules than human-only).
- **UserPersona** (`active` / `delegating` / `unknown`) — interaction
  intensity (turn count, pushback rate, agent authorship). Inferred
  in `infer_user_persona`; consumed by `adjusted_policy_thresholds`.
  Reviewer-148D: this is **not** a persona type, just a 2-value
  intensity split — that's all the data supports.
- **PushbackType** — six values: `correction`, `rejection`,
  `failure_report`, `non_pushback`, `positive_redirect`,
  `scope_constraint`. Inferred per turn from the developer's
  message; consumed by the regret detector and the hypothesis bank
  generators (e.g. repeated `scope_constraint` on a path is a
  candidate scoping preference).
- **TaskIntent** (`debug` / `refactor` / `create` / `test` /
  `understand` / `other`) — inferred from the prompt by
  `infer_task_intent`. `debug` is the strongest pushback predictor;
  thresholds and prompt assembly both react.
- **TurnPurpose** (`correction_or_directive` / `context_provision` /
  `structured_spec_input` / `session_continuation` / `other`) —
  orthogonal to PushbackType. Distinguishes "developer pasted an
  error log" (context_provision) from "developer wants a correction."
  Came out of the SWE-chat v3 analysis where 33 % of turns weren't
  pushback at all.

## 6. The two preference surfaces

There are two systems and they don't fight — they compose:

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

## 7. Hypothesis bank — how a hypothesis is formed

The most important section for the booth, because it's where the
"learning without overclaiming" story lives. All in
`sc/hypothesis_bank.py`.

Two generators feed candidates into `hypothesis_candidates`:

- **Rule-based generators** (`sc/preference_inference.py`) — pattern
  matchers that emit candidate `Preference` shapes when session
  signals fit (e.g. three rejections of writes to `*.md` in the same
  session → candidate "always check-in on docs writes").
- **LLM noticer** (`maybe_generate_llm_hypotheses`,
  `sc/hypothesis_bank.py:444`) — fires every
  `LLM_GENERATION_INTERVAL=5` turns when there are at least
  `MIN_EVIDENCE=3` traces. Sends Bedrock a digest of recent
  `decision_traces` rows and a strict JSON schema; parses the
  response with `_extract_json_array`, which uses string-aware
  bracket balancing (a non-greedy regex would lock onto inner
  arrays).

**Citation requirement.** Every LLM candidate must cite real
`decision_traces.id` values. Uncited or hallucinated cites are
filtered out before storage. Candidates that arrive citing
≥ `MIN_EVIDENCE` valid traces can promote directly to
`ready_to_surface` in the same call (`hypothesis_bank.py:573`) —
bootstrapped from concrete prior history, not vibes.

**Evidence accumulation.** Each new trace runs through
`update_evidence`: candidates whose Trigger fits get +1 for or +1
against based on the developer's decision. Confidence is
`evidence_for / total`.

**Surfacing and pruning thresholds.**

- `SURFACE_CONFIDENCE = 0.70` — confidence at or above this and
  total ≥ floor → surface for confirmation.
- `PRUNE_THRESHOLD = 0.30` — confidence at or below and total ≥
  floor → prune.
- `MIN_EVIDENCE = 3` — default floor.
- `high_stakes` (`hypothesis_bank.py:554`) — set by the LLM noticer
  on candidates whose mis-application would be costly; raises the
  floor to `2 * MIN_EVIDENCE = 6`.

**Confirmation flow.** Surfaced candidates appear in `/prefs` with
their cited traces and a confidence bar. Developer says yes →
status flips to confirmed and a `Preference` row is written
(provenance `inferred_user_confirmed`). Developer says no → status
flips to declined; the candidate stays in the bank for transparency
but stops accumulating evidence.

**Hypotheses never affect behavior until the developer confirms.**
This is the load-bearing claim against reviewer-148D's "you're
calling heuristics learning" critique: the only learning that
touches behavior is (a) the classifier (with the developer as the
labeler) and (b) confirmed preferences (with the developer as the
gatekeeper).

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

## 9. Token / context retrieval

`sc/prompt_builder.py::build_run_system_prompt` retrieves three
categories from `RuleStore` and ranks them by relevance to the
current task:

- **Logic notes** — repo-specific facts ("this project's tests live
  in `tests/`, not `test/`").
- **Behavioral guidelines** — soft rules that shape the agent's
  prose ("explain before patching").
- **Feedback snippets** — verbatim developer feedback from past
  sessions, retrieved when phrasing or context lines up.

Ranking is keyword-overlap with the task prompt (see
`rule_store.py`); the budget is fixed so a long history doesn't
crowd out new context. Hard constraints, active leases, and the
AutonomyPreferences mode are folded in on top.

## 10. Persistence

`sc/trust_db.py::TrustDB` is a thin facade over five focused mixins
under `sc/store/`:

- `lease_store.py` — `leases` and `read_leases` (temporary trust grants).
- `rule_store.py` — hard constraints, behavioral guidelines, logic
  notes, feedback snippets.
- `trace_store.py` — `decision_traces`, the primary substrate.
- `pref_store.py` — confirmed `Preference` rows and
  `hypothesis_candidates`.
- `model_store.py` — the pickled `PolicyClassifier` per repo.

Local files: `.sc/config.json`, `.sc/trust.db`. Both per-repo.

## 11. Observability

- `/prefs` (REPL) — accepted preferences plus pending and rejected
  hypotheses with confidence bars; one panel for the whole picture.
- `hw observe traces` — raw `decision_traces` browse with filters.
- `hw observe weights` — learned classifier drift versus the
  cold-start `_PATTERN_RISK` priors.
- `/retrospective` (REPL) — session-level regret summary.
- `hw observe report` — terminal dashboard.
- `hw observe export --html-report` — single-file researcher-grade
  dump (traces + weights + hypotheses + regret).

## 12. Booth-friendly Q&A

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

**"Per-developer preferences?"** No — per-repo. We had this debate
and the data didn't support per-developer: the SWE-chat
inter-coder ICC on style preferences was 0.25, i.e. two passes by
the same coder don't agree. Style isn't stable across sessions for
the same person, so claiming a per-developer preference would be
overclaiming what the trace can support. Per-repo gives stable
ground truth (the codebase) and is what `trust.db` actually holds.

**"What if Bedrock is down?"** Three layers of fallback. The
adversarial-reviewer call (`assess_risk_via_model`) defaults to
`(0.5, "")` — "no opinion" — on every failure path; both scorers
are designed to pass through that midpoint untouched. The LLM
hypothesis noticer is supplemental; the rule-based generators in
`preference_inference.py` keep firing. The classifier and heuristic
are local — pickle on disk, sklearn in-process. Hedwig stays
operational; what disappears is novel hypothesis surfacing and the
adversarial reviewer's pushback, both of which were advisory to
begin with.
