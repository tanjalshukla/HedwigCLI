---
name: repo-scan
description: When you are new to a repository this session (the first substantive turn, or when Hedwig's SessionStart context asks for a scan), reason over the file tree to identify security-sensitive files that keyword matching would miss and durable repo facts worth remembering, and record them to Hedwig. This sharpens which edits Hedwig surfaces for review and what it recalls in future sessions. Run once per repo per session, early.
---

# Scan the repo so Hedwig governs it well

Hedwig decides which of your edits to auto-apply and which to surface for human
review. Part of that decision is whether a file is **security-sensitive** —
today that's detected by keyword (paths/contents containing `auth`, `token`,
`secret`, `crypto`, …). Keywords miss files that are security-critical but
plainly named: `signing.py`, `vault_helpers.py`, `session_store.py`, a payments
module, a request-verification middleware.

You can read and reason about code; the keyword list can't. This skill lets you
flag those files **once per session** so Hedwig always pauses edits to them — and
record durable facts about the repo that future sessions should know.

## What this changes (and what it can't)

Flagging a file **only adds caution**: Hedwig will surface edits to it for
review. It can **never** make a file *less* governed — you cannot clear a file
Hedwig already treats as sensitive. So there is no risk in flagging generously
when a file genuinely handles secrets, auth, money, or trust boundaries.

## When to run

Run **once, early**, when you first start working in a repo this session — after
you've read enough of the structure to judge it (a few key files, the layout),
before you start making edits. If Hedwig's session-start context explicitly asks
you to scan, do it then. Don't re-run every turn; one scan per session is enough.

## How to scan

1. Look over the project's source files — names and, where it's quick, contents.
2. Pick the files a careful security reviewer would want eyes on for any change:
   authentication, authorization, secrets/keys, cryptography or signing, session
   or token handling, payments/billing, anything enforcing a trust boundary.
   **Only include files keyword matching would miss or under-weight** — you don't
   need to list an obvious `auth.py`; Hedwig already has it.
3. Note a few durable, repo-specific facts worth recalling next session (where
   the auth flow lives, how config/secrets are loaded, a non-obvious invariant).
4. Record it all in one call:

```bash
echo '{
  "cwd": "'"${CLAUDE_PROJECT_DIR:-$PWD}"'",
  "security_paths": [
    {"path": "notes/signing.py", "reason": "HMAC request signing"},
    {"path": "notes/billing.py", "reason": "handles card tokens"}
  ],
  "facts": [
    "auth flow: every request passes through notes/signing.py before routing",
    "secrets are loaded from env in notes/config.py, never hardcoded"
  ]
}' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-scan.py"
```

Paths are repo-relative (as you'd reference them in an edit). Both
`security_paths` and `facts` are optional; send whichever you found. Keep it
focused — a handful of genuinely sensitive files beats an exhaustive list.

The command needs no credentials and makes no network calls. If it fails, just
continue — Hedwig falls back to keyword detection, so nothing breaks.
