# Hedwig — Technical Deep Dive

*Everything you need to speak competently about the system without reading code. How things flow, where data goes, what fires when.*

---

## The Core Problem

Coding agents have a calibration problem. Auto-approve everything and the agent applies edits you'd have caught — touches auth code, rewrites 300 lines, creates files that break something downstream. Manual review and you're clicking approve on every trivial line edit, going on autopilot and not actually reading.

Hedwig is a governance layer that sits between Claude Code and your files. For every edit the agent proposes, Hedwig decides whether to apply it automatically or pause for your review. The decision is based on a five-layer cascade, and the cascade adjusts based on what actually happened in this repo. The short version: **Claude Code without Hedwig is a choice between two bad settings. With Hedwig, it calibrates.**

---

## How Hooks Work (The Plumbing)

Before anything else, understand how the plugin physically runs.

Claude Code has a hooks system. When specific events happen — a session starts, a tool fires, a session ends — Claude Code runs shell commands and passes event data to them as JSON on stdin. Hedwig registers six hooks:

- **SessionStart** — runs `hedwig-context.py` to inject repo memory into the agent's context
- **UserPromptSubmit** — runs `hedwig-context.py` again if relevant context changed
- **PreToolUse (Edit/Write/MultiEdit)** — runs `hedwig-decide.py` to make the governance decision
- **PostToolUse (Edit/Write/MultiEdit)** — runs `hedwig-record.py` to detect reversals/regret
- **Stop** — runs `hedwig-verify.py` to check if anything just applied needs review
- **SessionEnd** — runs bookkeeping

Every hook script receives a JSON payload on stdin. The payload always includes `session_id`, `cwd`, `tool_name`, and `tool_input`. The hooks write their results to stdout as JSON — Claude Code reads that to decide whether to proceed or block.

Claude Code sets two critical environment variables for every hook subprocess:
- `CLAUDE_PLUGIN_ROOT` — the path to the installed plugin version (e.g. `~/.claude/plugins/cache/hedwig-marketplace/hedwig/0.1.19`)
- `CLAUDE_PLUGIN_DATA` — where the plugin stores persistent data (e.g. `~/.claude/plugins/data/hedwig-hedwig-marketplace`)

Slash commands also run as subprocesses but `CLAUDE_PLUGIN_DATA` may differ between hooks and commands (a Claude Code platform quirk). The sibling-dir scan in `_iter_jsonl` handles this — it always scans all `hedwig*/` directories under `~/.claude/plugins/data/` regardless of which specific path the env var points to.

---

## Where Data Lives

Everything persistent goes into two places: **JSONL files** for event logs and **SQLite** for structured state.

### JSONL Files (append-only event logs)

These live in `CLAUDE_PLUGIN_DATA/`:

- `decisions.jsonl` — one row per governed edit. Contains: `session_id`, `cwd`, `file_path`, `verdict` (suppressed/surfaced/denied), `score`, `reason`, `diff_size`, `blast_radius`, `change_pattern`, `is_security_sensitive`, `scorer` (heuristic/learned), `ts`. This is what `/hedwig-status` reads.
- `regret.jsonl` — one row per detected reversal. Written by `hedwig-record.py` when it sees a PostToolUse that undoes a previous auto-applied edit. Contains the original trace reference, the cwd, and a `regret_key` that prevents the same regret from being replayed twice.
- `sentinel.jsonl` — diagnostic heartbeat. Every hook writes a row with its event name and env snapshot (including `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`). Used for debugging; not consumed by any feature.
- `self_checkins.jsonl` — written by `hedwig-declare.py` when the agent declares low confidence. One row per declaration with `session_id`, `file_path`, and `confidence`. `hedwig-decide.py` reads the most recent row for a given (session, file) before deciding.

### SQLite (`trust.db`)

Opened via `open_trust_db()`. Tables that matter:

