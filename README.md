# Hedwig

Hedwig (`hw`) is a governance-first coding agent CLI. The model can
read, plan, ask for guidance, and propose code changes. The local CLI
decides what is actually allowed, what requires review, and what gets
verified.

The point of the project is not full autonomy. It is selective,
inspectable reduction in friction: routine work should get smoother as
the system learns from prior interaction, while risky work remains
governed.

## What Hedwig Does

- Compiles freeform developer rules into either hard constraints or
  softer behavioral guidance.
- Separates model-initiated check-ins from policy-initiated check-ins.
- Learns per-developer approval preferences from interaction traces via
  an online logistic regression policy (warm-started from heuristic
  priors, updated per decision via `partial_fit`).
- Retrieves relevant prior guidance and corrections into later tasks.
- Runs verification after approved changes.
- Exports traces and session bundles for later analysis.

**Policy engine:** The approval policy is a logistic regression
classifier initialized from heuristic engineering priors and updated
online from developer approvals and denials stored in `decision_traces`.
Coefficients are persisted per repository and are directly inspectable
via `hw observe weights`. A heuristic fallback is used until 10 real
decisions have been recorded. The weights and feature definitions are
documented in `SPEC.md`.

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

The primary CLI entry point is:

```bash
hw
```

All public docs use `hw`. The older `sc` command remains available as a
compatibility alias.

## Quick Start

```bash
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw doctor --model-id <inference-profile-arn> --region us-east-1
hw run "Add a small unit test for function X in foo.py" --show-intent
```

Local Hedwig state lives in:

- `.sc/config.json`
- `.sc/trust.db`

## Core Commands

```bash
hw run "Update foo.py and add tests" --show-intent
hw rules add "Never modify files under config/prod/."
hw rules list
hw config set-mode balanced
hw observe report
hw observe traces --limit 20
hw observe export --out .sc/exports
```

## Demo Video Reproduction

The public demo fixture lives in [`demo_task_api/`](demo_task_api).

Start here:

- [`demo_task_api/DEMO_FLOW.md`](demo_task_api/DEMO_FLOW.md) — exact
  steps to reproduce the filmed two-session demo
- [`demo_task_api/README.md`](demo_task_api/README.md) — what the demo
  fixture contains

The demo shows:

1. Freeform rule compilation into hard constraints vs. behavioral
   guidance.
2. A model-initiated architectural check-in during session 1.
3. Reduced friction plus retrieved prior guidance during session 2.
4. Exportable observability through `hw observe report`.

## Repository Layout

- [`sc/`](sc) — Hedwig CLI implementation, policy logic, prompts, and
  runtime orchestration
- [`demo_task_api/`](demo_task_api) — the small reproducible fixture used
  in the public demo
- [`docs/READING_ORDER.md`](docs/READING_ORDER.md) — fastest path
  through the codebase
- [`SPEC.md`](SPEC.md) — architecture notes, policy details, and
  research framing

## Architecture Notes

For implementation details, policy logic, and the research-oriented
architecture notes, see [`SPEC.md`](SPEC.md).

## Testing

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests -q
PYTHONPATH=demo_task_api .venv/bin/python -m pytest demo_task_api/tests -q
```
