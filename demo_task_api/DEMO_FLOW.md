# Hedwig Demo Flow (2026 — post-SWE-chat revision)

Purpose: show in 6-8 minutes that Hedwig **adapts oversight from real traces**, using every capability that makes the system novel. Every scene proves one claim visible on-screen. This is the *product* demo, not the reproduction script for the submitted paper's video.

## The narrative

We show Hedwig doing three things no existing system does, in order:

1. **Hedwig explains itself before asking** — session context surfaces before any check-in, so oversight never feels arbitrary.
2. **Hedwig notices a pattern and asks to learn it** — the implicit-preference confirmation moment, Hedwig's clearest "I am learning you" surface.
3. **Hedwig stops proactively when the data says to** — the failure-signal check-in, grounded in the SWE-chat finding (3.4× lift over random).

Plus we close with `hw observe personas` to make the inference layer visible.

## Off-camera setup

```bash
cd demo_task_api
git init 2>/dev/null || true
git restore task_api/api.py task_api/service.py 2>/dev/null || true

export AWS_PROFILE=dev
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_SDK_LOAD_CONFIG=1
export SA_MODEL_ID='arn:aws:bedrock:us-east-1:676534553170:inference-profile/us.anthropic.claude-sonnet-4-6'
export VERIFY_CMD="../.venv/bin/python -m pytest tests -q"

aws sso login --profile dev
hw init --model-id "$SA_MODEL_ID" --region us-east-1
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all

# Pre-seed enough history that the learned scorer is active and that
# `hw observe personas` has something to show. Without this, session 1
# would be fully heuristic-era and the "learned" story doesn't land.
python ../scripts/seed_demo_db.py --repo-root .
```

Confirm baseline:

```bash
rg -n "summary_handler|get_task_summary" task_api/api.py task_api/service.py || true
```

Should print nothing.

## Scene 1 — Rules → two worlds

```bash
hw rules add "Never modify files under locked/."
hw rules add "Prefer small, reversible edits. Always check in on schema changes."
hw config set-mode balanced
hw config set-verification-cmd "$VERIFY_CMD"
```

**What to point at:** the first rule compiles to a hard constraint, the second to behavioral guidance. One sentence from you: *"Hard rules are enforced at the CLI boundary. Soft guidance is retrieved into the prompt when task-relevant. Same input, two completely different enforcement layers."*

**Polish target:** rule-compilation output should clearly show `[hard constraint]` vs `[behavioral guideline]` with color + icon.

## Scene 2 — Session 1: the plan check-in with visible context

```bash
hw run \
'Read task_api/api.py, task_api/service.py, and docs/task_api_spec.md. Add a new /tasks/summary endpoint that returns task counts by status. Preserve the existing list response envelope and all public handler signatures. If you hit an API design tradeoff, pause for a check-in.' \
--spec docs/task_api_spec.md \
--show-intent
```

**Expected scene:**

1. Read-request panel appears. Developer picks `r` (approve + remember).
2. **Model-initiated plan check-in** fires on the design tradeoff. *This is the key moment.*
3. The panel now shows `adapted check-in context` above the prompt — session history, retrieved guidance from the rules, rationale for why Hedwig is pausing.
4. Developer approves plan with `a`.
5. Patch renders inline (green/red/cyan).
6. Developer approves with `a`.
7. Verification runs and passes.

**What to point at:** *"Hedwig isn't just asking. It's explaining what it knows about this session before it asks. Rules we set 30 seconds ago are already retrieved and used."*

**Polish target:** the adapted-check-in panel should feel like one *thoughtful* pause, not a wall of text. Hierarchy: rationale on top in bold, then session signals, then files. Uses the cyan family.

## Scene 3 — Still in session 1: implicit-preference detected

After 2-3 more prompts in session 1 (or some seeded trace history), trigger the hypothesis confirmation. This is the scene the new backend makes possible.

```bash
# Either a third real prompt, or pre-seeded traces make this fire naturally.
hw run 'Add a priority field to the summary endpoint, just do the API layer, don't touch the service layer yet' --spec docs/task_api_spec.md
```

**Expected scene:**

1. Magenta panel: **"Hedwig noticed a pattern: you've narrowed scope on me a few times this session — want me to check in before multi-file changes for the rest of it?"**
2. Developer types `y`.
3. Confirmation message: preference saved, session-scoped.

