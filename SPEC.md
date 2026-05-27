# Hedwig

Hedwig is a local CLI that sits between a developer and an LLM coding agent. Its job is to decide when the agent should continue independently and when it should stop for review.

The model is untrusted. It can request reads, propose plans, generate edits, and initiate architectural check-ins, but the CLI is the enforcement boundary for every read, write, and verification step.

The system is designed for developers who already supervise and validate agent work. The goal is not zero oversight; it is lower-friction oversight that adapts to the developer's actual behavior over time.

---

# Part 1: Technical Implementation

This document focuses on architecture, runtime behavior, data flow, and future implementation work. Installation, command-line usage, and operator steps live in `README.md` and `demo_recipe_api/DEMO_FLOW.md`.

## Architecture

Check-ins come from two independent sources:

1. **CLI governance + policy engine** — evaluates constraints, leases, trace history, file-level risk signals, and session state. Decides auto-approve vs. check-in vs. deny. Runs regardless of what the model does.
2. **Model-side reasoning** — the system prompt gives the model trust context and asks it to surface uncertainty. It should pause for architectural decisions, approach tradeoffs, and plan deviations, not for routine file access or style choices.

Either side can trigger a check-in independently. Both are logged with `check_in_initiator` so we can learn which source is better calibrated over time.

```
┌─────────────────────────────────────────────────────┐
│                    Developer                         │
│  (terminal / IDE / reviews async queue)              │
└──────────────────────┬──────────────────────────────┘
                       │ commands, approvals, corrections
                       ▼
┌─────────────────────────────────────────────────────┐
│                  Hedwig CLI                     │
│                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  Governance  │  │   Policy     │  │   Trace    │ │
│  │  Engine      │←─│   Engine     │  │   Logger   │ │
│  │ - validates  │  │  (check-in   │  │  (records   │ │
│  │   diffs      │  │   vs. auto)  │  │   every    │ │
│  │ - enforces   │  │              │  │   decision │ │
│  │   scope      │  │ CLI-SIDE     │  │   + who    │ │
│  │ - hash check │  │ CHECK-INS    │  │   started  │ │
│  └─────────────┘  └──────────────┘  │   it)      │ │
│         │              ▲            └────────────┘  │
│         │              │ features         │          │
│         │         ┌────┴─────────┐        │ traces   │
│         │         │  Trust DB    │◄───────┘          │
│         │         │  (SQLite)    │                   │
│         │         └──────────────┘                   │
│         ▼                                            │
│  ┌──────────────────────────────────────────────┐   │
│  │   Rules importer                             │   │
│  │   (`hw rules import ...` -> constraints)     │   │
│  └──────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────┐   │
│  │   System prompt builder                      │   │
│  │   (injects trust context into model prompt)  │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────┘
                       │ governed API calls + system prompt
                       ▼
┌─────────────────────────────────────────────────────┐
│              LLM Agent (untrusted)                   │
│                                                     │
│  MODEL-SIDE CHECK-INS:                              │
│  Pauses for: architectural decisions, approach      │
│  tradeoffs, plan deviations, phase transitions,     │
│  low confidence on design intent.                   │
│                                                     │
│  Does NOT pause for: file permissions, routine      │
│  implementation, style choices.                     │
│                                                     │
│  Communicates via structured JSON protocol:         │
│  read_request, intent_declaration, file_update,     │
│  check_in_message, plan_revision                    │
└─────────────────────────────────────────────────────┘
```

## Runtime Flow

Every task flows through:

1. **Intent declaration** (`run/command.py`) — model produces a structured plan listing files to read and modify.
2. **Read stage** (`run/read_stage.py`) — each read request goes through the approval cascade. Approved files are loaded into context.
3. **Generate updates** (`run/model.py`) — model generates code changes. Can initiate proactive check-ins during generation.
4. **Apply + verify** (`run/apply_stage.py`) — each write goes through the approval cascade. Approved writes use atomic two-phase file writes (temp + `os.replace`). Verification runs post-write.

## Approval Cascade

For every file access, evaluated in order, and separately for reads and writes:

1. **Hard constraints** — permanent rules (`always_deny`, `always_check_in`, `always_allow`) resolved per access type. Override everything.
2. **Active leases** — temporary trust grants from prior approvals. Resolved per access type.
3. **Adaptive policy** — `select_scorer()` in `policy.py` picks between two adapters at the `PolicyScorer` seam: the hand-weighted `HeuristicScorer` (default, carries cold-start behavior) and the online `PolicyClassifier` (`ml_policy.py`, takes over once ≥ 10 real developer decisions are recorded). Risk signals for each action come from `assess_risk()` in `features.py`, which returns a pure `RiskSignals` data object consumed by whichever scorer is active.
4. **Threshold adaptation** (`autonomy.py`) — thresholds shift based on learned preferences and check-in calibration.

