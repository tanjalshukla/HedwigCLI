# Hedwig — Context

## What Hedwig is

A **governance layer** that wraps a coding agent (e.g. Claude Code) and decides, for each agent-proposed action, whether to proceed autonomously or hand control back to the developer. Hedwig does not generate code. Its novelty is learning *when to step away* from real interaction traces and human feedback, not from synthetic priors.

## Core vocabulary

### Action
A single agent-proposed operation scoped to one file: a read, a write, a patch application, a verification invocation. The unit of authority.

### Stage
A phase of the agent's workflow: `read`, `plan`, `apply`, `verify`, `report`. Each action belongs to exactly one stage. Authority is granted per stage.

### Check-in
A point where Hedwig pauses the agent and asks the developer to approve, edit, or deny. Check-ins have an **initiator**: either the *model* (the agent asked) or the *policy* (Hedwig decided to ask).

### Hard constraint
A deterministic rule enforced at the CLI boundary — never proceed on this path, never apply this pattern. Not negotiable at runtime.

### Behavioral guideline
A soft preference retrieved into the agent's prompt when task-relevant. Shapes agent behavior without blocking.

### Preference
A developer-expressed autonomy tuning (e.g. "skip low-risk plan checkpoints," "always check in on API changes"). Accumulated from feedback, revocable.

### Decision trace
An immutable record of one action's outcome: inputs, policy decision, check-in initiator, developer response, edit distance. Stored in SQLite. The substrate Hedwig learns from.

### Policy
The function that, given an action's risk and history, decides whether to auto-apply or check in. Has two implementations — heuristic (`sc/policy.py`) and learned (`sc/ml_policy.py`) — that share inputs but not yet a protocol.

## Verbs (use consistently)

- **assess** — compute risk signals for an action. (`assess_risk(action) -> RiskSignals`.)
- **score** — the policy's numeric output over an assessed action.
- **decide** — the policy's categorical output (auto, check-in, deny).
- **record** — write a decision trace.
- **retrieve** — pull behavioral guidelines relevant to the current task into the prompt.
- **revoke** — remove a preference (subtractive counterpart to merge).

Do not use: *classify*, *estimate*, *evaluate* as top-level verbs. Collapse into *assess*.

## RiskSignals

A pure data object describing one action's risk profile. Produced by `assess_risk`. Consumed by every scorer. Contains **raw signals only** — no weights, no scores.

Fields (to finalize):
- `change_pattern` — categorical (e.g. `api_change`, `data_model_change`, `test_generation`)
- `blast_radius` — integer, scope of downstream effects
- `is_security_sensitive` — bool
- `is_new_file` — bool
- `diff_size` — integer
- `files_touched` — int

Weights live with each scorer, not in `RiskSignals`. `features.py` is the single source of truth for what the categories are and how signals are computed.

## What Hedwig is not

- Not a coding agent. It does not propose code.
- Not a static permission system. Rules adapt from traces.
- Not a replacement for `AGENTS.md`. A layer above it.
