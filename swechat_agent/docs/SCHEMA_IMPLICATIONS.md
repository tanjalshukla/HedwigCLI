# Schema Implications

What Hedwig's 5-dim preference schema should change based on the SWE-chat
findings. Each section addresses one dimension. "Keep", "Change", "Add", or
"Remove" — with the specific evidence from FINDINGS.md.

---

## `Trigger` — what action pattern matches this preference

### Current shape
```python
change_patterns: tuple[str, ...]   # file-level change type
min/max_blast_radius: int | None
min/max_diff_size: int | None
requires_security_sensitive: bool | None
requires_new_file: bool | None
stages: tuple[str, ...]
```

### Finding
Q1 shows file-type features rank **low** among pushback predictors. File-level
action type explains little variance once intent and session history are
controlled for. The strongest turn-level signal is `intent_debug`/`intent_refactor`,
which is about the **task** being performed, not the file being touched.

Q2 shows failure reports are preceded by elevated bash activity (1.45× baseline),
not by write/edit patterns (1.19×). The relevant trigger is "agent ran several
bash commands" — not "agent wrote to a sensitive file."

### Changes

**Add to Trigger:**
- `task_intent`: enum — `debug` / `refactor` / `create` / `test` / `other`. Maps
  directly to `prompt_intent` in SWE-chat. Currently absent from Trigger entirely.
- `prev_bash_count_min`: int — minimum bash calls in the preceding agent block.
  A threshold of 3+ covers the elevated bash pattern preceding failure reports.

**Downgrade (consider removing or soft-deprecating):**
- `requires_security_sensitive`: No signal in SWE-chat data. Not a meaningful
  predictor of when developers push back. Keep only if there are non-SWE-chat
  reasons (compliance, regulatory) — mark as "governance constraint, not learned."
- `change_patterns`: Low predictive value. Keep for hard constraints (always_deny
  on certain patterns) but remove from learned preference triggers.

---

## `Condition` — when this preference fires (session context)

### Current shape
```python
required_coding_mode: CodingMode | None
required_persona: UserPersona | None
max_recent_denials: int | None
min_recent_approvals: int | None
max_uncertainty_band: float | None
```

### Finding
Q1: `cum_pushback_count` is the single strongest predictor (coef 2.77). The
current `max_recent_denials` captures this partially, but only for denials, not
corrections. Q4: pushback rate rises significantly from early to late session —
session position is a meaningful condition.

Q3: `required_persona` has no empirical backing. Behavioral clusters don't
match personas, and persona labels are essentially uncorrelated with behavioral
cluster membership.

### Changes

**Add to Condition:**
- `min_prior_pushback_count`: int — cumulative pushback count (corrections +
  rejections + failures) in the current session. More general than
  `max_recent_denials`. Covers Q1's top feature directly.
- `min_prior_failure_count`: int — cumulative failure reports. Q2 shows
  `cum_failure_count` has an independent positive coefficient for predicting
  future failure reports.
- `session_position_min`: float 0..1 — preference fires only after this
  fraction of the session has elapsed. Justified by Q4's within-session
  trend: late-session behavior is categorically different.

**Change:**
- `max_recent_denials` → consider merging into `min_prior_pushback_count`.
  The distinction between "denials" and "corrections" matters less than total
  pushback count.

**Remove or demote:**
- `required_persona`: Not empirically justified. UserPersona as currently
  defined is theory-driven, not data-driven. If kept, flag as "unvalidated
  prior" with low confidence in Lifecycle. See UserPersona note below.
- `required_coding_mode`: Marginally better than persona (coding_mode
  agreement was 59% in the old validation run), but still a session-level proxy.
  Keep with caveat: `VIBE` mode has reasonable support (82% of vibe sessions
  correctly identified); `HUMAN_ONLY` does not.

---

## `PreferenceAction` — what Hedwig does

### Current shape
```python
AUTO_APPLY / LIGHT_CONFIRM / FULL_CHECKIN / DEFER_TO_BATCH
```

### Finding
Q5 reveals that 33% of pushback turns are **positive redirects** — "I like it,
now do X" — which are currently treated as `non_pushback`. These turns combine
approval with new instruction. They don't trigger check-ins but they do contain
decision-altering information.

### Change

**Add:**
- `SOLICIT_DIRECTION`: A new action type that proactively asks for next-step
  direction before proceeding, without implying the previous action was wrong.
  Targets the "vibe" pattern where the developer is accepting everything — not
  to slow them down, but to surface the implicit redirect before it becomes a
  correction turn.

This is low priority but fills the gap Q5 identified: the 33% "other" category
is real behavior Hedwig currently can't respond to.

---

## `Scope` — where this preference applies