**What to point at:** *"Hedwig just turned three implicit signals into an explicit preference the developer confirmed. This is what learning looks like — not a black-box model update, but a visible handshake."*

**Polish target:** the magenta panel is the most visible moment in the demo. It needs to feel **intentional and rare**. Not a standard prompt. Uses the magenta family uniquely.

## Scene 4 — Still in session 1: the scope preference fires

Continue session 1 with another multi-file change to demonstrate the just-confirmed preference working.

```bash
hw run 'Extend the summary endpoint to also filter by due-date range. Update both api.py and service.py.'
```

**Expected scene:**

1. Because the developer confirmed the scope-narrowing preference, the preference now matches (blast radius ≥ 2, past turn-position threshold, prior pushbacks exist).
2. **Hedwig check-ins** even though the scorer would have auto-approved.
3. The reason surfaced in the policy snapshot: *"confirmed preference forced check-in"*.

**What to point at:** *"The preference Hedwig just learned is already changing behavior. Same scorer, different outcome, because the developer told Hedwig something about how they want oversight."*

**Polish target:** the reason string should be visually distinct — "learned preference active" as a little badge.

## Scene 5 — Scene optional: the failure-signal proactive check-in

This requires session state with debug intent + bash activity + at least one prior failure. If scene 2-4's trace doesn't produce this, we can force it with a scripted prompt:

```bash
hw run 'The tests are failing intermittently, can you debug why? Run them a few times and figure it out.'
```

**Expected scene:**

1. Hedwig runs a few bash iterations.
2. Eventually a verification failure or developer-flagged error.
3. **Next turn, Hedwig pauses proactively** before more bash.
4. The panel explicitly cites the failure-signal trigger and its grounding: *"Based on SWE-chat analysis of 62K real developer-agent turns, this pattern pre-empts failure reports 3.4× better than a random check-in."*

**What to point at:** *"This trigger isn't heuristic. It's the single most empirically-grounded check-in in the system, and it just fired."*

**Polish target:** this panel needs a distinct visual — red family, the 3.4× stat visible on-panel, a small inline reference to "SWE-chat N=62K" to show the citation surface.

## Scene 6 — `hw observe personas` — the inference made visible

```bash
hw observe personas
```

**Expected scene:** a table showing the session's inferred coding mode, inferred intensity (DELEGATING vs ACTIVE), pushback mix (6 columns), approval rate, and whether the failure-signal would have fired.

**What to point at:** *"Everything I just showed you is observable. Hedwig isn't a black box. Every preference, every pattern, every inferred signal has a command that surfaces it."*

**Polish target:** this table should look like a real product dashboard. Colored intensity column, sparkline-style pushback mix, clear highlighting of the session we just ran.

## Scene 7 — `hw observe weights`: learning made visible

```bash
hw observe weights
```

**Expected scene:** coefficient drift table with three columns (prior, current, delta) for the 13 features of the learned scorer, sample count in the title.

**What to point at:** *"And this is the learned scorer. 13 features, online SGD, every developer decision updates these coefficients. The cold-start value is zero everywhere — every bit of drift is real interaction data."*

**Polish target:** Delta column with green/red bars for direction, subtle animation on largest mover when the table renders.

## Close

End-of-run line: `Learned policy active (N decisions recorded this repo).`

## Timing target

| Scene | Target duration |
|---|---|
| 1. Rules | 30 sec |
| 2. Session 1 plan check-in | 90 sec |
| 3. Implicit-preference confirmation | 45 sec |
| 4. Confirmed preference fires | 60 sec |
| 5. Failure-signal trigger | 45 sec |
| 6. `observe personas` | 30 sec |
| 7. `observe weights` | 30 sec |
| **Total** | **~5 min 30 sec** |

Leaves buffer for discussion. Cuttable if needed: scene 5 if it doesn't happen naturally, or scene 7.

## Reset between runs

```bash
git restore task_api/api.py task_api/service.py
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all
python ../scripts/seed_demo_db.py --repo-root .
```

## What this demo proves

| Claim | Scene that proves it |
|---|---|
| Rules → two-tier governance (hard + soft) | Scene 1 |
| Oversight is contextual, not arbitrary | Scene 2 |
| Hedwig learns from implicit signals | Scene 3 |
| Learned preferences change future behavior | Scene 4 |
| Some triggers are empirically grounded | Scene 5 |
| The system is fully observable | Scenes 6, 7 |
| Online learning is real (not synthetic) | Scene 7 |
