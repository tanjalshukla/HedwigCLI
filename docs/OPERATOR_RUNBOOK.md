# Operator Runbook

This runbook is for running `sc` reliably in the packaged demo flow based on `demo_task_api/`.

## 1) Environment Bootstrap

From the tool repo root:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=<PROFILE>
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_SDK_LOAD_CONFIG=1
aws sso login --profile <PROFILE>
```

Verify account + Bedrock:

```bash
sc doctor --model-id <INFERENCE_PROFILE_ARN> --region us-east-1
```

## 2) Demo Baseline

Run from the demo repo:

```bash
cd demo_task_api
git init   # one-time, if this fixture is not already its own repo
git rev-parse --show-toplevel   # should print .../demo_task_api
sc reset --yes
sc rules constraints-clear --all
sc rules guidelines-clear --all
sc config set-mode balanced
sc config set-verification-cmd "python -m pytest tests -q"
```

This assumes the virtual environment from step 1 is active. If you rehearse from a shell without the venv activated, use an explicit interpreter path instead:

```bash
sc config set-verification-cmd "/absolute/path/to/.venv/bin/python -m pytest tests -q"
```

## 3) Standard Demo Flow

### On-camera rule authoring

```bash
sc rules add "Never modify files under locked/."
sc rules add "For routine validation and service-layer changes, continue autonomously; only check in for API, schema, or security changes."
```

### Session 1

```bash
sc run \
"Read task_api/api.py, task_api/service.py, and docs/task_api_spec.md. Add a new `/tasks/summary` endpoint that returns task counts by status while preserving the existing list response envelope and all public handler signatures. If there is an API design tradeoff, stop and check in with assumptions and options." \
--spec docs/task_api_spec.md \
--show-intent
```

If the model asks whether to extend the existing list route or add a dedicated summary path, use:

```text
Add a dedicated summary path. Do not change the existing list response envelope or handler signatures. Continue autonomously for low-risk internal changes; only check in for API, signature, schema, or security changes.
```

Export after the run using the printed session id:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
```

### Session 2

```bash
sc run \
"Using the same spec, extend the new `/tasks/summary` flow to accept an optional `priority` filter while preserving the existing list endpoint, response envelopes, and handler signatures. Reuse the existing priority validation logic, do not create new files, and continue autonomously for low-risk internal changes." \
--spec docs/task_api_spec.md \
--show-intent
```

Export again:

```bash
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

### Observability close

```bash
sc observe report
```

## 4) Common Failures and Recovery

### `ExpiredToken` / Bedrock 403

Cause: stale session credentials.

Fix:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sso login --profile <PROFILE>
sc doctor --model-id <INFERENCE_PROFILE_ARN> --region us-east-1
```

### `could not resolve credentials from session`

Cause: stale environment overrides or missing profile setup.

Fix:
- unset stale AWS environment variables
- ensure `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, and `AWS_SDK_LOAD_CONFIG` are set
- re-run `aws sso login --profile <PROFILE>`
- re-run `sc doctor ...`

### Model output fails schema validation

Cause: malformed or partial structured output.

Fix:
- rerun once
- keep `--show-intent` enabled
- tighten task wording if needed

### Model does not produce the expected architectural check-in

Cause: task phrasing was too narrow or too permissive.

Fix:
- reset the demo baseline
- use the task wording from `demo_task_api/DEMO_FLOW.md`
- if needed, make the route/interface tradeoff more explicit in the task prompt

## 5) Post-Session Export

Keep both exports:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

## 6) Post-Session Reset

```bash
cd demo_task_api
sc reset --yes
```
