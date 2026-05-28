# Hedwig

**Hedwig** is a governance harness for LLM coding agents. It sits between the developer and the agent and calibrates per-action autonomy from real interaction traces — no static configuration, no manual threshold tuning.

> Accepted demo paper — ACM Conference on AI and Agentic Systems (CAIS) 2026.

---

## The problem

Current coding agents either pause too much (every file write requires approval) or too little (full auto-pilot with no oversight). Neither adapts. The developer ends up either babysitting the agent or losing track of what it changed.

Hedwig's position: **oversight should calibrate from how this developer actually behaves in this repo**, not from a global config someone set once or probabilistic model context.

While built for coding agents, the architecture generalizes to any agent operating on behalf of a user in a high-stakes environment. An agent modifying a pipeline, updating a schema, or executing queries against production data faces the same core problem: when should it proceed autonomously, and when should it pause? The trace substrate, online classifier, and preference inference layer are domain-agnostic, with the change being in the risk signals and the action types, not the governance loop.

---

## What's novel

**1. Oversight that adapts from real decisions, not config.** Every approve, deny, and pushback updates an online classifier, with the developer is the labeler. The system cold-starts with defensible defaults and shifts toward what *this developer* actually accepts. Run `/weights` at any point to see exactly which signals have shifted and in which direction.

**2. A prompt that gets smarter over time.** Every task rebuilds the agent's system prompt from repo-scoped memory: rules the developer has added, past corrections, and facts about the codebase — retrieved by keyword overlap with the current task (and soon to be embedding retrieval). The agent opens each session with a "What we've learned about this repo" paragraph. It walks in with proper orientation to the codebase and developer preferences.

**3. Two channels for developer preferences — explicit and inferred.** Preferences expressed in plain English (`/rules add "Prefer composition over inheritance"`) are compiled into behavioral guidelines and retrieved into the agent's prompt on every relevant task. The agent reads them and follows them — no DSL, no config files. Separately, governance preferences (when to pause, when to trust) are inferred from behavior: "you consistently push back when the agent touches multiple files at once" surfaces as a candidate pause trigger on high blast radius. Both channels are repo-scoped, both persist across sessions, and neither requires upfront configuration.

**5. A hypothesis bank that surfaces standing rules without asking prematurely.** Hedwig watches for behavioral patterns across the session, such as scope-narrowing on multi-file changes, deliberate review pacing, or repeated pushback on certain file types. Two generators propose candidates: deterministic rule-based detectors that run every turn, and an LLM that reads the recent trace digest. When a pattern accumulates enough evidence, Hedwig surfaces exactly one question: "Want me to treat this as a standing rule?" Confirmed patterns persist at repo scope and shape future decisions. Declined ones stay in the bank with their evidence — nothing is silently discarded, and the developer can see exactly what Hedwig observed. LLM-proposed candidates must cite real interaction IDs; hallucinated evidence is dropped before storage.

**6. A regret loop that corrects past over-trust.** When an auto-approved action is later denied or fails verification, Hedwig treats it as a training signal that the classifier was too permissive. Corrected exactly once per event, tracked persistently.

**7. Co-change memory across sessions.** Files that have historically moved together under the same task surface at write time: *"`store.py` — historically co-changes with `models.py` (2 tasks)"*. This is what rules and preferences can't express, since it's learned from what actually happened, not from what the developer thought to write down.

**8. Taxonomy grounded in real behavioral data.** The response categories, session signals, and preference schema come from analyzing 5,776 real coding-agent sessions (SWE-chat). Key finding: developer style is not stable across their own sessions (ICC = 0.249) — per-developer personalization would encode noise. The repo is the stable ground truth.

---

## Architecture in brief

Every file action flows through a five-layer cascade:

1. **Hard rules** — compiled from plain English, non-negotiable
2. **Trust grants** — temporary leases from prior approve+remember decisions
3. **Threshold adjustment** — four additive shifts computed first, responding to session-level signals (engagement level, coding mode, model check-in calibration, persistent mode). These set the bar the score must clear. Hard-coded constants rather than learned weights, as session-level responsiveness is needed from turn 1, before the classifier has enough data to learn these effects. The SWE-chat findings are encoded directly as inductive bias.
4. **Decision model** — deterministic risk assessment produces a raw score; optional second-opinion model reviewer nudges it (separate system prompt, no access to agent intent); score compared against adjusted thresholds → verdict
5. **Preference override** — confirmed behavioral patterns tighten the verdict; never loosen it

The full cascade detail, session signals, and preference matching logic are in [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md).

---

## Observability

Everything inspectable from inside the REPL:

```bash
/showcase       # all surfaces at once — leave up as a live display
/weights        # classifier drift from starting point (▲▼ per feature)
/prefs          # accepted preferences, pending patterns with evidence bars, rejected candidates
/cochange       # files that historically move together in this repo
/context        # what was retrieved from repo memory for the last task
/retrospective  # regret events — where Hedwig was too loose or too cautious
/status         # current session: engagement level, coding mode, model state

hw observe export --html   # full HTML report: traces, weights, hypothesis bank, regret
```

---

## Install

Requirements: Python 3.11+, AWS SSO configured, Bedrock access to a Claude inference profile.

```bash
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install --no-build-isolation -e .
```

```bash
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw              # start the REPL
```

```bash
hedwig> add pagination to list_tasks in service.py
hedwig> /showcase
hedwig> /exit
```

---

## Repository layout

```
sc/
  features.py             # deterministic risk assessment
  policy.py               # decision model seam (default rules + learned adapter)
  ml_policy.py            # online logistic regression + isotonic calibration
  autonomy.py             # threshold adjustment
  preference_inference.py # session signal inference + pattern generators
  hypothesis_bank.py      # evidence accumulation, LLM noticer, surfacing
  preferences.py          # 5-dim preference taxonomy + matching
  regret.py               # regret detection and correction loop
  cochange.py             # co-change graph from trace history
  trust_db.py             # SQLite facade (single substrate)
  store/                  # trace, rule, lease, preference, model stores
  run/                    # REPL, apply/read cascade, UI, retrospective

demo_recipe_api/          # demo fixture (recipe REST API)
tests/                    # 329 tests, ~10s
```

## Testing

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests -q
PYTHONPATH=demo_recipe_api .venv/bin/python -m pytest demo_recipe_api/tests -q
```

---

## Further reading

- [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md) — full architecture walkthrough with file references
- [`CONTEXT.md`](CONTEXT.md) — domain vocabulary
- [`SPEC.md`](SPEC.md) — runtime architecture, policy weights, data model
