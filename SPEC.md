# Smart Coder

Smart Coder is a local CLI that sits between a developer and an LLM coding agent. Its job is to decide when the agent should continue independently and when it should stop for review.

The model is untrusted. It can request reads, propose plans, generate edits, and initiate architectural check-ins, but the CLI is the enforcement boundary for every read, write, and verification step.

The system is designed for developers who already supervise and validate agent work. The goal is not zero oversight; it is lower-friction oversight that adapts to the developer's actual behavior over time.

---

# Part 1: Technical Implementation

This document focuses on architecture, runtime behavior, data flow, and future implementation work. Installation, command-line usage, and operator steps live in `README.md` and `docs/OPERATOR_RUNBOOK.md`.

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
│                  Smart Coder CLI                     │
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
│  │   (`sc rules import ...` -> constraints)     │   │
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
3. **Adaptive policy** (`policy.py`) — computes a score from weighted signals, compared against thresholds.
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

**Internal thresholds (hidden from the normal user UX):**
- Score >= 0.9 → `proceed` (auto-approve silently)
- Score >= 0.2 → `proceed_flag` (approve, flag for summary)
- Score < 0.2 → `check_in` (ask the user)

These numeric thresholds remain implementation details. Lab participants should interact with qualitative autonomy modes and reason strings, not raw scores.

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

## Preference Learning

When the user gives feedback at any approval point, the text is summarized into structured preference data by `summarize_autonomy_feedback()`. The resulting preference state has four fields:

- `prefer_fewer_checkins` (boolean)
- `allowed_checkin_topics` (subset of: api, signature, schema, security, architecture, config, test, deployment)
- `skip_low_risk_plan_checkpoint` (boolean)
- `scoped_paths` (file path patterns where preferences apply)

Preferences merge additively (OR for booleans, UNION for collections) and persist in SQLite. They directly influence threshold adaptation.

## Behavioral Guidelines

When the system sees repeated denial feedback on the same pattern, `guideline_candidates()` drafts a candidate guideline (for example: "Use AppError with error codes, not generic Error"). Accepted guidelines are injected into the system prompt. The CLI proposes them; the developer decides whether they become part of the working policy context.

## Database Schema

SQLite with 8 tables:

| Table | Purpose |
|-------|---------|
| `leases` | Temporary write trust grants (repo_root, file_path, expires_at, source) |
| `read_leases` | Temporary read trust grants (same structure) |
| `decisions` | High-level approval records (task, approved, planned/touched files) |
| `decision_traces` | Per-file decision log — 28 columns capturing every signal, decision, and outcome |
| `plan_revisions` | Plan checkpoint history (revision rounds, developer feedback, approval) |
| `hard_constraints` | Permanent rules (path_pattern, read_policy, write_policy, source, overridable) |
| `behavioral_guidelines` | Learned/imported prompt directives (guideline text, source) |
| `autonomy_preferences` | Learned check-in preferences per repo (JSON blob) |

The `decision_traces` table is the primary data source for post-study analysis. It records: stage, action_type, file_path, change_type, diff_size, blast_radius, lease state, approval history, policy score + reasons, user decision, response time, rubber-stamp flag, edit distance, feedback text, verification result, model confidence, check-in initiator, participant/run/task study metadata, and autonomy mode.

## External Interface

The public interface is intentionally small:

- `sc run` — main governed coding loop
- `sc ask` — no-write question answering
- `sc rules ...` — import and inspect constraints/guidelines
- `sc rules add` — compile a narrow natural-language path rule into an enforced hard constraint
- `sc observe ...` — traces, exports, explainability, resets
- `sc config ...` — autonomy mode and verification setup

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

Current tools offer binary autonomy: ask before every edit, or edit automatically. Static config files (CLAUDE.md, .cursorrules) capture preferences the developer can articulate in advance, but most preferences are implicit — they show up as correction patterns, review timing, edit distance, and phase-of-work context.

Recent studies suggest the main bottleneck is not raw model capability but trust infrastructure: when to let the agent continue, when to intervene, and how to turn observed behavior into future calibration. Smart Coder is an attempt to make that boundary explicit and measurable.

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

- **CowCorpus** motivates the idea that users have stable interaction styles and that oversight behavior can be learned per user. Smart Coder takes the same premise but keeps the adaptation in a separable governance layer rather than retraining the model.
- **Grunde-McLaughlin et al.** motivate review-quality signals: Smart Coder uses assumptions in check-ins and discounts rubber-stamp approvals instead of treating every approval equally.
- **PAHF** motivates post-action personalization. Smart Coder adopts the same feedback-driven idea but keeps the memory and adaptation loop outside the model, in the local CLI.
- **Humans are Missing from AI Coding Agent Research** strengthens the motivation for Smart Coder's study design: oversight quality, steerability, verifiability, and adaptability should be evaluated on realistic human-agent workflows rather than only offline autonomous benchmarks.
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

