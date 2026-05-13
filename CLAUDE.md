# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Hedwig (`hw`) is a **governance layer that wraps a coding agent** (Claude via AWS Bedrock). It does not generate code. For each agent-proposed action, Hedwig decides whether to proceed autonomously or pause for developer review, and calibrates that decision from real interaction traces — not static configuration.

Context for any work here: this is a research prototype attached to an accepted ACM CAIS 2026 paper. Reviewer feedback explicitly criticized earlier framing that overclaimed "learning" where the code was running hand-tuned heuristics. The current architecture is the result of a deliberate refactor to make those claims defensible. **Do not reintroduce synthetic training data, do not describe the heuristic scorer as "learned," and do not describe preferences as "per-developer" (they are per-repo — see `trust_db.py`).**

Before proposing any architectural change, read `CONTEXT.md` (domain vocabulary) and `BRAINSTORM.md` (parked ideas and deliberate non-goals).

## Domain vocabulary (use these exactly)

These are load-bearing terms — use them instead of "component," "handler," "service," "feature," etc.:

- **Action** — one agent-proposed operation on one file (read, write, patch, verify).
- **Stage** — `read` / `plan` / `apply` / `verify` / `report`. Authority is granted per stage.
- **Check-in** — a pause for developer review. Always tagged with `initiator`: `model` or `policy`.
- **Hard constraint** — CLI-enforced rule (`always_deny` / `always_check_in` / `always_allow`).
- **Behavioral guideline** — soft prompt-level guidance, retrieved when task-relevant.
- **Decision trace** — immutable per-action record in `decision_traces` (SQLite). The substrate everything learns from.
- **PolicyScorer** — seam (`sc/policy.py`) with two adapters: `HeuristicScorer` (cold-start) and `PolicyClassifier` (online logistic regression, takes over at `MIN_SAMPLES_FOR_LEARNED=10` real decisions).
- **RiskSignals** — pure data object produced by `assess_risk()` in `sc/features.py`. Raw signals only, no weights. Consumed by every scorer.

Verbs: **assess** / **score** / **decide** / **record** / **retrieve** / **revoke**. Do not use *classify*, *estimate*, or *evaluate* as top-level verbs — they were deliberately collapsed into *assess*.

## Commands

Install (requires Python 3.11+, AWS SSO + Bedrock access to a Claude inference profile):

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
```

Run the CLI (entry points: `hw` preferred, `sc` alias):

```bash
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw doctor --model-id <inference-profile-arn> --region us-east-1
hw run "Add a small unit test for function X in foo.py" --show-intent
```

Tests:

```bash
# Full suite (from repo root, 130+ tests, ~2s)
PYTHONPATH=. .venv/bin/python -m pytest tests -q

# Single test file
PYTHONPATH=. .venv/bin/python -m pytest tests/test_policy_scorer.py -q

# Single test
PYTHONPATH=. .venv/bin/python -m pytest tests/test_features.py::AssessRiskTests::test_assess_risk_aggregates_all_signals -q

