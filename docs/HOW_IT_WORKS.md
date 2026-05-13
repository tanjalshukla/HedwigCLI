# How Hedwig Works

A plain-English guide for researchers, collaborators, and anyone who wants to understand the system at a glance.

---

## What Hedwig Is

Hedwig is a governance layer, not a coding agent. It does not write code. Instead, it wraps a coding agent and decides — for every action that agent proposes — whether to proceed automatically or pause for developer review. Think of it as a thoughtful supervisor standing between the developer and the agent, watching every move and calibrating when to step back versus when to ask. The novelty is that Hedwig calibrates that judgment from real interaction history.

---

## The Governed Run Loop

When a developer types a task, Hedwig orchestrates a five-stage process.

**Planning.** The agent first declares its intent: which files it plans to touch, what kinds of changes it expects to make, and a summary of the task. Hedwig can pause here if the plan is unusually large, contains unexpected change types, or if the session is set to a stricter oversight mode. The developer can approve the plan, ask for a revision, or cancel entirely.

**Reading.** Before writing anything, the agent may read existing files to understand what's there. Every read request goes through Hedwig's approval process — the same process used for writes, described in the next section. The developer isn't usually interrupted for routine reads, but Hedwig can pause if a read touches something sensitive.

**Generating.** With an approved plan and the file context it needs, the agent produces proposed changes. These are complete file replacements — the agent rewrites the whole file, and Hedwig computes what actually changed for display.

**Approving writes.** Before anything touches disk, Hedwig evaluates every proposed write through its approval cascade. This is the core of the system and is described in detail below.

**Writing and verifying.** Only after the approval cascade clears does Hedwig write the files. If verification is enabled, it runs automated checks and records whether they passed. A failed verification is remembered — it makes Hedwig more cautious in that repo.

---

## How Hedwig Decides Whether to Ask

Every file access — read or write — flows through four layers, evaluated in order. Earlier layers take complete priority over later ones.

### Layer 1: Hard Rules

Hard rules are developer-set, non-negotiable constraints. Examples: "never touch auth.py" or "always check in before modifying any migration file." These override everything else. A file covered by a hard rule never reaches the scoring system — the rule fires immediately. Hard rules can be set to always deny, always require review, or always allow, and they can apply separately to reads and writes.

### Layer 2: Temporary Trust Grants

If the developer approves the same file repeatedly, Hedwig notices. After enough approvals, it offers to stop asking about that file for the rest of the session, or permanently. These are called leases. An active lease short-circuits the rest of the cascade: no scoring needed, the action proceeds automatically. This is how Hedwig learns to stop interrupting on files the developer has clearly decided are safe.

Hedwig also auto-promotes reads that score safely on their own — if the policy is comfortable with a read, it quietly grants a read lease so the same file isn't re-evaluated every time.

### Layer 3: The Policy Scorer

If no hard rule and no lease applies, Hedwig scores the proposed action. The scorer produces a number that lands in one of three buckets: proceed automatically, proceed but flag it in the terminal, or pause for review. The scoring mechanism is described in detail in the next section.

### Layer 4: Preference Overrides

After the scorer produces its decision, a final layer of structured preferences can override it — but only in one direction. Preferences can tighten a "proceed" decision into a check-in. They cannot loosen a check-in into a proceed. Oversight can be added but never removed by this layer. Preferences are described in full in a later section.

---

## What the Scorer Actually Weighs

The scorer weighs two broad categories of evidence: the history of this file in this repo, and the properties of the proposed change itself.

**From history.** Has the developer approved this kind of change before? Have they denied it? How carefully did they review — did they spend time looking, or approve instantly? Did they make corrections after approving? Each approval adds confidence; each denial reduces it significantly. Quick approvals (under five seconds) count for less, because fast approvals often mean the developer didn't really look.

**From the change itself.** How large is the change? Larger diffs are riskier. Is this a new file being created from scratch? New files get less trust than modifications to known files. How many other files depend on this one? A change to a heavily-imported core module is riskier than a change to a leaf file. Is the file security-sensitive — does the path or content suggest authentication, encryption, credentials? Is it an API change or a database schema change? These are treated as higher-risk than, say, a test file or documentation update.

**From the current session.** If the developer has denied several things already this session, Hedwig's confidence drops across the board. Recent denials are a signal that something isn't going right.

**From overall quality signals.** If automated verification has been failing often in this repo, or if the agent's own confidence in its outputs has been low, Hedwig lowers its thresholds.

All of these signals combine into a single score. The score is compared against thresholds that are themselves shifted up or down based on session signals (described below).

---

## How Hedwig Learns

Two distinct learning mechanisms run in parallel.

**The online classifier.** Hedwig starts with hand-tuned heuristic rules — a fixed set of weights for the signals above. After the developer has made at least ten real decisions, Hedwig switches to a learned model that updates itself after every decision. The model is a simple logistic regression that trains incrementally. Each approval teaches it what the developer is comfortable with; each denial teaches it what to flag. The switch is automatic and logged, so it's always possible to tell whether a given decision was made by the heuristic rules or the learned model.

Quick approvals are discounted here too. If the developer approves something in under five seconds, the model treats that update as neutral — a fast tap of the approve key doesn't tell Hedwig much about what the developer actually thinks.

**Regret corrections.** Sometimes Hedwig auto-approves something and the developer later pushes back — denying a subsequent related change, reporting that something broke, or having verification fail. When Hedwig detects this pattern, it retroactively corrects itself: it replays the original auto-approval as a denial in the model's training. This way, mistakes don't silently accumulate.

---

## Session Signals

Hedwig doesn't just look at the static history of a repo. It also reads signals from the current session to adjust its behavior in real time.

