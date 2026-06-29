#!/usr/bin/env python3
"""Hedwig codebase scan intake — semantic security flags + durable repo facts.

Claude (guided by the `repo-scan` skill, prompted once per session) reasons over
the project's file tree and writes its findings back through this script. Two
kinds of finding, both AGENT-reasoned but persisted into the DETERMINISTIC
governance store so the decide hook reads them without any model call:

  * security_paths — repo-relative files the agent judged security-sensitive
    beyond the keyword heuristic (a crypto helper named `tokens.py` is caught by
    keywords; one named `vault_helpers.py` or `signing.py` is not). These ONLY
    add caution: features.is_security_sensitive ORs them with the keyword check
    and never lets them clear a keyword match — so even a prompt-injected scan
    can only make Hedwig more cautious (invariant 5 holds).

  * facts — durable, repo-specific notes ("payments flow goes through
    notes/billing.py", "auth uses a rotating key in config.py") stored as logic
    notes and injected into future sessions by hedwig-context.py. This is the
    plugin's write path for the repo-memory layer (the CLI had one; the plugin
    only read until now).

Input (JSON on stdin), produced by the skill:
    {
      "cwd": "/abs/project/root",
      "security_paths": [
        {"path": "notes/signing.py", "reason": "HMAC request signing"},
        {"path": "notes/billing.py", "reason": "handles card tokens"}
      ],
      "facts": ["payments route through notes/billing.py"]
    }
All fields optional; an empty scan is valid (clears nothing — replace-by-source).

Idempotent per session: re-running replaces this scan's security_paths (source
"agent_scan") so a fresh scan supersedes a stale one rather than accumulating.
Always exits 0; a scan failure must never break the session. Local, no Bedrock —
the reasoning already happened in Claude's turn; this only persists the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _hedwig_common import open_trust_db, repo_root_key  # noqa: E402

_SOURCE = "agent_scan"
_MAX_PATHS = 200  # a sane cap; a scan flagging more than this is miscalibrated


def _norm(path: str) -> str:
    """Normalize a repo-relative path so a flag matches the editor's `rel`
    regardless of a leading ./ or surrounding whitespace the agent may emit.
    decide compares by exact string, so the two must normalize the same way.
    Strips only a single leading './' (not arbitrary leading dots, so '.env'
    and '...x' survive)."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _coerce_security(raw) -> tuple[list[str], dict[str, str]]:
    """Pull (paths, {path: reason}) from the security_paths field, tolerating
    either a list of strings or a list of {path, reason} objects."""
    paths: list[str] = []
    reasons: dict[str, str] = {}
    if not isinstance(raw, list):
        return paths, reasons
    for item in raw[:_MAX_PATHS]:
        p = ""
        r = ""
        if isinstance(item, str):
            p = _norm(item)
        elif isinstance(item, dict):
            p = _norm(str(item.get("path") or ""))
            r = str(item.get("reason") or "").strip()
        if p:
            paths.append(p)
            if r:
                reasons[p] = r
    return paths, reasons


def _coerce_facts(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(f).strip() for f in raw if isinstance(f, (str, int, float)) and str(f).strip()]


def main() -> int:
    """Top-level guard — a skill-invoked intake must never exit non-zero or it
    would surface an error in the agent's turn. Any failure is swallowed."""
    try:
        return _main_inner()
    except Exception:
        return 0


def _main_inner() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stdout.write("Hedwig scan: no input.\n")
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.stdout.write("Hedwig scan: could not parse scan input.\n")
        return 0
    if not isinstance(payload, dict):
        return 0

    repo_root = repo_root_key(payload.get("cwd") or "")
    paths, reasons = _coerce_security(payload.get("security_paths"))
    facts = _coerce_facts(payload.get("facts"))

    try:
        db = open_trust_db()
    except Exception:
        sys.stdout.write("Hedwig scan: storage unavailable; nothing persisted.\n")
        return 0

    n_sec = 0
    try:
        # Replace-by-source so a fresh scan supersedes the previous one.
        n_sec = db.set_security_paths(repo_root, source=_SOURCE, paths=paths, reasons=reasons)
    except Exception:
        pass

    n_facts = 0
    if facts:
        try:
            n_facts = db.add_logic_notes(
                repo_root, source=_SOURCE, notes=facts, files=[],
            )
        except Exception:
            pass

    sys.stdout.write(
        f"Hedwig scan recorded: {n_sec} security-sensitive path"
        f"{'' if n_sec == 1 else 's'}, {n_facts} new repo fact"
        f"{'' if n_facts == 1 else 's'}. "
        "Security flags make Hedwig always surface edits to those files; "
        "facts are recalled in future sessions.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