## Policy Engine

Heuristic scoring from `policy.py`. The current weights are an explicit baseline, not a claimed optimum. Lab studies are meant to produce the data needed to recalibrate or replace them.

**Signals and weights (actual implementation):**

| Category | Signal | Weight | Notes |
|----------|--------|--------|-------|
| History | Prior approvals (rubber-stamp-discounted) | +0.4 per | Rubber-stamps <5s count as 0.5x |
| History | Prior denials | -0.7 per | |
| History | Deliberate review pace (>15s) | +0.15 | |
| History | High edit distance | -0.5 max | Developer heavily corrects output |
| Risk | Large diff (>80 lines) | -0.8 | |
| Risk | Medium diff (>30 lines) | -0.4 | |
| Risk | Blast radius >3 files | -0.8 | |
| Risk | Large multi-file action (>4) | -0.9 | |
| Risk | Multi-file action (>1) | -0.35 | |
| Risk | New file | -0.6 | |
| Risk | Security sensitive | -2.0 | Path/content keyword detection |
| Risk | API/data model change | -0.8 | |
| Risk | Config change | -0.4 | |
| Risk | Dependency update | -0.5 | |
| Risk | Test/documentation | +0.3 | Low impact |
| Risk | Error handling | +0.1 | Usually localized |
| Session | Recent denials | -0.7 per (max 3) | |
| Quality | Verification failure rate >30% | -0.6 | From trace history |
| Quality | Low model confidence (<0.40, 3+ samples) | -0.3 | From trace history |
| Quality | Adversarial-reviewer score (advisory) | ±0.3 max | `model_risk_score` from `model_risk.assess_risk_via_model`; mapped to [-1, +1] around the 0.5 "no opinion" default and weighted 0.3. Apply-stage only. Failure default 0.5 contributes nothing. |

**Scoring band behavior (three tiers):**

| Score Range | Action | Developer Sees |
|-------------|--------|----------------|
| >= 0.9 (default) | `proceed` — auto-approve silently | Nothing at apply time; appears in session summary |
| >= 0.2 and < 0.9 | `proceed_flag` — auto-approve, flag for review | "Apply approved. Flagged for review: [file]" |
| < 0.2 | `check_in` — pause for developer review | Full check-in UI with policy reasons |

**Gray-area handling:** When a score lands in the `proceed_flag` band, the system auto-approves but ensures the action appears in the session summary and is tagged in traces. The developer can inspect it with `hw observe traces` and, if incorrect, use `hw observe revoke` to revoke the lease and `hw observe preferences-revoke` to undo any preference that contributed to the auto-approval. No action silently disappears — everything is logged with full policy reasons.

These numeric thresholds remain implementation details. Lab participants should interact with qualitative autonomy modes and reason strings, not raw scores. The thresholds shift based on stored preferences (see `adjusted_policy_thresholds()` in `autonomy.py`) and are visible at any time via `hw observe preferences`.

## Online Learning Policy

The policy engine includes an online logistic regression classifier (`ml_policy.py`) that personalizes approval decisions to each developer's observed behavior. It runs alongside the heuristic scorer and activates once 10 real developer decisions have been recorded.

**Feature vector (14 dimensions):**

| Feature | Source | Rationale |
|---------|--------|-----------|
| `prior_approvals` | Trace history (per file/stage) | Files with repeated approvals warrant less interruption |
| `prior_denials` | Trace history | Recurring denials signal persistent risk for this file |
| `avg_response_ms` | Trace history | Slow review pace → developer took the action seriously |
| `avg_edit_distance` | Trace history | Heavy developer corrections → output quality low |
| `diff_size_log` | Current action | Larger diffs are riskier; log-scaled to reduce outlier dominance |
| `blast_radius` | `features.py` | Import fan-out: how many modules depend on this file |
| `is_new_file` | `features.py` | New files carry more uncertainty than modifications |
| `is_security_sensitive` | `features.py` | Path/content keyword detection (auth, credentials, secrets) |
| `files_in_action` | Current action | Multi-file writes are harder to review and revert |
| `recent_denials` | Session state | In-session denial streak signals the agent is off-track |
| `verification_failure_rate` | Trace history | Files with frequent test failures get tighter oversight |
| `model_confidence_avg` | Trace history | Aggregated model self-reported confidence on this file |
| `change_pattern_risk` | `features.py` | Semantic change class (api_change, config_change, test_generation, …) mapped to a risk scalar |
| `model_risk_score` | `model_risk.py` | Advisory adversarial-reviewer score in [0, 1]. Apply-stage only. Defaults to 0.5 ("no opinion") on Bedrock error, JSON parse failure, schema validation failure, or timeout — failure mode never silently flips a decision. |

