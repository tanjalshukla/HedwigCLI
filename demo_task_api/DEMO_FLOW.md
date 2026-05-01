# Reproducing the Hedwig Demo Video

This file documents the exact flow used for the public Hedwig demo.

The demo is intentionally short and shows four things:

1. Freeform rules become either hard constraints or behavioral guidance.
2. Session 1 produces a model-initiated design check-in.
3. Session 2 reuses prior interaction history to reduce friction.
4. The full run is observable through `hw observe report`.

## Demo Structure

- Session 1: add a summary endpoint
- Session 2: extend that endpoint with an optional priority filter

The important behavior is cross-session adaptation, not the size of the
code change.

## Off-Camera Setup

Run from this directory:

```bash
cd demo_task_api
git init
git restore task_api/api.py task_api/service.py

export AWS_PROFILE=dev
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_SDK_LOAD_CONFIG=1
export SA_MODEL_ID='arn:aws:bedrock:us-east-1:676534553170:inference-profile/global.anthropic.claude-sonnet-4-20250514-v1:0'
export VERIFY_CMD="/Users/tanjalshukla/dynamic_autonomy_mvp/.venv/bin/python -m pytest tests -q"

aws sso login --profile dev
hw init --model-id "$SA_MODEL_ID" --region us-east-1
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all

# Pre-seed ML policy with synthetic developer history so `hw observe weights`
# shows real coefficient drift during the demo, not a flat prior-only table.
python ../scripts/seed_demo_db.py --repo-root .
```

Before filming, confirm the fixture is at the pre-session1 baseline:

```bash
rg -n "summary_handler|get_task_summary" task_api/api.py task_api/service.py || true
```

That command should print nothing. If it prints anything, run:

```bash
git restore task_api/api.py task_api/service.py
```

## Guidance to Paste During Session 1

When Hedwig asks for optional architectural guidance, paste exactly:

```text
Use the nested object response. Preserve the existing response envelope and all public handler signatures. Make the new summary handler follow the same input style as existing read handlers. Prefer fewer check-ins. For low-risk internal changes, continue autonomously. Only check in for API, signature, schema, or security changes.
```

## On-Camera Flow

### 1. Add the two startup rules

```bash
hw rules add "Never modify files under locked/."
hw rules add "For routine backend changes, reuse existing validation helpers and avoid creating new files unless clearly necessary."
```

Then set the starting mode and verification command:

```bash
hw config set-mode balanced
hw config set-verification-cmd "$VERIFY_CMD"
```

What this shows:

- the first rule becomes a hard constraint
- the second rule becomes softer behavioral guidance
- Hedwig starts from a balanced cold-start mode
- verification is explicitly configured

### 2. Session 1

Run:

```bash
hw run \
'Read task_api/api.py, task_api/service.py, and docs/task_api_spec.md. Add a new /tasks/summary endpoint that returns task counts by status while preserving the existing list response envelope and all public handler signatures. If there is an API design tradeoff, stop and check in with assumptions and options.' \
--spec docs/task_api_spec.md \
--show-intent
```

Expected interaction:

- Hedwig prints `Session mode balanced`
- Hedwig asks to read:
  - `task_api/api.py`
  - `task_api/service.py`
  - `docs/task_api_spec.md`
- choose `r` at the read prompt:

```text
Approve once (a), approve & remember (r), or deny (d) [a/r/d]:
```

- Hedwig raises a model-initiated planning check-in about handler style
- choose option `1`
- paste the guidance text above
- approve the plan with `a`
- approve the patch with `a`

Expected session-1 result:

- changes applied to:
  - `task_api/api.py`
  - `task_api/service.py`
- verification passed
- end-of-run summary shows one check-in during the run

### 3. Optional code reveal after session 1

If you want to show the change in the editor, open:

- `task_api/api.py`
- `task_api/service.py`

### 4. Session 2

Run:

```bash
hw run \
'Using the same spec, extend the new /tasks/summary flow to accept an optional priority filter while preserving the existing list endpoint, response envelopes, and the already-added summary-handler signature. Work only in task_api/api.py and task_api/service.py, reuse the existing service-layer priority validation behavior, do not create new files, and continue autonomously for low-risk internal changes.' \
--spec docs/task_api_spec.md \
--show-intent
```

