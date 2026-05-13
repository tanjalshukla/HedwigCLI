# Feature Catalog

Defines every feature used in the analysis scripts. For each feature: what it
measures, which SWE-chat field(s) it's computed from, known limitations, and
which research questions it feeds.

Fields come from two tables: `sessions` (5,851 rows) and `conversations`
(2,692,480 rows across all turn types). The join key is `session_id`.

---

## Turn-level features (unit = one user_prompt turn)

These are computed per `conversations` row where `turn_type == "user_prompt"`.

| Feature | Source field(s) | Computation | Limitations | Used in |
|---|---|---|---|---|
| `turn_index` | `conversation_turn_number` | Direct (0-based within session) | Null for non-conversational rows | Q1, Q4 |
| `prompt_word_count` | `word_count` | Direct | Already computed by SWE-chat | Q1 |
| `prompt_char_count` | `char_count` | Direct | Already computed | Q1 |
| `time_since_prev_turn_s` | `timestamp` | Diff between consecutive user_prompt timestamps in same session | Missing for ~16% of sessions; null if first turn | Q1, Q4 |
| `is_first_turn` | `is_first_turn` | Direct boolean | None | Q1 |
| `is_continuation` | `is_continuation` | Direct boolean — turn starts with "This session is being continued" | Proxy for session resumption, not fresh start | Q1 |
| `prompt_pushback` | `prompt_pushback` | Direct label | 7% of user_prompt rows unannotated (null) | Q1, Q2, Q5 |
| `pushback_is_any` | `prompt_pushback` | 1 if correction/rejection/failure_report, 0 if non_pushback | Target variable for Q1 classifier | Q1 |
| `is_failure_report` | `prompt_pushback` | 1 if failure_report | Subset of pushback_is_any | Q2 |
| `prompt_intent` | `prompt_intent` | Direct label | Not available for all rows | Q1, Q5 |
| `language` | `language` | Direct (detected natural language) | Recoded to "english" outside allowlist | Q1 |

### Preceding-agent-turn features (window = last assistant_response before this user_prompt)

Computed by looking back in turn_number order within the same session for the
nearest assistant_response turn.

| Feature | Source | Computation | Limitations | Used in |
|---|---|---|---|---|
| `prev_response_word_count` | `word_count` on preceding assistant_response | Direct | Null if no preceding assistant turn | Q1 |
| `prev_tool_call_count` | count of `tool_use` rows between prev user_prompt and this one | Count by session_id + turn_number range | Requires full session context in memory | Q1, Q2 |
| `prev_bash_count` | `tool_name == "Bash"` in preceding agent block | Count | Same as above | Q1, Q2 |
| `prev_write_edit_count` | `tool_name in {Write, Edit, NotebookEdit}` | Count | Same | Q1, Q2 |
| `prev_read_count` | `tool_name in {Read, Grep, Glob}` | Count | Same | Q1, Q2 |
| `prev_file_types` | `file_path` on preceding tool_use rows | Set of extensions, encoded as boolean flags for common types (.ts/.py/.go/.json/.md) | Path not always present | Q1, Q2 |
| `prev_bash_category` | `bash_category` on Bash tool_use rows | Most common category in preceding block (git/test-build/file-ops/package-manager/other) | Sparse for some sessions | Q2 |

### Cumulative session state (at the point of this turn)

| Feature | Computation | Limitations | Used in |
|---|---|---|---|
| `cum_turn_index` | How many user_prompt turns have occurred so far in this session | None | Q1, Q4 |
| `cum_pushback_count` | Count of pushback turns before this one in session | Requires ordered scan | Q1 |
| `cum_failure_report_count` | Count of failure_report turns before this one | Same | Q1, Q2 |
| `cum_correction_count` | Count of correction turns before this one | Same | Q1 |
| `cum_distinct_files_touched` | Distinct file paths in preceding tool_use rows | file_path sometimes null | Q1, Q4 |
| `session_position_fraction` | `turn_index / total_turns_in_session` | Requires knowing session length ahead of time | Q4 |
| `session_third` | floor(session_position_fraction * 3) → 0/1/2 (early/mid/late) | Same | Q4 |

---

## Session-level features (unit = one session)

Aggregated from turn-level data or taken directly from `sessions` table.

| Feature | Source | Computation | Limitations | Used in |
|---|---|---|---|---|
| `session_id` | `sessions.session_id` | Key | — | All |
| `duration_seconds` | `sessions.duration_seconds` | Direct | Missing for 15.7% of sessions | Q3, Q4 |
| `turn_count` | `sessions.turn_count` | Direct (conversational turns) | — | Q3 |
| `prompt_count` | `sessions.prompt_count` | Non-continuation user turns | — | Q3 |
| `agent_percentage` | `sessions.agent_percentage` | % of committed code agent-authored | 4.8% null | Q3 |
| `action_count` | `sessions.action_count` | Write/Edit/Bash calls | — | Q3 |
| `research_count` | `sessions.research_count` | Read/Grep/Glob calls | — | Q3 |
| `files_touched_count` | `sessions.files_touched_count` | Direct | — | Q3 |
| `pushback_rate` | computed | pushback turns / total user_prompt turns in session | Derived | Q3, Q4 |
| `failure_report_rate` | computed | failure_report turns / total user_prompt turns | Derived | Q3 |
| `correction_rate` | computed | correction turns / total user_prompt turns | Derived | Q3 |
| `mean_prompt_word_count` | computed | mean of `word_count` over user_prompt turns | Derived | Q3 |
| `mean_time_between_turns_s` | computed | mean of `time_since_prev_turn_s` | Missing where timestamps null | Q3, Q4 |
| `distinct_task_count` | computed | count of distinct prompt strings (exact) | Exact match is noisy — two phrasings of same task look distinct | Q3 |
| `user_id` | `sessions.user_id` | For cross-session stability check | — | Q4 |
| `user_persona_gt` | `sessions.user_persona` | Ground-truth label from SWE-chat | Used as sanity-check on clusters, not as target | Q3 |

---

## Features that cannot be computed (gaps)

| Feature wanted (from BRIEF) | Why not available | Impact |
|---|---|---|
| Per-turn edit distance | SWE-chat has no per-turn diff. `agent_percentage` is session-level only. | Q1 loses an important feature; use `agent_percentage` as a session-level stand-in |
| Blast radius | Requires Hedwig's call-graph analysis; no source graph in SWE-chat | Absent from Q1 model entirely |
| `is_security_sensitive` | Requires AST/pattern matching not in SWE-chat | Absent |
| Per-turn file attribution | SWE-chat attributes at commit level, not turn level | Use `prev_file_types` heuristic instead |
| Inter-session time gap | No `created_at` timestamps consistent across sessions to compute "time since last session" reliably | Q4 cross-session check limited |
| Semantic task distinctness | `distinct_task_count` uses exact string matching; semantic similarity would require embeddings (out of scope) | `distinct_task_count` may overcount |

---

## Notes on Q5 (feedback topics)

The `content` field of user_prompt turns is the raw prompt text. For pushback
turns, this contains the developer's actual words. The feature extraction for
Q5 is different: it operates on text directly rather than on numeric features.

- Corpus: `content` where `prompt_pushback in {correction, rejection, failure_report}`
- Filter: exclude continuation turns (`is_continuation == True`)
- Representation: TF-IDF over unigrams + bigrams, stop-word removed
- Target: topic clusters, not classification
