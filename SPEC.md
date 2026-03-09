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

The prompt includes: role framing, check-in guidance, trust summary (high/low trust areas by name, no scores), hard constraints, behavioral guidelines, last 5 corrections with developer feedback text, autonomy preferences, phase-specific guidance, session warnings.

## Evaluation

### Metrics

**Primary:** correct trust rate, correct caution rate, unnecessary interruption rate, missed check-in rate.

**Two-sided calibration:** CLI-initiated useful/wasted, model-initiated useful/wasted, user interrupt rate, model-CLI agreement rate.

**Secondary:** check-in frequency over time, task completion time, user correction rate, trust score trajectory.

**Guideline loop:** adoption rate, correction rate pre/post guideline, pattern coverage.

**Approval quality:** rubber-stamp rate (<5s), batch vs. realtime accuracy, false confidence rate.

**Preference learning:** correction repeat rate across sessions, cold-start corrections to stable behavior.

### Baselines

1. Always Ask
2. Never Ask
3. Static Rules — explicit preferences only
4. Heuristic — current implementation
5. Bandit — future learned policy

The key comparison is between static rules and adaptive behavior learned from traces.

### Protocol (adapted from PAHF)

Phase 1 (sessions 1-5): cold start, sensible defaults, measure corrections to stable behavior.
Phase 2 (sessions 6-15): evaluate learned preferences, compare all baselines.
Phase 3 (sessions 16-20): preference shift (developer changes style/framework). Measure adaptation speed.
Phase 4 (sessions 21-25): evaluate post-shift adaptation. Should be faster than the original cold start.

Per-trace: extract features, run each policy, compare against ground truth, compute quality-weighted reward.

Reporting: accuracy/precision/recall per policy, learning curves, feature ablation, qualitative examples of implicit preference capture, survey alignment, approval quality distributions, preference learning curves, initiator distribution over time, model calibration, CLI calibration, three-way agreement (CLI x model x user), model confidence correlation with outcomes.

## Related Work

**CowCorpus (Huq et al., 2025, arXiv 2602.17588).** 400 real-user web navigation trajectories. Four interaction styles (hands-off, hands-on, collaborative, takeover) stable per user across tasks. Style-conditioned models improved intervention prediction 61-63%. Validates our bet that per-developer preferences are learnable. They retrain the model; we learn a separable governance policy that works with any model.