### Current shape
```python
level: "global" | "repo" | "session" | "path"
path_globs: tuple[str, ...]
session_id: str | None
```

### Finding
Q4 gives a split verdict:

- **Within-session trends are real** (p<0.001): pushback rises, prompts shorten,
  session behavior changes. `Scope.level = "session"` is justified.
- **Cross-session user stability is low** (ICC = 0.249): a developer's behavior
  varies too much across sessions to justify inferring stable per-user preferences
  from history. The `repo` scope is the right default — it averages over the
  developer's style on a specific codebase.

### Keep
- `"session"` scope is justified for session-state-dependent preferences
  (e.g., "after 3 corrections, auto-approve for the rest of this session").
- `"repo"` scope as the default is supported.

### Remove
- The implicit assumption that preferences are per-developer-style should be
  removed from documentation. Preferences are per-repo-behavior, not
  per-developer-personality. (This is already stated in CLAUDE.md but bears
  repeating since the `Condition.required_persona` field implies otherwise.)

---

## `Lifecycle` — provenance and confidence

### Current shape
```python
provenance: "user_explicit" | "inferred" | "default"
confidence: float  # 0..1
half_life_seconds: int  # 0 = no decay
```

### Finding
Q4 cross-session: ICC = 0.249 means inferred preferences should decay quickly.
A developer who pushes back heavily in one session may not push back at all in
the next. Any `provenance = "inferred"` preference should have a short half-life.

### Change

**Tighten half-life defaults for inferred preferences.** The current `half_life_seconds = 0` (no decay) is wrong for inferred preferences given ICC = 0.249. Recommended: inferred preferences should default to a half-life of 1-2 sessions (proxy: 7 days in wall-clock time, or reset at session end if session-scoped).

**Add a field:**
- `source_question`: str — which of Q1-Q5's signals drove this inference.
  This is diagnostic infrastructure for calibration, not a behavioral field.
  Low priority.

---

## `UserPersona` and `CodingMode` enums

### Finding (Q3 summary)
The 3-value `UserPersona` enum is not supported by behavioral clustering.
The natural behavioral split is **2-dimensional**: session intensity (short/long)
and engagement style (passive/active). SWE-chat's own labels are uncorrelated
with the behavioral clusters (all three persona types appear in all clusters).

`CodingMode` performs better — the `VIBE` cluster is behaviorally distinct —
but the `HUMAN_ONLY` end is almost undetectable with current proxies.

### Recommended changes

**UserPersona:** Replace or supplement the current 3-value enum with a
2-value behavioral split:
- `DELEGATING`: short sessions, low pushback, high agent authorship
- `ACTIVE`: long sessions, higher pushback, moderate agent authorship

If the 3-value schema must be kept for the paper, demote inferred persona to
low confidence and document that it is theory-driven.

**CodingMode:** Keep the current values but fix `HUMAN_ONLY` inference.
The threshold `_HUMAN_ONLY_APPROVAL_RATE_MAX = 0.15` is too low — the
session-level proxy inflates approval rates. Raise to 0.50 or use agent_percentage
directly when available.

**PushbackType:** Add two values:
- `SCOPE_CONSTRAINT`: "just X", "don't touch Y", "only" — currently absorbed
  into `correction` but behaviorally distinct
- `POSITIVE_REDIRECT`: approval with new direction — currently absorbed into
  `non_pushback` but contains instruction content

---

## Summary table

| Dimension | Action | Evidence |
|-----------|--------|----------|
| `Trigger.task_intent` | **Add** | Q1: top predictor |
| `Trigger.prev_bash_count_min` | **Add** | Q2: failure preceded by bash |
| `Trigger.change_patterns` | **Demote** | Q1: low predictive value |
| `Trigger.requires_security_sensitive` | **Demote/remove** | No signal |
| `Condition.min_prior_pushback_count` | **Add** | Q1: strongest predictor |
| `Condition.min_prior_failure_count` | **Add** | Q2: independent signal |
| `Condition.session_position_min` | **Add** | Q4: within-session trends |
| `Condition.required_persona` | **Demote** | Q3: not empirically backed |
| `PreferenceAction.SOLICIT_DIRECTION` | **Add** | Q5: 33% unclassified |
| `Scope.session` | **Keep** | Q4: within-session trends real |
| `Lifecycle.half_life` | **Tighten** | Q4: ICC=0.249, fast decay |
| `UserPersona` enum | **Replace** with 2-value intensity split | Q3 |
| `CodingMode.HUMAN_ONLY` threshold | **Raise** | Q3/baseline validation |
| `PushbackType` enum | **Add** SCOPE_CONSTRAINT + POSITIVE_REDIRECT | Q5 |
