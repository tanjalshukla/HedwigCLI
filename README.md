# Hedwig

Hedwig (`hw`) is a **governance layer that wraps a coding agent** (e.g. Claude via AWS Bedrock). The agent reads, plans, and proposes code changes. Hedwig decides, for each action, whether to proceed autonomously or pause for developer review — and calibrates that decision from observed interaction traces instead of static configuration.

The system is not trying to outperform Claude Code at code generation. It sits above it and adds selective, inspectable oversight. Routine, repeatedly-approved work gets smoother; risky or novel work stays governed.

## What Hedwig does

- Compiles freeform developer rules into either **hard constraints** (CLI-enforced) or **behavioral guidelines** (retrieved into the prompt).
- Evaluates every agent-proposed action through an approval cascade: hard constraint → active lease → policy scorer.
- Scores actions with a `PolicyScorer` seam that has two adapters: a hand-weighted **heuristic** (`sc/policy.py`) carrying cold-start behavior, and an **online logistic regression** (`sc/ml_policy.py`, SGD, log-loss) that takes over once ≥ 10 real developer decisions have accumulated.
- Separates **model-initiated** check-ins (the agent asked) from **policy-initiated** check-ins (Hedwig decided) and logs which fired.
- Records every decision as an inspectable trace; `hw observe` surfaces history, weights, preferences, and exports.

## Install

Requirements:

- Python 3.11+
- AWS IAM Identity Center (SSO) configured locally
- Bedrock access to a Claude inference profile

Setup:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
```

## Quick start

```bash
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw doctor --model-id <inference-profile-arn> --region us-east-1
hw run "Add a small unit test for function X in foo.py" --show-intent
```

Local state lives in `.sc/config.json` and `.sc/trust.db`.

## Core commands

```bash
hw run "Update foo.py and add tests" --show-intent
hw rules add "Never modify files under config/prod/."
hw rules list
hw config set-mode balanced
hw observe report
hw observe traces --limit 20
hw observe export --out .sc/exports
```

## Demo

The public demo fixture lives in [`demo_task_api/`](demo_task_api).

- [`demo_task_api/DEMO_FLOW.md`](demo_task_api/DEMO_FLOW.md) — steps to reproduce the filmed two-session demo
- [`demo_task_api/README.md`](demo_task_api/README.md) — what the fixture contains

The demo shows: freeform rule compilation, a model-initiated architectural check-in in session 1, reduced friction + retrieved prior guidance in session 2, and exportable observability via `hw observe report`.

## Architecture

For the runtime, policy, and data model, see [`SPEC.md`](SPEC.md). For the domain vocabulary this project uses consistently (Action, Stage, Check-in, PolicyScorer, RiskSignals, etc.), see [`CONTEXT.md`](CONTEXT.md). For parked research ideas and follow-ups, see [`BRAINSTORM.md`](BRAINSTORM.md).

## Testing

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests -q
PYTHONPATH=demo_task_api .venv/bin/python -m pytest demo_task_api/tests -q
```