**Cold start:** At `hw init` time, `build_cold_classifier()` creates an uninitialized `SGDClassifier` seeded with a single zero+one pair so `partial_fit` has seen both classes. No synthetic labels are generated. The `HeuristicScorer` in `policy.py` carries all cold-start behavior until real developer decisions accumulate. This makes the claim *"we learn from real traces, not fabricated priors"* defensible end-to-end — there is no synthetic data in the learning path.

**Online update rule:** After each developer decision (approve or deny at a check-in prompt, or approve-all on an auto-approved batch), the classifier receives one `partial_fit(x, y)` call. The feature vector at decision time is the same 14 features used for scoring. No batch retraining or offline collection period is required.

**Minimum sample gate:** `select_scorer()` returns the heuristic scorer until `PolicyClassifier.sample_count >= MIN_SAMPLES_FOR_LEARNED` (10). This prevents the learned scorer from acting on samples too small to generalize. The threshold is visible at any time via `hw observe weights`. Which scorer fired is recorded in `PolicyDecision.reasons` and persisted into `decision_traces` so longitudinal analysis can separate heuristic-era decisions from learned-era decisions.

**Observability:** `hw observe weights` displays a Rich table with three columns — Prior (cold-start coefficient, effectively zero), Current (learned coefficient after real decisions), Delta (signed drift) — one row per feature. Green/red coloring highlights features where the developer's behavior has meaningfully shifted the policy.

### Worked Example: Policy Update from Trace Data

**Scenario:** A developer is working on a Python API project. Over three sessions, the following interactions are recorded in `decision_traces`:

| Session | File | change_pattern | diff_size | user_decision | response_ms |
|---------|------|----------------|-----------|---------------|-------------|
| 1 | `api/routes.py` | `api_change` | 42 | `deny` | 18,400 |
| 2 | `api/routes.py` | `api_change` | 31 | `deny` | 22,100 |
| 3 | `api/routes.py` | `api_change` | 55 | `check_in → deny` | 31,200 |

After three denials on `api/routes.py`, the `prior_denials` feature value for future scoring of that file is `min(3 / 10.0, 3.0) = 0.3`, and the classifier has received three negative labels for the feature vectors associated with those decisions.

**Effect on the learned model:** The coefficient on `prior_denials` drifts more negative (strengthening its already-negative heuristic prior). The coefficient on `change_pattern_risk` for `api_change` also shifts negative, because all three training examples shared that pattern. Future `api_change` proposals on `api/routes.py` produce lower approval probabilities, increasing the likelihood of a check-in.

**Contrast with a different file:** Meanwhile, `utils/helpers.py` accumulates 5 approvals across the same sessions (`change_pattern = "general_change"`, avg response 4.2s, `diff_size ≤ 20`). The `prior_approvals` feature value is `min(5 / 10.0, 3.0) = 0.5`, and the classifier has received five positive labels for low-risk feature vectors. The `prior_approvals` coefficient drifts more positive. Future writes to `utils/helpers.py` score above the 0.9 `proceed` threshold and are auto-approved silently.

**What this means end-to-end:** After session 3, `hw observe weights` shows coefficients that have drifted from their cold-start zero baseline based on this developer's actual decisions — `prior_denials` and `change_pattern_risk` (for `api_change`) trending more negative, `prior_approvals` trending more positive. Because there are no synthetic priors, every bit of drift is attributable to real interaction data. The developer never tuned a weight. The policy shifted from observed behavior alone.

**Conflict resolution:** When a file has a mix of approvals and denials, the logistic regression resolves the conflict by the full feature vector, not just counts. A file with 3 approvals and 2 denials at `diff_size ≈ 80` will score lower than the same approval/denial ratio at `diff_size ≈ 15` because `diff_size_log` is a strong negative contributor and those two histories represent different risk profiles. The scoping is per `(repo_root, file_path, stage)` — signals from one file do not contaminate scoring for another.

## Autonomy Modes