# Demo fixture tests (separate PYTHONPATH)
PYTHONPATH=demo_task_api .venv/bin/python -m pytest demo_task_api/tests -q
```

Local state: `.sc/config.json`, `.sc/trust.db` (both per-repo).

## Architecture overview

### Approval cascade (the core control flow)

Every file access, for read and write separately, flows through this cascade in `sc/run/read_stage.py` and `sc/run/apply_stage.py`:

1. **Hard constraints** (`sc/constraints.py`, stored in `trust_db.hard_constraints`) — override everything.
2. **Active leases** (`trust_db.leases` / `trust_db.read_leases`) — temporary trust grants.
3. **PolicyScorer** (`sc/policy.py` → `sc/ml_policy.py`) — `select_scorer()` picks heuristic or learned; scores an `assess_risk()` result.
4. **Threshold adaptation** (`sc/autonomy.py::adjusted_policy_thresholds`) — shifts thresholds based on `AutonomyPreferences` and model-initiated-check-in calibration.

Understanding one approval decision requires reading these four files in that order. `sc/run/helpers.py::_policy_decision_for_file` is the convergence point.

### The PolicyScorer seam

Two adapters, real seam. Don't add a third without making it a `PolicyScorer` per `sc/policy.py`. The learned scorer takes over only after ≥ 10 real `update()` calls — enforced by `PolicyClassifier.ready()`, checked in `select_scorer()`. Which scorer fired for a given decision is recorded in `PolicyDecision.reasons` and lands in `decision_traces.policy_reasons` — longitudinal analysis can separate heuristic-era from learned-era traces.

No synthetic training data. `build_cold_classifier()` in `sc/ml_policy.py` seeds an `SGDClassifier(loss="log_loss")` with a single zero+one pair (so `partial_fit` has seen both classes) and nothing else. Cold-start behavior is entirely carried by `HeuristicScorer`.

### Two preference surfaces, and their precedence

There are two preference systems in the codebase, and they don't fight — they compose. Know which is which before editing either:

- **`AutonomyPreferences`** (`sc/autonomy.py`) — coarse, repo-scoped toggles (`prefer_fewer_checkins`, `allowed_checkin_topics`, `scoped_paths`). Consumed by `adjusted_policy_thresholds` to shift the scorer's proceed/flag thresholds. This is the legacy surface; inferred from model payloads and from `merge_preferences`.
- **`Preference`** (`sc/preferences.py`) — the 5-dim taxonomy (Trigger/Condition/PreferenceAction/Scope/Lifecycle). Matched per-file per-action in `apply_stage.py`. Populated by: (a) built-in defaults like `FAILURE_SIGNAL_CHECKIN`, (b) developer confirmations via the hypothesis flow.

**Precedence**: `AutonomyPreferences` adjusts thresholds *before* the scorer fires (threshold shift). `Preference` matches *after* the scorer decides (action override). A matched `Preference` can tighten a `proceed` to a `check_in` but **never** loosens a `check_in` to `proceed` — see `apply_stage.py::_evaluate_apply_stage` where the override only fires `if decision.action != "check_in"`. This asymmetry is deliberate: preferences add caution, they don't remove it.

### RiskSignals

Risk signals are computed once per action by `assess_risk()` in `sc/features.py` and threaded through as a single `RiskSignals` object. **Do not** reintroduce the parallel-dict pattern (`apply_change_types` / `apply_diff_sizes` / ...) that was deliberately removed — if you find yourself wanting to pass 5 separate args about one action, you want a `RiskSignals`. `features.py` is the single source of truth for change-pattern categories (see `CHANGE_PATTERNS`).

### Persistence and observability

`sc/trust_db.py` is one large file (2000+ lines) that owns the SQLite schema, CRUD, analytics, retrieval, and export. It's on the architecture backlog to split (see `BRAINSTORM.md`), but splitting it is deferred — it works and the churn risk is high. Eight tables; `decision_traces` is the primary artifact for any post-hoc analysis.

`hw observe` surfaces this: `hw observe report` (summary), `hw observe traces` (raw trace browse), `hw observe weights` (learned classifier drift vs. cold-start), `hw observe preferences` / `preferences-revoke`.

### Agent client

`sc/agent_client.py` wraps `AnthropicBedrock` with a strict structured-JSON protocol (`sc/schema.py`): `read_request`, `intent_declaration`, `file_update`, `check_in_message`, `plan_revision`. The model is untrusted — every structured output is validated before the CLI acts on it.

## Writing code in this repo

- **Prefer editing over adding.** The codebase was just refactored to be leaner. Before adding a new module or helper, check whether the concept already has a home (likely in `features.py`, `policy.py`, `autonomy.py`, or `run/helpers.py`).
- **Tests live in `tests/`.** New seams get their own test file (`test_policy_scorer.py`, `test_features.py`, `test_diff_view.py` are the templates). Smoke-test rich-rendered UI with the pattern in `test_diff_view.py` — assert no-raise on representative inputs rather than comparing terminal output.
- **Do not touch `sc/` internals without running the full test suite** afterward — even small changes in `apply_stage.py` or `helpers.py` ripple through many traces.
- **`policy.py` weights are documented priors, not tuning targets.** They live in SPEC.md's weight table. If you change them, update the table in the same commit.
- **When in doubt about scope, read `CONTEXT.md` first.** It's the short file; it tells you what is and isn't Hedwig's job.

## Reading order for new work

For policy/trust/adaptation changes: `features.py` → `policy.py` → `ml_policy.py` → `autonomy.py` → `plan_gate.py` → `trust_db.py`.

For the preference + hypothesis pipeline: `preferences.py` (schema + matching) → `preference_inference.py` (session signals + hypothesis generation) → `run/apply_stage.py` (where both are consumed).

For the run loop: `run/command.py` → `run/read_stage.py` → `run/model.py` → `run/apply_stage.py` → `run/apply_ui.py` (rendering) → `run/helpers.py` (policy seam).

## What *not* to add

These are deliberate non-goals, all in `BRAINSTORM.md` for context:

- A third `PolicyScorer` adapter (bandit, RF, etc.) — parked until after camera-ready.
- Per-developer (vs. per-repo) preferences — parked.
- Splitting `trust_db.py` — deferred; high churn risk, low leverage right now.
- Replacing the approval-cascade duplication between `read_stage.py` and `apply_stage.py` — the biggest structural win but the riskiest; post-conference.
- Any "learned" language in code comments or docstrings that isn't backed by `PolicyClassifier`. This was the reviewer-148D critique; don't reintroduce it.