## Outstanding Gaps from Papers and Survey

This subsection is the single place to look for important things discussed in related work or survey feedback that are not fully implemented in the current prototype.

- **Learned calibration instead of guessed weights** — the current approval policy is still heuristic.
- **Interaction-style cold start** — no CowCorpus-style hands-off / collaborative / takeover prior yet.
- **Reversibility as a separate risk dimension** — blast radius exists; reversibility does not.
- **Richer interrupt semantics** — no explicit `user_takeover`, `partial_approve`, or typed interrupt reasons yet.
- **Deeper spec-driven development** — current `--spec` support is bounded prompt grounding, not a full structured spec workflow.
- **Stronger semantic correction retrieval** — the current prototype uses lightweight local relevance ranking for historical corrections and guidelines, not embeddings or richer semantic retrieval over file clusters and task structure.
- **Deterministic promotion of soft rules** — guidelines can influence prompts, but most are not yet converted into enforceable checks.
- **Async delegation mode** — current UX is pair mode; no queue/review workflow yet.
- **Subagent planner/coder split** — still a research-track idea, not part of the shipped runtime.
- **Post-hoc correction after approval** — the current system captures denials and inline corrections, but it does not yet let a developer retroactively mark an already-approved change as a negative signal.
- **Checkpoint / rewind workflow** — the system applies writes atomically, but it does not yet expose first-class checkpoints or a rewind command that records deliberate rollback as a trust signal.
- **Git-aware local-change risk** — the policy does not yet treat uncommitted developer edits as a separate risk signal when the agent proposes touching the same file.
- **Longitudinal human-centered evaluation** — the current system is instrumented for lab studies, but it has not yet been validated over repeated human sessions where interruption burden, understanding, steerability, and trust calibration are measured directly.

## Todo Backlog

This is the prioritized post-demo backlog.

### Priority 1: better policy calibration

- **Learned policy replacement** — replace guessed heuristic weights with a learned decision policy. The first candidate is a contextual bandit: take the current context, choose among `proceed` / `proceed_flag` / `check_in`, and update from the observed outcome.
- **Shadow-mode evaluation** — log what the alternate baselines would have done during live sessions without changing the active experience.
- **Interaction-style priors** — use early-session behavior to infer a cold-start oversight style rather than treating every user identically.

### Priority 2: stronger rule enforcement and explanation

- **Deterministic promotion of soft rules** — promote verifiable guidelines into hard constraints, verification hooks, or static checks instead of leaving them as prompt-only guidance.
- **Verifiability-first policy taxonomy** — classify all rules as deterministic enforced, deterministic advisory, or best-effort.
- **Policy expectation disclosure** — when rules are added/imported, tell the user exactly how they will be enforced.
- **Vague-scope resolution** — require disambiguation for policies like “frontend style files” before persisting them.
- **Better explanations** — add counterfactual-style reasoning and richer rationale for why a check-in happened or was skipped.

### Priority 3: richer memory and spec use

- **Stronger correction retrieval** — upgrade the current lightweight relevance ranking into richer retrieval over task text, file clusters, change patterns, and accepted guidelines.
- **Deeper spec-driven execution** — move from bounded spec digests to structured requirement lineage and section-level grounding.
- **Post-hoc correction support** — allow a developer to retroactively mark an approved change as a negative signal.
- **Checkpoint / rewind support** — create pre-apply checkpoints and an explicit rewind path so rollback becomes both a user tool and a high-confidence negative preference signal.
- **Review-phase preference learning** — learn which failing checks are blocking vs. ignorable.

### Priority 4: broader workflow support

- **Async delegation mode** — background execution with queue/review UX.
- **Research-phase markdown writeback** — controlled markdown artifacts during research.
- **Git-aware risk features** — incorporate local working-tree state (for example, developer-uncommitted edits in a touched file) into risk scoring and check-in rationale.
- **Subagent planner/coder split** — experimental architecture only after deterministic traceability is strong enough.

### Longer-term research

- **Reversibility as a first-class risk signal**
- **Richer interrupt taxonomy**
- **Trust decay and drift detection**
- **Full RL only if simpler learned policies are insufficient**

## Design Decisions

| Decision | Current | Revisit if... |
|---|---|---|
| Learning algorithm | Heuristic weights | Lab study data available → contextual bandit (context -> choose approval action -> update from observed outcome) |
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
| Preference accumulation | OR/UNION, no decay | Preferences go stale → add decay mechanism |
