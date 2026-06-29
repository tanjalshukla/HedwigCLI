---
description: Scan this repo for security-sensitive files and durable facts, so Hedwig governs edits to them correctly
---

Reason over this project's files and record what Hedwig should treat as
security-sensitive — including files plain keyword matching would miss
(`signing.py`, `vault_helpers.py`, a payments module, a request-verification
middleware) — plus a few durable repo facts worth recalling in later sessions.

Do this:

1. Look over the source files (names, and contents where it's quick) enough to
   judge which handle authentication, authorization, secrets/keys, cryptography
   or signing, sessions/tokens, payments, or other trust boundaries — focusing
   on the ones a keyword check (`auth`, `token`, `secret`, `crypto`, …) would
   miss or under-weight.
2. Note a handful of durable, repo-specific facts (where the auth flow lives,
   how secrets are loaded, a non-obvious invariant).
3. Record both in one call:

```bash
echo '{
  "cwd": "'"${CLAUDE_PROJECT_DIR:-$PWD}"'",
  "security_paths": [
    {"path": "REPO/RELATIVE/PATH.py", "reason": "why it is sensitive"}
  ],
  "facts": ["a durable fact about this repo"]
}' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-scan.py"
```

Report the command's output verbatim — it confirms how many paths and facts
were recorded. Flagging a file only makes Hedwig **more** cautious about it (it
will surface edits there for review); it can never reduce governance, so flag
generously when a file genuinely handles secrets, auth, money, or trust. The
`repo-scan` skill has the full guidance; this command is the on-demand trigger.