For lab studies and product UX, the user should control autonomy through one qualitative setting instead of threshold tuning:

- `strict` — conservative approvals, heavier plan gating, more milestone check-ins.
- `balanced` — default mode; risk-aware with moderate autonomy.
- `milestone` — minimize routine interruptions, but always check in at milestone boundaries and meaningful design pivots.
- `autonomous` — proceed aggressively on low-risk routine work; still stop on hard constraints, security, interface changes, or verification failures.

Modes compile down to internal thresholds and plan-gate behavior. The numeric policy remains active, but it is not part of the normal user-facing API.

**Threshold adaptation** (`autonomy.py::adjusted_policy_thresholds`):
- User prefers fewer check-ins → thresholds drop by 0.25 (+ 0.10 if topic-scoped)
- Model check-in approval rate <40% (5+ samples) → thresholds rise by 0.15
- Floor clamp at -0.5 to prevent nonsensical values

## Heuristic Preference Inference

When the user gives feedback at any approval point, the text is parsed into structured preference data by `summarize_autonomy_feedback()`. This is an LLM-based extraction step — not model training or learned parameter updates. The resulting preference state has four fields:

- `prefer_fewer_checkins` (boolean)
- `allowed_checkin_topics` (subset of: api, signature, schema, security, architecture, config, test, deployment)
- `skip_low_risk_plan_checkpoint` (boolean)
- `scoped_paths` (file path patterns where preferences apply)

Preferences merge additively (OR for booleans, UNION for collections) and persist in SQLite. They directly influence threshold adaptation.

**Revocation path:** Because additive merges are monotonic, the system exposes `revoke_preferences()` / `trust_db.revoke_autonomy_preference()` so developers can walk back specific preferences without a full reset. The CLI surface is `hw observe preferences-revoke`. This ensures the system can tighten oversight when the developer's trust posture changes, not only loosen it. See also: `hw observe preferences-clear` to reset everything.

**Important framing note:** The preference inference mechanism described here (LLM text extraction → structured preference merge → threshold shift) is distinct from the online logistic regression in `ml_policy.py`. Preference inference has no gradient updates, no learned parameters, and no training loop. The "adaptation" is: an LLM parses feedback text → extracts a structured preference object → that object is OR/UNION-merged into stored state → stored state shifts the thresholds that gate the scoring function. The weights in `policy.py` are engineering priors documented as such. The actual parameter learning — gradient updates against real developer decisions — lives in the `PolicyClassifier` in `ml_policy.py` (see "Online Learning Policy" above).

## Behavioral Guidelines

When the system sees repeated denial feedback on the same pattern, `guideline_candidates()` drafts a candidate guideline (for example: "Use AppError with error codes, not generic Error"). Accepted guidelines are injected into the system prompt. The CLI proposes them; the developer decides whether they become part of the working policy context.

## Database Schema

SQLite with 8 tables:

| Table | Purpose |
|-------|---------|
| `leases` | Temporary write trust grants (repo_root, file_path, expires_at, source) |
| `read_leases` | Temporary read trust grants (same structure) |
| `decisions` | High-level approval records (task, approved, planned/touched files) |
| `decision_traces` | Per-file decision log — 33 columns capturing every signal, decision, and outcome |
| `plan_revisions` | Plan checkpoint history (revision rounds, developer feedback, approval) |
| `hard_constraints` | Permanent rules (path_pattern, read_policy, write_policy, source, overridable) |
| `behavioral_guidelines` | Learned/imported prompt directives (guideline text, source) |
| `autonomy_preferences` | Learned check-in preferences per repo (JSON blob) |

The `decision_traces` table is the primary data source for post-study analysis. It records: stage, action_type, file_path, change_type, diff_size, blast_radius, lease state, approval history, policy score + reasons, user decision, response time, rubber-stamp flag, edit distance, feedback text, verification result, model confidence, check-in initiator, participant/run/task study metadata, and autonomy mode.

## External Interface

The public interface is intentionally small:

- `hw run` — main governed coding loop
- `hw ask` — no-write question answering
- `hw rules ...` — import and inspect constraints/guidelines
- `hw rules add` — compile a freeform natural-language rule into either enforced constraints or prompt-level guidance
- `hw observe ...` — traces, exports, explainability, resets
- `hw config ...` — autonomy mode and verification setup

The operator-facing details belong in `README.md`. In this spec, only the behavior of those surfaces matters:

- the user selects a qualitative `autonomy_mode`
- verification is a configured local command
- exported study artifacts come from `observe export`
- mutable local state can be reset between sessions/participants

