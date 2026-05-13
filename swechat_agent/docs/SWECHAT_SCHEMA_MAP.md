# SWE-chat → Hedwig Schema Map

Maps every SWE-chat field used in extraction to Hedwig's `decision_traces`
column contract. Also documents fields present in one schema but absent in
the other ("gaps").

## Source tables used

SWE-chat exposes six tables via HuggingFace. The extractor uses three:

| Table | Why |
|---|---|
| `conversations` | Per-turn data: user prompts, pushback labels, tool calls |
| `sessions` | Session-level metadata: persona, coding mode, attribution |
| `commits` | Diff sizes for `edit_distance` proxy computation |

---

## Row-level field mapping

Each row in `data/swechat/sessions/<session_id>.jsonl` corresponds to one
conversational turn (`is_conversational = True` and `role = "user"`). These
are the turn types that carry user decisions.

| SWE-chat field | Hedwig field | Notes |
|---|---|---|
| `conversations.session_id` | `session_id` | Direct carry-through |
| `conversations.content` (user_prompt) | `task` | The user's literal prompt text |
| `conversations.prompt_pushback` | `_swechat_pushback` | Ground-truth pushback label; kept as shadow field |
| `conversations.prompt_intent` | — | No Hedwig equivalent; dropped |
| `conversations.turn_number` | `turn_number` | Kept for ordering; not in decision_traces schema |
| `sessions.user_persona` | `_swechat_persona` | Ground-truth persona label; kept as shadow field |
| `sessions.agent_percentage` | `_swechat_agent_pct` | Used to compute `_swechat_mode` (see below) |
| `sessions.session_success` | — | No Hedwig equivalent |
| `sessions.agent_lines` | — | No Hedwig equivalent |
| *(computed)* | `_swechat_mode` | Derived from `agent_percentage` using SWE-chat paper thresholds |
| *(computed)* | `user_decision` | Mapped from `prompt_pushback` (see mapping table below) |
| *(computed)* | `edit_distance` | Proxy from session-level `agent_percentage` (see proxy note) |
| *(computed)* | `user_feedback_text` | `content` field of user_prompt turns with pushback |
| *(computed)* | `change_type` | Derived from file extension of session's `files_touched` |
| *(computed)* | `stage` | Heuristic: "apply" for write/edit tool calls, "read" otherwise |

---

## Pushback → user_decision mapping

SWE-chat labels each user prompt with a `prompt_pushback` category.
Hedwig's `user_decision` is broader (approve / deny / interrupt). Mapped as:

| `prompt_pushback` | `user_decision` | Rationale |
|---|---|---|
| `non_pushback` | `approve` | No pushback = acceptance |
| `correction` | `approve_with_feedback` | Approval with inline correction |
| `rejection` | `deny` | Outright rejection |
| `failure_report` | `deny` | Developer reporting agent failure = effective denial |
| `pacing_complaint` | `approve_with_feedback` | Slowing agent but not blocking |
| `takeover` | `interrupt` | Human takes over = interrupt |
| `requirement_change` | `approve_with_feedback` | Goal shift, not flat rejection |
| `null` / missing | `approve` | Default assumption for turns without annotation |

---

## Coding mode derivation from `agent_percentage`

SWE-chat's `user_persona` session-level label is ground truth for persona.
For coding mode, SWE-chat doesn't expose a `coding_mode` column directly —
it is derived from `agent_percentage` per the paper's definitions:

| `agent_percentage` | `_swechat_mode` |
|---|---|
| >= 90 | `vibe` |
| > 10 and < 90 | `collaborative` |
| <= 10 | `human_only` |
| null / missing | `collaborative` (default) |

---

## edit_distance proxy

Hedwig's `edit_distance` is a 0..1 float representing how much the developer
rewrote the agent's output in a given turn. SWE-chat does not expose a
per-turn edit distance. Two proxies are used, in preference order:

1. **Session-level proxy** (used when available): `1.0 - (agent_percentage / 100.0)`.
   If the agent authored 80% of the session's code, the human-edit proxy is 0.20.
   This is a constant per session, applied to every turn — a known limitation.

2. **Fallback**: `0.0` when `agent_percentage` is null.

**Documented limitation**: This proxy conflates session-level authorship with
per-turn editing behavior. It will overstate edit_distance for sessions where
one heavy-edit turn dominates. The proxy is documented and reproducible.

---

## Gaps: fields Hedwig has that SWE-chat lacks

These Hedwig `decision_traces` columns are absent from SWE-chat and cannot
be computed from available data. They are **excluded** from extracted rows.

| Hedwig column | Gap reason |
|---|---|
| `blast_radius` | Requires Hedwig's file-graph analysis; SWE-chat has no call-graph data |
| `is_security_sensitive` | Requires Hedwig's pattern matching on AST; not available |
| `policy_score` | Hedwig-internal; computed by PolicyScorer |
| `policy_reasons` | Hedwig-internal; not applicable |
| `stage` | Approximated by heuristic (see above); real stage requires Hedwig runtime |
| `action_type` | Approximated from tool_name; not always exact |

---

## Gaps: fields SWE-chat has that Hedwig lacks

These SWE-chat fields are carried through as shadow `_swechat_*` fields to
enable comparison. They have no current home in `decision_traces`.

| SWE-chat field | Shadow field | Use in validation |
|---|---|---|
| `prompt_pushback` | `_swechat_pushback` | Ground truth for `classify_pushback` comparison |
| `user_persona` | `_swechat_persona` | Ground truth for `infer_user_persona` comparison |
| *(derived)* `_swechat_mode` | `_swechat_mode` | Ground truth for `infer_coding_mode` comparison |
| `prompt_intent` | `_swechat_intent` | Informational; not used in inference |
| `session_success` | `_swechat_success` | Informational; not used in inference |

---

## Coverage caveats

- Sessions with `user_persona = null` are extracted but have no persona
  ground truth; they are excluded from persona agreement calculations.
- Turns with `prompt_pushback = null` are treated as `non_pushback` in
  extraction but excluded from pushback agreement calculations.
- Sessions with fewer than 3 user turns are extracted but excluded from
  persona/mode agreement calculations (Hedwig returns `UNKNOWN` for n_turns < 3).
