#!/usr/bin/env python3
"""Hedwig confidence-handshake intake (R2 — the anti-allowlist pillar).

Claude calls this script (guided by the confidence-checkin skill) BEFORE a
risky edit to self-declare how sure it is — and, crucially, to *request its
own check-in* when it's uncertain. This is the bidirectional half of the
governance handshake: not "Hedwig rules, agent obeys", but "the agent can
pause itself". The decide hook (hedwig-decide.py) reads the most recent
declaration for a (session, file) and honors it.

Why a separate intake script instead of a hook: in plugin mode Hedwig does
not control Claude's prompt, so there is no IntentDeclaration stream to carry
`confidence` / `requesting_self_checkin`. A skill-invoked script is the
robust channel — it works when Claude complies and is simply absent (→ today's
inferred-intent behavior) when it doesn't. Best-effort by design.

Input (JSON on stdin), all fields optional except file:
  {
    "file": "src/auth.py",                 # required; repo-relative or absolute
    "session_id": "...",                   # optional; correlates with decide
    "cwd": "/abs/repo",                    # optional; for path normalization
    "confidence": 0.4,                     # optional float in [0,1]
    "requesting_self_checkin": true,       # optional bool — the self-pause primitive
    "reason": "unsure about token expiry"  # optional short string
  }

ZERO-DEP: stdlib only. No pydantic on this path (the vendored decide closure
must stay pydantic-free). Validation is hand-rolled and TOTAL — any malformed
field is dropped, never raised. Always exits 0: a bad declaration must never
break Claude's turn. A declaration can only ever make Hedwig *more* cautious
(decide.py enforces tighten-only), so a garbage payload is at worst a no-op.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _hedwig_common import SELF_CHECKINS_LOG, append_jsonl  # noqa: E402


def _coerce_confidence(value) -> float | None:
    """Return a float in [0,1] or None. Never raises on junk input."""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf != conf:  # NaN
        return None
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


def _rel_path(cwd: str, file_path: str) -> str:
    """Normalize to repo-relative so it matches what decide.py keys on."""
    try:
        target = Path(file_path)
        if target.is_absolute() and cwd:
            return str(target.relative_to(Path(cwd)))
    except (ValueError, OSError):
        pass
    return file_path


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    file_path = payload.get("file") or payload.get("file_path") or ""
    if not isinstance(file_path, str) or not file_path:
        return 0  # nothing to attach a declaration to

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    rel = _rel_path(cwd or "", file_path)

    confidence = _coerce_confidence(payload.get("confidence"))
    requesting = payload.get("requesting_self_checkin")
    requesting = bool(requesting) if isinstance(requesting, bool) else False
    reason = payload.get("reason")
    reason = reason.strip()[:280] if isinstance(reason, str) else ""

    # A declaration with neither a confidence nor a self-checkin request and
    # no reason carries no signal — drop it rather than log noise.
    if confidence is None and not requesting and not reason:
        return 0

    append_jsonl(
        SELF_CHECKINS_LOG,
        {
            "session_id": payload.get("session_id"),
            "cwd": cwd,
            "file_path": rel,
            "confidence": confidence,
            "requesting_self_checkin": requesting,
            "reason": reason,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
