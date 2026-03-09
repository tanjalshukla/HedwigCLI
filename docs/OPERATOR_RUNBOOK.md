# Operator Runbook

This runbook is for running `sc` reliably in demos, lab sessions, and internal studies.

## 1) Session Bootstrap

```bash
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sso login --profile dev
```

Verify account + Bedrock:

```bash
AWS_PROFILE=dev sc doctor --model-id <inference-profile-arn> --region us-east-1
```

## 2) Clean Baseline (Recommended Before Every Study Session)

```bash
git restore demo/checkin/service.py demo/feature.py demo/docs/notes.md
sc rules constraints-clear --all
sc rules guidelines-clear --all
sc observe reset-study-state --yes
sc config set-mode balanced
sc config set-verification-cmd ".venv/bin/python -m py_compile demo/feature.py demo/checkin/service.py"
sc rules import demo/DEMO_RULES.md
```

## 3) Standard Demo Flow

1. Import + inspect rules:
   - `sc rules constraints`
   - `sc rules guidelines`
2. Run multi-file task:
   - `sc run "<task>" --show-intent`
3. Show adaptive state:
   - `sc observe leases`
   - `sc observe traces --limit 20`
4. Show safety block:
   - attempt read/write under `demo/locked/*`
5. Show observability:
   - `sc observe report`
   - `sc observe checkin-stats`

Use `demo/DEMO_COMMANDS.md` for a paste-ready script.

## 4) Common Failures and Recovery

### `ExpiredToken` / Bedrock 403
Cause: stale session credentials.

Fix:
```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sso login --profile dev
AWS_PROFILE=dev sc doctor --model-id <inference-profile-arn> --region us-east-1
```

### `could not resolve credentials from session`
Cause: stale environment overrides.

Fix: same as above; ensure `AWS_PROFILE=dev` is set for command execution.

### Model output fails schema validation
Cause: non-JSON or malformed check-in payload.

Fix:
- rerun task once
- add tighter task wording
- keep `--show-intent` for visibility

## 5) Lab Study Hygiene

- Start each participant from a deterministic baseline with `sc observe reset-study-state --yes`.
- Keep the same verification command across sessions.
- Export traces after each session:

```bash
sc observe export --out .sc/exports
```

## 6) Post-Session Reset

```bash
git restore demo/checkin/service.py demo/feature.py demo/docs/notes.md
sc observe reset-study-state --yes
```
