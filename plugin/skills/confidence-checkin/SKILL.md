---
name: confidence-checkin
description: Before making a code edit you are genuinely unsure about — a security-sensitive change, a guess at an API you haven't verified, a refactor with unclear blast radius, or any edit where you'd want a human to look before it lands — declare your confidence to Hedwig so it can surface the edit for review instead of auto-applying it. Use this when you want to pause yourself.
---

# Self-declare confidence before a risky edit

Hedwig governs your file edits: low-risk ones are auto-applied silently, riskier
ones are surfaced to the developer. By default Hedwig infers risk from the edit
itself. This skill lets **you** add a signal when you know something the edit
alone doesn't show — that you're uncertain, or that this one deserves a human
look before it lands.

This is a handshake, not an override. Declaring **only ever makes Hedwig more
cautious** (it can turn an auto-apply into a check-in). It can never make a
risky edit auto-apply — so there is no downside to declaring when in doubt.

## When to declare

Declare **before** the edit (in the same turn, just before the Edit/Write tool
call) when any of these hold:

- The change touches authentication, secrets, permissions, or other
  security-sensitive code.
- You are guessing at an API, signature, or behavior you have not verified.
- The blast radius is unclear or larger than you'd like.
- You would genuinely want a human to review this before it takes effect.

Do **not** declare for routine, confident edits — that just adds noise. Silence
is the normal case and means "I'm confident; let Hedwig's own risk assessment
decide."

## How to declare

Run this once per uncertain edit, immediately before the edit:

```bash
echo '{
  "file": "RELATIVE_OR_ABSOLUTE_PATH_YOU_ARE_ABOUT_TO_EDIT",
  "session_id": "'"${CLAUDE_SESSION_ID:-}"'",
  "cwd": "'"${CLAUDE_PROJECT_DIR:-$PWD}"'",
  "confidence": 0.4,
  "requesting_self_checkin": true,
  "reason": "one short phrase: why you are unsure"
}' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-declare.py"
```

Fields (all optional except `file`):

- `file` — the path you are about to edit. Required so Hedwig can match the
  declaration to the edit.
- `confidence` — your self-rated certainty, 0.0–1.0. A value at or below 0.5 is
  treated as a request for review.
- `requesting_self_checkin` — set `true` to explicitly ask Hedwig to surface
  this edit regardless of confidence. This is the direct "pause me" signal.
- `reason` — a short phrase shown to the developer verbatim. This is your voice
  at the review moment; make it specific ("unsure if token TTL is in seconds or
  ms"), not generic.

The command requires no credentials and makes no network calls. If it fails for
any reason, just proceed with your edit — a missing declaration simply means
Hedwig falls back to its own risk assessment.
