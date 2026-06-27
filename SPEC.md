# Hedwig — Specification

The architecture reference. **What the system is now**, its data model, its
weight tables, and the decisions (and non-goals) behind it.

- New here? Start with [`README.md`](README.md) (pitch + install), then read
  [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md) for a narrative walkthrough of
  one task through the system and a file-by-file reading list.
- Working in the code? [`CLAUDE.md`](CLAUDE.md) has the behavioral rules and the
  load-bearing invariants.
- This doc is the lookup: vocabulary, cascade, weights, schema, non-goals.

Hedwig is a **governance layer that wraps an LLM coding agent**. It does not
generate code. For each agent-proposed action it decides whether to proceed
autonomously or pause for review, and calibrates that decision from real
interaction traces — not static configuration. The model is untrusted: it can
request reads, propose plans, generate edits, and raise check-ins, but the CLI
is the enforcement boundary for every read, write, and verification step.

It ships in **two forms over one core** (see [Two entry points](#two-entry-points)):
the Bedrock-backed research CLI (`hw`) and a local Claude Code plugin
(`plugin/`).

---

## Domain vocabulary

Use these exact terms in code, docs, and discussion — not "component,"
"handler," "service," or "feature." Each has a precise form (for code) and a
plain form (for reviewers / non-code audiences).

| Term | Precise | Plain |
|---|---|---|
| **Action** | One agent-proposed operation scoped to one file — read, write, patch, verify. The unit of authority. | One thing the agent wants to do to one file. |
| **Stage** | A phase of the workflow: `read` / `plan` / `apply` / `verify` / `report`. Each action belongs to one stage; authority is granted per stage. | Which phase of work the agent is in. |
| **Check-in** | A pause where Hedwig asks the developer to approve, edit, or deny. Tagged with `initiator`: `model` (agent asked) or `policy` (Hedwig decided). | A pause to ask before continuing — agent raised a hand, or Hedwig did. |
| **Hard constraint** | A deterministic rule enforced at the CLI boundary (`always_deny` / `always_check_in` / `always_allow`). Not negotiable at runtime; overrides everything. | A hard rule Hedwig always obeys. |
| **Behavioral guideline** | A soft preference retrieved into the agent's prompt when task-relevant. Shapes behavior without blocking. | A soft hint added to the prompt when relevant. Doesn't block. |
| **Decision trace** | An immutable per-action record in `decision_traces` (SQLite) — inputs, scorer decision, initiator, developer response, edit distance, outcome. | The log of every decision, why, and what the developer did. All learning comes from these. |
| **PolicyScorer** | The seam (`policy.py`) that decides auto-apply / flag / check-in. Two adapters: `HeuristicScorer` (hand-weighted, carries cold-start) and `PolicyClassifier` (online logistic regression, takes over at `MIN_SAMPLES_FOR_LEARNED=10`). `select_scorer()` picks one and tags the decision. | The part that decides "auto-do it" / "ask first" / "just flag it." Rules-based until 10 real decisions, then the learned version. |
| **RiskSignals** | Pure data object from `assess_risk()` (`features.py`). Raw signals only — no weights, no scores. Fields: `change_pattern`, `blast_radius`, `is_security_sensitive`, `is_new_file`, `diff_size`. | What we know about a change before deciding whether to ask. |
| **Preference** | A stored signal about how the developer wants oversight to work; 5 dimensions (see [Preference taxonomy](#preference-taxonomy)). | Something we've learned about how this developer wants Hedwig to behave. |
| **Hypothesis** | A candidate `Preference` accumulating trace-cited evidence in `hypothesis_candidates`. Surfaces for confirmation; never affects behavior until accepted. | A pattern Hedwig suspects but won't act on until it has evidence and you confirm. |
| **Regret event** | An auto-approved action the developer later denied, corrected, or that failed verification. Detected by `regret.py`; replayed as negative classifier signal exactly once per trace. | An auto-approve that turned out wrong — used to make the next similar one more cautious. |

**Verbs (use consistently):** **assess** (compute risk signals) / **score**
(policy's numeric output) / **decide** (categorical output) / **record** (write
a trace) / **retrieve** (pull guidelines into the prompt) / **revoke** (remove a
preference) / **infer** (derive a session signal). Do **not** use *classify*,
*estimate*, or *evaluate* as top-level verbs — collapsed into *assess*.

---

## Architecture

Check-ins come from two independent sources, both logged with `check_in_initiator`:

1. **CLI governance + policy engine** — evaluates constraints, leases, trace
   history, risk signals, and session state. Decides auto-approve vs. check-in
   vs. deny. Runs regardless of what the model does.
2. **Model-side reasoning** — the system prompt gives the model trust context
   and asks it to surface uncertainty (architectural decisions, approach
   tradeoffs, plan deviations) — not routine file access or style choices.

```
┌──────────────────────────────────────────────────────────┐
│ Developer (terminal / IDE)                                 │
└───────────────────────┬────────────────────────────────────┘
            commands, approvals, corrections
                        ▼
┌──────────────────────────────────────────────────────────┐
│ Hedwig                                                     │
│   Governance engine ── Policy engine ── Trace logger       │
│   (validates diffs,    (select_scorer:  (records every     │
│    enforces scope)      heuristic vs.    decision + who     │
│         │               classifier)      initiated it)     │
│         │                   ▲                 │            │
│         │              risk signals      traces│            │
│         │              (features.py)          ▼            │
│         │                   └──── Trust DB (SQLite) ◄──────┐│
│         ▼                                                 ││
│   System prompt builder (injects trust state into prompt) ││
└───────────────────────┬────────────────────────────────────┘
            governed API calls + system prompt
                        ▼
┌──────────────────────────────────────────────────────────┐
│ LLM agent (untrusted) — structured JSON protocol:          │
│   read_request, intent_declaration, file_update,           │
│   check_in_message, plan_revision                          │
└──────────────────────────────────────────────────────────┘
```

### Runtime flow

1. **Intent declaration** (`run/repl.py`) — model produces a structured plan of
   files to read and modify.
2. **Read stage** (`run/read_stage.py`) — each read goes through the cascade;
   approved files load into context.
3. **Generate** (`run/model.py`) — model generates changes; can raise proactive
   check-ins.
4. **Apply + verify** (`run/apply_stage.py`) — each write goes through the
   cascade; approved writes use atomic two-phase writes (temp + `os.replace`);
   verification runs post-write.

### Approval cascade

For every file access, evaluated in order, separately for reads and writes:

1. **Hard constraints** — permanent rules, resolved per access type. Override everything.
2. **Active leases** — temporary trust grants from prior approvals.
3. **Adaptive policy** — `select_scorer()` picks the active `PolicyScorer`
   adapter and scores the `RiskSignals` from `assess_risk()`.
4. **Threshold adaptation** (`autonomy.py`) — thresholds shift from learned
   preferences, model check-in calibration, and session intensity.
5. **Preference override** (apply only — `run/preference_coordinator.py`) —
   confirmed `Preference`s match per-file and **tighten** the verdict (one
   narrow loosening exception — see [the safety invariant in CLAUDE.md](CLAUDE.md)).

Steps 1–3 are shared between read and apply via
`helpers._resolve_pre_scorer` and `_policy_decision_for_file`. The remaining
differences are **intentional asymmetries**: apply has regret correction, the
hypothesis pipeline, classifier updates, and atomic writes; read does not. Each
stage's module docstring enumerates them.

---

## Policy engine

Heuristic scoring from `policy.py`. The weights are an explicit documented
baseline, **not a claimed optimum or a tuning target** — change one, update this
table in the same commit.

**Signals and weights:**

| Category | Signal | Weight | Notes |
|----------|--------|--------|-------|
| History | Prior approvals (rubber-stamp-discounted) | +0.4 per | Rubber-stamps <5s count 0.5× |
| History | Prior denials | -0.7 per | |
| History | Deliberate review pace (>15s) | +0.15 | |
| History | High edit distance | -0.5 max | Developer heavily corrects output |
| Risk | Large diff (>80 lines) | -0.8 | |
| Risk | Medium diff (>30 lines) | -0.4 | |
| Risk | Blast radius >3 files | -0.8 | |
| Risk | Large multi-file action (>4) | -0.9 | |
| Risk | Multi-file action (>1) | -0.35 | |
| Risk | New file | -0.6 | |
| Risk | Security sensitive | -2.0 | Path/content keyword detection |
| Risk | API/data model change | -0.8 | |
| Risk | Config change | -0.4 | |
| Risk | Dependency update | -0.5 | |
| Risk | Test/documentation | +0.3 | Low impact |
| Risk | Error handling | +0.1 | Usually localized |
| Session | Recent denials | -0.7 per (max 3) | |
| Quality | Verification failure rate >30% | -0.6 | From trace history |
| Quality | Low model confidence (<0.40, 3+ samples) | -0.3 | From trace history |
| Quality | Adversarial-reviewer score (advisory) | ±0.3 max | `model_risk_score`; mapped to [-1,+1] around the 0.5 default, weighted 0.3. Apply-stage only. Failure default 0.5 contributes nothing. |

**Reviewer call budget.** The adversarial-reviewer Bedrock call is gated by
`model_risk.should_review(risk, history)`. It fires only if at least one holds,
else the call is skipped and `model_risk_score` stays at 0.5 (zero contribution):

1. `risk.is_new_file` — no history for the path.
2. `risk.is_security_sensitive`.
3. `risk.blast_radius >= 4`.
4. `risk.diff_size >= 80`.
5. `risk.change_pattern in {api_change, data_model_change, config_change, dependency_update, security_change}`.
6. `history.effective_approvals == 0 and history.denials == 0` — cold path.

There is deliberately **no cap** on reviewer calls: `should_review()` is the
gate, and silently skipping a security-sensitive file because of an arbitrary
per-turn budget would be worse than the cost of one extra Bedrock call (see the
comment at `apply_stage.py`). The gate keeps the call count low in practice.

**Scoring bands:**

| Score | Action | Developer sees |
|-------|--------|----------------|
| ≥ 0.9 | `proceed` — auto-approve silently | Nothing at apply time; appears in session summary |
| ≥ 0.2 and < 0.9 | `proceed_flag` — auto-approve, flag for review | "Apply approved. Flagged for review: [file]" |
| < 0.2 | `check_in` — pause for review | Full check-in UI with policy reasons |

No action silently disappears — everything is logged with full policy reasons.
`proceed_flag` actions land in the session summary and traces; inspect via `hw
observe traces`, undo via `hw observe revoke` / `preferences-revoke`. These
numeric thresholds are implementation details: lab participants interact with
qualitative autonomy modes and reason strings, not raw scores.

---

## Online learning policy

`ml_policy.py` holds an online logistic-regression classifier (scikit-learn
`SGDClassifier`, log loss) that personalizes approval decisions to a repo's
observed behavior. It runs alongside the heuristic and **takes over once 10 real
developer decisions are recorded** (`select_scorer()`'s `ready()` gate).

**Feature vector (14 dimensions):**

| Feature | Source | Rationale |
|---------|--------|-----------|
| `prior_approvals` | Trace history (per file/stage) | Repeated approvals warrant less interruption |
| `prior_denials` | Trace history | Recurring denials signal persistent risk |
| `avg_response_ms` | Trace history | Slow review → took it seriously |
| `avg_edit_distance` | Trace history | Heavy corrections → output quality low |
| `diff_size_log` | Current action | Larger diffs riskier; log-scaled |
| `blast_radius` | `features.py` | Import fan-out: dependents of this file |
| `is_new_file` | `features.py` | New files carry more uncertainty |
| `is_security_sensitive` | `features.py` | Path/content keyword detection |
| `files_in_action` | Current action | Multi-file writes harder to review/revert |
| `recent_denials` | Session state | In-session denial streak → agent off-track |
| `verification_failure_rate` | Trace history | Frequent test failures → tighter oversight |
| `model_confidence_avg` | Trace history | Aggregated model self-reported confidence |
| `change_pattern_risk` | `features.py` | Semantic change class mapped to a risk scalar |
| `model_risk_score` | `model_risk.py` | Advisory adversarial-reviewer score [0,1]. Apply-stage only. Defaults to 0.5 on any failure — never silently flips a decision. |

**Cold start.** `build_cold_classifier()` creates an uninitialized
`SGDClassifier` seeded with a single zero+one pair so `partial_fit` has seen both
classes. **No synthetic labels.** The `HeuristicScorer` carries all cold-start
behavior until real decisions accumulate — so "we learn from real traces, not
fabricated priors" is defensible end-to-end.

**Online update.** After each developer decision (approve/deny at a check-in, or
approve-all on an auto-approved batch), the classifier receives one
`partial_fit(x, y)` on the same 14 features used for scoring. No batch
retraining, no offline collection period.

**Sample gate.** `select_scorer()` returns the heuristic until
`sample_count >= MIN_SAMPLES_FOR_LEARNED` (10). Which scorer fired is recorded
in `PolicyDecision.reasons` and persisted into `decision_traces`, so analysis
can separate heuristic-era from learned-era decisions. The classifier also
maintains an isotonic-regression calibrator that takes over after 20 real
decisions, and a `_corrected_regret_ids` set **persisted with the pickle** so
each regret is replayed exactly once across the repo's lifetime.

**Observability.** `hw observe weights` (REPL: `/weights`) shows a per-feature
table — Prior (cold-start ≈ zero) / Current (learned) / Delta (signed drift) —
with color on features the developer's behavior meaningfully shifted.

### Worked example: policy update from trace data

Three sessions on a Python API project record:

| Session | File | change_pattern | diff_size | user_decision |
|---|---|---|---|---|
| 1 | `api/routes.py` | `api_change` | 42 | `deny` |
| 2 | `api/routes.py` | `api_change` | 31 | `deny` |
| 3 | `api/routes.py` | `api_change` | 55 | `check_in → deny` |

After three denials, `prior_denials` for that file is `min(3/10, 3.0) = 0.3` and
the classifier has three negative labels for those vectors. The `prior_denials`
and `change_pattern_risk` (for `api_change`) coefficients drift more negative;
future `api_change` proposals on that file score lower → more check-ins.
Meanwhile `utils/helpers.py` accumulates 5 fast approvals
(`general_change`, diff ≤ 20); its vectors drift `prior_approvals` positive, and
future writes score above 0.9 and auto-apply. Per-`(repo_root, file_path,
stage)` scoping means one file's history never contaminates another's. Every bit
of drift traces to a real decision — the developer never tuned a weight.

---

## Threshold adaptation and autonomy modes

`AutonomyPreferences` (`autonomy.py`) is the coarse, repo-scoped surface that
shifts the scorer's proceed/flag thresholds **before** the score is compared
(`adjusted_policy_thresholds`):

- Prefers fewer check-ins → thresholds drop 0.25 (+0.10 if topic-scoped).
- Model check-in approval rate <40% (5+ samples) → thresholds rise 0.15.
- Floor clamp at -0.5.

Qualitative modes (`strict` / `balanced` / `milestone` / `autonomous`) are
cold-start presets that compile to these thresholds; the scorer + preferences
then carry the load. Preference state has four legacy fields
(`prefer_fewer_checkins`, `allowed_checkin_topics`, `skip_low_risk_plan_checkpoint`,
`scoped_paths`), populated by `summarize_autonomy_feedback()` (an LLM text
extraction — **not** parameter learning) and merged additively (OR/UNION).
`revoke_preferences()` walks them back (`hw observe preferences-revoke`).

This is distinct from the `ml_policy.py` classifier: preference inference has no
gradient updates, no learned parameters, no training loop. It parses feedback →
extracts a structured object → OR/UNION-merges it → that shifts thresholds. The
only parameter learning is in the `PolicyClassifier`.

---

## Preference taxonomy

The 5-dimension `Preference` (`preferences.py`) is the richer surface, matched
per-file per-action by `PreferenceCoordinator`. Each dimension captures
something the legacy 4-field schema couldn't:

| Dimension | Precise | Plain |
|---|---|---|
| **Trigger** | Predicate over `RiskSignals` + action context (AND semantics; `None` = wildcard). | What kind of action this cares about. |
| **Condition** | Contextual predicate at decision time — session state, persona, scorer confidence. `None` = don't care. | When it should fire, based on what's happening around the action. |
| **PreferenceAction** | Enum — `AUTO_APPLY` / `SOFT_CHECKIN` / `FULL_CHECKIN`. | What to do: just do it / quick non-blocking panel / full pause. |
| **Scope** | `global` / `repo` / `session` / `path`. Checked outermost-first. | How widely it applies. |
| **Lifecycle** | `provenance` (`user_explicit` / `inferred` / `default`), `confidence` (0..1), `last_reinforced_at`, `half_life_seconds`. | How we got it, how sure we are, how fast it fades. |

Preferences come from: built-in defaults (e.g. `FAILURE_SIGNAL_CHECKIN`),
developer confirmations via the hypothesis flow, and the
`autonomy_prefs_to_preferences()` bridge from the legacy surface.

---

## Session signals (SWE-chat grounded)

Computed each turn from traces, no developer input required:

| Signal | Precise | Plain |
|---|---|---|
| **CodingMode** | `human_only` / `collaborative` / `vibe`. Inferred from edit_distance + approval rate. | How much surviving code is the agent's. |
| **UserPersona** | Intensity enum `active` / `delegating` / `unknown`. Inferred from turn count + tool-calls-per-turn (SWE-chat centers: 24.9 vs. 7.6 turns). Affects thresholds and hypothesis surfacing (delegating sessions never see hypothesis prompts). | How engaged the developer is. |
| **Oversight** | User-facing label via `/oversight`: `hands-on` (→ `active`) / `balanced` (→ auto-infer) / `delegating`. Explicit overrides inference. | How much the developer wants Hedwig in their face. |
| **PushbackType** | Per-turn: `correction` / `rejection` / `failure_report` / `non_pushback` / `scope_constraint` / `positive_redirect`. The last two added because 33% of real pushback fell outside the original 4. | What kind of response the developer gave. |

**Key empirical finding:** developer style is **not** stable across a person's
own sessions (SWE-chat ICC = 0.249). Per-developer personalization would encode
noise — so preferences are per-session and per-repo, never per-person.

---

## Hypothesis bank (Trial-Error-Explain loop)

`hypothesis_bank.py` accumulates candidate `Preference`s in
`hypothesis_candidates`. Two generators feed it:

- **Rule-based** (`preference_inference.py`) — pattern-matchers that emit
  candidates every apply turn when session signals fit.
- **LLM noticer** (`maybe_generate_llm_hypotheses`) — every
  `LLM_GENERATION_INTERVAL` turns, sends a digest of recent traces to Bedrock for
  novel hypotheses. Each candidate must cite real `decision_traces.id` values;
  uncited/hallucinated cites are dropped. JSON parsing is string-aware bracket
  balancing (`_extract_json_array`). It is **supplemental** — disabling it
  doesn't break the loop.

Each new trace scores `+1 for` / `+1 against` every pending candidate
(`update_evidence`). Candidates surface when `evidence_for / total ≥
SURFACE_CONFIDENCE` (0.70) over ≥ `MIN_EVIDENCE` (3) traces; pruned when ≤
`PRUNE_THRESHOLD` (0.30). LLM candidates may mark `high_stakes` to raise their
own bar (2× `MIN_EVIDENCE`); one citing ≥ `MIN_EVIDENCE` real trace IDs is
promoted straight to `ready_to_surface`. **Confirmed → becomes a `Preference`
that fires in the cascade. Declined → stays in the bank with status. Nothing
affects behavior until the developer confirms.**

---

## Regret loop

`regret.py::detect_regret_events` walks session traces in order: an
auto-approved action followed by a denial, failure report, or verification
failure becomes a `RegretEvent` (reason: `deny` / `interrupt` /
`failure_report` / `verification_failed`). `apply_stage._apply_regret_corrections`
replays each as `classifier.update(pi, approved=False, count_sample=False)` —
`count_sample=False` because regret replay is a corrective gradient, not a new
decision (must not push past `MIN_SAMPLES_FOR_LEARNED`). `_corrected_regret_ids`
(persisted with the pickle) ensures each regret fires exactly once. Regret events
also surface in `/retrospective` and the HTML export.

Maps to the three governance layers (Sahoo 2026): **preventive** (hard
constraints), **detective** (regret tracking), **corrective** (the
calibration retrospective).

---

## Two entry points

One governance core, two front-ends:

| | Research CLI (`hw`) | Claude Code plugin |
|---|---|---|
| Lives in | `sc/`, `sc/cli.py` | `plugin/` |
| Drives | A Claude agent end-to-end via Bedrock | Claude Code's own edits via hooks |
| Needs cloud? | Yes — AWS SSO + Bedrock | No — fully local |
| Governance core | `sc/` directly | vendored into `plugin/vendor/sc/` |
| Decision point | The REPL cascade | `PreToolUse` / `PostToolUse` / `Stop` hooks |
| Learns from | Approvals/denials at REPL prompts | Outcomes of auto-applied edits (reversal, verification failure) — Claude Code owns the native prompt, so clicks are invisible |

The plugin vendors a standalone copy of the core so it installs without the
research repo. **Regenerate it with `make sync-vendor`** (`plugin/sync_vendor.py`)
after editing `sc/`; `make verify` checks for drift. The learned scorer needs
`numpy` + `scikit-learn`; if the hook interpreter lacks them the plugin degrades
cleanly to the stdlib heuristic. `plugin/bin/hedwig-setup.py` builds a dedicated
`~/.hedwig/venv` and the hooks re-exec under it so the learned scorer always runs.
See [`plugin/README.md`](plugin/README.md).

### Capability parity (current state)

This SPEC describes the full system as realized in the **CLI**. The plugin
shares the governance core and now delivers the headline mechanisms; the split
today:

| Capability | CLI | Plugin | Delivery channel in the plugin |
|---|---|---|---|
| Risk assessment + scorer cascade | ✅ | ✅ | `PreToolUse` decision |
| Online classifier | ✅ | ✅ | grows from edit outcomes (needs `hedwig-setup.py`) |
| Regret loop | ✅ | ✅ | `PostToolUse` (reversal) + `Stop` (verify-fail) |
| Confidence handshake | ✅ | ✅ (agent opt-in) | `hedwig-declare.py` → decide honors it |
| Hard constraints | ✅ | ✅ | `_constraint_decision` in the decide gate (layer 1); authored via `/hedwig-rules` |
| Memory layer (repo facts / guidelines into the model) | ✅ | ✅ | `SessionStart` / `UserPromptSubmit` `additionalContext` (`hedwig-context.py`); reuses `repo_memory.synthesize_repo_summary` |
| Hypothesis bank | ✅ | ✅ | generate/accumulate in `PostToolUse`; surface via `Stop` `additionalContext`; **confirm via `/hedwig-learn`** (hooks are non-interactive) |
| Preference application | ✅ | ✅ | `apply_confirmed_preferences` in the decide gate (layer 5); reuses `PreferenceCoordinator` |
| Threshold adaptation + session signals | ✅ | 🔜 | needs per-turn session-state tracking in the hooks |
| `/rules add` NL classification + LLM hypothesis noticer | ✅ | 🔜 opt-in | a model call; opt-in when an `ANTHROPIC_API_KEY` is present |
| Observability | ✅ | mostly | `/hedwig-status`, `/hedwig-weights` (classifier drift), `/hedwig-retrospective` (regret), `/hedwig-rules`, `/hedwig-learn`; `/cochange` + HTML export still CLI-only |

**Two structural platform differences** (not gaps): the plugin learns from edit
**outcomes**, not approve/deny clicks (Claude Code owns the native prompt and
hides the click from hooks); and hypothesis **confirmation** is a slash command,
not inline y/n (hooks are non-interactive). The remaining 🔜 rows are a wiring
effort, not a redesign — the logic exists in the shared core.

---

## Database schema

SQLite (WAL mode). `decision_traces` is the primary artifact for any post-hoc
analysis.

| Table | Purpose |
|-------|---------|
| `leases` / `read_leases` | Temporary write/read trust grants (repo_root, file_path, expires_at, source) |
| `decisions` | High-level approval records (task, approved, planned/touched files) |
| `decision_traces` | Per-file decision log — every signal, decision, and outcome |
| `plan_revisions` | Plan checkpoint history (rounds, feedback, approval) |
| `hard_constraints` | Permanent rules (path_pattern, read/write policy, source, overridable) |
| `behavioral_guidelines` | Prompt directives (text, source) |
| `autonomy_preferences` | Per-repo check-in preferences (JSON blob) |
| `hypothesis_candidates` | Pending hypotheses with evidence counts and provenance |
| `policy_models` | The persisted per-repo `PolicyClassifier` pickle |

`trust_db.py` is a thin facade over five focused mixin stores under `sc/store/`
(`lease_store`, `rule_store`, `trace_store`, `pref_store`, `model_store`);
`TrustDB` inherits from all five. Relatedness scoring for prompt retrieval goes
through the `Retrieval` seam (`sc/retrieval.py`): `EmbeddingRanker` (fastembed,
default) with `KeywordRanker` (token-overlap) as the offline fallback.

---

## Module map

| Module | Responsibility |
|--------|----------------|
| `features.py` | `assess_risk` → `RiskSignals`; single source of truth for change-pattern categories |
| `policy.py` / `ml_policy.py` | `PolicyScorer` seam: heuristic scorer + online classifier + isotonic calibration |
| `autonomy.py` | `AutonomyPreferences` + threshold adaptation |
| `preferences.py` | 5-dim `Preference` taxonomy + matching |
| `preference_inference.py` | Session-signal inference + rule-based candidate generators |
| `hypothesis_bank.py` | Evidence accumulation, LLM noticer, surfacing |
| `regret.py` | Regret detection + correction loop |
| `cochange.py` | Co-change graph from trace history |
| `model_risk.py` | Adversarial reviewer + `should_review` gate |
| `plan_gate.py` | Plan-stage authority shift before apply |
| `constraints.py` | Rule import and path-policy resolution |
| `retrieval.py` | `Retrieval` seam — embedding + keyword rankers |
| `prompt_builder.py` | Dynamic system prompt from trust state |
| `agent_client.py` / `schema.py` | Bedrock wrapper + strict structured-JSON protocol (untrusted-model boundary) |
| `verification.py` | Post-write checks |
| `trust_db.py` + `store/*` | SQLite persistence, analytics, traces, exports |
| `run/` | Orchestration: REPL, read/apply cascade, UI, retrospective |
| `commands/` | User-facing CLI surface (`observe`, `learning`, `admin`, `status`) |
| `plugin/` | Claude Code plugin: hooks (`bin/`), vendored core (`vendor/sc/`) |

For the recommended order to read these when learning the codebase, see
[`HEDWIG_END_TO_END.md` §14](HEDWIG_END_TO_END.md).

---

## Design decisions

| Decision | Current | Revisit if… |
|---|---|---|
| Learning algorithm | Online logistic regression (SGD, log-loss, cold-start); heuristic carries behavior until 10 real decisions | Enough lab data → contextual bandit as a third `PolicyScorer` adapter |
| Change pattern classification | Rule-based (`features.py`) | Rules miss too many patterns → lightweight LLM |
| Rule retrieval | `Retrieval` seam: embedding default, keyword fallback | — (extraction done; was parked, now shipped) |
| Trust decay | None implemented | Users report stale trust → exponential decay |
| Lease threshold | 3 consecutive approvals | Too aggressive/conservative |
| Model trust visibility | Vague summary, no scores | Model needs more to reason, or is gaming it |
| Initiator weighting | Equal CLI vs. model | Data shows one source is better calibrated |
| Model confidence | Logged, not trusted | Correlates with outcomes → make it active |
| Model writes own rules | Never | N/A — hard architectural constraint |
| Rubber-stamp threshold | <5s review duration | 5s too aggressive |
| Preference accumulation | OR/UNION additive merge; revocation via `preferences-revoke` | Preferences go stale → add decay |

---

## Deliberate non-goals

Parked decisions, not oversights. Don't re-propose without strong new evidence.

- **A third `PolicyScorer` adapter** (LinUCB / Thompson / RF / XGBoost). Tree
  models don't support `partial_fit` and would re-introduce the "static,
  retrained" shape reviewers criticized. A contextual bandit is the interesting
  one but needs real trace bootstrap to demo well. Post-camera-ready.
- **Per-developer (vs. per-repo) preferences.** SWE-chat ICC 0.249 says style
  isn't stable cross-session. Reviewer 148D asked us not to *claim* this — not to
  build it. Parked.
- **Unifying the read and apply cascades** into one parameterized `Cascade`. The
  shared work already lives in `helpers.py`; the remaining differences are
  intentional asymmetries. Cost is real, win is mostly cosmetic.
- **Any "learned" language not backed by `PolicyClassifier`** — the reviewer-148D
  critique. Don't reintroduce it in code or docs.
- **Further `trust_db` decomposition** — already split into five `sc/store/`
  mixins.

**Mid-leverage refactors deferred (not blockers):** factor a `PostApplyPipeline`
out of the ~1100-line `apply_stage.py` (concentrate hypothesis + regret +
classifier-update + trace recording behind one seam); deprecate
`AutonomyPreferences` into `Preference` (two surfaces, one bridge function);
tighter retrieval scoping (key on touched files, not just task prompt). A longer
research/feature backlog (reversibility as a risk dimension, async delegation
mode, checkpoint/rewind, git-aware risk, richer interrupt taxonomy) is tracked
separately and is not part of what the system is today.

---

## Research framing

**Why this matters.** Current tools calibrate autonomy through developer-authored
static config (CLAUDE.md, permission lists). Those capture only what a developer
can articulate in advance — but most preferences are implicit, emerging as
correction patterns, review timing, edit distance, and phase-of-work context.
The bottleneck isn't raw model capability; it's trust infrastructure. Hedwig
makes that boundary explicit and measurable.

**The trace-prompt feedback loop** is the core mechanism: every interaction
produces a trace → traces accumulate into trust scores, correction patterns, and
guidelines → those build the system prompt at session start → the model reasons
about when to check in → the developer responds → more traces. After 3+
corrections on the same pattern, the system suggests a behavioral guideline;
once accepted, correction overhead for that pattern drops to zero.

**O1–O5 (Bui & Evangelopoulos, 2026).** Hedwig is the first system to
meaningfully satisfy O1+O2+O3: cost-of-interruption is computed (O1), "stay
silent" is an explicit first-class action (O2), and per-developer feedback
updates the policy (O3). No deployed agent they audited (Cursor, Copilot, Jules,
Claude Code Routines) did.

**Related work.** Zhou et al. (CHI '26) schedule confirmations as a minimum-time
problem and defer personalization to future work — Hedwig fills that gap with
per-repo/per-session adaptation. CowCorpus motivates learning oversight from
traces. Grunde-McLaughlin et al. motivate review-quality signals (Hedwig
discounts rubber-stamps). PAHF motivates post-action personalization kept outside
the model.

**Evaluation plan.** Primary metrics: correct-trust / correct-caution /
unnecessary-interruption / missed-check-in rates. Plus calibration (useful vs.
wasted check-ins by initiator), learning (correction-repeat rate, trust
trajectory, preference carryover), and quality (rubber-stamp rate, review
duration, verification outcomes). Baselines: Always Ask, Never Ask, Static Rules,
Heuristic (current), Future Learned. The pilot (2 tasks, 11 ops) already shows
Hedwig catching 4/4 cautious-developer check-ins vs. 2/4 for agent+rules and 0/4
baseline; a larger lab study is the camera-ready follow-up.
