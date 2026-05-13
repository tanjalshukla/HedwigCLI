# Hedwig — Brainstorm / Parked Ideas

Ideas discussed but not in the pre-dinner / pre-CAIS scope. Revisit after the codebase walk.

## Policy / learning

- **Contextual bandit scorer (LinUCB or Thompson sampling).** Reframes check-in decision as arms (auto-apply, light check-in, full plan check-in) with reward = developer signal. The highest-novelty shift available; pairs with the PolicyScorer protocol (new adapter). Cold-start problem — would need real trace bootstrap to demo well.
- **Per-developer policies.** Currently `trust_db` keys on `repo_root`. Adding a developer dimension so a new dev on the same repo starts cold is worth exploring, but the SWE-chat analysis (ICC 0.249 cross-session) says developer style isn't stable enough to justify it today. Reviewer 148D asked us not to *claim* per-developer behavior when it's per-repo — they weren't asking us to build it. Parked.
- **Regret tracking.** After every decision, record counterfactual regret — did auto-apply get reverted? Did the check-in rubber-stamp? Surface in `hw observe report` and feed back as a loss signal. This is the measurement story reviewers kept asking for.
- **Active elicitation.** Occasionally force a check-in on actions Hedwig is confident about, just to test the confidence. Prevents the system from calcifying around early preferences.
- **Preference decay + confidence scoring.** Timestamp-based attenuation; preferences below threshold auto-prune. Already partly answered by `revoke_preferences()`; decay is the next step.
- **Cross-repo preference transfer.** Preferences learned in one repo bootstrap another at cold-start. Controversial — some prefs are repo-specific. Dinner conversation starter.

## Scorer alternatives considered and parked

- Random forest, XGBoost: don't natively support online updates (`partial_fit`). Overkill for 13 features + <10k rows. Would re-introduce the "static, retrained" shape reviewers criticized.
- Batch `LogisticRegression` (scikit-learn): same model as current `SGDClassifier(loss="log_loss")`, different solver. Not worth swapping — SGD gives online updates.
- Calibrated probabilities (`CalibratedClassifierCV`): useful if uncertainty-triggered check-ins ship and miscalibration becomes visible.

## Architecture

