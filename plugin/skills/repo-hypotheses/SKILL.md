---
name: repo-hypotheses
description: After a stretch of governed work in a repo (several edits Hedwig has auto-applied or surfaced this session), study the decision traces and propose standing rules Hedwig should consider — coding-style guidelines, repo facts, or when-to-pause preferences. This is the plugin's semantic noticer: you reason over the patterns; Hedwig stores your proposals as PENDING candidates the developer confirms later. Run occasionally, not every turn.
---

# Notice patterns worth turning into standing rules

Hedwig learns mechanical signals on its own (it tightens after a revert, tracks
approvals per file). What it can't see by itself is the *why* — the higher-level
pattern across a session: "this developer keeps narrowing scope to the service
layer," "tests always live under `tests/`," "they want a pause before touching
the migration files." You can read the decision traces and spot those. This
skill lets you propose them; Hedwig records them as **pending hypotheses** that
the developer reviews and confirms with `/hedwig-learn`.

Nothing you propose changes Hedwig's behavior until the developer confirms it.
So propose thoughtfully, but there's no risk in proposing.

## When to run

Run **occasionally** — after a meaningful stretch of governed edits in a session
(say, 5+ decisions), or when you notice a clear, repeated pattern in how the
developer is steering you. Not every turn. Once per session is plenty.

## How to do it — two steps

**Step 1 — read this session's decision traces.** Each line is one decision,
prefixed with a `[id]` you'll cite as evidence:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-notice.py" traces \
  "${CLAUDE_PROJECT_DIR:-$PWD}" "${CLAUDE_SESSION_ID:-}"
```

**Step 2 — propose grounded candidates.** Study the digest for genuine,
repeated patterns. For each, write one observation and cite the specific `[id]`s
that back it. Then pipe them in:

```bash
echo '{
  "cwd": "'"${CLAUDE_PROJECT_DIR:-$PWD}"'",
  "session_id": "'"${CLAUDE_SESSION_ID:-}"'",
  "candidates": [
    {
      "type": "preference",
      "text": "Pause for review before edits to files under migrations/",
      "driver": "pause_on_migrations",
      "rationale": "developer surfaced/denied every migration edit this session",
      "evidence_trace_ids": [12, 17, 23],
      "high_stakes": false
    },
    {
      "type": "behavioral_guideline",
      "text": "Keep service-layer changes separate from route changes",
      "driver": "separate_service_and_routes",
      "rationale": "developer split these every time",
      "evidence_trace_ids": [14, 19]
    },
    {
      "type": "logic_note",
      "text": "Tests for the API live in tests/test_api.py",
      "driver": "test_location_api",
      "rationale": "test edits all landed there",
      "evidence_trace_ids": [9]
    }
  ]
}' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-notice.py"
```

## The one hard rule: cite real trace IDs

**Every candidate must cite ≥1 real `[id]` from the Step-1 digest.** A proposal
with no valid citation is dropped on the floor — that's the anti-hallucination
gate. Don't invent patterns; only propose what the traces actually show.

## What to propose (the three types)

- `logic_note` — a fact about the codebase visible from the traces (where tests
  live, which files always change together). Stored directly as repo memory.
- `behavioral_guideline` — a coding-style pattern the developer enforces.
- `preference` — a when-to-pause governance rule. Set `"high_stakes": true`
  ONLY if wrongly applying it would touch security-sensitive paths — that raises
  the evidence bar before it can surface. You can never *lower* the bar.

Skip patterns Hedwig already tracks mechanically (raw approve/deny counts,
single reverts). Aim for the higher-level "why." If nothing rises above noise,
send an empty `candidates` list or just don't run — silence is fine.

No credentials, no network. If it fails, continue — it only affects whether a
suggestion gets recorded, never your edits.
