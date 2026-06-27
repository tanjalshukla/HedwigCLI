---
description: View or set Hedwig hard constraints (always deny / check-in / allow on file paths)
---

Hedwig hard constraints are non-negotiable rules on file paths: the plugin
enforces them before any risk scoring, so they override everything. Use this
command to inspect or set them. It runs locally and needs no credentials.

Parse the developer's request into one of these invocations and run it:

- **List current constraints** (default when they just ask what's set):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-rules.py" list
  ```

- **Add a constraint** — when they want to block, gate, or always-allow a path.
  Map their intent to a policy: block/never → `deny`, always-ask/review → `check_in`,
  always-allow/trust → `allow`. Translate the path they describe into a glob.
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-rules.py" add <deny|check_in|allow> <path-glob>
  ```
  Example: "never auto-edit anything under config/prod" →
  `... add deny "config/prod/**"`.

- **Remove a constraint**:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-rules.py" remove <path-glob>
  ```

Report the script's output verbatim. If you inferred a glob from a vague
description (e.g. "the prod config" → `config/prod/**`), state the exact glob
you set so the developer can confirm or correct it. The pattern uses
shell-glob matching against repo-relative paths.