- **Split `trust_db.py` (2096 lines).** Proposed split: `TrustStore` (CRUD), `TraceAnalytics` (queries), `RiskRetrieval` (prompt-time retrieval). Candidate #6 from the architecture audit. Deferred — low dinner leverage, high churn risk.
- **Approval-cascade seam (#1 from audit).** Biggest structural win; deferred because it's the riskiest pass and lands after #4 + #3 stabilize the inputs.
- **Trace-recording seam (#2 from audit).** `_record_traces` takes 14+ params from five parallel dicts. Deferred to post-dinner.

## Research / framing

- **Position Hedwig as a governance layer above coding agents** (not a competing agent). Novelty = learning from real trace + human feedback, not code generation. This is the framing for README, CONTEXT, and dinner pitch.
- **Pilot evaluation.** A small within-subjects comparison vs. vanilla Claude Code + AGENTS.md on 2-3 tasks, measuring check-in count and accepted/rejected proposals. The single highest-leverage edit for camera-ready if time allows.
- **Reframe "learning" language.** Reviewer 148D was specific: the paper says "learning" where the code does heuristic scoring. Post-refactor (with real-trace bootstrap + uncertainty-triggered check-ins), the language is defensible; until then, soften.

---

## Backlog moved from SPEC.md

The sections below were previously in SPEC.md but describe *desired* system behavior rather than current behavior. Moved here so SPEC stays a description of what the system is now. Reinstate into SPEC if/when implemented.

### Gaps from papers and survey (parked)

- **Interaction-style cold start** — no CowCorpus-style hands-off / collaborative / takeover prior yet.
- **Reversibility as a separate risk dimension** — blast radius exists; reversibility does not.
- **Richer interrupt semantics** — no explicit `user_takeover`, `partial_approve`, or typed interrupt reasons yet.
- **Deeper spec-driven development** — current `--spec` support is bounded prompt grounding, not a full structured spec workflow.
- **Stronger semantic correction retrieval** — lightweight relevance ranking today; embeddings / file-cluster retrieval deferred.
- **Structured logic-note memory is still lightweight** — retrieval exists but stays shallow.
- **No developer-intent labeling** — approvals, denials, and corrections are recorded, but the system cannot yet distinguish file-object vs. approach vs. quality vs. timing corrections. (Directly related to #15.)
- **Deterministic promotion of soft rules** — guidelines can influence prompts, but most are not yet converted into enforceable checks.
- **Process-rule compilation** — `hw rules add` cannot yet produce deterministic workflow rules (e.g., always-run-verification-before-completion).
- **Unified rule taxonomy across sources** — `--spec`, `CLAUDE.md`/`AGENTS.md`, and `hw rules add` are partially separate surfaces; a canonical taxonomy would unify them.
- **Model-assisted `hw rules import`** — still regex-style; should reuse the model-assisted compilation path from `hw rules add`.
- **Async delegation mode** — current UX is pair mode; no queue/review workflow yet.
- **Subagent planner/coder split** — research-track idea, not shipped.
- **Post-hoc correction after approval** — can't retroactively mark an approved change as negative signal.
- **Checkpoint / rewind workflow** — writes are atomic, but no first-class rewind command.
- **Git-aware local-change risk** — policy doesn't yet treat uncommitted local edits as a signal.
- **Pre-action uncertainty declarations** — model can check in today, but doesn't declare uncertainty / expected risk before attempting a new action class.
- **Asymmetric autonomy adaptation** — revocation exists; automatic tightening on novelty/impact spikes does not.
- **Phase model likely heavier than necessary** — four enforced phases; long-term may collapse to pre-write planning + post-write verification.
- **Autonomy modes are still a coarse control surface** — `strict`/`balanced`/`milestone`/`autonomous` probably right for cold-start presets only.
- **Longitudinal human-centered evaluation** — instrumented for lab studies but not validated over repeated sessions.

### Prioritized backlog (parked)

**Priority 1 — better policy calibration**

- Shadow-mode evaluation (log what baselines would have done without changing live experience).
- Interaction-style priors from early-session behavior.
- Asymmetric adaptation by complexity.
- Replace fixed heuristic constants (rubber-stamp multiplier, static thresholds) with learned signals.

**Priority 2 — stronger rule enforcement and explanation**

- Deterministic promotion of soft rules into hard constraints, verification hooks, or static checks.
- Process-rule compilation via `hw rules add`.
- Unified rule taxonomy: task contract / behavioral guidance / deterministic access / deterministic process.
- Replace regex-style `hw rules import` with model-assisted compilation.
- Verifiability-first policy taxonomy (deterministic enforced / deterministic advisory / best-effort).
- Policy expectation disclosure at add/import time.
- Vague-scope resolution (require disambiguation for "frontend style files" before persisting).
- Counterfactual-style rationale for check-ins.

**Priority 3 — richer memory and spec use**

- Stronger correction retrieval (embeddings / file clusters / change patterns / guidelines).
- Deeper semantic memory of prior work.
- Richer structured logic-note memory (quality, dedup, trigger conditions, retrieval).
- Developer-intent feedback taxonomy (file / approach / quality / timing / explanation). See #15.
- Deeper spec-driven execution (requirement lineage, section-level grounding).
- Post-hoc correction support.
- Checkpoint / rewind support.
- Review-phase preference learning (blocking vs. ignorable failures).

**Priority 4 — broader workflow support**

- Async delegation mode (background execution with queue/review UX).
- Research-phase markdown writeback.
- Git-aware risk features.
- Pre-action uncertainty declarations.
- Subagent planner/coder split (after traceability is stronger).
- Simplify workflow control (collapse to plan approval + verification-before-completion).
- Infer autonomy posture instead of selecting modes.

**Longer-term research**

- Reversibility as a first-class risk signal.
- Richer interrupt taxonomy.
- Trust decay and drift detection.
- Full RL only if simpler learned policies are insufficient.
