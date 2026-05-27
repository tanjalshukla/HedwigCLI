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
and per-repo.

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
- **`should_review`** (`sc/model_risk.py`) — gate in front of the
  reviewer call to keep Bedrock cost in check. Returns True only if at
  least one trigger holds: new file, security-sensitive,
  `blast_radius >= 4`, `diff_size >= 80`, change_pattern in
  {`api_change`, `data_model_change`, `config_change`,
  `dependency_update`}, or no prior history on this file. Otherwise the
  reviewer is skipped and the score stays at the 0.5 no-opinion default
  (which contributes zero to the heuristic). A per-`hw run` budget of
  **5 reviewer calls** caps pathological "edit 30 files at once" turns;
  when the cap binds, a single dim line surfaces in the apply panel.
  Net effect on a representative apply stage: ~50–70% reduction in
  reviewer call volume vs. unconditional invocation.
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

## 9. Context retrieval — what Hedwig pulls into every prompt

Every `hw run` rebuilds the system prompt from scratch by reading
repo-scoped memory. This is what makes Hedwig feel like it
*remembers* your project across sessions without ever claiming to
"learn." All retrieval lives in `sc/prompt_builder.py::build_run_system_prompt`.

**The three retrieval categories** (each pulled from `RuleStore` in
`sc/store/rule_store.py`):

- **Logic notes** (`relevant_logic_notes`, rule_store.py:360) —
  repo-specific facts the developer has taught Hedwig over time.
  *"This project's tests live in `tests/`, not `test/`."*
  *"`recipe-1` through `recipe-4` are seed fixtures; don't renumber."*
  Limit: 3 per task.
- **Behavioral guidelines** (`relevant_behavioral_guidelines`,
  rule_store.py:467) — soft prose-shaping rules. *"Explain before
  patching."* *"Avoid speculative refactors."* Limit: 6 per task.
- **Feedback snippets** (`relevant_feedback_snippets`,
  rule_store.py:528) — verbatim developer feedback from past
  sessions, retrieved when phrasing or context lines up. *"You said:
  'don't add error handling for things that can't fail.'"* Limit: 4
  per task.

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
   it at the booth to walk a visitor through what the agent actually
   saw on the previous turn.

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
codebase, co-change surfaces *what was done* — which files
historically move together when a developer is working on a task.

**Definition.** A file pair is considered to co-change if both files
appear under the same `task` (apply stage) in `decision_traces`.
*Task* — not `session_id` — is the grouping unit, because:

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

No new table, no migration — it's a derived view over data the trace
store already holds. `cochanged_files()` returns the per-file
adjacency; `cochange_graph()` returns the full repo-level
adjacency dict for visualization.

**Surfacing.** Two layers:

1. **Apply-panel inline line** (`sc/run/apply_ui.py::render_cochange_lines`).
   For each touched file at apply stage, one dim line:
   *"`store.py` — historically co-changes with: `models.py` (2)"*.
   Silent if the file has no co-change history meeting `min_count`.
   This is the booth's answer to *"what does Hedwig actually
   learn that CLAUDE.md can't?"* — a concrete, demonstrable
   cross-session pattern.

2. **`/cochange` REPL command** (`sc/run/repl.py`). Full graph view:
   each file with co-change history, indented under it the top-3
   neighbors with their session counts. Falls back to a
   *"patterns appear as you edit files together"* message when the
   repo has no qualifying pairs.

**Why this is honest.** The graph is *descriptive*, not prescriptive
— it doesn't change the cascade's verdict, doesn't shift thresholds,
doesn't seed the classifier. It just tells the developer: *"in this
repo's history, these files have moved together."* Acting on that
information is the developer's call. Same governance philosophy as
the hypothesis bank: surface patterns, let the human confirm.

## 11. Persistence

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

## 12. Observability

- `/prefs` (REPL) — accepted preferences plus pending and rejected
  hypotheses with confidence bars; one panel for the whole picture.
- `hw observe traces` — raw `decision_traces` browse with filters.
- `hw observe weights` — learned classifier drift versus the
  cold-start `_PATTERN_RISK` priors.
- `/retrospective` (REPL) — session-level regret summary.
- `hw observe report` — terminal dashboard.
- `hw observe export --html-report` — single-file researcher-grade
  dump (traces + weights + hypotheses + regret).

## 13. Booth-friendly Q&A

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

**"How does Hedwig manage context?"** Two layers. (1) A
session-start synthesized paragraph — *"What we've learned about
this repo"* — composed from the top confirmed preferences,
top logic notes, and the most-relevant feedback snippet. The
agent reads this before anything else, so it walks in oriented
instead of piecing the picture together from bullets. (2) Per-task
keyword-ranked retrieval from three categories: logic notes (repo
facts), behavioral guidelines (soft prose rules), and developer
feedback snippets (verbatim past corrections). Plus a 40-file
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
thresholds or seed the classifier — same governance philosophy as
the hypothesis bank.

**"Doesn't the adversarial reviewer double Bedrock cost?"** It would,
unconditionally — so it isn't unconditional. `should_review` gates
every call: the reviewer fires only when at least one risk signal
warrants a second opinion (new file, security-sensitive, large blast
radius, large diff, structural change pattern, or no prior history on
this file). For familiar, low-risk, well-trusted edits the heuristic
decides on its own and the reviewer never runs. A 5-call cap per
`hw run` is the backstop. Roughly 50–70% of apply-stage files in a
typical session don't trigger the gate; cold-start sessions trigger
more (which is correct — that's exactly when an outside opinion adds
the most signal).
