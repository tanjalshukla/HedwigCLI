# Post-CAIS Migration

A living document tracking the architectural and conceptual changes between
the ACM CAIS 2026 demo paper and the AI Engineer World Fair 2026 plugin demo
(scheduled 2026-07). Updated as work lands.

Status: in progress. Deltas appended at the bottom.

---

## CANONICAL PLAN & TASK LIST (source of truth — read this first)

**North star:** the deliverable for the World Fair is a *demoable product*,
not the academic protocol. Optimize for: installable in ~60s, legible in
~30s, clean code an engineer respects, one sharp number. **The spine ships
the demo; the protocol is the paper.**

> **⏱ 3-DAY EXECUTION ORDER (locked 2026-06-26 — Fair is Jun 29–Jul 2).**
> Build strictly in this order so the aggressive work sits on a verified base:
> 1. **S5** — restore the online log-reg classifier to the default path (this IS
>    the novelty; non-negotiable, first).
> 2. **S6** — clean-machine install QC (G2) + the live-machine QC items left on
>    R3 (fastembed fetch) and R4. The live-download window makes a broken
>    install the top threat — a failed install shared to 6,000 engineers is
>    worse than a missing feature.
> 3. **R5** — public landing site + plugin download (the shareable asset).
>    Ships WITHOUT a headline number — we haven't deployed, so there is no
>    honest number yet (a pre-deployment figure would be fabricated; G4). The
>    number is a post-launch artifact: "learns from real outcomes" *means* the
>    number comes from real usage. The 90s demo video is founder-produced and
>    dropped in separately.
> 4. **R6** — deny+reason self-correction loop (REQUIRED functionality, inline,
>    on by default — see its row). Build last because its blocking code sits on
>    the core decide path; its QC gate must prove no regression to auto-approve/
>    surface before it merges.
> 5. **R7** — repo polish (README/LICENSE/structure) — acquisition-DD + a
>    north-star criterion. Can run in parallel.
> Multi-agent: **positioning note only** (locked) — no pre-Fair build.
>
> **CUT (2026-06-26):** S7 (seeded headline number) and S4 (coding-agent
> demo/video task) are dropped. No honest number exists pre-deployment; the
> demo video is founder-produced, not a coding-agent deliverable. README work
> folds into R7. Report a real suppression number post-launch from `/hedwig-status`.

**Decisions locked (do not relitigate):**

1. **Learning signal = outcome-based, not click-based.** Native-prompt
   approvals are invisible to hooks, so the plugin learns from *outcomes*:
   auto-applied actions that survive the session (positive) vs. regret —
   revert / verification failure (negative) — plus the deny+reason loop on
   the risky path. Headline claim: **"learns from outcomes, not clicks."**
   This is sharper than the CLI's click signal (which the codebase already
   discounts 0.5× for rubber-stamps) and is the only kind that generalizes
   to non-coding domains.
2. **Intent source = infer + best-effort skill.** Hedwig no longer controls
   Claude's prompt, so it cannot force `IntentDeclaration`s. Robust spine:
   infer intent/risk from the observable tool stream (no agent cooperation).
   Richer handshake: ship a skill asking Claude to self-declare confidence on
   risky edits — works when it complies, degrades gracefully when it doesn't.

**Packaging decision (CORRECTED 2026-06-25 — classifier RESTORED to default):**
vendor a slim `sc/` subset into `plugin/vendor/sc/` (not pip-pin). The decide
path import closure is: `sc.features`, `sc.policy`,
`sc.store.{lease,model,pref,rule,trace}_store`, `sc.trust_db`,
`sc.session_state`, `sc.retrieval`, **and `sc.ml_policy`** (the
`PolicyClassifier` — now REQUIRED, see below).

