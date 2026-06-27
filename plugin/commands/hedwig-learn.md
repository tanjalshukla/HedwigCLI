---
description: Review and confirm a governance pattern Hedwig noticed in how you work
---

Hedwig watches how you work and, when a pattern accumulates enough evidence,
surfaces one suggestion for your confirmation (it never changes behavior on its
own). This command is how you review and decide. It runs locally, no credentials.

Map the developer's intent to one invocation and run it:

- **Show what's waiting** (default — when they ask what Hedwig noticed, or run
  `/hedwig-learn` bare):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-learn.py" show
  ```

- **Confirm** the surfaced pattern (they say yes / accept / sounds good):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-learn.py" confirm
  ```

- **Decline** it (they say no / not that one):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-learn.py" reject
  ```

Report the script's output verbatim. A confirmed pattern becomes an active
preference that tightens how Hedwig governs similar edits going forward; a
declined one stays in the bank for transparency and is not applied. If nothing
is waiting, tell the developer Hedwig will surface a suggestion once it has
enough evidence — there's nothing to do right now.
