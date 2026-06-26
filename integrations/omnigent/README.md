# Hedwig as an Omnigent custom Python policy (R4 spike)

> **Status:** working spike, not a maintained adapter. It proves the acquisition
> thesis: *Databricks/Neon built the harness and the policy socket; Hedwig is
> the learned policy layer that drops into that socket — the capability they
> have explicitly not built.* Their policies are **authored**; ours **adapt
> from outcomes**.

## What this is

[Omnigent](https://github.com/omnigent-ai/omnigent) (Databricks AI team + Neon,
Apache-2.0, alpha) is a meta-harness over coding agents. It has a **first-class
custom-Python-policy extension point**, but its policies are *user-authored*
with **no mechanism that learns, calibrates, or adjusts from past outcomes**;
verdicts are **ALLOW / ASK / DENY**.

`policy.py` registers Hedwig's existing decide logic into that socket. For each
agent action Omnigent is about to take, Hedwig builds `RiskSignals`
(`sc.features.assess_risk`), reads this repo's **per-file outcome history**
(`TrustDB.policy_history`), scores them with `HeuristicScorer`
(`sc.policy`), and maps the result to ALLOW / ASK / DENY. The scoring code is
reused verbatim from `sc/` — the adapter only translates Omnigent's event in
and Omnigent's verdict out.

## Wiring (how to register it)

A policy is registered in Omnigent's YAML config as a `type: function` policy
whose `handler` is a dotted import path:

```yaml
policies:
  hedwig_learned:
    type: function
    handler: integrations.omnigent.policy.decide
```

The handler must be importable on Omnigent's server (`integrations.omnigent`
must be on `PYTHONPATH`, and `sc/` alongside it). The policy reads the Hedwig
trust store from `$HEDWIG_TRUST_DB` if set, else `./.sc/trust.db` (the
conventional per-repo location). Point it at the repo's existing trust.db so it
sees real history.

## ALLOW / ASK / DENY mapping

Hedwig's scorer emits `proceed` / `proceed_flag` / `check_in`. Omnigent expects
`ALLOW` / `ASK` / `DENY`. The mapping (in `policy._VERDICT`):

| Hedwig `PolicyAction` | Omnigent verdict | Meaning |
|-----------------------|------------------|---------|
| `proceed`             | `ALLOW`          | Auto-approve; no pause. |
| `proceed_flag`        | `ALLOW`          | Auto-approve, flagged for observability (still no pause — the flag is a trace annotation, not a user-facing block). |
| `check_in`            | `ASK`            | Surface for developer approval. |
| *(scorer never emits a hard deny)* | `DENY` | Reserved. A hard **DENY** in Hedwig comes from a CLI **hard constraint** (`always_deny`), not the scorer. This scorer-only spike therefore never returns DENY; the value is wired in `_VERDICT`'s fallback and documented so a future hard-constraint pass can emit it. |

The base mapping (proceed→ALLOW, check_in→ASK, deny→DENY) is the 1:1
correspondence verified in the migration doc's competitive note.

## How the decision is proven history-driven (not a static rule)

`tests/test_omnigent_adapter.py::test_recorded_denial_flips_same_action_to_ask`
runs the **same** Omnigent event twice against the **same** repo:

