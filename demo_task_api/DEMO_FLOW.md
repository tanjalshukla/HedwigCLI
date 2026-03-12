# Demo Flow

This is the final 5-minute demo script.

The story is:
1. Smart Coder distinguishes hard governance from softer behavioral preferences.
2. The coding loop is grounded in a task contract (`--spec`).
3. The model can stop on a meaningful API tradeoff instead of guessing.
4. A preference from session 1 changes behavior in session 2.

Do not reset between session 1 and session 2.

## 5-minute timing

1. `0:00-0:25` - add one hard rule and one behavioral rule
2. `0:25-2:10` - session 1: spec-aware plan + model-initiated API check-in
3. `2:10-3:45` - session 2: semantically related follow-up with fewer interruptions
4. `3:45-5:00` - observability close

## Off-camera setup

Run from the demo repo:

```bash
cd demo_task_api
git init   # one-time; makes this fixture the Smart Coder repo root
git add .
git commit -m "Initial demo fixture"   # optional but useful for a clean baseline
git rev-parse --show-toplevel   # should print .../demo_task_api
sc reset --yes
sc rules constraints-clear --all
sc rules guidelines-clear --all
sc config set-mode balanced
```

Assume the verification command has already been configured once by the operator as:

```bash
sc config set-verification-cmd "python -m pytest tests -q"
```

That setup is operational, not part of the on-camera story.

## Part 1: rule authoring

Show these two commands on camera:

```bash
sc config set-mode balanced
sc rules add "Never modify files under locked/."
sc rules add "For routine validation and service-layer changes, continue autonomously; only check in for API, schema, or security changes."
```

What this proves:
- Smart Coder accepts freeform natural-language rules
- one rule becomes a deterministic CLI-enforced constraint
- one rule becomes prompt-level behavioral guidance
- the system separates task contract, hard governance, and soft preference

Script:
- the first rule is a hard boundary on a protected path
- the second rule is a softer preference about interruption style
- this is the distinction between governance and adaptive conditioning
- do not linger here; the point is classification, not rule administration

## Part 2: session 1

```bash
sc run \
"Read task_api/api.py, task_api/service.py, and docs/task_api_spec.md. Add a new `/tasks/summary` endpoint that returns task counts by status while preserving the existing list response envelope and all public handler signatures. If there is an API design tradeoff, stop and check in with assumptions and options." \
--spec docs/task_api_spec.md \
--show-intent
```

Script responses:
- approve the reads
- approve the plan
- if the model asks whether to extend the existing list route or add a dedicated summary path, choose the dedicated path and respond:

```text
Add a dedicated summary path. Do not change the existing list response envelope or handler signatures. Continue autonomously for low-risk internal changes; only check in for API, signature, schema, or security changes.
```

- approve the apply step

What this proves:
- the plan is grounded in the spec, not just the prompt
- the model surfaces a real API design tradeoff instead of silently guessing
- the user can explicitly calibrate future check-ins
- reviewers can understand the task immediately: add one summary endpoint without breaking the existing API

After the run:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
```

## Part 3: session 2

```bash
sc run \
"Using the same spec, extend the new `/tasks/summary` flow to accept an optional `priority` filter while preserving the existing list endpoint, response envelopes, and handler signatures. Reuse the existing priority validation logic, do not create new files, and continue autonomously for low-risk internal changes." \
--spec docs/task_api_spec.md \
--show-intent
```

Expected outcome:
- fewer unnecessary check-ins than session 1
- preserved response envelopes
- preserved handler signatures
- the model recognizes this as a semantically related follow-up to the earlier summary-endpoint work
- the session bootstrap line should show `history=loaded`
- low-risk follow-up work proceeds with less friction

After the run:

```bash
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

## Part 4: observability close

Show one command. Prefer the report:

```bash
sc observe report
```

What this proves:
- the interaction is fully instrumented
- model-initiated and policy-initiated oversight are distinguishable
- the run is exportable for later analysis

## Audience takeaway

Before coding:
- Smart Coder can compile freeform rules into hard constraints or softer behavioral guidance

Session 1:
- Smart Coder is grounded in an explicit task contract
- the model can issue a useful API-level check-in
- the CLI still governs the risky surface

Session 2:
- prior interaction history changes future behavior
- autonomy increases selectively, not blindly
- the adaptive part is the point; everything else supports that moment

## Evidence to keep

Export both sessions:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

Capture these screenshots:
- one hard-rule compilation example
- one guidance-rule compilation example
- the spec-aware intent summary
- the model-initiated architectural check-in
- the `history=loaded` bootstrap line at session 2 start
- one observability close

Evidence to pull into the paper:
- one example of a hard-rule compilation
- one example of a guidance-rule compilation
- whether session 1 completed successfully with verification passing
- model-initiated vs policy-initiated check-ins in session 1
- whether session 2 required fewer interruptions than session 1
- whether session 2 started from loaded history rather than cold start
- one exported bundle + trace CSV proving the run is fully instrumented