- **`decision_traces`** — the primary learning substrate. Every auto-applied edit gets a row here after the fact, recording the `PolicyInput` feature vector plus whether it was later reversed. This is what the classifier trains on.
- **`policy_history`** — aggregated stats per (repo, file): `effective_approvals`, `denials`, `avg_response_ms`, `avg_edit_distance`. The `effective_approvals` field is fractional — rubber-stamp approvals (under 5 seconds) count as 0.5. This is read by the classifier as prior-approval features.
- **`policy_models`** — the serialized `PolicyClassifier` for each repo. Loaded at decision time, updated after each outcome, written back. Pickle-serialized sklearn model + metadata (`sample_count`, `_corrected_regret_ids`).
- **`preferences`** — confirmed behavioral patterns. Each row has a `preference_type`, `file_pattern`, `change_pattern`, and `provenance` (user_explicit / inferred_user_confirmed / default). Used by `PreferenceCoordinator` at step 5 of the cascade.
- **`hypotheses`** — pending (unconfirmed) candidates. Evidence accumulates here. Once confidence is high enough, one surfaces to `/hedwig-learn` for confirmation.
- **`rules`** — hard constraints (`always_deny`, `always_check_in`, `always_allow`) keyed by glob pattern and repo root.
- **`leases`** — temporary auto-apply trust grants from prior approve+remember decisions (CLI only).
- **`security_paths`** — files explicitly flagged as security-sensitive by `/hedwig-scan`. Contains `file_path`, `reason`, `source` (user/scan/default).
- **`guidelines`** — free-text behavioral guidelines for this repo.
- **`logic_notes`** — factual statements about the repo (stored with embeddings for retrieval).

---

## The Decision Cascade — Exact Order

`hedwig-decide.py` runs on every PreToolUse for Edit/Write/MultiEdit. Here is every step in order.

**Step 0: Re-exec to capable interpreter**

Before reading stdin, `ensure_learned_interpreter()` checks whether the current Python has `numpy` and `scikit-learn`. If not, it looks for `~/.hedwig/venv/bin/python` (built by `/hedwig-setup`). If found and verified capable, it `os.execv()`s — replaces the current process with the capable interpreter. The new process re-runs the same script. The stdin pipe is inherited, so the payload isn't lost. If no capable interpreter exists, it continues with the heuristic-only path. A sentinel env var (`HEDWIG_REEXEC=1`) prevents infinite loops.

**Step 1: Parse the payload**

Read JSON from stdin. Extract `tool_name`, `cwd`, `session_id`, `tool_input` (which contains `file_path` plus the edit content). If parsing fails for any reason, output `{"continue": true}` and exit — a parse failure must never block an edit.

**Step 2: Filter tool type**

Only govern `Edit`, `Write`, `MultiEdit`. Anything else: `{"continue": true}` and exit. No friction on reads, shell commands, or other tools.

**Step 3: Hard constraints**

Call `db.hard_rules(repo_root)`. Walk the list and check each rule's glob pattern against `file_path`. Three outcomes:
- `always_deny` → emit `{"permissionDecision": "deny", "reason": "..."}` and exit
- `always_check_in` → emit nothing (surface to developer) and exit  
- `always_allow` → emit `{"permissionDecision": "allow"}` and exit

If no rule matches, continue.

**Step 4: Deterministic risk assessment**

Call `assess_risk(file_path, new_content, extra_security_paths=security_paths)`. This is pure Python — no model call, no network. It returns a `RiskSignals` object with:
- `diff_size` — character count of the change
- `blast_radius` — number of files that import `file_path` (computed by scanning `import` statements in the repo)
- `is_new_file` — does the file exist yet?
- `change_pattern` — one of eight categories (`api_change`, `data_model_change`, `config_change`, `dependency_update`, `error_handling`, `test_generation`, `documentation`, `general_change`) — determined by heuristic analysis of the file path and diff, no model call (see `classify_change_pattern` in `features.py`)
- `is_security_sensitive` — True if file name or content matches a security keyword list OR file is in `security_paths` from the database