## Project Structure

Key modules, by responsibility:

- `agent_client.py` — Bedrock client + strict structured output protocol
- `prompt_builder.py` — dynamic system prompt from trust state
- `policy.py` / `autonomy.py` — heuristic approval scoring + autonomy adaptation
- `plan_gate.py` / `phase.py` — milestone and phase enforcement
- `trust_db.py` — SQLite persistence, analytics, traces, exports
- `constraints.py` — rule import and path-policy resolution
- `features.py` — blast radius, sensitivity, semantic change classification
- `verification.py` — post-write checks
- `run/` — orchestration for declare/read/check-in/apply/report
- `commands/` — user-facing CLI surface
- `tests/` — behavior and regression coverage for policy, DB, parsing, prompts, and run stages
- `README.md` — installation, usage, operator workflow
- `SPEC.md` — architecture, data model, research framing, future work

---

# Part 2: Research

## Why This Matters

Current tools calibrate autonomy through developer-authored static configuration — CLAUDE.md files, permission lists, rule files. These capture preferences the developer can articulate in advance, but most preferences are implicit — they emerge as correction patterns, review timing, edit distance, and phase-of-work context that no static config file can anticipate.

Recent studies suggest the main bottleneck is not raw model capability but trust infrastructure: when to let the agent continue, when to intervene, and how to turn observed behavior into future calibration. Hedwig is an attempt to make that boundary explicit and measurable.

## The Trace-Prompt Feedback Loop

This is the core mechanism. Every developer interaction produces a trace. Traces accumulate into trust scores, correction patterns, and behavioral guidelines. These are queried at session start to build the system prompt the model receives. The model then uses that context to reason about when to check in.

Concretely: the developer corrects the agent's error handling in session 3. That correction is logged as a trace with `change_pattern = "error_handling"`, `user_decision = "approve"`, `user_feedback_text = "Use AppError with error codes"`. In session 4, the prompt builder does two things:

1. **Trust summary**: the model sees "Low-trust areas: error_handling — the developer has corrected you here before." Vague on purpose — no numeric scores, just enough for the model to reason about its own uncertainty.
2. **Recent corrections**: the model sees the developer's own words. Specific and actionable.

After 3+ corrections on the same pattern, the system suggests a behavioral guideline. Once accepted, the model follows the directive instead of checking in. Correction overhead drops to zero for that pattern.

Full cycle: **traces → trust scores → prompt context → model reasoning → check-in decisions → developer response → traces**.

## Pair Mode UX (implemented)

In pair mode, the developer sees:

- a structured plan before implementation when the plan gate fires
- policy snapshots for reads and writes
- model-initiated architectural check-ins when the model identifies uncertainty
- diff approval only for files that actually require review
- a run summary with session id, change patterns, and trace/export support

## Phase-Aware Behavior

| Phase | Default | Learns to... |
|---|---|---|
| Research | Read freely, write findings to markdown | Which modules need deep vs. shallow reads |
| Planning | Heavy check-ins, developer annotates iteratively | What the developer always overrides in plans |
| Implementation | Minimal interruptions, execute approved plan | Which implementation patterns get corrected |
| Review | Surface results, flag failures | Which test failures are blocking vs. ignorable |

## System Prompt

Built dynamically per-session from the trust DB. The model is an active participant in oversight but doesn't get information it could exploit — no exact trust scores (could game thresholds), no list of which files will be auto-approved (prevents strategic behavior). It does know which areas it's been corrected on and what the hard constraints are.

The prompt includes: role framing, check-in guidance, trust summary (high/low trust areas by name, no scores), hard constraints, relevance-ranked behavioral guidelines, relevance-ranked historical corrections with developer feedback text, autonomy preferences, phase-specific guidance, session warnings.

The current prototype already uses a lightweight local retrieval step over historical corrections and guidelines, keyed by task/spec text and simple token-overlap features. That is enough to move beyond pure recency. A future version should replace this with stronger semantic retrieval over richer task, file, and pattern context.

## Evaluation

The evaluation plan is intentionally simple:

- **Primary metrics** — correct trust rate, correct caution rate, unnecessary interruption rate, and missed check-in rate.
- **Calibration metrics** — useful vs. wasted check-ins, split by initiator (CLI vs. model), plus agreement rates between CLI, model, and developer.
- **Learning metrics** — correction repeat rate, trust trajectory, preference carryover across sessions, and change in interruption rate after feedback.
- **Quality metrics** — rubber-stamp rate, review duration, verification outcomes, and false-confidence indicators.
- **Human-centered metrics** — interruption burden, check-in usefulness, developer understanding, and trust calibration are first-class outcomes alongside task completion.

