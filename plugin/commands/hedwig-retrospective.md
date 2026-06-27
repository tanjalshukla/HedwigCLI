---
description: Show Hedwig's regret events — edits it auto-applied that were later reverted or failed verification
---

Show where Hedwig was too trusting: edits it auto-applied that the agent then
reverted, or that failed an end-of-turn verification. Each one tightened
Hedwig's next decision on that file. Runs locally, no credentials.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-observe.py" retrospective
```

Report the output verbatim. An empty list is a good sign — it means nothing
Hedwig auto-applied had to be walked back in this repo. Don't editorialize
beyond what the script prints.
