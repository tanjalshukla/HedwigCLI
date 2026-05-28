# Hedwig — Context

Shared vocabulary for the project. Every concept has two forms:

- **Precise** — the definition we use in code, docs, and with each other.
- **Plain** — the one-line version for reviewers, or a non-technical collaborator.

---

## What Hedwig is

**Precise:** A governance layer that wraps a coding agent (e.g. Claude via Bedrock) and decides, for each agent-proposed action, whether to proceed autonomously or hand control back to the developer. Hedwig does not generate code. Its novelty is learning *when to step away* from real interaction traces and human feedback, not from synthetic priors.

**Plain:** A thin layer that sits on top of a coding agent and decides, for every action the agent wants to take, whether to let it go or pause and ask. It learns when to ask from real developer reactions, not from rules we wrote.

---

## Core vocabulary

### Action

- **Precise:** A single agent-proposed operation scoped to one file — a read, a write, a patch application, a verification invocation. The unit of authority.
- **Plain:** One thing the agent wants to do to one file.

### Stage

- **Precise:** A phase of the agent's workflow: `read`, `plan`, `apply`, `verify`, `report`. Each action belongs to exactly one stage. Authority is granted per stage.
- **Plain:** Which phase of work the agent is in right now.

### Check-in

- **Precise:** A point where Hedwig pauses the agent and asks the developer to approve, edit, or deny. Check-ins have an `initiator`: the *model* (the agent asked) or the *policy* (Hedwig decided to ask).
- **Plain:** A pause where we ask the developer before continuing. Either the agent raised a hand, or Hedwig did.

### Hard constraint

- **Precise:** A deterministic rule enforced at the CLI boundary — never proceed on this path, never apply this pattern. Not negotiable at runtime.
- **Plain:** A hard rule Hedwig always obeys, set up once and never adjusted.

### Behavioral guideline

- **Precise:** A soft preference retrieved into the agent's prompt when task-relevant. Shapes agent behavior without blocking.
- **Plain:** A soft hint that gets added to the agent's prompt when it's relevant. Doesn't block anything.

### Preference

- **Precise:** A stored signal about how the developer wants oversight to work — either explicit or inferred from traces. Has five dimensions (see Preference taxonomy below).
- **Plain:** Something we've learned about how this developer wants Hedwig to behave.

### Decision trace

- **Precise:** An immutable record of one action's outcome — inputs, scorer decision, check-in initiator, developer response, edit distance. Stored in SQLite. The substrate Hedwig learns from.
- **Plain:** The log of every decision Hedwig has made, why, and what the developer did about it. All the learning comes from these.

### Policy / PolicyScorer

- **Precise:** The function that, given an action's risk and history, decides whether to auto-apply, flag, or check in. The `PolicyScorer` seam has two adapters:
  - `HeuristicScorer` — hand-weighted linear scorer; carries cold-start behavior.
  - `PolicyClassifier` — online logistic regression (scikit-learn `SGDClassifier`, log loss); takes over once ≥ 10 real decisions have accumulated.
  - `select_scorer(classifier)` picks one and tags the decision with which fired.
- **Plain:** The part that decides "auto-do this" vs "ask first" vs "just flag it." Two versions: rules we wrote, and a version that learns from real decisions. The learned version switches on after 10 real interactions; until then we use the rules. Every decision is labeled with which version made it.

### RiskSignals

- **Precise:** A pure data object describing one action's risk profile. Produced by `assess_risk`. Consumed by every scorer. Contains **raw signals only** — no weights, no scores.
- **Plain:** What we know about a proposed change before deciding whether to ask. Size of the change, type of change, whether it touches security-sensitive files, whether it's a new file, how many other files depend on this one.

Fields: `change_pattern`, `blast_radius`, `is_security_sensitive`, `is_new_file`, `diff_size`.

---

## Preference taxonomy (Hedwig's research contribution)

A preference has five dimensions. Each captures something the older 4-field schema couldn't express.

### Trigger

- **Precise:** Predicate over `RiskSignals` and action context. Matches if all specified fields hold (AND semantics; `None` = wildcard).
- **Plain:** What kind of action this preference cares about.

### Condition

- **Precise:** Contextual predicate evaluated at decision time — session state, persona, scorer confidence. `None` = don't care.
- **Plain:** When this preference should fire, based on what's happening around this action (not just the action itself).

### PreferenceAction

- **Precise:** Fixed enum — `AUTO_APPLY` / `SOFT_CHECKIN` / `FULL_CHECKIN`.
- **Plain:** What Hedwig should actually do when this preference matches — just do it, a quick non-blocking panel, or a full pause with explanation.

### Scope