Planned baselines:
- Always Ask
- Never Ask
- Static Rules
- Heuristic (current implementation)
- Future learned policy

Study protocol:
- cold start sessions
- stable-use sessions
- preference-shift sessions
- post-shift adaptation sessions

The key comparison is between static rules and adaptive behavior learned from traces.

## Related Work

- **Zhou et al. (CHI '26)** model confirmation as a minimum-time scheduling problem (dynamic programming over CDCR recovery costs + per-step agent accuracy), validated with 48 participants — 81% preferred intermediate checkpoints, 13.54% time reduction vs. confirm-at-end. Their §5.4.3 explicitly defers personalization to future work. Hedwig addresses that gap: rather than scheduling globally optimal interruptions, Hedwig learns per-repo and per-session risk tolerance from interaction traces and adapts check-in frequency to that repo's and session's history.
- **CowCorpus** motivates the idea that interaction styles are stable enough for oversight behavior to be learned from traces. Hedwig takes the same premise but scopes adaptation to the repo and the current session — cross-session behavioral stability was low in our SWE-chat analysis (ICC 0.249) — and keeps the adaptation in a separable governance layer rather than retraining the model.
- **Grunde-McLaughlin et al.** motivate review-quality signals: Hedwig uses assumptions in check-ins and discounts rubber-stamp approvals instead of treating every approval equally.
- **PAHF** motivates post-action personalization. Hedwig adopts the same feedback-driven idea but keeps the memory and adaptation loop outside the model, in the local CLI.
- **Humans are Missing from AI Coding Agent Research** strengthens the motivation for Hedwig's study design: oversight quality, steerability, verifiability, and adaptability should be evaluated on realistic human-agent workflows rather than only offline autonomous benchmarks.
- **Appropriate reliance / scalable oversight / capability security** provide the broader framing: the goal is calibrated reliance, meaningful oversight as capability grows, and explicit scoped authority rather than broad agent trust.

## Current Status

### Lab-study baseline (implemented)

The current prototype is in a lab-study-ready state with the following baseline:

- qualitative autonomy modes (`strict`, `balanced`, `milestone`, `autonomous`)
- hybrid milestone + heuristic check-ins
- read/write-split hard constraints
- plan gating, phase gating, and post-write verification
- trace capture with participant/run/task metadata
- export/reset commands for study operations
- qualitative reason strings in the runtime UI
- spec-aware planning via optional `--spec`

## Gaps and backlog

Moved to [`BRAINSTORM.md`](BRAINSTORM.md). SPEC.md describes what the system is now; BRAINSTORM.md tracks what it isn't yet.

## Design Decisions

| Decision | Current | Revisit if... |
|---|---|---|
| Learning algorithm | Online logistic regression (SGD, log-loss, cold-start); `HeuristicScorer` adapter carries behavior until `MIN_SAMPLES_FOR_LEARNED` (10) real decisions | Enough lab data → contextual bandit (context → choose approval action → update from observed outcome) as a third adapter at the `PolicyScorer` seam |
| Change pattern classification | Rule-based (features.py) | Rules miss too many patterns → lightweight LLM |
| Trust decay | None implemented | Users report stale trust → add exponential decay |
| Lease threshold | 3 consecutive approvals | Too aggressive or conservative |
| Model trust visibility | Vague summary, no scores | Model needs more to reason well, or is gaming it |
| Initiator weighting | Equal CLI vs. model | Data shows one source is consistently better |
| Model confidence | Logged, not trusted | Correlates well with outcomes → make it active |
| Cold start | Sensible defaults, no interview | Takes too many interactions → add light interview |
| Guideline threshold | 3 corrections on same pattern | Too noisy or too conservative |
| Guideline authorship | CLI drafts, developer confirms | Consistently accepted → reduce friction |
| Model writes own rules | Never | N/A — hard architectural constraint |
| Rubber-stamp threshold | <5s review duration | 5s too aggressive → adjust per task complexity |
| Approval quality discount | 0.5x rubber-stamp | Starves learning or corrupts trust |
| Preference learning | Model-based (no regex) | Model calls too slow → add fast-path heuristics |
| Preference accumulation | OR/UNION additive merge; revocation via `preferences-revoke` | Preferences go stale → add decay mechanism |
