# Smart Coder

Smart Coder (`sc`) is a governance-first coding agent CLI. The model proposes; the CLI enforces.

Core design:
- untrusted model with strict JSON protocol
- local policy and constraint enforcement
- trace-backed adaptive autonomy
- explicit read/apply checkpoints when risk is high

## Why It Exists

Most coding agents force a bad tradeoff: either approve everything manually, or trust the agent too broadly. Smart Coder is built to study and improve that boundary. It learns from developer interaction history, but keeps all authority in the local CLI:

- the model can request reads, propose plans, suggest check-ins, and generate updates
- the CLI decides what is actually read, written, verified, or blocked
- every decision is traced so autonomy can adapt over time and be studied later

For architecture, internals, and future work, see `SPEC.md`.

## Prerequisites

- Python 3.11+
- AWS IAM Identity Center (SSO) configured
- Bedrock access to a Claude inference profile (ID/ARN)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
```

After install, use `sc` as the CLI entry point. `python -m sc` still works.

## Quick Start

```bash
sc init --model-id <inference-profile-arn> --region us-east-1
sc doctor --model-id <inference-profile-arn> --region us-east-1
sc run "Add a small unit test for function X in foo.py" --show-intent
```

This creates local state in `.sc/config.json` and `.sc/trust.db`.

## Command Groups

- `sc config ...` config updates (`set-mode`, `set-verification-cmd`)
- `sc rules ...` import/manage hard constraints and guidelines
- `sc observe ...` traces, leases, explainability, stats, preferences

Legacy top-level aliases still work for compatibility.

## Core Workflows

Run with intent visibility:

```bash
sc run "Update foo.py and add tests" --show-intent
```

Inspect live policy state:

```bash
sc observe leases
sc observe traces --limit 20
sc observe report
```

Import rules from AGENTS/CLAUDE-style files:

```bash
sc rules import demo/DEMO_RULES.md
sc rules constraints
sc rules guidelines
```

Compile a direct natural-language path rule into an enforced constraint:

```bash
sc rules add "Never modify files under `config/prod/*`."
```

Set autonomy mode:

```bash
sc config set-mode balanced
sc config set-mode milestone
```

Reset learned autonomy preferences:

```bash
sc observe preferences-clear --yes
```

Export the latest session bundle for analysis:

```bash
sc observe export --out .sc/exports
```

## Enforcement Semantics

- Constraint precedence: `always_deny` > `always_check_in` > `always_allow`
- Phase gates:
  - research: no writes
  - planning: markdown-only writes
  - implementation/review: writes allowed under policy
- Apply is all-or-nothing: writes are staged and atomically replaced per file; failures abort with cleanup.

## Testing

```bash
.venv/bin/python -m pytest tests -q
```

## Demo

- Copy-paste demo script: `demo/DEMO_COMMANDS.md`
- Operator steps: `docs/OPERATOR_RUNBOOK.md`

## Notes

- Bedrock model ID can be set via `--model-id` or `SA_MODEL_ID`.
- Read context is truncated by `read_max_chars` in `.sc/config.json`.
- Numeric policy knobs remain in `.sc/config.json`, but the normal user-facing control is `autonomy_mode`.