- **Precise:** Where the preference applies — `global` / `repo` / `session` / `path`. Multi-level; checked outermost-first.
- **Plain:** How widely this preference applies — everywhere, just this codebase, just this session, or just certain file paths.

### Lifecycle

- **Precise:** Provenance + confidence + decay. Fields: `provenance` (`user_explicit` / `inferred` / `default`), `confidence` (0..1), `last_reinforced_at`, `half_life_seconds`.
- **Plain:** How we got this preference, how sure we are, and how fast it should fade if we don't see it again.

---

## Session signals (SWE-chat grounded)

Three inferred signals we compute from traces. Each has plain-English meaning in what a developer is doing, not just statistics.

### CodingMode

- **Precise:** Session-level enum — `human_only` / `collaborative` / `vibe`. Inferred from who authored the surviving code (proxy: edit_distance + approval rate).
- **Plain:** How much of the code in this session is actually the agent's. Three buckets: the human wrote everything, they wrote it together, or the developer is mostly accepting whatever the agent produces.

### UserPersona

- **Precise:** Session-level intensity enum — `active` / `delegating` / `unknown`. Replaces the old 4-value persona enum (`expert_nitpicker` etc.) which was not supported by behavioral clustering of 5,776 sessions. Inferred from turn count and tool-calls-per-turn. Affects scorer thresholds and hypothesis surfacing (delegating sessions never see hypothesis prompts).
- **Plain:** How engaged the developer is being. Active = deep in it, lots of back-and-forth. Delegating = accepting most of what the agent does, moving fast.

### Oversight

- **Precise:** The user-facing label for session intensity, exposed via `/oversight` in the REPL. Three values: `hands-on` (maps to internal `active`), `balanced` (maps to `None` / auto-infer), `delegating`. Set explicitly overrides inference.
- **Plain:** How much the developer wants Hedwig in their face right now. Can be pinned via `/oversight` or left to auto-infer.

### PushbackType

- **Precise:** Per-turn enum — `correction` / `rejection` / `failure_report` / `non_pushback` / `scope_constraint` / `positive_redirect`. The last two were added based on the SWE-chat analysis (33% of real pushback turns fell outside the original 4-category scheme).
- **Plain:** What kind of response the developer gave this turn. Fixing it, saying no, reporting a failure, silent agreement, narrowing scope, or "looks good, now do X."

---

## SWE-chat-derived terms

Terms that emerged from the 62K-turn analysis. These are research terms we use in discussions, not code identifiers.

### Session intensity

The real behavioral axis that distinguishes developers, per the SWE-chat clustering. Sessions split cleanly into *short / delegating / low-pushback* vs. *long / actively engaged / high-pushback*. Replaces persona-type as the primary descriptive axis.

- **Plain:** How involved the developer is being this session. Either they're letting the agent run and accepting most of it, or they're deep in it with lots of back-and-forth.

### Positive redirect

A pushback category missing from the current schema. "I like it, now do X" — approval combined with a new instruction. 33% of real pushback turns in SWE-chat fall into this category; we currently file them as `non_pushback` or `correction`.

- **Plain:** The developer is happy with what you did and wants to move on to the next thing. Not a complaint, not silence — a redirect.

### Scope constraint

Another pushback category missing from the current schema. "Just do X", "don't touch Y", "only". Behaviorally distinct from correction — the developer is narrowing what the agent should work on, not fixing what the agent did.

- **Plain:** The developer is narrowing what the agent is allowed to do, not correcting something the agent did wrong.

### Per-session vs. per-developer preferences

A scoping question with an empirical answer (SWE-chat ICC = 0.25 across sessions per user). Developer style is not stable across sessions. Per-session scoping is supported; per-developer scoping is not.

- **Plain:** The same developer behaves very differently in different sessions. So it doesn't make sense to learn "this is how Alice always wants things" — we should only learn "this is how Alice wants things in this session."

### Failure-signal check-in

A deployable proactive check-in trigger, grounded in finding 2 of the SWE-chat analysis. Pattern: session has debug intent + prior failure report or verification failure. Predicts future failure reports with AUC 0.90. Implemented as `FAILURE_SIGNAL_CHECKIN` in `preferences.py`.

- **Plain:** When the developer is clearly debugging and something's already gone wrong this session — pause before the next write.

### Hypothesis bank