Expected interaction:

- Hedwig prints `Session mode balanced`
- reads are auto-approved from remembered access
- the reduced-friction block appears immediately:
  - `Reduced friction (read): ...`
  - `Retrieved guidance: ...`
  - `Autonomy rationale (read): ...`
- the retrieved guidance should reflect the pasted session-1 guidance, not just the broad startup rule
- a plan checkpoint still appears; approve it with `a`
- the apply stage should auto-approve the patch and flag it for review rather than prompting again

Expected session-2 result:

- patch updates:
  - `task_api/api.py`
  - `task_api/service.py`
- Hedwig prints:
  - `Apply approved. Flagged for review:`
  - both files
  - `Verification passed.`

### 5. Optional code reveal after session 2

If you want to show the follow-up change in the editor, open:

- `task_api/api.py`
- `task_api/service.py`

### 6. Inspect stored policies

Run:

```bash
hw observe preferences
```

Expected highlights:

- `Prefer fewer check-ins: yes` (inferred from session-1 guidance)
- `Allowed check-in topics: api, schema, security, signature`
- Effective scoring bands table shows how those preferences shifted the
  auto-approve threshold

This demonstrates what the stored policies look like in practice, and
shows the audience why session-2 produced fewer prompts than session-1.

Point out: these preferences can be revoked individually without resetting
everything. For example, if the developer no longer trusts the agent with
API changes:

```bash
hw observe preferences-revoke --topic api
```

This tightens oversight selectively rather than wiping the slate. It
directly demonstrates the claim that oversight can both loosen and tighten
across sessions.

### 7. Show the learned policy weights

Run:

```bash
hw observe weights
```

Expected output: a table of all 13 feature coefficients with three columns —
prior (warm-start from heuristic), current (updated from trace history),
and signed delta. The sample count in the table title shows how many real
developer decisions have been incorporated.

Key talking point: the warm-start priors encode the hand-crafted heuristic
weights. Every approve/deny decision recorded in the trace log shifts
the coefficients toward this developer's actual risk preferences via online
logistic regression. The delta column shows which risk signals have moved
most — for example, `change_pattern_risk` and `prior_denials` typically
shift after just two sessions. This is Hedwig's trace-driven personalization
made directly observable.

After showing the table, point out that the end-of-session summary also
reports the sample count:

```
Learned policy active (N decisions recorded this repo).
```

### 8. Observability close

Run:

```bash
hw observe report
```

Expected highlights:

- model-initiated and policy-initiated oversight are separated
- verification passed
- the interaction is fully traced

Point to:

- `model_proactive`
- `policy`

## Best Moments to Capture

Use these for screenshots or the video pause:

1. Session 1:
   - the model check-in with the two interface options
2. Session 2:
   - `read: prior approvals N -- guidance: "..."` compact auto-approve line
   - end-of-session: `Check-ins: 1 policy-initiated.` vs session-1's higher count
3. Preferences inspection:
   - `hw observe preferences` table showing inferred topics and scoring bands
   - `hw observe preferences-revoke --topic api` showing that oversight can tighten
4. Learned policy weights:
   - `hw observe weights` table: prior vs. current coefficients with signed deltas
   - sample count in the title confirming real decisions drove the update
   - end-of-session line: `Learned policy active (N decisions recorded this repo).`
5. Observability close:
   - `model_proactive` vs `policy` separation in `hw observe report`

## If You Need to Reset and Rerun

```bash
git restore task_api/api.py task_api/service.py
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all
python ../scripts/seed_demo_db.py --repo-root .
```

## Troubleshooting

- If session 1 says the task is already complete, the fixture is not at
  the pre-session1 baseline. Run the reset block above and confirm that
  `task_api/api.py` does not contain `summary_handler`.
- If session 2 does not mention `priority`, stop and rerun from the
  clean baseline. The intended follow-up change updates both
  `task_api/api.py` and `task_api/service.py`.
- If Bedrock authentication fails, rerun `aws sso login --profile dev`
  before restarting the demo flow.
