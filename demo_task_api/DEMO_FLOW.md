# Demo Flow

This flow is optimized for a short research demo, not a full lab session.

Goal:
- show spec-aware planning
- trigger one strong model-initiated architectural check-in
- give one explicit developer preference
- show that the second session reuses that preference and interrupts less

Do not reset between session 1 and session 2.

## Setup

Run from the demo repo:

```bash
cd demo_task_api
sc reset --yes
sc rules import DEMO_RULES.md
sc config set-mode balanced
sc config set-verification-cmd "python -m pytest tests -q"
```

Optional sanity check:

```bash
sc rules constraints
sc rules guidelines
```

## Session 1: spec-aware planning + architectural check-in

```bash
sc run \
"Read task_api/api.py, task_api/service.py, and docs/task_api_spec.md. Add support for a summary view of task counts by status while preserving the existing response envelopes and handler signatures unless a change is clearly needed. If there is an API design tradeoff, stop and check in with assumptions and options." \
--spec docs/task_api_spec.md \
--show-intent
```

Recommended responses:
- approve read access for `task_api/api.py`
- approve the plan
- if the model asks whether to extend the existing list route or add a dedicated summary path, choose the dedicated path and paste:

```text
Add a dedicated summary path. Do not change the existing list response envelope or handler signatures. Continue autonomously for low-risk internal changes; only check in for API, signature, schema, or security changes.
```

- approve the apply step

What to emphasize while recording:
- the plan is grounded in the spec
- the model surfaces a real API design fork instead of silently guessing
- you are shaping future autonomy with explicit preference feedback

After the run, note the printed `Session id=...` and export it:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
```

## Session 2: show learned preference and reduced interruption

Inspect the learned preference state first:

```bash
sc observe preferences
```

Then run the follow-up task:

```bash
sc run \
"Using the same spec, add optional priority filtering and tighten validation messaging while preserving response envelopes and handler signatures. Continue autonomously for low-risk changes and only check in if an API or interface change is required." \
--spec docs/task_api_spec.md \
--show-intent
```

Expected outcome:
- fewer unnecessary check-ins than session 1
- preserved response envelope
- preserved handler signatures
- continued autonomy on service-layer and validation work
- any remaining check-in should be at the API/interface level

After the run, export again:

```bash
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

## Close the demo

Show one or two observability commands:

```bash
sc observe report
sc observe traces --limit 10
```

If you want one deeper view:

```bash
sc observe checkin-stats
```

## What the audience should take away

Session 1:
- the model is spec-aware
- the model can initiate a useful architectural check-in
- the CLI still governs the risky surface

Session 2:
- the system did not just store traces; it reused them
- the previous preference changed how the model and policy behaved
- autonomy increased selectively, not blindly