**Session intensity (active vs. delegating).** A developer who has been in a long session with heavy back-and-forth is probably engaged and watching closely. A developer who opened a session, set a task, and hasn't interacted much is probably delegating. Active sessions get slightly more check-ins — Hedwig is talking to someone who wants to stay in the loop. Delegating sessions get fewer interruptions — the developer has signaled they want the agent to run.

**Coding mode (vibe, collaborative, human-only).** Vibe coding is when the developer accepts almost everything the agent produces with minimal edits — the agent has effectively taken over authorship. Hedwig is slightly more cautious in vibe mode, because mistakes can compound silently when no one is reviewing carefully. Human-only mode is the opposite: the developer is approving fewer than half of the agent's suggestions, writing most of the code themselves. Hedwig is slightly more hands-off here.

**Pushback type.** When the developer does speak up, Hedwig classifies what they're saying. A correction (editing the agent's output significantly) signals that the agent missed something. A scope constraint ("just do X, don't touch Y") signals the developer is managing scope carefully. A failure report ("this broke", "traceback here") is the strongest signal — something went wrong. A positive redirect ("looks good, now do Z") is different from pushback; it's forward motion. Each type carries a different meaning for how cautious Hedwig should be.

**Turn purpose.** Hedwig distinguishes between a developer giving instructions and a developer providing context. If someone pastes an error log or a spec document, that's not pushback — it's information. Hedwig doesn't count those turns as negative signals against the agent's work.

---

## The Preference System

Hedwig has two preference layers, and they serve different purposes.

**Coarse preferences.** These are repo-scoped toggles set via natural interaction. Examples: "prefer fewer check-ins," "always check in on API changes," "only apply this in the `src/payments` directory." These shift Hedwig's thresholds before the scorer runs, so they affect how aggressively the system auto-approves. They're inferred from what the agent says in conversation and can be revoked explicitly.

**Rich preferences.** Each rich preference has five dimensions:

- **Trigger**: what pattern of action should activate this preference. For example: an apply-stage write to a security-sensitive file, or any change larger than a certain size during a debug session.
- **Condition**: what session state must be true. For example: at least two failures have occurred this session, or the developer is in active mode.
- **Action**: what to do when the preference fires — auto-apply silently, show a non-blocking panel (soft check-in), or require a full review (full check-in).
- **Scope**: how broadly this preference applies — globally, per repo, per session, or scoped to specific file paths.
- **Lifecycle**: how the preference was obtained — built-in default, inferred by Hedwig, or explicitly confirmed by the developer.

The asymmetry mentioned earlier applies here: a matched preference can only push the decision toward more oversight, never less.

---

## The Hypothesis Confirmation Loop

Rather than requiring developers to configure preferences manually, Hedwig watches for behavioral patterns across a session and surfaces a single question when it's confident enough to ask.

Hedwig watches for five patterns:

1. **Scope constraint pattern.** You've narrowed scope on the agent multiple times ("just do X, don't touch the tests"). Hedwig asks: "Want me to check in before multi-file changes for the rest of this session?"

2. **Failure reactive pattern.** Multiple things have gone wrong this session. Hedwig asks: "We've had N failures — want me to check in on any non-trivial change until things stabilize?"

3. **Deliberate reviewer pattern.** You've been approving things slowly, making real edits, reviewing carefully. Hedwig asks: "You're reviewing carefully — want me to show a non-blocking panel on small changes?"

4. **Rapid approver pattern.** You've approved many things quickly with no feedback. Hedwig asks: "You've been approving quickly — want me to always require full review on larger changes?" (Rapid approvals of large changes are a risk signal.)

5. **Positive redirect pattern.** You keep accepting small things and immediately asking for the next step. Hedwig asks: "You've been moving fast — want me to show a lightweight non-blocking panel on small single-file changes?"

Hedwig asks at most one hypothesis per session, and only after enough turns have passed to have real signal. Importantly, hypotheses are suppressed entirely in delegating sessions — if the developer isn't engaged, Hedwig doesn't ask them to configure things.

If the developer confirms a hypothesis, it becomes a repo-level preference that persists into future sessions.

---

## What Persists Across Sessions

Hedwig stores everything in a per-repo SQLite database.

**What persists:** the full history of every decision (approvals, denials, response times, whether the developer made edits), the learned classifier state, hard rules, active leases, coarse preferences, confirmed preferences from the hypothesis loop, behavioral guidelines the developer has set, and post-run summaries of completed work.

**What doesn't persist:** session-scoped signals like current intensity and coding mode. These are inferred fresh from the trace history at the start of each new session. Session-scoped preferences expire when the session ends.

The per-repo rather than per-developer design is deliberate. Analysis of real SWE-chat sessions showed low cross-session consistency in individual developer behavior — the same person codes very differently in different contexts. Repo-level preferences are more stable and more defensible.

---

## The REPL and Slash Commands

`hw run "<task>"` is the main entry point. It runs the full governed loop described above.

`/intensity` adjusts how interruptive Hedwig is for the current session. Setting intensity high means more check-ins; setting it low means more autonomy. This is a coarse manual override on top of whatever the scorer produces.

`/status` shows a plain-English summary of the current session: what Hedwig thinks is going on, whether it's been pausing often and why, what preferences are active.

`/learning` shows a repo-scoped summary across all sessions: how many decisions have been made, whether the learned classifier has taken over from the heuristic rules, which features have shifted the most since cold start, and what preference patterns have been confirmed.

`hw observe` surfaces deeper analysis: raw decision traces, classifier weight drift, per-session persona breakdowns, and a full HTML export for researcher-depth review.