Two keyword lists in `features.py`. Path hints (matched against the file path): `auth`, `permission`, `token`, `secret`, `password`, `credential`, `crypto`, `iam`. Content hints (matched against the file contents): `authorization`, `jwt`, `oauth`, `apikey`, `access_key`, `secret_key`, `password`, `encrypt`, `decrypt`. A file is security-sensitive if it matches either list, is in the `security_paths` table, or is passed in as an extra path. The detection only ever ADDS to the set — you can never make `is_security_sensitive` False by editing the file. The `/hedwig-scan` layer adds a second detection pass on top for files that miss on keywords.

**Step 5: Load history and build PolicyInput**

Read `db.policy_history(repo_root, file_path)` — the aggregated approval/denial stats for this file. Combine with the risk signals into a `PolicyInput` named tuple. This is the feature vector the scorer sees.

**Step 6: Select and run scorer**

`select_scorer(classifier)` decides which scorer runs:
- If `classifier.ready()` is True (sample_count ≥ 10) → use the `PolicyClassifier` (online logistic regression)
- Otherwise → use `HeuristicScorer`

The heuristic scorer is a weighted sum of the risk features with hand-tuned weights from the CAIS paper. It returns a score in roughly [-2, 2]; positive → auto-apply, negative → surface.

The learned classifier runs `sklearn.linear_model.SGDClassifier.predict_proba()` and returns a probability. Threshold is 0.5 — above that, auto-apply; below, surface.

Both scorers return `(action, reason)` where action is `"proceed"` or `"check_in"`.

**Step 7: Check confirmed preferences**

`PreferenceCoordinator.apply()` walks the confirmed preferences for this repo. Each preference can match on `file_pattern` (glob), `change_pattern`, or both. If a matching preference says `full_checkin` or `soft_checkin`, a `proceed` verdict is downgraded to `check_in`. A matching `auto_apply` preference can upgrade `check_in` to `proceed` under two tiers:

- **`user_explicit`** (developer deliberately authored this preference): respected unconditionally — no diff size or blast radius guards. If you said "don't ask me about this file," Hedwig won't, regardless of how large the change is. The Step 9 security floor still fires independently.
- **`inferred_user_confirmed`** (developer confirmed a Hedwig-inferred pattern via `/hedwig-learn`): loosens only when all four conditions hold: `diff_size < 20`, `blast_radius <= 2`, `is_security_sensitive == False`, `is_new_file == False`. The confirmed pattern may not have been intended for large or high-impact changes.

Built-in defaults and autonomy-derived preferences cannot loosen.

**Step 8: Confidence handshake**

Call `latest_self_checkin(session_id, file_path)` — reads `self_checkins.jsonl` for the most recent declaration from this session for this file. If the agent declared confidence ≤ 0.5, force `action = "check_in"`. Tighten-only.

**Step 9: Security floor**

```python
if action == "proceed" and risk.is_security_sensitive:
    action = "check_in"
```

This is the last thing before output and is unconditional. The classifier cannot override it. The developer cannot turn it off except with an explicit `always_allow` hard constraint.

**Step 10: Output**

Three possible outputs:
- **Auto-apply**: `{"permissionDecision": "allow", ...}` — Claude Code suppresses the native permission prompt. The developer never sees it. `hedwig-decide.py` logs the verdict as `suppressed` in `decisions.jsonl`.
- **Surface**: `{"permissionDecision": "ask", "permissionDecisionReason": "..."}` — Claude Code shows the permission prompt with Hedwig's reason attached. **This is emitted explicitly, not a silent exit.** The reason matters: `"ask"` *forces* the prompt even when the developer is in accept-edits mode or has an allow-rule for Edit/Write. A silent passthrough (exit 0, no output) would defer to the native flow, which auto-applies under accept-edits — silently bypassing the check-in Hedwig just decided. So a surfaced verdict always emits `"ask"`; silence is reserved for the cases where Hedwig has no opinion (non-governed tool, unparseable payload). Logged as the `surfaced` verdict.
- **Block (deny)**: `{"permissionDecision": "deny", "reason": "plain-English message to agent"}` — Claude Code rejects the edit and the message goes back to the agent. The agent revises in the same turn. This only fires for the highest-risk edits (security-sensitive AND new file, or blast_radius > 3, or file has prior reversals). Capped at `MAX_DENY_RETRIES` denies per file per session — past the cap, escalates to the human via `"ask"` instead of denying again.

