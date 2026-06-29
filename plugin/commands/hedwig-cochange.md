---
description: Show files that historically change together in this repo (Hedwig's co-change signal)
---

Show which files Hedwig has seen edited together across sessions in this repo —
its co-change view. Useful for spotting hidden coupling and understanding why an
edit to one file might warrant a closer look at its neighbors.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-observe.py" cochange
```

Report the output verbatim — it's already formatted (each source file with the
files it co-changes with, and how many sessions each pairing appeared in).
Co-change is grouped by session (the plugin's unit of related work). If it says
there's no pattern yet, that's expected early — pairings emerge as files get
edited together across multiple sessions.
