---
description: Show how many edit prompts Hedwig suppressed vs. surfaced this session
---

Run Hedwig's status script and report the result to the developer verbatim, then add one sentence of plain-English context.

Run this command (it requires no credentials and makes no network calls):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-status.py" --session "${CLAUDE_SESSION_ID:-}"
```

If `CLAUDE_SESSION_ID` is not set in the environment, run it without the `--session` flag to show all-time totals instead.

Present the dashboard the script prints verbatim (it's already formatted — the headline counts, the suppression bar, and the "why it surfaced these" plain-English reasons). Do not editorialize about whether the suppression rate is good or bad — it reflects this developer's own repo and the risk profile of the edits made so far. If the script reports that no edits have been governed yet, tell the developer to make a few edits and re-run `/hedwig-status`.

This is the one observability surface for Hedwig — there are no separate panels for hypotheses or preferences. Everything the developer needs to see at a glance is in this dashboard.