---

## The Learning Loop — Exact Mechanics

**How the plugin recovers approve/deny signal**

The CLI sees every approve/deny click directly because it owns its own UI. The plugin can't — Claude Code owns the native permission prompt. But the plugin now recovers the equivalent signal through outcome inference:

- **Surfaced + PostToolUse fires** = developer approved the edit. `hedwig-record.py` correlates the PostToolUse event back to the original surfaced decision and feeds it as a positive classifier sample. Response time is measured (time between PreToolUse and PostToolUse) and flagged as a rubber stamp if under 5 seconds.
- **Surfaced + no PostToolUse by session end** = developer denied. `hedwig-verify.py` (Stop hook) scans all surfaced decisions and infers denials for those with no corresponding execution. Each inferred denial becomes a negative classifier sample with `count_sample=True` — real developer decisions, not corrections.
- **Auto-applied + no reversal** = positive sample (unchanged).
- **Auto-applied + reversal** = regret, negative correction sample.
- **Verification failure** = negative sample on the files in the failing change.

This gives the plugin full learning signal parity with the CLI on the per-decision axis.

**What counts as a reversal**

`hedwig-record.py` uses two detection strategies. First, it checks `structuredPatch` from the PostToolUse payload: if the patch has more deletions than additions on a file Hedwig suppressed this session, that's a likely partial or full undo. This catches more reversals than exact string matching. The fallback is the original exact-inverse check: prior edit was `old→new`, this edit is `new→old`.

Both write to `regret.jsonl`. The `_corrected_regret_ids` set in the classifier ensures each regret fires exactly one negative gradient, even across restarts.

**The classifier update cycle**

On a positive outcome (auto-applied or surfaced+approved):
1. `hedwig-record.py` calls `classifier.update(pi, approved=True, count_sample=True)`
2. `sample_count` increments by 1
3. If `sample_count` crosses 10, `ready()` returns True — learned classifier takes over
4. Classifier written back to `policy_models` in SQLite

On an inferred denial (surfaced, no PostToolUse at session end):
1. `hedwig-verify.py` reconstructs `PolicyInput` from the logged decision
2. Calls `update_classifier_for_decision(db, repo_root, pi, approved=False)`
3. `count_sample=True` — a real developer decision, not a correction

On a regret:
1. Decrements `effective_approvals` by the original approval weight (0.5 if rubber stamp)
2. Calls `classifier.update(pi, approved=False, count_sample=False)` — correction, not a new sample
3. Adds `regret_key` to `_corrected_regret_ids`

**Rubber stamps**

When a surfaced edit executes in under 5 seconds (measured from PreToolUse timestamp to PostToolUse), it's marked `rubber_stamp=True`. The classifier weights this approval at 0.5. If it later gets reverted, only 0.5 units need to be unwound.

---

## The Memory System — How Data Moves

**Session start injection**

When a Claude Code session starts, `hedwig-context.py` runs as a SessionStart hook. It:
1. Reads `db.guidelines(repo_root)` — all stored guidelines for this repo
2. Computes embedding similarity between the current session context (project dir name, recent file names from the CLAUDE_PROJECT_DIR) and each guideline using `fastembed`
3. Selects the top-k most relevant guidelines by cosine similarity
4. Formats them into a short text block
5. Outputs `{"additionalContext": "..."}` — Claude Code injects this into the agent's system prompt before the session begins

The total injected context is capped at 4000 characters. A fresh repo with no guidelines costs nothing.

**Writing to memory**