- **Precise:** A per-session SQLite table (`hypothesis_candidates`) that stores generated hypothesis candidates with evidence counts. Two generators seed candidates: rule-based detectors that run every apply turn (`preference_inference.py`), and an intermittent LLM noticer (`maybe_generate_llm_hypotheses`) that fires every `LLM_GENERATION_INTERVAL` turns and is supplemental — disabling it does not break the loop. Each new trace scores `+1 for` or `+1 against` every pending candidate. Candidates are surfaced when `evidence_for / total ≥ SURFACE_CONFIDENCE` (0.70) over ≥ `MIN_EVIDENCE` (3) traces; pruned when `≤ PRUNE_THRESHOLD` (0.30). LLM-proposed candidates may raise their own bar by marking `high_stakes` (clamps to `2 × MIN_EVIDENCE`), and an LLM candidate citing ≥ `MIN_EVIDENCE` real `decision_traces.id`s is promoted directly to `ready_to_surface`. Implements the Trial-Error-Explain loop.
- **Plain:** Hedwig watches patterns without asking immediately. It waits until it has enough evidence that a pattern is real, then surfaces one question. Patterns that get contradicted are silently dropped. Two sources propose patterns: deterministic rules every turn, and an LLM that occasionally reads recent traces and can raise its own bar for risky guesses.

### Evidence accumulation

- **Precise:** The process of scoring each new decision trace against every pending hypothesis candidate and updating `evidence_for` / `evidence_against` counts. Handled by `update_evidence()` in `hypothesis_bank.py`, called on every apply decision.
- **Plain:** After each action, Hedwig quietly updates its confidence in each pattern hypothesis.

### REPL

- **Precise:** The persistent interactive session started by `hw` with no subcommand. A single `session_id` is shared across all tasks in the loop. Slash commands (`/status`, `/learning`, `/prefs`, `/rules`, `/observe`, `/oversight`) are handled inline without leaving the session.
- **Plain:** Start `hw` and stay inside it. Every task you type shares the same session, so behavioral signals accumulate across multiple tasks.

### O1–O5 criteria (Bui & Evangelopoulos, 2026)

Five criteria for evaluating coding-agent insight policies. Hedwig is the first system that meaningfully satisfies O1+O2+O3:

- **O1** — cost-of-interruption is computed. Hedwig's policy scorer models the probability that an action needs review.
- **O2** — "stay silent" is an explicit learned action, not a fallback. Hedwig's auto-approve path is a first-class decision, not the absence of a check-in.
- **O3** — per-developer feedback updates the policy. Hedwig's online classifier and hypothesis bank both update from real decisions.
- **O4** — cross-context observation. Hedwig's session signals (intensity, coding mode) aggregate across multiple tasks in a session.
- **O5** — initiation channel. Model check-ins let the agent proactively surface uncertainty.

No deployed coding agent audited by Bui & Evangelopoulos (Cursor, Copilot, Jules, Claude Code Routines) satisfied O1+O2+O3.

### Governance layers

Hedwig's governance maps to three layers (Sahoo, Controllability Trap, 2026):

- **Preventive** — hard constraints (always_deny / always_check_in rules)
- **Detective** — regret tracking (auto-approves that were later pushed back on)
- **Corrective** — calibration retrospective (session-end view of where Hedwig was too loose or too cautious)

---

## Verbs (use consistently)

- **assess** — compute risk signals for an action. (`assess_risk(action) -> RiskSignals`.)
- **score** — the policy's numeric output over an assessed action.
- **decide** — the policy's categorical output (auto, check-in, deny).
- **record** — write a decision trace.
- **retrieve** — pull behavioral guidelines relevant to the current task into the prompt.
- **revoke** — remove a preference (subtractive counterpart to merge).
- **infer** — derive a session-level signal (CodingMode, UserPersona) from traces.

Do not use: *classify*, *estimate*, *evaluate* as top-level verbs. Collapse into *assess*.

---

## Talking to advisors / non-code audiences

Rough translation table for common moments:

| You might want to say | Use this instead |
|---|---|
| PolicyScorer | "the part that decides auto vs. ask" |
| HeuristicScorer / PolicyClassifier | "the rule-based version / the learned version" |
| RiskSignals | "what we know about this proposed change" |
| Preference (as a 5-dim object) | "a stored signal about how this developer wants oversight to work" |
| Decision trace | "the log of every decision Hedwig has made" |
| Check-in initiator | "who raised the hand — the agent or Hedwig" |
| Session-scoped preference | "a preference that only lasts this session" |
| Inferred vs. user_explicit provenance | "learned from behavior vs. set by the developer" |

If a term you're about to use isn't in the plain column somewhere above, stop and write one. Then use that.

---

## What Hedwig is not

- Not a coding agent. It does not propose code.
- Not a static permission system. Rules adapt from traces.
- Not a replacement for `AGENTS.md`. A layer above it.
- Not a per-developer-personalization system. The data says developer style isn't stable across sessions. Preferences are per-session and per-repo, not per-person.
