---
description: Build Hedwig's learned-scorer interpreter so the online classifier runs on every edit
---

By default the hooks run under a bare `python3` that usually lacks `numpy` /
`scikit-learn`, so Hedwig falls back to the stdlib heuristic. This builds a
small dedicated virtualenv at `~/.hedwig/venv` with the classifier deps; the
hooks auto-discover it and re-exec under it — no shell config, survives plugin
updates. Run once per machine. Idempotent (re-running upgrades deps in place).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-setup.py"
```

This installs `numpy`, `scikit-learn`, and `fastembed` (none pull torch/GPU), so
the first run takes a minute. Report the output verbatim — on success it prints
the interpreter path and versions and confirms the online classifier is now
active. If it errors (no network, no `python3 -m venv`), pass the message along;
Hedwig keeps working on the heuristic until setup succeeds.
