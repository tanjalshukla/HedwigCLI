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

**2. A memory layer that grows from both explicit input and observed behavior.** Every task rebuilds the agent's prompt from repo-scoped memory: hard rules and style guidelines the developer stated, repo facts and style patterns the LLM noticer inferred from trace history, and verbatim past corrections auto-accumulated from prior sessions. All retrieved by keyword overlap with the current task. The agent opens each session with a synthesized "What we've learned about this repo" paragraph — oriented before it writes a line. The system captures both explicit and implicit developer intent, both deterministic (hard constraints, governance preferences that fire in the cascade) and soft (behavioral guidelines, logic notes retrieved into the prompt). See the Contribution table below for the full breakdown.

**3. A hypothesis bank that surfaces standing rules without asking prematurely.** Two generators propose candidates: deterministic rule-based detectors every turn, and an LLM that reads the trace digest every 5 turns. All LLM candidates must cite real trace IDs — hallucinated evidence dropped before storage. When a governance pattern accumulates enough evidence, Hedwig surfaces one question. Confirmed → fires deterministically in the apply cascade. Declined → stays in the bank for transparency, nothing silently discarded. Logic notes are auto-stored on inference; behavioral guidelines surface for one confirmation.

**4. A regret loop that corrects past over-trust.** When an auto-approved action is later denied or fails verification, Hedwig treats it as a training signal that the classifier was too permissive. Corrected exactly once per event, tracked persistently.

**5. Co-change memory across sessions.** Files that have historically moved together under the same task surface at write time: *"`store.py` — historically co-changes with `models.py` (2 tasks)"*. This is what rules and preferences can't express, since it's learned from what actually happened, not from what the developer thought to write down.

**6. Session signals inferred from behavior, not configuration.** Two signals are computed each turn without any developer input: *engagement level* (active vs. delegating) — inferred from turn count and tool-calls-per-turn, thresholds grounded in SWE-chat cluster centers (24.9 vs. 7.6 turns); *coding mode* (human-authored / collaborative / agent-led) — inferred from edit distance, how much the developer modified the agent's output. Both feed threshold adjustment immediately from turn 1. The agent's system prompt also opens each session with a synthesized "What we've learned about this repo" paragraph — top confirmed preferences in plain English, top repo facts, most-relevant past correction — before any task-specific content.

**7. Taxonomy grounded in real behavioral data.** The response categories, session signals, and preference schema come from analyzing 5,776 real coding-agent sessions (SWE-chat). Key finding: developer style is not stable across their own sessions (ICC = 0.249) — per-developer personalization would encode noise. The repo is the stable ground truth.

---

## Contribution

| What Hedwig learns | What it is | How it's used | Persists? |
|---|---|---|---|
| **Hard rules** | Unconditional constraints on the agent — what it can never touch, must always pause on | Blocks or forces pause before any scoring; bypasses trust grants | Yes, repo-scoped |
| **Behavioral guidelines** | Soft instructions about how the agent should write code — style, patterns, approach | Retrieved into the agent's prompt on relevant tasks; agent reads and follows them | Yes, repo-scoped |
| **Logic notes** | Facts about the repo the agent should know — where tests live, what's seeded, what files move together. Developer-stated or auto-inferred by LLM noticer (with cited trace evidence) | Retrieved into the agent's prompt to orient it before each task | Yes, repo-scoped |
| **Past feedback snippets** | Verbatim developer corrections from prior sessions — the actual words used when pushing back | Retrieved into the agent's prompt when task phrasing overlaps; closes the correction loop | Yes, repo-scoped |
| **Governance preferences** | Conditional pause rules inferred from behavioral patterns — when to stop vs. proceed based on action type, session state, and file scope | Tighten the scorer's verdict when all conditions match; never loosen | Yes, repo-scoped |
| **Approve / deny decisions** | Every file write the developer approved, denied, or pushed back on | Online classifier training signal — shifts which file/change patterns auto-proceed vs. pause | Yes, via classifier |
| **Pushback type** | How the developer responded — scope narrowing, correction, failure report, positive redirect | Feeds regret detector, hypothesis generators, session signal inference | Yes, in trace rows |
| **Regret events** | An auto-approved action the developer later denied or that failed verification | Replayed as a negative classifier signal exactly once per event | Yes, in classifier state |
| **Co-change pairs** | Files that historically appeared together under the same task | Surfaced at write time as context — descriptive, never affects scoring | Yes, derived from traces |
| **Session signals** | Per-turn inferences: engagement level, coding mode, task intent, turn purpose | Threshold adjustment and pattern generator filtering; resets each session | No, session-scoped |

**The design captures both explicit and implicit developer intent, across two axes:**

|  | Explicit (developer-stated) | Implicit (observed / inferred) |
|---|---|---|
| **Deterministic** | Hard constraints — `always_deny`, `always_check_in` on specific paths | Governance preferences — inferred by the hypothesis bank, confirmed by the developer, fire in the apply cascade |
| **Soft** | Behavioral guidelines — style rules retrieved into the agent's prompt | Logic notes and behavioral guidelines inferred by the LLM noticer from trace patterns, auto-stored or confirmed |

**How `/rules add` works:** a Bedrock call classifies the plain-English text into hard constraints (path-enforceable: `"never touch config/prod/"` → `always_deny`) or behavioral guidelines (prose-level: `"prefer composition over inheritance"` → retrieved into the agent's prompt). The developer never picks the category. `/rules add` does **not** produce governance preferences or logic notes — governance preferences come only from the hypothesis bank (observed patterns confirmed by the developer) or from built-in defaults. Logic notes come from `/rules add` facts about the repo, or are auto-inferred by the LLM noticer from trace patterns with cited evidence.

---

## Architecture in brief

Every file action flows through a five-layer cascade:

1. **Hard rules** — compiled from plain English, non-negotiable
2. **Trust grants** — temporary leases from prior approve+remember decisions
3. **Threshold adjustment** — four additive shifts set the proceed/flag bar: session engagement level, coding mode, model check-in calibration, persistent mode. Computed before the score is compared. Hard-coded constants grounded in SWE-chat findings — session-level responsiveness is needed from turn 1, before the classifier has enough data to learn these effects
4. **Decision model** — deterministic risk assessment produces a raw score; optional second-opinion model reviewer nudges it (separate system prompt, no access to agent intent); score compared against the adjusted bar → verdict
5. **Preference override** — confirmed behavioral patterns tighten the verdict; never loosen it

The full cascade detail, session signals, and preference matching logic are in [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md).

---

## Observability

Everything inspectable from inside the REPL:

```bash
/showcase                    # all surfaces at once — leave up as a live display
/weights                     # classifier drift from starting point (▲▼ per feature)
/prefs                       # accepted preferences, pending patterns, rejected candidates
/cochange                    # files that historically move together in this repo
/context                     # what was retrieved from repo memory for the last task
/retrospective               # regret events — where Hedwig was too loose or too cautious
/status                      # current session: engagement level, coding mode, model state
/config set-verification-cmd "pytest -q"   # set post-write verification command
/observe export --html       # full HTML report: traces, weights, hypothesis bank, regret
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
