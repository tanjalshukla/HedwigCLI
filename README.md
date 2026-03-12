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

## Research Inputs

Smart Coder is informed by a small set of papers and practitioner findings rather than a single source:

- *Overseeing Agents Without Constant Oversight: Challenges and Opportunities*
- *CowCorpus* (Huq et al., 2025, arXiv 2602.17588)
- Grunde-McLaughlin et al. (2025, arXiv 2602.16844)
- *PAHF* (Liang et al., 2026, arXiv 2602.16173)

The README stays focused on setup and usage. `SPEC.md` contains the architecture details, what we took from these papers, and what remains unimplemented.

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

Import structured rules from AGENTS/CLAUDE-style files:

```bash
sc rules import path/to/RULES.md
sc rules constraints
sc rules guidelines
```

Add a freeform natural-language rule. Smart Coder compiles it into enforced constraints and/or prompt-level guidance:

```bash
sc rules add "Never modify files under `config/prod/*`."
sc rules add "Only check in for API or schema changes."
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

The demo target repo is `demo_task_api/`.

- Two-session demo flow: `demo_task_api/DEMO_FLOW.md`
- Operator steps: `docs/OPERATOR_RUNBOOK.md`

### Install and Run the Demo

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
export AWS_PROFILE=<PROFILE>
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_SDK_LOAD_CONFIG=1
sc doctor --model-id <INFERENCE_PROFILE_ARN> --region us-east-1
cd demo_task_api
git init   # one-time, if the demo fixture is not already its own repo
git rev-parse --show-toplevel   # should print .../demo_task_api
sc reset --yes
sc rules constraints-clear --all
sc rules guidelines-clear --all
sc config set-mode balanced
sc config set-verification-cmd "python -m pytest tests -q"
```

## Notes

- Bedrock model ID can be set via `--model-id` or `SA_MODEL_ID`.
- Read context is truncated by `read_max_chars` in `.sc/config.json`.
- Numeric policy knobs remain in `.sc/config.json`, but the normal user-facing control is `autonomy_mode`.
