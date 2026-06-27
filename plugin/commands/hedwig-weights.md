---
description: Show how Hedwig's learned classifier has drifted from its cold-start baseline
---

Show which decision signals this repo's real interactions have shifted, and in
which direction (toward auto-applying or toward checking in). This is the
"watch it learn" surface — every bit of drift comes from actual decisions, not
hand-tuned weights. Runs locally, no credentials.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-observe.py" weights
```

Report the output verbatim — it's already formatted (per-feature drift with ▲
toward-trust / ▼ toward-caution arrows). If it says the learned classifier
isn't active, the developer hasn't run `hedwig-setup.py` yet (the scorer is
running the stdlib heuristic); pass that along. If it says there's no
meaningful drift yet, that's expected early — drift appears as real decisions
accumulate.