> **The SQLite trace store + online logistic-regression classifier IS the
> novelty — it ships in the DEFAULT plugin, not as an opt-in.** The earlier
> "exclude `ml_policy` to keep the plugin zero-dep" decision is **REVERSED** —
> it was wrong on both counts:
> 1. **The zero-dep wall already fell when R3 standardized `fastembed`**, which
>    pulls **numpy** transitively (via onnxruntime). numpy is in regardless.
> 2. **Given numpy is already present, sklearn's marginal cost is only scipy +
>    joblib** — modest, no torch. The goal was never "fewest deps possible," it's
>    "reasonably light," which easily absorbs scipy. Gutting the learned scorer
>    to save scipy traded away the core contribution.
>
> New honest framing: **"one pip install — SQLite-backed, learns locally, no
> GPU, no torch, no AWS."** Still dramatically lighter than the CAIS
> AWS-SSO+Bedrock wall (the property that actually mattered). The cold-start
> `HeuristicScorer` carries the first `MIN_SAMPLES_FOR_LEARNED` (10) decisions;
> `select_scorer()` then hands over to the learned `PolicyClassifier` exactly as
> in CAIS. **Both SQLite traces and online log-reg are load-bearing novelty —
> neither is optional.** Implemented by task S5 below.

Cost of vendoring: a sync step when those modules change.

> **Competitive note (Omnigent — VERIFIED from omnigent.ai + their policy docs,
> 2026-06-25).** "Built by the Databricks AI team and Neon," Apache-2.0, alpha,
> `curl|sh` install, `github.com/omnigent-ai/omnigent`. A **meta-harness**:
> "a common layer over Claude Code, Codex, Pi, and the agents you write
> yourself." Direct quotes confirming our differentiator: policies are
> **"authored by users"** (YAML or custom Python); **"there is no mechanism
> that learns, calibrates, or adjusts policy from past outcomes or feedback"**;
> risk scoring is **"user-authored arithmetic over predefined sensitive
> operations with a fixed threshold — no model infers the score"**; decisions
> are **ALLOW / ASK / DENY** (1:1 with our proceed/check-in/deny). Custom Python
> policies are a **first-class extension point registered on their server.**
> → THE OPENING: Hedwig is the *learned* policy that plugs into that socket and
> that they explicitly have not built. This is R4. KEY POSITIONING: **their
> policies are authored, ours are learned from outcomes** — you WILL be asked
> "how are you different from Omnigent?" and the answer is "we run *inside* it
> and add the learning it lacks." Complementary altitude (they
> orchestrate/sandbox/share; we decide-and-learn). Do NOT chase meta-harness
> features — that's their large surface, not our contribution.

> **Multi-agent positioning (locked 2026-06-26 — POSITIONING ONLY, no pre-Fair
> build).** Multi-agent/loops is the hot conference topic; capture the interest
> with the *answer*, not a rushed feature. **The booth line:** "Hedwig governs
> at the level of a single *action*, so it already generalizes to multi-agent
> loops — every agent's edits flow through the same learned trust layer. More
> agents just means more actions; the unit of governance doesn't change shape."
> This is honest (the decide path is per-action and agent-agnostic) and it
> pairs with R4 — a meta-harness like Omnigent *is* a multi-agent context, so
> "Hedwig as the learned policy inside a multi-agent harness" is the
> roadmap-slide answer. **Why not build it:** ~3 days; it competes with
> LangGraph/CrewAI/Omnigent on *their* turf underbuilt, and a shaky multi-agent
> bolt-on dilutes the sharp single-agent message that's actually uncontested.
> Deliverable = one slide + the booth line, captured in R7's README/booth notes
> (founder uses it live). Build = post-Fair.

### SPINE — the deliverable (build in this order)

