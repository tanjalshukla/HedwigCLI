---
description: Show what Hedwig has stored as repo memory — guidelines, facts, and hard constraints it injects into context
---

Show the repo memory Hedwig has accumulated and injects into the agent's context
each session: behavioral guidelines, durable repo facts, and hard constraints.
This is what Hedwig "knows" about this repo.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-observe.py" memory
```

Report the output verbatim. If it's empty, Hedwig hasn't learned anything
durable yet — it builds memory as you work, or you can seed it with
`/hedwig-scan`. To view confirmed governance preferences specifically, use
`/hedwig-learn active`; to manage hard constraints, use `/hedwig-rules`.
