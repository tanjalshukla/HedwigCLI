# Hedwig

**Hedwig** (`hw`) is a governance layer that wraps LLM coding agents. It sits between the developer and the agent and decides — for each proposed action — whether to proceed autonomously or pause for review. Oversight calibrates from real interaction traces, not static configuration.

> Accepted demo paper — ACM Conference on AI and Agentic Systems (CAIS) 2026.

## The three pillars

**1. Rule compilation** — Developers write rules in plain English. Hedwig compiles them into hard constraints (CLI-enforced, non-negotiable) or behavioral guidelines (retrieved into the agent's prompt when relevant). No DSL, no config files.

```bash
hw rules add "Never modify anything under config/prod/"
hw rules add "Always check in before changing the API schema"
```

**2. Learned scorer** — Every developer decision (approve / deny / push back) updates an online logistic regression classifier. Cold-starts with a hand-weighted heuristic; the learned model takes over after 10 real decisions. Platt-calibrated so confidence scores are meaningful. The developer can inspect exactly what the scorer has learned.

```bash
hw observe weights    # see how the classifier has shifted from cold-start
hw observe report     # session history, regret count, calibration
```

**3. Hypothesis bank** — Hedwig watches behavioral patterns across the session. When a pattern accumulates enough evidence (e.g. "you've narrowed scope on multi-file changes 4 times, 80% confidence"), it surfaces a single question: "Want me to treat this as a standing rule?" Confirmed hypotheses persist at repo scope. Contradicted ones are stored as rejected evidence, not silently discarded.

```bash
/prefs    # inside the REPL: see accepted preferences, pending hypotheses, rejected candidates
```

## Install

Requirements: Python 3.11+, AWS SSO configured, Bedrock access to a Claude inference profile.

```bash
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
```

## Quick start

```bash
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw doctor  # verify credentials and Bedrock connectivity

# Start a governed session (REPL — tasks persist in a shared session)
hw
```

Inside the REPL:
```
hedwig> add pagination to list_tasks in task_api/service.py
hedwig> /prefs        # see what Hedwig has learned
hedwig> /oversight    # adjust how closely Hedwig watches (hands-on / balanced / delegating)
hedwig> /retrospective  # session-end calibration: where was I too loose or too cautious?
hedwig> /exit
```

Or single-shot:
```bash
hw run "add a test for the pagination edge case"
```

## Repository structure

```
sc/                     # core package
  features.py           # RiskSignals — what Hedwig knows about a proposed change
  policy.py             # PolicyScorer seam (heuristic + learned adapters)
  ml_policy.py          # online logistic regression classifier
  autonomy.py           # threshold adaptation, AutonomyPreferences
  plan_gate.py          # phase-based write boundaries
  preference_inference.py  # session signals + hypothesis generators
  hypothesis_bank.py    # evidence accumulation, surfacing, pruning
  preferences.py        # 5-dim preference taxonomy + matching
  regret.py             # auto-approve correction signal
  trust_db.py           # SQLite facade (schema + dataclasses)
  store/                # persistence layer split by responsibility
    trace_store.py      # decision traces, history, calibration
    rule_store.py       # constraints, guidelines, logic notes
    lease_store.py      # temporary and permanent trust grants
    pref_store.py       # preferences, hypothesis bank
    model_store.py      # classifier blobs, snapshots
  run/                  # governed run loop
    repl.py             # hw REPL (persistent session)
    apply_stage.py      # write policy + approval cascade
    apply_ui.py         # apply-stage rendering (separated from logic)
    hypothesis_ui.py    # hypothesis confirmation panel
    retrospective.py    # post-session calibration
  commands/             # CLI commands (hw observe, hw rules, hw doctor)

demo_recipe_api/        # booth demo fixture (recipe API — open-ended, visitor-extensible)
tests/                  # 288 tests, ~3s full suite
```

## Observability

```bash
hw observe report     # prose summary: actions, check-ins, regret, learning state
hw observe weights    # classifier coefficient drift from cold-start
hw observe personas   # session intensity breakdown
hw observe export --html  # full HTML report for researchers
```

Inside the REPL, `/prefs` shows accepted preferences and pending hypotheses with confidence bars (the developer-facing view of what Hedwig has learned).

## Testing

```bash
# Hedwig core suite
PYTHONPATH=. .venv/bin/python -m pytest tests -q

# Demo fixture (recipe API — booth demo)
PYTHONPATH=demo_recipe_api .venv/bin/python -m pytest demo_recipe_api/tests -q
```

## Key concepts

See [`CONTEXT.md`](CONTEXT.md) for the full domain vocabulary (Action, Stage, Check-in, PolicyScorer, RiskSignals, Hypothesis Bank, REPL, O1-O5 criteria, etc.).

See [`SPEC.md`](SPEC.md) for the runtime architecture, policy weights, and data model.