**Grunde-McLaughlin et al. (2025, arXiv 2602.16844).** Three user studies on Computer Use Agents. The best trace interface helped developers find errors faster (Hedges' g: -0.65) but increased false confidence when wrong (g: 0.85). Developers rubber-stamp because the process *looks* reasonable. We took: review duration tracking as a quality signal, assumptions field in check-ins, asymmetric quality weighting (negative signals always full weight, positive signals discounted by review speed).

**PAHF (Liang et al., 2026, arXiv 2602.16173).** Meta/Princeton framework for continual personalization. Post-action feedback is "particularly important for robust personalization without pre-existing user data" — our cold-start scenario exactly. We took: immediate correction injection, drift detection design, four-phase evaluation protocol. Key difference: PAHF's memory is model-internal; ours is CLI-governed.

## Future Work

### Current lab-study baseline (implemented)

The current prototype is in a lab-study-ready state with the following baseline:

- qualitative autonomy modes (`strict`, `balanced`, `milestone`, `autonomous`)
- hybrid milestone + heuristic check-ins
- read/write-split hard constraints
- plan gating, phase gating, and post-write verification
- trace capture with participant/run/task metadata
- export/reset commands for study operations
- qualitative reason strings in the runtime UI
- spec-aware planning via optional `--spec`

### Outstanding gaps from papers and survey

This subsection is the single place to look for important things discussed in related work or survey feedback that are not fully implemented in the current prototype.

- **Learned calibration instead of guessed weights** — the current approval policy is still heuristic.
- **Interaction-style cold start** — no CowCorpus-style hands-off / collaborative / takeover prior yet.
- **Reversibility as a separate risk dimension** — blast radius exists; reversibility does not.
- **Richer interrupt semantics** — no explicit `user_takeover`, `partial_approve`, or typed interrupt reasons yet.
- **Deeper spec-driven development** — current `--spec` support is bounded prompt grounding, not a full structured spec workflow.
- **Deterministic promotion of soft rules** — guidelines can influence prompts, but most are not yet converted into enforceable checks.
- **Async delegation mode** — current UX is pair mode; no queue/review workflow yet.
- **Subagent planner/coder split** — still a research-track idea, not part of the shipped runtime.

## Todo Backlog

The backlog below is the canonical implementation list for the gaps above plus additional next steps.

### Near-term

1. **Learned policy weights** — replace guessed heuristic weights with a classifier trained on `decision_traces` data. Contextual bandit (LinUCB) over check-in decisions. Asymmetric reward: missing a needed check-in costs more than an unnecessary interruption.

2. **Async delegation mode** — background execution with `sc status`, `sc review`, check-in queue, session summaries.

3. **Structured plan rendering** — render plans as structured markdown with section headers, file-level summaries, and inline code samples. Goal: minimize ambiguity before the developer approves a plan.

4. **Deterministic guideline enforcement** — currently behavioral guidelines are soft prompt context. Add a promotion path that classifies accepted guidelines into:
   - **Path constraint** -> insert into `hard_constraints` (CLI-enforced)
   - **Content rule** -> generate a grep/lint/static check in verification
   - **Style preference** -> keep as prompt injection

5. **Spec-aware execution loop** — move beyond bounded spec digests toward structured spec retrieval, explicit requirement lineage, and section-level grounding during planning and implementation.

6. **Verifiability-first policy taxonomy** — classify every policy by enforcement capability, not by domain:
   - `deterministic_enforced` (hard block/fail)
   - `deterministic_advisory` (warn + explicit override)
   - `best_effort` (prompt steering only)
   This replaces the implicit “file-access => hard constraint, everything else => guideline” split.

7. **Policy expectation disclosure at creation time** — when a user adds/imports a rule, immediately report:
   - enforcement class (`deterministic_enforced` / `deterministic_advisory` / `best_effort`)
   - exact matched scope (files/globs)
   - failure behavior (block / prompt / note)
   - rationale if downgraded to best-effort.

8. **Vague-scope resolution step** — for ambiguous policies (e.g., “frontend style files”), require disambiguation before persisting:
   - propose concrete candidate scopes
   - require user confirmation
   - store normalized scope set + original natural-language policy text.

9. **Promotable policy pipeline** — automatically attempt to promote verifiable natural-language policies to deterministic enforcement:
   - process rules (e.g., “always run tests”) -> verification hooks
   - content rules (e.g., “use AppError, not Error”) -> static checks/lints
   - access rules -> hard constraints
   - non-verifiable policies remain best-effort guidelines.

10. **LLM policy judge/compiler** — add a dedicated policy-compiler model call that decides:
   - prompt-only guidance vs deterministic script/check
   - confidence + explanation for classification
   - generated checker spec (not raw shell by default), with CLI validation before activation.
   Runtime enforcement remains CLI-deterministic; the LLM is only a compiler/planner.

### Medium-term

11. **Interaction-style cold start prior (CowCorpus)** — classify users as hands-off/hands-on/collaborative/takeover from first 3-5 sessions using interrupt frequency, review duration, edit distance. Use style as a prior for thresholds before enough traces accumulate.

12. **Reversibility as first-class risk signal (McCain)** — add `is_reversible` to traces. Score irreversible actions with an explicit penalty separate from blast radius.

13. **Interrupt taxonomy (McCain)** — extend user response semantics with `partial_approve`, `user_takeover`, and `interrupt_reason` (correction/takeover/redirect/excessive_execution/sufficient_progress). Takeover/sufficient-progress interrupts should be neutral or positive, not denials.

14. **Review-phase failure preference learning** — track developer treatment of failing checks (blocking vs. ignorable). Persist in traces and surface in trust/prompt context.

15. **Research-phase markdown writeback** — controlled write policy for `.md` findings/plan artifacts during research. Keep non-markdown writes blocked.

16. **Subagent execution architecture (experimental)** — evaluate a two-agent topology:
   - `planner/oversight subagent`: plans work according to autonomy and policy rules, emits checkpoints/delegations
   - `coding subagent`: produces edits within delegated scope
   - CLI remains the single enforcement boundary across both agents.
   This is research-track; do not put on critical lab path until deterministic traceability is validated.

### Long-term

17. **Full RL** — only if the bandit can't capture sequential dependencies within sessions. Episode = session, state = context + session history, PPO or DQN.

18. **Trust decay** — exponential decay on trust scores (target: ~14-day half-life). Not yet implemented because we need real usage data to validate the decay rate.

19. **Drift detection** — track corrections that contradict existing guidelines. If 2+ contradictions, flag at session end for review.

## Design Decisions

| Decision | Current | Revisit if... |
|---|---|---|
| Learning algorithm | Heuristic weights | Lab study data available → contextual bandit |
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