Guidelines and logic notes can be written from `/hedwig-scan` (which adds security facts), or manually via the CLI's `/hedwig-memory` commands. In the plugin, agent-proposed notes come through `/hedwig-notice`, which routes them through the hypothesis bank before anything lands in the database.

**The hypothesis bank**

The bank is the buffer between "Hedwig noticed something" and "this is now a standing rule." A hypothesis is a candidate `Preference` with a `type` (preference / behavioral_guideline / logic_note), a proposed rule, and an evidence count that grows as matching traces arrive. It accumulates evidence silently across sessions and never affects behavior until you confirm it.

**What "a pattern" actually means — and how it's detected**

There are two generators, and it's worth being precise about each because "it learns your patterns" is exactly the kind of claim a sharp engineer will probe.

*Generator 1 — rule-based (autonomous, no model).* This is the one that runs on every PostToolUse. It computes a `SessionSummary` from the session's traces (approval rate, mean review seconds, denial count, failure count, etc.) plus `pushback_counts` — and seeds candidates when a threshold trips. A "pattern" here is a concrete, counted signal, not a vibe. The five drivers:

| Driver | What it detects | Trigger |
|--------|----------------|---------|
| `scope_constraint` | You keep telling the agent to narrow scope ("just do X", "don't touch Y") | ≥3 scope-narrowing messages → proposes "always check in before multi-file changes" |
| `failure_reactive` | Edits this session caused failures | ≥2 failures → proposes tighter review |
| `deliberate_reviewer` | You review carefully before approving | mean review > 12s + approval rate > 0.6 |
| `rapid_approver` | You approve fast with no feedback | mean review < 3s + approval rate > 0.8 + zero feedback |
| `positive_redirect` | You signal "good, now do X" approvals | ≥3 positive-redirect messages |