1. Clean trace history → `decide()` returns **ALLOW**.
2. Three denial traces are recorded on that exact file (the same write path the
   plugin's regret/reversal recorder uses).
3. The **identical** event now returns **ASK**, with a reason citing the
   recorded reversal.

Nothing about the action changed — only the trace history did. A static
authored policy cannot produce this flip; that is the whole point of the spike.
`test_denial_is_scoped_to_the_file` proves the tightening is per-file (a denial
on `util.py` does not taint a clean `other.py`).

## What is VERIFIED vs. ASSUMED

**Verified** (read from Omnigent `main`, 2026-06-25):

- Policy signature: `def policy(event: PolicyEvent) -> PolicyResponse | None`,
  imported types from `omnigent.policies.schema`. Source:
  `github.com/omnigent-ai/omnigent/blob/main/docs/POLICIES.md` and
  `omnigent/policies/schema.py`.
- Event shape: a dict with `type` (phase: `request` / `tool_call` /
  `tool_result` / `response` / `llm_request` / `llm_response`), `data`
  = `{"name": tool, "arguments": {...}}`, `context`, `session_state`. Source:
  `omnigent/policies/schema.py`.
- Return shape: a dict with required `result` ∈ `Literal["ALLOW","DENY","ASK"]`
  (case-insensitive), optional `reason` / `data` / `state_updates`; return
  `None` to abstain. Verdict enum `omnigent/spec/types.py`:
  `class PolicyAction(str, Enum): ALLOW="allow"; ASK="ask"; DENY="deny"`.
- File path lives in `data["arguments"]` under `path` (Omnigent tools) **or**
  `file_path` (Claude-native tools) — the exact dual-key fallback Omnigent's own
  builtin file policy uses. Tool name is `data["name"]`. Source:
  `omnigent/policies/builtins/safety.py` (`ask_on_os_tools`, `ask_on_add_policy`).
- Registration: YAML `type: function`, `handler: <dotted path>`, optional
  `factory_params`. Source: `docs/POLICIES.md`.

**Assumed** (could not confirm from docs — isolated behind the shims, flagged
`VERIFY AGAINST REAL OMNIGENT API` in `policy.py`):

- **Repo root / cwd field.** Omnigent's docs do not define a top-level
  `file_path`/`repo` event field; per-tool repo handling lives in specific
  builtins. We read `event["context"]["cwd"]` (fallback `["repo_root"]`, then
  process cwd) to root `blast_radius` scanning. If their context names this
  differently, fix it in `_extract_action()` only.
- **Write-tool content keys.** We read `old_string`/`new_string` (Edit-style) and
  `content`/`new_str` (write-style) from `arguments`. Verified that file tools
  key the *path*; the *content* key names are inferred from common tool schemas,
  not from a quoted Omnigent doc. `diff_size`/`is_new_file` degrade conservatively
  if absent.
- **`type: function` vs `type: python`.** The agent-side YAML uses
  `type: function`; the REST-API example used `"type": "python"`. Both reference
  the same dotted-handler mechanism per the docs; we document `function`.

The two-argument `(event, config)` factory form is supported by Omnigent but not
used here — `decide` is a plain zero-config evaluator.

## What QC must verify live (the spike was stubbed in dev)

The tests run against **stubbed** Omnigent event dicts (Omnigent is alpha and
not installed in the dev sandbox). On a real machine QC must:

1. **Pin & install Omnigent** (alpha, `curl|sh`) and record the commit tested.
2. **Register** `integrations.omnigent.policy.decide` as a `type: function`
   policy and confirm Omnigent imports and calls it (PYTHONPATH includes both
   `integrations` and `sc`).
3. **Confirm the real event shape** matches the assumptions above — especially
   the repo/cwd field and the write-tool content keys. If they differ, the only
   edits needed are in `_extract_action()` / `_response()`.
4. **Reproduce the history flip live**: seed a denial in the repo's trust.db,
   then trigger the same action twice through Omnigent and confirm ALLOW→ASK.
5. **No overclaim leaks into their UI** (G4): the `reason` strings say "adapts /
   got more cautious from outcome history", never "learned/AI/calibrated
   classifier". This spike uses only `HeuristicScorer` + per-file history; the
   cross-file `PolicyClassifier` is deliberately NOT on this path.

## Scope / honesty note (G4)

"Adapts from outcomes" here = the `HeuristicScorer` reading per-file
`decision_traces` history, where a recorded denial/regret tightens the next
like-action on that file (the `-0.7` denial weight in `sc/policy.py`). The
cross-file *generalizing* logistic classifier (`sc/ml_policy.PolicyClassifier`)
is a separate Tier-1 story and is **not** used here. Do not describe this spike
as running a trained classifier.