| # | Task | Status | Notes |
|---|------|--------|-------|
| S1 | **Self-contained installable plugin** | ✅ DONE | Slim `sc/` closure vendored into `plugin/vendor/sc/` via `sync_vendor.py`; decide path runs standalone, no research repo. (Note: S5 now adds `ml_policy` to the vendor set.) |
| S2 | **Prompt suppression + `/hedwig-status` number** | ✅ DONE | decide.py logs suppressed/surfaced per edit; `/hedwig-status` tallies "suppressed N of M (X%)". |
| S3.5 | **Status dashboard + plain-English reasons** | ✅ DONE | One `/hedwig-status` panel (number + regret moment) + plain-English `permissionDecisionReason` as the only in-flow voice. UI = exactly two surfaces (no extra panels). |
| S3 | **Outcome learning (decide ↔ SQLite)** | ✅ DONE (regret path updated by S5) | SQLite trace wiring: PostToolUse records auto-applies; Stop-hook records reversal/verification-failure as negative outcome → tightens the next like-action. S5 adds `classifier.update()` to this regret path. |
| S5 | **Restore the online log-reg classifier to the default plugin path** | ✅ DONE | All five steps landed. (1) `ml_policy.py` added to `sync_vendor.py`'s allowlist (13 modules now); docstring rewritten — the dep wall MOVED, didn't vanish. (2) `test_ml_policy_is_not_vendored` → **`test_ml_policy_is_vendored`** (asserts ml_policy present; torch/anthropic/boto absent). (3) decide.py opens trust.db once, loads the persisted `PolicyClassifier` via new `_hedwig_common.load_classifier`, routes through `select_active_scorer` (= CAIS `select_scorer`): heuristic carries cold-start, learned takes over at `ready()` (≥`MIN_SAMPLES_FOR_LEARNED`=10). **Threshold-by-scorer:** heuristic keeps 0.0/-1.0 (raw additive score — cold-start stays permissive so the demo's low-risk edits auto-apply); learned uses 0.5/0.25 (calibrated probability) — one pair would misthreshold whichever scorer it wasn't tuned for. Scorer label logged to decisions.jsonl. (4) Regret routes through `update_classifier_for_regret` → `classifier.update(pi, approved=False, count_sample=False)` in BOTH `hedwig-record.py` (reversal) and `hedwig-verify.py` (verification fail), in addition to the deny trace; `_corrected_regret_ids` keyed (`reversal:`/`verify_fail:`+session+file) so each fires exactly once; `pi` reconstructed from per-file history + latest trace, mirroring `apply_stage._apply_regret_corrections`. (5) scikit-learn/numpy/fastembed already in deps; plugin README rewritten to "SQLite-backed, learns locally — no torch, no GPU, no AWS" + graceful-degradation note. **Graceful degradation PROVEN (not vacuous):** all S5 helpers are best-effort (heuristic/None on any failure). Since sklearn is installed system-wide (the G2 vacuous-test trap), `test_decide_degrades_when_sklearn_unimportable` **shadows sklearn with a package that raises ImportError** and proves decide still emits a valid `allow`, exits 0, logs `scorer:heuristic`, no traceback. **Tests:** 4 in-process `test_plugin_classifier.py` (cold→learned flip at MIN_SAMPLES; regret fires once per key; **regret on one file lowers the learned score for a risk-similar edit on a DIFFERENT file** = the cross-file generalization the classifier buys over per-file history; cold model persisted on first load) + the degradation guard + inverted vendoring test. 400 tests green. **REMAINING QC (real clean machine):** confirm deps install (numpy+sklearn+fastembed, NO torch) and the cross-file flip reproduces live through the hooks after ≥10 real decisions. **G4:** "learns" language is now defensible (real classifier, `ready()`-gated) — but a cold-start decision must still not be called "learned" in UI. |
| S6 | **Clean-machine install QC (the #1 Fair threat)** | pending — DO AFTER S5 | The live-download window makes a broken install the worst failure mode (shared to 6,000 engineers). Close the open live-machine QC items: R3 (install fastembed, confirm it pulls numpy+onnxruntime but NOT torch, first-run ~30MB model fetch works, offline-no-cache degrades cleanly) and R4 (Omnigent live run). Then full G2: fresh clone → documented install → governed edit works, on a scrubbed machine with NO research repo present, NO dev-box deps leaking in. This is the gate R5 (site) depends on — never advertise an unverified install. |
| ~~S7~~ | ~~Seeded headline number~~ | ❌ CUT 2026-06-26 | No honest number exists pre-deployment; a seeded figure would be fabricated (G4). Report a real suppression number post-launch from `/hedwig-status`. |
| ~~S4~~ | ~~Demo/video (coding-agent task)~~ | ❌ CUT 2026-06-26 | 90s demo video is founder-produced, not a coding-agent deliverable. README folds into R7. The booth arc + multi-agent positioning line live in R7's README/BOOTH notes. |
| R7 | **Repo polish for acquisition-DD** | pending — parallel-safe | A north-star criterion ("clean code an engineer respects") and an acquisition checkpoint: an acquirer/engineer reads the repo before the site. Top-level README (what it is, the one number, install, the architecture in 5 lines), LICENSE, clean structure, no stray demo/debug files, no overclaim strings. Low-risk, runnable in parallel with anything. |

### REINVEST — depth bought with the runway (R1–R4, R6 DONE; R7 pending)

These hardened the contribution beyond the minimal spine. Full implementation
notes are in git history.

| # | Task | Status | One-line |
|---|------|--------|----------|
| R1 | **Outcome-signal attribution** | ✅ DONE | Verification-failure blame is git-diff-scoped (no false blame); agent reverting its own auto-applied edit is a regret signal needing no verify-command. The money-shot. |
| R2 | **Confidence handshake skill** | ✅ DONE | Agent self-declares confidence/self-pause before risky edits (`plugin/skills/confidence-checkin`); tighten-only (never loosens); silent graceful degrade when absent. |
| R3 | **Dense rule retrieval (fastembed)** | ✅ DONE (live-machine QC → S6) | `sc/retrieval.py` seam, `EmbeddingRanker` default + `KeywordRanker` fallback; semantic match where keyword overlap finds nothing; degrades to keyword if fastembed absent. |
| R4 | **Omnigent integration spike** | ✅ DONE (live-run QC → S6) | `integrations/omnigent/policy.py` — Hedwig's `decide()` as a learned Omnigent ALLOW/ASK policy; proven history-driven (a static authored policy can't flip ALLOW→ASK from traces). Not vendored; no plugin-dep impact. |

**GATE — ✅ BYPASSED (2026-06-25, settled by Claude Code hook docs).** Q1: `deny`+reason DOES feed back so the agent revises same-turn (→ R6 unblocked). Q3: `additionalContext` arrives next-turn, so `deny` is the self-correction primitive. Q2: native approve/deny is invisible to hooks (→ validates outcome-based learning). No live spike needed.

---

## EXECUTION CONTRACT (coding agent + QC team — this is the build spec)

The week's mission: ship a **powerhouse governance harness PLUS an
acquisition-legible integration artifact and a public launch surface** for the
AI Engineer World Fair — not a minimal demo. The strategic target is now
explicit: **build something acquirable by Databricks or Anthropic.** The spine
(S1–S3.5) is done; the week buys depth, an integration proof, and
distribution. Every task below has a **Definition of Done (DoD)** and an
independent **QC gate**. A task is not "done" until QC signs off against its
gate, on a clean machine, with the full suite green.

### The acquisition thesis (drives prioritization)

Verified from Omnigent's own docs (2026-06-25): their policies are
**"authored by users"** (YAML/Python), with **"no mechanism that learns,
calibrates, or adjusts policy from past outcomes or feedback"**; their
risk-escalation is **"user-authored arithmetic … no model infers the score"**;
decisions are **ALLOW / ASK / DENY**. Omnigent is a meta-harness
(orchestration + sandboxing + sharing) with a deliberately pluggable,
*unlearned* policy socket — and custom Python policies are a first-class
extension point on their server. **Hedwig is precisely the learned policy that
plugs into that socket and that they have not built.** The acqui-thesis:
*"Databricks/Neon built the harness and the policy socket; Hedwig is the
learned policy layer that drops in and adds the capability they're missing —
working today, not on a roadmap."* This is why R4 (integration) and the public
site exist; they make the thesis tangible to a buyer and to LinkedIn.

### The novelty, stated once (every demo string must ladder up to this)

Hedwig is the only coding-agent governance layer that **learns when to
interrupt you from real interaction outcomes**, and **corrects its own
over-trust**. Competitors (incl. Databricks Omnigent) *author* policies;
Hedwig *learns* them. The one sentence the whole booth serves:
> *"Hedwig auto-approved this edit. You reverted it. Watch it get more
> cautious on the next one like it."*
Three pillars that make it a powerhouse, not a toy: **(1) outcome-based
learning** (R1), **(2) a genuine bidirectional handshake** — the agent can
self-pause, not just obey (R2), **(3) semantic understanding of your rules**,
not keyword matching (R3), **plus** the agent self-correcting on a deny+reason
(R6). The founder packages these into the live demo + 90s video; R7 captures
the booth narrative.

### Global invariants (QC enforces on EVERY task, every PR)

- **G1 — Full suite green.** `PYTHONPATH=. .venv/bin/python -m pytest tests -q`
  passes (currently 367). No skips, no xfails introduced silently.
- **G2 — Clean-machine install.** Fresh clone + documented install → a governed
  edit works, with NO research repo present. QC runs this in a scrubbed env, not
  just the dev box (the prior zero-dep test was vacuous *because* it ran where
  sklearn was already installed — QC must verify in isolation).
- **G3 — Dependency wall holds (REVISED 2026-06-25).** Default path may import
  `fastembed`, `numpy`, and `scikit-learn` (the trace store + log-reg classifier
  are core novelty — see S5). It must NEVER import `torch`, `anthropic`, or
  `boto` on the decide path. The wall moved, it didn't vanish: the structural
  test asserts `torch`/`anthropic`/`boto` stay absent, and (per S5)
  `test_ml_policy_is_vendored` confirms the classifier IS present.
- **G4 — No overclaim.** No UI/README/demo string says "learned", "calibrated",
  "AI-powered", or "reinforcement" for a mechanism not actually running. This is
  the reviewer-148D discipline and an engineer at the booth WILL read the code.
- **G5 — Safety invariant intact.** Preferences only ever *add* caution except
  the one documented `auto_apply` loosening path. Any change touching the
  cascade re-runs `test_preference_coordinator.py` and the safety tests.

### Task contracts — PENDING work only (R1–R4 are ✅ DONE; see SPINE/REINVEST rows)

**S5 — Restore the log-reg classifier to the default path** (do FIRST).
- *DoD:* vendor `sc.ml_policy`; invert the not-vendored test → `test_ml_policy_is_vendored`; decide path loads the persisted `PolicyClassifier` and uses `select_scorer()` (heuristic <10 decisions, then learned — the CAIS cascade); regret also calls `classifier.update(approved=False, count_sample=False)`; add `scikit-learn`+`numpy` to plugin deps.
- *QC gate:* on a clean machine, after ≥10 decisions `select_scorer()` returns the learned scorer AND a regret shifts a *cross-file* (risk-similar) decision, not just same-file. No "learned" UI label until `ready()` fires (G4).

**S6 — Clean-machine install QC** (do after S5; the #1 Fair threat).
- *DoD:* close the live-machine items on R3 (fastembed pulls numpy+onnxruntime, NOT torch; ~30MB fetch works; offline degrades cleanly) and R4 (Omnigent live run). Full G2 on a scrubbed machine.
- *QC gate:* fresh clone → documented install → governed edit, no dev-box deps leaking. This is the gate R5 depends on.

**S7, S4 — CUT 2026-06-26.** No honest headline number exists pre-deployment
(report it post-launch from real `/hedwig-status` usage); the 90s demo video is
founder-produced, not a coding-agent task. README/booth notes fold into R7.

**R5 — Public landing site + plugin download** (needs S6).
- *DoD:* deployed static one-pager: one-liner, 30s how-it-works, working install, one-line local-only privacy statement. **No headline number and no embedded video pre-launch** — leave the number slot out (or "measured numbers coming as the community uses it"); founder drops the video in when ready. Don't fabricate a stat (G4).
- *QC gate:* **the advertised install is executed from the site on a clean machine and works** (a broken click is worse than no site). Privacy claim verified (grep for network calls). Static only — no login/dashboard/telemetry.

**R6 — Deny+reason self-correction loop** — ✅ DONE. On a *surfaced* edit that trips a high-risk gate, decide.py now emits `permissionDecision:"deny"` + an actionable plain-English reason so the agent revises same-turn, instead of silently passing through. **Gate (`_should_deny`):** security-sensitive OR blast_radius > 3 OR a prior reversal/verification-failure on this file — and NEVER on a brand-new file (you can't ask the agent to "narrow" a file it's creating; new files go to the human). **Retry cap (`MAX_DENY_RETRIES`=2):** prior denies for a (session, file) are counted from decisions.jsonl (`prior_deny_count`); past the cap, decide falls through to the native prompt so a stubborn disagreement always escalates to the human rather than looping. **Not on a handshake surface:** if the agent itself asked to check in (R2), we don't bounce its own request — it goes to the human. Reason reuses the S3.5 judgment voice, phrased as revise-or-escalate. Logged as a new `DENIED_VERDICT` ("denied") in decisions.jsonl, distinct from suppressed/surfaced, so /hedwig-status and the retry counter can tell them apart. **Re-scoring is fresh:** each decide call re-runs assess_risk + the scorer on the new edit, so a genuinely narrowed re-proposal that clears the gate auto-applies. Works for both the heuristic and learned scorer (the gate keys off risk/history, not the scorer). **Tests (5, `test_plugin_deny_loop.py`, subprocess + scrubbed):** security-sensitive edit denied with actionable reason; ordinary new file NOT denied (passthrough); low-risk still auto-approves (no regression); retry cap escalates to human after 2 denies; a previously-regretted ordinary file is gated into a deny. **Reconciliation:** 4 pre-R6 tests asserted "surfaced (passthrough)" for previously-regretted/security files — updated to a `_tightened()` helper (accepts surface OR deny, since deny is a stronger tightening) and the security-sensitive-new-file test now expects passthrough (new-file exemption). Proven standalone on a scrubbed interpreter. 405 tests green.

**R7 — Repo polish + README/booth notes** (parallel-safe): top-level README (what it is, install, architecture in 5 lines — NO fabricated number; "report real suppression rates post-launch"), LICENSE, clean structure, no stray files, no overclaim strings (G4). Also captures the booth narrative + multi-agent positioning line (the arc S4 would have scripted: low-risk auto-applies → risky surfaces with reason → reversal tightens (R1) → agent self-pauses (R2) → agent gets deny+reason and self-corrects (R6) → rule retrieved by meaning (R3)) as plain notes the founder uses for the live demo + video. Acquisition-DD checkpoint.

### Parallelization & sequencing

- **Strict order (3-day, locked at top):** S5 ✅ → S6 → R5 → R6 ✅ → R7 (R7 parallel-safe). S7/S4 cut. Remaining: **S6** (real-machine install QC), **R5** (site, needs S6), **R7** (repo polish, parallel-safe).
- **Parallel-safe:** R7 (repo polish) can run alongside anything. S6's two live-QC items (R3 fetch, R4 Omnigent run) can run while S5 is in progress if a second owner is free.
- **Daily checkpoint:** every task ends the day green-suite + clean-install (G1+G2). A red or uninstallable end-of-day is stop-the-line.
- **QC owns the scrubbed-machine G2 check** — the dev box has sklearn/torch/fastembed and will mask install regressions; QC verifies in isolation and is sole sign-off on each gate.
- **Out of scope (post-Fair):** semantic-drift detector, AutonomyPreferences unification, RL/bandit, MCP server, any multi-agent *build*. Finish early → harden the demo + S6, don't pull these forward.

### DEFER — post-Fair (the paper, not the booth)

| Task | Notes |
|------|-------|
| **Semantic-drift detector** (§2.3 embedding thrashing detection) | Stays deferred — depends on a clean intent stream and is invisible in a 30s demo. Can reuse R3's fastembed model when built. (The deny+reason self-correction loop moved UP to R6 — user decision 2026-06-25. The §2.2 confidence-handshake moved up to R2, already DONE.) |
| **AutonomyPreferences four-quadrant unification** (§3) | Pure refactor, 12-module blast radius across the safety invariant, zero demo value. Cut for the Fair; do last or never. (Embedding *retrieval* moved UP to R3 — standardized, not deferred.) |
| **RL / contextual-bandit scorer** | Feasible but not Fair-credible; blocker is reward engineering, not the algorithm. See the RL section below. |

### RL / contextual-bandit — answer for the applied scientist

**Feasible architecturally, not credible for the Fair.** It's a *contextual
bandit*, not full RL (the check-in is a single-step decision — no long-horizon
state, so value-function machinery buys nothing); arms = {auto-apply, soft
check-in, full check-in}, slots in as a third `PolicyScorer` adapter. **The
blocker is reward engineering, not the algorithm:** the outcome reward is still
sparse/noisy (R1 helps), bandits need off-policy exploration that's unsafe to do
in a governance tool, and at single-repo scale it never leaves exploration —
which would also trip the reviewer-148D overclaim line. Post-Fair / next-paper;
the honest precursor is the isotonic calibration already in `ml_policy.py`.

---
> # ⤵ BACKGROUND BELOW THIS LINE — NOT THE PLAN
> Condensed reference: the pivot rationale, the retained moat, and the protocol
> vision (post-Fair / paper material). The full day-by-day reasoning trail and
> resolved review notes were removed 2026-06-25 in a cleanup pass — they live in
> git history if needed. **Executors build only from the CANONICAL PLAN &
> EXECUTION CONTRACT above; this section is context, not instructions.**

## Background — the pivot

**CAIS:** Hedwig was a standalone governance CLI (`hw`) wrapping a Bedrock-backed
Claude agent. Novelty: a trace substrate that calibrates from real developer
decisions, a hypothesis bank, a regret loop, and per-repo (not per-developer)
scoping (ICC = 0.249 across SWE-chat sessions). Won 1 of 10 best-demo awards +
a World Fair invitation. Reviewer 148D's discipline still holds: don't overclaim
"learning" where heuristics run; preferences are per-repo, not per-developer.

**World Fair:** Hedwig becomes a **Claude Code plugin** — the same governance
loop riding on top of Claude Code via hooks. Why: (1) the defensible
contribution is the governance loop, not the agent; (2) Claude Code is where the
engineers are — zero switching cost; (3) it drops the AWS-SSO+Bedrock install
wall to **no credentials required** for the core loop (one pip install + a small
`fastembed` fetch; LLM features opt-in via `ANTHROPIC_API_KEY`).

## The moat retained from CAIS (verified accurate vs. code)

- Trace substrate (`decision_traces`, `sc/store/trace_store.py`).
- PolicyScorer seam (`HeuristicScorer` + online `PolicyClassifier`).
- Hypothesis bank, rule-based + LLM-noticer (`sc/hypothesis_bank.py`).
- Regret loop (`sc/regret.py`, `_corrected_regret_ids` persistence).
- 5-dim Preference taxonomy (`sc/preferences.py`).
- Cascade seam (`sc/run/helpers.py`), `trust_db` mixin store layer.

## The protocol vision (post-Fair — the paper, not the booth)

The research arc beyond the demo. All DEFERRED (see DEFER table above):

- **Bidirectional uncertainty handshake.** The agent self-declares confidence
  and can request its own pause; Hedwig honors it. (The cheap self-pause half is
  pulled forward as R2; the rest is deferred.)
- **Closed-loop self-correction.** On agent/harness disagreement, emit
  `permissionDecision:"deny"` + reason so the agent revises before committing.
  NOTE — VERIFIED: PreToolUse `additionalContext` reaches the model *after* the
  tool runs (next turn), so it can't drive same-turn revision; `deny`+reason is
  the only pre-commit primitive. (Confirmed via hook docs — see GATE bypass.)
- **Semantic-drift detector.** Embed consecutive intent declarations; rising
  drift on an unchanged task = thrashing → check-in. Can reuse R3's fastembed.
- **AutonomyPreferences → Preference unification.** Collapse the two preference
  systems under one `Mechanism` field. Pure refactor, 12-module blast radius,
  zero demo value — do last or never.
- **MCP server interface** so any meta-harness (e.g. Omnigent) can consume
  Hedwig as a learned policy provider. The acquisition end-state R4 prototypes.