The pushback classification (`classify_pushback`) is itself just phrase-matching against fixed lists — `_SCOPE_CONSTRAINT_PHRASES` contains "just do", "only the", "don't touch", "narrow scope", etc. No model decides what counts as pushback; it's deterministic keyword analysis of the developer's feedback text. (A nuance grounded in the SWE-chat data: turns classified as context-provision — pasting an error log — are explicitly *not* counted as pushback, because a third of apparent "pushback" text wasn't actually pushback.)

*Generator 2 — LLM-reasoned (`/hedwig-notice`, model in the loop).* This is the richer path that catches patterns rule-matching can't. Claude reasons over the session's decision-trace digest and proposes structured candidates piped back to `hedwig-notice.py`. The anti-hallucination gate: every candidate must cite at least one real trace ID, checked against the actual `session_traces` rows in SQLite — a cited ID that doesn't exist is rejected. The agent cannot invent its own justification.

**From candidate to ready**

A seeded candidate accumulates evidence as new traces match it. It's promoted to `ready_to_surface` only when confidence (`evidence_for / total`) ≥ 0.70 AND it has ≥ `MIN_EVIDENCE` (3) supporting traces — and security-relevant candidates require double that floor. A candidate whose confidence drops to ≤ 0.30 is pruned. Only then does the Stop hook nudge you to run `/hedwig-learn`.

**Confirmation is always yours**

Confirmed hypotheses go into the `preferences` table with `provenance=inferred_user_confirmed`. Declined ones stay in `hypotheses` with `status=rejected` — never deleted, for auditability. Nothing here changes a single governance decision until you confirm it.

---

## Security — The Full Threat Model

**Why risk assessment is model-independent**

`assess_risk()` in `sc/features.py` is pure Python that reads file contents and import graphs. It does not call any model. A compromised agent — whether from prompt injection or a bad dependency — cannot influence what this function returns. It sees the actual bytes being written, not the agent's description of them.

The agent's output is validated against a schema (`schema.py`) before the CLI acts on anything. On the plugin side, the hook runs in a separate subprocess and its output is parsed — the agent cannot push raw Python into the governance layer.

**Why the security floor is an `if` not a weight**

A classifier that learns "this developer trusts auth.py" would eventually auto-apply edits there. That's the threat. The floor is placed after the scorer precisely to prevent this — even a 0.99 classifier confidence is overridden by a single conditional check. The only escape hatch is an explicit `always_allow` hard constraint, which requires a deliberate developer action.

**Vendored governance core**

The plugin bundles a snapshot of the `sc/` governance core into `plugin/vendor/sc/`. This is the code that actually runs for governed edits. The agent's Claude Code context does not have write access to this code at runtime. `make sync-vendor` regenerates the vendor snapshot from `sc/` — you must run this after any `sc/` change or the plugin runs stale code.

---

## The Learned Classifier — What It Is and How It Works

**Algorithm: online logistic regression via `SGDClassifier`**

`PolicyClassifier` wraps sklearn's `SGDClassifier` with `loss="log_loss"`. This is logistic regression trained incrementally — one call to `partial_fit()` per developer decision, with no batch retraining. The model lives in SQLite (`policy_models` table, pickle-serialized) and is loaded fresh on every governance decision, updated in the same turn, and written back. It's one row per repo.

The classifier takes over from the heuristic scorer once `sample_count >= 10` real developer decisions have been recorded in that repo. Before that, `HeuristicScorer` runs exclusively.

**Why logistic regression, not a deep model?**

- Per-repo data is tiny — tens to hundreds of decisions, not millions. Deep models need orders of magnitude more data.
- Logistic regression has interpretable coefficients. `/hedwig-weights` shows you how each feature drifted from the cold-start baseline. You can reason about what the system learned.
- `partial_fit` is O(features) per update — fast enough to call synchronously inside a hook that must exit in under 100ms.
- Cold-start seeding is trivial: one zero-vector sample (approve=0) and one ones-vector sample (approve=1) gives both classes valid logits before any real data.

**The feature vector (14 features)**

`featurize(pi)` maps a `PolicyInput` to a normalized float array. All features are clipped or log-scaled to prevent outliers from dominating:

| Feature | What it measures | Normalization |
|---------|-----------------|---------------|
| `prior_approvals` | Effective approvals for this (repo, file, stage); rubber-stamp approvals count 0.5 | / 10, capped at 3 |
| `prior_denials` | Denials for this (repo, file, stage) | / 10, capped at 3 |
| `avg_response_ms` | Average developer response time for this file (fast = inattentive) | / 30,000ms, capped at 1 |
| `avg_edit_distance` | Avg diff size over file's history (0–1, already normalized in DB) | raw, capped at 1 |
| `diff_size_log` | Character count of this change | log1p / log1p(500) |
| `blast_radius` | Number of files importing this file | / 10, capped at 3 |
| `is_new_file` | File doesn't exist yet | 0.0 or 1.0 |
| `is_security_sensitive` | Security keyword match or in security_paths | 0.0 or 1.0 |
| `files_in_action` | Number of files in this edit batch | / 10, capped at 3 |
| `recent_denials` | Denials in this session in the last hour (3600s window, session-scoped) | / 3, capped at 1 |
| `verification_failure_rate` | Fraction of this file's last 50 verified traces that failed verification | raw [0, 1] |
| `model_confidence_avg` | Avg agent-declared confidence for this file (from self_checkins.jsonl) | raw [0, 1], defaults 0.5 |
| `change_pattern_risk` | Mapped from change_pattern: api_change/data_model_change = risky, test/docs = safe | mapped to [0, 1] |
| `model_risk_score` | Advisory adversarial-reviewer risk score (CLI-only path; defaults 0.5 in plugin) | raw [0, 1] |

The scaler is a `StandardScaler` fitted once at cold-seed time on a zeros-vector and a ones-vector, so all features start on the same scale. It's not refitted as new data arrives — the normalization is fixed at init.

**Probability calibration**

Raw `SGDClassifier.predict_proba()` output saturates quickly — after a few confident decisions, the model starts returning 0.02 or 0.98 rather than values that reflect actual uncertainty. Once 20 real decisions have accumulated, Hedwig fits an `IsotonicRegression` calibrator on `(raw_probability, true_label)` pairs. The calibrator maps the raw outputs back to probabilities that more accurately reflect observed approval rates. Below 20 decisions, raw probabilities are used directly.

The calibrator is refitted on every update once the threshold is crossed — it's cheap (monotone regression on at most a few hundred points) and keeps the calibration current.

**Cold-seed details**

`build_cold_classifier()` in `ml_policy.py` creates a fresh classifier seeded with exactly two synthetic samples:
- A zero-vector (`[0.0, 0.0, ...]`) labeled `approved=0` — represents a maximally risky edit
- A ones-vector (`[1.0, 1.0, ...]`) labeled `approved=1` — represents a maximally safe edit

This is the minimum viable seed: SGDClassifier requires both classes before it will produce valid `predict_proba` output. The seed exists only to satisfy that requirement — it carries no real-world prior, and it never matters in practice because `ready()` returns False until 10 real decisions accumulate, so the heuristic scorer (not the classifier) drives every cold-start decision. The CLAUDE.md invariant is: no synthetic training data beyond this two-sample seed.

**What `/hedwig-weights` shows**

`coef_delta()` diffs the current `clf.coef_[0]` against `prior_coef` (the coefficients at cold-seed time, stored in the model). A positive delta on `prior_approvals` means the classifier has learned to weight prior trust more heavily than baseline. A negative delta on `blast_radius` means it's learned to be more cautious about high-impact files than the cold-seed implied. If the classifier hasn't reached 10 samples, weights shows "not active" and no delta.

---

## What Each Command Actually Does

**`/hedwig-status`**
Reads `decisions.jsonl` across all sibling data dirs. Counts rows by verdict. Computes suppression rate. Extracts the `reason` field from surfaced rows for the "why it surfaced these" list. No database access — pure JSONL.

**`/hedwig-weights`**
Loads the `PolicyClassifier` from `policy_models` in SQLite. Extracts `coef_` from the SGDClassifier. Diffs it against the cold-start baseline coefficients. Formats as per-feature ▲/▼ drift. Shows "not active" if `sample_count < 10` or classifier isn't loaded.

**`/hedwig-retrospective`**
Reads `regret.jsonl`. Groups by file path. Joins against `decisions.jsonl` to get the original reason for each auto-applied edit that later got walked back.

**`/hedwig-rules`**
`list` → reads `db.hard_rules(repo_root)`, formats. `add` → `db.add_rule(...)`. `remove` → `db.remove_rule(...)`. These take effect immediately — the next `hedwig-decide.py` invocation will find the new rule in the database.

**`/hedwig-learn`**
Bare → reads the highest-confidence unconfirmed hypothesis from `hypotheses` table. `confirm` → moves it to `preferences` with `provenance=inferred_user_confirmed`. `reject` → marks `status=rejected`. `active` → reads all rows from `preferences` where `provenance != "default"`.

**`/hedwig-notice`**
Two paths. `traces` subcommand → reads `db.session_traces(repo_root, session_id)`, formats as a numbered digest with `[id]` prefixes (these IDs are what the agent must cite). stdin path → parses the agent's proposed candidates from JSON, validates each citation against real trace IDs, calls `ingest_llm_hypotheses()` to add valid candidates to the `hypotheses` table.

**`/hedwig-scan`**
Reads a JSON payload from stdin (produced by Claude reasoning over the file tree). Extracts proposed security paths and facts. Normalizes paths (strips `./` prefixes). Calls `db.set_security_paths(repo_root, source="scan", paths=[...], reasons={...})`. This is replace-by-source — a new scan replaces the previous scan's results but not paths added by other sources (e.g. user-explicit).

**`/hedwig-memory`**
`guidelines` → `db.guidelines(repo_root)`. `notes` → `db.logic_notes(repo_root)`. `security` → `db.security_paths(repo_root)`. Read-only display, no modification.

**`/hedwig-cochange`**
Reads all decision traces from `decisions.jsonl`. Groups file paths by `session_id`. Counts how often pairs of files appear in the same session. Ranks pairs by co-occurrence frequency. Displays top pairs. Note: the plugin uses `session_id` as the grouping unit (the CLI uses task string, but the plugin doesn't have a task concept — the whole session is one unit of work).

**`/hedwig-setup`**
Creates `~/.hedwig/venv/` using the current Python. `pip install numpy scikit-learn fastembed` into it. The hooks' `ensure_learned_interpreter()` will find this venv on the next invocation and re-exec into it, enabling the online classifier. Run once per machine. Re-running it after a machine migration or Python upgrade is safe.

---

## The Vendor Sync

`sc/` is the governance core. `plugin/vendor/sc/` is a bundled copy. When you edit anything in `sc/`, the plugin runs the old code until you run `make sync-vendor`. The CI (`make verify`) runs a diff check and fails if they're out of sync. This is the most common source of "I changed the code but nothing changed" bugs.

The sync script (`plugin/sync_vendor.py`) copies a specific list of modules. Not everything in `sc/` is vendored — only what the plugin hooks actually import. `NON_VENDORED_SC` lists the exclusions.

---

## Likely Questions

**"How is this different from just setting rules in CLAUDE.md?"**
CLAUDE.md is static — you write the rules before you know what you need. Hedwig builds them from outcomes. You don't author the policy; you confirm it after the system has accumulated enough evidence. The other difference: CLAUDE.md can't block an edit with a reason the agent can act on in the same turn, triggering a self-correction without a human in the loop.

**"Does it slow down Claude Code?"**
The cascade runs in 10-20ms from local SQLite with no network calls. There's no perceptible delay. The only setup cost is `/hedwig-setup`, which is once per machine.

**"What happens if Hedwig gets it wrong?"**
Auto-applies something it shouldn't: if you revert it, that's a regret event — one negative gradient update to the classifier. The system self-corrects. For security-sensitive files, the floor means "worst case is it surfaced something you didn't need to see" — never "applied something dangerous silently."

Surfaces something it shouldn't: one false-positive click. The decision registers as a positive training sample. The classifier learns.

**"Can it be tricked by a prompt injection?"**
No. Risk assessment is deterministic Python that reads actual file contents and import graphs. The agent never runs the scorer. The scorer runs in a separate subprocess from a vendored copy. A prompt-injected agent can claim something is safe; `features.py` doesn't listen to claims.

**"How long until it starts learning?"**
10 decisions. With active use, roughly one session. The heuristic covers cold start — deliberately permissive for low-risk edits so you see the value before any learning happens.

**"Does it work across multiple Claude sessions?"**
Yes. Everything is in SQLite per-repo. The classifier, confirmed preferences, hard constraints, security paths, co-change history, decision log all persist. The same governance state is shared across all Claude Code sessions on the same repo, including parallel agents. A regret recorded by one agent tightens the next one's decisions on that file.

**"Is my code sent anywhere?"**
Nothing leaves the machine. Governance runs from vendored Python. The database is local SQLite. `fastembed` runs locally. No cloud dependencies at runtime.

**"What's the difference between the plugin and the research CLI?"**
The plugin is the Claude Code integration — no credentials, fully local, hooks-based. What's at the fair is the plugin. The research CLI (`hw`) is a Bedrock-backed REPL that calls a hosted Claude model and runs a richer read/plan/apply/verify cascade. The CLI sees approve/deny clicks directly (its own UI). The plugin recovers equivalent signal through outcome inference: surfaced edits that execute = approvals, surfaced edits that don't = denials (inferred at session end), reversals and verify failures = regrets. The learning loop is effectively the same.

---

*Accurate as of v0.1.23. If the plugin version changes before the fair, re-verify the cascade steps and command list.*
